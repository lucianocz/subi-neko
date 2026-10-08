import asyncio
import gzip
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.core.config import settings
from app.metadata.base import (
    Character,
    CharacterGender,
    CharacterRole,
    Episode,
    MediaType,
    MetadataProvider,
    SearchResult,
    SeriesDetails,
)
from app.metadata.cache import MetadataCache

logger = logging.getLogger(__name__)

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_TITLES_URL = "https://anidb.net/api/anime-titles.xml.gz"
_API_URL = "http://api.anidb.net:9001/httpapi"
_HEADERS = {"User-Agent": "subineko/1"}

_ANIDB_FORMAT_MAP: dict[str, MediaType] = {
    "TV Series": MediaType.TV,
    "Web": MediaType.ONA,
    "OVA": MediaType.OVA,
    "Movie": MediaType.MOVIE,
    "TV Special": MediaType.SPECIAL,
    "Music Video": MediaType.MUSIC,
    "Other": MediaType.UNKNOWN,
}

_CHAR_ROLE_MAP = {
    "main character": CharacterRole.MAIN,
    "appears in": CharacterRole.BACKGROUND,
    "cameo appearance in": CharacterRole.BACKGROUND,
}

_GENDER_MAP: dict[str, CharacterGender] = {
    "male": CharacterGender.MALE,
    "female": CharacterGender.FEMALE,
}

_MARKUP_RE = re.compile(
    r"\[url=[^\]]*\](.*?)\[/url\]"
    r"|\[i\](.*?)\[/i\]"
    r"|\[b\](.*?)\[/b\]"
    r"|\[[^\]]+\]",
    re.DOTALL,
)


def _strip_anidb_markup(text: str) -> str:
    def _repl(m: re.Match) -> str:
        for g in m.groups():
            if g is not None:
                return g
        return ""
    return _MARKUP_RE.sub(_repl, text).strip()


class AniDBProvider(MetadataProvider):
    supports_search = True
    supports_characters = True
    supports_episodes = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._cache = MetadataCache(settings.config_root / "metadata_cache.db")
        self._api_lock = asyncio.Lock()
        self._last_api_call: float = 0.0

    # ------------------------------------------------------------------
    # Titles dump helpers
    # ------------------------------------------------------------------

    async def _load_titles(self) -> list[dict]:
        # v2: entries now carry "english" (v1 cache entries lack it)
        cached = self._cache.get("anidb:titles:v2")
        if cached is not None:
            return cached

        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(_TITLES_URL, headers=_HEADERS)
            resp.raise_for_status()

        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        root = ET.fromstring(raw.decode("utf-8"))
        entries = self._parse_titles_dump(root)

        self._cache.set("anidb:titles:v2", entries, ttl=86400)
        return entries

    @staticmethod
    def _parse_titles_dump(root: ET.Element) -> list[dict]:
        entries: list[dict] = []
        for anime in root.findall("anime"):
            aid = anime.get("aid", "")
            main_title = ""
            native_title = None
            english_title = None
            synonyms: list[str] = []

            for t in anime.findall("title"):
                lang = t.get(_XML_LANG, "")
                ttype = t.get("type", "")
                text = (t.text or "").strip()
                if not text:
                    continue
                if ttype == "main":
                    main_title = text
                    continue
                if ttype == "official" and lang == "ja" and native_title is None:
                    native_title = text
                    continue
                # Only official English titles — "syn"/"short" en entries are fan aliases.
                if ttype == "official" and lang == "en" and english_title is None:
                    english_title = text
                if ttype in ("official", "short", "syn"):
                    synonyms.append(text)

            if aid and main_title:
                entries.append({
                    "aid": aid,
                    "main": main_title,
                    "native": native_title,
                    "english": english_title,
                    "synonyms": synonyms,
                })
        return entries

    # ------------------------------------------------------------------
    # HTTP API helper
    # ------------------------------------------------------------------

    async def _fetch_anime(self, provider_id: str) -> dict:
        cache_key = f"anidb:anime:{provider_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with self._api_lock:
            # Double-check after acquiring lock
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            # Rate limiting: enforce 2s minimum between requests
            elapsed = time.monotonic() - self._last_api_call
            if elapsed < 2.0:
                await asyncio.sleep(2.0 - elapsed)

            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                resp = await client.get(
                    _API_URL,
                    params={
                        "request": "anime",
                        "client": "subineko",
                        "clientver": "1",
                        "protover": "1",
                        "aid": provider_id,
                    },
                    headers=_HEADERS,
                )
                resp.raise_for_status()

            self._last_api_call = time.monotonic()

        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        root = ET.fromstring(raw.decode("utf-8"))

        if root.tag == "error":
            raise RuntimeError(f"AniDB API error: {root.text}")

        result = self._parse_anime_xml(root)
        self._cache.set(cache_key, result, ttl=86400 * 7)
        return result

    def _parse_anime_xml(self, root: ET.Element) -> dict:
        def _text(tag: str) -> str | None:
            el = root.find(tag)
            return (el.text or "").strip() or None if el is not None else None

        # Titles
        main_title = ""
        native_title = None
        synonyms: list[str] = []
        titles_el = root.find("titles")
        if titles_el is not None:
            for t in titles_el.findall("title"):
                lang = t.get(_XML_LANG, "")
                ttype = t.get("type", "")
                text = (t.text or "").strip()
                if not text:
                    continue
                if ttype == "main":
                    main_title = text
                elif lang == "ja" and ttype == "official" and native_title is None:
                    native_title = text
                elif ttype in ("official", "short", "syn"):
                    synonyms.append(text)

        # Year
        year: int | None = None
        startdate = _text("startdate")
        if startdate:
            try:
                year = int(startdate.split("-")[0])
            except (ValueError, IndexError):
                pass

        # Media type
        ttype_el = root.find("type")
        media_type = MediaType.UNKNOWN
        if ttype_el is not None and ttype_el.text:
            media_type = _ANIDB_FORMAT_MAP.get(ttype_el.text.strip(), MediaType.UNKNOWN)

        # Episode count
        episode_count: int | None = None
        ep_el = root.find("episodecount")
        if ep_el is not None and ep_el.text:
            try:
                episode_count = int(ep_el.text.strip())
            except ValueError:
                pass

        # Description
        desc_el = root.find("description")
        description: str | None = None
        if desc_el is not None and desc_el.text:
            description = _strip_anidb_markup(desc_el.text)

        # Characters
        characters: list[dict] = []
        chars_el = root.find("characters")
        if chars_el is not None:
            for char_el in chars_el.findall("character"):
                char_type = (char_el.get("type") or "").lower()
                role = _CHAR_ROLE_MAP.get(char_type, CharacterRole.SUPPORTING).value

                name_el = char_el.find("name")
                char_name = (name_el.text or "").strip() if name_el is not None else ""
                if not char_name:
                    continue

                gender_el = char_el.find("gender")
                gender_raw = (gender_el.text or "").strip().lower() if gender_el is not None else None
                gender = _GENDER_MAP.get(gender_raw).value if gender_raw and gender_raw in _GENDER_MAP else None

                char_desc_el = char_el.find("description")
                char_desc: str | None = None
                if char_desc_el is not None and char_desc_el.text:
                    char_desc = _strip_anidb_markup(char_desc_el.text)

                char_type_el = char_el.find("charactertype")
                character_type = (char_type_el.text or "").strip() or None if char_type_el is not None else None

                seiyuu_el = char_el.find("seiyuu")
                voice_actor = (seiyuu_el.text or "").strip() or None if seiyuu_el is not None else None

                char_id = char_el.get("id")

                characters.append({
                    "provider_id": char_id,
                    "name": char_name,
                    "name_native": None,
                    "role": role,
                    "gender": gender,
                    "description": char_desc,
                    "voice_actor": voice_actor,
                    "character_type": character_type,
                })

        # Episodes — the same cached XML carries per-episode titles/airdates.
        # Only regular episodes (epno type="1") have an integer number.
        episodes: list[dict] = []
        eps_el = root.find("episodes")
        if eps_el is not None:
            for ep_el in eps_el.findall("episode"):
                epno_el = ep_el.find("epno")
                if epno_el is None or (epno_el.get("type") or "1") != "1":
                    continue
                try:
                    number = int((epno_el.text or "").strip())
                except ValueError:
                    continue

                ep_title = None
                ep_title_native = None
                for t in ep_el.findall("title"):
                    lang = t.get(_XML_LANG, "")
                    text = (t.text or "").strip()
                    if not text:
                        continue
                    if lang == "en" and ep_title is None:
                        ep_title = text
                    elif lang == "ja" and ep_title_native is None:
                        ep_title_native = text

                airdate_el = ep_el.find("airdate")
                air_date = (airdate_el.text or "").strip() or None if airdate_el is not None else None

                episodes.append({
                    "number": number,
                    "title": ep_title,
                    "title_native": ep_title_native,
                    "air_date": air_date,
                })
        episodes.sort(key=lambda e: e["number"])

        details = {
            "provider_id": root.get("id", ""),
            "title": main_title,
            "title_native": native_title,
            "title_synonyms": synonyms,
            "year": year,
            "media_type": media_type.value,
            "description": description,
            "episode_count": episode_count,
        }

        return {"details": details, "characters": characters, "episodes": episodes}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: str) -> list[SearchResult]:
        entries = await self._load_titles()
        q = query.lower()

        main_hits: list[dict] = []
        syn_hits: list[dict] = []

        for entry in entries:
            if q in entry["main"].lower():
                main_hits.append(entry)
            elif any(q in s.lower() for s in entry["synonyms"]):
                syn_hits.append(entry)

        combined = (main_hits + syn_hits)[:20]
        return [
            SearchResult(
                provider_id=e["aid"],
                title=e["main"],
                title_native=e["native"],
                # The titles dump has no start date; a year would need one
                # rate-limited HTTP API call per result, so it stays unknown.
                year=None,
                media_type=MediaType.UNKNOWN,
                title_english=e.get("english"),
            )
            for e in combined
        ]

    async def get_details(self, provider_id: str) -> SeriesDetails:
        data = await self._fetch_anime(provider_id)
        d = data["details"]
        return SeriesDetails(
            provider_id=d["provider_id"],
            title=d["title"],
            title_native=d["title_native"],
            title_synonyms=d["title_synonyms"],
            year=d["year"],
            media_type=MediaType(d["media_type"]),
            description=d["description"],
            episode_count=d["episode_count"],
        )

    async def get_characters(self, provider_id: str) -> list[Character]:
        data = await self._fetch_anime(provider_id)
        return [
            Character(
                name=c["name"],
                provider_id=c["provider_id"],
                name_native=c["name_native"],
                role=CharacterRole(c["role"]) if c["role"] else None,
                gender=CharacterGender(c["gender"]) if c["gender"] else None,
                description=c["description"],
                voice_actor=c.get("voice_actor"),
                character_type=c.get("character_type"),
            )
            for c in data["characters"]
        ]

    async def get_episodes(self, provider_id: str) -> list[Episode]:
        data = await self._fetch_anime(provider_id)
        # Entries cached before episode support lack the key — tolerate them.
        return [
            Episode(
                number=e["number"],
                title=e.get("title"),
                title_native=e.get("title_native"),
                air_date=e.get("air_date"),
            )
            for e in data.get("episodes", [])
        ]

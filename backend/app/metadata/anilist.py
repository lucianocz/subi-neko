import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.metadata.base import (
    Character,
    CharacterGender,
    CharacterRole,
    MediaType,
    MetadataProvider,
    SearchResult,
    SeriesDetails,
)
from app.metadata.cache import MetadataCache

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://graphql.anilist.co"

_SEARCH_QUERY = """
query Search($q: String) {
  Page(page: 1, perPage: 20) {
    media(search: $q, type: ANIME) {
      id
      title { romaji english native }
      startDate { year }
      seasonYear
      format
      episodes
    }
  }
}
"""

_DETAILS_QUERY = """
query Details($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    startDate { year }
    format
    description(asHtml: false)
    episodes
    synonyms
    characters(perPage: 50, sort: [ROLE, RELEVANCE]) {
      edges {
        role
        node {
          id
          name { full native }
          gender
          description(asHtml: false)
        }
      }
    }
  }
}
"""

_FORMAT_MAP: dict[str, MediaType] = {
    "TV": MediaType.TV,
    "MOVIE": MediaType.MOVIE,
    "OVA": MediaType.OVA,
    "ONA": MediaType.ONA,
    "SPECIAL": MediaType.SPECIAL,
    "MUSIC": MediaType.MUSIC,
}

_ROLE_MAP: dict[str, CharacterRole] = {
    "MAIN": CharacterRole.MAIN,
    "SUPPORTING": CharacterRole.SUPPORTING,
    "BACKGROUND": CharacterRole.BACKGROUND,
}

_GENDER_MAP: dict[str, CharacterGender] = {
    "Male": CharacterGender.MALE,
    "Female": CharacterGender.FEMALE,
    "Non-binary": CharacterGender.NON_BINARY,
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


class AniListProvider(MetadataProvider):
    supports_search = True
    supports_characters = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._cache = MetadataCache(settings.config_root / "metadata_cache.db")

    async def _post(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        if resp.status_code == 429:
            raise RuntimeError("AniList rate limit exceeded")
        resp.raise_for_status()
        return resp.json()

    async def _fetch_media(self, provider_id: str) -> dict:
        cache_key = f"anilist:media:{provider_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        data = await self._post(_DETAILS_QUERY, {"id": int(provider_id)})
        media = data["data"]["Media"]
        result = self._serialize_media(media)
        self._cache.set(cache_key, result, ttl=86400)
        return result

    def _serialize_media(self, media: dict) -> dict:
        title = media.get("title") or {}
        romaji = title.get("romaji") or ""
        english = title.get("english") or ""
        native = title.get("native") or None

        synonyms: list[str] = list(media.get("synonyms") or [])
        if english and english != romaji:
            synonyms.insert(0, english)

        year: int | None = None
        start_date = media.get("startDate") or {}
        if start_date.get("year"):
            year = start_date["year"]

        fmt = media.get("format") or ""
        media_type = _FORMAT_MAP.get(fmt, MediaType.UNKNOWN).value

        raw_desc = media.get("description") or None
        description = _strip_html(raw_desc) if raw_desc else None

        characters: list[dict] = []
        char_data = (media.get("characters") or {}).get("edges") or []
        for edge in char_data:
            role_str = (edge.get("role") or "").upper()
            role = _ROLE_MAP.get(role_str, CharacterRole.SUPPORTING).value
            node = edge.get("node") or {}
            name_obj = node.get("name") or {}
            full_name = name_obj.get("full") or ""
            if not full_name:
                continue
            native_name = name_obj.get("native") or None
            gender_raw = node.get("gender") or None
            gender = _GENDER_MAP.get(gender_raw).value if gender_raw and gender_raw in _GENDER_MAP else None
            char_desc_raw = node.get("description") or None
            char_desc = _strip_html(char_desc_raw) if char_desc_raw else None
            characters.append({
                "provider_id": str(node.get("id")) if node.get("id") else None,
                "name": full_name,
                "name_native": native_name,
                "role": role,
                "gender": gender,
                "description": char_desc,
            })

        return {
            "details": {
                "provider_id": str(media["id"]),
                "title": romaji,
                "title_native": native,
                "title_synonyms": synonyms,
                "year": year,
                "media_type": media_type,
                "description": description,
                "episode_count": media.get("episodes"),
            },
            "characters": characters,
        }

    @staticmethod
    def _search_item(media: dict) -> dict:
        title_obj = media.get("title") or {}
        # startDate.year is the real premiere; seasonYear only when it is missing.
        year = (media.get("startDate") or {}).get("year") or media.get("seasonYear") or None
        fmt = media.get("format") or ""
        return {
            "provider_id": str(media["id"]),
            "title": title_obj.get("romaji") or "",
            "title_english": title_obj.get("english") or None,
            "title_native": title_obj.get("native") or None,
            "year": year,
            "media_type": _FORMAT_MAP.get(fmt, MediaType.UNKNOWN).value,
            "episode_count": media.get("episodes"),
        }

    @staticmethod
    def _to_result(r: dict) -> SearchResult:
        return SearchResult(
            provider_id=r["provider_id"],
            title=r["title"],
            title_native=r["title_native"],
            year=r["year"],
            media_type=MediaType(r["media_type"]),
            # older cache entries predate these keys
            episode_count=r.get("episode_count"),
            title_english=r.get("title_english"),
        )

    async def search(self, query: str) -> list[SearchResult]:
        cache_key = f"anilist:search:{query.lower()}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return [self._to_result(r) for r in cached]

        data = await self._post(_SEARCH_QUERY, {"q": query})
        media_list = (data.get("data") or {}).get("Page", {}).get("media") or []

        results = [self._search_item(m) for m in media_list]
        self._cache.set(cache_key, results, ttl=3600)
        return [self._to_result(r) for r in results]

    async def get_details(self, provider_id: str) -> SeriesDetails:
        data = await self._fetch_media(provider_id)
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
        data = await self._fetch_media(provider_id)
        return [
            Character(
                name=c["name"],
                provider_id=c["provider_id"],
                name_native=c["name_native"],
                role=CharacterRole(c["role"]) if c["role"] else None,
                gender=CharacterGender(c["gender"]) if c["gender"] else None,
                description=c["description"],
            )
            for c in data["characters"]
        ]

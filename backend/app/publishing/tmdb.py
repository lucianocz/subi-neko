"""Small publishing-only TMDB client; never feeds translation metadata."""

from __future__ import annotations

import unicodedata

import httpx

from app.core.config import settings
from app.metadata.cache import MetadataCache
from app.publishing.web_client import PublishingError


def romaji_titles(item):
    """Use TMDB's labelled romanizations and Latin Japanese alternative names."""

    def latin(value):
        return (
            value
            and any(c.isalpha() for c in value)
            and all(
                not c.isalpha() or "LATIN" in unicodedata.name(c, "") for c in value
            )
        )

    alternatives = item.get("alternative_titles") or {}
    rows = alternatives.get("results", alternatives.get("titles", []))
    names = []
    for row in rows:
        title = (row.get("title") or "").strip()
        kind = (row.get("type") or "").casefold()
        labelled = any(marker in kind for marker in ("romaji", "roman", "hepburn"))
        japanese = (
            item.get("original_language") == "ja" and row.get("iso_3166_1") == "JP"
        )
        if latin(title) and (labelled or japanese):
            names.append(title)
    original = item.get("original_name") or item.get("original_title") or ""
    if not names and latin(original):
        names.append(original.strip())
    unique = {}
    for name in names:
        unique.setdefault(unicodedata.normalize("NFKC", name).casefold(), name)
    return list(unique.values())


class TmdbClient:
    def __init__(self):
        if not settings.tmdb_api_token:
            raise PublishingError(
                "tmdb_unconfigured",
                "Configure TMDB_API_TOKEN on the server.",
                503,
            )
        self.cache = MetadataCache(settings.config_root / "metadata_cache.db")

    def get(self, path, *, refresh=False, **params):
        params = {"language": "en-US", **params}
        key = f"web:tmdb:v1:{path}:{sorted(params.items())}"
        if not refresh:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        headers = {"Authorization": f"Bearer {settings.tmdb_api_token}"}
        try:
            with httpx.Client(
                timeout=httpx.Timeout(30, connect=10), follow_redirects=False
            ) as client:
                response = client.get(
                    f"https://api.themoviedb.org/3{path}",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, ValueError):
            raise PublishingError(
                "tmdb_request",
                "TMDB could not provide metadata. Check credentials or retry.",
                502,
            ) from None
        self.cache.set(key, value, ttl=3600 if path.startswith("/search") else 86400)
        return value

    def search(self, query):
        results = []
        for media in ("tv", "movie"):
            data = self.get(f"/search/{media}", query=query, include_adult="false")
            for item in data.get("results", []):
                results.append(
                    {
                        "id": str(item["id"]),
                        "media_type": media,
                        "title": item.get("name") or item.get("title"),
                        "year": (
                            item.get("first_air_date") or item.get("release_date") or ""
                        )[:4],
                        "poster_path": item.get("poster_path"),
                        "title_romaji": next(iter(romaji_titles(item)), None),
                        "number_of_seasons": item.get("number_of_seasons"),
                    }
                )
        return results

    def details(self, media, title_id, refresh=False):
        item = self.get(
            f"/{media}/{title_id}",
            refresh=refresh,
            append_to_response="alternative_titles",
        )
        year = (item.get("first_air_date") or item.get("release_date") or "")[:4]
        names = romaji_titles(item)
        return {
            "tmdb_title_id": str(item["id"]),
            "media_type": media,
            "episode_structure": "FLAT" if media == "movie" else "SEASONED",
            "order_kind": "standard",
            "group_id": None,
            "season": None,
            "metadata": {
                "title_english": item.get("name") or item.get("title"),
                "title_romaji": names[0] if len(names) == 1 else None,
                "year": int(year) if year.isdigit() else None,
                "original_language": item.get("original_language"),
                "original_description": item.get("overview") or None,
                "description": item.get("overview") or None,
            },
            "poster_path": item.get("poster_path"),
            "romaji_titles": names,
            "number_of_seasons": item.get("number_of_seasons"),
        }

    def catalog(self, title_id, group_id=None, season_number=None, refresh=False):
        if group_id:
            available = self.get(f"/tv/{title_id}/episode_groups", refresh=refresh)
            if not any(str(g["id"]) == group_id for g in available.get("results", [])):
                raise PublishingError(
                    "tmdb_group",
                    "Episode group does not belong to the selected TMDB title.",
                )
            data = self.get(f"/tv/episode_group/{group_id}", refresh=refresh)
            groups = data.get("groups", [])
            return {
                "seasons": [
                    {
                        "external_id": str(g["id"]),
                        "season_number": g["order"] + 1,
                        "name_english": g.get("name"),
                        "year": None,
                    }
                    for g in groups
                ],
                "episodes_by_season": {
                    str(g["id"]): [
                        {
                            "tmdb_episode_id": str(e["id"]),
                            "episode_number": e["order"] + 1,
                            "name_english": e.get("name"),
                        }
                        for e in sorted(g.get("episodes", []), key=lambda e: e["order"])
                    ]
                    for g in groups
                },
                "groups": [],
            }
        details = self.get(f"/tv/{title_id}", refresh=refresh)
        groups = self.get(f"/tv/{title_id}/episode_groups", refresh=refresh)
        seasons = [
            {
                "external_id": str(s["id"]),
                "season_number": s["season_number"],
                "name_english": s.get("name"),
                "year": int(s["air_date"][:4]) if s.get("air_date") else None,
            }
            for s in details.get("seasons", [])
        ]
        episodes = {}
        if season_number is not None:
            season = self.get(f"/tv/{title_id}/season/{season_number}", refresh=refresh)
            episodes[str(season["id"])] = [
                {
                    "tmdb_episode_id": str(e["id"]),
                    "episode_number": e["episode_number"],
                    "name_english": e.get("name"),
                }
                for e in season.get("episodes", [])
            ]
        return {
            "seasons": seasons,
            "episodes_by_season": episodes,
            "groups": [
                {"id": str(g["id"]), "name": g["name"]}
                for g in groups.get("results", [])
            ],
        }

    def poster(self, path):
        # path validated by WebConfig; no arbitrary upstream URLs or redirects.
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(30, connect=10), follow_redirects=False
                ) as client,
                client.stream(
                    "GET", f"https://image.tmdb.org/t/p/w780{path}"
                ) as response,
            ):
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 25 * 1024 * 1024:
                        raise PublishingError(
                            "poster_too_large", "TMDB poster exceeds 25 MiB."
                        )
            return bytes(data)
        except httpx.HTTPError:
            raise PublishingError(
                "poster_request", "Could not retrieve the approved TMDB poster.", 502
            ) from None

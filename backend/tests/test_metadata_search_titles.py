from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from app.metadata.anidb import AniDBProvider
from app.metadata.anilist import AniListProvider


class _NullCache:
    def __init__(self) -> None:
        self.store: dict = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value


def _anilist(payload: dict) -> AniListProvider:
    p = AniListProvider.__new__(AniListProvider)
    p.config = {}
    p._cache = _NullCache()

    async def _post(query, variables):
        return {"data": {"Page": {"media": payload}}}

    p._post = _post  # type: ignore[method-assign]
    return p


def _search(provider, q="x"):
    return asyncio.run(provider.search(q))


def test_anilist_maps_romaji_english_native_and_start_year():
    p = _anilist([{
        "id": 1,
        "title": {"romaji": "Mujikaku Seijo", "english": "The Oblivious Saint", "native": "無自覚聖女"},
        "startDate": {"year": 2025},
        "seasonYear": 2024,
        "format": "TV",
        "episodes": 12,
    }])
    [r] = _search(p)
    assert (r.title, r.title_english, r.title_native) == (
        "Mujikaku Seijo", "The Oblivious Saint", "無自覚聖女",
    )
    assert r.year == 2025  # startDate wins over seasonYear
    assert r.provider_id == "1"


def test_anilist_year_falls_back_to_season_year_then_none():
    p = _anilist([
        {"id": 1, "title": {"romaji": "A"}, "startDate": {"year": None}, "seasonYear": 2021},
        {"id": 2, "title": {"romaji": "B", "english": None}, "startDate": {}, "seasonYear": None},
    ])
    a, b = _search(p)
    assert a.year == 2021
    assert b.year is None and b.title_english is None and b.title_native is None


def test_anilist_search_query_requests_season_year():
    from app.metadata.anilist import _SEARCH_QUERY

    assert "seasonYear" in _SEARCH_QUERY and "startDate" in _SEARCH_QUERY


def test_anilist_cached_entries_without_english_still_load():
    p = _anilist([])
    p._cache.store["anilist:search:x"] = [{
        "provider_id": "5", "title": "T", "title_native": None,
        "year": 2000, "media_type": "TV",
    }]
    [r] = _search(p)
    assert r.title_english is None and r.year == 2000


_DUMP = """
<animetitles>
  <anime aid="10">
    <title xml:lang="x-jat" type="main">Sousou no Frieren</title>
    <title xml:lang="ja" type="official">葬送のフリーレン</title>
    <title xml:lang="en" type="syn">Frieren Fan Alias</title>
    <title xml:lang="en" type="official">Frieren: Beyond Journey's End</title>
    <title xml:lang="en" type="official">Second English</title>
  </anime>
  <anime aid="11">
    <title xml:lang="x-jat" type="main">Only Romaji</title>
    <title xml:lang="en" type="syn">Just A Synonym</title>
  </anime>
  <anime aid="12">
    <title xml:lang="x-jat" type="main">No Main Match</title>
  </anime>
  <anime>
    <title xml:lang="x-jat" type="main">Missing Aid</title>
  </anime>
</animetitles>
"""


def test_anidb_dump_title_variants_and_no_year():
    entries = AniDBProvider._parse_titles_dump(ET.fromstring(_DUMP))
    by_aid = {e["aid"]: e for e in entries}
    assert set(by_aid) == {"10", "11", "12"}
    e = by_aid["10"]
    assert e["main"] == "Sousou no Frieren"
    assert e["native"] == "葬送のフリーレン"
    assert e["english"] == "Frieren: Beyond Journey's End"  # official, first; not the syn
    assert by_aid["11"]["english"] is None and by_aid["11"]["native"] is None

    p = AniDBProvider.__new__(AniDBProvider)
    p.config = {}

    async def _load():
        return entries

    p._load_titles = _load  # type: ignore[method-assign]
    results = asyncio.run(p.search("frieren"))
    assert [r.provider_id for r in results] == ["10"]
    r = results[0]
    assert (r.title, r.title_english, r.title_native) == (
        "Sousou no Frieren", "Frieren: Beyond Journey's End", "葬送のフリーレン",
    )
    assert r.year is None  # dump carries no start date

"""AniBridge mappings: range grammar, TMDB resolution (matched / ambiguous /
conflicting / missing) and the cached, refreshable dataset store.

No network: the store takes an injected fetcher and clock."""
from __future__ import annotations

import json

import pytest

from app.metadata import anibridge as ab
from app.metadata.anibridge import (
    AniBridgeStore,
    EpisodeRange,
    FetchResult,
    TargetRange,
    build_index,
    count_episodes,
    parse_source_range,
    parse_target_range,
    resolve,
    suggest,
)

DAY = 24 * 3600

# Mirrors the real dataset's shape (schema 3.x): source descriptor -> target
# descriptor -> {source range: target range}.
DATASET = {
    "$meta": {"schema_version": "3.0.3", "generated_on": "2026-11-04T08:23:59Z"},
    # AniDB/AniList that map cleanly to one TMDB show season
    "anidb:1:R": {
        "anilist:290": {"1-13": "1-13"},
        "tmdb_show:26209:s1": {"1-13": "1-13"},
        "tvdb_show:72025:s1": {"1-13": "1-13"},
    },
    "anilist:290": {
        "anidb:1:R": {"1-13": "1-13"},
        "tmdb_show:26209:s1": {"1-13": "1-13"},
    },
    # Season offset with an open-ended source range (split cour)
    "anilist:300": {"tmdb_show:500:s1": {"1-12": "1-12", "14-": "13-"}},
    # Movie
    "anidb:20:R": {"tmdb_movie:777": {"1": "1"}},
    # One title, several seasons -> ambiguous season, unambiguous title
    "anilist:20": {
        "tmdb_show:46260:s1": {"1-52": "1-52"},
        "tmdb_show:46260:s2": {"53-104": "1-52"},
    },
    # Movie AND show specials -> ambiguous title
    "anilist:5": {"tmdb_movie:11299": {"1": "1"}, "tmdb_show:30991:s0": {"1": "1"}},
    # AniDB: nothing regular maps to TMDB, specials do
    "anidb:30:S": {"tmdb_show:900:s0": {"1": "4"}},
    # Direct entry for anilist:40 says show 111 ...
    "anilist:40": {"tmdb_show:111:s1": {"1-12": "1-12"}},
    # ... but an AniDB entry linked to anilist:40 says show 222 -> conflict
    "anidb:41:R": {"anilist:40": {"1-12": "1-12"}, "tmdb_show:222:s1": {"1-12": "1-12"}},
    # AniList without direct TMDB data, recoverable through a linked AniDB entry
    "anidb:50:R": {"anilist:51": {"1-12": "1-12"}, "tmdb_show:333:s2": {"1-12": "1-12"}},
    "anilist:51": {"anidb:50:R": {"1-12": "1-12"}},
    # Ratio and comma segments
    "anilist:60": {"tmdb_show:444:s1": {"1-10": "1-6,8-11|-2"}},
    # No TMDB targets at all
    "anilist:70": {"mal:70": {"1": "1"}},
}


@pytest.fixture(scope="module")
def index():
    return build_index(DATASET)


# ---------------------------------------------------------------------------
# Range grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1", EpisodeRange(1, 1)),
    ("1-13", EpisodeRange(1, 13)),
    ("14-", EpisodeRange(14, None)),
    (" 3-5 ", EpisodeRange(3, 5)),
])
def test_source_range_forms(text, expected):
    assert parse_source_range(text) == expected


@pytest.mark.parametrize("text", ["", "-", "a-b", "5-3", "1,2", "1|2", "1--2", None, 7])
def test_malformed_source_ranges_are_rejected(text):
    assert parse_source_range(text) is None


def test_target_range_segments_and_ratio():
    assert parse_target_range("1-6,8-13") == TargetRange(
        (EpisodeRange(1, 6), EpisodeRange(8, 13)), None)
    assert parse_target_range("14-|2") == TargetRange((EpisodeRange(14, None),), 2)
    assert parse_target_range("1-2|-2") == TargetRange((EpisodeRange(1, 2),), -2)
    assert parse_target_range("20,22,24") == TargetRange(
        (EpisodeRange(20, 20), EpisodeRange(22, 22), EpisodeRange(24, 24)), None)


@pytest.mark.parametrize("text", ["", "1-3|", "1-3|x", "1-3|0", "1,,2", "x"])
def test_malformed_target_ranges_are_rejected(text):
    assert parse_target_range(text) is None


def test_count_episodes():
    assert count_episodes([EpisodeRange(1, 12), EpisodeRange(14, 15)]) == 14
    assert count_episodes([EpisodeRange(1, 12), EpisodeRange(14, None)]) is None
    assert count_episodes([]) is None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_anidb_and_anilist_resolve_to_the_same_tmdb_season(index):
    for provider, pid in (("anidb", "1"), ("anilist", "290")):
        r = resolve(index, provider, pid)
        assert r.status == "matched" and r.reason is None
        assert r.title == {"media_type": "tv", "tmdb_id": "26209"}
        (c,) = r.candidates
        assert (c.media_type, c.season_number, c.source_episode_count) == ("tv", 1, 13)
        assert c.via is None


def test_anidb_scope_is_reported(index):
    (c,) = resolve(index, "anidb", "1").candidates
    assert c.source_scope == "R"
    assert resolve(index, "anilist", "290").candidates[0].source_scope is None


def test_movie_resolves_without_a_season(index):
    r = resolve(index, "anidb", "20")
    assert r.status == "matched" and r.title == {"media_type": "movie", "tmdb_id": "777"}
    assert r.candidates[0].season_number is None


def test_open_ended_source_range_has_no_episode_count(index):
    (c,) = resolve(index, "anilist", "300").candidates
    assert c.source_episode_count is None
    assert [m["source"] for m in c.episode_map] == ["1-12", "14-"]


def test_missing_mapping(index):
    for provider, pid in (("anilist", "999999"), ("anidb", "999999"), ("anilist", "70")):
        r = resolve(index, provider, pid)
        assert (r.status, r.reason, r.title, r.candidates) == ("none", "no_mapping", None, [])


def test_several_seasons_of_one_title_are_ambiguous_but_the_title_is_known(index):
    r = resolve(index, "anilist", "20")
    assert r.status == "ambiguous" and r.reason == "multiple_seasons"
    assert r.title == {"media_type": "tv", "tmdb_id": "46260"}
    assert sorted(c.season_number for c in r.candidates) == [1, 2]


def test_several_titles_are_ambiguous_and_none_is_preselected(index):
    r = resolve(index, "anilist", "5")
    assert r.status == "ambiguous" and r.reason == "multiple_titles"
    assert r.title is None
    assert {(c.media_type, c.tmdb_id) for c in r.candidates} == {("movie", "11299"), ("tv", "30991")}


def test_anidb_specials_are_used_only_when_no_regular_entry_maps(index):
    r = resolve(index, "anidb", "30")
    assert r.status == "matched" and r.candidates[0].source_scope == "S"
    assert r.candidates[0].season_number == 0


def test_direct_and_linked_disagreement_is_a_conflict(index):
    r = resolve(index, "anilist", "40")
    assert r.status == "conflict" and r.reason == "direct_vs_linked" and r.title is None
    assert {c.tmdb_id for c in r.candidates} == {"111", "222"}


def test_linked_entry_corroborating_the_direct_one_is_not_a_conflict(index):
    # anilist:290 <-> anidb:1:R both point at show 26209
    assert resolve(index, "anilist", "290").status == "matched"


def test_linked_only_match_is_flagged_and_has_no_trusted_episode_count(index):
    r = resolve(index, "anilist", "51")
    assert r.status == "matched" and r.title["tmdb_id"] == "333"
    (c,) = r.candidates
    assert c.via == "anidb:50:R" and c.season_number == 2
    assert c.source_episode_count is None  # the linked entry's numbering, not AniList's


def test_unsupported_provider_never_guesses(index):
    assert resolve(index, "kitsu", "1").status == "none"
    assert resolve(index, "anilist", "").status == "none"


def test_ratio_and_comma_segments_do_not_break_resolution(index):
    r = resolve(index, "anilist", "60")
    assert r.status == "matched" and r.candidates[0].episode_map == [
        {"source": "1-10", "target": "1-6,8-11|-2"}]


def test_index_only_keeps_tmdb_relevant_entries(index):
    assert "anilist:70" not in index.forward and "anidb:1:R" in index.forward


@pytest.mark.parametrize("data", [
    [], {}, {"$meta": {"schema_version": "2.0.0"}, "anidb:1:R": {}},
    {"$meta": {"schema_version": "3.0.0"}}, {"anidb:1:R": {}},
])
def test_unsupported_datasets_are_rejected(data):
    with pytest.raises(ValueError):
        ab.validate_dataset(data)


# ---------------------------------------------------------------------------
# Store: cache freshness, refresh and fallback
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Network:
    """Scripted fetcher; records every call."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, etag):
        self.calls.append((url, etag))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def payload(dataset=DATASET) -> FetchResult:
    return FetchResult(200, json.dumps(dataset).encode(), etag='"v1"')


def make_store(tmp_path, network, clock, **kw):
    kw.setdefault("spawn", lambda fn: fn())   # run the background refresh inline
    return AniBridgeStore(tmp_path / "ab", fetch=network, now=clock, **kw)


def test_first_use_downloads_and_caches(tmp_path):
    net, clock = Network(payload()), Clock()
    store = make_store(tmp_path, net, clock)
    index, info = store.get()
    assert index is not None and info.available and not info.stale
    assert info.generated_on == "2026-11-04T08:23:59Z" and info.schema_version == "3.0.3"
    assert len(net.calls) == 1 and net.calls[0][1] is None
    assert (tmp_path / "ab" / "mappings.json").exists()


def test_fresh_cache_is_not_refetched_and_survives_restart(tmp_path):
    net, clock = Network(payload()), Clock()
    make_store(tmp_path, net, clock).get()
    clock.t += DAY - 60
    restarted = make_store(tmp_path, Network(RuntimeError("must not fetch")), clock)
    index, info = restarted.get()
    assert index is not None and not info.stale
    assert len(net.calls) == 1


def test_refresh_happens_at_most_once_a_day(tmp_path):
    net, clock = Network(payload()), Clock()
    store = make_store(tmp_path, net, clock)
    store.get()
    for _ in range(5):
        clock.t += 60
        store.get()
    assert len(net.calls) == 1
    clock.t += DAY
    store.get()                       # stale -> background refresh
    assert len(net.calls) == 2
    for _ in range(5):
        clock.t += 60
        store.get()
    assert len(net.calls) == 2


def test_stale_cache_is_served_immediately_and_refreshed_behind(tmp_path):
    updated = {**DATASET, "$meta": {"schema_version": "3.0.4", "generated_on": "later"}}
    net, clock = Network(payload(), payload(updated)), Clock()
    spawned = []
    store = make_store(tmp_path, net, clock, spawn=spawned.append)
    store.get()
    clock.t += DAY + 1
    index, info = store.get()          # nothing ran yet: still the old data, flagged stale
    assert info.stale and info.schema_version == "3.0.3" and len(spawned) == 1
    spawned[0]()                        # the background refresh completes
    index, info = store.get()
    assert not info.stale and info.schema_version == "3.0.4"


def test_failed_refresh_keeps_the_last_valid_dataset(tmp_path):
    net, clock = Network(payload(), ConnectionError("down")), Clock()
    store = make_store(tmp_path, net, clock)
    store.get()
    clock.t += DAY + 1
    index, info = store.get()
    index, info = store.get()
    assert index is not None and resolve(index, "anidb", "1").status == "matched"
    assert info.available and info.stale and info.error == "ConnectionError"


def test_failed_attempts_back_off_instead_of_hammering(tmp_path):
    net, clock = Network(payload(), ConnectionError("down")), Clock()
    store = make_store(tmp_path, net, clock, retry_after=900)
    store.get()
    clock.t += DAY + 1
    store.get()
    attempts = len(net.calls)
    for _ in range(4):
        clock.t += 60
        store.get()
    assert len(net.calls) == attempts                # inside the cool-down: no new request
    clock.t += 900
    store.get()
    assert len(net.calls) == attempts + 1


def test_invalid_payload_never_replaces_a_good_cache(tmp_path):
    bad = [
        FetchResult(200, b"not json"),
        FetchResult(200, json.dumps({"$meta": {"schema_version": "9.0"}, "a": {}}).encode()),
        FetchResult(200, b""),
        FetchResult(500, b"x"),
    ]
    for response in bad:
        net, clock = Network(payload(), response), Clock()
        store = make_store(tmp_path / str(len(bad)) / response.status.__str__(), net, clock)
        store.get()
        before = (store.dir / "mappings.json").read_bytes()
        clock.t += DAY + 1
        store.get()
        assert (store.dir / "mappings.json").read_bytes() == before
        assert store.get()[0] is not None


def test_no_cache_and_no_network_is_unavailable_not_an_error(tmp_path):
    net, clock = Network(ConnectionError("offline")), Clock()
    store = make_store(tmp_path, net, clock)
    index, info = store.get()
    assert index is None and not info.available and info.error == "ConnectionError"
    store.get()
    assert len(net.calls) == 1                       # cool-down: second call is instant


def test_not_modified_response_extends_freshness_without_a_download(tmp_path):
    net, clock = Network(payload(), FetchResult(304)), Clock()
    store = make_store(tmp_path, net, clock)
    store.get()
    clock.t += DAY + 1
    store.get()
    assert net.calls[1][1] == '"v1"'                 # conditional request
    index, info = store.get()
    assert index is not None and not info.stale


def test_corrupt_cache_file_is_replaced(tmp_path):
    net, clock = Network(payload()), Clock()
    store = make_store(tmp_path, net, clock)
    store.get()
    (store.dir / "mappings.json").write_text("{broken", encoding="utf-8")
    fresh = make_store(tmp_path, Network(payload()), clock)
    index, info = fresh.get()
    # State says "fresh" but the file is unusable -> treated as no cache -> re-fetched.
    assert index is not None and info.available


def test_suggest_returns_api_shape_and_never_raises(tmp_path):
    store = make_store(tmp_path, Network(payload()), Clock())
    out = suggest("anilist", "290", store)
    assert out["status"] == "matched" and out["title"] == {"media_type": "tv", "tmdb_id": "26209"}
    assert out["candidates"][0]["season_number"] == 1 and out["dataset"]["available"] is True
    json.dumps(out)

    down = suggest("anilist", "290", make_store(tmp_path / "x", Network(OSError("no")), Clock()))
    assert down["status"] == "unavailable" and down["candidates"] == []

    class Boom:
        def get(self):
            raise RuntimeError("bug")
    assert suggest("anidb", "1", Boom())["status"] == "unavailable"

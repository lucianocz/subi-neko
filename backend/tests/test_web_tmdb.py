import httpx
import pytest

from app.core.config import settings
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError


@pytest.fixture
def tmdb(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_root", tmp_path)
    monkeypatch.setattr(settings, "tmdb_api_token", "private-token")
    real = httpx.Client
    calls = []

    def response(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer private-token"
        assert "api_key" not in request.url.params
        path = request.url.path
        if "/search/" in path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 42,
                            "name": "English",
                            "first_air_date": "2020-01-01",
                            "poster_path": "/poster.jpg",
                        }
                    ]
                },
            )
        if path.endswith("episode_groups"):
            return httpx.Response(
                200, json={"results": [{"id": "group", "name": "Alternative"}]}
            )
        if "/episode_group/" in path:
            return httpx.Response(
                200,
                json={
                    "groups": [
                        {
                            "id": "subgroup",
                            "order": 0,
                            "name": "Arc",
                            "episodes": [
                                {"id": 900, "order": 1, "name": "Second"},
                                {"id": 901, "order": 0, "name": "First"},
                            ],
                        }
                    ]
                },
            )
        if "/season/" in path:
            return httpx.Response(
                200,
                json={
                    "id": 500,
                    "episodes": [{"id": 501, "episode_number": 1, "name": "Special"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "name": "English",
                "original_name": "日本語",
                "first_air_date": "2020-01-01",
                "original_language": "ja",
                "overview": "English description",
                "poster_path": "/poster.jpg",
                "seasons": [
                    {
                        "id": 500,
                        "season_number": 0,
                        "name": "Specials",
                        "air_date": None,
                    }
                ],
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real(**kw, transport=httpx.MockTransport(response)),
    )
    return TmdbClient(), calls


def test_missing_token_requires_tmdb_api_token(monkeypatch):
    monkeypatch.setattr(settings, "tmdb_api_token", "")
    with pytest.raises(
        PublishingError, match="Configure TMDB_API_TOKEN on the server"
    ) as exc:
        TmdbClient()
    assert exc.value.status == 503


def test_search_tv_movie_and_cache(tmdb):
    client, calls = tmdb
    results = client.search("English")
    assert {r["media_type"] for r in results} == {"tv", "movie"}
    assert all(r["id"] == "42" and r["poster_path"] == "/poster.jpg" for r in results)
    client.search("English")
    assert len(calls) == 2


def test_english_metadata_no_fabricated_romaji_refresh(tmdb):
    client, calls = tmdb
    data = client.details("tv", "42")
    assert data["metadata"] == {
        "title_english": "English",
        "title_romaji": None,
        "year": 2020,
        "original_language": "ja",
        "original_description": "English description",
        "description": "English description",
    }
    client.details("tv", "42")
    assert len(calls) == 1
    client.details("tv", "42", refresh=True)
    assert len(calls) == 2


def test_standard_specials_use_actual_season_id(tmdb):
    client, _ = tmdb
    catalog = client.catalog("42", season_number=0)
    assert catalog["seasons"][0]["external_id"] == "500"
    assert catalog["seasons"][0]["season_number"] == 0
    assert catalog["episodes_by_season"]["500"][0]["tmdb_episode_id"] == "501"


def test_alternative_order_uses_stable_episode_ids(tmdb):
    client, _ = tmdb
    catalog = client.catalog("42", group_id="group")
    assert catalog["seasons"][0]["external_id"] == "subgroup"
    episodes = catalog["episodes_by_season"]["subgroup"]
    assert [e["tmdb_episode_id"] for e in episodes] == ["901", "900"]
    assert [e["episode_number"] for e in episodes] == [1, 2]


def test_wrong_group_rejected(tmdb):
    client, _ = tmdb

    with pytest.raises(PublishingError, match="does not belong"):
        client.catalog("42", group_id="other")


@pytest.mark.parametrize("media,field", [("tv", "results"), ("movie", "titles")])
def test_alternative_romaji_names_are_deduplicated_and_not_transliterated(
    tmdb, monkeypatch, media, field
):
    client, _ = tmdb

    def get(path, **params):
        assert params["append_to_response"] == "alternative_titles"
        return {
            "id": 42,
            "name": "English",
            "original_name": "日本語",
            "original_language": "ja",
            "number_of_seasons": 2 if media == "tv" else None,
            "alternative_titles": {
                field: [
                    {"title": "Neko no Ie", "type": "Romaji", "iso_3166_1": "JP"},
                    {"title": "neko no ie", "type": "Romaji", "iso_3166_1": "JP"},
                    {"title": "Neko no Iē", "type": "Romanized", "iso_3166_1": "JP"},
                    {"title": "猫の家", "type": "", "iso_3166_1": "JP"},
                    {"title": "House of Cats", "type": "", "iso_3166_1": "US"},
                ]
            },
        }

    monkeypatch.setattr(client, "get", get)
    data = client.details(media, "42")
    assert data["romaji_titles"] == ["Neko no Ie", "Neko no Iē"]
    assert data["metadata"]["title_romaji"] is None
    assert data["number_of_seasons"] == (2 if media == "tv" else None)


def test_single_japanese_latin_alternative_fills_romaji(tmdb, monkeypatch):
    client, _ = tmdb
    monkeypatch.setattr(
        client,
        "get",
        lambda *_a, **_kw: {
            "id": 42,
            "name": "English",
            "original_name": "日本語",
            "original_language": "ja",
            "alternative_titles": {
                "results": [{"title": "Neko no Ie", "iso_3166_1": "JP", "type": ""}]
            },
        },
    )
    assert client.details("tv", "42")["metadata"]["title_romaji"] == "Neko no Ie"

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.metadata.anidb import AniDBProvider

_SAMPLE_XML = """
<anime id="123">
  <type>TV Series</type>
  <episodecount>2</episodecount>
  <startdate>2023-09-29</startdate>
  <titles>
    <title xml:lang="x-jat" type="main">Sousou no Frieren</title>
  </titles>
  <episodes>
    <episode id="1001">
      <epno type="1">1</epno>
      <airdate>2023-09-29</airdate>
      <title xml:lang="en">The Journey's End</title>
      <title xml:lang="ja">旅の終わり</title>
    </episode>
    <episode id="1002">
      <epno type="1">2</epno>
      <airdate>2023-10-06</airdate>
      <title xml:lang="en">It Didn't Have to Be Magic</title>
    </episode>
    <episode id="2001">
      <epno type="2">S1</epno>
      <title xml:lang="en">Special</title>
    </episode>
    <episode id="3001">
      <epno type="3">C1</epno>
      <title xml:lang="en">Opening</title>
    </episode>
  </episodes>
</anime>
"""

_CHARACTER_XML = """
<anime id="456">
  <type>TV Series</type>
  <titles><title xml:lang="x-jat" type="main">Test</title></titles>
  <characters>
    <character id="77" type="main character">
      <name>Aria</name>
      <gender>female</gender>
      <charactertype>Human</charactertype>
      <seiyuu>Jane Actor</seiyuu>
      <description>[b]Leader.[/b] This deliberately long description contains relationship details and must survive parsing without truncation.</description>
    </character>
    <character id="78" type="appears in"><name>Silent</name></character>
  </characters>
</anime>
"""


def test_parse_anime_xml_extracts_regular_episodes(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "config_root", tmp_path)

    provider = AniDBProvider()
    root = ET.fromstring(_SAMPLE_XML)
    result = provider._parse_anime_xml(root)

    episodes = result["episodes"]
    # Specials (type 2) and credits (type 3) are excluded.
    assert [e["number"] for e in episodes] == [1, 2]
    assert episodes[0]["title"] == "The Journey's End"
    assert episodes[0]["title_native"] == "旅の終わり"
    assert episodes[0]["air_date"] == "2023-09-29"
    assert episodes[1]["title"] == "It Didn't Have to Be Magic"
    assert episodes[1]["title_native"] is None


def test_parse_anime_xml_preserves_full_character_metadata(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "config_root", tmp_path)
    provider = AniDBProvider()
    result = provider._parse_anime_xml(ET.fromstring(_CHARACTER_XML))
    aria, silent = result["characters"]
    assert aria["provider_id"] == "77"
    assert aria["name"] == "Aria"
    assert aria["gender"] == "female"
    assert aria["role"] == "MAIN"
    assert aria["voice_actor"] == "Jane Actor"
    assert aria["character_type"] == "Human"
    assert aria["description"].startswith("Leader. This deliberately long")
    assert aria["description"].endswith("without truncation.")
    assert silent["description"] is None
    assert silent["gender"] is None
    assert silent["voice_actor"] is None

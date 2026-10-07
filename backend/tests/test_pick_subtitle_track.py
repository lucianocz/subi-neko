from __future__ import annotations

from app.jobs.handlers.inspect_mkv import _describe_candidates, _pick_subtitle_track


def _track(track_id: int, *, codec_id: str = "S_TEXT/ASS", type_: str = "subtitles", **props):
    return {"id": track_id, "type": type_, "properties": {"codec_id": codec_id, **props}}


def test_full_track_wins_over_earlier_signs_songs_track():
    # Judas layout: signs/songs track comes first in the container.
    tracks = [
        _track(3, language="eng", track_name="English [Signs-Songs]"),
        _track(4, language="eng", track_name="English [Full]"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 4


def test_signs_track_still_picked_when_it_is_the_only_one():
    tracks = [_track(3, language="eng", track_name="English [Signs-Songs]")]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_english_preferred_over_a_full_track_in_another_language():
    tracks = [
        _track(2, language="jpn", track_name="Japanese [Full]"),
        _track(3, language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_hearing_impaired_and_forced_tracks_deprioritised():
    tracks = [
        _track(2, language="eng", track_name="English SDH", flag_hearing_impaired=True),
        _track(3, language="eng", track_name="English", forced_track=True),
        _track(4, language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 4


def test_container_order_breaks_ties_between_equivalent_tracks():
    tracks = [
        _track(5, language="eng", track_name="English"),
        _track(2, language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 2


def test_untitled_tracks_fall_back_to_container_order():
    tracks = [_track(3, language="eng"), _track(4, language="eng")]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_song_and_karaoke_names_also_deprioritised():
    for signs_name in ("OP/ED Songs", "Karaoke", "Lyrics", "Typesetting", "Forced"):
        tracks = [
            _track(3, language="eng", track_name=signs_name),
            _track(4, language="eng", track_name="English"),
        ]
        assert _pick_subtitle_track(tracks)["id"] == 4, signs_name


def test_ass_takes_precedence_over_plaintext_within_same_language():
    tracks = [
        _track(0, type_="video", codec_id="V_MPEG4/ISO/AVC"),
        _track(1, type_="audio", codec_id="A_AAC"),
        _track(2, codec_id="S_TEXT/UTF8", language="eng", track_name="English [Full]"),
        _track(3, codec_id="S_TEXT/SSA", language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_english_srt_beats_spanish_ass():
    tracks = [
        _track(2, codec_id="S_TEXT/ASS", language="spa", track_name="Spanish"),
        _track(3, codec_id="S_TEXT/UTF8", language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_english_ass_beats_spanish_ass():
    tracks = [
        _track(2, codec_id="S_TEXT/ASS", language="spa"),
        _track(3, codec_id="S_TEXT/ASS", language="eng"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_english_ass_beats_english_srt_regardless_of_order():
    tracks = [
        _track(2, codec_id="S_TEXT/UTF8", language="eng"),
        _track(3, codec_id="S_TEXT/ASS", language="eng"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_downstream_tiebreakers_unchanged_for_same_language_and_format():
    # Same language + format: signs name, then SDH, then forced, then "Full"
    # name, then container order decide exactly as before.
    tracks = [
        _track(2, language="eng", track_name="English [Signs]"),
        _track(3, language="eng", track_name="English", flag_hearing_impaired=True),
        _track(4, language="eng", track_name="English", forced_track=True),
        _track(5, language="eng", track_name="English"),
        _track(6, language="eng", track_name="English [Full]"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 6
    assert _pick_subtitle_track(tracks[:4])["id"] == 5
    assert _pick_subtitle_track(tracks[:3])["id"] == 4  # forced (4) beats SDH (3): SDH is compared first


def test_downstream_tiebreakers_apply_within_non_ass_english_tracks():
    tracks = [
        _track(2, codec_id="S_TEXT/UTF8", language="eng", track_name="English [Signs]"),
        _track(3, codec_id="S_TEXT/UTF8", language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 3


def test_plaintext_is_used_when_no_ass_candidate_exists():
    tracks = [
        _track(0, type_="video", codec_id="V_MPEG4/ISO/AVC"),
        _track(1, codec_id="S_TEXT/UTF8", language="eng", track_name="English"),
    ]
    assert _pick_subtitle_track(tracks)["id"] == 1


def test_unsupported_subtitle_codec_returns_none():
    tracks = [_track(1, codec_id="S_HDMV/PGS", language="eng")]
    assert _pick_subtitle_track(tracks) is None


def test_describe_candidates_lists_ass_tracks_best_first():
    tracks = [
        _track(0, type_="video", codec_id="V_MPEG4/ISO/AVC"),
        _track(3, language="eng", track_name="English [Signs-Songs]"),
        _track(4, language="eng", track_name="English [Full]"),
        _track(5, codec_id="S_TEXT/UTF8", language="eng", track_name="English"),
    ]
    assert _describe_candidates(tracks) == [
        {"id": 4, "language": "eng", "track_name": "English [Full]"},
        {"id": 3, "language": "eng", "track_name": "English [Signs-Songs]"},
        {"id": 5, "language": "eng", "track_name": "English"},
    ]

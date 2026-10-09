"""External (sidecar) source subtitles: discovery, parsing, priority over embedded tracks."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, Subtitle, SubtitleEvent, SubtitleStyle
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import extract_subtitles as extract_handler
from app.jobs.handlers import inspect_mkv as inspect_handler
from app.subs.sidecar import SidecarError, find_sidecars, load_sidecar, select_sidecar

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n2\n00:00:04,000 --> 00:00:05,500\nSecond line\n"
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello there\n\n00:00:04.000 --> 00:00:05.500\nSecond line\n"
SSA = (
    "[Script Info]\nScriptType: v4.00\n\n[V4 Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour, Bold, Italic, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
    "Style: Default,Arial,20,16777215,16777215,0,0,0,0,1,2,0,2,10,10,10,0,0\n\n[Events]\n"
    "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: Marked=0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there\n"
)
ASS = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding\n"
    "Style: Fancy,Comic Sans MS,33,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Fancy,Bob,0,0,0,,Hello there\n"
)
MICRODVD = "{24}{72}Hello there\n{96}{132}Second line\n"


def touch(path: Path, content: str | bytes = "x") -> Path:
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "series"
    d.mkdir()
    return touch(d / "Episode.mkv", b"MKV")


# -- discovery ----------------------------------------------------------------------

@pytest.mark.parametrize("ext", [".ass", ".ssa", ".srt", ".vtt", ".sub", ".SRT", ".Ass"])
def test_exact_basename_with_supported_extension_is_found(media, ext):
    sub = touch(media.with_name("Episode" + ext))
    assert find_sidecars(media) == [sub]


@pytest.mark.parametrize("name", [
    "Episode.en.srt", "Episode.eng.ass", "Episode.full.ass", "Episode.forced.srt",
    "Episode2.ass", "Episode Extra.srt", "Episode.txt", "Episode.idx", "Episode.mkv.srt",
])
def test_qualified_prefixed_or_unsupported_names_are_ignored(media, name):
    touch(media.with_name(name))
    assert find_sidecars(media) == []


@pytest.mark.parametrize("stem", [
    "Anime.S02E03", "[Group] Show - 01 [1080p]", "Příšerně žluťoučký kůň",
    "A B  C", "v1.2.final.release",
])
def test_unusual_basenames(tmp_path, stem):
    video = touch(tmp_path / f"{stem}.mkv", b"MKV")
    sub = touch(tmp_path / f"{stem}.srt")
    touch(tmp_path / f"{stem}.en.srt")
    touch(tmp_path / f"{stem}2.srt")
    assert find_sidecars(video) == [sub]


def test_multiple_sidecars_ranked_deterministically(media):
    for ext in (".sub", ".vtt", ".srt", ".ssa", ".ass"):
        touch(media.with_name("Episode" + ext))
    assert [p.suffix for p in find_sidecars(media)] == [".ass", ".ssa", ".srt", ".vtt", ".sub"]


# -- parsing ------------------------------------------------------------------------

def test_srt_and_vtt_parse_with_text_and_timing(media):
    for ext, content in ((".srt", SRT), (".vtt", VTT)):
        sc = load_sidecar(touch(media.with_name("Episode" + ext), content))
        assert sc.format == "srt"
        assert [(e.start, e.end, e.text) for e in sc.subs] == [
            (1000, 3000, "Hello there"), (4000, 5500, "Second line")]


def test_crlf_and_bom_srt_text_is_clean(media):
    raw = b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:03,000\r\nHello\r\nthere\r\n\r\n"
    sc = load_sidecar(touch(media.with_name("Episode.srt"), raw))
    assert [e.text for e in sc.subs] == ["Hello\\Nthere"]


def test_ssa_and_ass_are_ass_format(media):
    assert load_sidecar(touch(media.with_name("Episode.ssa"), SSA)).format == "ass"
    assert load_sidecar(touch(media.with_name("Episode.ass"), ASS)).format == "ass"


def test_text_sub_microdvd_is_supported(media):
    sc = load_sidecar(touch(media.with_name("Episode.sub"), MICRODVD))
    assert sc.format == "srt"
    assert [e.text for e in sc.subs] == ["Hello there", "Second line"]


def test_binary_vobsub_sub_is_rejected(media):
    path = touch(media.with_name("Episode.sub"), b"\x00\x00\x01\xba\x44\x00\x04\x00" + bytes(300))
    with pytest.raises(SidecarError):
        load_sidecar(path)


@pytest.mark.parametrize("ext,content", [
    (".srt", "this is not a subtitle"), (".ass", "garbage"), (".vtt", "no header"),
    (".srt", ""), (".sub", "plain prose without frames"),
])
def test_malformed_sidecars_raise_sidecar_error(media, ext, content):
    with pytest.raises(SidecarError):
        load_sidecar(touch(media.with_name("Episode" + ext), content))


def test_non_utf8_sidecar_rejected(media):
    with pytest.raises(SidecarError):
        load_sidecar(touch(media.with_name("Episode.srt"), b"1\n00:00:01,000 --> 00:00:02,000\n\xff\xfe\xfa\n"))


def test_malformed_sidecar_falls_through_to_next_candidate(media):
    touch(media.with_name("Episode.ass"), "garbage")
    touch(media.with_name("Episode.srt"), SRT)
    sc = select_sidecar(media)
    assert sc is not None and sc.path.suffix == ".srt"


def test_only_malformed_sidecar_means_no_sidecar(media):
    touch(media.with_name("Episode.srt"), "garbage")
    assert select_sidecar(media) is None


def test_no_sidecar(media):
    assert select_sidecar(media) is None


# -- pipeline integration ---------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for target in ("app.jobs.handlers.inspect_mkv.SyncSessionLocal",
                   "app.jobs.handlers.extract_subtitles.SyncSessionLocal"):
        monkeypatch.setattr(target, factory)
    import_root = tmp_path / "import"
    (import_root / "series").mkdir(parents=True)
    video = touch(import_root / "series" / "Episode.mkv", b"MKV")
    with factory() as s:
        p = Project(name="P", source_directory="series", anime_provider="t", anime_external_id="1",
                    status="discovering", created_at=_now(), updated_at=_now())
        s.add(p)
        s.flush()
        f = File(project_id=p.id, filename="Episode.mkv", relative_path="Episode.mkv", status="new", created_at=_now(), updated_at=_now())
        s.add(f)
        s.commit()
        file_id = f.id
    return SimpleNamespace(factory=factory, import_root=import_root, video=video, file_id=file_id)


EMBEDDED = {"tracks": [{"id": 3, "type": "subtitles", "properties": {
    "codec_id": "S_TEXT/ASS", "language": "eng", "track_name": "English"}}]}


def _ctx(env, **opts):
    return JobContext(import_root=env.import_root, output_root=env.import_root,
                      options=AppOptions.from_dict(opts))


def _fake_mkvmerge(monkeypatch, info=EMBEDDED):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(info).encode(), stderr=b"")
    monkeypatch.setattr(inspect_handler.subprocess, "run", run)
    return calls


def _inspect(env, **opts):
    return inspect_handler.inspect_mkv({"file_id": env.file_id}, _ctx(env, **opts), lambda *a, **k: None)


def _extract(env, **opts):
    return extract_handler.extract_subtitles({"file_id": env.file_id}, _ctx(env, **opts), lambda *a, **k: None)


def _file(env) -> File:
    with env.factory() as s:
        return s.get(File, env.file_id)


def test_sidecar_beats_embedded_ass_and_skips_mkvmerge(env, monkeypatch):
    calls = _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(".srt"), SRT)
    result = _inspect(env)
    assert result["status"] == "succeeded"
    assert result["result"]["external_subtitle"] == "Episode.srt"
    f = _file(env)
    assert (f.subtitle_track_index, f.detected_subtitle_format) == (inspect_handler.EXTERNAL_TRACK_INDEX, "srt")
    assert calls == []


def test_missing_sidecar_falls_back_to_embedded(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    result = _inspect(env)
    assert result["status"] == "succeeded" and result["result"]["subtitle_track_index"] == 3
    assert _file(env).subtitle_track_index == 3


def test_malformed_sidecar_falls_back_to_embedded(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(".srt"), "garbage")
    assert _inspect(env)["result"]["subtitle_track_index"] == 3


def test_embedded_selection_uses_configured_source_language(env, monkeypatch):
    info = {"tracks": [
        {"id": 1, "type": "subtitles", "properties": {"codec_id": "S_TEXT/ASS", "language": "eng"}},
        {"id": 2, "type": "subtitles", "properties": {"codec_id": "S_TEXT/ASS", "language": "jpn"}},
    ]}
    _fake_mkvmerge(monkeypatch, info)
    assert _inspect(env, SOURCE_LANG_CODE="ja")["result"]["subtitle_track_index"] == 2


@pytest.mark.parametrize("ext,content", [(".srt", SRT), (".vtt", VTT), (".sub", MICRODVD)])
def test_text_sidecars_enter_pipeline_with_plaintext_defaults(env, monkeypatch, ext, content):
    _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(ext), content)
    assert _inspect(env)["status"] == "succeeded"
    assert _extract(env)["status"] == "succeeded"
    with env.factory() as s:
        events = list(s.scalars(select(SubtitleEvent).where(SubtitleEvent.file_id == env.file_id)
                                .order_by(SubtitleEvent.line_index)))
        sub = s.scalar(select(Subtitle).where(Subtitle.file_id == env.file_id))
        styles = list(s.scalars(select(SubtitleStyle)))
    assert [e.source_text for e in events] == ["Hello there", "Second line"]
    assert all(e.translation_status == "pending" and e.content_type == "dialogue" for e in events)
    assert (sub.play_res_x, sub.play_res_y) == (1920, 1080)
    assert [st.style_name for st in styles] == ["Default"]


def test_ass_sidecar_preserves_styles_and_events(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(".ass"), ASS)
    assert _inspect(env)["status"] == "succeeded"
    assert _extract(env)["status"] == "succeeded"
    with env.factory() as s:
        sub = s.scalar(select(Subtitle).where(Subtitle.file_id == env.file_id))
        styles = list(s.scalars(select(SubtitleStyle)))
        event = s.scalar(select(SubtitleEvent).where(SubtitleEvent.file_id == env.file_id))
    assert (sub.play_res_x, sub.play_res_y) == (1280, 720)
    assert [(st.style_name, st.font_name, st.font_size) for st in styles] == [("Fancy", "Comic Sans MS", 33.0)]
    assert (event.style, event.name, event.start_ms, event.end_ms) == ("Fancy", "Bob", 1000, 3000)


def test_ssa_sidecar_is_imported(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(".ssa"), SSA)
    assert _inspect(env)["status"] == "succeeded"
    assert _extract(env)["status"] == "succeeded"
    with env.factory() as s:
        assert s.scalar(select(SubtitleEvent).where(SubtitleEvent.file_id == env.file_id)).source_text == "Hello there"


def test_sidecar_is_not_language_checked(env, monkeypatch):
    # Content is German but the source language is English: still accepted.
    _fake_mkvmerge(monkeypatch)
    touch(env.video.with_suffix(".srt"), SRT.replace("Hello there", "Guten Tag zusammen"))
    assert _inspect(env, SOURCE_LANG_CODE="en")["status"] == "succeeded"
    assert _extract(env)["status"] == "succeeded"


def test_extract_fails_cleanly_when_sidecar_disappears(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    sidecar = touch(env.video.with_suffix(".srt"), SRT)
    _inspect(env)
    sidecar.unlink()
    result = _extract(env)
    assert result["status"] == "failed" and result["error_code"] == "SIDECAR_MISSING"


def test_sidecar_is_not_modified_or_copied(env, monkeypatch):
    _fake_mkvmerge(monkeypatch)
    sidecar = touch(env.video.with_suffix(".srt"), SRT)
    before = sidecar.read_bytes()
    _inspect(env)
    _extract(env)
    assert sidecar.read_bytes() == before
    assert sorted(p.name for p in env.video.parent.iterdir()) == ["Episode.mkv", "Episode.srt"]


# -- embedded track extraction (real handler path, mkvextract faked) ------------------------

def _fake_mkvextract(monkeypatch, *, content: str = ASS, returncode: int = 0, stderr: str = ""):
    """Replace mkvextract: write ``content`` to the requested output path."""
    class FakePopen:
        def __init__(self, cmd, **kw):
            self.returncode = returncode
            self.stdout = iter(["Progress: 50%\n", "Progress: 100%\n"])
            self.stderr = SimpleNamespace(read=lambda: stderr)
            if returncode == 0:
                Path(cmd[-1].split(":", 1)[1]).write_text(content, encoding="utf-8")

        def wait(self):
            return self.returncode

    monkeypatch.setattr(extract_handler.subprocess, "Popen", FakePopen)


def _set_embedded_track(env, fmt: str = "ass") -> None:
    with env.factory() as s:
        f = s.get(File, env.file_id)
        f.subtitle_track_index, f.detected_subtitle_format = 3, fmt
        s.commit()


def test_embedded_track_extraction_succeeds(env, monkeypatch):
    _set_embedded_track(env)
    _fake_mkvextract(monkeypatch)
    result = _extract(env)
    assert result["status"] == "succeeded"
    assert result["result"] == {"events": 1, "styles": 1}
    with env.factory() as s:
        ev = s.scalar(select(SubtitleEvent).where(SubtitleEvent.file_id == env.file_id))
        assert ev.source_text == "Hello there"


def test_embedded_mkvextract_failure_is_returned_not_raised(env, monkeypatch):
    _set_embedded_track(env)
    _fake_mkvextract(monkeypatch, returncode=2, stderr="boom\n")
    result = _extract(env)
    assert result["status"] == "failed"
    assert result["error_code"] == "MKVEXTRACT_FAILED"
    assert result["error_message"] == "boom"
    with env.factory() as s:
        assert s.scalar(select(SubtitleEvent).where(SubtitleEvent.file_id == env.file_id)) is None


def test_embedded_parse_failure_sets_blocking_reason(env, monkeypatch):
    _set_embedded_track(env)
    _fake_mkvextract(monkeypatch, content="\x00garbage")

    def boom(*a, **k):
        raise ValueError("bad subs")
    monkeypatch.setattr(extract_handler.pysubs2, "load", boom)
    result = _extract(env)
    assert result["status"] == "failed" and result["error_code"] == "subtitle_parse_failed"
    assert _file(env).blocking_reason == "subtitle_parse_failed"

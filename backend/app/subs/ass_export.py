"""Database-only ASS export shared by downloads and WEB publication."""

from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    FileBranding,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.subs.ass_rendering import HEADER_NOTICE, build_ass
from app.subs.branding import spec_from_row


def subtitle_download_name(media_filename, variant, source_lang_code="en"):
    stem = Path(media_filename).stem
    return f"{stem}.{source_lang_code}.ass" if variant == "original" else f"{stem}.ass"


def export_ass(session, file, opts, variant="translated"):
    subtitle = session.scalar(select(Subtitle).where(Subtitle.file_id == file.id))
    if subtitle is None:
        raise ValueError("Subtitles have not been extracted yet")
    styles = session.scalars(
        select(SubtitleStyle)
        .join(
            file_subtitle_styles,
            file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id,
        )
        .where(file_subtitle_styles.c.file_id == file.id)
        .order_by(SubtitleStyle.id)
    ).all()
    events = session.scalars(
        select(SubtitleEvent)
        .where(SubtitleEvent.file_id == file.id)
        .order_by(SubtitleEvent.line_index)
    ).all()
    branding = spec_from_row(
        session.scalar(select(FileBranding).where(FileBranding.file_id == file.id))
    )
    subs = build_ass(
        subtitle,
        styles,
        events,
        text_variant=variant,
        title=(opts.target_lang_name or "") if variant == "translated" else None,
        use_font_replacements=opts.replace_incompatible_fonts,
        branding=branding,
    )
    return (
        subtitle_download_name(file.filename, variant, opts.source_lang_code),
        subs.to_string("ass", header_notice=HEADER_NOTICE).encode("utf-8"),
    )

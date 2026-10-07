import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProjectStatus(str, Enum):
    NEW = "new"
    DISCOVERING = "discovering"
    CONTEXT_REVIEW = "context_review"
    PROCESSING = "processing"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class FileStatus(str, Enum):
    # MUXING / COMPLETED belong to the retired automatic-output lifecycle: no
    # code produces them any more (publishing is a project-level state, see
    # Project.publish_state) and the publish-state migration converts legacy
    # rows to ACCEPTED. The values stay so old data/history still deserializes.
    NEW = "new"
    DISCOVERING = "discovering"
    WAITING = "waiting"
    READY = "ready"
    PROCESSING = "processing"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    MUXING = "muxing"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"


class FileBlockingReason(str, Enum):
    USER_REVIEW_REQUIRED = "user_review_required"
    SUBTITLE_MISSING = "subtitle_missing"
    SUBTITLE_PARSE_FAILED = "subtitle_parse_failed"
    ANALYSIS_FAILED = "analysis_failed"
    TRANSLATION_FAILED = "translation_failed"
    VALIDATION_FAILED = "validation_failed"
    MUX_FAILED = "mux_failed"  # legacy: publish failures live on Project.publish_*
    PAUSED = "paused"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WatchedWordType(str, Enum):
    ORIGINAL = "original"
    TRANSLATED = "translated"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class Option(Base):
    __tablename__ = "options"
    __table_args__ = (
        Index("idx_options_name", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("idx_projects_status", "status"),
        Index("idx_projects_mapping_status", "speaker_mapping_status"),
        UniqueConstraint("source_directory", name="uq_projects_source_directory"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_directory: Mapped[str] = mapped_column(Text, nullable=False)
    anime_provider: Mapped[str] = mapped_column(Text, nullable=False)
    anime_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_mapping_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="awaiting_discovery")
    speaker_coverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    is_paused: Mapped[bool] = mapped_column(Integer, nullable=False, server_default="0")
    context_approved_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # --- Output / publish lifecycle (see app/db/output_state.py) -------------
    # output_revision identifies the current state of everything that feeds the
    # final ASS/MKV; touch_output() bumps it. published_revision is the revision
    # the last *successful* publish represented. The user-facing state (not_ready
    # / ready / publishing / published / failed) is derived, never stored.
    output_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    published_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Last explicit publish run: None | publishing | published | failed.
    publish_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    publish_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Monotonic id of the latest Publish click: part of the job's dedupe key and
    # a fence so a superseded run can never write the project's publish state.
    publish_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # output_revision the latest run captured (a failure only counts while it
    # still matches the current revision).
    publish_target_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    files: Mapped[list["File"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    characters: Mapped[list["ProjectCharacter"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    speakers: Mapped[list["ProjectSpeaker"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    watched_words: Mapped[list["ProjectWatchedWord"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    jobs: Mapped[list["JobRecord"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectWatchedWord(Base):
    __tablename__ = "project_watched_words"
    __table_args__ = (
        UniqueConstraint("project_id", "word", "word_type", name="uq_project_watched_words_project_word_type"),
        Index("idx_project_watched_words_project_type", "project_id", "word_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    word: Mapped[str] = mapped_column(Text, nullable=False)
    word_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="watched_words")


class ProjectCharacter(Base):
    __tablename__ = "project_characters"
    __table_args__ = (
        Index("idx_project_characters_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    social_position: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aliases: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    voice_actor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    character_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="characters")
    speakers: Mapped[list["ProjectSpeaker"]] = relationship(back_populates="character")


class ProjectSpeaker(Base):
    """A raw speaker name found in subtitle events, mapped N:1 to a canonical
    character by the auto-mapping job (or manually corrected by the user)."""
    __tablename__ = "project_speakers"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    gender: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    character_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("project_characters.id", ondelete="SET NULL"), nullable=True)
    match_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0..1
    match_origin: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # manual|fuzzy|llm
    match_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sample_lines_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_extra: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # "Boy A", "Crowd", "TV"…
    content_tag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # sign|karaoke|song
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="speakers")
    character: Mapped[Optional["ProjectCharacter"]] = relationship(back_populates="speakers")


class ProjectEpisode(Base):
    """Per-episode metadata from the provider (AniDB) — titles and air dates
    used as prompt context for the file matching that episode number."""
    __tablename__ = "project_episodes"
    __table_args__ = (
        UniqueConstraint("project_id", "episode_number",
                         name="uq_project_episodes_project_number"),
        Index("idx_project_episodes_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title_native: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    air_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("project_id", "relative_path"),
        Index("idx_files_project_status", "project_id", "status"),
        Index("idx_files_project_blocking_reason", "project_id", "blocking_reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    episode_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    blocking_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    translation_requested_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_subtitle_format: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subtitle_track_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="files")
    subtitle: Mapped[Optional["Subtitle"]] = relationship(back_populates="file", cascade="all, delete-orphan", uselist=False)
    subtitle_events: Mapped[list["SubtitleEvent"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    # M:N — styles are project-level canonical records shared between files.
    # Deleting a file only removes its ``file_subtitle_styles`` rows.
    subtitle_styles: Mapped[list["SubtitleStyle"]] = relationship(
        secondary=lambda: file_subtitle_styles, back_populates="files",
    )
    subtitle_chunks: Mapped[list["SubtitleChunk"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    qa_items: Mapped[list["QaItem"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    jobs: Mapped[list["JobRecord"]] = relationship(back_populates="file", cascade="all, delete-orphan")


class Subtitle(Base):
    __tablename__ = "subtitles"
    __table_args__ = (
        UniqueConstraint("file_id"),
        Index("idx_subtitles_file", "file_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    script_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    wrap_style: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    play_res_x: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    play_res_y: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scaled_border_and_shadow: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    layout_res_x: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    layout_res_y: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ycbcr_matrix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kerning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra_script_info_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    file: Mapped["File"] = relationship(back_populates="subtitle")


class SubtitleEvent(Base):
    __tablename__ = "subtitle_events"
    __table_args__ = (
        UniqueConstraint("file_id", "line_index"),
        Index("idx_subtitle_events_file_translation_status", "file_id", "translation_status"),
        Index("idx_subtitle_events_file_event_type", "file_id", "event_type"),
        Index("idx_subtitle_events_file_name", "file_id", "name"),
        Index("idx_subtitle_events_file_content_type", "file_id", "content_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="dialogue")
    content_type_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    layer: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Immutable source timing, set once at extraction. start_ms/end_ms are the
    # editable translated/QC timing; the source ASS is built from these two.
    # Manual QC events have no source counterpart: original == current at
    # creation (structural fallback only; they are excluded from the source ASS).
    original_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    original_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    style: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    margin_l: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_r: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_v: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effect: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    original_ai_translated_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    translation_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    translation_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_user_edited: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_locked: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_approved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Final-QC state. Hidden events are dropped from the *translated* ASS only;
    # manual events are QC-created lines (fansub signs…) that exist only in the
    # translated ASS. Both reset on full retranslate / re-extraction.
    is_hidden: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_manual: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    file: Mapped["File"] = relationship(back_populates="subtitle_events")
    qa_items: Mapped[list["QaItem"]] = relationship(back_populates="subtitle_event", cascade="all, delete-orphan")


# Pure join table: which canonical styles a file's script defines.
file_subtitle_styles = Table(
    "file_subtitle_styles",
    Base.metadata,
    Column("file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), primary_key=True),
    Column("subtitle_style_id", Integer, ForeignKey("subtitle_styles.id", ondelete="CASCADE"), primary_key=True),
    Index("idx_file_subtitle_styles_style", "subtitle_style_id"),
)


class SubtitleStyle(Base):
    """Project-level canonical ASS style shared by every file that defines it.

    ``source_style_hash`` fingerprints the ORIGINAL normalized definition
    (name + source properties, see ``app.subs.style_canonical``) and is immutable:
    later edits to the replacement font never touch it, so re-importing the
    original style still resolves to this record.
    """

    __tablename__ = "subtitle_styles"
    __table_args__ = (
        UniqueConstraint("project_id", "source_style_hash"),
        Index("idx_subtitle_styles_project_font_check_status", "project_id", "font_check_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_style_hash: Mapped[str] = mapped_column(Text, nullable=False)
    style_name: Mapped[str] = mapped_column(Text, nullable=False)
    font_name: Mapped[str] = mapped_column(Text, nullable=False)
    font_size: Mapped[float] = mapped_column(Float, nullable=False)
    primary_colour: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secondary_colour: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outline_colour: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    back_colour: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    italic: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    underline: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strikeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scale_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scale_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spacing: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    angle: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    border_style: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    outline: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shadow: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    alignment: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_l: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_r: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_v: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    encoding: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    replacement_font_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    replacement_font_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    font_check_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unchecked")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    files: Mapped[list["File"]] = relationship(secondary=file_subtitle_styles, back_populates="subtitle_styles")


class QaItem(Base):
    __tablename__ = "qa_items"
    __table_args__ = (
        Index("idx_qa_items_file_resolved_severity", "file_id", "is_resolved", "severity"),
        Index("idx_qa_items_event", "subtitle_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    subtitle_event_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("subtitle_events.id", ondelete="CASCADE"), nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    qa_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_resolved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(Text, nullable=True)

    file: Mapped["File"] = relationship(back_populates="qa_items")
    subtitle_event: Mapped[Optional["SubtitleEvent"]] = relationship(back_populates="qa_items")


class SubtitleChunk(Base):
    __tablename__ = "subtitle_chunks"
    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index"),
        Index("idx_subtitle_chunks_file", "file_id"),
        Index("idx_subtitle_chunks_file_status", "file_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    translate_from_line: Mapped[int] = mapped_column(Integer, nullable=False)
    translate_to_line: Mapped[int] = mapped_column(Integer, nullable=False)
    context_prepend_from_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    context_prepend_to_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="dialogue")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_review_needed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    repair_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    polish_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prompt_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failed_job_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    file: Mapped["File"] = relationship(back_populates="subtitle_chunks")


class JobRecord(Base):
    """DB-backed job record. Source of truth for all job state."""
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("idx_jobs_status_priority_scheduled", "status", "priority", "scheduled_at"),
        Index("idx_jobs_project_status", "project_id", "status"),
        Index("idx_jobs_file_status", "file_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(Text, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="jobs")
    file: Mapped[Optional["File"]] = relationship(back_populates="jobs")


class ProjectGlossaryTerm(Base):
    """Established translation for a recurring term — the per-project
    terminology store injected into translate/polish/repair prompts."""
    __tablename__ = "project_glossary_terms"
    __table_args__ = (
        UniqueConstraint("project_id", "source_term", "category",
                         name="uq_project_glossary_terms_project_source_category"),
        Index("idx_project_glossary_terms_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_term: Mapped[str] = mapped_column(Text, nullable=False)
    target_term: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="other")  # name|place|technique|item|honorific|catchphrase|other
    gender: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # grammatical/personal gender
    vocative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # Czech vocative form for names
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="llm")  # manual|llm|metadata
    locked: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # user-edited; jobs never overwrite
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class ProjectStyleBible(Base):
    """Versioned project-wide translation style guidance (LLM-generated,
    user-editable). The highest version is the active one."""
    __tablename__ = "project_style_bibles"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_project_style_bibles_project_version"),
        Index("idx_project_style_bibles_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    tone_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    register_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    honorific_policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_from_file_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_user_edited: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class ProjectCharacterStyle(Base):
    """Per-character voice guidance (manner of speech, register)."""
    __tablename__ = "project_character_styles"
    __table_args__ = (
        UniqueConstraint("project_character_id", name="uq_project_character_styles_character"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_character_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project_characters.id", ondelete="CASCADE"), nullable=False)
    voice_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    register: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="llm")  # manual|llm
    locked: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())

    character: Mapped["ProjectCharacter"] = relationship()


class ProjectAddressPair(Base):
    """How one character addresses another (tykání/vykání).

    New generated rows use canonical character names when speaker mappings
    exist; legacy/raw labels remain supported and are resolved on access.
    """
    __tablename__ = "project_address_pairs"
    __table_args__ = (
        UniqueConstraint("project_id", "speaker_name", "addressee_name",
                         name="uq_project_address_pairs_project_speaker_addressee"),
        Index("idx_project_address_pairs_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    speaker_name: Mapped[str] = mapped_column(Text, nullable=False)
    addressee_name: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)  # tykani|vykani|mixed
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="llm")  # manual|llm
    locked: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class FileAnalysis(Base):
    """Per-file script analysis (synopsis, scene summaries, tricky lines)
    produced by analyze_script and injected into translate/polish prompts."""
    __tablename__ = "file_analyses"
    __table_args__ = (
        UniqueConstraint("file_id", name="uq_file_analyses_file"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    synopsis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scenes_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tricky_lines_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())


class TranslationMemoryEntry(Base):
    """Exact-match translation memory. Human entries (user-edited/approved)
    always win over AI entries and are never downgraded."""
    __tablename__ = "translation_memory"
    __table_args__ = (
        UniqueConstraint("project_id", "source_hash", name="uq_translation_memory_project_hash"),
        Index("idx_translation_memory_project_hash", "project_id", "source_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="dialogue")
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="ai")  # human|ai
    src_file_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    src_line_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now(), onupdate=func.now())


class FileBranding(Base):
    """Per-file render-time branding overlay (1:1 with ``files``).

    Only the template *filename* (under ``<config>/brand``) and one global
    start offset are stored; the template's styles/events are merged into the
    translated ASS by ``app.subs.branding`` and never become SubtitleEvent rows.
    """
    __tablename__ = "file_branding"
    __table_args__ = (
        UniqueConstraint("file_id", name="uq_file_branding_file"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    template_filename: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class FileQualityMetric(Base):
    """Per-file quality snapshot, refreshed progressively as the pipeline
    changes the file (never gated on mux/completion) — the long-term
    signal for whether the pipeline (glossary/TM/style bible/prompts) is
    getting better: falling human-edit distance across episodes means yes."""
    __tablename__ = "file_quality_metrics"
    __table_args__ = (
        UniqueConstraint("file_id", name="uq_file_quality_metrics_file"),
        Index("idx_file_quality_metrics_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    events_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    events_user_edited: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    events_approved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Human correction distance: original_ai_translated_text (kept in sync
    # with the polished pipeline output) vs shipped text — nonzero only when
    # a user edited after the pipeline finished. 0..1, mean over events.
    edit_distance_norm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Pipeline churn: how much the polish pass rewrote the cheap-model draft
    # (mean normalized Levenshtein over polish_edit before/after pairs).
    polish_churn_norm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    polish_edit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qa_blockers: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qa_warnings: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qa_info: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    mean_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mean_confidence_edited: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Fingerprint of the inputs this snapshot was computed from
    # (compute_file_metrics.metrics_fingerprint_stmt); a mismatch = stale.
    source_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())


class LlmCall(Base):
    """One row per LLM API attempt — cost/latency/token accounting."""
    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("idx_llm_calls_project", "project_id"),
        Index("idx_llm_calls_file", "file_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    file_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=True)
    subtitle_chunk_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(Text, nullable=False)  # succeeded | retried | failed
    response_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # json_schema | json_object | text
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Text, nullable=False, server_default=func.now())


# Registers the session listener that bumps Project.output_revision. Imported
# last: it needs the mapped classes above, and importing models anywhere (app,
# alembic env, tests) must be enough to arm it.
import app.db.output_state  # noqa: E402,F401

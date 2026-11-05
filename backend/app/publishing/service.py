from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal, SyncSessionLocal
from app.db import options as options_store
from app.db.models import File, JobRecord, Project, ProjectEpisode
from app.publishing.remote import (  # noqa: F401  (re-exported for callers/tests)
    bind_resolved,
    collect_targets,
    detach_file,
    dumps,
    ensure_content_episode,
    ensure_target_episodes,
    episodes_confirmed,
    file_locked,
    locate,
    mapping_value,
    now,
    pending_refs,
    receipt_progress,
    record_remote,
    record_subtitle_receipt,
    ref_of,
    replace_tmdb_episodes,
    targets_fingerprint,
)
from app.publishing.schemas import PublishRequest, SaveRequest, WebConfig
from app.publishing.web_client import (
    PublishingError,
    WebClient,
    destination,
    require_available,
)
from app.subs.ass_export import export_ass

# Threads can outlive cancelled asyncio tasks. Reconciliation must not release
# their durable claim until the actual handler returns (single-process app).
_live: set[tuple[int, int]] = set()
_live_lock = threading.Lock()


def fingerprint(config):
    # Hash the same approved website metadata shape as legacy configurations.
    # Local source provenance must not create a website metadata revision.
    metadata = config.get("metadata", {})
    if "description" in metadata or "original_description" in metadata:
        config = {**config, "metadata": {
            **{k: v for k, v in metadata.items() if k not in {"description", "original_description"}},
            "description_english": metadata.get("description"),
        }}
    return hashlib.sha256(dumps(config).encode()).hexdigest()


def load(project):
    value = json.loads(project.web_config_json or "{}")
    # destination is None for a mapping captured at project import: it has not
    # been bound to a website yet (the first wizard save binds it).
    if value and value.get("destination") not in (None, destination()):
        raise PublishingError(
            "web_destination_changed",
            "Website destination changed. Existing mappings require explicit recovery; restore the previous destination to continue.",
        )
    return value


def project_check(project, mutation=True):
    require_available()
    if not project:
        raise PublishingError("project_not_found", "Project not found.", 404)
    if mutation and project.archived_at:
        raise PublishingError(
            "project_archived", "Unarchive this project before publishing."
        )
    return load(project)


def order(config):
    return {"provider": "tmdb", "kind": config.order_kind, "group_id": config.group_id}


def compatible(snapshot, config):
    if snapshot.get("current_api"):
        # The current website contract stores only the presentation category;
        # structure and episode order are local publisher configuration and are
        # protected by the durable local locks instead.
        if snapshot["media_type"] != config.media_type:
            raise PublishingError(
                "web_identity_conflict",
                "The website title has a different media type.",
            )
        return
    remote_order = snapshot["episode_order"]
    if (
        snapshot["media_type"] != config.media_type
        or snapshot["episode_structure"] != config.episode_structure
        or remote_order["kind"] != config.order_kind
        or remote_order.get("provider", "tmdb") != "tmdb"
        or remote_order.get("group_id") != config.group_id
    ):
        raise PublishingError(
            "web_identity_conflict",
            "The website title has a different immutable media type, structure or episode order.",
        )


def lookup(config):
    client = WebClient()
    try:
        return client.lookup(config)
    finally:
        client.close()


def release_suggestion(filename):
    match = re.match(r"^\s*\[([^\[\]]+)\]", filename)
    value = match[1].strip() if match else ""
    return value if value and len(value) <= 200 else ""


def validate_ass(filename, content):
    if (
        not filename.strip()
        or len(filename) > 255
        or any(c in filename for c in "/\\:")
        or any(unicodedata.category(c) == "Cc" for c in filename)
    ):
        raise PublishingError(
            "web_filename", "ASS download filename is invalid for the website."
        )
    if len(content) > 25 * 1024 * 1024:
        raise PublishingError(
            "web_too_large", "ASS exceeds the default 25 MiB website limit."
        )
    import pysubs2

    subs = pysubs2.SSAFile.from_string(content.decode("utf-8"), format_="ass")
    if not any(
        not e.is_comment and e.text.strip() and e.end > e.start >= 0 for e in subs
    ):
        raise PublishingError(
            "web_invalid_ass",
            "ASS must include a nonempty Dialogue with valid start/end times.",
        )


def bound(project, envelope):
    """May this project's mapping be published to the configured website?

    A mapping captured at import has ``destination: null`` (unbound) and is
    bound by the first WEB wizard save — or by the first publication when the
    operator chose "Publish & Import" (explicit approval of that mapping)."""
    return bool(envelope) and (
        envelope.get("destination") is not None
        or project.web_publish_intent == "publish_and_import"
    )


def prepare(session, project, request, opts):
    """Validate one attempt and capture everything it will send.

    Returns ``(envelope, config, targets)``. ``targets`` is every remote episode
    of the project (stored TMDB episodes always included); the ones whose file is
    selected additionally carry the rendered ASS (``content``)."""
    envelope = project_check(project)
    if project.web_config_version != request.version:
        raise PublishingError(
            "web_version_conflict",
            "Publishing configuration changed. Reload the wizard.",
        )
    if request.file_ids and opts.target_lang_code != "cs":
        raise PublishingError(
            "web_language", "WEB publishing requires target language Czech (cs)."
        )
    if not bound(project, envelope):
        # No mapping, or one captured at import and never saved in the WEB wizard
        # (saving binds it to this website destination).
        raise PublishingError(
            "web_mapping", "Save the title and episode mappings first."
        )
    config = WebConfig.model_validate(envelope["config"])
    if config.episode_structure == "SEASONED" and not config.season:
        raise PublishingError("web_mapping", "Select one season before publishing.")
    ids = request.file_ids
    if len(set(ids)) != len(ids):
        raise PublishingError("web_mapping", "A file was selected more than once.")
    files = (
        list(
            session.scalars(
                select(File)
                .where(File.project_id == project.id, File.id.in_(ids))
                .order_by(File.id)
            )
        )
        if ids
        else []
    )
    if len(files) != len(ids) or any(f.status != "accepted" for f in files):
        raise PublishingError(
            "web_not_accepted",
            "Every selected file must belong to this project and be accepted.",
        )
    uploads = {}
    targets = set()
    for file in files:
        mapping = mapping_value(file)
        if not mapping or not (file.web_tmdb_episode_id or file.web_publisher_key):
            raise PublishingError(
                "web_mapping", f"Confirm the mapping for file {file.id}."
            )
        if config.episode_structure == "SEASONED" and not file.web_tmdb_episode_id:
            raise PublishingError(
                "web_mapping", "SEASONED episodes require TMDB episode IDs."
            )
        target = file.web_tmdb_episode_id or file.web_publisher_key
        if target in targets:
            raise PublishingError("web_mapping", "Two files target the same episode.")
        targets.add(target)
        try:
            filename, content = export_ass(session, file, opts)
            validate_ass(filename, content)
        except PublishingError as exc:
            # Invalid ASS / filename / size: this file's failure only.
            uploads[file.id] = {
                "filename": file.filename,
                "render_error": exc.message,
                "render_code": exc.code,
            }
            continue
        except (ValueError, OSError):
            # One unrenderable file must not stop the catalog synchronization of
            # everything else: the attempt reports this file's failure.
            uploads[file.id] = {
                "filename": file.filename,
                "render_error": f"Could not render file {file.id}. Check its subtitles and branding.",
            }
            continue
        uploads[file.id] = {
            "filename": filename,
            "content": content,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    captured = collect_targets(session, project.id, config, selected=set(ids))
    for item in captured:
        if item["file_id"] in uploads:
            item.update(uploads[item["file_id"]])
    return envelope, config, captured


def check_identity_edit(project, envelope, config):
    """Once the title binding is durably recorded (or a write is unresolved) every
    structural choice — title, structure, order and season — is frozen."""
    if envelope and (project.web_identity_locked_at or envelope.get("pending_title")):
        before = WebConfig.model_validate(envelope["config"])
        if before.identity() != config.identity():
            raise PublishingError(
                "web_identity_locked",
                "Confirmed website title, order and season cannot be changed.",
            )


def _unique_by_number(episodes):
    seen, unique = set(), []
    for episode in sorted(
        (e for e in episodes if "episode_number" in e), key=lambda e: e["episode_number"]
    ):
        if episode["episode_number"] not in seen:
            seen.add(episode["episode_number"])
            unique.append(episode)
    return unique


def save(project_id, body: SaveRequest):
    # Read-only lookup precedes the short local transaction. Never remote PUT.
    with SyncSessionLocal() as session:
        p = session.get(Project, project_id)
        old = project_check(p)
        if p.web_publish_state == "publishing":
            raise PublishingError("web_busy", "WEB publication is still active.")
        if p.web_config_version != body.version:
            raise PublishingError(
                "web_version_conflict", "Configuration changed. Reload the wizard."
            )
        check_identity_edit(p, old, body.config)
    previous = WebConfig.model_validate(old["config"]) if old else None
    placement_changed = not previous or previous.identity() != body.config.identity()
    # Verify newly selected provider identities against TMDB, while confirmed
    # mappings remain usable even after the provider removes an episode.
    catalog_episodes = None
    if body.config.episode_structure == "SEASONED" and body.config.season:
        with SyncSessionLocal() as session:
            existing = {
                f.id: mapping_value(f)
                for f in session.scalars(
                    select(File).where(File.project_id == project_id)
                )
            }
        new_mappings = [
            m
            for m in body.mappings or []
            if placement_changed
            or existing.get(m.file_id) != m.model_dump(exclude={"file_id"})
        ]
        if new_mappings or placement_changed:
            from app.publishing.tmdb import TmdbClient

            catalog = TmdbClient().catalog(
                body.config.tmdb_title_id,
                body.config.group_id,
                body.config.season.season_number,
                detailed=True,
            )
            selected = next(
                (
                    s
                    for s in catalog["seasons"]
                    if s["external_id"] == body.config.season.external_id
                ),
                None,
            )
            if (
                not selected
                or selected["season_number"] != body.config.season.season_number
            ):
                raise PublishingError(
                    "web_mapping",
                    "Select a real TMDB season/subgroup from the selected order.",
                )
            catalog_episodes = _unique_by_number(
                catalog["episodes_by_season"].get(selected["external_id"], [])
            )
            available = {
                e["tmdb_episode_id"]
                for e in catalog["episodes_by_season"].get(selected["external_id"], [])
            }
            if any(m.tmdb_episode_id not in available for m in new_mappings):
                raise PublishingError(
                    "web_mapping",
                    "A new mapping is not part of the selected TMDB season/order.",
                )
    elif body.config.media_type == "movie" and any(
        m.tmdb_episode_id for m in body.mappings or []
    ):
        raise PublishingError(
            "web_mapping",
            "Movie content items use publisher keys, not movie IDs as episode IDs.",
        )
    remote = lookup(body.config)
    if remote:
        compatible(remote, body.config)
    if old.get("title_uuid") and (not remote or remote["id"] != old["title_uuid"]):
        raise PublishingError(
            "web_recovery_required",
            "Previously recorded website title is missing or changed. Explicit recovery is required.",
        )
    with SyncSessionLocal() as session:
        # CAS takes SQLite's writer lock before mappings are read/changed.
        claimed = session.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.web_config_version == body.version,
                Project.archived_at.is_(None),
                or_(
                    Project.web_publish_state.is_(None),
                    Project.web_publish_state != "publishing",
                ),
            )
            .values(web_config_version=Project.web_config_version + 1)
        )
        if not claimed.rowcount:
            raise PublishingError(
                "web_version_conflict",
                "Project configuration changed or publishing started. Reload the wizard.",
            )
        p = session.get(Project, project_id)
        envelope = project_check(p)
        # A WEB attempt may have established identities while the earlier
        # read-only lookup was in flight. Recheck under the writer lock.
        check_identity_edit(p, envelope, body.config)
        previous = WebConfig.model_validate(envelope["config"]) if envelope else None
        config = body.config.model_dump()
        if previous and (previous.tmdb_title_id, previous.media_type) == (body.config.tmdb_title_id, body.config.media_type):
            before, incoming = previous.metadata, body.config.metadata
            if not incoming.original_description:
                config["metadata"]["original_description"] = before.original_description
            if (
                incoming.original_description != before.original_description
                and incoming.description == incoming.original_description
                and (before.description != before.original_description or not incoming.original_description)
            ):
                config["metadata"]["description"] = before.description
        # Provider omission must not silently remove the approved poster.
        if (
            previous
            and previous.tmdb_title_id == body.config.tmdb_title_id
            and not config["poster_path"]
        ):
            config["poster_path"] = previous.poster_path
        files = list(
            session.scalars(
                select(File).where(File.project_id == project_id).order_by(File.id)
            )
        )
        by_id = {f.id: f for f in files}
        placement_changed = (
            not previous or previous.identity() != body.config.identity()
        )
        if previous and placement_changed:
            if episodes_confirmed(session, project_id) or pending_refs(envelope):
                raise PublishingError(
                    "web_identity_locked",
                    "A remote episode already exists; placement is locked.",
                )
            for f in files:
                f.web_mapping_json = f.web_tmdb_episode_id = f.web_publisher_key = None
        if placement_changed:
            # Unbound/unlocked edit: the stored TMDB episodes always describe the
            # selected title/order/season (never a mix of old and new).
            if (
                body.config.episode_structure == "SEASONED"
                and body.config.season
                and catalog_episodes is None
            ):
                raise PublishingError(
                    "web_version_conflict",
                    "Project configuration changed. Reload the wizard.",
                )
            replace_tmdb_episodes(session, project_id, catalog_episodes or [])
        if body.expected_episode_count is not None:
            p.expected_episode_count = body.expected_episode_count
        content_episode = ensure_content_episode(session, project_id, body.config)
        if body.mappings is not None:
            if len({m.file_id for m in body.mappings}) != len(body.mappings):
                raise PublishingError("web_mapping", "Duplicate file mappings.")
            # An explicit list is the COMPLETE file -> episode association: files
            # left out are unassigned, and a file moved to another episode is
            # detached from its old one first. Receipts live on the episodes
            # (ProjectEpisode), so they are copied there before any file loses
            # them; File.episode_number and episode identities are never touched.
            ensure_target_episodes(session, project_id)
            incoming = {m.file_id: m for m in body.mappings}
            for f in files:
                m = incoming.get(f.id)
                if m is None and f.web_mapping_json or (
                    m is not None
                    and f.web_tmdb_episode_id
                    and m.tmdb_episode_id != f.web_tmdb_episode_id
                ):
                    detach_file(f)
            session.flush()
            taken_keys = {f.web_publisher_key for f in files if f.web_publisher_key}
            for mapping in body.mappings:
                file = by_id.get(mapping.file_id)
                if file is None:
                    raise PublishingError(
                        "web_mapping", "Mapping references a file outside this project."
                    )
                value = mapping.model_dump(exclude={"file_id"})
                old_ref = (
                    ref_of(file.web_tmdb_episode_id, file.web_publisher_key)
                    if file.web_tmdb_episode_id or file.web_publisher_key
                    else None
                )
                if (
                    body.config.episode_structure == "SEASONED"
                    and not mapping.tmdb_episode_id
                ):
                    raise PublishingError(
                        "web_mapping", "SEASONED mappings require a TMDB episode ID."
                    )
                file.web_mapping_json = dumps(value)
                file.web_tmdb_episode_id = mapping.tmdb_episode_id
                if not mapping.tmdb_episode_id:
                    if not file.web_publisher_key:
                        # The first mapped file of a movie is the content episode.
                        key = (
                            content_episode.web_publisher_key
                            if content_episode
                            and content_episode.web_publisher_key not in taken_keys
                            else f"subineko:{uuid.uuid4()}"
                        )
                        taken_keys.add(key)
                        file.web_publisher_key = key
                else:
                    file.web_publisher_key = None
                if old_ref and old_ref != ref_of(
                    file.web_tmdb_episode_id, file.web_publisher_key
                ):
                    # Manual reassignment: the confirmed episode keeps its
                    # receipts (ProjectEpisode); only this file's upload
                    # association is cleared so it can never be tied to, or
                    # published as, the episode it no longer maps to.
                    file.web_episode_uuid = file.web_identity_locked_at = None
                    file.web_published_sha256 = file.web_published_at = None
            targets = [
                f.web_tmdb_episode_id or f.web_publisher_key
                for f in files
                if f.web_mapping_json
            ]
            if len(targets) != len(set(targets)):
                raise PublishingError(
                    "web_mapping", "Two files target the same episode."
                )
        session.flush()
        ensure_target_episodes(session, project_id)
        envelope.update(destination=destination(), config=config)
        if remote:
            if body.config.episode_structure == "FLAT" or body.config.season:
                # Adopt durable remote ids (title, season, episodes) in this
                # transaction; ids are the only thing that creates locks.
                session.flush()
                config_model = WebConfig.model_validate(config)
                record_remote(
                    session,
                    p,
                    envelope,
                    config_model,
                    remote,
                    collect_targets(session, project_id, config_model),
                )
            else:
                envelope["title_uuid"] = remote["id"]
                p.web_identity_locked_at = p.web_identity_locked_at or now()
        if len(dumps(envelope).encode()) > 65536:
            raise PublishingError(
                "web_metadata_size", "Approved metadata is too large."
            )
        p.web_config_json = dumps(envelope)
        try:
            session.commit()
        except IntegrityError:
            raise PublishingError(
                "web_mapping", "Another file already targets this episode."
            ) from None
    return state(project_id)


def remote_state(project_id):
    with SyncSessionLocal() as session:
        p = session.get(Project, project_id)
        envelope = project_check(p, mutation=False)
        if not envelope:
            return None
        config = WebConfig.model_validate(envelope["config"])
        version = p.web_config_version
        captured = collect_targets(session, project_id, config)
    snapshot = lookup(config)
    if envelope.get("title_uuid") and (
        not snapshot or snapshot["id"] != envelope["title_uuid"]
    ):
        raise PublishingError(
            "web_recovery_required",
            "Recorded website title is missing or changed. Explicit recovery is required.",
        )
    if snapshot:
        compatible(snapshot, config)
        if config.episode_structure != "SEASONED" or config.season:
            season, _ = locate(snapshot, config, captured)
            if envelope.get("season_uuid") and (
                not season or season["id"] != envelope["season_uuid"]
            ):
                raise PublishingError(
                    "web_recovery_required",
                    "Recorded website season is missing or changed. Explicit recovery is required.",
                )
    # Resolve provisional markers only after an explicit remote read, never by
    # guessing from a failed request. No remote mutations occur here.
    with SyncSessionLocal() as session:
        p = session.get(Project, project_id)
        current = project_check(p, mutation=False)
        claimed = session.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.web_config_version == version,
                Project.archived_at.is_(None),
                or_(
                    Project.web_publish_state.is_(None),
                    Project.web_publish_state != "publishing",
                ),
            )
            .values(updated_at=Project.updated_at)
        )
        if claimed.rowcount and current.get("config") == envelope.get("config"):
            # A completed worker may have bound identities while the remote
            # lookup was in flight, without changing approved metadata.
            if any(
                current.get(key) != envelope.get(key)
                for key in ("title_uuid", "season_uuid")
            ):
                return snapshot
            fresh = collect_targets(session, project_id, config)
            if [t["episode_uuid"] for t in fresh] != [t["episode_uuid"] for t in captured]:
                return snapshot
            before_json, before_lock = p.web_config_json, p.web_identity_locked_at
            changed = False
            resolved = {}
            if snapshot:
                if config.episode_structure == "FLAT" or config.season:
                    _, resolved = record_remote(session, p, current, config, snapshot, fresh)
                    backfill_subtitle_receipts(session, snapshot, fresh, resolved)
                else:
                    current["title_uuid"] = snapshot["id"]
                    current["destination"] = destination()
                    p.web_identity_locked_at = p.web_identity_locked_at or now()
                changed = any(session.is_modified(o) for o in session.dirty)
            elif not current.get("title_uuid"):
                p.web_identity_locked_at = None
            if snapshot or not current.get("title_uuid"):
                current.pop("pending_title", None)
            # A read proves what exists, not what a lost acknowledgement did: refs
            # the website still does not show stay pending (identities frozen).
            unresolved = [
                r for r in pending_refs(current) if isinstance(r, str) and r not in resolved
            ]
            if snapshot and unresolved:
                current["pending_hierarchy"] = unresolved
            else:
                current.pop("pending_hierarchy", None)
            p.web_config_json = dumps(current)
            if (
                changed
                or before_lock != p.web_identity_locked_at
                or before_json != p.web_config_json
            ):
                p.web_config_version += 1
            session.commit()
    return snapshot


def backfill_subtitle_receipts(session, snapshot, targets, resolved):
    """Adopt receipts the website itself proves: an episode whose current subtitle
    is byte-identical to this project's rendered output is published, even if the
    acknowledgement of that upload was lost. Never lowers or replaces a receipt."""
    episodes = {
        e["id"]: e
        for e in snapshot.get("episodes", [])
        + [e for s in snapshot.get("seasons", []) for e in s.get("episodes", [])]
    }
    opts = options_store.snapshot()
    for item in targets:
        if not (item.get("file_id") and item.get("pe_id")):
            continue
        pe = session.get(ProjectEpisode, item["pe_id"])
        remote = (episodes.get(resolved.get(item["ref"])) or {}).get("current_subtitle")
        if pe.web_subtitle_sha256 or not remote:
            continue
        f = session.get(File, item["file_id"])
        if f.status != "accepted":
            continue
        try:
            _, content = export_ass(session, f, opts)
        except Exception:  # noqa: BLE001, S112 - an unrenderable file simply proves nothing
            continue
        sha = hashlib.sha256(content).hexdigest()
        if sha == remote.get("sha256"):
            record_subtitle_receipt(
                session, item, sha, remote.get("revisionNumber") or remote.get("revision_number")
            )


def _job_view(job):
    if not job:
        return None
    payload = json.loads(job.payload_json or "{}")
    result = json.loads(job.result_json or "null")
    progress = (result or {}).get("progress")
    if job.status == "completed" or (job.status == "failed" and progress is None):
        progress = 1.0 if job.status == "completed" else (progress or 0.0)
    return {
        "id": job.id,
        "status": job.status,
        "result": result,
        "error": job.error_message,
        "file_ids": payload.get("file_ids", []),
        "metadata_only": not payload.get("file_ids"),
        "release_label": payload.get("release_label") or "",
        "changelog": payload.get("changelog"),
        # Monotonic fraction of this job's real work units; see publish_web.
        "progress": progress or 0.0,
        "stage": (result or {}).get("stage"),
        "operations": (result or {}).get("operations", []),
    }


def state(project_id):
    require_available()
    with SyncSessionLocal() as session:
        p = session.get(Project, project_id)
        envelope = project_check(p, mutation=False)
        files = list(
            session.scalars(
                select(File).where(File.project_id == project_id).order_by(File.id)
            )
        )
        job = session.scalar(
            select(JobRecord)
            .where(
                JobRecord.project_id == project_id, JobRecord.job_type == "publish_web"
            )
            .order_by(JobRecord.id.desc())
        )
        opts = options_store.snapshot()
        file_rows = []
        for f in files:
            current_hash = None
            error = None
            if f.status == "accepted":
                try:
                    _, content = export_ass(session, f, opts)
                    current_hash = hashlib.sha256(content).hexdigest()
                except Exception:  # noqa: BLE001 - report render failure without leaking internal paths
                    error = "Check subtitle data and branding template."
            file_rows.append(
                {
                    "id": f.id,
                    "filename": f.filename,
                    "episode_number": f.episode_number,
                    "status": f.status,
                    "mapping": mapping_value(f),
                    "locked": file_locked(envelope, f),
                    "published_at": f.web_published_at,
                    "current_sha256": current_hash,
                    "published_sha256": f.web_published_sha256,
                    "render_error": error,
                }
            )
        config = WebConfig.model_validate(envelope["config"]) if envelope.get("config") else None
        episode_rows, targets = [], []
        if config:
            targets = collect_targets(session, project_id, config)
            files_by_ref = {
                ref_of(f.web_tmdb_episode_id, f.web_publisher_key): f.id
                for f in files
                if f.web_mapping_json
            }
            for pe in session.scalars(
                select(ProjectEpisode)
                .where(ProjectEpisode.project_id == project_id)
                .order_by(ProjectEpisode.episode_number)
            ):
                if not (pe.tmdb_episode_id or pe.web_publisher_key):
                    continue
                ref = ref_of(pe.tmdb_episode_id, pe.web_publisher_key)
                episode_rows.append(
                    {
                        "id": pe.id,
                        "tmdb_episode_id": pe.tmdb_episode_id,
                        "publisher_key": pe.web_publisher_key,
                        "episode_number": pe.episode_number,
                        "name_english": pe.tmdb_name,
                        "remote_id": pe.web_episode_uuid,
                        "confirmed": bool(pe.web_episode_uuid),
                        "locked": bool(pe.web_identity_locked_at)
                        or ref in pending_refs(envelope),
                        "file_id": files_by_ref.get(ref),
                    }
                )
        receipts = receipt_progress(session, p)
        accepted = [r for r in file_rows if r["status"] == "accepted"]
        published = [r for r in accepted if r["published_at"]]
        changed = any(r["current_sha256"] != r["published_sha256"] for r in accepted)
        metadata_published = bool(envelope and envelope.get("published_config_hash"))
        metadata_changed = bool(
            envelope
            and envelope.get("published_config_hash") != fingerprint(envelope["config"])
        ) or bool(
            envelope.get("published_episodes_hash")
            and envelope["published_episodes_hash"] != targets_fingerprint(targets)
        )
        if p.web_publish_state == "publishing":
            status = "publishing"
        elif p.web_publish_state == "failed":
            status = "partial" if published or receipts["confirmed_episodes"] else "failed"
        elif published:
            status = "changes_available" if changed or metadata_changed else "published"
        elif metadata_published:
            status = "changes_available" if metadata_changed else "published"
        elif accepted and all(r["mapping"] for r in accepted):
            status = "ready"
        else:
            status = "configured" if envelope else "not_published"
        return {
            "version": p.web_config_version,
            "config": config.model_dump() if config else None,
            "expected_episode_count": p.expected_episode_count,
            "intent": p.web_publish_intent,
            "bound": bool(envelope) and envelope.get("destination") is not None,
            "locked": bool(p.web_identity_locked_at),
            "identity_pending": bool(envelope.get("pending_title") or pending_refs(envelope)),
            "season_locked": bool(
                envelope.get("season_uuid") or envelope.get("pending_hierarchy")
            ),
            "state": status,
            "episodes": episode_rows,
            "receipt": receipts,
            "files": file_rows,
            "release_label": envelope.get("release_label")
            or release_suggestion(files[0].filename if files else ""),
            "job": _job_view(job),
        }


async def start(project_id, body: PublishRequest, enqueue):
    import asyncio

    from app.orchestrator.orchestrator import _get_project_lock

    require_available()

    # Rendering outside the project lock; CAS below rejects changed config.
    def preflight():
        with SyncSessionLocal() as session:
            prepare(
                session,
                session.get(Project, project_id),
                body,
                options_store.snapshot(),
            )

    await asyncio.to_thread(preflight)
    async with _get_project_lock(project_id):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(Project)
                .where(
                    Project.id == project_id,
                    Project.web_config_version == body.version,
                    Project.archived_at.is_(None),
                    or_(
                        Project.web_publish_state.is_(None),
                        Project.web_publish_state != "publishing",
                    ),
                )
                .values(
                    web_publish_state="publishing",
                    web_publish_attempt=Project.web_publish_attempt + 1,
                )
            )
            if not result.rowcount:
                raise PublishingError(
                    "web_busy", "Publication is active or configuration changed."
                )
            project = await session.get(Project, project_id)
            envelope = json.loads(project.web_config_json or "{}")
            if envelope and envelope.get("destination") is None:
                # "Publish & Import" approved this import-captured mapping: the
                # first publication binds it to the configured website.
                envelope["destination"] = destination()
                project.web_config_json = dumps(envelope)
            await session.commit()
            attempt = project.web_publish_attempt
        try:
            job = await enqueue(
                "publish_web",
                project_id=project_id,
                payload={
                    "project_id": project_id,
                    "attempt": attempt,
                    **body.model_dump(),
                },
                dedupe_key=f"publish_web:{project_id}:{attempt}",
                max_attempts=1,
            )
            if job is None:
                raise PublishingError("web_enqueue", "Could not queue WEB publication.")
        except Exception:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Project)
                    .where(
                        Project.id == project_id, Project.web_publish_attempt == attempt
                    )
                    .values(web_publish_state="failed")
                )
                await session.commit()
            raise
    return {"job_id": job.id, "attempt": attempt}


async def reconcile(session_factory=None):
    from app.orchestrator.orchestrator import _get_project_lock

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        ids = list(
            await session.scalars(
                select(Project.id).where(Project.web_publish_state == "publishing")
            )
        )
    for pid in ids:
        async with _get_project_lock(pid), factory() as session:
            p = await session.get(Project, pid)
            if not p or p.web_publish_state != "publishing":
                continue
            with _live_lock:
                if (pid, p.web_publish_attempt) in _live:
                    continue
            job = await session.scalar(
                select(JobRecord).where(
                    JobRecord.dedupe_key == f"publish_web:{pid}:{p.web_publish_attempt}"
                )
            )
            if job and job.status in ("queued", "running"):
                continue
            p.web_publish_state = "failed"
            await session.commit()

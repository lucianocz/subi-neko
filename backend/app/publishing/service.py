from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal, SyncSessionLocal
from app.db import options as options_store
from app.db.models import File, JobRecord, Project
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


def now():
    return datetime.now(UTC).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    if value and value["destination"] != destination():
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


def mapping_value(file):
    return json.loads(file.web_mapping_json) if file.web_mapping_json else None


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


def prepare(session, project, request, opts):
    envelope = project_check(project)
    if project.web_config_version != request.version:
        raise PublishingError(
            "web_version_conflict",
            "Publishing configuration changed. Reload the wizard.",
        )
    if opts.target_lang_code != "cs":
        raise PublishingError(
            "web_language", "WEB publishing requires target language Czech (cs)."
        )
    if not envelope:
        raise PublishingError(
            "web_mapping", "Save the title and episode mappings first."
        )
    config = WebConfig.model_validate(envelope["config"])
    if config.episode_structure == "SEASONED" and not config.season:
        raise PublishingError("web_mapping", "Select one season before publishing.")
    ids = request.file_ids
    if len(set(ids)) != len(ids):
        raise PublishingError("web_mapping", "A file was selected more than once.")
    files = list(
        session.scalars(
            select(File)
            .where(File.project_id == project.id, File.id.in_(ids))
            .order_by(File.id)
        )
    )
    if len(files) != len(ids) or any(f.status != "accepted" for f in files):
        raise PublishingError(
            "web_not_accepted",
            "Every selected file must belong to this project and be accepted.",
        )
    captured = []
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
        except (ValueError, OSError) as exc:
            raise PublishingError(
                "web_render",
                f"Could not render file {file.id}. Check its subtitles and branding.",
            ) from exc
        captured.append(
            {
                "file_id": file.id,
                "filename": filename,
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "mapping": mapping,
                "tmdb_episode_id": file.web_tmdb_episode_id,
                "publisher_key": file.web_publisher_key,
                "episode_uuid": file.web_episode_uuid,
            }
        )
    return envelope, config, captured


def check_identity_edit(project, envelope, config):
    if envelope and project.web_identity_locked_at:
        before = WebConfig.model_validate(envelope["config"])
        season_locked = envelope.get("season_uuid") or envelope.get("pending_hierarchy")
        if before.identity()[:5] != config.identity()[:5] or (
            season_locked and before.identity() != config.identity()
        ):
            raise PublishingError(
                "web_identity_locked",
                "Confirmed website title, order and season cannot be changed.",
            )


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
    # Verify newly selected provider identities against TMDB, while confirmed
    # mappings remain usable even after the provider removes an episode.
    if body.config.episode_structure == "SEASONED" and body.config.season:
        with SyncSessionLocal() as session:
            existing = {
                f.id: mapping_value(f)
                for f in session.scalars(
                    select(File).where(File.project_id == project_id)
                )
            }
        placement_changed = (
            not previous or previous.identity() != body.config.identity()
        )
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
            if (incoming.original_description != before.original_description
                    and incoming.description == incoming.original_description):
                if before.description != before.original_description or not incoming.original_description:
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
        if previous and previous.identity() != body.config.identity():
            if any(f.web_identity_locked_at for f in files):
                raise PublishingError(
                    "web_identity_locked",
                    "A remote episode already exists; placement is locked.",
                )
            for f in files:
                f.web_mapping_json = f.web_tmdb_episode_id = f.web_publisher_key = None
        if body.mappings is not None:
            if len({m.file_id for m in body.mappings}) != len(body.mappings):
                raise PublishingError("web_mapping", "Duplicate file mappings.")
            for mapping in body.mappings:
                file = by_id.get(mapping.file_id)
                if file is None:
                    raise PublishingError(
                        "web_mapping", "Mapping references a file outside this project."
                    )
                value = mapping.model_dump(exclude={"file_id"})
                if file.web_identity_locked_at and mapping_value(file) != value:
                    raise PublishingError(
                        "web_identity_locked", "This remote episode mapping is locked."
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
                    file.web_publisher_key = (
                        file.web_publisher_key or f"subineko:{uuid.uuid4()}"
                    )
                else:
                    file.web_publisher_key = None
            targets = [
                f.web_tmdb_episode_id or f.web_publisher_key
                for f in files
                if f.web_mapping_json
            ]
            if len(targets) != len(set(targets)):
                raise PublishingError(
                    "web_mapping", "Two files target the same episode."
                )
        envelope.update(destination=destination(), config=config)
        if remote:
            envelope["title_uuid"] = remote["id"]
            p.web_identity_locked_at = p.web_identity_locked_at or now()
            if body.config.episode_structure == "FLAT" or body.config.season:
                from app.jobs.handlers.publish_web import locate

                captured = [
                    {
                        "file_id": f.id,
                        "tmdb_episode_id": f.web_tmdb_episode_id,
                        "publisher_key": f.web_publisher_key,
                        "episode_uuid": f.web_episode_uuid,
                    }
                    for f in files
                    if f.web_mapping_json
                ]
                season, resolved = locate(remote, body.config, captured)
                if envelope.get("season_uuid") and (
                    not season or season["id"] != envelope["season_uuid"]
                ):
                    raise PublishingError(
                        "web_recovery_required",
                        "Recorded website season is missing or changed. Explicit recovery is required.",
                    )
                if season:
                    envelope["season_uuid"] = season["id"]
                for fid, remote_id in resolved.items():
                    by_id[fid].web_episode_uuid = remote_id
                    by_id[fid].web_identity_locked_at = (
                        by_id[fid].web_identity_locked_at or now()
                    )
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
        captured = [
            {
                "file_id": f.id,
                "tmdb_episode_id": f.web_tmdb_episode_id,
                "publisher_key": f.web_publisher_key,
                "episode_uuid": f.web_episode_uuid,
            }
            for f in session.scalars(
                select(File).where(
                    File.project_id == project_id, File.web_mapping_json.is_not(None)
                )
            )
        ]
    snapshot = lookup(config)
    if envelope.get("title_uuid") and (
        not snapshot or snapshot["id"] != envelope["title_uuid"]
    ):
        raise PublishingError(
            "web_recovery_required",
            "Recorded website title is missing or changed. Explicit recovery is required.",
        )
    if snapshot:
        from app.jobs.handlers.publish_web import locate

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
    # Resolve provisional locks only after an explicit remote read, never by
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
            if any(
                session.get(File, item["file_id"]).web_episode_uuid
                != item["episode_uuid"]
                for item in captured
            ):
                return snapshot
            before_json, before_lock = p.web_config_json, p.web_identity_locked_at
            changed_files = False
            if snapshot:
                current["title_uuid"] = snapshot["id"]
                p.web_identity_locked_at = p.web_identity_locked_at or now()
                if config.episode_structure == "FLAT" or config.season:
                    season, resolved = locate(snapshot, config, captured)
                    if season:
                        current["season_uuid"] = season["id"]
                    for item in captured:
                        f = session.get(File, item["file_id"])
                        if item["file_id"] in resolved:
                            f.web_episode_uuid = resolved[item["file_id"]]
                            f.web_identity_locked_at = f.web_identity_locked_at or now()
                        elif not f.web_episode_uuid and item["file_id"] in current.get(
                            "pending_hierarchy", []
                        ):
                            f.web_identity_locked_at = None
                        changed_files = changed_files or session.is_modified(f)
            elif not current.get("title_uuid"):
                p.web_identity_locked_at = None
            current.pop("pending_title", None)
            current.pop("pending_hierarchy", None)
            p.web_config_json = dumps(current)
            if (
                changed_files
                or before_lock != p.web_identity_locked_at
                or before_json != p.web_config_json
            ):
                p.web_config_version += 1
            session.commit()
    return snapshot


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
        job_payload = json.loads(job.payload_json or "{}") if job else {}
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
                    "locked": bool(f.web_identity_locked_at),
                    "published_at": f.web_published_at,
                    "current_sha256": current_hash,
                    "published_sha256": f.web_published_sha256,
                    "render_error": error,
                }
            )
        accepted = [r for r in file_rows if r["status"] == "accepted"]
        published = [r for r in accepted if r["published_at"]]
        changed = any(r["current_sha256"] != r["published_sha256"] for r in accepted)
        metadata_changed = bool(
            envelope
            and envelope.get("published_config_hash") != fingerprint(envelope["config"])
        )
        if p.web_publish_state == "publishing":
            status = "publishing"
        elif p.web_publish_state == "failed":
            status = "partial" if published else "failed"
        elif published:
            status = "changes_available" if changed or metadata_changed else "published"
        elif accepted and all(r["mapping"] for r in accepted):
            status = "ready"
        else:
            status = "configured" if envelope else "not_published"
        return {
            "version": p.web_config_version,
            "config": WebConfig.model_validate(envelope["config"]).model_dump() if envelope.get("config") else None,
            "locked": bool(p.web_identity_locked_at),
            "season_locked": bool(
                envelope.get("season_uuid") or envelope.get("pending_hierarchy")
            ),
            "state": status,
            "files": file_rows,
            "release_label": envelope.get("release_label")
            or release_suggestion(files[0].filename if files else ""),
            "job": {
                "id": job.id,
                "status": job.status,
                "result": json.loads(job.result_json or "null"),
                "error": job.error_message,
                "file_ids": job_payload.get("file_ids", []),
                "release_label": job_payload.get("release_label", ""),
                "changelog": job_payload.get("changelog"),
            }
            if job
            else None,
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
            await session.commit()
            attempt = await session.scalar(
                select(Project.web_publish_attempt).where(Project.id == project_id)
            )
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

"""One explicit WEB attempt; remote history and idempotency belong to the website.

A publication always synchronizes the title, the season/episode structure (every
stored TMDB ``ProjectEpisode`` plus mapped files) and the poster; selecting files
only adds subtitle uploads, so a metadata-only attempt (no files) is a normal
run. Durable remote ids returned by the website are recorded as soon as they are
known — after the title, after every episode batch, also on partial failure — and
are the only thing that locks an episode.

The job result (``JobRecord.result_json``) is the live view for the UI:
``operations`` is the actual log of website requests, ``progress`` the monotonic
fraction of this job's real work units, ``receipt`` the project-level confirmed
progress.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import JobRecord, Project
from app.jobs.registry import register_job_handler
from app.publishing import service
from app.publishing.remote import (  # noqa: F401  (compat re-exports)
    has_identity,
    locate,
    pending_refs,
)
from app.publishing.remote import (
    provider_mapping as mapping,
)
from app.publishing.schemas import PublishRequest, WebConfig
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError, WebClient

BATCH = 500  # website limit is 1000 episodes per request
MAX_OPERATIONS = 2000


@register_job_handler("publish_web")
def publish_web(payload, ctx, progress):
    pid, attempt = payload["project_id"], payload["attempt"]
    result = {
        "stage": "Preparing",
        "progress": 0.0,
        "work": {"done": 0, "total": 1},
        "operations": [],
        "files": [],
        "poster": None,
        "metadata": [],
        "receipt": None,
        "error": None,
    }
    client = None
    outcome = "failed"
    key = f"publish_web:{pid}:{attempt}"
    with service._live_lock:
        service._live.add((pid, attempt))

    def check():
        with SyncSessionLocal() as session:
            p = session.get(Project, pid)
            if (
                not p
                or p.web_publish_attempt != attempt
                or p.web_publish_state != "publishing"
                or p.archived_at
            ):
                raise PublishingError("web_cancelled", "WEB publication stopped.")
            job = session.scalar(select(JobRecord).where(JobRecord.dedupe_key == key))
            if job and job.status == "cancelled":
                raise PublishingError(
                    "web_cancelled",
                    "WEB publication cancelled; acknowledged remote operations were retained.",
                )
            service.load(p)  # Destination changes fail closed, including between calls.

    def save_result():
        del result["operations"][:-MAX_OPERATIONS]
        with SyncSessionLocal() as session:
            job = session.scalar(select(JobRecord).where(JobRecord.dedupe_key == key))
            if job:
                job.result_json = service.dumps(result)
                session.commit()

    def checkpoint(stage):
        result["stage"] = stage
        save_result()
        progress(result["progress"], stage)

    def advance(stage, units=1):
        """Complete real work units; the fraction can only grow."""
        work = result["work"]
        work["done"] = min(work["total"], work["done"] + units)
        result["progress"] = max(result["progress"], round(work["done"] / work["total"], 4))
        checkpoint(stage)

    def begin(stage, **detail):
        op = {
            "seq": len(result["operations"]) + 1,
            "stage": stage,
            "status": "running",
            "outcome": "running",
            "started_at": service.now(),
            "finished_at": None,
            **({"detail": detail} if detail else {}),
        }
        result["operations"].append(op)
        save_result()
        return op

    def end(op, outcome, exc=None, **detail):
        op["status"] = "failed" if outcome in ("failed", "uncertain") else "succeeded"
        op["outcome"] = outcome
        op["finished_at"] = service.now()
        if detail:
            op["detail"] = {**op.get("detail", {}), **detail}
        if exc is not None:
            op["error"], op["code"] = exc.message, exc.code
        save_result()

    def step(stage, fn, *, label=lambda value: value["outcome"], **detail):
        """Run one website operation and log what actually happened."""
        op = begin(stage, **detail)
        try:
            value = fn()
        except PublishingError as exc:
            end(op, "uncertain" if exc.code == "web_uncertain" else "failed", exc)
            raise
        end(op, label(value))
        return value

    def persist(**changes):
        """Write the (in-memory) approval envelope back; never touches ids/locks."""
        with SyncSessionLocal() as session:
            p = session.get(Project, pid)
            if not p or p.web_publish_attempt != attempt:
                raise PublishingError(
                    "web_superseded", "Publication attempt was superseded."
                )
            for name, value in changes.items():
                if value is None:
                    envelope.pop(name, None)
                else:
                    envelope[name] = value
            envelope["destination"] = service.destination()  # about to talk to this site
            p.web_config_json = service.dumps(envelope)
            session.commit()

    def adopt(snapshot):
        """Record the durable remote ids a snapshot proves, and lock exactly those."""
        with SyncSessionLocal() as session:
            p = session.get(Project, pid)
            if not p or p.web_publish_attempt != attempt:
                raise PublishingError(
                    "web_superseded", "Publication attempt was superseded."
                )
            season, resolved = service.record_remote(
                session, p, envelope, config, snapshot, targets
            )
            p.web_config_json = service.dumps(envelope)
            session.commit()
        return season, resolved

    def reconcile(stage):
        """After a lost acknowledgement: learn the real outcome by reading, never by guessing."""
        try:
            op = begin(stage)
        except Exception:  # noqa: BLE001 - best effort only
            return
        try:
            snapshot = client.lookup(config)
            if snapshot:
                service.compatible(snapshot, config)
                _, resolved = adopt(snapshot)
                persist(
                    pending_title=None,
                    pending_hierarchy=[
                        r for r in pending_refs(envelope) if r not in resolved
                    ]
                    or None,
                )
            if not snapshot:
                persist(pending_title=None, pending_hierarchy=None)  # a read proves absence
            end(op, "found" if snapshot else "not_found")
        except PublishingError as exc:
            if exc.code == "web_cancelled":
                end(op, "failed", exc)
                raise
            end(op, "failed", exc)  # markers stay: a later explicit read resolves them

    try:
        check()
        body = PublishRequest.model_validate(
            {
                k: payload.get(k)
                for k in ("version", "file_ids", "release_label", "changelog")
            }
        )
        with SyncSessionLocal() as session:
            p = session.get(Project, pid)
            envelope = service.load(p)
            if envelope.get("config"):
                # A movie's content episode gets its stable publisher key once.
                service.ensure_content_episode(
                    session, pid, WebConfig.model_validate(envelope["config"])
                )
                # Every mapped file's episode gets a stable ProjectEpisode that
                # carries the receipts (also backfills older file-only receipts).
                service.ensure_target_episodes(session, pid)
                session.commit()
        with SyncSessionLocal() as session:
            session.connection().exec_driver_sql("BEGIN")
            p = session.get(Project, pid)
            envelope, config, targets = service.prepare(session, p, body, ctx.options)
        client = WebClient(check)
        uploads = [t for t in targets if t.get("content") or t.get("render_error")]
        seasoned = config.episode_structure == "SEASONED"
        batches = [targets[i : i + BATCH] for i in range(0, len(targets), BATCH)]
        if seasoned and not batches:
            batches = [[]]  # the season itself is part of the structure
        result["work"]["total"] = (
            2
            + len(batches)
            + (1 if config.poster_path else 0)
            + len(uploads)
            + 1  # progress synchronization
            + 1
        )
        result["episodes"] = {"total": len(targets), "uploads": len(uploads)}
        checkpoint("Title metadata")

        snapshot = step(
            "Lookup",
            lambda: client.lookup(config),
            label=lambda value: "found" if value else "not_found",
        )
        if envelope.get("title_uuid") and (
            not snapshot or snapshot["id"] != envelope["title_uuid"]
        ):
            raise PublishingError(
                "web_recovery_required",
                "Previously confirmed website title is missing or changed. Explicit recovery is required.",
            )
        if snapshot:
            service.compatible(snapshot, config)
            adopt(snapshot)
        advance("Title metadata")

        # Persist intent before a mutation: a lost acknowledgement must not
        # reopen identities for remapping. A later lookup reconciles the intent.
        persist(
            pending_title=True,
            **({"release_label": body.release_label} if body.release_label else {}),
        )
        title_body = {
            **config.metadata.website_payload(),
            "media_type": config.media_type,
            "episode_structure": config.episode_structure,
            "episode_order": service.order(config),
            "mappings": [mapping(config.tmdb_title_id, config.media_type)],
        }
        path = f"/api/v1/titles/{snapshot['id']}" if snapshot else "/api/v1/titles"
        try:
            title_result = step("Title", lambda: client.metadata(path, title_body))
        except PublishingError as exc:
            if exc.code != "web_cancelled":
                reconcile("Reconcile title")
            raise
        snapshot = title_result["title"]
        adopt(snapshot)
        persist(pending_title=None)
        result["metadata"].append(
            {"stage": "Title", "outcome": title_result["outcome"]}
        )
        check()
        advance("Season/episode metadata")

        season, _ = locate(snapshot, config, targets)
        kind = "season" if config.order_kind == "standard" else "episode_group_subgroup"
        for index, batch in enumerate(batches):
            check()
            refs = [t["ref"] for t in batch]
            episodes = []
            for item in batch:
                episode = {
                    "episode_number": item["episode_number"],
                    "name_english": item["name_english"],
                    "mappings": [],
                }
                if item["tmdb_episode_id"]:
                    episode["mappings"] = [
                        mapping(item["tmdb_episode_id"], config.media_type)
                    ]
                else:
                    episode["publisher_key"] = item["publisher_key"]
                if item["episode_uuid"]:
                    episode["id"] = item["episode_uuid"]
                episodes.append(episode)
            # Unresolved until a response proves otherwise; blocks identity edits.
            persist(pending_hierarchy=sorted(set(pending_refs(envelope)) | set(refs)))
            if seasoned:
                hierarchy = {
                    **config.season.model_dump(exclude={"external_id"}),
                    "mappings": [mapping(config.season.external_id, config.media_type, kind)],
                    "episodes": episodes,
                }
                if season:
                    hierarchy["id"] = season["id"]
                url, request_body = f"/api/v1/titles/{snapshot['id']}/seasons", hierarchy
                stage = "Season"
            else:
                url, request_body = (
                    f"/api/v1/titles/{snapshot['id']}/episodes",
                    {"episodes": episodes},
                )
                stage = "Episodes"
            label = f"{stage} {index + 1} / {len(batches)}" if len(batches) > 1 else stage
            try:
                response = step(
                    label,
                    lambda url=url, request_body=request_body: client.metadata(url, request_body),
                    episodes=len(episodes),
                )
            except PublishingError as exc:
                # The entity-oriented website applies each PUT separately, so a
                # failure can follow confirmed ones: read back and durably record
                # every id that exists before reporting the error.
                if exc.code != "web_cancelled":
                    reconcile(f"Reconcile {label.lower()}")
                raise
            snapshot = response["title"]
            season, resolved = adopt(snapshot)
            # Whatever the website confirmed stays confirmed, even if the response
            # omitted part of the batch.
            persist(
                pending_hierarchy=[
                    r for r in pending_refs(envelope) if r not in resolved
                ]
                or None
            )
            result["metadata"].append({"stage": "Episodes", "outcome": response["outcome"]})
            if any(r not in resolved for r in refs):
                raise PublishingError(
                    "web_response", "Website response omitted a requested episode.", 502
                )
            advance(f"Season/episode metadata {index + 1} / {len(batches)}")

        # The catalog is now synchronized: record that independently of whatever
        # the poster or the subtitle uploads do next.
        persist(
            published_config_hash=service.fingerprint(config.model_dump()),
            published_episodes_hash=service.targets_fingerprint(targets),
        )

        if config.poster_path:
            checkpoint("Poster")
            check()
            try:
                poster = TmdbClient().poster(config.poster_path)
                uploaded = step(
                    "Poster",
                    lambda: client.upload(
                        f"/api/v1/titles/{snapshot['id']}/poster",
                        config.poster_path.rsplit("/", 1)[1],
                        poster,
                    ),
                )
                if (
                    uploaded.get("outcome") not in ("created", "updated", "noop")
                    or "poster" not in uploaded
                ):
                    raise PublishingError(
                        "web_response",
                        "Website returned an invalid poster response.",
                        502,
                    )
                result["poster"] = {"outcome": uploaded["outcome"]}
            except PublishingError as exc:
                if exc.code == "web_cancelled":
                    raise
                result["poster"] = {"outcome": "failed", "error": exc.message}
            advance("Poster")
        else:
            result["poster"] = {"outcome": "unavailable"}

        for index, item in enumerate(uploads):
            check()
            checkpoint(f"Subtitle uploads {index} / {len(uploads)}")
            file_result = {"file_id": item["file_id"], "filename": item["filename"]}
            if item.get("render_error"):
                exc = PublishingError(item.get("render_code", "web_render"), item["render_error"])
                end(begin("Subtitle", file_id=item["file_id"]), "failed", exc)
                file_result.update(outcome="failed", error=exc.message, code=exc.code)
                result["files"].append(file_result)
                advance(f"Subtitle uploads {index + 1} / {len(uploads)}")
                continue
            try:
                fields = {"release_label": body.release_label}
                if body.changelog is not None:
                    fields["changelog"] = body.changelog
                uploaded = step(
                    "Subtitle",
                    lambda item=item, fields=fields: client.upload(
                        f"/api/v1/episodes/{item['episode_uuid']}/subtitles",
                        item["filename"],
                        item["content"],
                        fields,
                    ),
                    file_id=item["file_id"],
                    episode_number=item["episode_number"],
                )
                revision = uploaded.get("revision", {})
                if (
                    uploaded.get("outcome") not in ("created", "updated", "noop")
                    or revision.get("sha256") != item["sha256"]
                ):
                    raise PublishingError(
                        "web_response",
                        "Website returned an invalid subtitle receipt.",
                        502,
                    )
                file_result["outcome"] = uploaded["outcome"]
                with SyncSessionLocal() as session:
                    p = session.get(Project, pid)
                    if p and p.web_publish_attempt == attempt:
                        # The receipt belongs to the episode (and survives any
                        # later file reassignment).
                        service.record_subtitle_receipt(
                            session,
                            item,
                            item["sha256"],
                            revision.get("revisionNumber") or revision.get("revision_number"),
                        )
                        session.commit()
            except PublishingError as exc:
                if exc.code == "web_cancelled":
                    raise
                file_result.update(outcome="failed", error=exc.message, code=exc.code)
            result["files"].append(file_result)
            advance(f"Subtitle uploads {index + 1} / {len(uploads)}")
        check()
        checkpoint("Final verification")
        final = step(
            "Verify",
            lambda: client.get_title(snapshot["id"]),
            label=lambda _value: "verified",
        )
        service.compatible(final, config)
        _, verified = locate(final, config, targets)  # raises if a confirmed episode vanished
        all_episodes = final.get("episodes", []) + [
            e for s in final.get("seasons", []) for e in s.get("episodes", [])
        ]
        for row in result["files"]:
            item = next(i for i in uploads if i["file_id"] == row["file_id"])
            remote = next(
                (e for e in all_episodes if e["id"] == verified.get(item["ref"])), {}
            )
            remote_sha = (remote.get("current_subtitle") or {}).get("sha256")
            if row["outcome"] != "failed":
                if remote_sha != item["sha256"]:
                    row.update(
                        outcome="failed",
                        code="web_concurrent_change",
                        error="Website current subtitle changed during publication. Review before retrying.",
                    )
            elif item.get("sha256") and remote_sha == item["sha256"]:
                # The upload's acknowledgement was lost (or malformed) but the final
                # read proves the website holds exactly the captured bytes: that read
                # is the publication receipt. Anything else stays a failed upload.
                op = begin("Reconcile subtitle", file_id=item["file_id"])
                with SyncSessionLocal() as session:
                    p = session.get(Project, pid)
                    if p and p.web_publish_attempt == attempt:
                        revision = remote.get("current_subtitle") or {}
                        service.record_subtitle_receipt(
                            session,
                            item,
                            item["sha256"],
                            revision.get("revisionNumber") or revision.get("revision_number"),
                        )
                        session.commit()
                end(op, "reconciled")
                row.pop("error", None)
                row.pop("code", None)
                row["outcome"] = "reconciled"
        # Website translationProgress = THIS project's confirmed-subtitle ratio
        # (never an aggregate over projects sharing the title, never a maximum).
        # It is synchronized by every attempt, uploads or not, whenever the
        # project has its own subtitle receipts or already synchronized its own
        # progress; a changed expected count is therefore picked up by the next
        # attempt (even if it lowers the value) and a failed update is retried
        # without repeating any upload. A project with neither leaves whatever
        # the website already shows untouched.
        with SyncSessionLocal() as session:
            receipt = service.receipt_progress(session, session.get(Project, pid))
        percent = receipt["percent"]
        remote_progress = final.get("translation_progress")
        synced = envelope.get("last_synced_project_progress")
        own = receipt["subtitles_published"] > 0 or synced is not None
        result["progress_update"] = {"percent": percent}
        if percent is None or not own:
            reason = (
                "no expected episode count" if percent is None else "no project receipts yet"
            )
            end(begin("Progress", percent=percent), "skipped", reason=reason)
            result["progress_update"]["outcome"] = "skipped"
        elif remote_progress == percent:
            end(begin("Progress", percent=percent), "noop", reason="already current")
            persist(last_synced_project_progress=percent)
            result["progress_update"]["outcome"] = "noop"
        else:
            try:
                update = step(
                    "Progress",
                    lambda: client.update_progress(config, percent),
                    label=lambda value: value["outcome"],
                    percent=percent,
                )
                persist(last_synced_project_progress=percent)
                result["progress_update"]["outcome"] = update["outcome"]
            except PublishingError as exc:
                if exc.code == "web_cancelled":
                    raise
                result["progress_update"].update(outcome="failed", error=exc.message)
        advance("Progress")
        failed = (
            any(r["outcome"] == "failed" for r in result["files"])
            or result["poster"]["outcome"] == "failed"
            or result["progress_update"].get("outcome") == "failed"
        )
        outcome = "failed" if failed else "published"
        advance(
            "Partially published / Failed" if failed else "Published",
            result["work"]["total"] - result["work"]["done"],  # the job is finished
        )
        return {
            "status": "failed" if failed else "succeeded",
            "result": result,
            "error_code": "web_partial" if failed else None,
            "error_message": "Some operations failed; successful uploads were retained."
            if failed
            else None,
        }
    except PublishingError as exc:
        result["error"] = {"code": exc.code, "message": exc.message}
        return {
            "status": "failed",
            "result": result,
            "error_code": exc.code,
            "error_message": exc.message,
        }
    except Exception:  # noqa: BLE001 - sanitize all upstream errors before JobManager logging
        # Never allow httpx URLs, tokens or validation input into job logs/results.
        result["error"] = {
            "code": "web_unexpected",
            "message": "WEB publication stopped unexpectedly. Retry to reconcile acknowledged operations.",
        }
        return {
            "status": "failed",
            "result": result,
            "error_code": "web_unexpected",
            "error_message": result["error"]["message"],
        }
    finally:
        try:
            if client:
                client.close()
            with SyncSessionLocal() as session:
                p = session.get(Project, pid)
                if p and p.web_publish_attempt == attempt:
                    p.web_publish_state = outcome
                    result["receipt"] = service.receipt_progress(session, p)
                    job = session.scalar(
                        select(JobRecord).where(JobRecord.dedupe_key == key)
                    )
                    if job:
                        job.result_json = service.dumps(result)
                    session.commit()
        finally:
            # A close/commit failure must not leave a dead thread marked live;
            # the existing reconciler can release its durable claim afterwards.
            with service._live_lock:
                service._live.discard((pid, attempt))

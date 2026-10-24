"""One explicit WEB attempt; remote history and idempotency belong to the website."""

from __future__ import annotations

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import File, JobRecord, Project
from app.jobs.registry import register_job_handler
from app.publishing import service
from app.publishing.schemas import PublishRequest
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError, WebClient


def mapping(external_id, media, kind=None):
    value = {"provider": "tmdb", "media_type": media, "external_id": external_id}
    if kind:
        value["kind"] = kind
    return value


def has_identity(entity, external_id, kind=None):
    return any(
        m.get("provider") == "tmdb"
        and m.get("external_id") == external_id
        and (kind is None or m.get("kind") == kind)
        for m in entity.get("mappings", [])
    )


def locate(snapshot, config, captured):
    season = None
    if config.episode_structure == "SEASONED":
        kind = "season" if config.order_kind == "standard" else "episode_group_subgroup"
        season = next(
            (
                s
                for s in snapshot.get("seasons", [])
                if has_identity(s, config.season.external_id, kind)
            ),
            None,
        )
        episodes = season.get("episodes", []) if season else []
    else:
        episodes = snapshot.get("episodes", [])
    resolved = {}
    for item in captured:
        found = next(
            (
                e
                for e in episodes
                if (
                    has_identity(e, item["tmdb_episode_id"])
                    if item["tmdb_episode_id"]
                    else e.get("publisher_key") == item["publisher_key"]
                )
            ),
            None,
        )
        if item["episode_uuid"] and (not found or found["id"] != item["episode_uuid"]):
            raise PublishingError(
                "web_recovery_required",
                f"Previously confirmed episode for file {item['file_id']} is missing or changed. Explicit recovery is required.",
            )
        if found:
            resolved[item["file_id"]] = found["id"]
    return season, resolved


@register_job_handler("publish_web")
def publish_web(payload, ctx, progress):
    pid, attempt = payload["project_id"], payload["attempt"]
    result = {
        "stage": "Preparing",
        "files": [],
        "poster": None,
        "metadata": [],
        "error": None,
    }
    client = None
    outcome = "failed"
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
            job = session.scalar(
                select(JobRecord).where(
                    JobRecord.dedupe_key == f"publish_web:{pid}:{attempt}"
                )
            )
            if job and job.status == "cancelled":
                raise PublishingError(
                    "web_cancelled",
                    "WEB publication cancelled; acknowledged remote operations were retained.",
                )
            service.load(p)  # Destination changes fail closed, including between calls.

    def checkpoint(stage, pct):
        result["stage"] = stage
        with SyncSessionLocal() as session:
            job = session.scalar(
                select(JobRecord).where(
                    JobRecord.dedupe_key == f"publish_web:{pid}:{attempt}"
                )
            )
            if job:
                job.result_json = service.dumps(result)
                session.commit()
        progress(pct, stage)

    def persist(envelope, resolved=None, lock_files=False):
        with SyncSessionLocal() as session:
            p = session.get(Project, pid)
            if not p or p.web_publish_attempt != attempt:
                raise PublishingError(
                    "web_superseded", "Publication attempt was superseded."
                )
            p.web_config_json = service.dumps(envelope)
            p.web_identity_locked_at = p.web_identity_locked_at or service.now()
            for item in captured if lock_files else []:
                f = session.get(File, item["file_id"])
                f.web_identity_locked_at = f.web_identity_locked_at or service.now()
            for fid, remote_id in (resolved or {}).items():
                f = session.get(File, fid)
                f.web_episode_uuid = remote_id
                f.web_identity_locked_at = f.web_identity_locked_at or service.now()
            session.commit()

    try:
        check()
        body = PublishRequest.model_validate(
            {
                k: payload[k]
                for k in ("version", "file_ids", "release_label", "changelog")
            }
        )
        with SyncSessionLocal() as session:
            session.connection().exec_driver_sql("BEGIN")
            p = session.get(Project, pid)
            envelope, config, captured = service.prepare(session, p, body, ctx.options)
        client = WebClient(check)
        checkpoint("Title metadata", 0.05)
        snapshot = client.lookup(config)
        if envelope.get("title_uuid") and (
            not snapshot or snapshot["id"] != envelope["title_uuid"]
        ):
            raise PublishingError(
                "web_recovery_required",
                "Previously confirmed website title is missing or changed. Explicit recovery is required.",
            )
        if snapshot:
            service.compatible(snapshot, config)
            season, resolved = locate(snapshot, config, captured)
            if envelope.get("season_uuid") and (
                not season or season["id"] != envelope["season_uuid"]
            ):
                raise PublishingError(
                    "web_recovery_required",
                    "Previously confirmed website season is missing or changed. Explicit recovery is required.",
                )
            envelope["title_uuid"] = snapshot["id"]
            persist(envelope, resolved)

        # Persist intent before a mutation: a lost acknowledgement must not
        # reopen identities for remapping. A later lookup reconciles the intent.
        envelope["pending_title"] = True
        envelope["release_label"] = body.release_label
        persist(envelope)
        title_body = {
            **config.metadata.website_payload(),
            "media_type": config.media_type,
            "episode_structure": config.episode_structure,
            "episode_order": service.order(config),
            "mappings": [mapping(config.tmdb_title_id, config.media_type)],
        }
        path = f"/api/v1/titles/{snapshot['id']}" if snapshot else "/api/v1/titles"
        title_result = client.metadata(path, title_body)
        snapshot = title_result["title"]
        envelope["title_uuid"] = snapshot["id"]
        envelope.pop("pending_title", None)
        result["metadata"].append(
            {"stage": "Title", "outcome": title_result["outcome"]}
        )
        persist(envelope)
        check()
        checkpoint("Season/episode metadata", 0.20)
        season, resolved = locate(snapshot, config, captured)
        if envelope.get("season_uuid") and (
            not season or season["id"] != envelope["season_uuid"]
        ):
            raise PublishingError(
                "web_recovery_required",
                "The recorded season disappeared. Explicit recovery is required.",
            )
        episodes = []
        for item in captured:
            episode = {
                "episode_number": item["mapping"]["episode_number"],
                "name_english": item["mapping"]["name_english"],
                "mappings": [],
            }
            if item["tmdb_episode_id"]:
                episode["mappings"] = [
                    mapping(item["tmdb_episode_id"], config.media_type)
                ]
            else:
                episode["publisher_key"] = item["publisher_key"]
            if item["file_id"] in resolved:
                episode["id"] = resolved[item["file_id"]]
            episodes.append(episode)
        envelope["pending_hierarchy"] = [i["file_id"] for i in captured]
        persist(envelope, resolved, lock_files=True)
        if config.episode_structure == "SEASONED":
            kind = (
                "season"
                if config.order_kind == "standard"
                else "episode_group_subgroup"
            )
            hierarchy = {
                **config.season.model_dump(exclude={"external_id"}),
                "mappings": [
                    mapping(config.season.external_id, config.media_type, kind)
                ],
                "episodes": episodes,
            }
            if season:
                hierarchy["id"] = season["id"]
            response = client.metadata(
                f"/api/v1/titles/{snapshot['id']}/seasons", hierarchy
            )
        else:
            response = client.metadata(
                f"/api/v1/titles/{snapshot['id']}/episodes", {"episodes": episodes}
            )
        snapshot = response["title"]
        season, resolved = locate(snapshot, config, captured)
        if len(resolved) != len(captured):
            raise PublishingError(
                "web_response", "Website response omitted a requested episode.", 502
            )
        if season:
            envelope["season_uuid"] = season["id"]
        envelope.pop("pending_hierarchy", None)
        persist(envelope, resolved)
        result["metadata"].append({"stage": "Episodes", "outcome": response["outcome"]})
        checkpoint("Poster", 0.35)
        if config.poster_path:
            check()
            try:
                poster = TmdbClient().poster(config.poster_path)
                uploaded = client.upload(
                    f"/api/v1/titles/{snapshot['id']}/poster",
                    config.poster_path.rsplit("/", 1)[1],
                    poster,
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
        else:
            result["poster"] = {"outcome": "unavailable"}

        for index, item in enumerate(captured):
            check()
            checkpoint(
                f"Subtitle uploads {index} / {len(captured)}",
                0.40 + 0.50 * index / len(captured),
            )
            file_result = {"file_id": item["file_id"], "filename": item["filename"]}
            try:
                fields = {"release_label": body.release_label}
                if body.changelog is not None:
                    fields["changelog"] = body.changelog
                uploaded = client.upload(
                    f"/api/v1/episodes/{resolved[item['file_id']]}/subtitles",
                    item["filename"],
                    item["content"],
                    fields,
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
                        f = session.get(File, item["file_id"])
                        f.web_published_sha256 = item["sha256"]
                        f.web_published_at = service.now()
                        session.commit()
            except PublishingError as exc:
                if exc.code == "web_cancelled":
                    raise
                file_result.update(outcome="failed", error=exc.message, code=exc.code)
            result["files"].append(file_result)
        check()
        checkpoint(
            f"Subtitle uploads {len(captured)} / {len(captured)}; final verification",
            0.95,
        )
        final = client.get_title(snapshot["id"])
        service.compatible(final, config)
        _, verified = locate(
            final,
            config,
            [{**i, "episode_uuid": resolved[i["file_id"]]} for i in captured],
        )
        all_episodes = final.get("episodes", []) + [
            e for s in final.get("seasons", []) for e in s.get("episodes", [])
        ]
        for row in result["files"]:
            item = next(i for i in captured if i["file_id"] == row["file_id"])
            remote = next(
                (e for e in all_episodes if e["id"] == verified[item["file_id"]]), {}
            )
            if (
                row["outcome"] != "failed"
                and (remote.get("current_subtitle") or {}).get("sha256")
                != item["sha256"]
            ):
                row.update(
                    outcome="failed",
                    code="web_concurrent_change",
                    error="Website current subtitle changed during publication. Review before retrying.",
                )
        failed = (
            any(r["outcome"] == "failed" for r in result["files"])
            or result["poster"]["outcome"] == "failed"
        )
        outcome = "failed" if failed else "published"
        if not failed:
            envelope["published_config_hash"] = service.fingerprint(config.model_dump())
            persist(envelope)
        checkpoint("Partially published / Failed" if failed else "Published", 1.0)
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
                    job = session.scalar(
                        select(JobRecord).where(
                            JobRecord.dedupe_key == f"publish_web:{pid}:{attempt}"
                        )
                    )
                    if job:
                        job.result_json = service.dumps(result)
                    session.commit()
        finally:
            # A close/commit failure must not leave a dead thread marked live;
            # the existing reconciler can release its durable claim afterwards.
            with service._live_lock:
                service._live.discard((pid, attempt))

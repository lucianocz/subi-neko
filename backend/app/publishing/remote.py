"""Episode targets, durable remote identities and receipts for WEB publishing.

Everything here is about *which remote episodes a project should have* and *which
of them the website has confirmed*:

* ``collect_targets`` — every stored TMDB ``ProjectEpisode`` (plus a movie's
  content episode and any mapped file) is a target, regardless of which files a
  publication uploads;
* ``locate``/``bind_resolved``/``record_remote`` — adopt remote ids from a
  website response. Ids are the *only* thing that locks an episode and they are
  never cleared or replaced;
* ``receipt_progress`` — project-level progress from confirmed receipts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import File, Project, ProjectEpisode
from app.publishing.web_client import PublishingError, destination


def now():
    return datetime.now(UTC).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def mapping_value(file):
    return json.loads(file.web_mapping_json) if file.web_mapping_json else None


def clean_name(value):
    value = value.strip() if isinstance(value, str) else ""
    return value[:255] or None


def ref_of(tmdb_episode_id, publisher_key):
    """Stable identity of one remote episode target inside a project."""
    return f"t:{tmdb_episode_id}" if tmdb_episode_id else f"k:{publisher_key}"


def has_identity(entity, external_id, kind=None):
    return any(
        m.get("provider") == "tmdb"
        and m.get("external_id") == external_id
        and (kind is None or m.get("kind") == kind)
        for m in entity.get("mappings", [])
    )


def provider_mapping(external_id, media, kind=None):
    value = {"provider": "tmdb", "media_type": media, "external_id": external_id}
    if kind:
        value["kind"] = kind
    return value


def ensure_content_episode(session, project_id, config):
    """A movie's single content episode needs a stable publisher key.

    The website identifies FLAT episodes without a TMDB episode id by a
    title-scoped ``publisher_key``; a TMDB id is never invented. The key is
    generated once on a ``ProjectEpisode`` row and reused by the first mapped
    file. Skipped when files already carry their own keys.
    """
    if config.media_type != "movie":
        return None
    rows = list(
        session.scalars(
            select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)
        )
    )
    existing = next((r for r in rows if r.web_publisher_key), None)
    if existing:
        return existing
    if session.scalar(
        select(File.id).where(
            File.project_id == project_id, File.web_publisher_key.is_not(None)
        )
    ):
        return None
    row = next((r for r in rows if r.episode_number == 1), None)
    if row is None:
        row = ProjectEpisode(project_id=project_id, episode_number=1)
        session.add(row)
    row.web_publisher_key = f"subineko:{uuid.uuid4()}"
    session.flush()
    return row


def ensure_target_episodes(session, project_id):
    """Give every mapped file's episode a stable ``ProjectEpisode`` row.

    Receipts (remote id, lock, subtitle) live on the episode, so a file can be
    reassigned without changing identity or confirmed progress. A file-only
    target (e.g. FLAT TV) gets its row here; receipts a file already carried are
    copied over once. A provider-only row with the same number is adopted; a
    row with a different identity at that number is a conflict."""
    rows = list(
        session.scalars(
            select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)
        )
    )
    by_tmdb = {r.tmdb_episode_id: r for r in rows if r.tmdb_episode_id}
    by_key = {r.web_publisher_key: r for r in rows if r.web_publisher_key}
    by_number = {r.episode_number: r for r in rows}
    files = session.scalars(
        select(File)
        .where(File.project_id == project_id, File.web_mapping_json.is_not(None))
        .order_by(File.id)
    )
    for f in files:
        tmdb, key = f.web_tmdb_episode_id, f.web_publisher_key
        if not (tmdb or key):
            continue
        pe = by_tmdb.get(tmdb) if tmdb else by_key.get(key)
        mapping = mapping_value(f) or {}
        if pe is None:
            number = mapping.get("episode_number", 0)
            pe = by_number.get(number)
            if pe is not None and (pe.tmdb_episode_id or pe.web_publisher_key):
                raise PublishingError(
                    "web_mapping",
                    f"Episode number {number} already belongs to another project episode.",
                )
            if pe is None:
                pe = by_number[number] = ProjectEpisode(
                    project_id=project_id, episode_number=number
                )
                session.add(pe)
            if tmdb:
                pe.tmdb_episode_id, pe.tmdb_name = (
                    tmdb,
                    clean_name(mapping.get("name_english")),
                )
                by_tmdb[tmdb] = pe
            else:
                pe.web_publisher_key = key
                by_key[key] = pe
        # One-time backfill of receipts a file recorded before they moved here.
        if f.web_episode_uuid and not pe.web_episode_uuid:
            pe.web_episode_uuid = f.web_episode_uuid
            pe.web_identity_locked_at = f.web_identity_locked_at or now()
            pe.web_confirmed_at = pe.web_confirmed_at or now()
        if f.web_published_sha256 and not pe.web_subtitle_sha256:
            pe.web_subtitle_sha256 = f.web_published_sha256
            pe.web_subtitle_published_at = f.web_published_at
    session.flush()


def detach_file(file):
    """Unassign a file from its episode without touching the episode itself.

    The stable ``web_publisher_key`` stays with the file (an identity token, never
    reassigned); the mapping and everything this file recorded about an upload
    association are cleared. Confirmed receipts remain on the ProjectEpisode."""
    file.web_mapping_json = file.web_tmdb_episode_id = None
    file.web_episode_uuid = file.web_identity_locked_at = None
    file.web_published_sha256 = file.web_published_at = None


def collect_targets(session, project_id, config, selected=None):
    """Every remote episode this project should have, regardless of uploads.

    Stored TMDB ``ProjectEpisode`` rows (and a movie's content episode) are
    always targets; mapped files overlay number/name and may add targets that
    exist only as a file mapping. ``selected`` (file ids) only flags which
    targets also upload a subtitle."""
    pes = list(
        session.scalars(
            select(ProjectEpisode)
            .where(ProjectEpisode.project_id == project_id)
            .order_by(ProjectEpisode.episode_number)
        )
    )
    files = list(
        session.scalars(
            select(File)
            .where(File.project_id == project_id, File.web_mapping_json.is_not(None))
            .order_by(File.id)
        )
    )
    targets: dict[str, dict] = {}
    movie_ref = None
    for pe in pes:
        if config.episode_structure == "SEASONED" and pe.tmdb_episode_id:
            tmdb, key = pe.tmdb_episode_id, None
        elif (
            config.episode_structure == "FLAT"
            and pe.web_publisher_key
            and not pe.tmdb_episode_id
        ):
            tmdb, key = None, pe.web_publisher_key
            movie_ref = ref_of(None, key)
        else:
            continue
        targets[ref_of(tmdb, key)] = {
            "ref": ref_of(tmdb, key),
            "episode_number": pe.episode_number,
            "name_english": clean_name(pe.tmdb_name),
            "tmdb_episode_id": tmdb,
            "publisher_key": key,
            "episode_uuid": pe.web_episode_uuid,
            "file_id": None,
            "pe_id": pe.id,
            "upload": False,
        }
    movie_shared = False
    for f in files:
        if not (f.web_tmdb_episode_id or f.web_publisher_key):
            continue
        mapping = mapping_value(f) or {}
        ref = ref_of(f.web_tmdb_episode_id, f.web_publisher_key)
        target = targets.get(ref)
        if target is None:
            target = targets[ref] = {
                "ref": ref,
                "tmdb_episode_id": f.web_tmdb_episode_id,
                "publisher_key": f.web_publisher_key,
                "pe_id": None,
                "episode_uuid": None,
            }
        elif ref == movie_ref:
            movie_shared = True
        target.update(
            episode_number=mapping.get(
                "episode_number", target.get("episode_number", 0)
            ),
            name_english=clean_name(mapping.get("name_english"))
            or target.get("name_english"),
            file_id=f.id,
            upload=selected is not None and f.id in selected,
        )
        target["episode_uuid"] = target.get("episode_uuid") or f.web_episode_uuid
    # The content episode stands in for the movie only while no file targets an
    # episode of its own (otherwise it would be a stray empty episode).
    if (
        movie_ref
        and not movie_shared
        and not targets[movie_ref]["episode_uuid"]
        and any(t["file_id"] for t in targets.values())
    ):
        del targets[movie_ref]
    for t in targets.values():
        t.setdefault("file_id", None)
        t.setdefault("upload", False)
    return sorted(targets.values(), key=lambda t: (t["episode_number"], t["ref"]))


def locate(snapshot, config, targets):
    """Find the remote season and the episodes of ``targets``.

    ``resolved`` maps target ref -> remote episode id. A target that already has
    a confirmed id but cannot be found (or has a different id) is a deletion or
    rebind on the website and requires explicit recovery."""
    season = None
    if config.episode_structure == "SEASONED":
        if not config.season:
            return None, {}
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
    for item in targets:
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
                f"Previously confirmed episode {item['episode_number']} is missing or changed. Explicit recovery is required.",
            )
        if found:
            resolved[item["ref"]] = found["id"]
    return season, resolved


def bind_resolved(session, targets, resolved):
    """Record durable remote ids (receipts) and lock exactly those episodes."""
    stamp = now()
    for item in targets:
        remote_id = resolved.get(item["ref"])
        if not remote_id:
            continue
        item["episode_uuid"] = remote_id
        if item.get("file_id"):
            f = session.get(File, item["file_id"])
            f.web_episode_uuid = remote_id
            f.web_identity_locked_at = f.web_identity_locked_at or stamp
        if item.get("pe_id"):
            pe = session.get(ProjectEpisode, item["pe_id"])
            pe.web_episode_uuid = remote_id
            pe.web_identity_locked_at = pe.web_identity_locked_at or stamp
            pe.web_confirmed_at = pe.web_confirmed_at or stamp


def record_subtitle_receipt(session, item, sha256, revision):
    """The website confirmed this episode's subtitle: persist the episode receipt
    (and the uploading file's convenience copy)."""
    stamp = now()
    if item.get("pe_id"):
        pe = session.get(ProjectEpisode, item["pe_id"])
        pe.web_subtitle_sha256 = sha256
        pe.web_subtitle_revision = revision
        pe.web_subtitle_published_at = stamp
    f = session.get(File, item["file_id"])
    f.web_published_sha256, f.web_published_at = sha256, stamp


def record_remote(session, project, envelope, config, snapshot, targets):
    """Adopt a remote snapshot: durable title/season/episode ids and their locks."""
    if envelope.get("title_uuid") and envelope["title_uuid"] != snapshot["id"]:
        raise PublishingError(
            "web_recovery_required", "Previously confirmed website title changed."
        )
    season, resolved = locate(snapshot, config, targets)
    if envelope.get("season_uuid") and (
        not season or season["id"] != envelope["season_uuid"]
    ):
        raise PublishingError(
            "web_recovery_required",
            "Recorded website season is missing or changed. Explicit recovery is required.",
        )
    envelope["title_uuid"] = snapshot["id"]
    # Durable remote ids exist from here on: the mapping is bound to this website
    # (an import-captured, unbound mapping must never send ids elsewhere later).
    envelope["destination"] = destination()
    project.web_identity_locked_at = project.web_identity_locked_at or now()
    if season:
        envelope["season_uuid"] = season["id"]
    bind_resolved(session, targets, resolved)
    return season, resolved


def pending_refs(envelope):
    value = envelope.get("pending_hierarchy") or []
    return value if isinstance(value, list) else []


def file_locked(envelope, file):
    """A file's episode is frozen by a durable remote id or an unresolved write."""
    if file.web_identity_locked_at or file.web_episode_uuid:
        return True
    pending = pending_refs(envelope)
    return (
        file.id in pending
        or ref_of(file.web_tmdb_episode_id, file.web_publisher_key) in pending
    )


def episodes_confirmed(session, project_id):
    """True when any episode already has a durable remote id."""
    return bool(
        session.scalar(
            select(File.id).where(
                File.project_id == project_id,
                (File.web_episode_uuid.is_not(None))
                | (File.web_identity_locked_at.is_not(None)),
            )
        )
        or session.scalar(
            select(ProjectEpisode.id).where(
                ProjectEpisode.project_id == project_id,
                (ProjectEpisode.web_episode_uuid.is_not(None))
                | (ProjectEpisode.web_identity_locked_at.is_not(None)),
            )
        )
    )


_TMDB_COLUMNS = (
    "tmdb_episode_id",
    "tmdb_name",
    "tmdb_air_date",
    "tmdb_overview",
    "tmdb_runtime",
)


def replace_tmdb_episodes(session, project_id, episodes):
    """Make the stored TMDB episodes equal ``episodes`` (unbound/unlocked edits).

    Confirmed rows (remote id recorded) are never touched. TMDB columns of the
    other rows are cleared first so identities can move between numbers without
    tripping the unique index; provider (AniDB) columns are preserved and rows
    left without any data are removed."""
    rows = {
        r.episode_number: r
        for r in session.scalars(
            select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)
        )
    }
    keep = {
        n for n, r in rows.items() if r.web_episode_uuid or r.web_identity_locked_at
    }
    for number, row in rows.items():
        if number not in keep:
            for column in _TMDB_COLUMNS:
                setattr(row, column, None)
    session.flush()
    seen_numbers = set()
    seen_ids = {r.tmdb_episode_id for r in rows.values() if r.tmdb_episode_id}
    for ep in sorted(
        (e for e in episodes if "episode_number" in e and e.get("tmdb_episode_id")),
        key=lambda e: e["episode_number"],
    ):
        number = ep["episode_number"]
        if (
            number in seen_numbers
            or number in keep
            or ep["tmdb_episode_id"] in seen_ids
        ):
            continue
        seen_numbers.add(number)
        seen_ids.add(ep["tmdb_episode_id"])
        row = rows.get(number)
        if row is None:
            row = rows[number] = ProjectEpisode(
                project_id=project_id, episode_number=number
            )
            session.add(row)
        row.tmdb_episode_id = ep["tmdb_episode_id"]
        row.tmdb_name = ep.get("name_english")
        row.tmdb_air_date = ep.get("air_date")
        row.tmdb_overview = ep.get("overview")
        row.tmdb_runtime = ep.get("runtime")
    session.flush()
    for row in rows.values():
        if not any(
            getattr(row, c)
            for c in (
                *_TMDB_COLUMNS,
                "title",
                "title_native",
                "air_date",
                "web_episode_uuid",
                "web_publisher_key",
            )
        ):
            session.delete(row)
    session.flush()


def receipt_progress(session, project: Project):
    """Project-level progress from confirmed *subtitle* receipts only.

    ``percent = clamp(100 * published episode subtitles / expected_episode_count)``
    (floored: 100 means every expected episode has its subtitle on the website).
    The numerator is the number of this project's episodes whose
    ``ProjectEpisode`` carries a subtitle receipt; metadata-only synchronization
    never raises it (a fresh project is 0%), and since receipts are only ever
    set, never cleared, no failed, uncertain or repeated attempt lowers it. It is
    per project (one season/release), not an aggregate over the website title.
    ``confirmed_episodes`` additionally reports episodes whose existence the
    website confirmed (metadata)."""
    rows = list(
        session.execute(
            select(
                ProjectEpisode.web_episode_uuid,
                ProjectEpisode.web_subtitle_sha256,
            ).where(ProjectEpisode.project_id == project.id)
        )
    )
    confirmed = len({r[0] for r in rows if r[0]})
    subtitles = sum(1 for r in rows if r[1])
    expected = project.expected_episode_count
    percent = None if not expected else max(0, min(100, subtitles * 100 // expected))
    return {
        "expected_episode_count": expected,
        "subtitles_published": subtitles,
        "confirmed_episodes": confirmed,
        "percent": percent,
    }


def targets_fingerprint(targets):
    return hashlib.sha256(
        dumps(
            [
                [t["ref"], t["episode_number"], t.get("name_english")]
                for t in sorted(targets, key=lambda t: t["ref"])
            ]
        ).encode()
    ).hexdigest()

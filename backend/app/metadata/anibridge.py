"""AniBridge mappings: suggest TMDB titles/seasons for AniDB and AniList ids.

Dataset: https://github.com/anibridge/anibridge-mappings (public, no API key).
``mappings.min.json`` is one JSON object whose keys are *source descriptors*
``provider:id[:scope]`` and whose values map *target descriptors* to
``{source_range: target_range}`` dicts, e.g.::

    "anidb:1:R": {"tmdb_show:26209:s1": {"1-13": "1-13"}, "anilist:290": {...}}

Scopes: ``sN`` = season N for ``*_show`` providers (``s0`` = specials), AniDB uses
episode-type scopes (``R`` regular, ``S`` specials, ``O`` other, ...), AniList /
MAL / ``*_movie`` have none. Source ranges are ``x[-y]`` (open-ended ``x-``);
target ranges are ``x[-y][,x2[-y2]...][|ratio]``. Mappings are unidirectional.

Suggestions are *advice only*: ``resolve`` reports ``matched`` solely when exactly
one (title, season) candidate exists and otherwise lists every candidate with an
explicit status; it never picks one of several. Everything here is best-effort —
a missing/unavailable dataset yields ``status="unavailable"`` and never an error,
so importing never depends on this module.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

DATASET_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/latest/download/mappings.min.json"
)
SUPPORTED_SCHEMA_MAJOR = "3"
REFRESH_INTERVAL_SECONDS = 24 * 3600     # a *successful* refresh at most once per day
RETRY_AFTER_FAILURE_SECONDS = 15 * 60    # …but a failed attempt backs off, not hammers
MAX_DATASET_BYTES = 128 * 1024 * 1024

# Providers whose entries we index. Only these can be import sources (anidb,
# anilist) or corroborate a link to one (mal).
_SOURCE_PROVIDERS = ("anidb", "anilist", "mal")
# AniDB: regular episodes first; the rest only if nothing regular maps to TMDB.
_ANIDB_SCOPE_ORDER = ("R", "S", "O", "C", "T", "P")

Status = Literal["matched", "ambiguous", "conflict", "none", "unavailable"]


# ─── Range parsing ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EpisodeRange:
    start: int
    end: int | None  # None = open-ended ("13-")

    def count(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1


@dataclass(frozen=True)
class TargetRange:
    segments: tuple[EpisodeRange, ...]
    # >0: each source episode spans n target episodes; <0: spans 1/|n|; None: 1:1.
    ratio: int | None = None


_SEGMENT = re.compile(r"^(\d+)(?:(-)(\d*))?$")


def _parse_segment(text: str) -> EpisodeRange | None:
    m = _SEGMENT.match(text.strip())
    if not m:
        return None
    start = int(m.group(1))
    if m.group(2) is None:          # "x"
        return EpisodeRange(start, start)
    if m.group(3) == "":            # "x-"
        return EpisodeRange(start, None)
    end = int(m.group(3))
    return EpisodeRange(start, end) if end >= start else None


def parse_source_range(text: str) -> EpisodeRange | None:
    """``x[-y]`` — a single contiguous range; ``None`` when malformed."""
    if not isinstance(text, str):
        return None
    return _parse_segment(text)


def parse_target_range(text: str) -> TargetRange | None:
    """``x[-y][,x2[-y2]...][|ratio]``; ``None`` when any part is malformed."""
    if not isinstance(text, str):
        return None
    body, sep, ratio_text = text.partition("|")
    ratio: int | None = None
    if sep:
        try:
            ratio = int(ratio_text)
        except ValueError:
            return None
        if ratio == 0:
            return None
    segments = [_parse_segment(part) for part in body.split(",")]
    if not segments or any(s is None for s in segments):
        return None
    return TargetRange(tuple(s for s in segments if s is not None), ratio)


def count_episodes(ranges: list[EpisodeRange]) -> int | None:
    """Total episodes across closed ranges; ``None`` if any is open-ended/empty."""
    if not ranges:
        return None
    total = 0
    for r in ranges:
        n = r.count()
        if n is None:
            return None
        total += n
    return total


# ─── Descriptors / index ──────────────────────────────────────────────────────

def parse_descriptor(text: str) -> tuple[str, str, str | None] | None:
    parts = text.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1], (parts[2] if len(parts) == 3 else None)


@dataclass(frozen=True)
class _TmdbTarget:
    media_type: Literal["tv", "movie"]
    tmdb_id: str
    season_number: int | None
    pairs: tuple[tuple[str, str], ...]  # raw (source_range, target_range)


@dataclass
class Index:
    """Compact, query-ready view of the dataset (the raw JSON is discarded)."""
    forward: dict[str, tuple[_TmdbTarget, ...]] = field(default_factory=dict)
    # target descriptor (anidb:ID:R / anilist:ID) -> sources that map to it AND to TMDB
    links: dict[str, list[str]] = field(default_factory=dict)
    schema_version: str = ""
    generated_on: str = ""


def _tmdb_target(descriptor: str, mapping: Any) -> _TmdbTarget | None:
    parsed = parse_descriptor(descriptor)
    if parsed is None or not isinstance(mapping, dict):
        return None
    provider, tmdb_id, scope = parsed
    pairs = tuple((str(k), str(v)) for k, v in mapping.items())
    if provider == "tmdb_movie" and scope is None:
        return _TmdbTarget("movie", tmdb_id, None, pairs)
    if provider == "tmdb_show" and scope and re.fullmatch(r"s\d+", scope):
        return _TmdbTarget("tv", tmdb_id, int(scope[1:]), pairs)
    return None


def validate_dataset(data: Any) -> None:
    """Raise ``ValueError`` unless ``data`` looks like a usable v3 dataset."""
    if not isinstance(data, dict):
        raise ValueError("dataset is not a JSON object")
    meta = data.get("$meta")
    version = meta.get("schema_version") if isinstance(meta, dict) else None
    if not isinstance(version, str) or version.split(".")[0] != SUPPORTED_SCHEMA_MAJOR:
        raise ValueError(f"unsupported schema_version {version!r}")
    if not any(k != "$meta" and isinstance(v, dict) for k, v in data.items()):
        raise ValueError("dataset has no mappings")


def build_index(data: dict[str, Any]) -> Index:
    validate_dataset(data)
    meta = data["$meta"]
    index = Index(
        schema_version=str(meta.get("schema_version", "")),
        generated_on=str(meta.get("generated_on", "")),
    )
    for key, targets in data.items():
        if key == "$meta" or not isinstance(targets, dict):
            continue
        parsed = parse_descriptor(key)
        if parsed is None or parsed[0] not in _SOURCE_PROVIDERS:
            continue
        tmdb = tuple(t for d, m in targets.items() if (t := _tmdb_target(d, m)) is not None)
        if not tmdb:
            continue
        index.forward[key] = tmdb
        for target in targets:
            tp = parse_descriptor(target)
            if tp and tp[0] in ("anidb", "anilist"):
                index.links.setdefault(target, []).append(key)
    return index


# ─── Resolution ───────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    media_type: Literal["tv", "movie"]
    tmdb_id: str
    season_number: int | None
    source_scope: str | None = None     # AniDB episode-type scope the entry came from
    via: str | None = None              # linked descriptor when not a direct entry
    episode_map: list[dict[str, str]] = field(default_factory=list)
    # Source episodes the mapping covers; None when open-ended or when the
    # numbering belongs to a linked entry (``via``) rather than the chosen source.
    source_episode_count: int | None = None

    def key(self) -> tuple[str, str, int | None]:
        return (self.media_type, self.tmdb_id, self.season_number)

    def title_key(self) -> tuple[str, str]:
        return (self.media_type, self.tmdb_id)


@dataclass
class Resolution:
    status: Status
    reason: str | None = None
    title: dict[str, str] | None = None   # set only when exactly one TMDB title is implied
    candidates: list[Candidate] = field(default_factory=list)


def _candidates_for(
    targets: tuple[_TmdbTarget, ...], *, scope: str | None, via: str | None,
) -> list[Candidate]:
    merged: dict[tuple[str, str, int | None], Candidate] = {}
    for t in targets:
        cand = merged.setdefault(
            (t.media_type, t.tmdb_id, t.season_number),
            Candidate(t.media_type, t.tmdb_id, t.season_number, source_scope=scope, via=via),
        )
        cand.episode_map.extend({"source": s, "target": d} for s, d in t.pairs)
    for cand in merged.values():
        if cand.via is None:
            ranges = [parse_source_range(m["source"]) for m in cand.episode_map]
            cand.source_episode_count = (
                count_episodes([r for r in ranges if r is not None])
                if ranges and all(r is not None for r in ranges) else None
            )
    return list(merged.values())


def resolve(index: Index, provider: str, provider_id: str) -> Resolution:
    provider_id = str(provider_id).strip()
    if provider == "anidb":
        keys = [(f"anidb:{provider_id}:{s}", s) for s in _ANIDB_SCOPE_ORDER]
    elif provider == "anilist":
        keys = [(f"anilist:{provider_id}", None)]
    else:
        return Resolution("none", "unsupported_provider")
    if not provider_id:
        return Resolution("none", "no_mapping")

    direct: list[Candidate] = []
    for key, scope in keys:           # first scope that maps to TMDB wins (R before S…)
        if key in index.forward:
            direct = _candidates_for(index.forward[key], scope=scope, via=None)
            break

    linked: list[Candidate] = []
    seen_via: set[str] = set()
    for key, _scope in keys:
        for source in index.links.get(key, ()):
            if source in seen_via or source == key:
                continue
            seen_via.add(source)
            linked.extend(_candidates_for(index.forward[source], scope=None, via=source))

    if not direct and not linked:
        return Resolution("none", "no_mapping")

    direct_titles = {c.title_key() for c in direct}
    linked_titles = {c.title_key() for c in linked}
    if direct and linked and not linked_titles <= direct_titles:
        # Independent entries disagree about which TMDB title this is.
        return Resolution("conflict", "direct_vs_linked", None, direct + linked)

    chosen = direct or _dedupe(linked)
    titles = {c.title_key() for c in chosen}
    if len(titles) == 1:
        media, tmdb_id = next(iter(titles))
        title = {"media_type": media, "tmdb_id": tmdb_id}
        if len(chosen) == 1:
            return Resolution("matched", None, title, chosen)
        return Resolution("ambiguous", "multiple_seasons", title, chosen)
    return Resolution("ambiguous", "multiple_titles", None, chosen)


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    out: dict[tuple[str, str, int | None], Candidate] = {}
    for c in candidates:
        if c.key() in out:
            out[c.key()].episode_map.extend(c.episode_map)
        else:
            out[c.key()] = c
    return list(out.values())


# ─── Local cache + refresh ────────────────────────────────────────────────────

@dataclass
class FetchResult:
    status: int                  # 200 or 304
    body: bytes | None = None
    etag: str | None = None


def _download(url: str, etag: str | None) -> FetchResult:
    headers = {"If-None-Match": etag} if etag else {}
    with httpx.Client(
        timeout=httpx.Timeout(30, connect=5), follow_redirects=True,
        headers={"User-Agent": "subi-neko"},
    ) as client:
        response = client.get(url, headers=headers)
        if response.status_code == 304:
            return FetchResult(304)
        response.raise_for_status()
        if len(response.content) > MAX_DATASET_BYTES:
            raise ValueError("dataset too large")
        return FetchResult(200, response.content, response.headers.get("etag"))


@dataclass
class DatasetInfo:
    available: bool
    stale: bool = False
    fetched_at: float | None = None
    generated_on: str | None = None
    schema_version: str | None = None
    error: str | None = None


class AniBridgeStore:
    """Cached dataset with a 24 h refresh and last-good fallback.

    * fresh cache → used as is (no network);
    * stale cache → served immediately, refreshed in the background (never blocks);
    * no cache → one bounded synchronous download; a failure is remembered for
      ``retry_after`` so repeated requests return ``unavailable`` instantly.
    A refresh is only accepted if the payload parses and passes
    :func:`validate_dataset`; otherwise the previous file stays untouched.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetch: Callable[[str, str | None], FetchResult] = _download,
        now: Callable[[], float] = time.time,
        url: str = DATASET_URL,
        ttl: float = REFRESH_INTERVAL_SECONDS,
        retry_after: float = RETRY_AFTER_FAILURE_SECONDS,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.dir = Path(cache_dir)
        self._fetch, self._now, self._url = fetch, now, url
        self._ttl, self._retry_after = ttl, retry_after
        self._spawn = spawn or self._spawn_thread
        self._lock = threading.Lock()
        self._refreshing = threading.Lock()
        self._index: Index | None = None
        self._index_stamp: tuple[float, int] | None = None

    # -- files ---------------------------------------------------------------
    @property
    def _data_path(self) -> Path:
        return self.dir / "mappings.json"

    @property
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._atomic_write(self._state_path, json.dumps(state).encode("utf-8"))

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _load_index(self) -> Index | None:
        """Parse the cached file (memoised by mtime+size); ``None`` if unusable."""
        try:
            stat = self._data_path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime, stat.st_size)
        if self._index is not None and self._index_stamp == stamp:
            return self._index
        try:
            index = build_index(json.loads(self._data_path.read_bytes()))
        except (OSError, ValueError) as exc:
            logger.warning("AniBridge cache is unusable: %s", exc)
            return None
        self._index, self._index_stamp = index, stamp
        return index

    # -- refresh -------------------------------------------------------------
    @staticmethod
    def _spawn_thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="anibridge-refresh", daemon=True).start()

    def refresh(self) -> bool:
        """One refresh attempt (single-flight). True when the cache is now valid
        and current; on any failure the previous good file is left in place."""
        if not self._refreshing.acquire(blocking=False):
            return False
        try:
            state = self._read_state()
            had_cache = self._data_path.exists()
            now = self._now()
            try:
                result = self._fetch(self._url, state.get("etag") if had_cache else None)
                if result.status == 304 and had_cache:
                    state.update(fetched_at=now, last_attempt_at=now, error=None)
                    self._write_state(state)
                    return True
                if result.status != 200 or not result.body:
                    raise ValueError(f"unexpected response {result.status}")
                data = json.loads(result.body)
                index = build_index(data)           # validates
                self._atomic_write(self._data_path, result.body)
                stat = self._data_path.stat()
                with self._lock:
                    self._index, self._index_stamp = index, (stat.st_mtime, stat.st_size)
                state.update(
                    fetched_at=now, last_attempt_at=now, etag=result.etag, error=None,
                    generated_on=index.generated_on, schema_version=index.schema_version,
                )
                self._write_state(state)
                return True
            except Exception as exc:  # noqa: BLE001 - never let the optional source raise
                logger.warning("AniBridge refresh failed: %s", type(exc).__name__)
                state.update(last_attempt_at=now, error=type(exc).__name__)
                try:
                    self._write_state(state)
                except OSError:
                    pass
                return False
        finally:
            self._refreshing.release()

    def get(self) -> tuple[Index | None, DatasetInfo]:
        """The best available index plus its freshness; never raises."""
        state = self._read_state()
        now = self._now()
        with self._lock:
            index = self._load_index()
        fetched_at = state.get("fetched_at") if isinstance(state.get("fetched_at"), (int, float)) else None
        last_attempt = state.get("last_attempt_at") if isinstance(state.get("last_attempt_at"), (int, float)) else None
        cooling = last_attempt is not None and now - last_attempt < self._retry_after and state.get("error")
        fresh = index is not None and fetched_at is not None and now - fetched_at < self._ttl

        if not fresh and not cooling:
            if index is not None:
                self._spawn(self.refresh)            # stale: serve now, refresh behind
            elif self.refresh():                     # nothing cached: one bounded attempt
                state = self._read_state()
                with self._lock:
                    index = self._load_index()
                fetched_at, fresh = state.get("fetched_at"), True
            else:
                state = self._read_state()

        if index is None:
            return None, DatasetInfo(False, error=str(state.get("error") or "unavailable"))
        return index, DatasetInfo(
            True, stale=not fresh, fetched_at=fetched_at,
            generated_on=index.generated_on or None,
            schema_version=index.schema_version or None,
            error=str(state["error"]) if state.get("error") else None,
        )


_store: AniBridgeStore | None = None
_store_lock = threading.Lock()


def get_store() -> AniBridgeStore:
    """Process-wide store rooted at ``<config>/cache/anibridge``."""
    global _store
    with _store_lock:
        if _store is None:
            from app.core.config import settings
            _store = AniBridgeStore(settings.cache_dir / "anibridge")
        return _store


def suggest(provider: str, provider_id: str, store: AniBridgeStore | None = None) -> dict[str, Any]:
    """API-shaped suggestion for one source identity; never raises."""
    try:
        index, info = (store or get_store()).get()
        if index is None:
            return {"status": "unavailable", "reason": "dataset_unavailable", "title": None,
                    "candidates": [], "dataset": _dataset_json(info)}
        res = resolve(index, provider, provider_id)
        return {
            "status": res.status, "reason": res.reason, "title": res.title,
            "candidates": [_candidate_json(c) for c in res.candidates],
            "dataset": _dataset_json(info),
        }
    except Exception:  # noqa: BLE001 - suggestions are optional
        logger.warning("AniBridge suggestion failed", exc_info=True)
        return {"status": "unavailable", "reason": "dataset_unavailable", "title": None,
                "candidates": [], "dataset": {"available": False}}


def _candidate_json(c: Candidate) -> dict[str, Any]:
    return {
        "media_type": c.media_type, "tmdb_id": c.tmdb_id, "season_number": c.season_number,
        "source_scope": c.source_scope, "via": c.via, "episode_map": c.episode_map,
        "source_episode_count": c.source_episode_count,
    }


def _dataset_json(info: DatasetInfo) -> dict[str, Any]:
    return {
        "available": info.available, "stale": info.stale, "fetched_at": info.fetched_at,
        "generated_on": info.generated_on, "schema_version": info.schema_version,
        "error": info.error,
    }

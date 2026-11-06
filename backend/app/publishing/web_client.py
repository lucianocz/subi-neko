"""Contract-only client. No redirects, credential-bearing exceptions or raw errors."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable

import httpx

from app.core.config import settings


class _HidePublisherConfig(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for value in (
            settings.subineko_web_token,
            settings.subineko_web_url.strip().rstrip("/"),
        ):
            if value:
                message = message.replace(value, "[WEB private]")
        record.msg, record.args = message, ()
        return True


# httpx logs request URLs at INFO even though callers sanitize exceptions.
logging.getLogger("httpx").addFilter(_HidePublisherConfig())


class PublishingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require_available():
    if not settings.web_publishing_available:
        raise PublishingError(
            "web_unavailable", "WEB publishing is not configured.", 503
        )


def destination() -> str:
    require_available()
    return hashlib.sha256(
        settings.subineko_web_url.strip().rstrip("/").lower().encode()
    ).hexdigest()


class WebClient:
    def __init__(self, check: Callable[[], None] | None = None):
        require_available()
        self.check = check or (lambda: None)
        self.client = httpx.Client(
            base_url=settings.subineko_web_url.strip().rstrip("/"),
            headers={"Authorization": f"Bearer {settings.subineko_web_token}"},
            timeout=httpx.Timeout(60, connect=10, write=60, pool=10),
            follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, *, missing_ok=False, **kwargs):
        for attempt in range(3):
            self.check()
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 2:
                    raise PublishingError(
                        "web_uncertain",
                        "Website request timed out or could not connect; retry publication to reconcile.",
                        502,
                    ) from None
            else:
                if response.status_code == 404 and missing_ok:
                    return None
                if response.status_code in (200, 201):
                    try:
                        return response.json()
                    except ValueError:
                        raise PublishingError(
                            "web_response", "Website returned an invalid response.", 502
                        ) from None
                if response.status_code not in (429, 500, 502, 503, 504):
                    code = {
                        401: "web_auth",
                        404: "web_recovery_required",
                        409: "web_identity_conflict",
                        413: "web_too_large",
                        415: "web_invalid_ass",
                        422: "web_validation",
                    }.get(response.status_code, "web_response")
                    raise PublishingError(
                        code,
                        f"Website rejected the request (HTTP {response.status_code}).",
                        502,
                    )
                if attempt == 2:
                    raise PublishingError(
                        "web_uncertain",
                        "Website temporarily unavailable; the request may have committed. Retry to reconcile.",
                        502,
                    )
            time.sleep(0.5 * 2**attempt)

    # The wire contract is the current website's entity-oriented API. The
    # service keeps its existing normalized snapshots/UUID receipts; no second
    # identity store is introduced by this boundary adapter.
    def _initialize(self, config):
        self.config = config
        self.title_external = f"tmdb-{config.media_type}-{config.tmdb_title_id}"
        self.raw_title = None

    @staticmethod
    def _segment(value):
        from urllib.parse import quote

        return quote(str(value), safe="")

    def _title_path(self):
        return f"/api/v1/titles/{self._segment(self.title_external)}"

    def _remember(self, raw, expected_external=None):
        if not isinstance(raw, dict) or not all(
            key in raw
            for key in (
                "id",
                "externalId",
                "mediaType",
                "seasons",
                "episodes",
                "translationProgress",
            )
        ):
            raise PublishingError(
                "web_response", "Website returned an invalid title state.", 502
            )
        if raw["externalId"] != (expected_external or self.title_external) or (
            self.raw_title and self.raw_title["id"] != raw["id"]
        ):
            raise PublishingError(
                "web_recovery_required", "Previously confirmed website title changed."
            )
        self.raw_title = raw
        self.title_external = raw["externalId"]
        return self._normalize(raw)

    def _refresh_title(self, *, missing_ok=False):
        raw = self.request("GET", self._title_path(), missing_ok=missing_ok)
        if raw is None:
            self.raw_title = None  # a confirmed 404 permits recreation at this external ID
            return None
        self._remember(raw)
        return raw

    @staticmethod
    def _find_alias(rows, preferred, legacy):
        # Canonical IDs win even when a legacy alias appears earlier in a full
        # snapshot (e.g. direct episodes are listed before seasoned episodes).
        return next((r for r in rows if r["externalId"] == preferred), None) or next(
            (r for r in rows if r["externalId"] == legacy), None
        )

    def _normalize(self, raw):
        config = self.config
        episode_prefix = "tmdb-episode-"
        canonical_episodes = {
            e["externalId"].removeprefix(episode_prefix)
            for e in raw["episodes"]
            + [e for s in raw["seasons"] for e in s["episodes"]]
            if e["externalId"].startswith(episode_prefix)
        }

        def alias(external, kind=None):
            return {
                "provider": "tmdb",
                "media_type": config.media_type,
                "external_id": external,
                **({"kind": kind} if kind else {}),
            }

        def episode(value):
            external = value["externalId"]
            prefix = episode_prefix
            mapped = external.startswith(prefix) or (
                external.isdecimal() and external not in canonical_episodes
            )
            subtitle = value.get("currentSubtitle")
            return {
                "id": value["id"],
                "external_id": external,
                "episode_number": value["episodeNumber"],
                "name_english": value.get("nameEnglish"),
                "mappings": [alias(external.removeprefix(prefix))] if mapped else [],
                "publisher_key": None if mapped else external,
                "current_subtitle": {
                    **subtitle,
                    "revision_number": subtitle.get("revisionNumber"),
                }
                if subtitle
                else None,
            }

        seasons = []
        for value in raw["seasons"]:
            external = value["externalId"]
            prefix = (
                f"tmdb-group-{config.group_id}-"
                if config.order_kind == "episode_group"
                else "tmdb-season-"
            )
            external_id = external.removeprefix(prefix)
            kind = (
                "episode_group_subgroup"
                if config.order_kind == "episode_group"
                else "season"
            )
            seasons.append(
                {
                    "id": value["id"],
                    "external_id": external,
                    "season_number": value["seasonNumber"],
                    "name_english": value.get("nameEnglish"),
                    "year": value.get("year"),
                    "mappings": [alias(external_id, kind)]
                    if external.startswith(prefix)
                    or not any(
                        s["externalId"] == prefix + external for s in raw["seasons"]
                    )
                    else [],
                    "episodes": [episode(e) for e in value["episodes"]],
                }
            )
        return {
            "id": raw["id"],
            "external_id": raw["externalId"],
            "current_api": True,
            "media_type": raw["mediaType"].lower(),
            # Structure/order are local publisher configuration, absent from the
            # current website contract. Local durable locks still enforce them.
            "episode_structure": config.episode_structure,
            "episode_order": {
                "provider": "tmdb",
                "kind": config.order_kind,
                "group_id": config.group_id,
            },
            "mappings": [alias(config.tmdb_title_id)],
            "title_english": raw.get("titleEnglish"),
            "title_romaji": raw.get("titleRomaji"),
            "year": raw.get("year"),
            "original_language": raw.get("originalLanguage"),
            "description_english": raw.get("descriptionEnglish"),
            "translation_progress": raw["translationProgress"],
            "poster": raw.get("currentPoster"),
            "seasons": seasons,
            "episodes": [episode(e) for e in raw["episodes"]],
        }

    def lookup(self, config):
        self._initialize(config)
        expected_external = self.title_external
        raw = self.request("GET", self._title_path(), missing_ok=True)
        if raw is None:
            # Adopt a pre-existing receiver title whose external ID was the bare
            # TMDB id. Do not create a second resource for that known identity.
            raw = self.request(
                "GET",
                f"/api/v1/titles/{self._segment(config.tmdb_title_id)}",
                missing_ok=True,
            )
            expected_external = config.tmdb_title_id
            # Bare TMDB numbers overlap between TV and movie namespaces. A
            # different media type is a different title, not our legacy alias.
            if (
                isinstance(raw, dict)
                and raw.get("mediaType", "").lower() != config.media_type
            ):
                raw = None
        if raw is not None:
            if not isinstance(raw, dict):
                raise PublishingError(
                    "web_response", "Website returned an invalid title state.", 502
                )
            if raw.get("mediaType", "").lower() != config.media_type:
                raise PublishingError(
                    "web_identity_conflict", "Website title has a different media type."
                )
            return self._remember(raw, expected_external)
        return None

    def get_title(self, title_id):
        # UUIDs are receipts, never URL identities in the current contract.
        if not hasattr(self, "config"):
            raise PublishingError(
                "web_mapping", "Resolve the configured title before reading its state."
            )
        raw = self.request("GET", self._title_path())
        if not isinstance(raw, dict):
            raise PublishingError(
                "web_response", "Website returned an invalid title state.", 502
            )
        if raw.get("id") != title_id:
            raise PublishingError(
                "web_recovery_required", "Previously confirmed website title changed."
            )
        return self._remember(raw)

    @staticmethod
    def _title_metadata(raw):
        return {
            k: raw.get(k)
            for k in (
                "mediaType",
                "translationProgress",
                "titleEnglish",
                "titleRomaji",
                "year",
                "originalLanguage",
                "descriptionEnglish",
            )
        }

    def _upsert(self, path, body, before):
        # The API deliberately has no outcome field. Compare current metadata
        # locally so created/updated/noop retain their existing UI semantics.
        if before is not None and all(before.get(k) == v for k, v in body.items()):
            return "noop"
        response = self.request("PUT", path, json=body)
        if (
            not isinstance(response, dict)
            or not response.get("id")
            or not response.get("externalId")
        ):
            raise PublishingError(
                "web_response", "Website returned an invalid entity receipt.", 502
            )
        from urllib.parse import unquote

        if response["externalId"] != unquote(path.rsplit("/", 1)[-1]) or (
            before and response["id"] != before["id"]
        ):
            raise PublishingError(
                "web_recovery_required", "Previously confirmed website entity changed."
            )
        return "created" if before is None else "updated"

    @staticmethod
    def _batch_outcome(outcomes):
        return (
            "created"
            if "created" in outcomes
            else "updated"
            if "updated" in outcomes
            else "noop"
        )

    def metadata(self, path, body):
        if not hasattr(self, "config"):
            raise PublishingError(
                "web_mapping", "Resolve the configured title before publishing."
            )
        # Preserve the latest remote progress, and verify recorded title identity
        # before any write; an old lookup is not a current metadata snapshot.
        current = self._refresh_title(missing_ok=True)
        if path.endswith(("/seasons", "/episodes")):
            outcomes = []
            if current is None:
                raise PublishingError(
                    "web_recovery_required", "Website title is missing."
                )
            before = current
            season_external = None
            if path.endswith("/seasons"):
                external = body["mappings"][0]["external_id"]
                prefix = (
                    f"tmdb-group-{self.config.group_id}-"
                    if self.config.order_kind == "episode_group"
                    else "tmdb-season-"
                )
                existing = self._find_alias(
                    before.get("seasons", []), prefix + external, external
                )
                if body.get("id") and existing and existing["id"] != body["id"]:
                    raise PublishingError(
                        "web_recovery_required",
                        "Previously confirmed website season changed.",
                    )
                season_external = (
                    existing["externalId"] if existing else prefix + external
                )
                season_body = {
                    "seasonNumber": body["season_number"],
                    "nameEnglish": body.get("name_english"),
                    "year": body.get("year"),
                }
                outcomes.append(
                    self._upsert(
                        f"{self._title_path()}/seasons/{self._segment(season_external)}",
                        season_body,
                        existing,
                    )
                )
            all_episodes = before.get("episodes", []) + [
                e for s in before.get("seasons", []) for e in s.get("episodes", [])
            ]
            for item in body.get("episodes", []):
                mappings = item.get("mappings") or []
                external = (
                    f"tmdb-episode-{mappings[0]['external_id']}"
                    if mappings
                    else item.get("publisher_key")
                )
                if not external:
                    raise PublishingError(
                        "web_mapping", "An episode requires a stable external identity."
                    )
                existing = self._find_alias(
                    all_episodes,
                    external,
                    mappings[0]["external_id"] if mappings else external,
                )
                if existing:
                    external = existing["externalId"]
                if item.get("id") and existing and existing["id"] != item["id"]:
                    raise PublishingError(
                        "web_recovery_required",
                        "Previously confirmed website episode changed.",
                    )
                # Never silently move an identity between seasons/projects.
                if existing and existing.get("seasonExternalId") != season_external:
                    raise PublishingError(
                        "web_identity_conflict",
                        "Website episode belongs to a different season.",
                    )
                episode_body = {
                    "seasonExternalId": season_external,
                    "episodeNumber": item["episode_number"],
                    "nameEnglish": item.get("name_english"),
                }
                outcomes.append(
                    self._upsert(
                        f"{self._title_path()}/episodes/{self._segment(external)}",
                        episode_body,
                        existing,
                    )
                )
            outcome = self._batch_outcome(outcomes)
        else:
            previous = current
            title_body = {
                "mediaType": body["media_type"].upper(),
                "translationProgress": previous["translationProgress"]
                if previous
                else 0,
                "titleEnglish": body["title_english"],
                "titleRomaji": body.get("title_romaji"),
                "year": body.get("year"),
                "originalLanguage": body.get("original_language"),
                "descriptionEnglish": body.get("description_english"),
            }
            outcome = self._upsert(self._title_path(), title_body, previous)
        self._refresh_title()
        return {"outcome": outcome, "title": self._normalize(self.raw_title)}

    def update_progress(self, config, percent):
        """Send only the project's calculated percentage, no episode counts/status.

        The website requires a full title PUT. Preserve its latest descriptive
        fields rather than overwriting intervening edits during progress sync.
        """
        if not hasattr(self, "config"):
            self.lookup(config)
        raw = self._refresh_title()
        body = self._title_metadata(raw)
        body["translationProgress"] = max(0, min(100, int(percent)))
        outcome = self._upsert(self._title_path(), body, raw)
        self._refresh_title()
        return {"outcome": outcome, "percent": body["translationProgress"]}

    def upload(self, path, filename, content, fields=None):
        if not getattr(self, "raw_title", None):
            raise PublishingError(
                "web_mapping", "Resolve the configured title before uploading."
            )
        sha = hashlib.sha256(content).hexdigest()
        fields = fields or {}
        if path.endswith("/poster"):
            actual_path = f"{self._title_path()}/poster"
            previous = self.request("GET", actual_path, missing_ok=True)
            if previous and previous.get("sha256") == sha:
                return {"outcome": "noop", "poster": previous}
            suffix = filename.rsplit(".", 1)[-1].lower()
            content_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
            }.get(suffix, "application/octet-stream")
            result = self.request(
                "PUT", actual_path, files={"file": (filename, content, content_type)}
            )
            receipt = result.get("poster", {})
            if receipt.get("sha256") != sha:
                raise PublishingError(
                    "web_response", "Website returned an invalid poster receipt.", 502
                )
            return {"outcome": "updated" if previous else "created", "poster": receipt}
        episode_uuid = path.split("/")[-2]
        raw = self.request("GET", self._title_path())
        self._remember(raw)
        episodes = raw["episodes"] + [e for s in raw["seasons"] for e in s["episodes"]]
        episode = next((e for e in episodes if e["id"] == episode_uuid), None)
        if not episode:
            raise PublishingError(
                "web_recovery_required",
                "Previously confirmed website episode is missing.",
            )
        actual_path = f"{self._title_path()}/episodes/{self._segment(episode['externalId'])}/subtitles"
        previous = episode.get("currentSubtitle")
        if previous and previous.get("sha256") == sha:
            return {"outcome": "noop", "revision": previous}
        data = {
            "revisionNumber": str((previous or {}).get("revisionNumber", 0) + 1),
            "releaseLabel": fields.get("release_label", ""),
        }
        if "changelog" in fields:
            data["changelog"] = fields["changelog"]
        result = self.request(
            "PUT",
            actual_path,
            files={"file": (filename, content, "application/octet-stream")},
            data=data,
        )
        receipt = result.get("subtitle", {})
        if receipt.get("sha256") != sha:
            raise PublishingError(
                "web_response", "Website returned an invalid subtitle receipt.", 502
            )
        return {"outcome": "updated" if previous else "created", "revision": receipt}

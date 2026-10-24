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

    def lookup(self, config):
        return self.request(
            "GET",
            "/api/v1/titles",
            missing_ok=True,
            params={
                "provider": "tmdb",
                "media_type": config.media_type,
                "external_id": config.tmdb_title_id,
            },
        )

    def get_title(self, title_id):
        return self.request("GET", f"/api/v1/titles/{title_id}")

    def metadata(self, path, body):
        result = self.request("PUT", path, json=body)
        if (
            not isinstance(result, dict)
            or "title" not in result
            or result.get("outcome") not in ("created", "updated", "noop")
        ):
            raise PublishingError(
                "web_response", "Website returned an invalid metadata response.", 502
            )
        return result

    def upload(self, path, filename, content, fields=None):
        # bytes and fields are reused unchanged across all retries.
        return self.request(
            "PUT",
            path,
            files={"file": (filename, content, "application/octet-stream")},
            data=fields or {},
        )

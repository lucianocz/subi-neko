"""One router-level guard that keeps archived projects read-only.

Attached as a dependency to every router whose paths start with
``/projects/{project_id}``. Safe methods pass (listing, download, status
reads); any other method is rejected with 409 ``project_archived`` unless it is
Archive/Unarchive or the explicit project DELETE. Unknown project ids pass
through so the endpoint produces its own 404.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.database import AsyncSessionLocal
from app.db.archive_state import is_project_archived

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_EXEMPT_SUFFIXES = ("/archive", "/unarchive")


def archived_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "project_archived",
            "message": "This project is archived. Unarchive it to make changes.",
        },
    )


async def reject_if_archived(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    project_id = request.path_params.get("project_id")
    if project_id is None:
        return
    path = request.url.path.rstrip("/")
    if path.endswith(_EXEMPT_SUFFIXES):
        return
    if request.method == "DELETE" and path.endswith(f"/projects/{project_id}"):
        return
    async with AsyncSessionLocal() as session:
        if await is_project_archived(session, int(project_id)):
            raise archived_error()

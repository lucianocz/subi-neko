"""Direct source-media streaming and configured-font serving (Final QC infra).

``GET|HEAD /projects/{pid}/files/{fid}/media`` serves the ORIGINAL source MKV —
no transcoding, no remuxing, no ffmpeg/mkvmerge. Range handling (206, suffix
and open-ended ranges, 416, ``Accept-Ranges``/``Content-Range``) is Starlette's
``FileResponse``; the file is streamed in chunks, never buffered. Whether the
browser can decode the codecs inside is a client requirement.

Access model = the rest of the app (no auth exists). What is enforced here:
the file must belong to the project, and the path comes only from the DB row,
resolved through ``resolve_source_path`` (traversal/symlink-safe).

Reverse proxy: forward ``Range``/``If-Range`` untouched, do not gzip media
(``video/x-matroska`` is incompressible), and disable response buffering for
this location (e.g. nginx ``proxy_buffering off``) or seeking stalls until the
proxy has pulled the body. X-Accel-Redirect/X-Sendfile are deliberately not
used: the app serves straight from disk.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.database import AsyncSessionLocal
from app.core.media_paths import SourceFileMissingError, SourcePathError, resolve_source_path
from app.db.models import File, Project
from app.subs.font_registry import FontFace, get_configured_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

MEDIA_TYPE = "video/x-matroska"
# Font bytes are addressed by content hash, so they never change under an id.
FONT_CACHE_CONTROL = "public, max-age=31536000, immutable"


def resolve_or_http(project: Project, file: File) -> Path:
    """``resolve_source_path`` mapped to HTTP: unsafe and missing both read as
    404 (no hint about the filesystem layout)."""
    try:
        return resolve_source_path(project, file)
    except SourceFileMissingError:
        raise HTTPException(status_code=404, detail="Source media file not found")
    except SourcePathError:
        logger.warning("Rejected unsafe source path for file id=%s", file.id)
        raise HTTPException(status_code=404, detail="Source media file not found")


async def load_project_file(project_id: int, file_id: int) -> tuple[Project, File]:
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project, file


def font_response(face: FontFace) -> FileResponse:
    """Font bytes: correct MIME, immutable cache. Same-origin, so no CORS headers
    are needed (a JASSUB worker fetch from the same origin works as-is)."""
    return FileResponse(
        face.path, media_type=face.media_type,
        headers={"Cache-Control": FONT_CACHE_CONTROL},
    )


@router.api_route(
    "/projects/{project_id}/files/{file_id}/media",
    methods=["GET", "HEAD"],
    response_model=None,
)
async def stream_source_media(project_id: int, file_id: int) -> FileResponse:
    project, file = await load_project_file(project_id, file_id)
    path = resolve_or_http(project, file)
    return FileResponse(path, media_type=MEDIA_TYPE)


@router.api_route("/fonts/configured/{font_id}", methods=["GET", "HEAD"], response_model=None)
async def get_configured_font(font_id: str) -> FileResponse:
    face = get_configured_registry().get(font_id)
    if face is None or not face.path.is_file():
        raise HTTPException(status_code=404, detail="Font not found")
    return font_response(face)

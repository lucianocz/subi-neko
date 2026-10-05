"""Final-QC availability (derived, never stored).

A file is open for Final QC iff it has no incomplete chunk::

    qc_available = NOT EXISTS (chunk of the file WHERE status != 'complete')

* A file with zero chunks is therefore available (nothing is in flight).
* It deliberately does NOT depend on ``FileStatus`` — QC edits never change
  acceptance, and an unaccepted file whose chunks are all complete is editable.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubtitleChunk

CHUNK_COMPLETE = "complete"


async def incomplete_chunk_file_ids(session: AsyncSession, file_ids: list[int]) -> set[int]:
    """Subset of ``file_ids`` that still has at least one non-complete chunk."""
    if not file_ids:
        return set()
    rows = await session.scalars(
        select(SubtitleChunk.file_id)
        .where(SubtitleChunk.file_id.in_(file_ids), SubtitleChunk.status != CHUNK_COMPLETE)
        .distinct()
    )
    return set(rows.all())


async def is_qc_available(session: AsyncSession, file_id: int) -> bool:
    return file_id not in await incomplete_chunk_file_ids(session, [file_id])


async def qc_available_map(session: AsyncSession, file_ids: list[int]) -> dict[int, bool]:
    blocked = await incomplete_chunk_file_ids(session, file_ids)
    return {fid: fid not in blocked for fid in file_ids}

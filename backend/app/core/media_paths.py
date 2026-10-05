"""One safe resolver for a project file's source media path.

``IMPORT_ROOT / project.source_directory / file.relative_path`` is resolved
(symlinks and ``..`` collapsed) and must stay inside the project's own
directory, which in turn must stay inside ``IMPORT_ROOT``. Nothing outside the
import tree can be reached through DB-stored strings.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SourcePathError(ValueError):
    """The stored path is unsafe or does not point at a regular file.

    Subclasses ``ValueError`` so job handlers that already catch it for the
    "escapes import root" case keep working unchanged.
    """


class SourceFileMissingError(SourcePathError):
    """The path is safe but there is no regular file at it."""


class _HasSourceDirectory(Protocol):
    source_directory: str


class _HasRelativePath(Protocol):
    relative_path: str


def resolve_source_path_parts(
    source_directory: str,
    relative_path: str,
    *,
    import_root: Path | None = None,
    must_exist: bool = True,
) -> Path:
    """Resolve and validate ``source_directory`` / ``relative_path``.

    ``must_exist=False`` keeps only the containment checks (job handlers that
    let ``mkvmerge``/``mkvextract`` report a missing file themselves).
    """
    if import_root is None:
        from app.core.config import settings
        import_root = settings.import_root
    root = Path(import_root).resolve()

    project_dir = (root / source_directory).resolve()
    if not project_dir.is_relative_to(root):
        raise SourcePathError(f"source_directory '{source_directory}' escapes import root")

    candidate = (project_dir / relative_path).resolve()
    if not candidate.is_relative_to(project_dir):
        raise SourcePathError(f"Resolved path {candidate} escapes the project directory")

    if must_exist and not candidate.is_file():
        raise SourceFileMissingError(f"Source file not found: {relative_path}")
    return candidate


def resolve_source_path(
    project: _HasSourceDirectory,
    file: _HasRelativePath,
    *,
    import_root: Path | None = None,
    must_exist: bool = True,
) -> Path:
    """``resolve_source_path(project, file)`` — see module docstring."""
    return resolve_source_path_parts(
        project.source_directory, file.relative_path,
        import_root=import_root, must_exist=must_exist,
    )

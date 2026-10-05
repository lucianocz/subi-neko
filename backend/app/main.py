from contextlib import asynccontextmanager
import os
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.database import verify_connection, close_db, run_migrations
from app.core.logging_config import apply_log_level, configure_logging
from app.db import options as options_store
from app.jobs.manager import job_manager
from app.scheduler.manager import scheduler_manager
from app.ws.connection_manager import connection_manager
from app.orchestrator.orchestrator import (
    orchestrate_on_job_complete,
    start_sweep_loop,
    stop_sweep_loop,
)

# Import handlers so they self-register via @register_job_handler
import app.jobs.handlers.inspect_mkv       # noqa: F401
import app.jobs.handlers.extract_subtitles # noqa: F401
import app.jobs.handlers.aggregate_speakers  # noqa: F401
import app.jobs.handlers.publish_project     # noqa: F401
import app.jobs.handlers.scan_project        # noqa: F401
import app.jobs.handlers.plan_translation_chunks  # noqa: F401
import app.jobs.handlers.translate_chunk          # noqa: F401
import app.jobs.handlers.validate_chunk           # noqa: F401
import app.jobs.handlers.resolve_style_fonts      # noqa: F401
import app.jobs.handlers.repair_chunk             # noqa: F401
import app.jobs.handlers.polish_chunk             # noqa: F401
import app.jobs.handlers.review_chunk_final       # noqa: F401
import app.jobs.handlers.audit_chunk_final        # noqa: F401
import app.jobs.handlers.analyze_script           # noqa: F401
import app.jobs.handlers.style_bible              # noqa: F401
import app.jobs.handlers.infer_character_mapping  # noqa: F401
import app.jobs.handlers.compute_file_metrics     # noqa: F401

APP_NAME = "subi-neko"


def _configure_logging(level: str) -> None:
    configure_logging(level, settings.config_root)


async def _on_option_change(name: str, value: str | None) -> None:
    if name == "JOB_WORKER_COUNT" and value is not None:
        from app.db.options import _validated_worker_count
        await job_manager.resize(_validated_worker_count(value))
    elif name == "LOG_LEVEL":
        from app.db.options import _validated_log_level
        apply_log_level(_validated_log_level(value))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings.ensure_directories()
    await run_migrations()
    try:
        from app.subs.font_registry import refresh_configured_registry
        refresh_configured_registry()
    except Exception:
        logging.getLogger(__name__).exception("Configured font scan failed; fonts will be rescanned on demand")
    await verify_connection()

    opts = await options_store.asnapshot()
    _configure_logging(opts.log_level)
    options_store.register_change_listener(_on_option_change)
    logging.getLogger(__name__).info("Lifespan startup: logging level=%s", opts.log_level)

    # Wire broadcast: job events → WebSocket
    async def _job_broadcast(event: str, data: dict) -> None:
        await connection_manager.broadcast(event, data)

    async def _scheduler_broadcast(event: str, data: dict) -> None:
        await connection_manager.broadcast(event, data)

    job_manager.set_broadcast(_job_broadcast)
    job_manager.set_on_complete(
        lambda job_id: orchestrate_on_job_complete(job_id, job_manager.enqueue)
    )
    scheduler_manager.set_enqueue(job_manager.enqueue)
    scheduler_manager.set_broadcast(_scheduler_broadcast)

    await job_manager.start(opts.job_worker_count)
    await scheduler_manager.start()
    await start_sweep_loop(job_manager.enqueue, interval_seconds=10.0)

    yield

    # Shutdown
    await stop_sweep_loop()
    await scheduler_manager.stop()
    await job_manager.stop()
    await close_db()


def resolve_static_file(static_dir: str, rel_path: str) -> str | None:
    """Return the real path of ``rel_path`` inside ``static_dir``, or None.

    None when the file doesn't exist or resolves (``..``, symlinks, absolute
    paths, drive letters) outside the static root.
    """
    root = os.path.realpath(static_dir)
    if "\x00" in rel_path:
        return None
    candidate = os.path.realpath(os.path.join(root, rel_path))
    if os.path.commonpath([root, candidate]) != root:
        return None
    return candidate if os.path.isfile(candidate) else None


def mount_spa(app: FastAPI, static_dir: str) -> None:
    """Serve the built frontend: /assets plus an index.html SPA fallback."""
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        try:
            target = resolve_static_file(static_dir, full_path)
        except ValueError:  # e.g. different drives on Windows
            target = None
        return FileResponse(target or os.path.join(static_dir, "index.html"))


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # API routes
    from app.api.routes import health, ws, projects, qc, media, options, metadata, import_, jobs
    app.include_router(health.router, prefix="/api")
    app.include_router(ws.router)
    app.include_router(projects.router, prefix="/api")
    app.include_router(qc.router, prefix="/api")
    app.include_router(media.router, prefix="/api")
    app.include_router(options.router, prefix="/api")
    app.include_router(metadata.router, prefix="/api")
    app.include_router(import_.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")

    # Serve frontend static files (populated by Docker build)
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(static_dir):
        mount_spa(app, static_dir)

    return app


app = create_app()

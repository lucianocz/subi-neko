"""Application logging configuration shared by startup and live options."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_FORMAT = "[%(asctime)s][%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARKER = "_subi_neko_handler"
_LOGGER_PREFIXES = ("app", "uvicorn")


def _is_application_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _LOGGER_PREFIXES)


def apply_log_level(level: str) -> None:
    """Apply a validated level to active application logging immediately.

    This process-local update intentionally touches the root handlers and any
    handlers owned by active ``app``/``uvicorn`` loggers.  DB persistence is
    handled by the options store, so separately started worker processes pick
    up the saved value during their own initialization.
    """
    numeric_level = logging._nameToLevel.get(level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    relevant_loggers = [root]
    for name, candidate in logging.root.manager.loggerDict.items():
        if isinstance(candidate, logging.Logger) and _is_application_logger(name):
            candidate.setLevel(numeric_level)
            relevant_loggers.append(candidate)

    seen_handlers: set[int] = set()
    for logger in relevant_loggers:
        for handler in logger.handlers:
            if id(handler) in seen_handlers:
                continue
            handler.setLevel(numeric_level)
            seen_handlers.add(id(handler))


def configure_logging(level: str, config_root: Path) -> None:
    """Configure one console and one daily rotating application log handler."""
    log_dir = config_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "subi-neko.log").resolve()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    root = logging.getLogger()

    console_handler = next(
        (handler for handler in root.handlers
         if getattr(handler, _HANDLER_MARKER, None) == "console"),
        None,
    )
    if console_handler is None:
        console_handler = logging.StreamHandler(sys.stderr)
        setattr(console_handler, _HANDLER_MARKER, "console")
        root.addHandler(console_handler)
    console_handler.setFormatter(formatter)

    file_handler = next(
        (handler for handler in root.handlers
         if getattr(handler, _HANDLER_MARKER, None) == "file"
         and Path(getattr(handler, "baseFilename", "")).resolve() == log_path),
        None,
    )
    if file_handler is None:
        # A repeated initialization against a different config root (primarily
        # tests) replaces only our old file handler and preserves all others.
        for handler in list(root.handlers):
            if getattr(handler, _HANDLER_MARKER, None) == "file":
                root.removeHandler(handler)
                handler.close()
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=False,
        )
        setattr(file_handler, _HANDLER_MARKER, "file")
        root.addHandler(file_handler)
    file_handler.setFormatter(formatter)

    apply_log_level(level)

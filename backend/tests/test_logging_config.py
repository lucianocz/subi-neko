from __future__ import annotations

import io
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from app.core.logging_config import apply_log_level, configure_logging
from app.db.options import AppOptions
from app.llm import client as llm_client


@pytest.fixture
def isolated_root_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_handler_levels = {id(handler): handler.level for handler in original_handlers}
    original_logger_levels = {
        name: candidate.level
        for name, candidate in logging.root.manager.loggerDict.items()
        if isinstance(candidate, logging.Logger)
    }
    yield root
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    for handler in original_handlers:
        handler.setLevel(original_handler_levels[id(handler)])
    for name, candidate in logging.root.manager.loggerDict.items():
        if isinstance(candidate, logging.Logger):
            candidate.setLevel(original_logger_levels.get(name, logging.NOTSET))


def _owned_handlers(root: logging.Logger):
    return [handler for handler in root.handlers
            if getattr(handler, "_subi_neko_handler", None)]


def test_configure_logging_creates_daily_rotating_utf8_file_without_duplicates(
    tmp_path, monkeypatch, isolated_root_logging,
):
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    config_root = tmp_path / "missing-config"

    configure_logging("INFO", config_root)
    configure_logging("INFO", config_root)

    handlers = _owned_handlers(isolated_root_logging)
    console_handlers = [h for h in handlers
                        if getattr(h, "_subi_neko_handler") == "console"]
    file_handlers = [h for h in handlers
                     if getattr(h, "_subi_neko_handler") == "file"]
    assert len(console_handlers) == 1
    assert len(file_handlers) == 1

    file_handler = file_handlers[0]
    assert isinstance(file_handler, TimedRotatingFileHandler)
    assert (config_root / "logs" / "subi-neko.log").exists()
    assert file_handler.when == "MIDNIGHT"
    assert file_handler.interval == 24 * 60 * 60
    assert file_handler.backupCount == 30
    assert file_handler.utc is False
    assert file_handler.encoding.lower().replace("-", "") == "utf8"
    assert file_handler.suffix == "%Y-%m-%d"

    logging.getLogger("app.logging_test").info("Application started")
    for handler in handlers:
        handler.flush()
    line = (config_root / "logs" / "subi-neko.log").read_text(encoding="utf-8").strip()
    assert line.endswith("[INFO] Application started")
    assert line.startswith("[")
    assert "][INFO] " in line


def test_runtime_level_change_immediately_updates_console_and_file(
    tmp_path, monkeypatch, isolated_root_logging,
):
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    configure_logging("INFO", tmp_path)
    logger = logging.getLogger("app.runtime_level_test")
    log_path = tmp_path / "logs" / "subi-neko.log"

    logger.debug("hidden before change")
    assert "hidden before change" not in stderr.getvalue()
    assert "hidden before change" not in log_path.read_text(encoding="utf-8")

    apply_log_level("DEBUG")
    logger.debug("visible after change")
    for handler in _owned_handlers(isolated_root_logging):
        handler.flush()

    assert "[DEBUG] visible after change" in stderr.getvalue()
    assert "[DEBUG] visible after change" in log_path.read_text(encoding="utf-8")
    assert isolated_root_logging.level == logging.DEBUG
    assert all(handler.level == logging.DEBUG
               for handler in _owned_handlers(isolated_root_logging))


def test_runtime_level_change_immediately_enables_raw_llm_response_logging(
    tmp_path, monkeypatch, isolated_root_logging,
):
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    configure_logging("INFO", tmp_path)
    response = type("Response", (), {"id": "req-live"})()
    kwargs = dict(
        model="test-model",
        mode="json_schema",
        response=response,
        raw_content='{"value":"raw"}',
        finish_reason="stop",
        prompt_tokens=3,
        completion_tokens=4,
    )

    llm_client._debug_log_response(**kwargs)
    assert "LLM response" not in stderr.getvalue()

    apply_log_level("DEBUG")
    llm_client._debug_log_response(**kwargs)
    for handler in _owned_handlers(isolated_root_logging):
        handler.flush()

    assert "LLM response model=test-model mode=json_schema" in stderr.getvalue()
    assert 'content={"value":"raw"}' in stderr.getvalue()


def test_runtime_option_listener_uses_live_level_helper():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert 'elif name == "LOG_LEVEL":' in main_source
    assert "apply_log_level(_validated_log_level(value))" in main_source


def test_saved_level_is_used_by_subsequent_initialization(
    tmp_path, isolated_root_logging,
):
    configured = AppOptions.from_dict({"LOG_LEVEL": "ERROR"})
    configure_logging(configured.log_level, tmp_path)
    assert isolated_root_logging.level == logging.ERROR
    assert all(handler.level == logging.ERROR
               for handler in _owned_handlers(isolated_root_logging))


def test_apply_log_level_updates_existing_application_logger_handlers(
    isolated_root_logging,
):
    logger = logging.getLogger("app.worker")
    handler = logging.StreamHandler(io.StringIO())
    logger.addHandler(handler)
    try:
        apply_log_level("WARNING")
        assert logger.level == logging.WARNING
        assert handler.level == logging.WARNING
    finally:
        logger.removeHandler(handler)
        handler.close()

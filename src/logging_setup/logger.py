"""Reusable logging setup for the bot."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from config.defaults import (
    DEFAULT_LOG_DATE_FORMAT,
    DEFAULT_LOG_FILE_PATH,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
)

BOT_LOGGER_NAME = "abm_ib_bot"
_HANDLER_MARKER = "_abm_ib_bot_handler"


def setup_logging(
    settings: Any | None = None,
    *,
    file_path: str | Path | None = None,
    level: int | str | None = None,
    log_format: str | None = None,
    date_format: str | None = None,
) -> logging.Logger:
    """Configure console and current-run file logging.

    The log file is opened in write mode so each bot start keeps only the
    current run's logs.
    """

    logger_settings = getattr(settings, "logger", None)
    log_path = _resolve_file_path(logger_settings, file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_level = _normalize_log_level(_resolve_setting(logger_settings, "level", level, DEFAULT_LOG_LEVEL))
    formatter = logging.Formatter(
        _resolve_setting(
            logger_settings,
            "format",
            log_format,
            DEFAULT_LOG_FORMAT,
        ),
        datefmt=_resolve_setting(logger_settings, "date_format", date_format, DEFAULT_LOG_DATE_FORMAT),
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    _remove_existing_bot_handlers(root_logger)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    setattr(console_handler, _HANDLER_MARKER, True)

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logger = logging.getLogger(BOT_LOGGER_NAME)
    logger.setLevel(log_level)
    logger.debug("Logging initialized. log_file=%s", log_path)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a bot logger or a child logger."""

    if not name:
        return logging.getLogger(BOT_LOGGER_NAME)
    return logging.getLogger(f"{BOT_LOGGER_NAME}.{name}")


def _resolve_file_path(settings: Any | None, file_path: str | Path | None) -> Path:
    resolved = _resolve_setting(settings, "file_path", file_path, DEFAULT_LOG_FILE_PATH)
    return Path(resolved)


def _resolve_setting(settings: Any | None, name: str, override: Any | None, default: Any) -> Any:
    if override is not None:
        return override
    if settings is None:
        return default
    return getattr(settings, name, default)


def _normalize_log_level(level: int | str) -> int:
    if isinstance(level, int):
        return level

    normalized = logging.getLevelName(level.upper())
    if isinstance(normalized, int):
        return normalized

    raise ValueError(f"Invalid log level: {level}")


def _remove_existing_bot_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

"""Diagnostics shared by ReplayLab's API and vendor-native Agent engines.

Write a rotating log under ``%LOCALAPPDATA%/ReplayLab/logs`` while exposing
only a bounded, secret-redacted version of an exception to the UI.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import tempfile
import traceback
from pathlib import Path

LOGGER_NAME = "replaylab.agent"
_MAX_PUBLIC_ERROR_CHARS = 600
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*[^\s,;]{4,})"
)
_handler: logging.Handler | None = None
_handler_root: Path | None = None


def default_log_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path(tempfile.gettempdir())
    return root / "ReplayLab" / "logs"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(root: Path | None = None, *, level: int = logging.DEBUG) -> logging.Logger:
    """Attach one rotating file handler under ``root`` (idempotent, never raises).

    Calling it again with a different ``root`` closes the previous handler and
    opens a new one; ``close_logging()`` releases the file (Windows cannot
    delete an open log file, which matters for tests and log rotation tools).
    """

    global _handler, _handler_root
    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    log_root = (root or default_log_root())
    if _handler is not None and _handler_root == log_root:
        return logger
    close_logging()
    try:
        log_root.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_root / "agent-core.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s %(process)d %(name)s: %(message)s"
            )
        )
    except Exception:  # pragma: no cover - logging must never break a run
        handler = logging.NullHandler()
    logger.addHandler(handler)
    _handler, _handler_root = handler, log_root
    if os.environ.get("REPLAYLAB_CORE_LOG_STDERR") and not any(
        isinstance(existing, logging.StreamHandler) and not isinstance(existing, logging.FileHandler)
        for existing in logger.handlers
    ):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-5s %(name)s: %(message)s"))
        logger.addHandler(stream)
    return logger


def close_logging() -> None:
    """Flush and release the log file handler (safe to call repeatedly)."""

    global _handler, _handler_root
    handler, _handler, _handler_root = _handler, None, None
    if handler is None:
        return
    logger = get_logger()
    try:
        logger.removeHandler(handler)
        handler.flush()
        handler.close()
    except Exception:  # pragma: no cover
        pass


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(0).split()[0][:12] + "[REDACTED]", str(text or ""))


def public_error(exc: BaseException, *, limit: int = _MAX_PUBLIC_ERROR_CHARS) -> str:
    """One bounded, redacted line that still names the real failure.

    The exception chain is walked so that ``ConnectorError("ACP agent failed")``
    raised from ``FileNotFoundError('claude-code-acp')`` still tells the user
    which executable was missing.
    """

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = str(current).strip() or current.__class__.__name__
        parts.append(f"{current.__class__.__name__}: {message}")
        current = current.__cause__ or current.__context__
    text = " <- ".join(parts)
    text = redact(text).replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    logger.error(
        "%s: %s\n%s",
        message,
        public_error(exc, limit=4000),
        redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
    )


__all__ = [
    "LOGGER_NAME",
    "close_logging",
    "configure_logging",
    "default_log_root",
    "get_logger",
    "log_exception",
    "public_error",
    "redact",
]

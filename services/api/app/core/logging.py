from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Any

from app.core.config import LOGS_DIR
from app.core.precision import utc_iso
from app.core.security import redact_sensitive_data


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": utc_iso(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_data = getattr(record, "structured_data", None)
        if extra_data:
            payload["data"] = redact_sensitive_data(extra_data)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MiB per file
LOG_BACKUP_COUNT = 5              # 5 rotated files -> ~60 MiB ceiling per logger


def get_logger(name: str = "terminal") -> logging.Logger:
    """
    Structured JSON logger with size-based rotation.

    Rotation matters on a 24h-resident Mac mini: an unrotated `terminal.log` will
    eventually fill the disk, and a full disk is one of the alert thresholds that
    would otherwise force new entries to be blocked (§9.4.7 / §9.4.8).
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = StructuredJsonFormatter()

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        file_handler = RotatingFileHandler(
            LOGS_DIR / "terminal.log",
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **kwargs: Any) -> None:
    logger.log(level, message, extra={"structured_data": redact_sensitive_data(kwargs)})

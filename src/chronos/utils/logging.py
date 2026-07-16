"""Console and rotating-file logging with account masking."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_ACCOUNT_PATTERN = re.compile(r"\b(?:DU|U)\d{4,}\b", flags=re.IGNORECASE)


def mask_account_id(account_id: str) -> str:
    """Retain enough of an account identifier for user verification without logging it."""

    if not account_id:
        return "Not configured"
    if len(account_id) <= 4:
        return "•" * len(account_id)
    return f"{account_id[:2]}{'•' * max(len(account_id) - 6, 3)}{account_id[-4:]}"


def mask_account_identifiers(value: str) -> str:
    return _ACCOUNT_PATTERN.sub(lambda match: mask_account_id(match.group(0)), value)


class StructuredJsonFormatter(logging.Formatter):
    """One-line JSON logs suitable for local inspection and rotation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_account_identifiers(record.getMessage()),
        }
        for name in (
            "event",
            "correlation_id",
            "symbol",
            "broker_order_id",
            "contract_id",
            "contract_ids",
            "error_code",
            "operation",
            "attempt",
            "delay_seconds",
        ):
            if (value := getattr(record, name, None)) is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = mask_account_identifiers(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    level: str = "INFO",
    log_file: Path = Path("logs/chronos.log"),
) -> logging.Logger:
    """Configure idempotent Chronos-owned console and rotating-file handlers."""

    logger = logging.getLogger("chronos")
    logger.setLevel(level)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if getattr(handler, "_chronos_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = StructuredJsonFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._chronos_handler = True  # type: ignore[attr-defined]

    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    rotating._chronos_handler = True  # type: ignore[attr-defined]

    logger.addHandler(console)
    logger.addHandler(rotating)
    return logger

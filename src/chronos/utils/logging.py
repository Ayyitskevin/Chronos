"""Console and rotating-file logging with account masking."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_ACCOUNT_PATTERN = re.compile(r"\b(?:DU|U)\d{4,}\b", flags=re.IGNORECASE)


def _secure_existing_log(path: Path) -> None:
    """Make an owned regular log private without following links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Chronos log path must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise ValueError(f"Chronos log must be an owned regular file: {path}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _secure_log_family(base_path: Path, backup_count: int) -> None:
    _secure_existing_log(base_path)
    for index in range(1, backup_count + 1):
        _secure_existing_log(Path(f"{base_path}.{index}"))


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Keep the active log private, including after rotation creates a new file."""

    def _open(self) -> Any:
        def private_opener(path: str, flags: int) -> int:
            descriptor = os.open(
                path,
                flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise ValueError("Chronos log must be an owned regular file")
                os.fchmod(descriptor, 0o600)
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

        stream = open(  # noqa: SIM115
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            opener=private_opener,
        )
        return stream

    def doRollover(self) -> None:
        base_path = Path(self.baseFilename)
        _secure_log_family(base_path, self.backupCount)
        super().doRollover()
        _secure_log_family(base_path, self.backupCount)


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
            "error_type",
            "operation",
            "attempt",
            "delay_seconds",
            "candidate_count",
            "rejected_count",
            "outcome",
            "wheel_stage",
            "reconciliation_status",
            "guardrail",
            "passed",
            "basis_entry_type",
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

    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_log_family(log_file, 5)
    rotating = _SecureRotatingFileHandler(
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

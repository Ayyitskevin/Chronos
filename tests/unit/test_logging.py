import json
import logging
import os
import stat
from pathlib import Path

import pytest

from chronos.utils.logging import (
    StructuredJsonFormatter,
    configure_logging,
    mask_account_id,
)


def test_account_id_masking_retains_only_verification_suffix() -> None:
    assert mask_account_id("DU1234567") == "DU•••4567"
    assert mask_account_id("") == "Not configured"


def test_structured_formatter_masks_account_ids() -> None:
    record = logging.LogRecord(
        name="chronos.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Connected account DU1234567",
        args=(),
        exc_info=None,
    )

    payload = json.loads(StructuredJsonFormatter().format(record))

    assert payload["message"] == "Connected account DU•••4567"
    assert payload["level"] == "INFO"


def test_structured_formatter_preserves_safe_market_data_diagnostics() -> None:
    record = logging.LogRecord(
        name="chronos.broker.market_data",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Pacing retry",
        args=(),
        exc_info=None,
    )
    record.event = "market_data_rejected"
    record.contract_id = 123
    record.error_code = 420
    record.attempt = 1
    record.delay_seconds = 0.0

    payload = json.loads(StructuredJsonFormatter().format(record))

    assert payload["event"] == "market_data_rejected"
    assert payload["contract_id"] == 123
    assert payload["error_code"] == 420
    assert payload["attempt"] == 1
    assert payload["delay_seconds"] == 0.0


def test_structured_formatter_preserves_candidate_summary() -> None:
    record = logging.LogRecord(
        name="chronos.strategy.strike_resolver",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Option candidates evaluated",
        args=(),
        exc_info=None,
    )
    record.event = "candidate_evaluation_completed"
    record.symbol = "AAPL"
    record.candidate_count = 2
    record.rejected_count = 3
    record.outcome = "ELIGIBLE"

    payload = json.loads(StructuredJsonFormatter().format(record))

    assert payload["candidate_count"] == 2
    assert payload["rejected_count"] == 3
    assert payload["outcome"] == "ELIGIBLE"


def test_configured_log_file_is_private(tmp_path: Path) -> None:
    log_file = tmp_path / "private" / "chronos.log"

    logger = configure_logging(log_file=log_file)
    logger.info("private evidence")
    for handler in logger.handlers:
        handler.flush()

    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_existing_active_and_rotated_logs_are_made_private(tmp_path: Path) -> None:
    log_file = tmp_path / "chronos.log"
    backup = tmp_path / "chronos.log.1"
    log_file.write_text("active\n")
    backup.write_text("backup\n")
    log_file.chmod(0o666)
    backup.chmod(0o666)

    configure_logging(log_file=log_file)

    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_rollover_keeps_active_and_backup_logs_private(tmp_path: Path) -> None:
    log_file = tmp_path / "chronos.log"
    logger = configure_logging(log_file=log_file)
    rotating = next(handler for handler in logger.handlers if hasattr(handler, "doRollover"))

    logger.info("evidence before rotation")
    rotating.doRollover()
    logger.info("evidence after rotation")
    rotating.flush()

    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{log_file}.1").stat().st_mode) == 0o600


@pytest.mark.parametrize("target_name", ["chronos.log", "chronos.log.1"])
def test_log_configuration_rejects_symlink_targets(
    tmp_path: Path,
    target_name: str,
) -> None:
    real_file = tmp_path / "real.log"
    real_file.write_text("do not follow\n")
    os.symlink(real_file, tmp_path / target_name)

    with pytest.raises(ValueError, match="regular file"):
        configure_logging(log_file=tmp_path / "chronos.log")

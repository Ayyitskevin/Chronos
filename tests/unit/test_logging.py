import json
import logging

from chronos.utils.logging import StructuredJsonFormatter, mask_account_id


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

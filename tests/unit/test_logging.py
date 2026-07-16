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

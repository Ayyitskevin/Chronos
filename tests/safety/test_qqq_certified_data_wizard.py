from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIZARD = ROOT / "scripts" / "qqq_certified_data_wizard.sh"
TEMPLATE_PREFIX_SHA256 = "99763366717d77c1fa17649c367810d38b7fe2ef3616a7cbc8af56680d63a0aa"


def _text() -> str:
    return WIZARD.read_text(encoding="utf-8")


def test_wizard_keeps_the_shared_library_byte_exact() -> None:
    prefix = _text().split("\nTOTAL_STAGES=8\n", maxsplit=1)[0] + "\n"
    assert hashlib.sha256(prefix.encode()).hexdigest() == TEMPLATE_PREFIX_SHA256


def test_wizard_pins_the_reviewed_daily_capture_and_safe_gateway_posture() -> None:
    source = _text()
    assert 'SYMBOLS="QQQ,SPY,IWM,DIA,GLD,TLT"' in source
    assert "--end-date 2026-08-21" in source
    assert "--duration-days 9500" in source
    assert "--bar-size 1d" in source
    assert "--exchange SMART" in source
    assert "IB_ENVIRONMENT=paper" in source
    assert "ALLOW_ORDER_TRANSMIT=false" in source
    assert "ALLOW_LIVE_TRADING=false" in source
    assert "Read-Only API" in source
    assert "--allow-correction" not in source
    assert "--bar-size 1h" not in source


def test_wizard_has_no_order_holdout_unlock_or_trial_authority() -> None:
    source = _text()
    for forbidden in (
        "placeOrder",
        "submit_order",
        "cancel_order",
        "holdout unlock",
        "request_unlock",
        "mediated_holdout_read",
        "register_run",
        "trial_started",
    ):
        assert forbidden not in source
    assert sum(line.startswith('stage "') for line in source.splitlines()) == 8
    assert "NOT_CERTIFIED is a valid result" in source
    assert "no threshold may be changed" in source


def test_wizard_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(WIZARD)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

"""Runtime paper decision-ledger bootstrap (paper-only, no orders cycle)."""

from __future__ import annotations

from pathlib import Path

from chronos.config.settings import Settings
from chronos.paperops.bootstrap import open_paper_decision_ledger
from chronos.paperops.ledger import DecisionLedger, verify_decision_ledger


def test_paper_settings_open_ledger(tmp_path: Path) -> None:
    path = tmp_path / "data" / "paper_decision_ledger.jsonl"
    settings = Settings(
        _env_file=None,
        ib_environment="paper",
        enable_paper_decision_ledger=True,
        paper_decision_ledger_file=path,
        symbol_allowlist=("SPY",),
    )
    ledger = open_paper_decision_ledger(settings)
    assert ledger is not None
    assert isinstance(ledger, DecisionLedger)
    assert path.parent.is_dir()
    ok, detail = verify_decision_ledger(path)
    assert ok, detail


def test_live_settings_never_open_ledger(tmp_path: Path) -> None:
    path = tmp_path / "live.jsonl"
    # Live conjunction is strict; use a valid full conjunction so Settings loads.
    settings = Settings(
        _env_file=None,
        broker_mode="ibkr",
        broker_adapter="official_ibkr",
        ib_environment="live",
        allow_order_transmit=True,
        allow_live_trading=True,
        ib_account_id="U7654321",
        ib_account_allowlist=("U7654321",),
        require_live_arming=True,
        require_typed_confirmation=True,
        enable_paper_decision_ledger=True,
        paper_decision_ledger_file=path,
        symbol_allowlist=("SPY",),
    )
    assert open_paper_decision_ledger(settings) is None
    assert not path.exists()


def test_disabled_flag_skips_ledger(tmp_path: Path) -> None:
    path = tmp_path / "off.jsonl"
    settings = Settings(
        _env_file=None,
        ib_environment="paper",
        enable_paper_decision_ledger=False,
        paper_decision_ledger_file=path,
        symbol_allowlist=("SPY",),
    )
    assert open_paper_decision_ledger(settings) is None

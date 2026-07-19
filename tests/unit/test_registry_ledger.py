"""Experiment-registry ledger + trial-count + budget tests (C2, ADR-0013 §1/§2/§5/§6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronos.registry import (
    RegistryLedger,
    RunStage,
    accrued_capture_sessions,
    available_budget,
    data_fingerprint,
    register_run,
    trial_count,
)


def _ledger(tmp_path: Path) -> RegistryLedger:
    return RegistryLedger(tmp_path / "registry.jsonl")


def _run(ledger: RegistryLedger, strategy: str, *, touched: bool = True) -> None:
    register_run(
        ledger,
        stage=RunStage.DEV,
        strategy_id=strategy,
        config_hash="cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "b", "actions_sha": "a"}},
        criteria_ref="frozen@2026-01-01",
        touched_data=touched,
    )


def test_append_and_verify_chain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "regime_trend_v1")
    assert len(ledger.records()) == 2
    ok, detail = ledger.verify()
    assert ok, detail


def test_verify_detects_tampering(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "mean_reversion_v1")
    # Edit a committed record's payload in place — the chain must catch it.
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["payload"]["strategy_id"] = "tampered"
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = ledger.verify()
    assert ok is False


def test_trial_count_is_derived_and_scoped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "regime_trend_v1")
    _run(ledger, "mean_reversion_v1")
    _run(ledger, "regime_trend_v1", touched=False)  # a non-data-touching run is not a trial
    assert trial_count(ledger) == 3
    assert trial_count(ledger, strategy_id="regime_trend_v1") == 2


def test_data_fingerprint_reads_bars_and_actions(tmp_path: Path) -> None:
    history = tmp_path / "research/data/history"
    history.mkdir(parents=True)
    (history / "MANIFEST.json").write_text(
        json.dumps(
            {
                "symbols": {
                    "SPY": {
                        "bars": {"sha256": "bars_hash"},
                        "corporate_actions": {"sha256": "actions_hash"},
                    },
                    "QQQ": {"bars": {"sha256": "qqq_bars"}},  # no actions file yet
                }
            }
        ),
        encoding="utf-8",
    )
    fp = data_fingerprint(tmp_path, ["SPY", "QQQ", "IWM"])
    assert fp["SPY"] == {"bars_sha": "bars_hash", "actions_sha": "actions_hash"}
    assert fp["QQQ"] == {"bars_sha": "qqq_bars", "actions_sha": None}
    assert fp["IWM"] == {"bars_sha": None, "actions_sha": None}  # absent symbol


def test_budget_rations_against_accrual(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    # 40 accrued sessions, one credit per 20 → 2 earned, 0 spent, capped at 2 → 2.
    assert (
        available_budget(
            ledger, accrued_sessions=40, sessions_per_unlock=20, max_outstanding_unlocks=2
        )
        == 2
    )
    # Zero accrual fails closed.
    assert (
        available_budget(
            ledger, accrued_sessions=0, sessions_per_unlock=20, max_outstanding_unlocks=2
        )
        == 0
    )
    # The cap bounds it below what's earned.
    assert (
        available_budget(
            ledger, accrued_sessions=200, sessions_per_unlock=20, max_outstanding_unlocks=2
        )
        == 2
    )


def test_accrued_capture_sessions_counts_option_snapshots(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    (history / "MANIFEST.json").write_text(
        json.dumps(
            {
                "symbols": {
                    "SPY": {"options": {"snapshots": {"2026-07-19": {}, "2026-07-20": {}}}},
                    "QQQ": {"options": {"snapshots": {"2026-07-20": {}}}},
                }
            }
        ),
        encoding="utf-8",
    )
    assert accrued_capture_sessions(history) == 3


# --- CLI wiring -------------------------------------------------------------


def test_cli_registry_stats_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from chronos.cli.main import main

    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    path = str(ledger.path)
    assert main(["registry", "stats", "--ledger", path]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trials"] == 1 and payload["chain_ok"] is True
    assert main(["registry", "verify", "--ledger", path]) == 0


def test_cli_holdout_status(tmp_path: Path) -> None:
    from chronos.cli.main import main

    ledger = _ledger(tmp_path)
    code = main(
        ["holdout", "status", "--ledger", str(ledger.path), "--history-root", str(tmp_path)]
    )
    assert code == 0


def test_cli_holdout_unlock_requires_env_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chronos.cli.main import main

    monkeypatch.delenv("CHRONOS_HOLDOUT_UNLOCK_PHRASE", raising=False)
    ledger = _ledger(tmp_path)
    code = main(
        [
            "holdout",
            "unlock",
            "--window",
            "w",
            "--reason",
            "r",
            "--ledger",
            str(ledger.path),
            "--history-root",
            str(tmp_path),
        ]
    )
    assert code == 2  # no phrase in env → refused before any ledger write
    assert ledger.records() == ()

"""`research selection` — the frozen criteria at the operator's hand (slice two).

The verdict module answers the six criteria; this suite pins the command that shows them.
Its subject is the *artifact*: a real campaign report, produced here from the checked-in
corpus by the real pipeline, evaluated against the real frozen manifest, printed as a table
an owner can read — with every UNVERIFIED naming its missing input and the function that
would compute it, and an exit code that is 1 rather than 0 because promotion is not this
command's to make.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from chronos.backtest.engine import BacktestConfig
from chronos.cli.selection_commands import cmd_research_selection, load_reports
from chronos.registry import RegistryLedger
from chronos.research.campaign import run_campaign
from chronos.research.selection_verdict import SELECTION_MANIFEST_PATH
from chronos.risk.policy import load_risk_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / SELECTION_MANIFEST_PATH
RAW = REPO_ROOT / "research" / "data" / "raw"
CANDIDATE = "mean_reversion_v1"
TWIN = "baseline_random_entries"
SYMBOL = "SPY"


def _tripwire(reason: str) -> Any:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(reason)

    return _fail


@pytest.fixture(scope="module")
def real_report(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real campaign report: the checked-in corpus, the real pipeline, the real writer.

    Serialised exactly as ``cmd_research_campaign`` serialises it (``dataclasses.asdict``),
    so the loader is exercised against the shape the CLI will actually be handed.
    """

    root = tmp_path_factory.mktemp("selection-cli")
    report = run_campaign(
        strategies=[CANDIDATE, TWIN],
        symbols=[SYMBOL],
        data_dir=RAW,
        policy=load_risk_policy(REPO_ROOT / "config" / "risk.research.yaml"),
        ledger=RegistryLedger(root / "ledger.jsonl"),
        halt_dir=root / "halt",
        config=BacktestConfig(initial_cash_usd=3000.0, slippage_bps_per_side=2.0),
        seed=0,
    )
    path = root / "campaign.json"
    path.write_text(json.dumps(dataclasses.asdict(report), indent=2, default=str), encoding="utf-8")
    return path


def _args(report: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "manifest": MANIFEST,
        "report": report,
        "baseline_report": [],
        "expected_sha256": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_the_table_for_a_real_campaign_report(real_report: Path, capsys: Any) -> None:
    """The owner-visible artifact, on real data, exiting 1 by design."""

    code = cmd_research_selection(_args(real_report))
    out = capsys.readouterr().out

    assert code == 1, "exit 0 is reserved for BACKTEST_VALIDATED"
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    assert digest in out, "the table must say which criteria document it judged against"

    for criterion in ("C1", "C2", "C3", "C4", "C5", "C6"):
        assert f"  {criterion}  " in out, criterion
    # The frozen text is quoted, not paraphrased.
    assert "net-positive after base costs" in out
    assert "profit factor >= 1.1" in out
    # Every gap names the function that would close it.
    assert "MISSING:" in out
    assert "chronos.backtest.metrics.compute_metrics" in out
    assert "PerformanceMetrics.total_return_fraction" in out
    # And the aggregate, with the reason promotion did not happen.
    assert "VERDICT: UNVERIFIED" in out
    assert "multiple" in out.lower()


def test_one_verdict_per_cell_and_no_pooling(real_report: Path, capsys: Any) -> None:
    """A campaign report yields a verdict per cell; the criteria are per symbol."""

    reports = load_reports(real_report)
    assert {report.strategy_id for report in reports} == {CANDIDATE, TWIN}
    cmd_research_selection(_args(real_report))
    out = capsys.readouterr().out
    for strategy in (CANDIDATE, TWIN):
        assert f"{strategy} / {SYMBOL}" in out


def test_a_digest_that_is_not_the_expected_one_judges_nothing(
    real_report: Path, capsys: Any
) -> None:
    """Exit 2, not 1: a refusal to judge is not a judgement."""

    wrong = "0" * 64
    code = cmd_research_selection(_args(real_report, expected_sha256=wrong))
    out = capsys.readouterr().out

    assert code == 2
    assert "UNUSABLE" in out
    assert wrong in out
    # The table is wrapped for reading, so assert on the text with whitespace collapsed.
    flat = " ".join(out.split())
    assert "these are not the criteria this verdict was asked to judge against" in flat
    assert "C1 UNVERIFIED" in flat, "a mismatch refuses every criterion, not just the aggregate"


def test_an_unreadable_report_is_refused_rather_than_judged(tmp_path: Path, capsys: Any) -> None:
    bad = tmp_path / "not-a-report.json"
    bad.write_text('{"strategy_id": "x"}', encoding="utf-8")
    code = cmd_research_selection(_args(bad))
    out = capsys.readouterr().out
    assert code == 2
    assert "UNUSABLE" in out and "not a walk-forward report" in out


def test_the_command_touches_no_network_no_settings_and_writes_nothing(
    real_report: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """The A4 boundary, held by the same tripwires the intake commands use."""

    monkeypatch.setattr(socket, "socket", _tripwire("network attempted"))
    monkeypatch.setattr("chronos.config.settings.Settings", _tripwire("Settings constructed"))
    monkeypatch.chdir(tmp_path)

    before = sorted(p.name for p in tmp_path.iterdir())
    report_before = real_report.read_bytes()
    manifest_before = MANIFEST.read_bytes()

    code = cmd_research_selection(_args(real_report))
    capsys.readouterr()

    assert code == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "the command wrote something"
    assert real_report.read_bytes() == report_before
    assert MANIFEST.read_bytes() == manifest_before, "the frozen manifest must be read-only"

"""Deterministic research-run manifests and readiness assessments."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.backtest.engine import BacktestConfig
from chronos.registry import RegistryLedger
from chronos.research.campaign import run_campaign
from chronos.research.manifest import (
    ResearchRunManifest,
    manifest_from_campaign,
    stable_hash,
    write_manifest,
)
from chronos.research.readiness import (
    LIVE_TRADING_BLOCKED,
    LiveReviewReadiness,
    PaperReadiness,
    assess_campaign_readiness,
)
from chronos.research.walkforward import WalkForwardVerdict
from chronos.risk.policy import load_risk_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_POLICY_PATH = REPO_ROOT / "config" / "risk.research.yaml"
WARMUP = 20
TEST_WINDOW = 20


def _write_csv(data_dir: Path, symbol: str, n_bars: int, start: date = date(2005, 1, 3)) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,volume"]
    session = start
    prev_close = 100.0
    for i in range(n_bars):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        lines.append(f"{session.isoformat()},{open_},{high},{low},{close},1000000")
        prev_close = close
        session += timedelta(days=1)
    (data_dir / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_bad_ohlc_csv(data_dir: Path, symbol: str, n_bars: int = 80) -> None:
    """Valid length but impossible OHLC on every bar (high < low)."""

    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,volume"]
    session = date(2005, 1, 3)
    for _i in range(n_bars):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        # high < low -> IMPOSSIBLE_OHLC (blocking)
        lines.append(f"{session.isoformat()},100.0,90.0,110.0,100.0,1000000")
        session += timedelta(days=1)
    (data_dir / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_campaign(tmp_path: Path, symbols: list[str], seed: int = 7) -> object:
    data = tmp_path / "data"
    for sym in symbols:
        if not (data / f"{sym}.csv").exists():
            _write_csv(data, sym, 120)
    return run_campaign(
        strategies=["baseline_buy_hold"],
        symbols=symbols,
        data_dir=data,
        policy=load_risk_policy(RESEARCH_POLICY_PATH),
        ledger=RegistryLedger(tmp_path / f"reg_{seed}.jsonl"),
        halt_dir=tmp_path / f"halts_{seed}",
        config=BacktestConfig(initial_cash_usd=3000.0, slippage_bps_per_side=0.0),
        stage_end="2021-12-31",
        warmup=WARMUP,
        test_window=TEST_WINDOW,
        min_trades=20,
        seed=seed,
        block_size=5,
        n_resamples=100,
    )


def test_stable_hash_is_deterministic() -> None:
    a = stable_hash({"b": 2, "a": 1})
    b = stable_hash({"a": 1, "b": 2})
    assert a == b
    assert len(a) == 64


def test_manifest_from_campaign_includes_provenance_and_verdicts(tmp_path: Path) -> None:
    report = _run_campaign(tmp_path, ["SPY"], seed=3)
    manifest = manifest_from_campaign(report)

    assert isinstance(manifest, ResearchRunManifest)
    assert manifest.schema_version == "research-manifest-v1"
    assert manifest.code_commit not in {"", "unknown"}
    assert manifest.policy_hash
    assert manifest.config_hash
    assert "SPY" in manifest.data_hashes
    assert len(manifest.data_hashes["SPY"]) == 64
    assert WalkForwardVerdict.PASS.value in manifest.verdict_counts
    assert WalkForwardVerdict.FAIL.value in manifest.verdict_counts
    assert WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value in manifest.verdict_counts
    # Low-sample buy-hold on synthetic bars → not a fabricated PASS.
    assert manifest.overall_verdict in {
        WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value,
        WalkForwardVerdict.FAIL.value,
        "mixed",
        "empty",
    }
    assert manifest.overall_verdict != WalkForwardVerdict.PASS.value

    d1 = manifest.to_dict()
    d2 = manifest.to_dict()
    assert d1 == d2
    assert manifest.fingerprint() == stable_hash(d1)


def test_manifest_write_and_reread_is_stable(tmp_path: Path) -> None:
    report = _run_campaign(tmp_path, ["SPY"], seed=5)
    m1 = manifest_from_campaign(report)
    m2 = manifest_from_campaign(report)
    assert m1.to_dict() == m2.to_dict()
    assert m1.fingerprint() == m2.fingerprint()

    path = tmp_path / "out" / "manifest.json"
    write_manifest(m1, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["config_hash"] == m1.config_hash
    assert loaded["code_commit"] == m1.code_commit
    assert loaded["policy_hash"] == m1.policy_hash
    assert loaded["data_hashes"]["SPY"] == m1.data_hashes["SPY"]
    assert loaded["manifest_fingerprint"] == m1.fingerprint()


def test_readiness_keeps_live_blocked_and_insufficient_evidence_valid(
    tmp_path: Path,
) -> None:
    report = _run_campaign(tmp_path, ["SPY"], seed=9)
    assessment = assess_campaign_readiness(report)

    assert assessment.live_trading_blocked is True
    assert assessment.live_outcome == LIVE_TRADING_BLOCKED
    assert assessment.live_outcome == "LIVE TRADING BLOCKED"
    assert assessment.paper is PaperReadiness.NOT_READY
    assert assessment.live_review is LiveReviewReadiness.NOT_ELIGIBLE
    assert assessment.overall_verdict in {
        WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value,
        WalkForwardVerdict.FAIL.value,
        "mixed",
        "empty",
    }
    # INSUFFICIENT_EVIDENCE is first-class: blockers name it, never "fix" it.
    assert any("PASS" in b or "verdict" in b for b in assessment.paper_blockers)
    assert assessment.evidence_required_for_paper
    assert assessment.evidence_required_for_live_review
    assert any("LIVE TRADING BLOCKED" in b for b in assessment.live_review_blockers)

    payload = assessment.to_dict()
    assert payload["live_trading_blocked"] is True
    assert payload["paper"] == "not_ready"
    assert "manifest_fingerprint" in payload


def test_campaign_excludes_blocking_data_quality(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", 120)
    _write_bad_ohlc_csv(data, "BAD")
    report = run_campaign(
        strategies=["baseline_buy_hold"],
        symbols=["SPY", "BAD"],
        data_dir=data,
        policy=load_risk_policy(RESEARCH_POLICY_PATH),
        ledger=RegistryLedger(tmp_path / "reg.jsonl"),
        halt_dir=tmp_path / "h",
        config=BacktestConfig(initial_cash_usd=3000.0, slippage_bps_per_side=0.0),
        stage_end="2021-12-31",
        warmup=WARMUP,
        test_window=TEST_WINDOW,
        min_trades=20,
        seed=1,
        block_size=5,
        n_resamples=50,
    )
    excluded = dict(report.excluded)
    assert "BAD" in excluded
    assert "blocking data-quality" in excluded["BAD"] or "IMPOSSIBLE_OHLC" in excluded["BAD"]
    assert {row.symbol for row in report.verdict_table} == {"SPY"}
    # Contaminated symbol must not contribute a data fingerprint used in stats.
    bad_cells = [c for c in report.cells if c.symbol == "BAD"]
    assert bad_cells
    assert all(c.report is None for c in bad_cells)


def test_runner_refuses_blocking_data_quality(tmp_path: Path) -> None:
    from chronos.research.runner import run_named_backtest

    data = tmp_path / "data"
    _write_bad_ohlc_csv(data, "SPY", n_bars=40)
    policy = REPO_ROOT / "config" / "risk.research.yaml"
    with pytest.raises(ValueError, match="blocking data-quality"):
        run_named_backtest(
            strategy_name="baseline_buy_hold",
            symbol="SPY",
            data_dir=data,
            policy_path=policy,
            initial_cash=3000.0,
            slippage_bps=0.0,
            halt_path=tmp_path / "halt.json",
        )


def test_verdict_enum_has_three_first_class_outcomes() -> None:
    values = {v.value for v in WalkForwardVerdict}
    assert values == {"pass", "fail", "insufficient_evidence"}

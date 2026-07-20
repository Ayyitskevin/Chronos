"""Walk-forward loop + sample-honest verdict (C3, ADR-0014 §2).

Two layers:

1. ``_verdict`` as a pure decision table — INSUFFICIENT_EVIDENCE is the blocking default;
   PASS needs a positive CI *and* a deflated Sharpe over the threshold; FAIL is a positive
   rejection (CI strictly <= 0).
2. An end-to-end ``walk_forward`` over a deterministic synthetic backtest, asserting a
   thin sample never PASSes, exactly one trial is registered per configuration, and the
   whole report is reproducible under a fixed seed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from chronos.backtest.engine import BacktestConfig
from chronos.control.halt import HaltStore
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.registry import RegistryLedger, trial_count
from chronos.research.stats import moments
from chronos.research.walkforward import (
    WalkForwardReport,
    WalkForwardVerdict,
    _deflated_or_none,
    _verdict,
    walk_forward,
)
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import (
    PositionSide,
    ProposalDirection,
    StrategyContext,
    StrategyProposal,
)

INITIAL_CASH = 3000.0
N_BARS = 60


def _make_series() -> BarSeries:
    session = date(2020, 1, 1)
    bars: list[Bar] = []
    prev_close = 100.0
    for i in range(N_BARS):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.5 * i
        open_ = prev_close
        bars.append(
            Bar(
                symbol="SPY",
                source="synthetic",
                exchange="ARCA",
                interval=BarInterval.DAY_1,
                session_date=session,
                timestamp_utc=datetime.combine(session, time(21, 0), tzinfo=UTC),
                open=open_,
                high=max(open_, close) + 1.0,
                low=min(open_, close) - 1.0,
                close=close,
                volume=1_000_000.0,
            )
        )
        prev_close = close
        session += timedelta(days=1)
    return BarSeries(symbol="SPY", interval=BarInterval.DAY_1, bars=tuple(bars))


class _AlwaysEnterStrategy:
    """Enters long once while flat and holds forever — a run with zero closed trades."""

    strategy_id = "strat_a"
    version = "1"
    warmup_bars = 1

    def on_bar(self, context: StrategyContext) -> StrategyProposal | None:
        if context.position.side is not PositionSide.FLAT:
            return None
        bar = context.bars[-1]
        return StrategyProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            timestamp_utc=bar.timestamp_utc,
            symbol=bar.symbol,
            direction=ProposalDirection.ENTER_LONG,
            desired_exposure_fraction=0.5,
            stop_fraction=0.05,
            source_bar_ids=(bar.sequence_id,),
            reason="always enter",
        )


def _permissive_policy() -> RiskPolicy:
    return RiskPolicy.model_validate(
        {
            "policy_version": "test",
            "allowed_symbols": ("SPY",),
            "allowed_strategy_ids": ("strat_a",),
            "allow_long_entries": True,
            "max_bot_capital_usd": 3000,
            "max_position_notional_usd": 3000,
            "max_aggregate_exposure_usd": 3000,
            "max_symbol_exposure_fraction": 1.0,
            "max_risk_per_trade_fraction": 0.10,
            "max_simultaneous_positions": 1,
            "max_open_orders": 2,
            "max_daily_loss_usd": 300,
            "max_weekly_loss_usd": 600,
            "max_drawdown_fraction": 0.5,
            "max_consecutive_losses": 10,
            "max_quote_age_seconds": 300,
            "max_bar_age_seconds": 5 * 86400,
            "max_price_deviation_fraction": 0.05,
            "allow_overnight_positions": True,
        }
    )


def _run(ledger_path: Path, halt_path: Path, seed: int, min_trades: int = 20) -> WalkForwardReport:
    halt_store = HaltStore(halt_path)
    halt_store.rearm("armed for walk-forward test")
    return walk_forward(
        _make_series(),
        _AlwaysEnterStrategy,
        _permissive_policy(),
        BacktestConfig(initial_cash_usd=INITIAL_CASH, slippage_bps_per_side=0.0),
        halt_store=halt_store,
        ledger=RegistryLedger(ledger_path),
        strategy_id="strat_a",
        config_hash="cfg-1",
        code_commit="deadbeefcafe",
        data_hashes={"SPY": {"bars_sha": "abc"}},
        criteria_ref="test-criteria-v1",
        test_window=10,
        warmup=5,
        min_trades=min_trades,
        seed=seed,
        block_size=5,
        n_resamples=200,
    )


# --- Layer 1: the verdict decision table -------------------------------------------------


def test_verdict_defaults_to_insufficient_evidence_below_the_trade_floor() -> None:
    verdict, reason = _verdict(pooled_trades=3, ci=(1.0, 2.0), dsr=0.99, min_trades=20)
    assert verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE
    assert "3 OOS trades" in reason


def test_verdict_fails_when_ci_is_strictly_non_positive() -> None:
    verdict, _ = _verdict(pooled_trades=50, ci=(-0.5, -0.1), dsr=0.99, min_trades=20)
    assert verdict is WalkForwardVerdict.FAIL


def test_verdict_is_insufficient_when_ci_straddles_zero() -> None:
    verdict, _ = _verdict(pooled_trades=50, ci=(-0.1, 0.5), dsr=0.99, min_trades=20)
    assert verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE


def test_verdict_is_insufficient_when_deflated_sharpe_is_below_threshold() -> None:
    verdict, _ = _verdict(pooled_trades=50, ci=(0.1, 0.5), dsr=0.80, min_trades=20)
    assert verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE


def test_verdict_passes_only_when_ci_positive_and_dsr_clears() -> None:
    verdict, _ = _verdict(pooled_trades=50, ci=(0.1, 0.5), dsr=0.97, min_trades=20)
    assert verdict is WalkForwardVerdict.PASS


def test_verdict_is_insufficient_when_statistics_are_undefined() -> None:
    verdict, _ = _verdict(pooled_trades=50, ci=None, dsr=None, min_trades=20)
    assert verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE


# --- Layer 2: end-to-end walk-forward ----------------------------------------------------


def test_thin_sample_run_never_passes_and_registers_one_trial(tmp_path: Path) -> None:
    ledger_path = tmp_path / "registry.jsonl"
    report = _run(ledger_path, tmp_path / "halt.json", seed=7)

    # A hold-forever strategy closes no trades, so the honest verdict is a blocking
    # "insufficient evidence" — never PASS on a thin sample.
    assert report.verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE
    assert report.pooled_trades == 0

    # The full statistics path *did* run on the (non-flat, uptrending) equity curve — a
    # huge Sharpe with a strictly positive CI and PSR/DSR at the ceiling. The trade floor
    # is therefore a genuine independent gate: a statistically dazzling but untradeable
    # curve still cannot PASS. (Guards against the floor only mattering when stats are None.)
    assert report.pooled_sharpe is not None and report.pooled_sharpe > 0
    assert report.sharpe_ci is not None and report.sharpe_ci[0] > 0
    assert report.deflated_sharpe is not None and report.deflated_sharpe > 0.95

    # Exactly one trial was recorded for this configuration, and the report's trial
    # count is read back from the ledger (not self-reported).
    ledger = RegistryLedger(ledger_path)
    assert trial_count(ledger, strategy_id="strat_a") == 1
    assert report.trial_count == 1
    assert report.purged_cv == "not applicable (fixed-rule replay)"


def test_walk_forward_is_deterministic_under_a_fixed_seed(tmp_path: Path) -> None:
    # Separate fresh ledgers so each run registers exactly one trial (identical N),
    # isolating engine + bootstrap determinism.
    first = _run(tmp_path / "a.jsonl", tmp_path / "a-halt.json", seed=11)
    second = _run(tmp_path / "b.jsonl", tmp_path / "b-halt.json", seed=11)
    assert first == second


def test_a_second_run_on_the_same_ledger_raises_the_trial_count(tmp_path: Path) -> None:
    ledger_path = tmp_path / "shared.jsonl"
    first = _run(ledger_path, tmp_path / "h1.json", seed=3)
    second = _run(ledger_path, tmp_path / "h2.json", seed=3)
    # The registry is the multiple-testing memory: a second evaluation deflates harder.
    assert first.trial_count == 1
    assert second.trial_count == 2


# --- Layer 3: review remediation (honesty seams) -----------------------------------------


def test_deflated_sharpe_is_none_when_variance_unavailable_for_multiple_trials() -> None:
    # Finding 1: with N > 1 trials but no estimable cross-trial variance, we must NOT report
    # an *undeflated* PSR under the deflated_sharpe name — return None (→ INSUFFICIENT).
    m = moments([0.01, 0.02, -0.01, 0.03, 0.0, 0.02])
    assert m is not None
    assert _deflated_or_none(0.30, 120, 50, None, m) is None  # variance unestimable
    assert _deflated_or_none(0.30, 120, 50, 0.0, m) is None  # degenerate zero variance
    # A single trial needs no deflation → a real number (== the undeflated PSR, honestly).
    single = _deflated_or_none(0.30, 120, 1, None, m)
    assert single is not None and single > 0
    # A real positive cross-trial variance deflates strictly below the single-trial value.
    deflated = _deflated_or_none(0.30, 120, 50, 0.01, m)
    assert deflated is not None and deflated < single
    # An undefined point Sharpe is None regardless of N/variance.
    assert _deflated_or_none(None, 120, 1, 0.01, m) is None


def test_verdict_clamps_a_sub_one_trade_floor() -> None:
    # Finding 2: a 0/negative floor must not defeat the gate — zero trades still blocks.
    verdict_zero, _ = _verdict(pooled_trades=0, ci=(0.1, 0.5), dsr=0.99, min_trades=0)
    assert verdict_zero is WalkForwardVerdict.INSUFFICIENT_EVIDENCE
    verdict_neg, _ = _verdict(pooled_trades=0, ci=(0.1, 0.5), dsr=0.99, min_trades=-5)
    assert verdict_neg is WalkForwardVerdict.INSUFFICIENT_EVIDENCE


def test_verdict_distinguishes_dsr_unavailable_from_too_few_bars() -> None:
    # dsr None with a defined CI is the deflation-variance case, reported distinctly.
    verdict, reason = _verdict(pooled_trades=50, ci=(0.1, 0.5), dsr=None, min_trades=20)
    assert verdict is WalkForwardVerdict.INSUFFICIENT_EVIDENCE
    assert "deflated Sharpe undefined" in reason


def test_walk_forward_rejects_a_sub_one_trade_floor(tmp_path: Path) -> None:
    # The public entry fails closed on an invalid safety floor rather than trusting it.
    with pytest.raises(ValueError, match="min_trades"):
        _run(tmp_path / "x.jsonl", tmp_path / "xh.json", seed=1, min_trades=0)


def test_a_post_registration_failure_leaves_no_orphan_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The trial is registered LAST: a raise in the statistics block must leave the ledger
    # untouched, so a failed evaluation cannot inflate a sibling's multiple-testing N (C4).
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("synthetic stats failure")

    monkeypatch.setattr("chronos.research.walkforward.block_bootstrap_ci", boom)
    ledger_path = tmp_path / "reg.jsonl"
    with pytest.raises(RuntimeError, match="synthetic stats failure"):
        _run(ledger_path, tmp_path / "h.json", seed=1)
    assert trial_count(RegistryLedger(ledger_path), strategy_id="strat_a") == 0

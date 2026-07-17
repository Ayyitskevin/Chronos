"""Concrete breach ⇒ deny tests for every enforced risk-engine limit.

An independent review (M5) showed the deny-monotonicity property test is
structurally blind to a *disabled* check: a limit whose code is deleted denies
in neither the loose nor the strict policy, so monotonicity holds vacuously.
These tests close that hole: for each enforced limit, a generous baseline
policy APPROVES the reference intent, then breaching exactly one dimension
must produce a denial carrying that limit's specific rejection code. Deleting
any one check now fails its test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from chronos.control.halt import HaltStore
from chronos.control.modes import ModeLock, TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.intents import OrderIntent, TimeInForce
from chronos.risk.engine import AccountView, MarketViewEntry, RiskEngine, RiskRejectionCode
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)

_GENEROUS = {
    "policy_version": "limits-test",
    "allowed_symbols": ("SPY",),
    "allowed_strategy_ids": ("s",),
    "allow_long_entries": True,
    "allow_overnight_positions": True,
    "max_bot_capital_usd": 100_000,
    "max_position_notional_usd": 100_000,
    "max_aggregate_exposure_usd": 100_000,
    "max_symbol_exposure_fraction": 1.0,
    "max_risk_per_trade_fraction": 1.0,
    "max_simultaneous_positions": 10,
    "max_open_orders": 10,
    "max_daily_loss_usd": 10_000,
    "max_weekly_loss_usd": 10_000,
    "max_drawdown_fraction": 0.99,
    "max_consecutive_losses": 100,
    "max_quote_age_seconds": 3600,
    "max_bar_age_seconds": 5 * 86400,
    "max_price_deviation_fraction": 0.10,
}


def _policy(**overrides: object) -> RiskPolicy:
    payload: dict[str, object] = dict(_GENEROUS)
    payload.update(overrides)
    return RiskPolicy.model_validate(payload)


def _intent(
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    limit: str = "100.00",
    stop: str | None = "95.00",
) -> OrderIntent:
    return OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="SPY",
        side=side,
        quantity=quantity,
        limit_price=Decimal(limit),
        stop_price=Decimal(stop) if stop is not None else None,
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id="test:SPY:1d:2024-06-03",
        proposal_reason="test",
    )


def _account(**overrides: object) -> AccountView:
    payload: dict[str, object] = {
        "account_equity_usd": 3000.0,
        "cash_usd": 3000.0,
        "position_shares": {},
        "position_notional_usd": {},
        "open_order_count": 0,
        "realized_pnl_today_usd": 0.0,
        "realized_pnl_week_usd": 0.0,
        "peak_equity_usd": 3000.0,
        "consecutive_losses": 0,
        "as_of_utc": NOW,
    }
    payload.update(overrides)
    return AccountView(**payload)  # type: ignore[arg-type]


def _market(last: float = 100.0) -> MarketViewEntry:
    return MarketViewEntry(last_price=last, bar_close_utc=NOW, quote_utc=NOW)


def _sim_lock() -> ModeLock:
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


def _armed(tmp_path: Path):
    store = HaltStore(tmp_path / "halt.json")
    store.rearm("test")
    return store.read()


def _decide(
    tmp_path: Path,
    policy: RiskPolicy,
    *,
    intent: OrderIntent | None = None,
    account: AccountView | None = None,
    market: MarketViewEntry | None = None,
):
    return RiskEngine(policy).validate(
        intent if intent is not None else _intent(),
        account=account if account is not None else _account(),
        market=market if market is not None else _market(),
        halt=_armed(tmp_path),
        mode_lock=_sim_lock(),
        now_utc=NOW,
    )


def test_generous_baseline_approves(tmp_path: Path) -> None:
    decision = _decide(tmp_path, _policy())
    assert decision.approved, decision.explanations


def test_direction_not_allowed(tmp_path: Path) -> None:
    decision = _decide(tmp_path, _policy(allow_long_entries=False))
    assert not decision.approved
    assert RiskRejectionCode.DIRECTION_NOT_ALLOWED in decision.codes


def test_aggregate_exposure_limit(tmp_path: Path) -> None:
    # Existing 900 of other-symbol exposure + 1000 new notional > 1500 cap.
    decision = _decide(
        tmp_path,
        _policy(max_aggregate_exposure_usd=1500),
        account=_account(position_notional_usd={"QQQ": 900.0}, position_shares={"QQQ": 3}),
        # allow the extra position slot so only the aggregate limit binds
    )
    assert not decision.approved
    assert RiskRejectionCode.AGGREGATE_EXPOSURE_LIMIT in decision.codes


def test_symbol_exposure_limit(tmp_path: Path) -> None:
    # 1000 notional on 3000 equity = 33.4% > the 20% per-symbol cap.
    decision = _decide(tmp_path, _policy(max_symbol_exposure_fraction=0.20))
    assert not decision.approved
    assert RiskRejectionCode.SYMBOL_EXPOSURE_LIMIT in decision.codes


def test_risk_per_trade_limit(tmp_path: Path) -> None:
    # Trade risk (100-95)*10 = $50 = 1.67% of equity > 1% cap.
    decision = _decide(tmp_path, _policy(max_risk_per_trade_fraction=0.01))
    assert not decision.approved
    assert RiskRejectionCode.RISK_PER_TRADE_LIMIT in decision.codes


def test_max_positions(tmp_path: Path) -> None:
    decision = _decide(
        tmp_path,
        _policy(max_simultaneous_positions=1),
        account=_account(position_shares={"QQQ": 5}, position_notional_usd={"QQQ": 500.0}),
    )
    assert not decision.approved
    assert RiskRejectionCode.MAX_POSITIONS in decision.codes


def test_max_open_orders(tmp_path: Path) -> None:
    decision = _decide(
        tmp_path,
        _policy(max_open_orders=2),
        account=_account(open_order_count=2),
    )
    assert not decision.approved
    assert RiskRejectionCode.MAX_OPEN_ORDERS in decision.codes


def test_weekly_loss_limit(tmp_path: Path) -> None:
    decision = _decide(
        tmp_path,
        _policy(max_weekly_loss_usd=100),
        account=_account(realized_pnl_week_usd=-150.0),
    )
    assert not decision.approved
    assert RiskRejectionCode.WEEKLY_LOSS_LIMIT in decision.codes


def test_drawdown_limit(tmp_path: Path) -> None:
    # Equity 3000 off a 4000 peak = 25% drawdown >= the 20% cap.
    decision = _decide(
        tmp_path,
        _policy(max_drawdown_fraction=0.20),
        account=_account(peak_equity_usd=4000.0),
    )
    assert not decision.approved
    assert RiskRejectionCode.DRAWDOWN_LIMIT in decision.codes


def test_consecutive_losses(tmp_path: Path) -> None:
    decision = _decide(
        tmp_path,
        _policy(max_consecutive_losses=3),
        account=_account(consecutive_losses=3),
    )
    assert not decision.approved
    assert RiskRejectionCode.CONSECUTIVE_LOSSES in decision.codes


def test_price_deviation(tmp_path: Path) -> None:
    # Limit 100 vs last 90 = 11.1% deviation > 5% cap.
    decision = _decide(
        tmp_path,
        _policy(max_price_deviation_fraction=0.05),
        market=_market(last=90.0),
    )
    assert not decision.approved
    assert RiskRejectionCode.PRICE_DEVIATION in decision.codes


def test_pyramiding_forbidden(tmp_path: Path) -> None:
    # Already long SPY; adding without allow_pyramiding must deny. Position and
    # symbol-exposure caps are opened so only the pyramiding rule binds.
    decision = _decide(
        tmp_path,
        _policy(),
        account=_account(position_shares={"SPY": 5}, position_notional_usd={"SPY": 500.0}),
    )
    assert not decision.approved
    assert RiskRejectionCode.PYRAMIDING_FORBIDDEN in decision.codes


def test_each_denial_is_attributable(tmp_path: Path) -> None:
    # The baseline approves, so every test above proves its OWN check fired:
    # if the named check were deleted, the breach scenario would be approved
    # (or denied under a different code) and the assertion would fail.
    decision = _decide(tmp_path, _policy())
    assert decision.approved and decision.codes == ()

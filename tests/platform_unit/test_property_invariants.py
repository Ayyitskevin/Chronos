"""Property-based tests (hypothesis) for the load-bearing safety invariants.

These complement the example-based tests: instead of a handful of hand-picked
cases, hypothesis searches the input space for a counterexample to a stated
invariant. Four invariants are pinned here:

1. Intent identity — economically identical intents share an id; any change to
   an economic field changes it (the duplicate-suppression key must not alias).
2. State-machine legality — the machine never *accepts* an illegal transition,
   and terminal states are absorbing.
3. Sizer bounds — a produced entry never exceeds its cash/equity budget, is a
   positive whole number of shares, and carries a stop strictly below the limit.
4. Risk-engine deny-monotonicity — tightening any limit never turns a denial
   into an approval (a stricter policy approves a subset of a looser one).
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.control.halt import HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderSide
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.state_machine import (
    _ALLOWED,
    _TERMINAL,
    OrderStateMachine,
    OrderTransitionError,
)
from chronos.portfolio.sizer import PortfolioPolicy, convert_proposal
from chronos.risk.engine import AccountView, MarketViewEntry, RiskEngine
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import ProposalDirection, StrategyProposal

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def _intent(
    *,
    strategy_id: str = "s",
    strategy_version: str = "1",
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    limit_price: str = "500.00",
    stop_price: str | None = "480.00",
    tif: TimeInForce = TimeInForce.DAY,
    bar_id: str = "test:SPY:1d:2024-06-03",
) -> OrderIntent:
    return OrderIntent(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        side=side,
        quantity=quantity,
        limit_price=Decimal(limit_price),
        stop_price=Decimal(stop_price) if stop_price is not None else None,
        time_in_force=tif,
        decision_timestamp_utc=NOW,
        source_bar_sequence_id=bar_id,
        proposal_reason="test",
    )


# --------------------------------------------------------------------------- #
# 1. Intent identity
# --------------------------------------------------------------------------- #


@given(
    reason=st.text(max_size=20),
    minute=st.integers(min_value=0, max_value=59),
)
@settings(max_examples=100)
def test_intent_id_ignores_non_economic_fields(reason: str, minute: int) -> None:
    # decision timestamp and proposal_reason are NOT part of the identity: two
    # intents with identical economic content are the same order.
    base = _intent()
    other = OrderIntent(
        strategy_id="s",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=Decimal("500.00"),
        stop_price=Decimal("480.00"),
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=datetime(2024, 6, 3, 21, minute, tzinfo=UTC),
        source_bar_sequence_id="test:SPY:1d:2024-06-03",
        proposal_reason=reason,
    )
    assert base.intent_id == other.intent_id


@given(
    quantity=st.integers(min_value=1, max_value=10_000),
    limit=st.integers(min_value=1, max_value=100_000),
    symbol=st.sampled_from(["SPY", "QQQ", "IWM"]),
    side=st.sampled_from(list(OrderSide)),
)
@settings(max_examples=200)
def test_intent_id_changes_with_any_economic_field(
    quantity: int, limit: int, symbol: str, side: OrderSide
) -> None:
    # A reference intent that differs from the generated one in at least one
    # economic field must never share its id.
    reference = _intent(symbol="SPY", side=OrderSide.BUY, quantity=10, limit_price="500")
    candidate = _intent(
        symbol=symbol,
        side=side,
        quantity=quantity,
        limit_price=str(limit),
    )
    economically_same = (
        symbol == "SPY" and side is OrderSide.BUY and quantity == 10 and limit == 500
    )
    if economically_same:
        assert candidate.intent_id == reference.intent_id
    else:
        assert candidate.intent_id != reference.intent_id


# --------------------------------------------------------------------------- #
# 2. State-machine legality
# --------------------------------------------------------------------------- #


@given(targets=st.lists(st.sampled_from(list(IntentStatus)), min_size=1, max_size=25))
@settings(max_examples=300)
def test_state_machine_never_accepts_illegal_transition(targets: list[IntentStatus]) -> None:
    machine = OrderStateMachine(intent_id="x")
    for target in targets:
        source = machine.status
        try:
            changed = machine.apply(target, at_utc=NOW, evidence="e")
        except OrderTransitionError:
            # Illegal transitions must raise, never silently mutate state.
            assert machine.status == source
            continue
        if changed:
            # Every *accepted* transition must be in the legal table.
            assert target in _ALLOWED.get(source, frozenset())


@given(terminal=st.sampled_from(sorted(_TERMINAL)), target=st.sampled_from(list(IntentStatus)))
@settings(max_examples=100)
def test_terminal_states_are_absorbing(terminal: IntentStatus, target: IntentStatus) -> None:
    machine = OrderStateMachine(intent_id="x", status=terminal)
    if target is terminal:
        assert machine.apply(target, at_utc=NOW, evidence="e") is False
    else:
        try:
            machine.apply(target, at_utc=NOW, evidence="e")
        except OrderTransitionError:
            pass
        else:  # pragma: no cover - a leak here is the failure we are guarding
            raise AssertionError(f"terminal {terminal} accepted transition to {target}")
        assert machine.status is terminal


# --------------------------------------------------------------------------- #
# 3. Sizer bounds
# --------------------------------------------------------------------------- #


@given(
    equity=st.integers(min_value=1, max_value=10_000_000),
    cash=st.integers(min_value=1, max_value=10_000_000),
    price=st.integers(min_value=1, max_value=100_000),
    exposure=st.floats(min_value=0.01, max_value=1.0),
    stop_fraction=st.floats(min_value=0.01, max_value=0.9),
    max_fraction=st.floats(min_value=0.01, max_value=1.0),
)
@settings(max_examples=300)
def test_sizer_entry_never_exceeds_budget(
    equity: int,
    cash: int,
    price: int,
    exposure: float,
    stop_fraction: float,
    max_fraction: float,
) -> None:
    proposal = StrategyProposal(
        strategy_id="s",
        strategy_version="1",
        timestamp_utc=NOW,
        symbol="SPY",
        direction=ProposalDirection.ENTER_LONG,
        desired_exposure_fraction=exposure,
        stop_fraction=stop_fraction,
        source_bar_ids=("test:SPY:1d:2024-06-03",),
        reason="test",
    )
    conversion = convert_proposal(
        proposal,
        policy=PortfolioPolicy(max_fraction_per_position=max_fraction),
        equity_usd=Decimal(equity),
        cash_usd=Decimal(cash),
        held_shares=0,
        reference_price=Decimal(price),
    )
    intent = conversion.intent
    if intent is None:
        return  # a skipped conversion (e.g. budget buys no whole share) is fine
    capped = Decimal(str(min(exposure, max_fraction)))
    budget = min(Decimal(equity) * capped, Decimal(cash))
    assert intent.side is OrderSide.BUY
    assert intent.quantity >= 1
    # notional never exceeds the budget (whole-share floor rounding is downward)
    assert intent.limit_price * intent.quantity <= budget
    # a stop is always present and strictly below the limit
    assert intent.stop_price is not None
    assert intent.stop_price < intent.limit_price


# --------------------------------------------------------------------------- #
# 4. Risk-engine deny-monotonicity
# --------------------------------------------------------------------------- #

_TMP_HALT = HaltStore(Path(tempfile.mkdtemp()) / "halt.json")
_TMP_HALT.rearm("property test arm")
_ARMED_HALT = _TMP_HALT.read()

# Numeric limits where a *smaller* value is always at least as strict (denies at
# least as much) for a single validation. cooldown is intentionally excluded.
_MONOTONE_LIMIT_FIELDS = (
    "max_bot_capital_usd",
    "max_position_notional_usd",
    "max_aggregate_exposure_usd",
    "max_symbol_exposure_fraction",
    "max_risk_per_trade_fraction",
    "max_simultaneous_positions",
    "max_open_orders",
    "max_daily_loss_usd",
    "max_weekly_loss_usd",
    "max_drawdown_fraction",
    "max_consecutive_losses",
    "max_price_deviation_fraction",
    "max_order_rejections_per_day",
)


def _sim_lock() -> object:
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


def _healthy_account() -> AccountView:
    return AccountView(
        account_equity_usd=3000.0,
        cash_usd=3000.0,
        position_shares={},
        position_notional_usd={},
        open_order_count=0,
        realized_pnl_today_usd=0.0,
        realized_pnl_week_usd=0.0,
        peak_equity_usd=3000.0,
        consecutive_losses=0,
        as_of_utc=NOW,
    )


def _policy(limits: dict[str, float]) -> RiskPolicy:
    payload: dict[str, object] = {
        "policy_version": "prop",
        "allowed_symbols": ("SPY",),
        "allowed_strategy_ids": ("s",),
        "allow_long_entries": True,
        "allow_overnight_positions": True,
        # freshness kept generous and equal in both policies so denials come
        # from the exposure/loss limits under test, not from staleness.
        "max_quote_age_seconds": 3600,
        "max_bar_age_seconds": 5 * 86400,
    }
    payload.update(limits)
    return RiskPolicy.model_validate(payload)


@given(
    loose=st.fixed_dictionaries(
        {
            "max_bot_capital_usd": st.integers(min_value=0, max_value=10_000),
            "max_position_notional_usd": st.integers(min_value=0, max_value=10_000),
            "max_aggregate_exposure_usd": st.integers(min_value=0, max_value=10_000),
            "max_symbol_exposure_fraction": st.floats(min_value=0.0, max_value=1.0),
            "max_risk_per_trade_fraction": st.floats(min_value=0.0, max_value=1.0),
            "max_simultaneous_positions": st.integers(min_value=0, max_value=5),
            "max_open_orders": st.integers(min_value=0, max_value=5),
            "max_daily_loss_usd": st.integers(min_value=0, max_value=1_000),
            "max_weekly_loss_usd": st.integers(min_value=0, max_value=2_000),
            "max_drawdown_fraction": st.floats(min_value=0.0, max_value=1.0),
            "max_consecutive_losses": st.integers(min_value=0, max_value=10),
            "max_price_deviation_fraction": st.floats(min_value=0.0, max_value=0.5),
            "max_order_rejections_per_day": st.integers(min_value=0, max_value=10),
        }
    ),
    scales=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=13, max_size=13),
    quantity=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=250, deadline=None)
def test_tightening_a_limit_never_flips_deny_to_approve(
    loose: dict[str, float], scales: list[float], quantity: int
) -> None:
    strict = dict(loose)
    for field_name, scale in zip(_MONOTONE_LIMIT_FIELDS, scales, strict=True):
        strict[field_name] = type(loose[field_name])(loose[field_name] * scale)

    intent = _intent(quantity=quantity, limit_price="100.00", stop_price="95.00")
    market = MarketViewEntry(last_price=100.0, bar_close_utc=NOW, quote_utc=NOW)

    def decide(limits: dict[str, float]) -> bool:
        engine = RiskEngine(_policy(limits))
        return engine.validate(
            intent,
            account=_healthy_account(),
            market=market,
            halt=_ARMED_HALT,
            mode_lock=_sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        ).approved

    loose_approved = decide(loose)
    strict_approved = decide(strict)
    # The invariant: if the looser policy denied, the stricter one must deny too
    # (equivalently, approval is downward-closed under tightening).
    if not loose_approved:
        assert not strict_approved


def test_tightening_notional_below_intent_denies() -> None:
    # A concrete, high-signal instance: an approved entry becomes denied the
    # moment the notional cap drops below the intent's notional.
    intent = _intent(quantity=10, limit_price="100.00", stop_price="95.00")  # notional 1000
    market = MarketViewEntry(last_price=100.0, bar_close_utc=NOW, quote_utc=NOW)
    generous = {
        "max_bot_capital_usd": 5000,
        "max_position_notional_usd": 5000,
        "max_aggregate_exposure_usd": 5000,
        "max_symbol_exposure_fraction": 1.0,
        "max_risk_per_trade_fraction": 1.0,
        "max_simultaneous_positions": 5,
        "max_open_orders": 5,
        "max_daily_loss_usd": 1000,
        "max_weekly_loss_usd": 2000,
        "max_drawdown_fraction": 1.0,
        "max_consecutive_losses": 10,
        "max_price_deviation_fraction": 0.5,
        "max_order_rejections_per_day": 10,
    }

    def approved(limits: dict[str, float]) -> bool:
        return (
            RiskEngine(_policy(limits))
            .validate(
                intent,
                account=_healthy_account(),
                market=market,
                halt=_ARMED_HALT,
                mode_lock=_sim_lock(),  # type: ignore[arg-type]
                now_utc=NOW,
            )
            .approved
        )

    assert approved(generous) is True
    tightened = dict(generous)
    tightened["max_position_notional_usd"] = 500  # below the 1000 notional
    assert approved(tightened) is False

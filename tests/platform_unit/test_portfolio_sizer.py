"""Proposal-to-intent conversion: sizing, rounding, stops, and skip reasons."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from chronos.domain.enums import OrderSide
from chronos.execution.intents import TimeInForce
from chronos.portfolio.sizer import PortfolioPolicy, convert_proposal
from chronos.strategies.base import ProposalDirection, StrategyProposal

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_proposal(**overrides: object) -> StrategyProposal:
    payload: dict[str, object] = {
        "strategy_id": "strat_a",
        "strategy_version": "1",
        "timestamp_utc": NOW,
        "symbol": "SPY",
        "direction": ProposalDirection.ENTER_LONG,
        "desired_exposure_fraction": 0.5,
        "stop_fraction": 0.05,
        "source_bar_ids": ("test:SPY:1d:2024-06-03",),
        "reason": "test",
    }
    payload.update(overrides)
    return StrategyProposal(**payload)  # type: ignore[arg-type]


POLICY = PortfolioPolicy(max_fraction_per_position=0.95)


class TestEntrySizing:
    def test_whole_share_floor_and_limit_and_stop(self) -> None:
        # budget = min(10000 * 0.5, 10000) = 5000
        # limit  = 100 * 1.001 = 100.10 (already on tick)
        # qty    = floor(5000 / 100.10) = floor(49.95...) = 49
        # stop   = 100.10 * (1 - 0.05) = 95.095 -> HALF_UP to tick -> 95.10
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        intent = conversion.intent
        assert intent is not None
        assert intent.side is OrderSide.BUY
        assert intent.quantity == 49
        assert intent.limit_price == Decimal("100.10")
        assert intent.stop_price == Decimal("95.10")
        assert intent.time_in_force is TimeInForce.DAY

    def test_budget_is_min_of_cash_and_equity_fraction(self) -> None:
        # cash 1000 < equity*fraction 5000 -> budget = 1000
        # qty = floor(1000 / 100.10) = floor(9.99...) = 9
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("1000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is not None
        assert conversion.intent.quantity == 9

    def test_policy_cap_overrides_greedy_proposal(self) -> None:
        # desired 1.0 capped at policy 0.25 -> budget = 2500 -> qty 24
        policy = PortfolioPolicy(max_fraction_per_position=0.25)
        conversion = convert_proposal(
            make_proposal(desired_exposure_fraction=1.0),
            policy=policy,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is not None
        assert conversion.intent.quantity == 24  # floor(2500 / 100.10)

    def test_entry_limit_rounds_to_tick_half_up(self) -> None:
        # 123.456 * 1.001 = 123.579456 -> 123.58
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("100000"),
            cash_usd=Decimal("100000"),
            held_shares=0,
            reference_price=Decimal("123.456"),
        )
        assert conversion.intent is not None
        assert conversion.intent.limit_price == Decimal("123.58")

    def test_zero_quantity_budget_is_skipped(self) -> None:
        # budget = min(10000*0.5, 50) = 50 < one share at 100.10
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("50"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is None
        assert "no whole share" in conversion.skipped_reason

    def test_missing_stop_fraction_is_skipped(self) -> None:
        conversion = convert_proposal(
            make_proposal(stop_fraction=None),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is None
        assert "stop fraction" in conversion.skipped_reason

    def test_pyramiding_attempt_is_skipped(self) -> None:
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=10,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is None
        assert "pyramiding" in conversion.skipped_reason


class TestExitsAndHold:
    def test_exit_sells_exactly_held_shares(self) -> None:
        # exit limit = 100 * (1 - 0.001) = 99.90
        conversion = convert_proposal(
            make_proposal(
                direction=ProposalDirection.EXIT_LONG,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
            ),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("100"),
            held_shares=42,
            reference_price=Decimal("100"),
        )
        intent = conversion.intent
        assert intent is not None
        assert intent.side is OrderSide.SELL
        assert intent.quantity == 42
        assert intent.limit_price == Decimal("99.90")
        assert intent.stop_price is None

    def test_exit_without_position_is_skipped(self) -> None:
        conversion = convert_proposal(
            make_proposal(
                direction=ProposalDirection.EXIT_LONG,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
            ),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is None
        assert "no position" in conversion.skipped_reason

    def test_hold_produces_no_intent(self) -> None:
        conversion = convert_proposal(
            make_proposal(
                direction=ProposalDirection.HOLD,
                desired_exposure_fraction=0.0,
                stop_fraction=None,
            ),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("100"),
        )
        assert conversion.intent is None
        assert "HOLD" in conversion.skipped_reason

    def test_non_positive_reference_price_is_skipped(self) -> None:
        conversion = convert_proposal(
            make_proposal(),
            policy=POLICY,
            equity_usd=Decimal("10000"),
            cash_usd=Decimal("10000"),
            held_shares=0,
            reference_price=Decimal("0"),
        )
        assert conversion.intent is None
        assert "not positive" in conversion.skipped_reason

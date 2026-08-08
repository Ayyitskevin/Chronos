from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chronos.research.five_tool.planning import ExitReason, LegId, PositionSide
from chronos.research.five_tool.validation import (
    ClosedLeg,
    ProfitFactorState,
    ValidationInputError,
    aggregate_economic_positions,
    build_validation_report,
    chronological_oos_legs,
    pine_equal_count_closed_leg_dashboard,
    profit_factor,
)

BASE = datetime(2025, 1, 1, 21, tzinfo=UTC)


def _leg(
    position: str,
    leg: LegId,
    *,
    day: int,
    gross: float,
    side: PositionSide = PositionSide.LONG,
    instrument: str = "SPY",
    regime: str = "bull",
    variant: str = "base",
    commission: float = 0.0,
    slippage: float = 0.0,
    turnover: float = 1000.0,
) -> ClosedLeg:
    exit_time = BASE + timedelta(days=day)
    return ClosedLeg(
        position_id=position,
        leg_id=leg,
        side=side,
        instrument=instrument,
        regime=regime,
        parameter_variant=variant,
        entry_time_utc=exit_time - timedelta(days=2),
        exit_time_utc=exit_time,
        gross_pnl=gross,
        commission_cost=commission,
        slippage_cost=slippage,
        turnover=turnover,
        exit_reason=ExitReason.TARGET_1 if leg is LegId.LEG_1 else ExitReason.TARGET_2,
    )


def test_profit_factor_has_explicit_non_numeric_states() -> None:
    no_trades = profit_factor([])
    no_losses = profit_factor([2.0, 3.0, 0.0])
    no_gains = profit_factor([-2.0, -3.0, 0.0])
    finite = profit_factor([6.0, -2.0, -1.0])

    assert no_trades.state is ProfitFactorState.UNDEFINED_NO_TRADES
    assert no_trades.value is None
    assert no_losses.state is ProfitFactorState.UNBOUNDED_NO_LOSSES
    assert no_losses.value is None  # no infinity or Pine's 999 sentinel
    assert no_gains.state is ProfitFactorState.UNDEFINED_NO_GAINS
    assert no_gains.value == 0.0
    assert finite.state is ProfitFactorState.FINITE
    assert finite.value == 2.0


def test_three_closed_legs_are_one_economic_position() -> None:
    legs = [
        _leg("p1", LegId.LEG_3, day=4, gross=5.0),
        _leg("p1", LegId.LEG_1, day=2, gross=10.0),
        _leg("p1", LegId.LEG_2, day=3, gross=-2.0),
    ]
    positions = aggregate_economic_positions(legs)
    assert len(positions) == 1
    assert positions[0].leg_count == 3
    assert positions[0].gross_pnl == 13.0
    assert positions[0].exit_time_utc == BASE + timedelta(days=4)


def test_position_aggregation_rejects_duplicate_leg_and_side_switch() -> None:
    with pytest.raises(ValidationInputError, match="repeats"):
        aggregate_economic_positions(
            [
                _leg("p1", LegId.LEG_1, day=1, gross=1.0),
                _leg("p1", LegId.LEG_1, day=2, gross=2.0),
            ]
        )
    with pytest.raises(ValidationInputError, match="inconsistent"):
        aggregate_economic_positions(
            [
                _leg("p1", LegId.LEG_1, day=1, gross=1.0),
                _leg(
                    "p1",
                    LegId.LEG_2,
                    day=2,
                    gross=2.0,
                    side=PositionSide.SHORT,
                ),
            ]
        )


def test_oos_selection_uses_exit_time_and_is_chronological() -> None:
    before = _leg("before", LegId.LEG_1, day=2, gross=1.0)
    at_boundary = _leg("boundary", LegId.LEG_1, day=5, gross=2.0)
    after = _leg("after", LegId.LEG_1, day=8, gross=3.0)
    selected = chronological_oos_legs(
        [after, before, at_boundary], start_utc=BASE + timedelta(days=5)
    )
    assert [leg.position_id for leg in selected] == ["boundary", "after"]


def test_pine_dashboard_is_explicitly_a_closed_leg_heuristic() -> None:
    legs = [
        _leg(f"p{index}", LegId.LEG_1, day=index, gross=float(index - 2)) for index in range(1, 6)
    ]
    dashboard = pine_equal_count_closed_leg_dashboard(legs, segments=2)
    assert dashboard.label == "heuristic_equal_count_closed_legs_not_walk_forward"
    assert not dashboard.independent_observations
    assert [chunk.closed_legs for chunk in dashboard.chunks] == [3, 2]
    assert sum(chunk.net_pnl for chunk in dashboard.chunks) == sum(leg.net_pnl for leg in legs)


def test_full_report_covers_cost_risk_slices_sensitivity_and_removal() -> None:
    legs = [
        _leg(
            "p1",
            LegId.LEG_1,
            day=2,
            gross=60.0,
            commission=2.0,
            slippage=3.0,
            variant="base",
        ),
        _leg(
            "p1",
            LegId.LEG_2,
            day=3,
            gross=45.0,
            commission=1.0,
            slippage=1.0,
            variant="base",
        ),
        _leg(
            "p2",
            LegId.LEG_1,
            day=10,
            gross=-40.0,
            side=PositionSide.SHORT,
            instrument="QQQ",
            regime="bear",
            variant="loose",
            commission=2.0,
            slippage=3.0,
        ),
        _leg(
            "p3",
            LegId.LEG_1,
            day=35,
            gross=30.0,
            instrument="QQQ",
            regime="neutral",
            variant="tight",
            commission=1.0,
            slippage=1.0,
        ),
        _leg(
            "p4",
            LegId.LEG_1,
            day=40,
            gross=20.0,
            variant="base",
        ),
    ]
    neighbors = {"base": ("loose", "tight"), "loose": ("base",), "tight": ("base",)}
    report = build_validation_report(
        list(reversed(legs)),
        oos_start_utc=BASE + timedelta(days=5),
        initial_equity=1_000.0,
        cvar_alpha=0.5,
        minimum_economic_positions=5,
        parameter_neighbors=neighbors,
    )

    assert report.closed_leg_count == 5
    assert report.economic_position_count == 4
    assert report.chronological_position_ids == ("p1", "p2", "p3", "p4")
    assert report.oos.position_ids_in_exit_order == ("p2", "p3", "p4")
    assert report.costs.commission_cost == 6.0
    assert report.costs.slippage_cost == 8.0
    assert report.costs.turnover == 5_000.0
    assert report.costs.net_pnl == 101.0
    assert report.risk.max_drawdown_usd == 45.0
    assert report.risk.max_drawdown_fraction_of_initial_equity == 0.045
    assert report.risk.cvar_tail_observations == 2
    assert report.risk.cvar_tail_mean_pnl_usd == pytest.approx(-12.5)
    assert report.concentration.largest_exit_month_abs_pnl_share == pytest.approx(143.0 / 191.0)
    assert [item.key for item in report.regime_evidence] == ["bear", "bull", "neutral"]
    assert [item.key for item in report.instrument_evidence] == ["QQQ", "SPY"]
    assert [item.key for item in report.parameter_sensitivity.variants] == [
        "base",
        "loose",
        "tight",
    ]
    assert not report.parameter_sensitivity.plateau_supported
    assert report.best_trade_removal.removed_key == "p1"
    assert report.best_trade_removal.net_pnl_after_removal == 3.0
    assert report.best_month_removal.removed_key == "2025-01"
    assert report.low_sample
    assert any("closed legs are not independent" in warning for warning in report.warnings)

    same_report = build_validation_report(
        legs,
        oos_start_utc=BASE + timedelta(days=5),
        initial_equity=1_000.0,
        cvar_alpha=0.5,
        minimum_economic_positions=5,
        parameter_neighbors=neighbors,
    )
    assert report.digest == same_report.digest


def test_empty_report_fails_soft_without_inventing_zero_quality() -> None:
    report = build_validation_report(
        [],
        oos_start_utc=BASE,
        minimum_economic_positions=1,
    )
    assert report.closed_leg_count == report.economic_position_count == 0
    assert report.all_economic_positions_profit_factor.state is (
        ProfitFactorState.UNDEFINED_NO_TRADES
    )
    assert report.risk.max_drawdown_usd is None
    assert report.risk.cvar_tail_mean_pnl_usd is None
    assert report.concentration.dominant_position_id is None
    assert report.best_trade_removal.positive_after_removal is None
    assert report.low_sample


def test_removing_best_trade_removes_the_whole_economic_position_not_one_leg() -> None:
    report = build_validation_report(
        [
            _leg("multi", LegId.LEG_1, day=1, gross=40.0),
            _leg("multi", LegId.LEG_2, day=2, gross=30.0),
            _leg("single", LegId.LEG_1, day=3, gross=-20.0),
        ],
        oos_start_utc=BASE,
        minimum_economic_positions=1,
    )
    assert report.best_trade_removal.removed_key == "multi"
    assert report.best_trade_removal.removed_net_pnl == 70.0
    assert report.best_trade_removal.net_pnl_after_removal == -20.0


def test_parameter_plateau_can_only_pass_with_declared_positive_neighbors() -> None:
    legs = [
        _leg("a", LegId.LEG_1, day=1, gross=10.0, variant="center"),
        _leg("b", LegId.LEG_1, day=2, gross=2.0, variant="left"),
        _leg("c", LegId.LEG_1, day=3, gross=3.0, variant="right"),
    ]
    unassessed = build_validation_report(legs, oos_start_utc=BASE, minimum_economic_positions=1)
    assessed = build_validation_report(
        legs,
        oos_start_utc=BASE,
        minimum_economic_positions=1,
        parameter_neighbors={"center": ("left", "right")},
    )
    assert unassessed.parameter_sensitivity.plateau_supported is None
    assert assessed.parameter_sensitivity.plateau_supported is True


def test_naive_datetimes_and_nonfinite_pnl_are_rejected() -> None:
    with pytest.raises(ValidationInputError, match="timezone-aware"):
        chronological_oos_legs([], start_utc=BASE.replace(tzinfo=None))
    with pytest.raises(ValidationInputError, match="finite"):
        _leg("p1", LegId.LEG_1, day=1, gross=float("nan"))

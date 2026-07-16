from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from chronos.strategy.scenarios import (
    CoveredCallScenarioInput,
    ShortPutScenarioInput,
    covered_call_scenario,
    short_put_scenario,
)


def test_short_put_scenario_uses_qualified_standard_share_deliverable() -> None:
    result = short_put_scenario(
        ShortPutScenarioInput(
            strike=Decimal("50"),
            premium_per_share=Decimal("2.00"),
            multiplier=Decimal("10"),
            deliverable_shares=Decimal("10"),
            deliverable_verified=True,
            contracts=2,
            commission=Decimal("0.75"),
            commission_is_estimate=False,
            scenario_price=Decimal("40"),
            net_liquidation=Decimal("10000"),
            broker_margin_impact=Decimal("250"),
        )
    )

    assert result.assigned_shares == Decimal("20")
    assert result.gross_premium == Decimal("40.00")
    assert result.commission == Decimal("0.75")
    assert result.commission_is_estimate is False
    assert result.net_premium == Decimal("39.25")
    assert result.gross_assignment_obligation == Decimal("1000")
    assert result.net_cash_deployed_if_assigned == Decimal("960.75")
    assert result.effective_entry_price == Decimal("48.0375")
    assert result.expiration_pnl == Decimal("-160.75")
    assert result.cash_secured_notional_pct == Decimal("0.1")
    assert result.broker_margin_impact == Decimal("250")


def test_short_put_scenario_rejects_unverified_deliverable() -> None:
    with pytest.raises(ValidationError, match="deliverable_verified"):
        ShortPutScenarioInput(
            strike=Decimal("50"),
            premium_per_share=Decimal("2"),
            multiplier=Decimal("100"),
            deliverable_shares=Decimal("100"),
            deliverable_verified=False,  # type: ignore[arg-type]
            contracts=1,
            commission=Decimal("0"),
            scenario_price=Decimal("50"),
            net_liquidation=Decimal("100000"),
        )


@pytest.mark.parametrize(
    ("scenario_input", "message"),
    [
        (
            {
                "strike": Decimal("50"),
                "premium_per_share": Decimal("2"),
                "multiplier": Decimal("100"),
                "deliverable_shares": Decimal("99"),
                "deliverable_verified": True,
                "contracts": 1,
                "commission": Decimal("0"),
                "scenario_price": Decimal("50"),
                "net_liquidation": Decimal("100000"),
            },
            "Short-put scenarios support only verified standard deliverables",
        ),
        (
            {
                "strike": Decimal("50"),
                "premium_per_share": Decimal("2"),
                "multiplier": Decimal("100"),
                "deliverable_shares": Decimal("99"),
                "deliverable_verified": True,
                "contracts": 1,
                "commission": Decimal("0"),
                "scenario_price": Decimal("50"),
                "broker_average_cost": Decimal("40"),
                "strategy_adjusted_basis": Decimal("40"),
                "held_shares": Decimal("100"),
            },
            "Covered-call scenarios support only verified standard deliverables",
        ),
    ],
)
def test_scenario_inputs_reject_nonstandard_deliverables(
    scenario_input: dict[str, object],
    message: str,
) -> None:
    model = (
        ShortPutScenarioInput if "net_liquidation" in scenario_input else CoveredCallScenarioInput
    )
    with pytest.raises(ValidationError, match=message):
        model.model_validate(scenario_input)


def test_covered_call_scenario_rejects_unverified_deliverable() -> None:
    with pytest.raises(ValidationError, match="deliverable_verified"):
        CoveredCallScenarioInput(
            strike=Decimal("50"),
            premium_per_share=Decimal("2"),
            multiplier=Decimal("100"),
            deliverable_shares=Decimal("100"),
            deliverable_verified=False,  # type: ignore[arg-type]
            contracts=1,
            commission=Decimal("0"),
            scenario_price=Decimal("50"),
            broker_average_cost=Decimal("40"),
            strategy_adjusted_basis=Decimal("40"),
            held_shares=Decimal("100"),
        )


@pytest.mark.parametrize(
    "contracts",
    [True, 1.0, 1.5, Decimal("1"), Decimal("1.5")],
)
def test_scenario_inputs_and_helpers_reject_non_integer_contract_counts(
    contracts: object,
) -> None:
    short_values: dict[str, object] = {
        "strike": Decimal("50"),
        "premium_per_share": Decimal("2"),
        "multiplier": Decimal("100"),
        "deliverable_shares": Decimal("100"),
        "deliverable_verified": True,
        "contracts": contracts,
        "commission": Decimal("0"),
        "scenario_price": Decimal("50"),
        "net_liquidation": Decimal("100000"),
    }
    covered_values: dict[str, object] = {
        "strike": Decimal("50"),
        "premium_per_share": Decimal("2"),
        "multiplier": Decimal("100"),
        "deliverable_shares": Decimal("100"),
        "deliverable_verified": True,
        "contracts": contracts,
        "commission": Decimal("0"),
        "scenario_price": Decimal("50"),
        "broker_average_cost": Decimal("40"),
        "strategy_adjusted_basis": Decimal("40"),
        "held_shares": Decimal("100"),
    }

    with pytest.raises(ValidationError):
        ShortPutScenarioInput.model_validate(short_values)
    with pytest.raises(ValidationError):
        CoveredCallScenarioInput.model_validate(covered_values)

    valid_short = ShortPutScenarioInput.model_validate({**short_values, "contracts": 1})
    valid_covered = CoveredCallScenarioInput.model_validate({**covered_values, "contracts": 1})
    with pytest.raises(ValueError, match="positive integer"):
        short_put_scenario(valid_short.model_copy(update={"contracts": contracts}))
    with pytest.raises(ValueError, match="positive integer"):
        covered_call_scenario(valid_covered.model_copy(update={"contracts": contracts}))


@pytest.mark.parametrize("commission_is_estimate", [True, False])
def test_short_put_preserves_commission_estimate_status(
    commission_is_estimate: bool,
) -> None:
    result = short_put_scenario(
        ShortPutScenarioInput(
            strike=Decimal("25"),
            premium_per_share=Decimal("1"),
            multiplier=Decimal("25"),
            deliverable_shares=Decimal("25"),
            deliverable_verified=True,
            contracts=1,
            commission=Decimal("0.50"),
            commission_is_estimate=commission_is_estimate,
            scenario_price=Decimal("25"),
            net_liquidation=Decimal("5000"),
        )
    )

    assert result.commission == Decimal("0.50")
    assert result.commission_is_estimate is commission_is_estimate
    assert result.net_premium == Decimal("24.50")


def test_covered_call_scenario_uses_exact_spec_formulas() -> None:
    result = covered_call_scenario(
        CoveredCallScenarioInput(
            strike=Decimal("120"),
            premium_per_share=Decimal("1.50"),
            multiplier=Decimal("50"),
            deliverable_shares=Decimal("50"),
            deliverable_verified=True,
            contracts=2,
            commission=Decimal("1.00"),
            commission_is_estimate=True,
            scenario_price=Decimal("80"),
            broker_average_cost=Decimal("90"),
            strategy_adjusted_basis=Decimal("95"),
            held_shares=Decimal("175"),
            already_encumbered_shares=Decimal("50"),
            broker_margin_impact=Decimal("125"),
        )
    )

    assert result.gross_premium == Decimal("150.00")
    assert result.commission == Decimal("1.00")
    assert result.commission_is_estimate is True
    assert result.net_premium == Decimal("149.00")
    assert result.shares_covered == Decimal("100")
    assert result.remaining_unencumbered_shares == Decimal("25")
    assert result.called_away_proceeds == Decimal("12000")
    assert result.stock_pnl_vs_broker_average_cost == Decimal("3000")
    assert result.total_cycle_pnl_vs_strategy_adjusted_basis == Decimal("2649.00")
    assert result.strategy_adjusted_basis_after_premium == Decimal("93.51")
    assert result.upside_forfeited == Decimal("0")
    assert result.downside_stock_exposure_after_premium == Decimal("1351.00")
    assert result.broker_margin_impact == Decimal("125")


def test_covered_call_allows_negative_strategy_adjusted_basis() -> None:
    result = covered_call_scenario(
        CoveredCallScenarioInput(
            strike=Decimal("10"),
            premium_per_share=Decimal("1"),
            multiplier=Decimal("100"),
            deliverable_shares=Decimal("100"),
            deliverable_verified=True,
            contracts=1,
            commission=Decimal("0"),
            scenario_price=Decimal("0"),
            broker_average_cost=Decimal("5"),
            strategy_adjusted_basis=Decimal("-2"),
            held_shares=Decimal("100"),
        )
    )

    assert result.total_cycle_pnl_vs_strategy_adjusted_basis == Decimal("1300")
    assert result.strategy_adjusted_basis_after_premium == Decimal("-3")
    assert result.downside_stock_exposure_after_premium == Decimal("-100")


def test_covered_call_upside_and_retained_premium_are_not_clamped() -> None:
    result = covered_call_scenario(
        CoveredCallScenarioInput(
            strike=Decimal("120"),
            premium_per_share=Decimal("1.50"),
            multiplier=Decimal("50"),
            deliverable_shares=Decimal("50"),
            deliverable_verified=True,
            contracts=2,
            commission=Decimal("1.00"),
            scenario_price=Decimal("135"),
            broker_average_cost=Decimal("90"),
            strategy_adjusted_basis=Decimal("95"),
            held_shares=Decimal("100"),
        )
    )

    assert result.upside_forfeited == Decimal("1500")
    assert result.downside_stock_exposure_after_premium == Decimal("-149.00")


def test_covered_call_allows_exact_coverage_boundary() -> None:
    result = covered_call_scenario(
        CoveredCallScenarioInput(
            strike=Decimal("120"),
            premium_per_share=Decimal("1"),
            multiplier=Decimal("50"),
            deliverable_shares=Decimal("50"),
            deliverable_verified=True,
            contracts=2,
            commission=Decimal("0"),
            scenario_price=Decimal("120"),
            broker_average_cost=Decimal("90"),
            strategy_adjusted_basis=Decimal("95"),
            held_shares=Decimal("150"),
            already_encumbered_shares=Decimal("50"),
        )
    )

    assert result.shares_covered == Decimal("100")
    assert result.remaining_unencumbered_shares == Decimal("0")


def test_covered_call_rejects_any_naked_exposure() -> None:
    with pytest.raises(ValidationError, match="naked-call exposure"):
        CoveredCallScenarioInput(
            strike=Decimal("120"),
            premium_per_share=Decimal("1"),
            multiplier=Decimal("50"),
            deliverable_shares=Decimal("50"),
            deliverable_verified=True,
            contracts=2,
            commission=Decimal("0"),
            scenario_price=Decimal("120"),
            broker_average_cost=Decimal("90"),
            strategy_adjusted_basis=Decimal("95"),
            held_shares=Decimal("149.99"),
            already_encumbered_shares=Decimal("50"),
        )


def test_covered_call_coverage_check_is_independent_of_ambient_precision() -> None:
    with localcontext() as ambient:
        ambient.prec = 6
        with pytest.raises(ValidationError, match="naked-call exposure"):
            CoveredCallScenarioInput(
                strike=Decimal("120"),
                premium_per_share=Decimal("1"),
                multiplier=Decimal("100.0001"),
                deliverable_shares=Decimal("100.0001"),
                deliverable_verified=True,
                contracts=1,
                commission=Decimal("0"),
                scenario_price=Decimal("120"),
                broker_average_cost=Decimal("90"),
                strategy_adjusted_basis=Decimal("95"),
                held_shares=Decimal("200"),
                already_encumbered_shares=Decimal("100"),
            )


def test_more_than_fifty_digit_one_unit_naked_exposure_is_rejected() -> None:
    held_shares = Decimal("5" + "0" * 60)
    with localcontext() as ambient:
        ambient.prec = 6
        with pytest.raises(ValidationError, match="naked-call exposure"):
            CoveredCallScenarioInput(
                strike=Decimal("1"),
                premium_per_share=Decimal("0"),
                multiplier=Decimal("1"),
                deliverable_shares=Decimal("1"),
                deliverable_verified=True,
                contracts=1,
                commission=Decimal("0"),
                scenario_price=Decimal("1"),
                broker_average_cost=Decimal("1"),
                strategy_adjusted_basis=Decimal("1"),
                held_shares=held_shares,
                already_encumbered_shares=held_shares,
            )


def test_scenario_formulas_are_stable_at_low_ambient_precision() -> None:
    short_values = ShortPutScenarioInput(
        strike=Decimal("123.4567"),
        premium_per_share=Decimal("1.234567"),
        multiplier=Decimal("100"),
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
        contracts=3,
        commission=Decimal("0.65"),
        scenario_price=Decimal("100.1234"),
        net_liquidation=Decimal("100000.01"),
    )
    covered_values = CoveredCallScenarioInput(
        strike=Decimal("123.4567"),
        premium_per_share=Decimal("1.234567"),
        multiplier=Decimal("100"),
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
        contracts=2,
        commission=Decimal("0.65"),
        scenario_price=Decimal("150.9876"),
        broker_average_cost=Decimal("98.7654"),
        strategy_adjusted_basis=Decimal("97.6543"),
        held_shares=Decimal("300"),
        already_encumbered_shares=Decimal("50"),
    )
    expected_short = short_put_scenario(short_values)
    expected_covered = covered_call_scenario(covered_values)

    with localcontext() as ambient:
        ambient.prec = 6
        actual_short = short_put_scenario(short_values)
        actual_covered = covered_call_scenario(covered_values)

    assert actual_short == expected_short
    assert actual_covered == expected_covered

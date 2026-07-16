from decimal import Decimal

import pytest

from chronos.domain.enums import AssignmentPressure, OptionRight
from chronos.strategy.assignment_pressure import (
    ASSIGNMENT_HEURISTIC_NOTICE,
    AssignmentPressureInput,
    AssignmentPressureResult,
    assess_assignment_pressure,
)


def _assess(**overrides: object) -> AssignmentPressureResult:
    values: dict[str, object] = {
        "right": OptionRight.CALL,
        "stock_price": Decimal("100"),
        "strike": Decimal("110"),
        "dte": 10,
        "delta": Decimal("0.20"),
        "remaining_extrinsic_value": Decimal("0.20"),
    }
    values.update(overrides)
    return assess_assignment_pressure(AssignmentPressureInput.model_validate(values))


def test_otm_with_meaningful_extrinsic_and_more_than_five_dte_is_low() -> None:
    result = _assess(dte=6, remaining_extrinsic_value=Decimal("0.10"))

    assert result.status is AssignmentPressure.LOW
    assert result.is_itm is False
    assert result.fired_rules == ("otm_with_time_and_extrinsic",)
    assert result.notice == ASSIGNMENT_HEURISTIC_NOTICE
    assert "not an assignment probability" in result.notice


@pytest.mark.parametrize(
    ("overrides", "expected_rule"),
    [
        (
            {
                "stock_price": Decimal("111"),
                "dte": 30,
                "delta": Decimal("0.20"),
            },
            "in_the_money",
        ),
        (
            {"dte": 30, "delta": Decimal("0.50")},
            "elevated_absolute_delta",
        ),
        (
            {"dte": 5, "delta": Decimal("0.20")},
            "near_expiration",
        ),
    ],
)
def test_elevated_rules_include_their_literal_boundaries(
    overrides: dict[str, object],
    expected_rule: str,
) -> None:
    result = _assess(**overrides)

    assert result.status is AssignmentPressure.ELEVATED
    assert expected_rule in result.fired_rules


def test_itm_at_three_dte_is_high() -> None:
    result = _assess(
        stock_price=Decimal("111"),
        dte=3,
        remaining_extrinsic_value=Decimal("0.20"),
    )

    assert result.status is AssignmentPressure.HIGH
    assert result.fired_rules == ("itm_near_expiration",)


def test_itm_after_three_dte_falls_to_elevated() -> None:
    result = _assess(
        stock_price=Decimal("111"),
        dte=4,
        remaining_extrinsic_value=Decimal("0.20"),
    )

    assert result.status is AssignmentPressure.ELEVATED
    assert "in_the_money" in result.fired_rules


def test_near_zero_extrinsic_is_a_literal_independent_high_rule() -> None:
    result = _assess(
        stock_price=Decimal("100"),
        strike=Decimal("150"),
        dte=30,
        delta=Decimal("0.05"),
        remaining_extrinsic_value=Decimal("0.05"),
    )

    assert result.is_itm is False
    assert result.status is AssignmentPressure.HIGH
    assert result.fired_rules == ("near_zero_extrinsic",)


def test_put_itm_direction_uses_stock_below_strike() -> None:
    result = _assess(
        right=OptionRight.PUT,
        stock_price=Decimal("99"),
        strike=Decimal("100"),
        dte=3,
        delta=Decimal("-0.60"),
        remaining_extrinsic_value=Decimal("0.20"),
    )

    assert result.is_itm is True
    assert result.status is AssignmentPressure.HIGH


@pytest.mark.parametrize(
    ("dividend_data_reliable", "expected_status", "expect_dividend_rule"),
    [
        (True, AssignmentPressure.HIGH, True),
        (False, AssignmentPressure.ELEVATED, False),
    ],
)
def test_dividend_escalation_requires_reliable_data(
    dividend_data_reliable: bool,
    expected_status: AssignmentPressure,
    expect_dividend_rule: bool,
) -> None:
    result = _assess(
        stock_price=Decimal("111"),
        strike=Decimal("110"),
        dte=30,
        remaining_extrinsic_value=Decimal("0.50"),
        dividend_data_reliable=dividend_data_reliable,
        expected_dividend=Decimal("1.00"),
        days_to_ex_dividend=5,
    )

    assert result.status is expected_status
    assert ("reliable_dividend_exceeds_extrinsic" in result.fired_rules) is expect_dividend_rule


def test_dividend_must_strictly_exceed_extrinsic_to_escalate() -> None:
    result = _assess(
        stock_price=Decimal("111"),
        strike=Decimal("110"),
        dte=30,
        remaining_extrinsic_value=Decimal("1.00"),
        dividend_data_reliable=True,
        expected_dividend=Decimal("1.00"),
        days_to_ex_dividend=5,
    )

    assert result.status is AssignmentPressure.ELEVATED
    assert "reliable_dividend_exceeds_extrinsic" not in result.fired_rules


@pytest.mark.parametrize(
    ("days_to_ex_dividend", "expected_status", "expect_dividend_rule"),
    [
        (0, AssignmentPressure.HIGH, True),
        (4, AssignmentPressure.HIGH, True),
        (5, AssignmentPressure.ELEVATED, False),
    ],
)
def test_dividend_escalation_requires_ex_dividend_date_on_or_before_expiration(
    days_to_ex_dividend: int,
    expected_status: AssignmentPressure,
    expect_dividend_rule: bool,
) -> None:
    result = _assess(
        stock_price=Decimal("111"),
        strike=Decimal("110"),
        dte=4,
        remaining_extrinsic_value=Decimal("0.50"),
        dividend_data_reliable=True,
        expected_dividend=Decimal("1.00"),
        days_to_ex_dividend=days_to_ex_dividend,
    )

    assert result.status is expected_status
    assert ("reliable_dividend_exceeds_extrinsic" in result.fired_rules) is expect_dividend_rule


@pytest.mark.parametrize("missing_field", ["stock_price", "strike", "dte"])
def test_missing_required_input_is_unknown(missing_field: str) -> None:
    result = _assess(**{missing_field: None})

    assert result.status is AssignmentPressure.UNKNOWN
    assert result.is_itm is None
    assert result.fired_rules == ("required_inputs_missing",)


def test_missing_extrinsic_can_leave_an_otm_contract_unknown() -> None:
    result = _assess(remaining_extrinsic_value=None)

    assert result.status is AssignmentPressure.UNKNOWN
    assert result.is_itm is False
    assert result.fired_rules == ("insufficient_evidence_for_category",)


def test_corporate_action_warning_forces_unknown() -> None:
    result = _assess(corporate_action_warning=True)

    assert result.status is AssignmentPressure.UNKNOWN
    assert result.is_itm is None
    assert result.fired_rules == ("corporate_action_warning",)


def test_borrow_warning_escalates_otherwise_low_pressure() -> None:
    result = _assess(borrow_warning=True)

    assert result.status is AssignmentPressure.ELEVATED
    assert result.fired_rules == (
        "otm_with_time_and_extrinsic",
        "borrow_warning",
    )

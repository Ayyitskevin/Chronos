"""Explainable assignment-pressure heuristic; never a probability estimate."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from chronos.domain.enums import AssignmentPressure, OptionRight
from chronos.domain.models import ChronosModel

if TYPE_CHECKING:
    from chronos.config.settings import Settings

ASSIGNMENT_HEURISTIC_NOTICE = (
    "Assignment pressure is a heuristic warning, not an assignment probability."
)


class AssignmentPressurePolicy(ChronosModel):
    near_zero_extrinsic: Decimal = Field(default=Decimal("0.05"), ge=0)
    meaningful_extrinsic: Decimal = Field(default=Decimal("0.10"), ge=0)
    elevated_abs_delta: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    high_dte: int = Field(default=3, ge=0)
    elevated_dte: int = Field(default=5, ge=0)
    ex_dividend_window_days: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> AssignmentPressurePolicy:
        if self.near_zero_extrinsic > self.meaningful_extrinsic:
            raise ValueError("near-zero extrinsic must not exceed meaningful extrinsic")
        if self.high_dte > self.elevated_dte:
            raise ValueError("HIGH DTE must not exceed ELEVATED DTE")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> AssignmentPressurePolicy:
        return cls(
            near_zero_extrinsic=settings.assignment_near_zero_extrinsic,
            meaningful_extrinsic=settings.assignment_meaningful_extrinsic,
            elevated_abs_delta=settings.assignment_elevated_abs_delta,
            high_dte=settings.assignment_high_dte,
            elevated_dte=settings.assignment_elevated_dte,
            ex_dividend_window_days=settings.assignment_ex_dividend_window_days,
        )


class AssignmentPressureInput(ChronosModel):
    right: OptionRight
    stock_price: Decimal | None = Field(default=None, gt=0)
    strike: Decimal | None = Field(default=None, gt=0)
    dte: int | None = Field(default=None, ge=0)
    delta: Decimal | None = Field(default=None, ge=-1, le=1)
    remaining_extrinsic_value: Decimal | None = Field(default=None, ge=0)
    dividend_data_reliable: bool = False
    expected_dividend: Decimal | None = Field(default=None, ge=0)
    days_to_ex_dividend: int | None = Field(default=None, ge=0)
    borrow_warning: bool = False
    corporate_action_warning: bool = False


class AssignmentPressureResult(ChronosModel):
    status: AssignmentPressure
    is_itm: bool | None
    fired_rules: tuple[str, ...]
    rationale: str
    notice: str = ASSIGNMENT_HEURISTIC_NOTICE


def assess_assignment_pressure(
    values: AssignmentPressureInput,
    *,
    policy: AssignmentPressurePolicy | None = None,
) -> AssignmentPressureResult:
    """Classify pressure using explicit conservative precedence and evidence."""

    selected_policy = policy or AssignmentPressurePolicy()
    if values.corporate_action_warning:
        return _result(
            status=AssignmentPressure.UNKNOWN,
            is_itm=None,
            rules=("corporate_action_warning",),
            rationale="A corporate-action warning makes the contract inputs unreliable.",
        )
    if values.stock_price is None or values.strike is None or values.dte is None:
        return _result(
            status=AssignmentPressure.UNKNOWN,
            is_itm=None,
            rules=("required_inputs_missing",),
            rationale="Stock price, strike, and DTE are required for this heuristic.",
        )

    is_itm = (
        values.stock_price < values.strike
        if values.right is OptionRight.PUT
        else values.stock_price > values.strike
    )
    is_otm = (
        values.stock_price > values.strike
        if values.right is OptionRight.PUT
        else values.stock_price < values.strike
    )
    absolute_delta = values.delta.copy_abs() if values.delta is not None else None
    rules: list[str] = []

    # The product specification makes near-zero extrinsic an independent HIGH rule.
    if (
        values.remaining_extrinsic_value is not None
        and values.remaining_extrinsic_value <= selected_policy.near_zero_extrinsic
    ):
        status = AssignmentPressure.HIGH
        rules.append("near_zero_extrinsic")
    elif is_itm and values.dte <= selected_policy.high_dte:
        status = AssignmentPressure.HIGH
        rules.append("itm_near_expiration")
    elif (
        is_itm
        or (absolute_delta is not None and absolute_delta >= selected_policy.elevated_abs_delta)
        or values.dte <= selected_policy.elevated_dte
    ):
        status = AssignmentPressure.ELEVATED
        if is_itm:
            rules.append("in_the_money")
        if absolute_delta is not None and absolute_delta >= selected_policy.elevated_abs_delta:
            rules.append("elevated_absolute_delta")
        if values.dte <= selected_policy.elevated_dte:
            rules.append("near_expiration")
    elif (
        is_otm
        and values.remaining_extrinsic_value is not None
        and values.remaining_extrinsic_value >= selected_policy.meaningful_extrinsic
        and values.dte > selected_policy.elevated_dte
    ):
        status = AssignmentPressure.LOW
        rules.append("otm_with_time_and_extrinsic")
    else:
        status = AssignmentPressure.UNKNOWN
        rules.append("insufficient_evidence_for_category")

    dividend_escalation = (
        values.right is OptionRight.CALL
        and is_itm
        and values.dividend_data_reliable
        and values.expected_dividend is not None
        and values.days_to_ex_dividend is not None
        and 0
        <= values.days_to_ex_dividend
        <= min(selected_policy.ex_dividend_window_days, values.dte)
        and values.remaining_extrinsic_value is not None
        and values.remaining_extrinsic_value < values.expected_dividend
    )
    if dividend_escalation:
        status = AssignmentPressure.HIGH
        rules.append("reliable_dividend_exceeds_extrinsic")
    if values.borrow_warning:
        if status in {AssignmentPressure.LOW, AssignmentPressure.UNKNOWN}:
            status = AssignmentPressure.ELEVATED
        rules.append("borrow_warning")

    rationale = (
        f"{status.value} because " + ", ".join(rule.replace("_", " ") for rule in rules) + "."
    )
    return _result(status=status, is_itm=is_itm, rules=tuple(rules), rationale=rationale)


def _result(
    *,
    status: AssignmentPressure,
    is_itm: bool | None,
    rules: tuple[str, ...],
    rationale: str,
) -> AssignmentPressureResult:
    return AssignmentPressureResult(
        status=status,
        is_itm=is_itm,
        fired_rules=rules,
        rationale=rationale,
    )

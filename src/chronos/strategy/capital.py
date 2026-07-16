"""Capital and concentration checks kept separate from broker margin."""

from __future__ import annotations

from contextlib import AbstractContextManager
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from pydantic import Field, field_validator, model_validator

from chronos.domain.models import ChronosModel
from chronos.utils.identifiers import normalize_account_fingerprint


def _calculation_context(*values: Decimal | int) -> AbstractContextManager[Context]:
    precision = max(
        50,
        sum(
            len(value.as_tuple().digits) if isinstance(value, Decimal) else len(str(abs(value)))
            for value in values
        )
        + 16,
    )
    return localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN))


class CapitalContext(ChronosModel):
    """Account allocation state before a proposed Wheel action."""

    account_fingerprint: str
    net_liquidation: Decimal = Field(ge=0)
    uncommitted_cash: Decimal
    current_symbol_allocation: Decimal = Field(ge=0)
    current_total_wheel_allocation: Decimal = Field(ge=0)
    max_symbol_allocation_pct: Decimal = Field(gt=0, le=1)
    max_total_wheel_allocation_pct: Decimal = Field(gt=0, le=1)
    currency: str = "USD"

    @field_validator("account_fingerprint")
    @classmethod
    def validate_account_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("currency must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_allocation_relationship(self) -> CapitalContext:
        if self.current_symbol_allocation > self.current_total_wheel_allocation:
            raise ValueError(
                "current symbol allocation must not exceed current total Wheel allocation"
            )
        return self


class CapitalCheck(ChronosModel):
    """Transparent allocation result; this is never labeled broker margin."""

    passed: bool
    proposal_amount: Decimal = Field(ge=0)
    resulting_symbol_allocation: Decimal = Field(ge=0)
    resulting_total_wheel_allocation: Decimal = Field(ge=0)
    resulting_symbol_allocation_pct: Decimal | None
    resulting_total_wheel_allocation_pct: Decimal | None
    shares_required: Decimal = Field(ge=0)
    remaining_unencumbered_shares: Decimal | None = Field(default=None, ge=0)
    reasons: tuple[str, ...]


def short_put_notional(
    *,
    strike: Decimal,
    multiplier: Decimal,
    contracts: int,
) -> Decimal:
    """Return the gross cash-secured obligation for a short put."""

    _validate_contract_inputs(strike=strike, multiplier=multiplier, contracts=contracts)
    with _calculation_context(strike, multiplier, contracts):
        return strike * multiplier * contracts


def stock_capital(*, shares: Decimal, reference_price: Decimal) -> Decimal:
    """Value stock allocation at an explicit, caller-selected reference price."""

    if shares < 0:
        raise ValueError("shares must not be negative")
    if reference_price < 0:
        raise ValueError("reference_price must not be negative")
    with _calculation_context(shares, reference_price):
        return shares * reference_price


def evaluate_short_put_capital(
    *,
    context: CapitalContext,
    strike: Decimal,
    multiplier: Decimal,
    verified_deliverable_shares: Decimal,
    contracts: int,
) -> CapitalCheck:
    """Check cash security plus post-trade symbol and portfolio allocation."""

    _validate_contract_inputs(strike=strike, multiplier=multiplier, contracts=contracts)
    _validate_verified_standard_deliverable(
        multiplier=multiplier,
        verified_deliverable_shares=verified_deliverable_shares,
    )
    with _calculation_context(
        context.net_liquidation,
        context.uncommitted_cash,
        context.current_symbol_allocation,
        context.current_total_wheel_allocation,
        strike,
        multiplier,
        verified_deliverable_shares,
        contracts,
    ):
        obligation = strike * verified_deliverable_shares * contracts
        resulting_symbol = context.current_symbol_allocation + obligation
        resulting_total = context.current_total_wheel_allocation + obligation
        symbol_pct, total_pct, reasons = _allocation_result(
            context=context,
            resulting_symbol=resulting_symbol,
            resulting_total=resulting_total,
        )
        if context.uncommitted_cash < obligation:
            reasons.append("Uncommitted cash is below the gross assignment obligation.")
        return CapitalCheck(
            passed=not reasons,
            proposal_amount=obligation,
            resulting_symbol_allocation=resulting_symbol,
            resulting_total_wheel_allocation=resulting_total,
            resulting_symbol_allocation_pct=symbol_pct,
            resulting_total_wheel_allocation_pct=total_pct,
            shares_required=verified_deliverable_shares * contracts,
            reasons=tuple(reasons),
        )


def evaluate_covered_call_capital(
    *,
    context: CapitalContext,
    multiplier: Decimal,
    verified_deliverable_shares: Decimal,
    contracts: int,
    unencumbered_shares: Decimal,
) -> CapitalCheck:
    """Require full coverage and verify the existing stock allocation remains permitted."""

    _validate_contract_count(contracts)
    _validate_verified_standard_deliverable(
        multiplier=multiplier,
        verified_deliverable_shares=verified_deliverable_shares,
    )
    if unencumbered_shares < 0:
        raise ValueError("unencumbered_shares must not be negative")
    with _calculation_context(
        context.net_liquidation,
        context.current_symbol_allocation,
        context.current_total_wheel_allocation,
        multiplier,
        verified_deliverable_shares,
        contracts,
        unencumbered_shares,
    ):
        required = verified_deliverable_shares * contracts
        remaining = (
            unencumbered_shares - required if unencumbered_shares >= required else Decimal("0")
        )
        symbol_pct, total_pct, reasons = _allocation_result(
            context=context,
            resulting_symbol=context.current_symbol_allocation,
            resulting_total=context.current_total_wheel_allocation,
        )
        if unencumbered_shares < required:
            reasons.append("The proposed calls are not fully covered by unencumbered shares.")
        return CapitalCheck(
            passed=not reasons,
            proposal_amount=Decimal("0"),
            resulting_symbol_allocation=context.current_symbol_allocation,
            resulting_total_wheel_allocation=context.current_total_wheel_allocation,
            resulting_symbol_allocation_pct=symbol_pct,
            resulting_total_wheel_allocation_pct=total_pct,
            shares_required=required,
            remaining_unencumbered_shares=remaining,
            reasons=tuple(reasons),
        )


def _allocation_result(
    *,
    context: CapitalContext,
    resulting_symbol: Decimal,
    resulting_total: Decimal,
) -> tuple[Decimal | None, Decimal | None, list[str]]:
    reasons: list[str] = []
    if context.net_liquidation <= 0:
        reasons.append("Net liquidation value must be positive for allocation checks.")
        return None, None, reasons
    with _calculation_context(
        context.net_liquidation,
        context.max_symbol_allocation_pct,
        context.max_total_wheel_allocation_pct,
        resulting_symbol,
        resulting_total,
    ):
        symbol_limit = context.net_liquidation * context.max_symbol_allocation_pct
        total_limit = context.net_liquidation * context.max_total_wheel_allocation_pct
        if resulting_symbol > symbol_limit:
            reasons.append("The resulting symbol allocation exceeds its configured limit.")
        if resulting_total > total_limit:
            reasons.append("The resulting total Wheel allocation exceeds its configured limit.")
        symbol_pct = resulting_symbol / context.net_liquidation
        total_pct = resulting_total / context.net_liquidation
        return symbol_pct, total_pct, reasons


def _validate_contract_inputs(
    *,
    strike: Decimal,
    multiplier: Decimal,
    contracts: int,
) -> None:
    _validate_contract_count(contracts)
    if strike <= 0:
        raise ValueError("strike must be positive")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")


def _validate_verified_standard_deliverable(
    *,
    multiplier: Decimal,
    verified_deliverable_shares: Decimal,
) -> None:
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")
    if verified_deliverable_shares <= 0:
        raise ValueError("verified_deliverable_shares must be positive")
    if verified_deliverable_shares != multiplier:
        raise ValueError("Capital checks support only verified standard share deliverables")


def _validate_contract_count(contracts: object) -> None:
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts <= 0:
        raise ValueError("contracts must be a positive integer")

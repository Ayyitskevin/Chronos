"""Decimal-only expiration scenarios for proposed Wheel option sales."""

from __future__ import annotations

from contextlib import AbstractContextManager
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from chronos.domain.models import ChronosModel


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


class ShortPutScenarioInput(ChronosModel):
    strike: Decimal = Field(gt=0)
    premium_per_share: Decimal = Field(ge=0)
    multiplier: Decimal = Field(gt=0)
    deliverable_shares: Decimal = Field(gt=0)
    deliverable_verified: Literal[True]
    contracts: int = Field(gt=0, strict=True)
    commission: Decimal = Field(ge=0)
    commission_is_estimate: bool = True
    scenario_price: Decimal = Field(ge=0)
    net_liquidation: Decimal = Field(gt=0)
    broker_margin_impact: Decimal | None = None

    @model_validator(mode="after")
    def validate_standard_deliverable(self) -> ShortPutScenarioInput:
        if self.deliverable_shares != self.multiplier:
            raise ValueError("Short-put scenarios support only verified standard deliverables")
        return self


class ShortPutScenario(ChronosModel):
    gross_premium: Decimal
    commission: Decimal
    commission_is_estimate: bool
    net_premium: Decimal
    assigned_shares: Decimal
    gross_assignment_obligation: Decimal
    net_cash_deployed_if_assigned: Decimal
    effective_entry_price: Decimal
    expiration_pnl: Decimal
    cash_secured_notional_pct: Decimal
    broker_margin_impact: Decimal | None


class CoveredCallScenarioInput(ChronosModel):
    strike: Decimal = Field(gt=0)
    premium_per_share: Decimal = Field(ge=0)
    multiplier: Decimal = Field(gt=0)
    deliverable_shares: Decimal = Field(gt=0)
    deliverable_verified: Literal[True]
    contracts: int = Field(gt=0, strict=True)
    commission: Decimal = Field(ge=0)
    commission_is_estimate: bool = True
    scenario_price: Decimal = Field(ge=0)
    broker_average_cost: Decimal = Field(ge=0)
    strategy_adjusted_basis: Decimal
    held_shares: Decimal = Field(ge=0)
    already_encumbered_shares: Decimal = Field(default=Decimal("0"), ge=0)
    broker_margin_impact: Decimal | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> CoveredCallScenarioInput:
        if self.deliverable_shares != self.multiplier:
            raise ValueError("Covered-call scenarios support only verified standard deliverables")
        with _calculation_context(
            self.deliverable_shares,
            self.contracts,
            self.already_encumbered_shares,
            self.held_shares,
        ):
            shares_covered = self.deliverable_shares * self.contracts
            if self.already_encumbered_shares + shares_covered > self.held_shares:
                raise ValueError("The covered-call scenario would create naked-call exposure")
        return self


class CoveredCallScenario(ChronosModel):
    gross_premium: Decimal
    commission: Decimal
    commission_is_estimate: bool
    net_premium: Decimal
    shares_covered: Decimal
    remaining_unencumbered_shares: Decimal
    called_away_proceeds: Decimal
    stock_pnl_vs_broker_average_cost: Decimal
    total_cycle_pnl_vs_strategy_adjusted_basis: Decimal
    strategy_adjusted_basis_after_premium: Decimal
    upside_forfeited: Decimal
    downside_stock_exposure_after_premium: Decimal
    broker_margin_impact: Decimal | None


def short_put_scenario(values: ShortPutScenarioInput) -> ShortPutScenario:
    """Calculate a short-put expiration scenario without broker-margin assumptions."""

    _validate_contract_count(values.contracts)
    with _calculation_context(
        values.strike,
        values.premium_per_share,
        values.multiplier,
        values.deliverable_shares,
        values.contracts,
        values.commission,
        values.scenario_price,
        values.net_liquidation,
    ):
        assigned_shares = values.deliverable_shares * values.contracts
        gross_premium = values.premium_per_share * values.multiplier * values.contracts
        net_premium = gross_premium - values.commission
        obligation = values.strike * assigned_shares
        intrinsic_loss = max(values.strike - values.scenario_price, Decimal("0"))
        return ShortPutScenario(
            gross_premium=gross_premium,
            commission=values.commission,
            commission_is_estimate=values.commission_is_estimate,
            net_premium=net_premium,
            assigned_shares=assigned_shares,
            gross_assignment_obligation=obligation,
            net_cash_deployed_if_assigned=obligation - net_premium,
            effective_entry_price=values.strike - (net_premium / assigned_shares),
            expiration_pnl=net_premium - intrinsic_loss * assigned_shares,
            cash_secured_notional_pct=obligation / values.net_liquidation,
            broker_margin_impact=values.broker_margin_impact,
        )


def covered_call_scenario(values: CoveredCallScenarioInput) -> CoveredCallScenario:
    """Calculate covered-call outcomes while preserving both cost-basis labels."""

    _validate_contract_count(values.contracts)
    with _calculation_context(
        values.strike,
        values.premium_per_share,
        values.multiplier,
        values.deliverable_shares,
        values.contracts,
        values.commission,
        values.scenario_price,
        values.broker_average_cost,
        values.strategy_adjusted_basis,
        values.held_shares,
        values.already_encumbered_shares,
    ):
        shares_covered = values.deliverable_shares * values.contracts
        gross_premium = values.premium_per_share * values.multiplier * values.contracts
        net_premium = gross_premium - values.commission
        remaining = values.held_shares - values.already_encumbered_shares - shares_covered
        called_away = values.strike * shares_covered
        downside_stock_loss = (
            max(
                values.strategy_adjusted_basis - values.scenario_price,
                Decimal("0"),
            )
            * shares_covered
        )
        return CoveredCallScenario(
            gross_premium=gross_premium,
            commission=values.commission,
            commission_is_estimate=values.commission_is_estimate,
            net_premium=net_premium,
            shares_covered=shares_covered,
            remaining_unencumbered_shares=remaining,
            called_away_proceeds=called_away,
            stock_pnl_vs_broker_average_cost=(values.strike - values.broker_average_cost)
            * shares_covered,
            total_cycle_pnl_vs_strategy_adjusted_basis=(
                values.strike - values.strategy_adjusted_basis
            )
            * shares_covered
            + net_premium,
            strategy_adjusted_basis_after_premium=(
                values.strategy_adjusted_basis - net_premium / shares_covered
            ),
            upside_forfeited=(
                max(values.scenario_price - values.strike, Decimal("0")) * shares_covered
            ),
            # A negative value means retained premium still exceeds the selected stock loss.
            downside_stock_exposure_after_premium=downside_stock_loss - net_premium,
            broker_margin_impact=values.broker_margin_impact,
        )


def _validate_contract_count(contracts: object) -> None:
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts <= 0:
        raise ValueError("contracts must be a positive integer")

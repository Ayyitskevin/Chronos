"""Deterministic sizing: the model requests, the kernel decides (ADR-0016 §2).

The single most important property of the autonomy design is that
``AITradeDecision.requested_quantity`` is **not executable**. This module is
where that becomes true in code: it takes the model's request as an *upper
bound suggestion* and independently derives a final quantity from the mandate's
limits and the supervisor's own account evidence, clamping downward and
refusing outright when nothing safe remains.

Rules, in order of severity:

- The result is never larger than the model asked for, and never larger than any
  mandate ceiling. Every binding constraint is recorded, so a clamp is
  explainable after the fact.
- Money and quantity are ``Decimal`` throughout, matching the order plane. There
  is no float arithmetic on a size or a price anywhere in this module.
- Missing evidence is refusal, not a default. A price of zero, an absent
  net-liquidation value, or a non-positive limit produces ``None``, never an
  unbounded or guessed size.
- Cash and buying-power **floors** are subtracted from what is spendable before
  sizing, so a mandate's floor genuinely reserves capital rather than being
  advisory.

This module performs no I/O and knows nothing about brokers or contracts. It is
pure arithmetic over evidence the supervisor already gathered.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from chronos.autonomy import AutonomyMandate, TradableAssetClass

#: Asset classes whose quantity is a contract count with a multiplier, rather
#: than a share count. Kept local: the model plane owns no order semantics.
_CONTRACT_CLASSES = frozenset(
    {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION, TradableAssetClass.FUTURE}
)


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    """Account facts the supervisor observed. Never model-supplied."""

    net_liquidation_usd: Decimal
    total_cash_usd: Decimal
    buying_power_usd: Decimal
    #: Current exposure to this instrument, in USD notional.
    symbol_exposure_usd: Decimal = Decimal(0)
    #: Current gross exposure across the account, in USD notional.
    gross_exposure_usd: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class SizingOutcome:
    """The kernel's independent answer. ``quantity`` of ``None`` means refuse."""

    quantity: Decimal | None
    requested: Decimal | None
    #: Human-readable reasons, in the order applied. Always populated.
    constraints: tuple[str, ...]
    refusal: str = ""

    @property
    def was_clamped(self) -> bool:
        return (
            self.quantity is not None
            and self.requested is not None
            and self.quantity < self.requested
        )


def _floor_to_whole(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding="ROUND_FLOOR")


def size_order(
    *,
    mandate: AutonomyMandate,
    asset_class: TradableAssetClass,
    reference_price: Decimal,
    multiplier: Decimal,
    evidence: AccountEvidence,
    requested_quantity: Decimal | None,
    whole_units: bool = True,
) -> SizingOutcome:
    """Derive the final executable quantity, or ``None`` to refuse.

    ``reference_price`` and ``multiplier`` come from the qualified contract, not
    from the decision — the model never supplies the numbers its own size is
    computed from.
    """

    constraints: list[str] = []

    if not reference_price.is_finite() or reference_price <= 0:
        return SizingOutcome(
            quantity=None,
            requested=requested_quantity,
            constraints=(),
            refusal="reference price is missing or non-positive; refusing to size",
        )
    if not multiplier.is_finite() or multiplier <= 0:
        return SizingOutcome(
            quantity=None,
            requested=requested_quantity,
            constraints=(),
            refusal="contract multiplier is missing or non-positive; refusing to size",
        )
    if evidence.net_liquidation_usd <= 0:
        return SizingOutcome(
            quantity=None,
            requested=requested_quantity,
            constraints=(),
            refusal="net liquidation is not positive; refusing to size",
        )

    unit_notional = reference_price * multiplier
    capital = mandate.capital
    candidates: list[Decimal] = []

    # The model's request is an upper bound, never a floor.
    if requested_quantity is not None:
        candidates.append(requested_quantity)
        constraints.append(f"model requested {requested_quantity}")

    # Per-order notional ceiling.
    if capital.max_order_notional_usd > 0:
        allowed = capital.max_order_notional_usd / unit_notional
        candidates.append(allowed)
        constraints.append(f"max_order_notional_usd {capital.max_order_notional_usd}")

    # Per-order unit ceiling, by asset class.
    if asset_class in _CONTRACT_CLASSES:
        if capital.max_contracts_per_order > 0:
            candidates.append(Decimal(capital.max_contracts_per_order))
            constraints.append(f"max_contracts_per_order {capital.max_contracts_per_order}")
    elif capital.max_shares_per_order > 0:
        candidates.append(Decimal(capital.max_shares_per_order))
        constraints.append(f"max_shares_per_order {capital.max_shares_per_order}")

    # Allocated capital for the whole mandate.
    if capital.allocated_capital_usd > 0:
        candidates.append(capital.allocated_capital_usd / unit_notional)
        constraints.append(f"allocated_capital_usd {capital.allocated_capital_usd}")

    # Spendable cash after the mandate's floors. A floor that is not subtracted
    # is not a floor.
    spendable = evidence.total_cash_usd - capital.min_cash_floor_usd
    candidates.append(spendable / unit_notional if spendable > 0 else Decimal(0))
    constraints.append(
        f"cash {evidence.total_cash_usd} less min_cash_floor_usd {capital.min_cash_floor_usd}"
    )

    usable_buying_power = evidence.buying_power_usd - capital.min_buying_power_usd
    candidates.append(
        usable_buying_power / unit_notional if usable_buying_power > 0 else Decimal(0)
    )
    constraints.append(
        f"buying power {evidence.buying_power_usd} less min_buying_power_usd "
        f"{capital.min_buying_power_usd}"
    )

    # Per-symbol concentration, measured against net liquidation.
    symbol_cap_pct = mandate.concentration.max_symbol_exposure_pct
    if symbol_cap_pct > 0:
        headroom = symbol_cap_pct * evidence.net_liquidation_usd - evidence.symbol_exposure_usd
        candidates.append(headroom / unit_notional if headroom > 0 else Decimal(0))
        constraints.append(f"max_symbol_exposure_pct {symbol_cap_pct}")

    # Gross exposure ceiling.
    if capital.max_gross_exposure_usd > 0:
        headroom = capital.max_gross_exposure_usd - evidence.gross_exposure_usd
        candidates.append(headroom / unit_notional if headroom > 0 else Decimal(0))
        constraints.append(f"max_gross_exposure_usd {capital.max_gross_exposure_usd}")

    quantity = min(candidates)
    if whole_units:
        quantity = _floor_to_whole(quantity)

    if quantity <= 0:
        return SizingOutcome(
            quantity=None,
            requested=requested_quantity,
            constraints=tuple(constraints),
            refusal="no quantity survives the mandate limits and available capital",
        )
    return SizingOutcome(
        quantity=quantity, requested=requested_quantity, constraints=tuple(constraints)
    )

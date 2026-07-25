"""Deterministic sizing: the model requests, the kernel decides (ADR-0016 §2).

The single most important property of the autonomy design is that
``AITradeDecision.requested_quantity`` is **not executable**. This module is
where that becomes true in code: it takes the model's request as an *upper bound
suggestion* and independently derives a final quantity from the mandate's limits
and the supervisor's own account evidence, clamping downward and refusing
outright when nothing safe remains.

Rules, in order of severity:

- **Every ceiling the mandate sets binds, or sizing refuses.** A limit that is
  set but whose evidence the supervisor did not gather is a *refusal*, never an
  ignored limit. The M2 adversarial review found the opposite: four of seven
  ``CapitalLimits`` ceilings were silently skipped while this docstring claimed
  the result was "never larger than any mandate ceiling". That claim is now
  true because unenforceable limits refuse instead.
- **Zero binds at zero.** A ceiling left at its default authorizes nothing; it
  is not "unset". (The first version skipped zero limits, which inverted
  deny-by-default — see the M2 fix in CHANGELOG.)
- **Absent evidence is refusal, not a default.** ``AccountEvidence`` exposure
  fields are ``None`` when unknown rather than ``0``, because zero is the *most*
  permissive value for a headroom calculation.
- Money and quantity are ``Decimal`` throughout. There is no float arithmetic on
  a size or a price anywhere in this module.
- **This function never raises.** Adversarial Decimal inputs that would overflow
  or divide by zero produce a refusal outcome, because a raising sizer inside a
  supervisor loop is an availability failure that could strand a decision.

This module performs no I/O and knows nothing about brokers or contracts. It is
pure arithmetic over evidence the supervisor already gathered.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

from chronos.autonomy import AutonomyMandate, TradableAssetClass

#: Asset classes whose quantity is a contract count with a multiplier, rather
#: than a share count. Kept local: the model plane owns no order semantics.
_CONTRACT_CLASSES = frozenset(
    {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION, TradableAssetClass.FUTURE}
)

#: Asset classes whose quantity may legally be fractional. Flooring a crypto
#: size to a whole unit would refuse every sub-unit order (M2 review).
_FRACTIONAL_CLASSES = frozenset({TradableAssetClass.CRYPTO})


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    """Account facts the supervisor observed. Never model-supplied.

    Optional fields are ``None`` when the supervisor did not gather them. They
    are deliberately **not** defaulted to zero: zero is the most permissive value
    for an exposure headroom, so defaulting would silently widen authority. If a
    mandate sets a limit whose evidence is ``None``, sizing refuses.
    """

    net_liquidation_usd: Decimal
    total_cash_usd: Decimal
    buying_power_usd: Decimal
    #: Current exposure to this instrument, in USD notional.
    symbol_exposure_usd: Decimal | None = None
    #: Current gross exposure across the account, in USD notional.
    gross_exposure_usd: Decimal | None = None
    #: Current net (signed) exposure across the account, in USD notional.
    net_exposure_usd: Decimal | None = None
    #: Current notional of the position this decision would extend.
    position_notional_usd: Decimal | None = None
    #: Broker-reported maintenance margin currently used.
    maintenance_margin_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizingOutcome:
    """The kernel's independent answer. ``quantity`` of ``None`` means refuse."""

    quantity: Decimal | None
    requested: Decimal | None
    #: Every constraint considered, in the order applied. Always populated.
    constraints: tuple[str, ...]
    #: Which constraint produced the final number. Makes a clamp explainable.
    binding_constraint: str = ""
    refusal: str = ""

    @property
    def was_clamped(self) -> bool:
        """True only when a model request existed and the kernel reduced it."""

        return (
            self.quantity is not None
            and self.requested is not None
            and self.quantity < self.requested
        )

    @property
    def kernel_derived(self) -> bool:
        """True when the kernel sized without any model request to clamp."""

        return self.quantity is not None and self.requested is None


def _refuse(requested: Decimal | None, constraints: list[str], reason: str) -> SizingOutcome:
    return SizingOutcome(
        quantity=None, requested=requested, constraints=tuple(constraints), refusal=reason
    )


def size_order(
    *,
    mandate: AutonomyMandate,
    asset_class: TradableAssetClass,
    reference_price: Decimal,
    multiplier: Decimal,
    evidence: AccountEvidence,
    requested_quantity: Decimal | None,
    whole_units: bool | None = None,
) -> SizingOutcome:
    """Derive the final executable quantity, or ``None`` to refuse.

    ``reference_price`` and ``multiplier`` come from the qualified contract, not
    from the decision — the model never supplies the numbers its own size is
    computed from. ``whole_units`` defaults to per-asset-class semantics:
    fractional for crypto, whole units everywhere else.
    """

    constraints: list[str] = []
    try:
        return _size(
            mandate=mandate,
            asset_class=asset_class,
            reference_price=reference_price,
            multiplier=multiplier,
            evidence=evidence,
            requested_quantity=requested_quantity,
            whole_units=whole_units,
            constraints=constraints,
        )
    except (decimal.DecimalException, ArithmeticError, ValueError) as error:
        # The contract is that this function returns an outcome. A raising sizer
        # inside the supervisor loop would strand the decision (M2 review).
        return _refuse(
            requested_quantity,
            constraints,
            f"sizing arithmetic failed on the supplied values ({type(error).__name__}); "
            "refusing rather than guessing",
        )


def _size(
    *,
    mandate: AutonomyMandate,
    asset_class: TradableAssetClass,
    reference_price: Decimal,
    multiplier: Decimal,
    evidence: AccountEvidence,
    requested_quantity: Decimal | None,
    whole_units: bool | None,
    constraints: list[str],
) -> SizingOutcome:
    if not reference_price.is_finite() or reference_price <= 0:
        return _refuse(
            requested_quantity, constraints, "reference price is missing or non-positive"
        )
    if not multiplier.is_finite() or multiplier <= 0:
        return _refuse(
            requested_quantity, constraints, "contract multiplier is missing or non-positive"
        )
    if not evidence.net_liquidation_usd.is_finite() or evidence.net_liquidation_usd <= 0:
        return _refuse(requested_quantity, constraints, "net liquidation is not positive")
    if requested_quantity is not None and (
        not requested_quantity.is_finite() or requested_quantity <= 0
    ):
        return _refuse(requested_quantity, constraints, "requested quantity is not positive")

    unit_notional = reference_price * multiplier
    capital = mandate.capital
    concentration = mandate.concentration
    # (candidate quantity, label) pairs. min() picks the binding one.
    candidates: list[tuple[Decimal, str]] = []

    def bind(value: Decimal, label: str) -> None:
        candidates.append((value if value > 0 else Decimal(0), label))
        constraints.append(label)

    def headroom(cap: Decimal, used: Decimal | None, label: str) -> str | None:
        """Bind a ceiling measured against current usage, or name what is missing."""

        if used is None:
            return label
        bind((cap - used) / unit_notional, label)
        return None

    if requested_quantity is not None:
        candidates.append((requested_quantity, "model request"))
        constraints.append(f"model requested {requested_quantity}")

    # --- ceilings that need only the mandate --------------------------------
    bind(
        capital.max_order_notional_usd / unit_notional,
        f"max_order_notional_usd {capital.max_order_notional_usd}",
    )
    if asset_class in _CONTRACT_CLASSES:
        bind(
            Decimal(capital.max_contracts_per_order),
            f"max_contracts_per_order {capital.max_contracts_per_order}",
        )
    else:
        bind(
            Decimal(capital.max_shares_per_order),
            f"max_shares_per_order {capital.max_shares_per_order}",
        )
    bind(
        capital.allocated_capital_usd / unit_notional,
        f"allocated_capital_usd {capital.allocated_capital_usd}",
    )

    # --- floors: subtracted, so a floor genuinely reserves -------------------
    spendable = evidence.total_cash_usd - capital.min_cash_floor_usd
    bind(spendable / unit_notional, f"cash less min_cash_floor_usd {capital.min_cash_floor_usd}")
    usable_bp = evidence.buying_power_usd - capital.min_buying_power_usd
    bind(
        usable_bp / unit_notional,
        f"buying power less min_buying_power_usd {capital.min_buying_power_usd}",
    )

    # --- ceilings that need supervisor evidence -----------------------------
    # A limit the mandate sets but whose evidence is absent REFUSES. Ignoring it
    # would make the "never larger than any mandate ceiling" claim false, which
    # is exactly what the M2 review found.
    missing: list[str] = []
    for cap, used, label in (
        (
            concentration.max_symbol_exposure_pct * evidence.net_liquidation_usd,
            evidence.symbol_exposure_usd,
            f"max_symbol_exposure_pct {concentration.max_symbol_exposure_pct}",
        ),
        (
            capital.max_gross_exposure_usd,
            evidence.gross_exposure_usd,
            f"max_gross_exposure_usd {capital.max_gross_exposure_usd}",
        ),
        (
            capital.max_net_exposure_usd,
            evidence.net_exposure_usd,
            f"max_net_exposure_usd {capital.max_net_exposure_usd}",
        ),
        (
            capital.max_position_notional_usd,
            evidence.position_notional_usd,
            f"max_position_notional_usd {capital.max_position_notional_usd}",
        ),
    ):
        absent = headroom(cap, used, label)
        if absent is not None:
            missing.append(absent)

    # Leverage and margin utilisation are ceilings on account state rather than
    # on this order's size; without broker margin evidence they cannot be
    # evaluated, so a mandate that sets them and evidence that lacks them refuse.
    if capital.max_leverage > 0 and evidence.gross_exposure_usd is None:
        missing.append(f"max_leverage {capital.max_leverage}")
    if capital.max_margin_utilization_pct > 0 and evidence.maintenance_margin_usd is None:
        missing.append(f"max_margin_utilization_pct {capital.max_margin_utilization_pct}")

    if missing:
        return _refuse(
            requested_quantity,
            constraints,
            "the mandate sets limits the supervisor has no evidence for, so they cannot be "
            f"enforced and no order may be sized: {'; '.join(missing)}",
        )

    # Leverage: gross exposure after this order may not exceed the multiple.
    if capital.max_leverage > 0:
        assert evidence.gross_exposure_usd is not None  # guarded above
        allowed_gross = capital.max_leverage * evidence.net_liquidation_usd
        bind(
            (allowed_gross - evidence.gross_exposure_usd) / unit_notional,
            f"max_leverage {capital.max_leverage}",
        )
    if capital.max_margin_utilization_pct > 0:
        assert evidence.maintenance_margin_usd is not None  # guarded above
        allowed_margin = capital.max_margin_utilization_pct * evidence.net_liquidation_usd
        if evidence.maintenance_margin_usd >= allowed_margin:
            bind(Decimal(0), f"max_margin_utilization_pct {capital.max_margin_utilization_pct}")
        else:
            constraints.append(
                f"max_margin_utilization_pct {capital.max_margin_utilization_pct} (headroom)"
            )

    quantity, binding = min(candidates, key=lambda pair: pair[0])
    fractional = asset_class in _FRACTIONAL_CLASSES
    if whole_units if whole_units is not None else not fractional:
        quantity = quantity.to_integral_value(rounding=decimal.ROUND_FLOOR)

    if quantity <= 0:
        return SizingOutcome(
            quantity=None,
            requested=requested_quantity,
            constraints=tuple(constraints),
            binding_constraint=binding,
            refusal=f"no quantity survives the mandate limits; binding constraint: {binding}",
        )
    return SizingOutcome(
        quantity=quantity,
        requested=requested_quantity,
        constraints=tuple(constraints),
        binding_constraint=binding,
    )

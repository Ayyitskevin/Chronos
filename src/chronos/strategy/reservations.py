"""Conservative cash and share reservation accounting.

The owner spec's core solvency rule: before proposing a new order, reserve —
at gross, never premium-netted — everything already committed: existing short
puts, partially filled and pending opening orders, the proposed order itself,
and the configured cash buffer. Shares likewise: shares covering active short
calls and pending call orders are encumbered. All calculations are pure
Decimal functions over explicit inputs; nothing here reads a broker or a
database, so the order service can feed it reconciled evidence directly.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from chronos.domain.models import ChronosModel


class CashReservationInput(ChronosModel):
    """Gross put obligations (strike x multiplier x contracts), per bucket."""

    existing_short_put_obligation: Decimal = Decimal("0")
    pending_put_order_obligation: Decimal = Decimal("0")
    proposed_order_obligation: Decimal = Decimal("0")
    cash_buffer: Decimal = Decimal("0")


class CashReservationSummary(ChronosModel):
    total_reserved: Decimal
    available_cash: Decimal
    remaining_after_reservations: Decimal
    sufficient: bool
    reasons: tuple[str, ...]


def reserve_cash(
    values: CashReservationInput, *, available_cash: Decimal
) -> CashReservationSummary:
    """Sum gross obligations + buffer against explicitly eligible cash."""

    buckets = (
        values.existing_short_put_obligation,
        values.pending_put_order_obligation,
        values.proposed_order_obligation,
        values.cash_buffer,
    )
    if any(bucket < 0 for bucket in buckets):
        raise ValueError("reservation buckets must be non-negative")
    total = sum(buckets, Decimal("0"))
    remaining = available_cash - total
    reasons: list[str] = []
    if remaining < 0:
        reasons.append(
            f"reserved {total} exceeds eligible cash {available_cash} (shortfall {-remaining})"
        )
    return CashReservationSummary(
        total_reserved=total,
        available_cash=available_cash,
        remaining_after_reservations=remaining,
        sufficient=remaining >= 0,
        reasons=tuple(reasons),
    )


class ShareReservationInput(ChronosModel):
    settled_long_shares: Decimal = Decimal("0")
    shares_covering_short_calls: Decimal = Decimal("0")
    shares_reserved_for_pending_calls: Decimal = Decimal("0")
    shares_reserved_by_other_orders: Decimal = Decimal("0")
    contract_multiplier: Decimal = Decimal("100")


class ShareReservationSummary(ChronosModel):
    unencumbered_shares: Decimal
    max_new_call_contracts: int
    reasons: tuple[str, ...]


def reserve_shares(values: ShareReservationInput) -> ShareReservationSummary:
    """Unencumbered shares and the covered-call capacity they support."""

    fields = (
        values.settled_long_shares,
        values.shares_covering_short_calls,
        values.shares_reserved_for_pending_calls,
        values.shares_reserved_by_other_orders,
    )
    if any(field < 0 for field in fields):
        raise ValueError("share buckets must be non-negative")
    if values.contract_multiplier <= 0:
        raise ValueError("contract multiplier must be positive")
    unencumbered = (
        values.settled_long_shares
        - values.shares_covering_short_calls
        - values.shares_reserved_for_pending_calls
        - values.shares_reserved_by_other_orders
    )
    reasons: list[str] = []
    if unencumbered < 0:
        reasons.append(
            "encumbrances exceed settled long shares; broker and local evidence disagree"
        )
        unencumbered_for_capacity = Decimal("0")
    else:
        unencumbered_for_capacity = unencumbered
    capacity = int(
        (unencumbered_for_capacity / values.contract_multiplier).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return ShareReservationSummary(
        unencumbered_shares=unencumbered,
        max_new_call_contracts=capacity,
        reasons=tuple(reasons),
    )

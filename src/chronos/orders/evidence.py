"""Concrete broker-reading risk-evidence provider.

Gathers the :class:`~chronos.orders.risk.RiskEvidence` the engine needs from
fresh broker reads (account, positions, open orders) plus persisted active
intents and the family-aware trading session. This is the integration seam the
owner exercises against a real paper gateway; the pipeline orchestration is
tested with a canned provider, so this module is deliberately thin and
side-effect free beyond serialized broker reads.

All gross obligations are computed from broker truth (option strike times
deliverable times contracts), never premium-netted, matching the reservation
rules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.domain.enums import (
    OptionRight,
    OrderIntent,
    OrderSide,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import (
    BrokerOrder,
    BrokerPosition,
    CryptoContract,
    OptionContract,
    UnderlyingContract,
)
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import RiskEvidence
from chronos.services.liquid_hours import parse_liquid_hours
from chronos.services.trading_hours import session_for

_logger = logging.getLogger(__name__)


class _OpeningCounter(Protocol):
    """The single repository method the daily opening cap needs.

    Structural rather than nominal so :mod:`chronos.orders` keeps not importing
    :mod:`chronos.persistence` — the same one-directional layering the rest of
    this module holds to, and the reason wiring the cap needed no new
    dependency edge.
    """

    def count_opening_since(self, *, current_account_id: str, since: datetime) -> int: ...


class BrokerRiskEvidenceProvider:
    """Read the broker + repos to assemble one intent's risk evidence."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        settings: Settings,
        reconciliation_readiness: ReconciliationReadiness | None = None,
        intents: _OpeningCounter | None = None,
    ) -> None:
        self._connection = connection
        self._settings = settings
        self._reconciliation_readiness = reconciliation_readiness or ReconciliationReadiness()
        # Optional so every existing test double keeps constructing. Absent, the
        # daily-opening count is UNKNOWN rather than zero — see `_opening_today`,
        # where the difference decides whether the cap binds or evaporates.
        self._intents = intents

    def gather(self, intent: WheelOrderIntent, *, now: datetime) -> RiskEvidence:
        readiness_before = self._reconciliation_readiness.snapshot()
        account = self._connection.run(self._connection.broker.account_summary())
        positions = self._connection.run(self._connection.broker.positions())
        open_orders = self._connection.run(self._connection.broker.open_orders())
        readiness_after = self._reconciliation_readiness.snapshot()
        # D43-FIX2 (D-53): a quarantined row means the venue's book is known-bad right now; the
        # READY proof was taken before it, so this evidence cannot claim it, whatever the adapter's
        # readiness hook did (absent, raising or working)
        book_is_readable = all(order.quantity_quarantine is None for order in open_orders)
        bracket_is_consistent = (
            readiness_before.session_id == readiness_after.session_id
            and readiness_before.generation == readiness_after.generation
            and book_is_readable
        )

        opening_today = self._opening_today(intent, now=now)

        existing_short_put_obligation = _short_put_obligation(positions)
        pending_open_put_obligation = _pending_put_obligation(open_orders)
        active_short_option_contracts = _short_option_contracts(positions)
        if intent.intent is OrderIntent.CLOSE_LONG_STOCK and isinstance(
            intent.contract, UnderlyingContract
        ):
            settled_long_shares = _long_shares_for_contract(positions, intent.contract)
            (
                shares_covering_short_calls,
                shares_reserved_for_pending_calls,
                stock_close_call_evidence_issues,
            ) = _stock_close_call_reservations(positions, open_orders, intent.contract)
        else:
            settled_long_shares = _long_shares(positions, intent.symbol)
            shares_covering_short_calls = _short_call_shares(positions, intent.symbol)
            shares_reserved_for_pending_calls = _pending_call_shares(open_orders, intent.symbol)
            stock_close_call_evidence_issues = ()
        held_short_option_contracts = _held_short_for_symbol(positions, intent)
        # Resting (unfilled) covered-call orders commit shares too; without this
        # two calls could both be approved against the same shares -> naked call.
        stock_sell_reserved_shares = (
            _stock_sell_reserved(open_orders, intent.contract.con_id)
            if isinstance(intent.contract, UnderlyingContract)
            else Decimal("0")
        )
        option_buy_reserved_contracts, option_buy_reservation_non_integral = (
            _option_buy_reserved(open_orders, intent.contract.con_id)
            if isinstance(intent.contract, OptionContract)
            else (Decimal("0"), False)
        )
        # Concentration baseline: existing gross wheel exposure, per symbol and
        # across the account, so the check caps AGGREGATE exposure, not just the
        # single new order.
        current_symbol_allocation = _symbol_allocation(positions, intent.symbol)
        current_total_wheel_allocation = _total_allocation(positions)

        # Crypto evidence (ADR-0010 §4), gathered separately from the wheel
        # aggregates. Allocation is MARKED from broker market prices; if any
        # held crypto lacks a mark the allocation-cap check fails UNKNOWN.
        held_crypto_quantity = _held_crypto(positions, intent.symbol)
        crypto_sell_reserved_quantity = _crypto_sell_reserved(open_orders, intent.symbol)
        current_crypto_allocation, crypto_allocation_marked = _crypto_allocation(positions)
        pending_crypto_buy_notional = _pending_crypto_buy_notional(open_orders)

        session = session_for(
            intent.product_family,
            now=now,
            broker_confirms_open=self._broker_confirms_open(intent, now),
        )
        reconciliation_status = (
            readiness_after.status if bracket_is_consistent else ReconciliationStatus.PENDING
        )
        return RiskEvidence(
            account=account,
            reconciliation_status=reconciliation_status,
            reconciliation_generation=(
                readiness_after.generation
                if reconciliation_status is ReconciliationStatus.RECONCILED
                else None
            ),
            reconciliation_session_id=(
                readiness_after.session_id
                if reconciliation_status is ReconciliationStatus.RECONCILED
                else None
            ),
            session=session,
            opening_orders_today=opening_today,
            existing_short_put_obligation=existing_short_put_obligation,
            pending_open_put_obligation=pending_open_put_obligation,
            active_short_option_contracts=active_short_option_contracts,
            settled_long_shares=settled_long_shares,
            shares_covering_short_calls=shares_covering_short_calls,
            shares_reserved_for_pending_calls=shares_reserved_for_pending_calls,
            stock_close_call_evidence_issues=stock_close_call_evidence_issues,
            stock_sell_reserved_shares=stock_sell_reserved_shares,
            held_short_option_contracts=held_short_option_contracts,
            option_buy_reserved_contracts=option_buy_reserved_contracts,
            option_buy_reservation_non_integral=option_buy_reservation_non_integral,
            current_symbol_allocation=current_symbol_allocation,
            current_total_wheel_allocation=current_total_wheel_allocation,
            held_crypto_quantity=held_crypto_quantity,
            crypto_sell_reserved_quantity=crypto_sell_reserved_quantity,
            current_crypto_allocation=current_crypto_allocation,
            crypto_allocation_marked=crypto_allocation_marked,
            pending_crypto_buy_notional=pending_crypto_buy_notional,
        )

    def _opening_today(self, intent: WheelOrderIntent, *, now: datetime) -> int | None:
        """Opening intents created so far in this market-local day (R-25).

        Before M10 nothing set this at all, so ``RiskEvidence.opening_orders_today``
        took its ``0`` default and ``max_opening_orders_per_day`` passed
        unconditionally — a configured limit that had never once refused
        anything. ADR-0010 §4 claimed it was wired; it was not.

        **The day is the market's, not UTC's.** 22:00 in New York is already
        tomorrow in UTC, so a UTC boundary would split one trading afternoon
        across two counters and silently double the cap for anyone trading the
        close. This is the same reasoning R-34 applied to the autonomy session
        counters, using the timezone `chronos.orders` already validates and
        already uses for its other session-day boundaries.

        Returns ``None`` when the count cannot be taken, which the caller turns
        into a refusal rather than a zero: a cap that reads "0 used" because the
        repository was unavailable is a cap that has quietly stopped existing.
        """

        if self._intents is None:
            return None
        zone = ZoneInfo(self._settings.market_timezone)
        local_midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            return self._intents.count_opening_since(
                current_account_id=intent.account_id,
                since=local_midnight.astimezone(UTC),
            )
        except Exception:
            _logger.exception(
                "Could not count today's opening intents; the daily cap will refuse",
                extra={"event": "opening_count_unavailable"},
            )
            return None

    def _broker_confirms_open(self, intent: WheelOrderIntent, now: datetime) -> bool | None:
        """Broker session evidence for this intent's contract, or ``None`` (M9, R-26).

        From Milestone 5 until M9 this hard-returned ``None``, so every equity
        and option instant inside regular trading hours resolved to AMBIGUOUS and
        blocked. Fail-closed, so never a money hazard — but it also meant the
        session gate had never been exercised and no live equity or option order
        could pass risk at all.

        The evidence now comes from ``liquidHours`` on the qualified contract, so
        this costs **no broker round-trip**: it is contract detail IBKR already
        returned during qualification, carried on the instrument the intent was
        built from. Putting a gateway call here would have added latency to the
        order path for a fact that was already in hand.

        Absent or unparseable hours stay ``None``, which is still AMBIGUOUS and
        still blocks. That is the direction every failure here degrades in, and
        :mod:`chronos.services.liquid_hours` is where the reasoning lives.
        """

        contract = intent.contract
        raw = getattr(contract, "liquid_hours", "")
        zone = getattr(contract, "time_zone_id", "")
        if not raw or not zone:
            return None
        hours = parse_liquid_hours(raw, time_zone_id=zone)
        if hours is None:
            return None
        return hours.confirms_open(now)


def _short_put_obligation(positions: tuple[BrokerPosition, ...]) -> Decimal:
    total = Decimal("0")
    for position in positions:
        contract = position.contract
        if (
            isinstance(contract, OptionContract)
            and contract.right is OptionRight.PUT
            and position.quantity < 0
        ):
            contracts = abs(position.quantity)
            deliverable = contract.deliverable_shares or contract.multiplier
            total += contract.strike * deliverable * contracts
    return total


def _pending_put_obligation(open_orders: tuple[BrokerOrder, ...]) -> Decimal:
    total = Decimal("0")
    for order in open_orders:
        contract = order.contract
        if isinstance(contract, OptionContract) and contract.right is OptionRight.PUT:
            deliverable = contract.deliverable_shares or contract.multiplier
            total += contract.strike * deliverable * order.remaining_quantity
    return total


def _short_option_contracts(positions: tuple[BrokerPosition, ...]) -> int:
    total = Decimal("0")
    for position in positions:
        if isinstance(position.contract, OptionContract) and position.quantity < 0:
            total += int(abs(position.quantity))
    return total


def _long_shares(positions: tuple[BrokerPosition, ...], symbol: str) -> Decimal:
    total = Decimal("0")
    for position in positions:
        contract = position.contract
        if (
            isinstance(contract, UnderlyingContract)
            and contract.symbol == symbol
            and position.quantity > 0
        ):
            total += position.quantity
    return total


def _long_shares_for_contract(
    positions: tuple[BrokerPosition, ...], contract: UnderlyingContract
) -> Decimal:
    """Long shares held for the exact stock contract and currency."""

    total = Decimal("0")
    for position in positions:
        held_contract = position.contract
        if (
            isinstance(held_contract, UnderlyingContract)
            and held_contract.con_id == contract.con_id
            and held_contract.currency == contract.currency
            and position.quantity > 0
        ):
            total += position.quantity
    return total


def _stock_close_call_reservations(
    positions: tuple[BrokerPosition, ...],
    open_orders: tuple[BrokerOrder, ...],
    underlying: UnderlyingContract,
) -> tuple[Decimal, Decimal, tuple[str, ...]]:
    """Return verified call encumbrances for one exact stock close."""

    covering_shares = Decimal("0")
    pending_shares = Decimal("0")
    unverified: set[str] = set()

    for position in positions:
        contract = position.contract
        if (
            not isinstance(contract, OptionContract)
            or contract.right is not OptionRight.CALL
            or position.quantity >= 0
            or not _may_encumber_stock_close(contract, underlying)
        ):
            continue
        deliverable = _verified_stock_close_call_deliverable(contract, underlying)
        if deliverable is None:
            unverified.add(_stock_close_call_label(contract))
        else:
            covering_shares += abs(position.quantity) * deliverable

    for order in open_orders:
        contract = order.contract
        if (
            not isinstance(contract, OptionContract)
            or contract.right is not OptionRight.CALL
            or order.side is not OrderSide.SELL
            or not _may_encumber_stock_close(contract, underlying)
        ):
            continue
        deliverable = _verified_stock_close_call_deliverable(contract, underlying)
        if deliverable is None:
            unverified.add(_stock_close_call_label(contract))
        else:
            pending_shares += order.remaining_quantity * deliverable

    return covering_shares, pending_shares, tuple(sorted(unverified))


def _may_encumber_stock_close(
    contract: OptionContract, underlying: UnderlyingContract
) -> bool:
    """Find calls that could encumber this ticker, then require exact evidence."""

    return contract.underlying_con_id == underlying.con_id or contract.symbol == underlying.symbol


def _verified_stock_close_call_deliverable(
    contract: OptionContract, underlying: UnderlyingContract
) -> Decimal | None:
    if (
        contract.deliverable_verified
        and contract.underlying_con_id == underlying.con_id
        and contract.currency == underlying.currency
        and contract.deliverable_shares is not None
    ):
        return contract.deliverable_shares
    return None


def _stock_close_call_label(contract: OptionContract) -> str:
    return (
        f"{contract.symbol} {contract.expiration.isoformat()} "
        f"CALL {contract.strike}"
    )


def _short_call_shares(positions: tuple[BrokerPosition, ...], symbol: str) -> Decimal:
    total = Decimal("0")
    for position in positions:
        contract = position.contract
        if (
            isinstance(contract, OptionContract)
            and contract.right is OptionRight.CALL
            and contract.symbol == symbol
            and position.quantity < 0
        ):
            deliverable = contract.deliverable_shares or contract.multiplier
            total += abs(position.quantity) * deliverable
    return total


def _held_short_for_symbol(positions: tuple[BrokerPosition, ...], intent: WheelOrderIntent) -> int:
    if intent.product_family is not ProductFamily.OPTION:
        return 0
    con_id = getattr(intent.contract, "con_id", None)
    total = 0
    for position in positions:
        contract = position.contract
        if (
            isinstance(contract, OptionContract)
            and contract.con_id == con_id
            and position.quantity < 0
        ):
            total += int(abs(position.quantity))
    return total


def _pending_call_shares(open_orders: tuple[BrokerOrder, ...], symbol: str) -> Decimal:
    """Shares committed to resting (unfilled) covered-call sell orders."""

    total = Decimal("0")
    for order in open_orders:
        contract = order.contract
        if (
            isinstance(contract, OptionContract)
            and contract.right is OptionRight.CALL
            and contract.symbol == symbol
            and order.side is OrderSide.SELL
        ):
            deliverable = contract.deliverable_shares or contract.multiplier
            total += order.remaining_quantity * deliverable
    return total


def _long_stock_value(position: BrokerPosition) -> Decimal:
    price = position.market_price if position.market_price is not None else position.average_cost
    return position.quantity * price


def _held_crypto(positions: tuple[BrokerPosition, ...], symbol: str) -> Decimal:
    """Long crypto quantity held for one symbol (trade-date broker truth)."""

    total = Decimal("0")
    for position in positions:
        contract = position.contract
        if (
            isinstance(contract, CryptoContract)
            and contract.symbol == symbol
            and position.quantity > 0
        ):
            total += position.quantity
    return total


def _crypto_sell_reserved(open_orders: tuple[BrokerOrder, ...], symbol: str) -> Decimal:
    """Crypto quantity committed to resting (unfilled) SELL orders for a symbol."""

    total = Decimal("0")
    for order in open_orders:
        contract = order.contract
        if (
            isinstance(contract, CryptoContract)
            and contract.symbol == symbol
            and order.side is OrderSide.SELL
        ):
            total += order.remaining_quantity
    return total


def _stock_sell_reserved(open_orders: tuple[BrokerOrder, ...], con_id: int) -> Decimal:
    """Stock shares committed to resting SELL orders for the exact contract."""

    total = Decimal("0")
    for order in open_orders:
        contract = order.contract
        if (
            isinstance(contract, UnderlyingContract)
            and contract.con_id == con_id
            and order.side is OrderSide.SELL
        ):
            total += order.remaining_quantity
    return total


def _option_buy_reserved(
    open_orders: tuple[BrokerOrder, ...], con_id: int
) -> tuple[Decimal, bool]:
    """Contracts committed to resting BUY orders for the exact option."""

    total = 0
    has_non_integral_quantity = False
    for order in open_orders:
        contract = order.contract
        if (
            isinstance(contract, OptionContract)
            and contract.con_id == con_id
            and order.side is OrderSide.BUY
        ):
            total += order.remaining_quantity
            has_non_integral_quantity |= (
                order.remaining_quantity != order.remaining_quantity.to_integral_value()
            )
    return total, has_non_integral_quantity


def _crypto_allocation(positions: tuple[BrokerPosition, ...]) -> tuple[Decimal, bool]:
    """Market value of all held crypto, and whether every holding had a mark.

    Returns ``(value, marked)``. ``marked`` is False if ANY long crypto
    position lacks a broker ``market_price`` — the allocation-cap check then
    fails UNKNOWN rather than under-count appreciated crypto at cost
    (ADR-0010 ris-6). Absent holdings ⇒ (0, True): nothing to mark.
    """

    total = Decimal("0")
    marked = True
    for position in positions:
        contract = position.contract
        if not isinstance(contract, CryptoContract) or position.quantity <= 0:
            continue
        # A non-positive mark is treated the same as a missing one: a real venue
        # never marks a held coin at 0, so 0/negative is anomalous data that must
        # fail UNKNOWN rather than silently value the holding at zero and let an
        # over-allocated BUY slip past the cap.
        if position.market_price is None or position.market_price <= 0:
            marked = False
            continue
        total += position.quantity * position.market_price
    return total, marked


def _pending_crypto_buy_notional(open_orders: tuple[BrokerOrder, ...]) -> Decimal:
    """USD notional committed to resting (unfilled) crypto BUY orders."""

    total = Decimal("0")
    for order in open_orders:
        contract = order.contract
        if isinstance(contract, CryptoContract) and order.side is OrderSide.BUY:
            total += order.remaining_quantity * order.limit_price
    return total


def _symbol_allocation(positions: tuple[BrokerPosition, ...], symbol: str) -> Decimal:
    """Gross wheel exposure for one symbol: short-put obligations + long stock."""

    total = Decimal("0")
    for position in positions:
        contract = position.contract
        if contract.symbol != symbol:
            continue
        if (
            isinstance(contract, OptionContract)
            and contract.right is OptionRight.PUT
            and position.quantity < 0
        ):
            deliverable = contract.deliverable_shares or contract.multiplier
            total += contract.strike * deliverable * abs(position.quantity)
        elif isinstance(contract, UnderlyingContract) and position.quantity > 0:
            total += _long_stock_value(position)
    return total


def _total_allocation(positions: tuple[BrokerPosition, ...]) -> Decimal:
    """Gross wheel exposure across the whole account."""

    total = _short_put_obligation(positions)
    for position in positions:
        contract = position.contract
        if isinstance(contract, UnderlyingContract) and position.quantity > 0:
            total += _long_stock_value(position)
    return total

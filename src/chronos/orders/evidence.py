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

from datetime import datetime
from decimal import Decimal

from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.domain.enums import OptionRight, OrderSide, ProductFamily, ReconciliationStatus
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


class BrokerRiskEvidenceProvider:
    """Read the broker + repos to assemble one intent's risk evidence."""

    def __init__(
        self,
        *,
        connection: BrokerConnectionManager,
        settings: Settings,
        reconciliation_readiness: ReconciliationReadiness | None = None,
    ) -> None:
        self._connection = connection
        self._settings = settings
        self._reconciliation_readiness = reconciliation_readiness or ReconciliationReadiness()

    def gather(self, intent: WheelOrderIntent, *, now: datetime) -> RiskEvidence:
        readiness_before = self._reconciliation_readiness.snapshot()
        account = self._connection.run(self._connection.broker.account_summary())
        positions = self._connection.run(self._connection.broker.positions())
        open_orders = self._connection.run(self._connection.broker.open_orders())
        readiness_after = self._reconciliation_readiness.snapshot()
        evidence_provenance_is_current = (
            readiness_before.ready
            and readiness_after.ready
            and readiness_before.session_id == readiness_after.session_id
            and readiness_before.generation == readiness_after.generation
        )

        existing_short_put_obligation = _short_put_obligation(positions)
        pending_open_put_obligation = _pending_put_obligation(open_orders)
        active_short_option_contracts = _short_option_contracts(positions)
        settled_long_shares = _long_shares(positions, intent.symbol)
        shares_covering_short_calls = _short_call_shares(positions, intent.symbol)
        held_short_option_contracts = _held_short_for_symbol(positions, intent)
        # Resting (unfilled) covered-call orders commit shares too; without this
        # two calls could both be approved against the same shares -> naked call.
        shares_reserved_for_pending_calls = _pending_call_shares(open_orders, intent.symbol)
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
        return RiskEvidence(
            account=account,
            reconciliation_status=(
                ReconciliationStatus.RECONCILED
                if evidence_provenance_is_current
                else ReconciliationStatus.PENDING
            ),
            reconciliation_generation=(
                readiness_after.generation if evidence_provenance_is_current else None
            ),
            reconciliation_session_id=(
                readiness_after.session_id if evidence_provenance_is_current else None
            ),
            session=session,
            existing_short_put_obligation=existing_short_put_obligation,
            pending_open_put_obligation=pending_open_put_obligation,
            active_short_option_contracts=active_short_option_contracts,
            settled_long_shares=settled_long_shares,
            shares_covering_short_calls=shares_covering_short_calls,
            shares_reserved_for_pending_calls=shares_reserved_for_pending_calls,
            held_short_option_contracts=held_short_option_contracts,
            current_symbol_allocation=current_symbol_allocation,
            current_total_wheel_allocation=current_total_wheel_allocation,
            held_crypto_quantity=held_crypto_quantity,
            crypto_sell_reserved_quantity=crypto_sell_reserved_quantity,
            current_crypto_allocation=current_crypto_allocation,
            crypto_allocation_marked=crypto_allocation_marked,
            pending_crypto_buy_notional=pending_crypto_buy_notional,
        )

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
    total = 0
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

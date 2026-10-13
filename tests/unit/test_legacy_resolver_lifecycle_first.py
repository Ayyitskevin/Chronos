"""S-3: the legacy resolver decides LIFECYCLE before QUANTITY (RED order plane).

``resolve_from_broker_evidence`` used to refine an exactly owned listed row by fill quantity
BEFORE it looked at the row's typed lifecycle: ``filled >= quantity`` was ``FILLED``,
``0 < filled < quantity`` was ``PARTIALLY_FILLED``, and the typed lifecycle only mattered when
nothing was filled. So a row the venue typed ``REJECTED`` or ``CANCELLED`` that also reported
fills (contradictory evidence: an order cannot be both rejected and filled) was written
``FILLED``, which takes the intent out of ``remaining_active`` and lets
``permits_new_opening_order`` go true. FM-3 is that fail-open; FM-3b is the same cause in the
fail-closed direction (a venue-terminal order recorded as a *working* partial fill).

The answer pinned here is the packet's recommended one (Q3): such a row is contradictory
evidence and the intent stays locked. Quantity refines only WORKING lifecycles. Typed terminal
rows WITHOUT fills keep their terminal write (an exactly owned listed rejection is evidence).

The positive controls pass on main today and must keep passing (they pin what the fix must not
touch). The identity rule (``_is_owned``) is not under test here: every row carries the owned
``CHR-`` reference exactly (S-12 is the blank-reference slice).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerOrder, UnderlyingContract
from chronos.orders.reconciliation_recovery import resolve_from_broker_evidence
from chronos.persistence.order_repositories import OrderIntentRecord
from chronos.utils.identifiers import account_fingerprint

ACCOUNT = "DU1234567"
CLIENT_ID = 17
REF = "CHR-ORD-" + "S" * 32
BROKER_ID = 9001
PERM_ID = 109001
QUANTITY = Decimal("10")
LIMIT = Decimal("150.00")
CON_ID = 265598
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)

# The four sent-active statuses the resolver is asked about (``_UNRESOLVED_STATUSES``).
SENT_ACTIVE = (
    OrderLifecycle.SUBMITTED,
    OrderLifecycle.SUBMISSION_UNKNOWN,
    OrderLifecycle.PARTIALLY_FILLED,
    OrderLifecycle.CANCEL_PENDING,
)
CONTRADICTED = (OrderLifecycle.REJECTED, OrderLifecycle.CANCELLED)


def _intent(status: OrderLifecycle) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id="intent-1",
        idempotency_key="idem-intent-1",
        account_fingerprint=account_fingerprint(ACCOUNT),
        environment="paper",
        product_family=ProductFamily.STOCK,
        wheel_cycle_id=None,
        symbol="AAPL",
        con_id=CON_ID,
        local_symbol=None,
        action=OrderSide.BUY,
        open_close_effect="OPEN",
        quantity=QUANTITY,
        order_type="LMT",
        limit_price=LIMIT,
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=REF,
        status=status,
        created_at=NOW,
        confirmed_at=NOW,
        submitted_at=None,
        expires_at=None,
    )


def _row(lifecycle: OrderLifecycle, filled: str) -> BrokerOrder:
    """An exactly owned listed row: the owned reference, the persisted ids, the right economics."""

    filled_quantity = Decimal(filled)
    return BrokerOrder(
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        account_id=ACCOUNT,
        order_ref=REF,
        contract=UnderlyingContract(con_id=CON_ID, symbol="AAPL"),
        side=OrderSide.BUY,
        quantity=QUANTITY,
        filled_quantity=filled_quantity,
        remaining_quantity=QUANTITY - filled_quantity,
        limit_price=LIMIT,
        lifecycle=lifecycle,
    )


def _resolve(status: OrderLifecycle, *rows: BrokerOrder) -> OrderLifecycle | None:
    update = resolve_from_broker_evidence(
        _intent(status),
        open_orders=tuple(rows),
        executions=(),
        current_account_id=ACCOUNT,
        expected_broker_client_id=CLIENT_ID,
        expected_limit_price=LIMIT,
        persisted_broker_order_id=BROKER_ID,
        persisted_permanent_id=PERM_ID,
        now=NOW,
    )
    return None if update is None else update.lifecycle


# --- RED: a typed REJECTED / CANCELLED row that reports fills is never promoted -----------------


@pytest.mark.parametrize("filled", ["1", "4", "10"])
@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_rejected_or_cancelled_listed_row_with_fills_is_never_promoted(
    local: OrderLifecycle, typed: OrderLifecycle, filled: str
) -> None:
    """FM-3 (filled = Q was FILLED) and FM-3b (0 < filled < Q was PARTIALLY_FILLED): no update."""

    assert _resolve(local, _row(typed, filled)) is None


def test_a_rejected_row_with_a_partial_fill_is_not_recorded_as_a_working_partial() -> None:
    """FM-3b by name: a venue-terminal order is not a working PARTIALLY_FILLED one."""

    outcome = _resolve(OrderLifecycle.SUBMITTED, _row(OrderLifecycle.REJECTED, "4"))
    assert outcome is not OrderLifecycle.PARTIALLY_FILLED
    assert outcome is None


# --- positive controls: these pass on main today and must keep passing ---------------------------


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_working_row_with_filled_at_or_above_quantity_is_still_filled(
    local: OrderLifecycle,
) -> None:
    """Design C3: quantity refines a WORKING row, so a submitted row filled to Q is FILLED."""

    assert _resolve(local, _row(OrderLifecycle.SUBMITTED, "10")) is OrderLifecycle.FILLED
    assert _resolve(local, _row(OrderLifecycle.PARTIALLY_FILLED, "10")) is OrderLifecycle.FILLED


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_working_row_with_a_partial_fill_is_still_partially_filled(
    local: OrderLifecycle,
) -> None:
    assert _resolve(local, _row(OrderLifecycle.SUBMITTED, "4")) is OrderLifecycle.PARTIALLY_FILLED
    assert (
        _resolve(local, _row(OrderLifecycle.PARTIALLY_FILLED, "4"))
        is OrderLifecycle.PARTIALLY_FILLED
    )


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_typed_filled_row_is_still_filled(local: OrderLifecycle) -> None:
    """The controls caught the packet's first draft, which guarded every terminal and froze this."""

    assert _resolve(local, _row(OrderLifecycle.FILLED, "10")) is OrderLifecycle.FILLED


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_rejected_or_cancelled_row_without_fills_keeps_its_terminal_write(
    local: OrderLifecycle, typed: OrderLifecycle
) -> None:
    """Q3: an exactly owned listed rejection (or cancel) with nothing filled is evidence."""

    assert _resolve(local, _row(typed, "0")) is typed


@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_no_listed_row_and_no_executions_leaves_the_intent_unresolved(
    local: OrderLifecycle,
) -> None:
    """Absence is not evidence of rejection: unchanged and locked."""

    assert _resolve(local) is None


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
def test_a_contradicted_row_does_not_hide_behind_another_orders_evidence(
    typed: OrderLifecycle,
) -> None:
    """Two owned rows are ambiguous (the old rule, untouched): still no update."""

    assert _resolve(OrderLifecycle.SUBMITTED, _row(typed, "10"), _row(typed, "10")) is None

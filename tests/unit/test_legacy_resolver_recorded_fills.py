"""LEGACY-5 (option B): ``recorded_filled_quantity`` on ``resolve_from_broker_evidence``.

A terminal listed row whose filled figure is BELOW the cumulative fills Chronos already recorded
settles nothing (the intent stays locked): the adapter reports filled 0 whenever its order-status
cache is empty, which is every order after a reconnect, and a terminal write at 0 would drop the
recorded fills and release the opening-order lock.

The keyword is optional and keyword-only. Omitting it, or passing ``None``, must leave EVERY cell of
the 110-cell lifecycle-first differential (``test_legacy_resolver_lifecycle_first``) byte-identical,
which is how the U-A/U-B planner (#302) and every other existing caller are unaffected. Where the
figure is supplied the rule is terminal rows only (FILLED, CANCELLED, REJECTED) and strictly below:
a row AT the recorded figure, or above it, is judged exactly as before.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tests.unit.test_legacy_resolver_lifecycle_first import (
    _DIFFERENTIAL,
    _DIFFERENTIAL_BLANK,
    ACCOUNT,
    BROKER_ID,
    CLIENT_ID,
    CONTRADICTED,
    LIMIT,
    NOW,
    PERM_ID,
    REF,
    SENT_ACTIVE,
    _intent,
    _row,
)

from chronos.domain.enums import OrderLifecycle
from chronos.orders.reconciliation_recovery import resolve_from_broker_evidence

_OMITTED: Any = object()
RECORDED = Decimal(3)  # the PARTIALLY_FILLED seed the integration rig records (3 of 10)


def _resolve(
    status: OrderLifecycle,
    row: Any,
    recorded: Any = _OMITTED,
) -> OrderLifecycle | None:
    extra: dict[str, Any] = {} if recorded is _OMITTED else {"recorded_filled_quantity": recorded}
    update = resolve_from_broker_evidence(
        _intent(status),
        open_orders=(row,),
        executions=(),
        current_account_id=ACCOUNT,
        expected_broker_client_id=CLIENT_ID,
        expected_limit_price=LIMIT,
        persisted_broker_order_id=BROKER_ID,
        persisted_permanent_id=PERM_ID,
        now=NOW,
        **extra,
    )
    return None if update is None else update.lifecycle


# --- the default keeps every existing cell identical ---------------------------------------------


@pytest.mark.parametrize("reference", ["exact", "blank-with-permid"])
@pytest.mark.parametrize("typed", list(OrderLifecycle), ids=lambda lifecycle: lifecycle.value)
def test_an_explicit_none_equals_omitting_it_across_the_differential(
    typed: OrderLifecycle, reference: str
) -> None:
    ref = REF if reference == "exact" else None
    table = _DIFFERENTIAL if reference == "exact" else _DIFFERENTIAL_BLANK
    got = tuple(
        _resolve(OrderLifecycle.SUBMITTED, _row(typed, filled, ref=ref), None)
        for filled in ("0", "1", "4", "9", "10")
    )
    assert got == table[typed]


# --- the rule: a terminal row strictly below the recorded fills settles nothing -------------------


@pytest.mark.parametrize("filled", ["0", "1", "2"])
@pytest.mark.parametrize(
    "typed", [*CONTRADICTED, OrderLifecycle.FILLED], ids=lambda lifecycle: lifecycle.value
)
@pytest.mark.parametrize("local", SENT_ACTIVE, ids=lambda lifecycle: lifecycle.value)
def test_a_terminal_row_below_the_recorded_fills_settles_nothing(
    local: OrderLifecycle, typed: OrderLifecycle, filled: str
) -> None:
    assert _resolve(local, _row(typed, filled), RECORDED) is None


def test_the_boundary_is_strictly_below_a_row_at_the_recorded_fills_is_judged_as_before() -> None:
    # typed FILLED at exactly the recorded figure is refined by quantity as it always was (3 of 10)
    at = _row(OrderLifecycle.FILLED, "3")
    assert _resolve(OrderLifecycle.SUBMITTED, at, RECORDED) is OrderLifecycle.PARTIALLY_FILLED
    assert _resolve(OrderLifecycle.SUBMITTED, at) is OrderLifecycle.PARTIALLY_FILLED
    # one below it settles nothing
    assert _resolve(OrderLifecycle.SUBMITTED, _row(OrderLifecycle.FILLED, "2"), RECORDED) is None


@pytest.mark.parametrize("typed", CONTRADICTED, ids=lambda lifecycle: lifecycle.value)
def test_nothing_recorded_means_a_terminal_row_at_zero_still_settles(typed: OrderLifecycle) -> None:
    for recorded in (Decimal(0), None, _OMITTED):
        assert _resolve(OrderLifecycle.SUBMITTED, _row(typed, "0"), recorded) is typed


def test_a_terminal_row_above_the_recorded_fills_settles_as_before() -> None:
    row = _row(OrderLifecycle.FILLED, "10")
    assert _resolve(OrderLifecycle.PARTIALLY_FILLED, row, RECORDED) is OrderLifecycle.FILLED


@pytest.mark.parametrize(
    "working",
    [OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED, OrderLifecycle.CANCEL_PENDING],
    ids=lambda lifecycle: lifecycle.value,
)
def test_a_working_row_is_never_touched_by_the_rule(working: OrderLifecycle) -> None:
    """Terminal rows only: a working row below the recorded figure is progress, judged as before."""

    row = _row(working, "1")
    assert _resolve(OrderLifecycle.PARTIALLY_FILLED, row, RECORDED) == _resolve(
        OrderLifecycle.PARTIALLY_FILLED, row
    )

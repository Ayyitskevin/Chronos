"""The fill-floor hold scenarios (FLOORHOLD): one per place the venue figure is refused for being
below a figure Chronos recorded or witnessed, each driven with exact, differing data.

A scenario takes any object with ``seed``/``event``/``pending``/``venue``/``repo``/``tracker``
(the settle rig, whether built by ``make_rig`` or around the order-pipeline harness) and leaves it
in the state the reconciler will refuse. ``basis``, ``floor`` and ``venue`` are what the typed hold
must say, as plain text; ``None`` means the intent is held for some other reason and must carry no
fill-floor hold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from chronos.domain.enums import OrderLifecycle as L
from chronos.orders.tracker import OrderStatusUpdate
from tests.support.settle_rig import (
    BROKER_ID,
    CLIENT_ID,
    EARLIER_TODAY,
    NOW,
    PERM_ID,
    PRIOR,
    QUANTITY,
    venue_execution,
    venue_order,
)


@dataclass(frozen=True, slots=True)
class FloorScenario:
    name: str
    setup: Callable[[Any], None]
    basis: str | None
    floor: str | None
    venue: str | None
    reason_fragment: str


def _recorded_seven(rig: Any, status: L) -> None:
    rig.seed()
    rig.event(status, at=EARLIER_TODAY, filled="7", source="FILL_PROGRESS")


def legacy_recorded_fills(rig: Any) -> None:
    """Legacy resolver: 7 recorded on a SUBMITTED intent, the venue's terminal row says 0."""

    _recorded_seven(rig, L.SUBMITTED)
    rig.venue.open = [venue_order(L.CANCELLED, "0")]


def legacy_snapshot_executions(rig: Any) -> None:
    """Legacy resolver: a row-bound blank-reference execution of 5, a terminal row of 0."""

    rig.seed()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = [venue_execution("blank", "5", PRIOR, ref=None, perm=None)]


def planner_witnessed_terminal(rig: Any) -> None:
    """Planner: recorded 7 and CANCEL_PENDING, the venue's terminal row ends at 5."""

    _recorded_seven(rig, L.PARTIALLY_FILLED)
    rig.pending(at=EARLIER_TODAY)
    rig.venue.open = [venue_order(L.CANCELLED, "5")]
    rig.venue.execs = [venue_execution("venue-five", "5", EARLIER_TODAY)]


def planner_row_bound(rig: Any) -> None:
    """Planner: a row-bound blank-reference execution of 4 against a terminal row of 0."""

    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = [venue_execution("blank", "4", PRIOR, ref=None, perm=None)]


def planner_row_bound_over_quantity(rig: Any) -> None:
    """The #361 quantity bound: row-bound executions sum to 10.00000001 of a 10 share order."""

    rig.seed()
    rig.pending()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    rig.venue.execs = [
        venue_execution("blank-1", "6", PRIOR, ref=None, perm=None),
        venue_execution("blank-2", "4.00000001", PRIOR, ref=None, perm=None),
    ]


def planner_working_order(rig: Any) -> None:
    """Planner, working-order variant: 3 before the session plus 2 today, the venue says 4."""

    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="3", source="FILL_PROGRESS")
    rig.pending()
    rig.venue.execs = [venue_execution("e1", "2", EARLIER_TODAY)]
    rig.venue.open = [venue_order(L.PARTIALLY_FILLED, "4")]


def atomic_floor_on_planned_write(rig: Any) -> None:
    """Planner write: a fill of 1 lands between the confirming re-plan and the CANCELLED@0 write."""

    rig.seed()
    rig.pending()
    rig.venue.completed = [venue_order(L.CANCELLED, "0")]
    real = rig.repo.record_transition
    injected = False

    def record_after_late_fill(**kwargs: Any) -> bool:
        nonlocal injected
        if kwargs.get("to_status") is L.CANCELLED and not injected:
            injected = True
            rig.tracker.ingest(
                OrderStatusUpdate(
                    intent_id="intent-1",
                    broker_order_id=BROKER_ID,
                    permanent_id=PERM_ID,
                    client_id=CLIENT_ID,
                    lifecycle=L.CANCEL_PENDING,
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("9"),
                    occurred_at=NOW,
                ),
                current_account_id=rig.account,
            )
        return real(**kwargs)

    rig.repo.record_transition = record_after_late_fill


def atomic_floor_on_legacy_write(rig: Any) -> None:
    """Legacy write: a fill of 5 lands after the pass read a recorded figure of 0."""

    rig.seed()
    rig.event(L.SUBMISSION_UNKNOWN, at=PRIOR)
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    tracker = rig.tracker
    real = tracker.recorded_filled_quantity
    reads = 0

    def read_then_fill(intent_id: str, *, current_account_id: str) -> Decimal:
        nonlocal reads
        value = real(intent_id, current_account_id=current_account_id)
        reads += 1
        if reads == 1:
            tracker.ingest(
                OrderStatusUpdate(
                    intent_id="intent-1",
                    broker_order_id=BROKER_ID,
                    permanent_id=PERM_ID,
                    client_id=CLIENT_ID,
                    lifecycle=L.PARTIALLY_FILLED,
                    filled_quantity=Decimal(5),
                    remaining_quantity=QUANTITY - 5,
                    occurred_at=NOW,
                ),
                current_account_id=current_account_id,
            )
        return value

    tracker.recorded_filled_quantity = read_then_fill


def held_but_not_below_a_figure(rig: Any) -> None:
    """Recorded 7, CANCEL_PENDING, and the venue shows nothing at all: held, but no venue figure
    is below anything, so this is not a fill-floor hold."""

    _recorded_seven(rig, L.PARTIALLY_FILLED)
    rig.pending(at=EARLIER_TODAY)


def held_on_untyped_fills(rig: Any) -> None:
    """A terminal row that reports fills is locked by the D-27 rule, not by a floor."""

    rig.seed()
    rig.venue.open = [venue_order(L.CANCELLED, "5")]


SCENARIOS: tuple[FloorScenario, ...] = (
    FloorScenario(
        "legacy-recorded-fills",
        legacy_recorded_fills,
        "RECORDED_FILLS",
        "7",
        "0",
        "below the 7 filled already recorded",
    ),
    FloorScenario(
        "legacy-snapshot-executions",
        legacy_snapshot_executions,
        "SNAPSHOT_EXECUTIONS",
        "5",
        "0",
        "snapshot fill conflict: terminal venue figure is below",
    ),
    FloorScenario(
        "planner-witnessed-terminal",
        planner_witnessed_terminal,
        "WITNESSED_LOWER_BOUND",
        "7",
        "5",
        "venue fill figure 5 is below the witnessed lower bound",
    ),
    FloorScenario(
        "planner-row-bound",
        planner_row_bound,
        "ROW_BOUND_EXECUTIONS",
        "4",
        "0",
        "from row-bound snapshot executions",
    ),
    FloorScenario(
        "planner-row-bound-over-quantity",
        planner_row_bound_over_quantity,
        "ROW_BOUND_EXECUTIONS",
        "10.00000001",
        "0",
        "exceed the order quantity",
    ),
    FloorScenario(
        "planner-working-order",
        planner_working_order,
        "WITNESSED_LOWER_BOUND",
        "5",
        "4",
        "venue fill figure 4 is below the witnessed lower bound",
    ),
    FloorScenario(
        "atomic-floor-on-planned-write",
        atomic_floor_on_planned_write,
        "WRITE_FLOOR",
        "1",
        "0",
        "fill_floor_conflict: RecordedFillsAdvanced",
    ),
    FloorScenario(
        "atomic-floor-on-legacy-write",
        atomic_floor_on_legacy_write,
        "WRITE_FLOOR",
        "5",
        "0",
        "recorded fills advanced",
    ),
    FloorScenario(
        "held-but-not-below-a-figure",
        held_but_not_below_a_figure,
        None,
        None,
        None,
        "order absent",
    ),
    FloorScenario(
        "held-on-untyped-fills",
        held_on_untyped_fills,
        None,
        None,
        None,
        "not a working lifecycle",
    ),
)

HOLD_SCENARIOS = tuple(s for s in SCENARIOS if s.basis is not None)

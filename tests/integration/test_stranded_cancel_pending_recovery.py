"""Q-Q3-1, re-pointed onto the real recovery path (Muse's ruling, 2026-10-14).

The two strict xfails in ``tests/integration/test_cancel_refusal_tracker.py`` pin paths U-B
deliberately does not take: a direct ``tracker.ingest`` of a venue SUBMITTED (the state machine
has no CANCEL_PENDING -> SUBMITTED edge, by design) and a second cancel while CANCEL_PENDING
(refused). Under U-B (design r3 §4.3) a stranded CANCEL_PENDING whose order still works at the
venue is recovered by RECONCILIATION after the dwell, through the repository CAS. These pins prove
that, on U-B's own rig, with the stranded mark written by the REAL ``OrderCancellationService``
(an ambiguous cancel: the connection drops during the send, so the mark stays) rather than by
the rig's shortcut.

Times: the cancel runs at ``PRIOR`` (older than the mark floor ``M`` = 60 s); reconciliation pass
``k`` runs at ``NOW + 120 s * k`` with the monotonic clock at ``1000 + 120 k`` (the rig's
cadence), so pass 1 is the first at least ``D`` = 90 s after pass 0. Fake-proven only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from tests.integration.test_cancel_pending_recovery import CP, SUBMITTED, World
from tests.support.settle_rig import NOW, PRIOR

from chronos.broker.base import BrokerConnectionError
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import CancellationResult
from chronos.orders.mutations import OrderCancellationService, OrderMutationError


@dataclass
class _Cancels:
    """How the scripted venue answers a cancel, and every cancel that reached it."""

    mode: str = "ambiguous"
    calls: list[int] = field(default_factory=list)


@pytest.fixture
def world() -> Iterator[tuple[World, _Cancels]]:
    """U-B's rig, whose scripted venue answers a cancel ambiguously until told to accept."""

    w = World()
    cancels = _Cancels()

    async def cancel_order(broker_order_id: int) -> CancellationResult:
        cancels.calls.append(broker_order_id)
        if cancels.mode == "ambiguous":
            raise BrokerConnectionError("connection lost during the cancel; state unknown")
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=OrderLifecycle.CANCELLED,
            message="accepted",
            timestamp=NOW,
        )

    venue: Any = w.venue
    venue.cancel_order = cancel_order
    try:
        yield w, cancels
    finally:
        w.rig.close()


def _service(w: World) -> OrderCancellationService:
    r = w.rig
    return OrderCancellationService(
        connection=r.connection, intents=r.intents, tracker=r.tracker, tracker_repo=r.repo
    )


def _strand(w: World, cancels: _Cancels, service: OrderCancellationService) -> None:
    """The REAL service writes the CANCEL mark, then the send is ambiguous: CANCEL_PENDING stays."""

    assert w.rig.status() is SUBMITTED
    with pytest.raises(BrokerConnectionError):
        service.cancel("intent-1", current_account_id=w.rig.account, now=PRIOR)
    assert w.rig.status() is CP
    mark = [e for e in w.rig.events() if e.to_status is CP][-1]
    assert (mark.source, mark.from_status) == ("CANCEL", SUBMITTED)  # the service's own mark
    assert mark.evidence.get("phase") == "cancel_requested"
    assert cancels.calls  # the send really left (ambiguous), so the mark is not reverted


def test_v1_a_stranded_cancel_pending_recovers_through_reconciliation_after_the_dwell(
    world: tuple[World, _Cancels],
) -> None:
    """Pin 1, re-pointed: the venue shows the order working at 0 fills; the first
    reconciliation pass writes nothing; the next one (after the dwell) recovers to SUBMITTED
    with exactly one RECOVERY event. Never through ``tracker.ingest``."""

    w, cancels = world
    _strand(w, cancels, _service(w))
    w.working("0")
    w.at(0)
    assert w.rig.status() is CP  # the first observation writes nothing (the dwell starts)
    assert w.recoveries() == []
    w.at(1)
    assert w.rig.status() is SUBMITTED, "the stranded CANCEL_PENDING was not recovered"
    assert w.recoveries() == [(CP, SUBMITTED, Decimal("0"))]  # exactly one RECOVERY event


def test_v2_a_second_cancel_is_refused_while_pending_and_succeeds_after_the_recovery(
    world: tuple[World, _Cancels],
) -> None:
    """Pin 2, re-pointed: a second cancel BEFORE recovery is refused ("already pending", by
    design: CANCEL_PENDING is not cancellable); after the reconciler recovers the intent to
    SUBMITTED, the same cancel succeeds and the intent reaches CANCELLED."""

    w, cancels = world
    service = _service(w)
    _strand(w, cancels, service)
    w.working("0")
    w.at(0)
    sends_before = len(cancels.calls)
    with pytest.raises(OrderMutationError, match="already pending"):
        service.cancel("intent-1", current_account_id=w.rig.account, now=NOW)
    assert len(cancels.calls) == sends_before  # the refusal sent nothing
    assert w.rig.status() is CP
    w.at(1)
    assert w.rig.status() is SUBMITTED
    cancels.mode = "accept"
    result = service.cancel(
        "intent-1", current_account_id=w.rig.account, now=NOW + timedelta(seconds=300)
    )
    assert result.lifecycle is OrderLifecycle.CANCELLED
    assert w.rig.status() is OrderLifecycle.CANCELLED
    assert len(cancels.calls) == sends_before + 1  # exactly one more send

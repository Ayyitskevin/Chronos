"""Test doubles for ``OrderOwnershipLedger`` (S1-F1, Q-P1 Option A).

The real ledger is ``chronos.orders.ownership.PersistedOrderOwnershipLedger`` over the persisted
order book (tested against a real in-memory database in
``tests/integration/test_order_ownership_ledger.py``). The adapter tests need ledgers whose
answers are scripted and whose calls are recorded:

* :class:`ExactLedger` knows a set of ``VenueOrderIdentity`` records and answers True only for an
  EXACT match of all five fields (frozen-dataclass equality), like the real one;
* :class:`RaisingLedger` and :class:`AnswerLedger` script the failure and the odd-answer paths;
* :func:`seeded_identity` reads the Chronos order the FakeGateway seed holds the way the VENUE
  reports it, so the positive control compares what the venue says with what the ledger knows.
"""

from __future__ import annotations

from typing import Any

from chronos.broker.base import VenueOrderIdentity


class ExactLedger:
    def __init__(self, *known: VenueOrderIdentity) -> None:
        self.known = set(known)
        self.calls: list[VenueOrderIdentity] = []

    def proves_owned(self, identity: VenueOrderIdentity) -> bool:
        self.calls.append(identity)
        return identity in self.known


class RaisingLedger:
    def __init__(self) -> None:
        self.calls = 0

    def proves_owned(self, identity: VenueOrderIdentity) -> bool:
        del identity
        self.calls += 1
        raise RuntimeError("the order book is unavailable")


class AnswerLedger:
    """Returns whatever it was given, however odd (None, a truthy string, 1)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    def proves_owned(self, identity: VenueOrderIdentity) -> bool:
        del identity
        self.calls += 1
        return self.answer  # type: ignore[no-any-return]


def seeded_identity(gateway: Any, broker_order_id: int, *, client_id: int) -> VenueOrderIdentity:
    """The identity the venue reports for a seeded order (read from the fake's own book)."""

    from tests.support.fake_ibapi import ACCOUNT

    order = next(
        order
        for order in gateway._run(gateway.sim.open_orders())
        if order.broker_order_id == broker_order_id
    )
    assert order.order_ref and order.permanent_id, "the seed order has no ref or permId"
    return VenueOrderIdentity(
        broker_order_id=broker_order_id,
        order_ref=order.order_ref,
        account_id=ACCOUNT,
        client_id=client_id,
        permanent_id=order.permanent_id,
    )

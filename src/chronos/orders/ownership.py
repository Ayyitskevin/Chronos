"""The durable proof that an order on the venue is one Chronos submitted (S1-F1, Q-P1 Option A).

``PersistedOrderOwnershipLedger`` answers :class:`~chronos.broker.base.OrderOwnershipLedger` over
the PERSISTED order book (``order_intents`` + ``order_events``; no schema change). A venue row is
ours only if one persisted intent matches ALL of: the exact ``order_ref`` and account, and, across
every event of that intent, exactly one distinct broker order id, one permId and one client id,
each equal to what the venue reports. Everything else is False, including:

* an intent with no permId yet (an unresolved ``SUBMISSION_UNKNOWN``; the cancel is manual in TWS);
* an intent with no broker order id, or with two DISTINCT values of any field (a conflict is
  evidence, not a choice: never the first, never the latest, as ``OrderIdentityConflict``).

A database or scope error is NOT caught here; the adapter turns it into a refusal.
"""

from __future__ import annotations

from chronos.broker.base import VenueOrderIdentity
from chronos.persistence.order_repositories import OrderIntentRepository, OrderTrackerRepository
from chronos.utils.identifiers import account_fingerprint


class PersistedOrderOwnershipLedger:
    def __init__(self, intents: OrderIntentRepository, tracker: OrderTrackerRepository) -> None:
        self._intents = intents
        self._tracker = tracker

    def proves_owned(self, identity: VenueOrderIdentity) -> bool:
        intent = self._intents.by_order_ref(
            identity.order_ref, current_account_id=identity.account_id
        )
        if intent is None or intent.order_ref != identity.order_ref:
            return False
        if intent.account_fingerprint != account_fingerprint(identity.account_id):
            return False
        events = self._tracker.events(intent.intent_id, current_account_id=identity.account_id)
        return (
            {e.broker_order_id for e in events if e.broker_order_id is not None}
            == {identity.broker_order_id}
            and {e.permanent_id for e in events if e.permanent_id is not None}
            == {identity.permanent_id}
            and {e.client_id for e in events if e.client_id is not None} == {identity.client_id}
        )

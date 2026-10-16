"""The real ``PersistedOrderOwnershipLedger`` over a real (in-memory) order book.

An order is submitted through the real boundary (FakeBroker double, no network), so the persisted
intent and its SUBMITTED event carry the ref, broker id, permId and client id exactly as
production writes them; the ledger is then asked about identities built from what a venue row
would say. Exact match is True; every single-field deviation, a missing permId, a conflict and an
unknown ref are False; a wrong account scope raises (the adapter turns that into a refusal).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from tests.integration.test_order_pipeline import (
    _drive_to_confirmed,
    _Harness,
    _short_put_intent,
)
from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker, paper_settings
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

from chronos.broker.base import VenueOrderIdentity
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import OrderRequest, OrderSubmission
from chronos.orders.ownership import PersistedOrderOwnershipLedger
from chronos.utils.identifiers import account_fingerprint


class _NoPermIdBroker(FakeBroker):
    """The submit ack carried no permId (the gateway had not assigned one yet)."""

    async def submit_order(self, request: OrderRequest, **kwargs: Any) -> OrderSubmission:
        submission = await super().submit_order(request, **kwargs)
        return submission.model_copy(update={"permanent_id": None})


def _rig(broker: FakeBroker) -> tuple[_Harness, PersistedOrderOwnershipLedger, VenueOrderIdentity]:
    harness = _Harness(broker, paper_settings())
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.order_ref
    ledger = PersistedOrderOwnershipLedger(
        harness.intents, harness.tracker_repo, persistence_key=TEST_PERSISTENCE_KEY
    )
    identity = VenueOrderIdentity(
        broker_order_id=9001,
        order_ref=stored.order_ref,
        account_id=PAPER_ACCOUNT,
        client_id=17,
        permanent_id=109001,
    )
    return harness, ledger, identity


@pytest.fixture
def rig() -> Iterator[tuple[_Harness, PersistedOrderOwnershipLedger, VenueOrderIdentity]]:
    harness, ledger, identity = _rig(FakeBroker())
    try:
        yield harness, ledger, identity
    finally:
        harness.close()


def test_the_exact_persisted_identity_is_proven_owned(rig: Any) -> None:
    _harness, ledger, identity = rig
    assert ledger.proves_owned(identity) is True


@pytest.mark.parametrize(
    "field,wrong",
    [
        ("order_ref", "CHR-ORD-" + "0" * 32),
        ("broker_order_id", 9002),
        ("client_id", 18),
        ("permanent_id", 109002),
    ],
)
def test_one_wrong_field_is_not_owned(rig: Any, field: str, wrong: object) -> None:
    _harness, ledger, identity = rig
    assert ledger.proves_owned(replace(identity, **{field: wrong})) is False


def test_a_wrong_account_scope_raises_rather_than_answering(rig: Any) -> None:
    _harness, ledger, identity = rig
    with pytest.raises(Exception):  # noqa: B017 (the exact type is the repository's)
        ledger.proves_owned(replace(identity, account_id="DU0000000"))


@pytest.mark.parametrize(
    "field,other", [("broker_order_id", 9002), ("permanent_id", 109002), ("client_id", 18)]
)
def test_a_second_distinct_value_of_any_identity_field_is_a_conflict_and_not_owned(
    rig: Any, field: str, other: int
) -> None:
    """Never the first, never the latest: the contradiction itself refuses (OrderIdentityConflict
    policy), for the broker id and the client id as well as the permId."""

    harness, ledger, identity = rig
    values = {"broker_order_id": 9001, "permanent_id": 109001, "client_id": 17}
    values[field] = other
    assert harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key=f"intent-1:conflicting-{field}",
        source="CALLBACK",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        evidence={},
        occurred_at=FIXED_NOW,
        **values,
    )
    assert ledger.proves_owned(identity) is False


def test_an_order_whose_perm_id_was_never_persisted_is_not_owned() -> None:
    harness, ledger, identity = _rig(_NoPermIdBroker())
    try:
        assert ledger.proves_owned(identity) is False
    finally:
        harness.close()


# --- r3: the two re-checks the repositories already imply, pinned against a lying repository ---


class _Event:
    def __init__(self, **fields: int | None) -> None:
        self.broker_order_id = fields.get("broker_order_id", 9001)
        self.permanent_id = fields.get("permanent_id", 109001)
        self.client_id = fields.get("client_id", 17)


class _StubIntents:
    """A repository that returns whatever it is told, even the wrong record."""

    def __init__(self, *, order_ref: str, fingerprint: str) -> None:
        self._record = SimpleNamespace(
            intent_id="intent-1", order_ref=order_ref, account_fingerprint=fingerprint
        )

    def by_order_ref(self, order_ref: str, *, current_account_id: str) -> Any:
        return self._record


class _StubTracker:
    def events(self, intent_id: str, *, current_account_id: str) -> tuple[_Event, ...]:
        return (_Event(),)


_STUB_IDENTITY = VenueOrderIdentity(
    broker_order_id=9001,
    order_ref="CHR-ORD-" + "a" * 32,
    account_id=PAPER_ACCOUNT,
    client_id=17,
    permanent_id=109001,
)


def _stub_ledger(*, order_ref: str, fingerprint: str) -> PersistedOrderOwnershipLedger:
    return PersistedOrderOwnershipLedger(
        _StubIntents(order_ref=order_ref, fingerprint=fingerprint),  # type: ignore[arg-type]
        _StubTracker(),
        persistence_key=TEST_PERSISTENCE_KEY,  # type: ignore[arg-type]
    )


def test_the_ledger_does_not_trust_the_repository_to_return_the_ref_it_was_asked_for() -> None:
    """Positive control, then the lie: another order_ref, everything else right."""

    right = _stub_ledger(
        order_ref=_STUB_IDENTITY.order_ref,
        fingerprint=account_fingerprint(PAPER_ACCOUNT, key=TEST_PERSISTENCE_KEY),
    )
    assert right.proves_owned(_STUB_IDENTITY) is True
    wrong = _stub_ledger(
        order_ref="CHR-ORD-" + "b" * 32,
        fingerprint=account_fingerprint(PAPER_ACCOUNT, key=TEST_PERSISTENCE_KEY),
    )
    assert wrong.proves_owned(_STUB_IDENTITY) is False


def test_the_ledger_does_not_trust_the_repository_to_return_the_right_accounts_record() -> None:
    wrong = _stub_ledger(
        order_ref=_STUB_IDENTITY.order_ref,
        fingerprint=account_fingerprint("DU0000000", key=TEST_PERSISTENCE_KEY),
    )
    assert wrong.proves_owned(_STUB_IDENTITY) is False

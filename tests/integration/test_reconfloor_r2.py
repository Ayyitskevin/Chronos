"""The ruled planner, reused-ID, capture-order and operator-path folds."""

import logging
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    BROKER_ID,
    CLIENT_ID,
    PERM_ID,
    PRIOR,
    Rig,
    contract,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L
from chronos.domain.enums import OrderSide
from chronos.orders.reconciliation_recovery import RECOVERY_SOURCE


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("status", [L.CANCEL_PENDING, L.PARTIALLY_FILLED, L.SUBMITTED])
@pytest.mark.parametrize("source", ["listed", "completed-zero-id"])
@pytest.mark.parametrize("figure", ["0", "3"])
def test_planned_terminal_cannot_drop_row_bound_snapshot_executions(
    rig: Rig, status: L, source: str, figure: str
) -> None:
    rig.seed()
    if status is L.SUBMITTED:
        rig.pending()
        rig.event(status, at=PRIOR, source=RECOVERY_SOURCE)
    else:
        rig.event(status, at=PRIOR)
    before = rig.events()
    row = venue_order(L.CANCELLED, figure, broker_id=0 if source == "completed-zero-id" else BROKER_ID)
    if source == "listed":
        rig.venue.open = [row]
    else:
        rig.venue.completed = [row]
    rig.venue.execs = [
        venue_execution("blank-3", "3", PRIOR, ref=None, perm=None),
        venue_execution("blank-2", "2", PRIOR, ref=None, perm=None),
    ]

    report = rig.reconcile()

    assert rig.status() is status and rig.events() == before
    assert report.applied_updates == () and not report.permits_new_opening_order
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


@pytest.mark.parametrize("kind", ["foreign-reference", "other-contract", "other-side", "other-permid"])
def test_reused_broker_id_does_not_count_another_orders_execution(rig: Rig, kind: str) -> None:
    rig.seed()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    execution = venue_execution("unrelated", "5", PRIOR, ref=None, perm=None)
    changes = {
        "foreign-reference": {"order_ref": "CHR-OTHER", "permanent_id": PERM_ID + 1},
        "other-contract": {"contract": contract(con_id=999001, symbol="OTHER")},
        "other-side": {"side": OrderSide.SELL},
        "other-permid": {"permanent_id": PERM_ID + 1},
    }
    rig.venue.execs = [execution.model_copy(update=changes[kind])]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and report.unresolved == ()
    assert len(report.applied_updates) == 1


def test_reused_id_for_a_real_second_intent_does_not_hold_either_intent(rig: Rig) -> None:
    rig.seed()
    other_ref = "CHR-OTHER-INTENT"
    rig.seed(intent_id="intent-2", ref=other_ref, permanent_id=PERM_ID + 1)
    rig.venue.open = [
        venue_order(L.CANCELLED, "0"),
        venue_order(L.FILLED, "10", ref=other_ref, perm=PERM_ID + 1),
    ]
    rig.venue.execs = [venue_execution("other-full", "10", PRIOR, ref=other_ref, perm=PERM_ID + 1)]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and rig.status("intent-2") is L.FILLED
    assert {item.intent_id for item in report.applied_updates} == {"intent-1", "intent-2"}
    assert report.unresolved == ()


def test_own_full_fill_is_not_overcounted_with_a_foreign_fill_on_reused_id(rig: Rig) -> None:
    rig.seed()
    rig.venue.open = [venue_order(L.FILLED, "10")]
    rig.venue.execs = [
        venue_execution("own-full", "10", PRIOR),
        venue_execution("foreign-five", "5", PRIOR, ref="CHR-OTHER", perm=PERM_ID + 1),
    ]

    report = rig.reconcile()

    assert rig.status() is L.FILLED and report.unresolved == ()
    assert rig.events()[-1].filled_quantity == Decimal("10")


@pytest.mark.parametrize("evidence", ["executions", "terminal-row"])
def test_operator_unknown_resolution_refuses_two_persisted_broker_ids(
    rig: Rig, evidence: str
) -> None:
    rig.seed()
    rig.event(L.SUBMISSION_UNKNOWN, at=PRIOR)
    other_id = BROKER_ID + 77
    assert rig.repo.record_transition(
        intent_id="intent-1", event_key="other-broker", source="TEST",
        from_status=L.SUBMISSION_UNKNOWN, to_status=L.SUBMISSION_UNKNOWN,
        current_account_id=rig.account, broker_order_id=other_id, occurred_at=PRIOR,
    )
    before = rig.events()
    if evidence == "executions":
        rig.venue.execs = [venue_execution("latest-full", "10", PRIOR).model_copy(
            update={"broker_order_id": other_id}
        )]
    else:
        rig.venue.open = [venue_order(L.CANCELLED, "0", broker_id=other_id)]

    with pytest.raises(ValueError, match="persisted order identity is contradictory.*broker order ids"):
        rig.reconciler.operator_resolve(
            "intent-1", operator_note="synthetic verification", current_account_id=rig.account,
            now=PRIOR + timedelta(days=3),
        )
    assert rig.status() is L.SUBMISSION_UNKNOWN and rig.events() == before


def test_first_permid_after_row_read_cannot_be_folded_into_the_captured_snapshot(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.seed(permanent_id=None)
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
    rig.venue.execs = [venue_execution("own-full", "10", PRIOR)]
    original = rig.tracker.permanent_id
    after_race = []

    def racing(intent_id, **kwargs):
        value = original(intent_id, **kwargs)
        if not after_race:
            assert value is None
            assert rig.repo.record_transition(
                intent_id="intent-1", event_key="first-permid", source="IDENTITY",
                from_status=L.PARTIALLY_FILLED, to_status=L.PARTIALLY_FILLED,
                current_account_id=rig.account, permanent_id=PERM_ID + 1, occurred_at=PRIOR,
            )
            after_race.append(rig.events())
        return value

    monkeypatch.setattr(rig.tracker, "permanent_id", racing)
    report = rig.reconcile()

    assert len(after_race) == 1 and rig.events() == after_race[0]
    assert rig.status() is L.PARTIALLY_FILLED and report.applied_updates == ()
    assert "identity history advanced" in report.unresolved[0].reason


def test_scope_n6_another_intents_reference_never_seeds_our_inferred_permid_set(rig: Rig) -> None:
    rig.seed(permanent_id=None)
    other_ref = "CHR-OTHER-INTENT"
    rig.seed(intent_id="intent-2", ref=other_ref, broker_id=BROKER_ID + 1, permanent_id=PERM_ID + 1)
    rig.venue.open = [
        venue_order(L.CANCELLED, "0"),
        venue_order(L.SUBMITTED, "0", ref=other_ref, broker_id=BROKER_ID + 1, perm=PERM_ID + 1),
    ]
    rig.venue.execs = [venue_execution("other-five", "5", PRIOR, ref=other_ref, perm=PERM_ID + 1).model_copy(
        update={"broker_order_id": BROKER_ID + 1}
    )]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and rig.status("intent-2") is L.SUBMITTED
    assert [item.intent_id for item in report.applied_updates] == ["intent-1"]
    assert report.unresolved == ()


def test_lifecycle_warning_uses_current_terminal_status_after_a_race(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig.seed()
    rig.venue.open = [venue_order(L.FILLED, "10")]
    original = rig.tracker.ingest

    def racing(update, **kwargs):
        assert rig.repo.record_transition(
            intent_id="intent-1", event_key="late-cancel", source="TEST",
            from_status=L.SUBMITTED, to_status=L.CANCELLED, current_account_id=rig.account,
            filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"), occurred_at=PRIOR,
        )
        return original(update, **kwargs)

    monkeypatch.setattr(rig.tracker, "ingest", racing)
    with caplog.at_level(logging.WARNING, logger="chronos.orders.reconciliation_recovery"):
        report = rig.reconcile()

    records = [r for r in caplog.records if r.name == "chronos.orders.reconciliation_recovery"]
    assert len(records) == 1 and "local=CANCELLED observed=FILLED" in records[0].getMessage()
    assert "local=SUBMITTED" not in records[0].getMessage()
    assert rig.status() is L.CANCELLED and report.unresolved


@pytest.mark.parametrize("status", [L.SUBMITTED, L.SUBMISSION_UNKNOWN])
def test_unplanned_status_keeps_no_identity_snapshot_and_can_learn_first_ids(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, status: L
) -> None:
    rig.seed(permanent_id=None, broker_id=None, client_id=None)
    if status is L.SUBMISSION_UNKNOWN:
        rig.event(status, at=PRIOR)
    rig.venue.open = [venue_order(L.CANCELLED, "0")]

    def forbidden(*args, **kwargs):
        pytest.fail("unplanned identity snapshots are outside this ruling")

    monkeypatch.setattr(rig.tracker, "persisted_identities", forbidden)
    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and report.unresolved == ()

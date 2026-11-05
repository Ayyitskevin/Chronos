"""Refusal-only RECONFLOOR integration and missing-identity regressions."""

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
    REF,
    Rig,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.domain.enums import OrderLifecycle as L
from chronos.orders.state_machine import OrderLifecycleError


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("status", [L.SUBMITTED, L.SUBMISSION_UNKNOWN])
@pytest.mark.parametrize(
    ("persisted_perm", "execution_perm"),
    [(None, PERM_ID), (PERM_ID, None), (None, None)],
    ids=["row-permid", "broker-client-with-persisted-permid", "broker-client-only"],
)
def test_blank_reference_executions_still_prevent_an_undercounted_terminal(
    rig: Rig, status: L, persisted_perm: int | None, execution_perm: int | None
) -> None:
    rig.seed(permanent_id=persisted_perm)
    if status is L.SUBMISSION_UNKNOWN:
        rig.event(status, at=PRIOR)
    before = rig.events()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    execution = venue_execution("blank-fill", "5", PRIOR, ref=None, perm=execution_perm)
    if persisted_perm is None and execution_perm == PERM_ID:
        execution = execution.model_copy(update={"broker_order_id": BROKER_ID + 9})
    rig.venue.execs = [execution]

    report = rig.reconcile()

    assert rig.status() is status and rig.events() == before
    assert report.applied_updates == () and not report.permits_new_opening_order
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


@pytest.mark.parametrize("foreign", [False, True], ids=["own-row", "foreign-row"])
def test_own_execution_permid_reveals_conflicting_reference_without_persisted_permid(
    rig: Rig, foreign: bool
) -> None:
    rig.seed(permanent_id=None)
    before = rig.events()
    other_ref = "CHR-OTHER-SNAPSHOT"
    rig.venue.open = [venue_order(L.SUBMITTED, "0", ref=other_ref, client=CLIENT_ID + int(foreign))]
    rig.venue.execs = [venue_execution("own-full", "10", PRIOR)]

    report = rig.reconcile()

    assert rig.status() is L.SUBMITTED and rig.events() == before
    assert report.unresolved[0].reason.startswith("identity conflict:")
    if foreign:
        assert all(value not in report.unresolved[0].reason for value in (other_ref, str(PERM_ID)))
    else:
        assert other_ref in report.unresolved[0].reason
        assert str(PERM_ID) in report.unresolved[0].reason


def test_conflicting_execution_is_not_dropped_when_full_own_executions_exist(rig: Rig) -> None:
    rig.seed()
    before = rig.events()
    other_ref = "CHR-OTHER-EXECUTION"
    rig.venue.execs = [
        venue_execution("own-full", "10", PRIOR),
        venue_execution("other-fill", "1", PRIOR, ref=other_ref).model_copy(
            update={"client_id": CLIENT_ID + 1, "broker_order_id": BROKER_ID + 7}
        ),
    ]

    report = rig.reconcile()

    assert rig.status() is L.SUBMITTED and rig.events() == before
    assert report.unresolved[0].reason.startswith("identity conflict:")
    assert all(value not in report.unresolved[0].reason for value in (other_ref, str(PERM_ID)))


@pytest.mark.parametrize("lifecycle,figure", [(L.FILLED, "3"), (L.REJECTED, "0")])
def test_every_terminal_type_is_bounded_by_the_sum_not_the_largest_execution(
    rig: Rig, lifecycle: L, figure: str
) -> None:
    rig.seed()
    before = rig.events()
    rig.venue.open = [venue_order(lifecycle, figure)]
    rig.venue.execs = [venue_execution("fill-3", "3", PRIOR), venue_execution("fill-2", "2", PRIOR)]

    report = rig.reconcile()

    assert rig.status() is L.SUBMITTED and rig.events() == before
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


def test_nonterminal_row_is_not_held_by_the_terminal_snapshot_floor(rig: Rig) -> None:
    rig.seed()
    before = rig.events()
    rig.venue.open = [venue_order(L.SUBMITTED, "0")]
    rig.venue.execs = [venue_execution("own-fill", "5", PRIOR)]

    report = rig.reconcile()

    assert rig.status() is L.SUBMITTED and rig.events() == before
    assert report.unresolved == ()
    assert [item.intent_id for item in report.proven] == ["intent-1"]


def test_another_intents_executions_do_not_hold_a_clean_terminal(rig: Rig) -> None:
    rig.seed()
    other_ref = "CHR-ORD-" + "T" * 32
    rig.seed(intent_id="intent-2", ref=other_ref, broker_id=BROKER_ID + 1, permanent_id=PERM_ID + 1)
    rig.venue.open = [
        venue_order(L.CANCELLED, "0"),
        venue_order(L.SUBMITTED, "0", ref=other_ref, broker_id=BROKER_ID + 1, perm=PERM_ID + 1),
    ]
    rig.venue.execs = [
        venue_execution("other-fill", "5", PRIOR, ref=other_ref, perm=PERM_ID + 1).model_copy(
            update={"broker_order_id": BROKER_ID + 1}
        )
    ]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED
    assert rig.status("intent-2") is L.SUBMITTED
    assert [item.intent_id for item in report.applied_updates] == ["intent-1"]
    assert report.unresolved == ()


def test_contained_lifecycle_conflict_emits_one_classified_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
    rig.venue.open = [venue_order(L.SUBMITTED, "0")]
    broker_text = "foreign-broker-text-must-not-enter-logs"

    def refuse(*args: object, **kwargs: object) -> None:
        raise OrderLifecycleError(broker_text)

    monkeypatch.setattr(rig.tracker, "ingest", refuse)
    with caplog.at_level(logging.WARNING, logger="chronos.orders.reconciliation_recovery"):
        report = rig.reconcile()

    assert rig.status() is L.PARTIALLY_FILLED and report.unresolved
    records = [r for r in caplog.records if r.name == "chronos.orders.reconciliation_recovery"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert all(
        value in records[0].getMessage() for value in ("intent-1", "PARTIALLY_FILLED", "SUBMITTED")
    )
    assert broker_text not in records[0].getMessage()


def test_non_lifecycle_failure_still_escapes_instead_of_becoming_a_hold(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.seed()
    rig.venue.open = [venue_order(L.CANCELLED, "0")]

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(rig.tracker, "ingest", fail)
    with pytest.raises(RuntimeError, match="synthetic storage failure"):
        rig.reconcile()


@pytest.mark.parametrize("status", [L.SUBMITTED, L.SUBMISSION_UNKNOWN])
def test_two_persisted_broker_ids_refuse_full_execution_settlement(rig: Rig, status: L) -> None:
    rig.seed()
    if status is L.SUBMISSION_UNKNOWN:
        rig.event(status, at=PRIOR)
    other_id = BROKER_ID + 77
    assert rig.repo.record_transition(
        intent_id="intent-1",
        event_key="second-broker-id",
        source="TEST",
        from_status=status,
        to_status=status,
        current_account_id=rig.account,
        broker_order_id=other_id,
        occurred_at=PRIOR + timedelta(seconds=1),
    )
    before = rig.events()
    rig.venue.execs = [
        venue_execution("full-latest", "10", PRIOR).model_copy(update={"broker_order_id": other_id})
    ]

    report = rig.reconcile()

    assert rig.status() is status and rig.events() == before
    assert report.unresolved[0].reason.startswith("identity conflict:")
    assert not report.permits_new_opening_order


@pytest.mark.parametrize("shape", ["listed", "executions"])
@pytest.mark.parametrize("field", ["broker_order_id", "permanent_id", "client_id"])
def test_terminal_legacy_write_refuses_identity_history_changed_after_capture(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, shape: str, field: str
) -> None:
    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
    if shape == "listed":
        rig.venue.open = [venue_order(L.SUBMITTED, "10")]
    else:
        rig.venue.execs = [venue_execution("own-full", "10", PRIOR)]
    original = rig.repo.record_transition
    before_write = []
    values = {
        "broker_order_id": BROKER_ID + 77,
        "permanent_id": PERM_ID + 1,
        "client_id": CLIENT_ID + 1,
    }

    def racing(**kwargs):
        if kwargs.get("source") == "RECONCILE":
            assert original(
                intent_id="intent-1",
                event_key="late-identity",
                source="IDENTITY",
                from_status=L.PARTIALLY_FILLED,
                to_status=L.PARTIALLY_FILLED,
                current_account_id=rig.account,
                occurred_at=PRIOR + timedelta(seconds=1),
                **{field: values[field]},
            )
            before_write.append(rig.events())
        return original(**kwargs)

    monkeypatch.setattr(rig.repo, "record_transition", racing)
    report = rig.reconcile()

    assert len(before_write) == 1  # the race reached the actual write boundary
    assert rig.status() is L.PARTIALLY_FILLED
    assert rig.events() == before_write[0]  # only the racing writer's row survives
    assert report.applied_updates == () and not report.permits_new_opening_order
    assert "identity history advanced" in report.unresolved[0].reason


@pytest.mark.parametrize("status", [L.SUBMITTED, L.CANCEL_PENDING])
def test_r6_own_reference_permid_refusal_survives_under_foreign_client(rig: Rig, status: L) -> None:
    rig.seed()
    if status is L.CANCEL_PENDING:
        rig.pending()
    before = rig.events()
    rig.venue.open = [venue_order(L.CANCELLED, "0", perm=PERM_ID + 1, client=CLIENT_ID + 1)]
    rig.venue.completed = [venue_order(L.CANCELLED, "0")]

    report = rig.reconcile()

    assert rig.status() is status and rig.events() == before
    assert report.unresolved[0].reason.startswith("identity conflict:")
    assert all(value not in report.unresolved[0].reason for value in (REF, str(PERM_ID + 1)))


@pytest.mark.parametrize("missing", ["permanent_id", "client_id", "neither"])
def test_unchanged_captured_history_allows_a_valid_legacy_terminal_and_identity_refinement(
    rig: Rig, missing: str
) -> None:
    rig.seed(
        permanent_id=None if missing == "permanent_id" else PERM_ID,
        client_id=None if missing == "client_id" else CLIENT_ID,
    )
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
    rig.venue.open = [venue_order(L.SUBMITTED, "10")]

    report = rig.reconcile()

    assert rig.status() is L.FILLED
    assert report.unresolved == () and len(report.applied_updates) == 1
    assert rig.events()[-1].filled_quantity == Decimal("10")


@pytest.mark.parametrize("foreign_field", ["account_id", "client_id"])
def test_foreign_blank_reference_execution_does_not_become_an_own_row_witness(
    rig: Rig, foreign_field: str
) -> None:
    rig.seed(permanent_id=None)
    rig.venue.open = [venue_order(L.CANCELLED, "0")]
    value = rig.account + "-OTHER" if foreign_field == "account_id" else CLIENT_ID + 1
    rig.venue.execs = [
        venue_execution("foreign-blank", "5", PRIOR, ref=None).model_copy(
            update={foreign_field: value}
        )
    ]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and report.unresolved == ()
    assert len(report.applied_updates) == 1


def test_terminal_noop_race_cannot_append_identity_from_a_stale_snapshot(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.seed()
    rig.event(L.PARTIALLY_FILLED, at=PRIOR, filled="5")
    rig.venue.open = [venue_order(L.SUBMITTED, "10")]
    original = rig.tracker.ingest
    before_write = []

    def racing(update, **kwargs):
        assert rig.repo.record_transition(
            intent_id="intent-1",
            event_key="competing-terminal",
            source="TEST",
            from_status=L.PARTIALLY_FILLED,
            to_status=L.FILLED,
            current_account_id=rig.account,
            broker_order_id=BROKER_ID,
            permanent_id=PERM_ID + 1,
            client_id=CLIENT_ID,
            filled_quantity=Decimal("10"),
            remaining_quantity=Decimal("0"),
            occurred_at=PRIOR + timedelta(seconds=1),
        )
        before_write.append(rig.events())
        return original(update, **kwargs)

    monkeypatch.setattr(rig.tracker, "ingest", racing)
    report = rig.reconcile()

    assert len(before_write) == 1 and rig.events() == before_write[0]
    assert rig.status() is L.FILLED  # solely the competing writer's terminal result
    assert report.applied_updates == () and report.unresolved
    assert "identity history advanced" in report.unresolved[0].reason

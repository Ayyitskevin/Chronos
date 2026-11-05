from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support.settle_rig import (
    BROKER_ID,
    CLIENT_ID,
    NOW,
    PERM_ID,
    PRIOR,
    REF,
    Rig,
    ScriptedVenue,
    issues,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.broker.base import BrokerDataError
from chronos.broker.callbacks import CallbackBridge
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import OrderLifecycle
from chronos.persistence.order_repositories import IdentityHistoryAdvanced, RecordedFillsAdvanced

CP = OrderLifecycle.CANCEL_PENDING
PF = OrderLifecycle.PARTIALLY_FILLED
FILLED = OrderLifecycle.FILLED
OTHER_BROKER_ID = BROKER_ID + 77


def _add_second_broker_id(rig: Rig, *, status: OrderLifecycle) -> None:
    assert rig.repo.record_transition(
        intent_id="intent-1",
        event_key="r4:other-broker-id",
        source="TEST",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=rig.account,
        broker_order_id=OTHER_BROKER_ID,
        occurred_at=PRIOR,
    )
    assert rig.repo.record_transition(
        intent_id="intent-1",
        event_key="r4:latest-broker-id",
        source="TEST",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=status,
        current_account_id=rig.account,
        broker_order_id=BROKER_ID,
        filled_quantity=Decimal("2") if status is PF else Decimal("0"),
        occurred_at=PRIOR + timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    ("starting_status", "evidence"),
    (
        (CP, "executions"),
        (PF, "executions"),
        (CP, "working"),
        (PF, "working"),
    ),
)
def test_two_persisted_broker_ids_refuse_every_settle_path(
    starting_status: OrderLifecycle,
    evidence: str,
) -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        _add_second_broker_id(rig, status=starting_status)
        if evidence == "executions":
            venue.execs = [venue_execution("R4-EXEC", "10", NOW - timedelta(minutes=5))]
        else:
            venue.open = [venue_order(OrderLifecycle.SUBMITTED, "5")]

        before = len(rig.events())
        report = rig.reconcile()

        assert rig.status() is starting_status
        assert len(rig.events()) == before
        assert any("identity conflict" in reason for reason in issues(report))
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("starting_status", "evidence"),
    (
        (CP, "executions"),
        (PF, "executions"),
        (CP, "working"),
        (PF, "working"),
    ),
)
def test_one_persisted_broker_id_still_settles_each_path(
    starting_status: OrderLifecycle,
    evidence: str,
) -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.event(
            starting_status,
            at=PRIOR + timedelta(seconds=1),
            filled="2" if starting_status is PF else "0",
        )
        if evidence == "executions":
            venue.execs = [venue_execution("R4-EXEC", "10", NOW - timedelta(minutes=5))]
        else:
            venue.open = [venue_order(OrderLifecycle.SUBMITTED, "5")]

        report = rig.reconcile()

        assert report.unresolved == ()
        if evidence == "executions":
            assert rig.status() is FILLED
            assert rig.events()[-1].filled_quantity == Decimal("10")
        else:
            assert rig.status() is (CP if starting_status is CP else PF)
            assert rig.events()[-1].filled_quantity == Decimal("5")
    finally:
        rig.close()


def test_second_broker_id_landing_after_precheck_refuses_typed_identity_conflict() -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        persisted_identities = rig.tracker.persisted_identities

        def race_after_precheck(intent_id: str, *, current_account_id: str):  # type: ignore[no-untyped-def]
            assert rig.repo.record_transition(
                intent_id=intent_id,
                event_key="r5:post-precheck-broker-id",
                source="TEST",
                from_status=CP,
                to_status=CP,
                current_account_id=current_account_id,
                broker_order_id=OTHER_BROKER_ID,
                occurred_at=PRIOR,
            )
            rig.tracker.persisted_identities = persisted_identities
            return persisted_identities(intent_id, current_account_id=current_account_id)

        rig.tracker.persisted_identities = race_after_precheck
        venue.execs = [venue_execution("R5-RACE-EXEC", "10", NOW - timedelta(minutes=5))]
        before = len(rig.events())

        report = rig.reconcile()

        assert rig.status() is CP
        assert len(rig.events()) == before + 1  # the injected identity row, no settlement row
        assert rig.events()[-1].source == "TEST"
        reason = " ".join(issues(report))
        assert reason.startswith("identity conflict:")
        assert "persisted broker order ids" in reason
        assert "not bound to this intent" not in reason
    finally:
        rig.close()


def test_identity_advance_precedes_lower_fill_when_both_conflict_in_savepoint() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.event(PF, at=PRIOR + timedelta(seconds=1), filled="3")
        expected = rig.tracker.persisted_identities("intent-1", current_account_id=rig.account)
        assert rig.repo.record_transition(
            intent_id="intent-1",
            event_key="land:second-broker-id",
            source="TEST",
            from_status=PF,
            to_status=PF,
            current_account_id=rig.account,
            broker_order_id=OTHER_BROKER_ID,
            filled_quantity=Decimal("3"),
            remaining_quantity=Decimal("7"),
            occurred_at=PRIOR + timedelta(seconds=2),
        )
        before = rig.events()

        with pytest.raises(IdentityHistoryAdvanced):
            rig.tracker.record_planned_transition(
                intent_id="intent-1",
                current_account_id=rig.account,
                from_status=PF,
                to_status=FILLED,
                event_key="land:both-conflicts",
                source="RECONCILE",
                broker_order_id=BROKER_ID,
                permanent_id=PERM_ID,
                client_id=CLIENT_ID,
                filled_quantity=Decimal("2"),
                remaining_quantity=Decimal("8"),
                evidence={"interaction": "identity-before-fill"},
                now=NOW,
                expected_identities=expected,
            )

        assert rig.status() is PF
        assert rig.events() == before
    finally:
        rig.close()


def test_matching_two_id_snapshot_still_exposes_typed_lower_fill_refusal() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed()
        rig.pending()
        rig.event(PF, at=PRIOR + timedelta(seconds=1), filled="3")
        assert rig.repo.record_transition(
            intent_id="intent-1",
            event_key="land:known-second-broker-id",
            source="TEST",
            from_status=PF,
            to_status=PF,
            current_account_id=rig.account,
            broker_order_id=OTHER_BROKER_ID,
            filled_quantity=Decimal("3"),
            remaining_quantity=Decimal("7"),
            occurred_at=PRIOR + timedelta(seconds=2),
        )
        expected = rig.tracker.persisted_identities("intent-1", current_account_id=rig.account)
        before = rig.events()

        with pytest.raises(RecordedFillsAdvanced):
            rig.tracker.record_planned_transition(
                intent_id="intent-1",
                current_account_id=rig.account,
                from_status=PF,
                to_status=FILLED,
                event_key="land:fill-conflict-after-matching-identity",
                source="RECONCILE",
                broker_order_id=BROKER_ID,
                permanent_id=PERM_ID,
                client_id=CLIENT_ID,
                filled_quantity=Decimal("2"),
                remaining_quantity=Decimal("8"),
                evidence={"interaction": "fill-after-identity-match"},
                now=NOW,
                expected_identities=expected,
            )

        assert rig.status() is PF
        assert rig.events() == before
    finally:
        rig.close()


def test_candidate_row_is_excluded_when_it_adds_the_first_client_id() -> None:
    rig = make_rig(ScriptedVenue())
    try:
        rig.seed(client_id=None)
        rig.pending()
        expected = rig.tracker.persisted_identities("intent-1", current_account_id=rig.account)
        assert expected.client_ids == frozenset()
        before = rig.events()

        assert rig.tracker.record_planned_transition(
            intent_id="intent-1",
            current_account_id=rig.account,
            from_status=CP,
            to_status=FILLED,
            event_key="r6:first-client-id-on-candidate",
            source="RECONCILE",
            broker_order_id=BROKER_ID,
            permanent_id=PERM_ID,
            client_id=CLIENT_ID,
            filled_quantity=Decimal("10"),
            remaining_quantity=Decimal("0"),
            evidence={"candidate_row": "must_be_excluded"},
            now=NOW,
            expected_identities=expected,
        )

        assert rig.status() is FILLED
        assert len(rig.events()) == len(before) + 1
        assert rig.events()[-1].client_id == CLIENT_ID
    finally:
        rig.close()


@pytest.mark.parametrize("kind", ("order", "execution"))
@pytest.mark.parametrize("contradicts", (True, False))
def test_foreign_session_own_reference_permid_conflict_is_typed_and_narrow(
    kind: str,
    contradicts: bool,
) -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        foreign_account = "DU7654321"
        foreign_client = CLIENT_ID + 1
        foreign_broker_id = BROKER_ID + 987
        foreign_permanent_id = PERM_ID + 987
        evidence_ref = REF if contradicts else "CHR-UNRELATED-R6-987654321"
        if kind == "order":
            venue.open = [
                venue_order(
                    OrderLifecycle.SUBMITTED,
                    "0",
                    account=foreign_account,
                    client=foreign_client,
                    ref=evidence_ref,
                    perm=foreign_permanent_id,
                    broker_id=foreign_broker_id,
                )
            ]
        else:
            venue.execs = [
                venue_execution(
                    "FOREIGN-R6-EXECUTION-987654321",
                    "1",
                    NOW - timedelta(minutes=5),
                    ref=evidence_ref,
                    perm=foreign_permanent_id,
                ).model_copy(
                    update={
                        "account_id": foreign_account,
                        "client_id": foreign_client,
                        "broker_order_id": foreign_broker_id,
                    }
                )
            ]
        venue.completed = [venue_order(OrderLifecycle.CANCELLED, "5")]

        report = rig.reconcile()

        if contradicts:
            assert rig.status() is CP
            assert len(report.unresolved) == 1
            assert report.unresolved[0].reason == (
                f"identity conflict: broker {kind} evidence reports the owned order reference "
                "with a contradicting permanent id"
            )
            reason = report.unresolved[0].reason
            assert foreign_account not in reason
            assert str(foreign_client) not in reason
            assert str(foreign_broker_id) not in reason
            assert str(foreign_permanent_id) not in reason
        else:
            assert report.unresolved == ()
            assert rig.status() is OrderLifecycle.CANCELLED
            assert rig.events()[-1].filled_quantity == Decimal("5")
    finally:
        rig.close()


def test_completed_order_timeout_guidance_reaches_the_real_reconciler() -> None:
    bridge = CallbackBridge(RequestRegistry())
    flight = bridge.start_completed_orders()
    bridge.release_completed_orders(flight)
    with pytest.raises(BrokerDataError) as refused:
        bridge.start_completed_orders()

    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        venue.completed_error = refused.value

        reason = " ".join(issues(rig.reconcile()))

        assert rig.status() is CP
        assert "reconnect is required" in reason
        assert type(refused.value) is not BrokerDataError
    finally:
        rig.close()


def test_completed_order_timeout_taint_logs_once_without_broker_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    flight = bridge.start_completed_orders()

    with caplog.at_level(logging.WARNING, logger="chronos.broker.callbacks"):
        bridge.release_completed_orders(flight)
        bridge.release_completed_orders(flight)

    warnings = [record.getMessage() for record in caplog.records]
    assert warnings == ["Completed-orders stream timed out; reconnect is required before retrying"]
    assert "broker" not in warnings[0].lower()


@pytest.mark.parametrize(
    ("account", "client"),
    (("DU7654321", CLIENT_ID), (None, CLIENT_ID + 1)),
)
def test_foreign_session_row_with_our_permid_refuses_without_foreign_identifiers(
    account: str | None,
    client: int,
) -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        foreign_ref = "CHR-FOREIGN-R4-987654321"
        foreign_broker_id = 987654321
        venue.open = [
            venue_order(
                OrderLifecycle.SUBMITTED,
                "0",
                account=account or rig.account,
                client=client,
                ref=foreign_ref,
                perm=PERM_ID,
                broker_id=foreign_broker_id,
            )
        ]
        venue.completed = [venue_order(OrderLifecycle.CANCELLED, "0")]

        reason = " ".join(issues(rig.reconcile()))

        assert rig.status() is CP
        assert "identity conflict" in reason
        assert "broker order evidence" in reason
        assert foreign_ref not in reason
        assert str(foreign_broker_id) not in reason
        assert "DU7654321" not in reason
    finally:
        rig.close()


def test_foreign_execution_with_our_permid_names_kind_without_identifiers() -> None:
    venue = ScriptedVenue()
    rig = make_rig(venue)
    try:
        rig.seed()
        rig.pending()
        foreign_ref = "CHR-FOREIGN-EXEC-R5-987654321"
        foreign_execution_id = "FOREIGN-EXEC-R5-987654321"
        foreign_broker_id = 987654321
        foreign_client_id = CLIENT_ID + 1
        venue.execs = [
            venue_execution(
                foreign_execution_id,
                "1",
                NOW - timedelta(minutes=5),
                ref=foreign_ref,
                perm=PERM_ID,
            ).model_copy(
                update={
                    "broker_order_id": foreign_broker_id,
                    "client_id": foreign_client_id,
                }
            )
        ]
        venue.completed = [venue_order(OrderLifecycle.CANCELLED, "0")]

        reason = " ".join(issues(rig.reconcile()))

        assert rig.status() is CP
        assert "identity conflict" in reason
        assert "broker execution evidence" in reason
        assert foreign_ref not in reason
        assert foreign_execution_id not in reason
        assert str(foreign_broker_id) not in reason
        assert str(foreign_client_id) not in reason
        assert rig.account not in reason
    finally:
        rig.close()


def test_overlap_refusal_is_non_sticky_and_does_not_request_reconnect() -> None:
    bridge = CallbackBridge(RequestRegistry())
    first = bridge.start_completed_orders()

    with pytest.raises(BrokerDataError) as overlap:
        bridge.start_completed_orders()

    assert "already in flight" in str(overlap.value)
    assert "reconnect" not in str(overlap.value).lower()
    bridge.on_completed_orders_end()
    bridge.release_completed_orders(first)
    retry = bridge.start_completed_orders()
    assert retry is not first

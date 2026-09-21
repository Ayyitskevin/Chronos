"""BP-2 — order identity completes on the event path (migration 0014, schema v15).

``permanent_id`` and ``client_id`` are first-class, nullable columns of ``order_events``,
written from the broker's orderStatus/openOrder evidence by the ONE callback→event call
site (``OrderTracker.ingest`` → ``OrderTrackerRepository.record_transition``), read back by
reconciliation to match a working order by permId when the venue's ``orderRef`` is blank —
and read by no decision input. Numbered to the packet contract.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from tests.integration.test_order_pipeline import _drive_to_confirmed, _Harness, _short_put_intent
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    paper_settings,
)

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerOrder, OrderRequest, OrderSubmission
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    _unresolved_evidence_reason,
    resolve_from_broker_evidence,
)
from chronos.orders.tracker import (
    IngestOutcome,
    OrderIdentityConflict,
    OrderStatusUpdate,
    OrderTracker,
)
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.persistence.repositories import account_fingerprint

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "src/chronos/persistence/migrations/versions/0014_order_event_identity.py"
_CLIENT_ID = 17
_PERM_ID = 4242
_LIMIT = Decimal("1.20")


def _intent(intent_id: str, *, status: OrderLifecycle) -> OrderIntentRecord:
    contract = option_contract()
    return OrderIntentRecord(
        intent_id=intent_id,
        idempotency_key=f"key-{intent_id}",
        account_fingerprint=account_fingerprint(PAPER_ACCOUNT),
        environment="paper",
        product_family=ProductFamily.OPTION,
        wheel_cycle_id=None,
        symbol=contract.symbol,
        con_id=contract.con_id,
        local_symbol=contract.local_symbol,
        action=OrderSide.SELL,
        open_close_effect="OPEN",
        quantity=Decimal("1"),
        order_type="LMT",
        limit_price=_LIMIT,
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=f"CHR-ORD-{intent_id}",
        status=status,
        created_at=FIXED_NOW,
        confirmed_at=None,
        submitted_at=None,
        expires_at=None,
    )


def _working_order(*, order_ref: str | None, permanent_id: int | None) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=9001,
        permanent_id=permanent_id,
        client_id=_CLIENT_ID,
        account_id=PAPER_ACCOUNT,
        order_ref=order_ref,
        contract=option_contract(),
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        limit_price=_LIMIT,
        lifecycle=OrderLifecycle.SUBMITTED,
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'orders.db'}")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT)
    yield db
    db.dispose()


def _event_rows(database: Database, intent_id: str) -> list[tuple[int, int | None, int | None]]:
    with database.engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                sa.text(
                    "SELECT sequence, permanent_id, client_id FROM order_events "
                    "WHERE intent_id = :intent ORDER BY sequence"
                ),
                {"intent": intent_id},
            )
        ]


# --- 1. migration 0014 + schema mirror + version pins ----------------------------------------


def test_1_migration_0014_adds_nullable_identity_columns_and_the_schema_mirrors_them(
    database: Database,
) -> None:
    inspector = sa.inspect(database.engine)
    columns = {column["name"]: column for column in inspector.get_columns("order_events")}
    assert columns["permanent_id"]["nullable"] is True
    assert columns["client_id"]["nullable"] is True
    assert any(
        index["name"] == "ix_order_events_permanent_id"
        and index["column_names"] == ["permanent_id"]
        for index in inspector.get_indexes("order_events")
    )
    assert SCHEMA_VERSION == 15

    migration = _MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "0014"' in migration
    assert 'down_revision = "0013"' in migration
    assert "version=15" in migration
    assert "server_default" not in migration  # unknown stays unknown, never a synthesized 0
    release_gate = (_ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert re.search(r'^_MIGRATION_HEAD: Final\[str\] = "0014"$', release_gate, re.MULTILINE)

    # a row written without any identity reads back None on both columns
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    intents.create(
        _intent("i-1", status=OrderLifecycle.SUBMITTED), current_account_id=PAPER_ACCOUNT
    )
    assert repo.record_transition(
        intent_id="i-1",
        event_key="i-1:presubmit",
        source="SUBMIT",
        from_status=None,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    assert _event_rows(database, "i-1") == [(1, None, None)]
    event = repo.events("i-1", current_account_id=PAPER_ACCOUNT)[0]
    assert (event.permanent_id, event.client_id) == (None, None)


# --- 2. the callback→event path writes the columns --------------------------------------------


def test_2_an_order_status_observation_with_permid_and_clientid_lands_in_the_columns(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-2", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )

    applied = tracker.ingest(
        OrderStatusUpdate(
            intent_id="i-2",
            broker_order_id=9001,
            permanent_id=_PERM_ID,
            client_id=_CLIENT_ID,
            lifecycle=OrderLifecycle.SUBMITTED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            source="ORDER_STATUS",
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    )
    assert applied.lifecycle_changed is True
    assert _event_rows(database, "i-2") == [(1, _PERM_ID, _CLIENT_ID)]
    latest = repo.events("i-2", current_account_id=PAPER_ACCOUNT)[-1]
    assert (latest.permanent_id, latest.client_id) == (_PERM_ID, _CLIENT_ID)
    assert latest.evidence["permanent_id"] == _PERM_ID  # the JSON copy is unchanged

    # a callback that reports no permId: None, never 0
    intents.create(
        _intent("i-2b", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    assert tracker.ingest(
        OrderStatusUpdate(
            intent_id="i-2b",
            broker_order_id=9002,
            lifecycle=OrderLifecycle.SUBMITTED,
            remaining_quantity=Decimal("1"),
            occurred_at=FIXED_NOW,
        ),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert _event_rows(database, "i-2b") == [(1, None, None)]


# --- 3. idempotency unchanged; a later event fills in the permanent_id ------------------------


def test_3_same_event_key_is_one_row_and_a_later_event_fills_in_the_permanent_id(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-3", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    ack = dict(
        intent_id="i-3",
        event_key="i-3:9001:SUBMITTED",
        source="SUBMIT",
        from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        broker_order_id=9001,
        occurred_at=FIXED_NOW,
    )
    assert repo.record_transition(**ack) is True  # sequence 1: the ack knew no permId
    assert repo.record_transition(**ack) is False  # the same event_key: one row, nothing written
    assert tracker.permanent_id("i-3", current_account_id=PAPER_ACCOUNT) is None

    assert repo.record_transition(
        intent_id="i-3",
        event_key="i-3:9001:SUBMITTED:refined",
        source="ORDER_STATUS",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        broker_order_id=9001,
        permanent_id=_PERM_ID,
        client_id=_CLIENT_ID,
        occurred_at=FIXED_NOW,
    )
    assert _event_rows(database, "i-3") == [(1, None, None), (2, _PERM_ID, _CLIENT_ID)]
    assert tracker.permanent_id("i-3", current_account_id=PAPER_ACCOUNT) == _PERM_ID
    events = repo.events("i-3", current_account_id=PAPER_ACCOUNT)
    assert [event.permanent_id for event in events] == [None, _PERM_ID]  # append-only, never edited


# --- 4. reconciliation matches by the persisted permanent_id ---------------------------------


def test_4_reconciliation_resolves_a_working_order_that_matches_only_by_persisted_permid(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intent = _intent("i-4", status=OrderLifecycle.SUBMISSION_UNKNOWN)
    intents.create(intent, current_account_id=PAPER_ACCOUNT)
    assert repo.record_transition(
        intent_id="i-4",
        event_key="i-4:presubmit:SUBMISSION_UNKNOWN",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        permanent_id=_PERM_ID,
        client_id=_CLIENT_ID,
        occurred_at=FIXED_NOW,
    )
    # the venue reports the order back WITHOUT its orderRef, but with the permId we persisted
    order = _working_order(order_ref=None, permanent_id=_PERM_ID)
    common = dict(
        open_orders=(order,),
        executions=(),
        current_account_id=PAPER_ACCOUNT,
        expected_broker_client_id=_CLIENT_ID,
        expected_limit_price=_LIMIT,
        persisted_broker_order_id=None,
        now=FIXED_NOW,
    )
    update = resolve_from_broker_evidence(intent, persisted_permanent_id=_PERM_ID, **common)
    assert update is not None
    assert (update.lifecycle, update.permanent_id, update.client_id) == (
        OrderLifecycle.SUBMITTED,
        _PERM_ID,
        _CLIENT_ID,
    )
    # controls: no persisted permId → the base behaviour (absent, unresolved); a different
    # permId is not this order; a blank-ref order under another client never matches
    assert resolve_from_broker_evidence(intent, persisted_permanent_id=None, **common) is None
    assert resolve_from_broker_evidence(intent, persisted_permanent_id=4243, **common) is None
    assert (
        resolve_from_broker_evidence(
            intent,
            persisted_permanent_id=_PERM_ID,
            **{**common, "expected_broker_client_id": _CLIENT_ID + 1},
        )
        is None
    )

    # end to end: the restart reconciler reads the persisted permId from the events itself
    broker = FakeBroker(open_orders=(order,))
    connection = BrokerConnectionManager(broker, readiness=ReconciliationReadiness())
    connection.start()
    try:
        report = OrderRestartReconciler(
            connection=connection,
            intents=intents,
            tracker=tracker,
            reconciliation_readiness=ReconciliationReadiness(),
            expected_broker_client_id=_CLIENT_ID,
        ).reconcile_report(current_account_id=PAPER_ACCOUNT, now=FIXED_NOW)
    finally:
        connection.close()
    assert [observation.intent_id for observation in report.proven] == ["i-4"]
    assert report.proven[0].broker_status is OrderLifecycle.SUBMITTED
    assert report.proven[0].transition_applied is True
    assert report.unresolved == ()
    stored = intents.get("i-4", current_account_id=PAPER_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED
    assert _event_rows(database, "i-4")[-1] == (2, _PERM_ID, _CLIENT_ID)


# --- 5. authority line, both ways ------------------------------------------------------------

_AUTHORITY_PREFIXES = (
    "chronos.orders.risk",
    "chronos.orders.submission",
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.control",
    "chronos.execution",
)
# Every attribute read named permanent_id/client_id in the decision modules, keyed by the
# expression it is read from — TODAY's set, frozen. Each base is a broker object (the submit
# result, an execution); none is an order-event row/record or the tracker's event accessor.
_ALLOWED_IDENTITY_READS = {
    "src/chronos/orders/risk.py": set(),
    "src/chronos/orders/submission.py": {"submission"},
    "src/chronos/supervisor/admission.py": set(),
    "src/chronos/supervisor/position_admission.py": {"executions[0]", "execution"},
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _identity_reads(path: Path) -> set[str]:
    reads: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and node.attr in {"permanent_id", "client_id"}:
            reads.add(ast.unparse(node.value))
    return reads


def test_5_the_event_identity_is_no_decision_input_and_the_writer_imports_no_authority() -> None:
    for module in (_ROOT / "src/chronos/persistence/order_repositories.py", _MIGRATION):
        offending = {name for name in _imports(module) if name.startswith(_AUTHORITY_PREFIXES)}
        assert offending == set(), f"{module.name} imports an authority module: {offending}"

    for relative, allowed in _ALLOWED_IDENTITY_READS.items():
        reads = _identity_reads(_ROOT / relative)
        assert reads == allowed, (
            f"{relative} reads permanent_id/client_id from {sorted(reads - allowed)} — the "
            "order_events identity columns are evidence, never a decision input"
        )
        source = (_ROOT / relative).read_text(encoding="utf-8")
        assert ".permanent_id(" not in source  # the tracker's event-derived accessor
        assert "OrderEventRecord" not in source


def test_5b_the_reads_scan_catches_a_planted_event_column_read(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def f(session, OrderEventRow):\n"
        "    return session.scalar(select(OrderEventRow.permanent_id))\n",
        encoding="utf-8",
    )
    assert _identity_reads(probe) == {"OrderEventRow"}
    probe.write_text("def g(events):\n    return [event.client_id for event in events]\n")
    assert _identity_reads(probe) == {"event"}


# =============================================================================================
# BP-2 r1 — Daybreak's HOLD at 54309b9, under rulings R-c..R-f. Every pin goes through the
# production path: the submission boundary for the ACK row, OrderTracker.ingest for callbacks,
# the restart reconciler for matching.
# =============================================================================================


class _AckBroker(FakeBroker):
    """The venue's placeOrder acknowledgement reports permId 4242 / clientId 17."""

    async def submit_order(self, request: OrderRequest, **kwargs: object) -> OrderSubmission:
        submission = await super().submit_order(request, **kwargs)
        return submission.model_copy(update={"permanent_id": _PERM_ID, "client_id": _CLIENT_ID})


def _harness_events(h: _Harness, intent_id: str) -> list[tuple[int, int | None, int | None]]:
    return [
        (event.sequence, event.permanent_id, event.client_id)
        for event in h.tracker_repo.events(intent_id, current_account_id=PAPER_ACCOUNT)
    ]


def _submitted_update(
    intent_id: str, *, permanent_id: int | None, client_id: int | None
) -> OrderStatusUpdate:
    return OrderStatusUpdate(
        intent_id=intent_id,
        broker_order_id=9001,
        permanent_id=permanent_id,
        client_id=client_id,
        lifecycle=OrderLifecycle.SUBMITTED,
        remaining_quantity=Decimal("1"),
        source="ORDER_STATUS",
        occurred_at=FIXED_NOW,
    )


# --- r1-1. the production ACK row carries the identity (R-c) ----------------------------------


def test_r1_1_the_production_ack_row_written_by_the_submission_boundary_carries_the_columns() -> (
    None
):
    h = _Harness(_AckBroker(), paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)
        outcome = h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
        assert outcome.submitted is True
        assert outcome.submission is not None
        assert (outcome.submission.permanent_id, outcome.submission.client_id) == (
            _PERM_ID,
            _CLIENT_ID,
        )
        # Daybreak's probe shape (logs/daybreak-probe-bp2.out:10): columns / tracker_perm /
        # blank_ref_resolved — now through the REAL call at submission.py:797
        rows = _harness_events(h, "intent-1")
        assert rows[-1] == (2, _PERM_ID, _CLIENT_ID)  # the ACK row; sequence 1 is pre-submit
        assert rows[0][1:] == (None, None)  # the pre-submit row knew no identity (unchanged)
        assert h.tracker.permanent_id("intent-1", current_account_id=PAPER_ACCOUNT) == _PERM_ID
        # the venue reports the order back WITHOUT its orderRef, with the ACK's permId
        h.broker._open_orders = (
            _working_order(order_ref=None, permanent_id=_PERM_ID).model_copy(
                update={"client_id": h.settings.ib_client_id, "limit_price": intent.limit_price}  # type: ignore[attr-defined]
            ),
        )
        report = h.service.reconcile_on_restart_report(now=FIXED_NOW)
        assert report.unresolved == ()
        assert [item.intent_id for item in report.proven] == ["intent-1"]
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.SUBMITTED
    finally:
        h.close()


def test_r1_1b_the_submission_boundary_reads_identity_only_from_the_submission_it_holds() -> None:
    # the reverse authority scan of test_5, re-run on the changed file: the two new reads are
    # `submission.permanent_id` / `submission.client_id` — the base the evidence dict already read
    reads = _identity_reads(_ROOT / "src/chronos/orders/submission.py")
    assert reads == {"submission"}
    source = (_ROOT / "src/chronos/orders/submission.py").read_text(encoding="utf-8")
    assert "permanent_id=submission.permanent_id," in source
    assert "client_id=submission.client_id," in source


# --- r1-2. a same-status callback that newly supplies identity is recorded (R-d) --------------


def test_r1_2_a_same_status_callback_that_newly_supplies_identity_records_a_refinement_row(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r2", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    # the ACK knew no identity
    assert tracker.ingest(
        _submitted_update("i-r2", permanent_id=None, client_id=None),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert _event_rows(database, "i-r2") == [(1, None, None)]

    # SUBMITTED -> SUBMITTED, first supplying permId / clientId: a refinement row through ingest
    applied = tracker.ingest(
        _submitted_update("i-r2", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    )
    assert applied.identity_refined is True
    assert _event_rows(database, "i-r2") == [(1, None, None), (2, _PERM_ID, _CLIENT_ID)]
    events = repo.events("i-r2", current_account_id=PAPER_ACCOUNT)
    assert events[1].from_status is OrderLifecycle.SUBMITTED
    assert events[1].to_status is OrderLifecycle.SUBMITTED
    assert events[1].source == "IDENTITY"
    assert [event.permanent_id for event in events] == [None, _PERM_ID]  # row 1 never edited
    assert tracker.permanent_id("i-r2", current_account_id=PAPER_ACCOUNT) == _PERM_ID
    stored = intents.get("i-r2", current_account_id=PAPER_ACCOUNT)
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED

    # the same identity again on the same status: nothing new — no row
    assert (
        tracker.ingest(
            _submitted_update("i-r2", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
            current_account_id=PAPER_ACCOUNT,
        ).recorded
        is False
    )
    # a same-status callback with NO identity at all: no row
    assert (
        tracker.ingest(
            _submitted_update("i-r2", permanent_id=None, client_id=None),
            current_account_id=PAPER_ACCOUNT,
        ).recorded
        is False
    )
    assert len(_event_rows(database, "i-r2")) == 2


def test_r1_2c_a_same_status_callback_adding_only_a_client_id_is_refinement_evidence_too(
    database: Database,
) -> None:
    # R-d clarified (r2): EITHER permanent_id OR client_id going None -> non-None is new
    # identity; (None, None) -> (None, 17) records one row
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r2c", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    assert tracker.ingest(
        _submitted_update("i-r2c", permanent_id=None, client_id=None),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert tracker.ingest(
        _submitted_update("i-r2c", permanent_id=None, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert _event_rows(database, "i-r2c") == [(1, None, None), (2, None, _CLIENT_ID)]
    assert tracker.client_id("i-r2c", current_account_id=PAPER_ACCOUNT) == _CLIENT_ID
    assert tracker.permanent_id("i-r2c", current_account_id=PAPER_ACCOUNT) is None


def test_r1_2d_a_same_status_callback_repeating_the_acked_permid_records_nothing(
    database: Database,
) -> None:
    # the ACK (a lifecycle transition, its own event_key) already persisted 4242; a later
    # SUBMITTED->SUBMITTED orderStatus repeating 4242 is not new identity — no refinement row.
    # (event_key idempotency cannot cover this: the two keys differ.)
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r2d", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    assert tracker.ingest(
        _submitted_update("i-r2d", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert (
        tracker.ingest(
            _submitted_update("i-r2d", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
            current_account_id=PAPER_ACCOUNT,
        ).recorded
        is False
    )
    assert _event_rows(database, "i-r2d") == [(1, _PERM_ID, _CLIENT_ID)]


def test_r1_2b_an_out_of_order_callback_after_a_terminal_state_still_records_nothing(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r2b", status=OrderLifecycle.CANCELLED), current_account_id=PAPER_ACCOUNT
    )
    assert (
        tracker.ingest(
            _submitted_update("i-r2b", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
            current_account_id=PAPER_ACCOUNT,
        ).recorded
        is False
    )
    assert _event_rows(database, "i-r2b") == []


# --- r1-3. divergent permanent ids fail closed (R-e) -----------------------------------------


def test_r1_3_divergent_permanent_ids_raise_a_typed_conflict_and_leave_the_intent_unresolved(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r3", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    assert tracker.ingest(
        _submitted_update("i-r3", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    # the same intent / broker order now reports permId 4243 (Daybreak's probe, line 12)
    assert tracker.ingest(
        _submitted_update("i-r3", permanent_id=_PERM_ID + 1, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert [row[1] for row in _event_rows(database, "i-r3")] == [_PERM_ID, _PERM_ID + 1]

    with pytest.raises(OrderIdentityConflict, match=r"4242.*4243|4243.*4242") as caught:
        tracker.permanent_id("i-r3", current_account_id=PAPER_ACCOUNT)
    assert "i-r3" in str(caught.value)
    assert caught.value.permanent_ids == (_PERM_ID, _PERM_ID + 1)

    # reconciliation: UNRESOLVED with the typed reason — never the latest, never the first
    broker = FakeBroker(open_orders=(_working_order(order_ref=None, permanent_id=_PERM_ID + 1),))
    connection = BrokerConnectionManager(broker, readiness=ReconciliationReadiness())
    connection.start()
    try:
        report = OrderRestartReconciler(
            connection=connection,
            intents=intents,
            tracker=tracker,
            reconciliation_readiness=ReconciliationReadiness(),
            expected_broker_client_id=_CLIENT_ID,
        ).reconcile_report(current_account_id=PAPER_ACCOUNT, now=FIXED_NOW)
    finally:
        connection.close()
    assert report.proven == ()
    assert [item.intent_id for item in report.unresolved] == ["i-r3"]
    assert report.unresolved[0].transition_applied is False
    assert report.unresolved[0].reason.startswith("identity conflict:")
    assert "4242" in report.unresolved[0].reason and "4243" in report.unresolved[0].reason
    assert len(_event_rows(database, "i-r3")) == 2  # nothing applied


# --- r1-4. permId fallback only for a blank orderRef (R-f) -----------------------------------


def _resolve(intent: OrderIntentRecord, order: BrokerOrder, *, persisted_permanent_id: int | None):
    common = dict(
        open_orders=(order,),
        executions=(),
        current_account_id=PAPER_ACCOUNT,
        expected_broker_client_id=_CLIENT_ID,
        expected_limit_price=_LIMIT,
        persisted_broker_order_id=None,
        persisted_permanent_id=persisted_permanent_id,
    )
    update = resolve_from_broker_evidence(intent, now=FIXED_NOW, **common)
    reason = _unresolved_evidence_reason(intent, **common)
    return update, reason


def test_r1_4_permid_fallback_applies_only_to_a_blank_order_ref_and_contradictions_are_typed() -> (
    None
):
    intent = _intent("i-r4", status=OrderLifecycle.SUBMISSION_UNKNOWN)

    # blank orderRef + the persisted permId → resolved (the fallback's only case)
    update, _ = _resolve(
        intent,
        _working_order(order_ref=None, permanent_id=_PERM_ID),
        persisted_permanent_id=_PERM_ID,
    )
    assert update is not None and update.lifecycle is OrderLifecycle.SUBMITTED

    # a NONBLANK, mismatching orderRef with the persisted permId → unresolved, typed conflict
    update, reason = _resolve(
        intent,
        _working_order(order_ref="CHR-OTHER", permanent_id=_PERM_ID),
        persisted_permanent_id=_PERM_ID,
    )
    assert update is None
    assert reason.startswith("identity conflict:")
    assert "CHR-OTHER" in reason and "4242" in reason

    # the owned orderRef with a permId that contradicts the persisted one → unresolved, typed
    update, reason = _resolve(
        intent,
        _working_order(order_ref=intent.order_ref, permanent_id=_PERM_ID + 1),
        persisted_permanent_id=_PERM_ID,
    )
    assert update is None
    assert reason.startswith("identity conflict:")
    assert "4243" in reason and "4242" in reason

    # the owned orderRef with the persisted permId, or with no permId reported → resolved
    for reported in (_PERM_ID, None):
        update, _ = _resolve(
            intent,
            _working_order(order_ref=intent.order_ref, permanent_id=reported),
            persisted_permanent_id=_PERM_ID,
        )
        assert update is not None

    # the docstring states the rule the code enforces
    doc = resolve_from_broker_evidence.__doc__ or ""
    assert "only when" in doc and "blank" in doc


def test_r1_4b_a_mismatching_nonblank_order_ref_stays_unresolved_through_the_reconciler(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r4b", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    assert tracker.ingest(
        _submitted_update("i-r4b", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    # push it back to SUBMISSION_UNKNOWN-like state is not possible; use a fresh intent that is
    # SUBMITTED and observe the reconciler's verdict on the contradictory snapshot
    broker = FakeBroker(open_orders=(_working_order(order_ref="CHR-OTHER", permanent_id=_PERM_ID),))
    connection = BrokerConnectionManager(broker, readiness=ReconciliationReadiness())
    connection.start()
    try:
        report = OrderRestartReconciler(
            connection=connection,
            intents=intents,
            tracker=tracker,
            reconciliation_readiness=ReconciliationReadiness(),
            expected_broker_client_id=_CLIENT_ID,
        ).reconcile_report(current_account_id=PAPER_ACCOUNT, now=FIXED_NOW)
    finally:
        connection.close()
    assert report.proven == ()
    assert [item.intent_id for item in report.unresolved] == ["i-r4b"]
    assert report.unresolved[0].reason.startswith("identity conflict:")


# =============================================================================================
# BP-2 r2 — Daybreak's HOLD-DELTA at 631a485: client identity is refinement evidence too
# (R-d clarified); the repository docstring states the R-e policy, never latest-wins.
# =============================================================================================


def _identity_rows(database: Database, intent_id: str) -> list[tuple[int | None, int | None]]:
    return [(perm, client) for _sequence, perm, client in _event_rows(database, intent_id)]


def test_r2_1_a_same_status_callback_that_first_supplies_the_client_id_records_one_row(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r21", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    # persisted (4242, None) — Daybreak's probe shape (logs/daybreak-probe-bp2r1.out:14)
    assert tracker.ingest(
        _submitted_update("i-r21", permanent_id=_PERM_ID, client_id=None),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert _identity_rows(database, "i-r21") == [(_PERM_ID, None)]

    applied = tracker.ingest(
        _submitted_update("i-r21", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    )
    assert applied.identity_refined is True
    assert _identity_rows(database, "i-r21") == [(_PERM_ID, None), (_PERM_ID, _CLIENT_ID)]
    events = repo.events("i-r21", current_account_id=PAPER_ACCOUNT)
    assert events[1].from_status is events[1].to_status is OrderLifecycle.SUBMITTED
    assert events[1].source == "IDENTITY"
    assert tracker.client_id("i-r21", current_account_id=PAPER_ACCOUNT) == _CLIENT_ID
    assert tracker.permanent_id("i-r21", current_account_id=PAPER_ACCOUNT) == _PERM_ID

    # a duplicate (4242, 17) after (4242, 17): idempotent, no row
    assert (
        tracker.ingest(
            _submitted_update("i-r21", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
            current_account_id=PAPER_ACCOUNT,
        ).recorded
        is False
    )
    assert len(_identity_rows(database, "i-r21")) == 2


def test_r2_1b_a_divergent_client_id_is_recorded_as_evidence_and_the_client_accessor_raises(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r21b", status=OrderLifecycle.SUBMISSION_UNKNOWN),
        current_account_id=PAPER_ACCOUNT,
    )
    assert tracker.ingest(
        _submitted_update("i-r21b", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    # (4242, 17) -> (4242, 18): the contradiction is evidence (a row), and the CLIENT accessor
    # is the one that raises — the permId accessor still resolves the single 4242
    assert tracker.ingest(
        _submitted_update("i-r21b", permanent_id=_PERM_ID, client_id=_CLIENT_ID + 1),
        current_account_id=PAPER_ACCOUNT,
    ).recorded
    assert _identity_rows(database, "i-r21b") == [
        (_PERM_ID, _CLIENT_ID),
        (_PERM_ID, _CLIENT_ID + 1),
    ]
    assert tracker.permanent_id("i-r21b", current_account_id=PAPER_ACCOUNT) == _PERM_ID
    with pytest.raises(OrderIdentityConflict, match=r"client ids 17 and 18") as caught:
        tracker.client_id("i-r21b", current_account_id=PAPER_ACCOUNT)
    assert (caught.value.field, caught.value.values) == ("client_id", (_CLIENT_ID, _CLIENT_ID + 1))
    assert caught.value.permanent_ids == ()

    # reconciliation fails closed on it too: UNRESOLVED with the typed reason
    broker = FakeBroker(open_orders=(_working_order(order_ref=None, permanent_id=_PERM_ID),))
    connection = BrokerConnectionManager(broker, readiness=ReconciliationReadiness())
    connection.start()
    try:
        report = OrderRestartReconciler(
            connection=connection,
            intents=intents,
            tracker=tracker,
            reconciliation_readiness=ReconciliationReadiness(),
            expected_broker_client_id=_CLIENT_ID,
        ).reconcile_report(current_account_id=PAPER_ACCOUNT, now=FIXED_NOW)
    finally:
        connection.close()
    assert report.proven == ()
    assert [item.intent_id for item in report.unresolved] == ["i-r21b"]
    assert report.unresolved[0].reason.startswith("identity conflict:")
    assert "17" in report.unresolved[0].reason and "18" in report.unresolved[0].reason


def test_r2_2_the_repository_docstring_states_the_typed_conflict_policy_not_latest_wins() -> None:
    doc = OrderTrackerRepository.record_transition.__doc__ or ""
    assert "OrderIdentityConflict" in doc
    assert "never" in doc  # the policy sentence: never latest-wins, never first-wins
    assert "latest" not in doc.lower()


# =============================================================================================
# BP-2 r4 — Daybreak's HOLD-DELTA at 018028a (R-g): ingest returns a structured outcome; the
# restart report counts ONLY lifecycle changes; a refinement-only observation is reported
# through the existing reason string — no report-shape change.
# =============================================================================================


class _NoPermIdAckBroker(FakeBroker):
    """The venue's placeOrder acknowledgement reports clientId 17 but no permId yet — the
    live production shape (OrderSubmission.permanent_id is optional; official_ibkr.py
    initialises it to None until an acknowledgement supplies it)."""

    async def submit_order(self, request: OrderRequest, **kwargs: object) -> OrderSubmission:
        submission = await super().submit_order(request, **kwargs)
        return submission.model_copy(update={"permanent_id": None, "client_id": _CLIENT_ID})


def test_r4_1_ingest_returns_a_structured_outcome_that_cannot_be_mistaken_for_the_old_bool(
    database: Database,
) -> None:
    intents = OrderIntentRepository(database.sessions)
    repo = OrderTrackerRepository(database.sessions)
    tracker = OrderTracker(intents, repo)
    intents.create(
        _intent("i-r41", status=OrderLifecycle.SUBMISSION_UNKNOWN), current_account_id=PAPER_ACCOUNT
    )
    lifecycle = tracker.ingest(
        _submitted_update("i-r41", permanent_id=None, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    )
    assert isinstance(lifecycle, IngestOutcome)
    assert (lifecycle.lifecycle_changed, lifecycle.identity_refined) == (True, False)
    refinement = tracker.ingest(
        _submitted_update("i-r41", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    )
    assert (refinement.lifecycle_changed, refinement.identity_refined) == (False, True)
    duplicate = tracker.ingest(
        _submitted_update("i-r41", permanent_id=_PERM_ID, client_id=_CLIENT_ID),
        current_account_id=PAPER_ACCOUNT,
    )
    assert (duplicate.lifecycle_changed, duplicate.identity_refined) == (False, False)
    assert (lifecycle.recorded, refinement.recorded, duplicate.recorded) == (True, True, False)
    with pytest.raises(TypeError, match="lifecycle_changed"):
        bool(duplicate)  # the old `if ingest(...)` / `assert ingest(...)` fails loudly


def test_r4_2_a_restart_identity_refinement_is_evidence_not_an_applied_lifecycle_transition() -> (
    None
):
    # Daybreak's production-shaped probe (logs/daybreak-probe-bp2r3.out:17): a post-R-c ACK row
    # (None, 17), then the broker reports (4242, 17) with the SAME lifecycle.
    h = _Harness(_NoPermIdAckBroker(), paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)
        assert h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW).submitted  # type: ignore[arg-type]
        assert _harness_events(h, "intent-1")[-1] == (2, None, _CLIENT_ID)  # the ACK row
        h.broker._open_orders = (
            _working_order(order_ref=intent.correlation_id, permanent_id=_PERM_ID).model_copy(  # type: ignore[attr-defined]
                update={"client_id": h.settings.ib_client_id, "limit_price": intent.limit_price}  # type: ignore[attr-defined]
            ),
        )

        report = h.service.reconcile_on_restart_report(now=FIXED_NOW)

        rows = _harness_events(h, "intent-1")
        assert rows[-1] == (3, _PERM_ID, _CLIENT_ID)  # ONE identity row appended
        assert [item.intent_id for item in report.proven] == ["intent-1"]
        assert (
            report.proven[0].local_status
            is report.proven[0].broker_status
            is OrderLifecycle.SUBMITTED
        )
        assert report.proven[0].transition_applied is False
        assert report.proven[0].reason == "identity evidence refined; lifecycle unchanged"
        assert report.applied_updates == ()
        assert report.unresolved == ()
        # the operator-visible integer (service.reconcile_on_restart -> api applied_count)
        assert h.service.reconcile_on_restart(now=FIXED_NOW) == 0
        assert _harness_events(h, "intent-1") == rows  # the second pass is idempotent
        assert h.tracker.permanent_id("intent-1", current_account_id=PAPER_ACCOUNT) == _PERM_ID
    finally:
        h.close()


def test_r4_3_a_genuine_lifecycle_recovery_still_counts_as_an_applied_transition() -> None:
    h = _Harness(_NoPermIdAckBroker(), paper_settings())
    try:
        intent = _short_put_intent()
        _drive_to_confirmed(h, intent, FIXED_NOW)
        assert h.service.submit(intent, writer_lease_held=True, now=FIXED_NOW).submitted  # type: ignore[arg-type]
        h.broker._open_orders = (
            _working_order(order_ref=intent.correlation_id, permanent_id=_PERM_ID).model_copy(  # type: ignore[attr-defined]
                update={
                    "client_id": h.settings.ib_client_id,
                    "limit_price": intent.limit_price,  # type: ignore[attr-defined]
                    "filled_quantity": Decimal("1"),
                    "remaining_quantity": Decimal("0"),
                    "lifecycle": OrderLifecycle.FILLED,
                }
            ),
        )
        report = h.service.reconcile_on_restart_report(now=FIXED_NOW)
        assert report.proven[0].transition_applied is True
        assert report.proven[0].reason == "matching broker order or execution observed"
        assert len(report.applied_updates) == 1
        assert report.applied_updates[0].lifecycle is OrderLifecycle.FILLED
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.FILLED
        assert _harness_events(h, "intent-1")[-1][1:] == (_PERM_ID, _CLIENT_ID)  # on the row
        # a plain proven no-op (nothing recorded) keeps the original reason
        report_again = h.service.reconcile_on_restart_report(now=FIXED_NOW)
        assert report_again.proven == () or report_again.applied_updates == ()
    finally:
        h.close()


# =============================================================================================
# BP-2-F1 — kimi2's #249 pre-merge read, P3 #1: the operator-resolution guard fails closed with
# a TYPED refusal on contradictory persisted identity. `operator_resolve` catches
# OrderIdentityConflict from BOTH accessors and raises the method's own ValueError; nothing is
# written; the intent stays SUBMISSION_UNKNOWN. Pinned through the reconciler and the service.
# =============================================================================================


def _unknown_intent_with_identity_rows(
    h: _Harness, intent_id: str, rows: list[tuple[int, int]]
) -> None:
    """A SUBMISSION_UNKNOWN intent (entered today, FIXED_NOW — the evidence-window guard passes)
    whose persisted rows carry the given (permanent_id, client_id) pairs."""
    intent = _short_put_intent(intent_id=intent_id)
    _drive_to_confirmed(h, intent, FIXED_NOW)
    assert h.tracker_repo.record_transition(
        intent_id=intent_id,
        event_key=f"{intent_id}:presubmit:SUBMISSION_UNKNOWN",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
    )
    for n, (perm, client) in enumerate(rows, start=1):
        assert h.tracker_repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:identity:{n}",
            source="ORDER_STATUS",
            from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            current_account_id=PAPER_ACCOUNT,
            broker_order_id=9001,
            permanent_id=perm,
            client_id=client,
            occurred_at=FIXED_NOW,
        )


def _assert_typed_refusal(caught: pytest.ExceptionInfo, *ids: str) -> None:
    assert type(caught.value) is ValueError  # the method's own refusal type, never the accessor's
    assert "contradictory" in str(caught.value)
    for value in ids:
        assert value in str(caught.value)
    # the typed cause is inspectable, but nothing in the chain is RAISED as OrderIdentityConflict
    exc = caught.value
    seen = []
    while exc is not None:
        seen.append(type(exc))
        exc = exc.__cause__ or exc.__context__
    assert seen[0] is ValueError
    assert OrderIdentityConflict not in seen[:1]


def test_f1_1_operator_resolve_refuses_typed_on_contradictory_permanent_ids() -> None:
    h = _Harness(FakeBroker(), paper_settings())
    try:
        _unknown_intent_with_identity_rows(
            h, "intent-1", [(_PERM_ID, _CLIENT_ID), (_PERM_ID + 1, _CLIENT_ID)]
        )
        before = _harness_events(h, "intent-1")
        with pytest.raises(ValueError) as caught:
            h.service._reconciler.operator_resolve(
                "intent-1", operator_note="probe", current_account_id=PAPER_ACCOUNT, now=FIXED_NOW
            )
        _assert_typed_refusal(caught, "4242", "4243")
        assert _harness_events(h, "intent-1") == before  # zero rows written
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    finally:
        h.close()


def test_f1_2_the_service_wrapper_surfaces_the_same_typed_refusal() -> None:
    # OrderManagementService.resolve_submission_unknown (service.py:305-317) → the route maps
    # ValueError to 409 with detail=str(error) (api/routes/orders.py:361-365); pinned here at
    # the service level — there is no existing route test for /orders/{id}/resolve.
    h = _Harness(FakeBroker(), paper_settings())
    try:
        _unknown_intent_with_identity_rows(
            h, "intent-1", [(_PERM_ID, _CLIENT_ID), (_PERM_ID + 1, _CLIENT_ID)]
        )
        before = _harness_events(h, "intent-1")
        with pytest.raises(ValueError) as caught:
            h.service.resolve_submission_unknown("intent-1", operator_note="probe", now=FIXED_NOW)
        _assert_typed_refusal(caught, "4242", "4243")
        assert str(caught.value).startswith("persisted order identity is contradictory: ")
        assert _harness_events(h, "intent-1") == before
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    finally:
        h.close()


def test_f1_3_a_divergent_client_id_refuses_the_same_way_on_the_operator_path() -> None:
    h = _Harness(FakeBroker(), paper_settings())
    try:
        _unknown_intent_with_identity_rows(
            h, "intent-1", [(_PERM_ID, _CLIENT_ID), (_PERM_ID, _CLIENT_ID + 1)]
        )
        before = _harness_events(h, "intent-1")
        with pytest.raises(ValueError) as caught:
            h.service.resolve_submission_unknown("intent-1", operator_note="probe", now=FIXED_NOW)
        _assert_typed_refusal(caught, "17", "18")
        assert _harness_events(h, "intent-1") == before
        stored = h.service.get("intent-1")
        assert stored is not None and stored.status is OrderLifecycle.SUBMISSION_UNKNOWN
    finally:
        h.close()


def test_f1_3b_the_restart_loop_catch_is_unchanged() -> None:
    # symmetry: the restart path's typed catch (reconciliation_recovery.py:452-470) — its own pins
    # (test_r1_3, test_r2_1b) stay green; this pin reads the source so a refactor that drops one
    # side of the symmetry is caught by name
    src = (_ROOT / "src/chronos/orders/reconciliation_recovery.py").read_text(encoding="utf-8")
    i = src.index("def reconcile_report")
    j = src.index("def operator_resolve")
    restart, operator = src[i:j], src[j:]
    assert restart.count("except OrderIdentityConflict") == 1
    assert operator.count("except OrderIdentityConflict") == 1
    assert "self._tracker.client_id(" in restart and "self._tracker.client_id(" in operator

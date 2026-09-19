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
from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker, option_contract

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerOrder
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    resolve_from_broker_evidence,
)
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
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
    assert applied is True
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
    )
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

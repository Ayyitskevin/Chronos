from datetime import UTC

from sqlalchemy import inspect, select

from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.repositories import ApplicationEventRepository
from chronos.persistence.schema import SchemaVersionRow


def test_schema_initialization_creates_required_evidence_tables() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        names = set(inspect(database.engine).get_table_names())
    finally:
        database.dispose()

    assert {
        "application_events",
        "candidate_evaluations",
        "commissions",
        "fills",
        "guardrail_decisions",
        "order_drafts",
        "order_previews",
        "reconciliation_runs",
        "rejected_candidate_reasons",
        "schema_version",
        "strategy_basis_entries",
        "strategy_state",
        "submitted_orders",
        "wheel_cycles",
    } <= names
    assert SCHEMA_VERSION == 1


def test_application_events_are_append_only_and_queryable() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        repository = ApplicationEventRepository(database.sessions)
        event_id = repository.append(
            event_type="demo_started",
            message="Deterministic demo started",
            event_data={"mode": "demo"},
        )
        events = repository.recent()
    finally:
        database.dispose()

    assert event_id > 0
    assert len(events) == 1
    assert events[0].event_type == "demo_started"
    assert events[0].occurred_at.tzinfo is UTC


def test_sqlite_schema_timestamps_round_trip_as_aware_utc() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        with database.sessions() as session:
            applied_at = session.scalar(select(SchemaVersionRow.applied_at))
    finally:
        database.dispose()

    assert applied_at is not None
    assert applied_at.tzinfo is UTC

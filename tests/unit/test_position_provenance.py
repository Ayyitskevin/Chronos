"""AP-1 — allocation provenance for every observed position: recorded, never acted on.

Numbering follows the packet contract (run-20260913-pm/AP-1-brief-opus-builder.md):
1 migration + schema + drift checker · 2 the classifier, one fixture per class, precedence ·
3 append-only · 4 evidence-only (authority both ways, no broker) · 5 the operator read ·
6 record-only as BEHAVIOUR (the demo admission path is byte-identical with and without rows).

Synthetic / demo evidence only (sqlite tmp stores, stub broker); UNVERIFIED on live.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.repositories import ApplicationEventRepository
from chronos.persistence.schema import (
    Base,
    BasisEntryRow,
    PositionProvenanceRow,
    ReconciliationRunRow,
    WheelCycleRow,
)
from chronos.portfolio import provenance
from chronos.portfolio.provenance import (
    ACKNOWLEDGEMENT_KIND,
    ORIGIN_CLASSES,
    PositionProvenanceRepository,
    UnknownReconciliationRun,
    classify,
    position_key,
    record_position_provenance,
    snapshot_digest,
)
from chronos.utils.identifiers import account_fingerprint

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
_MIGRATION = "src/chronos/persistence/migrations/versions/0015_position_provenance.py"


# --- fixtures -------------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> Any:
    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    db.bind_scope(broker_mode="demo", environment="paper", account_id=DEMO_ACCOUNT_ID)
    yield db
    db.dispose()


@pytest.fixture
def unbound_database(tmp_path: Path) -> Any:
    """For the tests that seed a REAL binding: the admission fixture binds the scope itself."""

    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    yield db
    db.dispose()


def _position(symbol: str, con_id: int, quantity: str, security_type: str = "STK") -> dict:
    return {
        "contract": {"con_id": con_id, "symbol": symbol, "security_type": security_type},
        "quantity": quantity,
        "average_cost": "100.00",
        "position_key": f"{symbol}:{con_id}",  # BP-3's own key travels with the entry
    }


def _run_row(
    database: Database,
    run_id: str,
    positions: tuple[dict, ...],
    *,
    decisions: tuple[dict, ...] = (),
    started_at: datetime = _NOW,
) -> None:
    """Persist a reconciliation run the BP-3 way (masked account facts, positions, decisions)."""

    snapshot = {
        "account_facts": {
            "masked_account_id": "DU****567",
            "account_fingerprint": FINGERPRINT,
            "net_liquidation": "100000.50",
            "currency": "USD",
        },
        "positions": list(positions),
        "open_orders": [],
        "readiness": {"status": "RECONCILED", "reason": "parity", "generation": 1},
        "reasons": [],
    }
    with database.sessions.begin() as session:
        session.add(
            ReconciliationRunRow(
                id=run_id,
                trigger="periodic",
                status="RECONCILED",
                broker_snapshot=snapshot,
                decisions=list(decisions),
                started_at=started_at,
                completed_at=started_at + timedelta(seconds=2),
            )
        )


def _rows(database: Database) -> list[tuple[str, str, str, str, str, str]]:
    with database.sessions() as session:
        return [
            (
                row.observed_run_id,
                row.position_key,
                row.origin_class,
                row.evidence_ref,
                row.first_seen_run_id,
                str(row.quantity),
            )
            for row in session.scalars(
                sa.select(PositionProvenanceRow).order_by(PositionProvenanceRow.id.asc())
            )
        ]


def _seed_wheel(database: Database, symbol: str, con_id: int | None, cycle_id: str) -> None:
    with database.sessions.begin() as session:
        session.add(WheelCycleRow(id=cycle_id, symbol=symbol, status="OPEN", opened_at=_NOW))
        session.flush()  # the basis entry's foreign key needs the cycle row first
        session.add(
            BasisEntryRow(
                id=f"basis-{cycle_id}",
                wheel_cycle_id=cycle_id,
                symbol=symbol,
                entry_type="STOCK_ASSIGNMENT",
                amount=Decimal("100"),
                account_fingerprint=FINGERPRINT,
                contract_id=con_id,
                reconciliation_status="RECONCILED",
                occurred_at=_NOW,
            )
        )


def _seed_binding(database: Database, con_id: int, symbol: str) -> str:
    """A REAL binding through the admission path (the safety suite's fixture), never a hand row."""

    from tests.safety.test_managed_position_admission import (
        _ACCOUNT,
        _ORDER_REF,
        _Broker,
        _readiness,
        _Runner,
        _seed_filled_opening,
    )
    from tests.safety.test_managed_position_admission import (
        _NOW as ADMISSION_NOW,
    )

    from chronos.supervisor.position_admission import (
        AdmittedManagedPosition,
        ManagedPositionAdmission,
    )

    assert _ACCOUNT == DEMO_ACCOUNT_ID
    _seed_filled_opening(database)
    admission = ManagedPositionAdmission(
        connection=_Runner(_Broker()),
        sessions=database.sessions,
        readiness=_readiness(),
        account_id=_ACCOUNT,
    )
    admitted = admission.admit_opening(_ORDER_REF, ADMISSION_NOW)
    assert isinstance(admitted, AdmittedManagedPosition), admitted
    # the fixture's intent names the QQQ contract; the caller's con_id must be that one
    with database.sessions() as session:
        intent_con_id = session.scalar(
            sa.text("SELECT con_id FROM order_intents WHERE order_ref = :ref"), {"ref": _ORDER_REF}
        )
    assert intent_con_id == con_id, (intent_con_id, con_id)
    return admitted.state.plan.position_id


# --- 1. migration, schema mirror, drift checker ---------------------------------------------


def _alembic(db_path: Path) -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_ROOT / "src" / "chronos" / "persistence" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_1_migration_0015_creates_position_provenance_and_the_drift_checker_accepts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = [t for name, t in Base.metadata.tables.items() if name != "position_provenance"]
    Base.metadata.create_all(engine, tables=tables)  # a store from BEFORE this revision
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO schema_version (version, applied_at) VALUES (15, :now)"),
            {"now": _NOW.isoformat()},
        )
        # a pre-0015 order_events row carrying #249's identity columns: it must read back unchanged
        connection.execute(
            sa.text(
                "INSERT INTO order_events (intent_id, event_key, sequence, source, from_status, "
                "to_status, permanent_id, client_id, evidence, occurred_at, recorded_at) VALUES "
                "('intent-1', 'k-1', 1, 'broker', 'SUBMITTED', 'FILLED', 4242, 7, '{}', :now, :now)"
            ),
            {"now": _NOW.isoformat()},
        )
    engine.dispose()
    config = _alembic(db_path)
    command.stamp(config, "0014")
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "position_provenance" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("position_provenance")}
    assert columns == {
        "id",
        "account_fingerprint",
        "position_key",
        "origin_class",
        "evidence_ref",
        "first_seen_run_id",
        "observed_run_id",
        "quantity",
        "recorded_at",
    }
    unique = {
        tuple(u["column_names"]) for u in inspector.get_unique_constraints("position_provenance")
    }
    assert ("observed_run_id", "position_key", "origin_class", "evidence_ref") in unique
    with engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
        kept = connection.execute(
            sa.text("SELECT intent_id, permanent_id, client_id FROM order_events")
        ).all()
    engine.dispose()
    assert kept == [("intent-1", 4242, 7)]  # the v15 row survived the v16 upgrade unchanged
    assert version == SCHEMA_VERSION  # the chain head's version row, never a literal
    # schema.py mirrors the migration: the metadata table has the same columns
    mirrored = {column.name for column in Base.metadata.tables["position_provenance"].columns}
    assert mirrored == columns
    # the fail-closed drift checker accepts the upgraded store (needs SCHEMA_VERSION == 15)
    # the chain: 0013 → 0014 (#249, v15) → 0015 (this, v16); one head
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    (head,) = script.get_heads()
    assert head == "0015" and script.get_revision("0015").down_revision == "0014"
    upgraded = Database(f"sqlite:///{db_path}")
    try:
        upgraded.initialize()
    finally:
        upgraded.dispose()
    # the migration is the 0010 shape: table-only, idempotent, a schema_version row
    source = (_ROOT / _MIGRATION).read_text(encoding="utf-8")
    assert 'revision = "0015"' in source and "_V16_TABLES" in source
    assert "op.drop_table" in source and "DELETE FROM schema_version WHERE version" in source


# --- 2. the classifier: one fixture per class, precedence ----------------------------------


def test_2a_foreign_when_nothing_persisted_claims_the_position(database: Database) -> None:
    _run_row(database, "run-1", (_position("ZZZ", 9001, "50"),))
    with database.sessions() as session:
        run = session.get(ReconciliationRunRow, "run-1")
        assert run is not None
        (item,) = classify(session, run)
        digest = snapshot_digest(run.broker_snapshot)
    assert item.origin_class == "FOREIGN"
    assert item.position_key == "9001:STK:LONG"
    assert item.evidence_ref == f"run-1:{digest}"
    assert item.first_seen_run_id == "run-1" and item.observed_run_id == "run-1"
    assert item.quantity == Decimal("50")


def test_2b_wheel_when_a_basis_cycle_holds_the_symbol(database: Database) -> None:
    _seed_wheel(database, "QQQ", 320227571, "cycle-qqq-1")
    _run_row(database, "run-1", (_position("QQQ", 320227571, "100"),))
    with database.sessions() as session:
        run = session.get(ReconciliationRunRow, "run-1")
        assert run is not None
        (item,) = classify(session, run)
    assert (item.origin_class, item.evidence_ref) == ("WHEEL", "cycle-qqq-1")


def test_2c_manual_when_the_run_carries_an_operator_acknowledgement_for_the_key(
    database: Database,
) -> None:
    key = position_key(con_id=9002, security_type="STK", quantity=Decimal("-10"))
    assert key == "9002:STK:SHORT"
    _run_row(
        database,
        "run-1",
        (_position("MAN", 9002, "-10"),),
        decisions=({"kind": ACKNOWLEDGEMENT_KIND, "position_key": key, "by": "operator"},),
    )
    with database.sessions() as session:
        run = session.get(ReconciliationRunRow, "run-1")
        assert run is not None
        (item,) = classify(session, run)
    assert (item.origin_class, item.evidence_ref) == ("MANUAL", f"{ACKNOWLEDGEMENT_KIND}:run-1")


def test_2d_managed_when_a_real_binding_resolves_to_the_contract_and_managed_beats_wheel(
    unbound_database: Database,
) -> None:
    database = unbound_database
    con_id = 320227571
    binding_id = _seed_binding(database, con_id, "QQQ")
    _seed_wheel(database, "QQQ", con_id, "cycle-qqq-1")  # the same position also in a cycle
    _run_row(
        database,
        "run-1",
        (_position("QQQ", con_id, "3"), _position("ZZZ", 9001, "5")),
        decisions=({"kind": ACKNOWLEDGEMENT_KIND, "position_key": f"{con_id}:STK:LONG"},),
    )
    with database.sessions() as session:
        run = session.get(ReconciliationRunRow, "run-1")
        assert run is not None
        items = classify(session, run)
    by_key = {item.position_key: item for item in items}
    managed = by_key[f"{con_id}:STK:LONG"]
    # precedence: binding + cycle + acknowledgement all match → MANAGED, evidence = the binding
    assert (managed.origin_class, managed.evidence_ref) == ("MANAGED", binding_id)
    assert binding_id.startswith("CHR-POS-")
    assert by_key["9001:STK:LONG"].origin_class == "FOREIGN"
    assert set(ORIGIN_CLASSES) == {"MANAGED", "WHEEL", "FOREIGN", "MANUAL"}


# --- 3. append-only -------------------------------------------------------------------------


def test_3_a_class_change_is_a_new_row_and_a_replay_writes_nothing(
    unbound_database: Database,
) -> None:
    database = unbound_database
    database.bind_scope(broker_mode="ibkr", environment="paper", account_id=DEMO_ACCOUNT_ID)
    repo = PositionProvenanceRepository(database.sessions)
    _run_row(database, "run-1", (_position("QQQ", 320227571, "3"),), started_at=_NOW)
    assert repo.record_run("run-1") == 1
    assert repo.record_run("run-1") == 0  # replay: the same (run, key, class, evidence) once
    first = _rows(database)
    assert [row[2] for row in first] == ["FOREIGN"]

    _seed_binding(database, 320227571, "QQQ")  # at run 2 the same position is MANAGED
    _run_row(
        database,
        "run-2",
        (_position("QQQ", 320227571, "3"),),
        started_at=_NOW + timedelta(minutes=2),
    )
    assert repo.record_run("run-2") == 1
    rows = _rows(database)
    assert len(rows) == 2 and rows[0] == first[0]  # the FOREIGN row is byte-unchanged
    assert rows[1][2] == "MANAGED" and rows[1][0] == "run-2"
    assert rows[1][4] == "run-1"  # first seen at run 1, carried forward
    with database.sessions() as session:
        run_1 = session.get(ReconciliationRunRow, "run-1")
        assert run_1 is not None
        assert first[0][3] == f"run-1:{snapshot_digest(run_1.broker_snapshot)}"
    # the module has no update/delete path at all (K1(a))
    tree = ast.parse((_ROOT / "src/chronos/portfolio/provenance.py").read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not names & {"delete", "update", "prune", "purge", "truncate", "merge"}, names


def test_3b_the_raw_account_id_never_reaches_a_row(database: Database) -> None:
    _run_row(database, "run-1", (_position("QQQ", 320227571, "3"),))
    with database.sessions.begin() as session:
        run = session.get(ReconciliationRunRow, "run-1")
        assert run is not None
        snapshot = dict(run.broker_snapshot)
        snapshot["account_facts"] = {
            **snapshot["account_facts"],
            "account_fingerprint": DEMO_ACCOUNT_ID,
        }
        run.broker_snapshot = snapshot
    with pytest.raises(ValueError, match="raw broker account"):
        PositionProvenanceRepository(database.sessions).record_run("run-1")
    assert _rows(database) == []


# --- 4. evidence-only: authority both ways, no broker ----------------------------------------

_AUTHORITY = (
    "src/chronos/orders/risk.py",
    "src/chronos/orders/submission.py",
    "src/chronos/autonomy",
    "src/chronos/supervisor",
    "src/chronos/control",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return found


def test_4_the_classifier_is_evidence_only(database: Database, tmp_path: Path) -> None:
    module = _ROOT / "src/chronos/portfolio/provenance.py"
    outbound = _imports(module)
    forbidden = (
        "chronos.orders.risk",
        "chronos.orders.submission",
        "chronos.autonomy",
        "chronos.supervisor",
        "chronos.control",
        "chronos.broker",
    )
    assert not any(name.startswith(prefix) for name in outbound for prefix in forbidden), outbound
    inbound: list[str] = []
    for target in _AUTHORITY:
        paths = (
            [_ROOT / target] if target.endswith(".py") else sorted((_ROOT / target).rglob("*.py"))
        )
        for path in paths:
            if any(name.startswith("chronos.portfolio.provenance") for name in _imports(path)):
                inbound.append(str(path.relative_to(_ROOT)))
    assert inbound == []
    # positive control: the SAME scanner sees a planted import
    plant = tmp_path / "planted.py"
    plant.write_text("from chronos.portfolio.provenance import classify\n", encoding="utf-8")
    assert any(name.startswith("chronos.portfolio.provenance") for name in _imports(plant))
    # no broker call: a counting demo broker on the runtime path sees 0 calls across classify+write
    from tests.support.order_fakes import FakeBroker

    broker = FakeBroker(account_id=DEMO_ACCOUNT_ID)
    before = {k: v for k, v in vars(broker).items() if isinstance(v, int)}
    _run_row(database, "run-1", (_position("QQQ", 320227571, "3"),))
    assert PositionProvenanceRepository(database.sessions).record_run("run-1") == 1
    assert {k: v for k, v in vars(broker).items() if isinstance(v, int)} == before
    # and transitively: a fresh interpreter importing the module loads no chronos.broker*
    probe = (
        "import sys, chronos.portfolio.provenance, chronos.cli.main\n"
        "print('|'.join(sorted(m for m in sys.modules if m.startswith('chronos.broker'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "BROKER_MODE": "demo"},
        check=True,
    )
    assert result.stdout.strip() == ""


# --- 5. the operator read -------------------------------------------------------------------


def test_5_the_cli_prints_the_rows_masked_and_refuses_an_unknown_run(
    database: Database, tmp_path: Path
) -> None:
    _run_row(database, "run-1", (_position("QQQ", 320227571, "3"), _position("ZZZ", 9001, "-2")))
    assert PositionProvenanceRepository(database.sessions).record_run("run-1") == 2
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'chronos.db'}",
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *argv], cwd=_ROOT, capture_output=True, text=True, env=env
        )

    real = run("-m", "chronos.cli", "position-provenance", "--run", "run-1")
    assert real.returncode == 0, real.stderr
    lines = [json.loads(line) for line in real.stdout.splitlines()]
    assert [(line["position_key"], line["origin_class"]) for line in lines] == [
        ("320227571:STK:LONG", "FOREIGN"),
        ("9001:STK:SHORT", "FOREIGN"),
    ]
    assert all(line["account_fingerprint"] == FINGERPRINT for line in lines)
    assert DEMO_ACCOUNT_ID not in real.stdout
    module_form = run(
        "-m", "chronos.cli.reconciliation_commands", "position-provenance", "--run", "run-1"
    )
    assert module_form.returncode == 0 and module_form.stdout == real.stdout
    unknown = run("-m", "chronos.cli", "position-provenance", "--run", "run-404")
    assert unknown.returncode == 2 and unknown.stdout == ""
    assert (
        unknown.stderr.strip() == "position-provenance: no persisted reconciliation run 'run-404'"
    )
    for argv in (("-m", "chronos.cli"), ("-m", "chronos.cli.reconciliation_commands")):
        assert run(*argv, "position-provenance", "--help").returncode == 0
    with pytest.raises(UnknownReconciliationRun):
        PositionProvenanceRepository(database.sessions).rows_for_run("run-404")


# --- 6. record-only as BEHAVIOUR ------------------------------------------------------------


def test_6_the_demo_admission_path_is_byte_identical_with_and_without_provenance_rows(
    tmp_path: Path,
) -> None:
    from tests.safety.test_managed_position_admission import (
        _ACCOUNT,
        _ORDER_REF,
        _Broker,
        _readiness,
        _Runner,
        _seed_filled_opening,
    )
    from tests.safety.test_managed_position_admission import (
        _NOW as ADMISSION_NOW,
    )

    from chronos.supervisor.position_admission import ManagedPositionAdmission

    def admit(with_rows: bool) -> tuple[dict[str, Any], int]:
        db = Database(f"sqlite:///{tmp_path / ('with' if with_rows else 'without')}.db")
        db.initialize()
        try:
            _seed_filled_opening(db)  # binds the scope; a FILLED QQQ opening the fixture's way
            written = 0
            if with_rows:
                key = position_key(con_id=9002, security_type="STK", quantity=Decimal("-10"))
                _run_row(
                    db,
                    "run-1",
                    (_position("ZZZ", 9001, "5"), _position("MAN", 9002, "-10")),
                    decisions=({"kind": ACKNOWLEDGEMENT_KIND, "position_key": key},),
                )
                written = PositionProvenanceRepository(db.sessions).record_run("run-1")
                assert [row[2] for row in _rows(db)] == ["FOREIGN", "MANUAL"]  # positive control
            admission = ManagedPositionAdmission(
                connection=_Runner(_Broker()),
                sessions=db.sessions,
                readiness=_readiness(),
                account_id=_ACCOUNT,
            )
            result = admission.admit_opening(_ORDER_REF, ADMISSION_NOW)
            return json.loads(json.dumps(asdict(result), sort_keys=True, default=str)), written
        finally:
            db.dispose()

    without, none_written = admit(False)
    with_rows, written = admit(True)
    assert none_written == 0 and written == 2
    assert with_rows == without  # the decision never saw the rows
    assert "position_id" in json.dumps(with_rows)


def test_6b_a_provenance_failure_is_a_typed_application_event_never_a_raise(
    database: Database,
) -> None:
    assert record_position_provenance(database.sessions, "run-404") is None
    events = ApplicationEventRepository(database.sessions).recent(limit=5)
    assert [event.event_type for event in events] == [provenance.EVENT_TYPE]
    assert events[0].event_data == {"run_id": "run-404"}
    assert "UnknownReconciliationRun" in events[0].message

"""AP-1b — the operator acknowledgement producer for MANUAL provenance: record-only end to end.

Numbering follows the packet contract (run-20260913-pm/AP-1b-brief-opus-builder.md):
1 schema (migration 0016, mirror, the bump set, drift checker) · 2 append-only (second row,
withdrawal = a superseding row, no UPDATE/DELETE, ORM listeners refuse) · 3 timing + the
end-to-end MANUAL/withdrawal pins + authority both ways + record-only behaviour · 4 the CLI.

Synthetic / demo evidence only (sqlite tmp stores, stub broker); UNVERIFIED on live.
"""

from __future__ import annotations

import ast
import importlib
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
from alembic.script import ScriptDirectory
from tests.unit.test_reconciliation_runs_persist import _snapshot

from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.domain.enums import ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.persistence import acknowledgement_repository as ack_module
from chronos.persistence.acknowledgement_repository import (
    ACKNOWLEDGEMENT_KIND,
    AcknowledgementImmutable,
    AcknowledgementRepository,
    InvalidAcknowledgement,
    UnknownAcknowledgement,
    acknowledgement_decisions,
    operator_fingerprint,
)
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.reconciliation_repository import (
    ReconciliationRunRecorder,
)
from chronos.persistence.repositories import ApplicationEventRepository
from chronos.persistence.schema import (
    Base,
    PositionAcknowledgementRow,
    PositionProvenanceRow,
    ReconciliationRunRow,
)
from chronos.portfolio.provenance import PositionProvenanceRepository, position_key
from chronos.utils.identifiers import account_fingerprint

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
OPERATOR = operator_fingerprint(user="kevin", host="flow")
_MODULE = "src/chronos/persistence/acknowledgement_repository.py"
_MIGRATION = "src/chronos/persistence/migrations/versions/0016_position_acknowledgements.py"
#: BP-3's `_snapshot()` reports one position SYM0/con_id 1000, quantity 100 (STK, LONG)
SYM0_KEY = position_key(con_id=1000, security_type="STK", quantity=Decimal("100"))


@pytest.fixture
def database(tmp_path: Path) -> Any:
    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    db.bind_scope(broker_mode="demo", environment="paper", account_id=DEMO_ACCOUNT_ID)
    yield db
    db.dispose()


def _ack_rows(database: Database) -> list[tuple[int, str, str, str, int | None]]:
    with database.sessions() as session:
        return [
            (row.id, row.position_key, row.note, row.operator_fingerprint, row.superseded_by)
            for row in session.scalars(
                sa.select(PositionAcknowledgementRow).order_by(PositionAcknowledgementRow.id)
            )
        ]


def _provenance(database: Database) -> list[tuple[str, str, str, str]]:
    with database.sessions() as session:
        return [
            (row.observed_run_id, row.position_key, row.origin_class, row.evidence_ref)
            for row in session.scalars(
                sa.select(PositionProvenanceRow).order_by(PositionProvenanceRow.id)
            )
        ]


def _decisions(database: Database, run_id: str) -> list[dict[str, Any]]:
    with database.sessions() as session:
        run = session.get(ReconciliationRunRow, run_id)
        assert run is not None
        return list(run.decisions)


# --- 1. schema ------------------------------------------------------------------------------


def _alembic(db_path: Path) -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_ROOT / "src" / "chronos" / "persistence" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_1_migration_0016_creates_the_table_the_bump_set_moved_and_the_drift_checker_accepts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = [t for name, t in Base.metadata.tables.items() if name != "position_acknowledgements"]
    Base.metadata.create_all(engine, tables=tables)  # a store from BEFORE this revision
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO schema_version (version, applied_at) VALUES (16, :now)"),
            {"now": _NOW.isoformat()},
        )
        # pre-0016 rows on the v16 store: a persisted run and its provenance row must survive
        connection.execute(
            sa.text(
                "INSERT INTO reconciliation_runs (id, trigger, status, broker_snapshot, decisions, "
                "started_at) VALUES ('run-v16', 'periodic', 'RECONCILED', '{}', '[]', :now)"
            ),
            {"now": _NOW.isoformat()},
        )
        connection.execute(
            sa.text(
                "INSERT INTO position_provenance (account_fingerprint, position_key, origin_class, "
                "evidence_ref, first_seen_run_id, observed_run_id, quantity, recorded_at) VALUES "
                "(:fp, '1000:STK:LONG', 'FOREIGN', 'run-v16:digest', 'run-v16', 'run-v16', "
                "100, :now)"
            ),
            {"fp": FINGERPRINT, "now": _NOW.isoformat()},
        )
    engine.dispose()
    config = _alembic(db_path)
    command.stamp(config, "0015")
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "position_acknowledgements" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("position_acknowledgements")}
    assert columns == {
        "id",
        "position_key",
        "note",
        "acknowledged_at",
        "operator_fingerprint",
        "superseded_by",
    }
    assert columns == {c.name for c in Base.metadata.tables["position_acknowledgements"].columns}
    foreign = inspector.get_foreign_keys("position_acknowledgements")
    assert [(fk["constrained_columns"], fk["referred_table"]) for fk in foreign] == [
        (["superseded_by"], "position_acknowledgements")
    ]
    with engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
        kept = connection.execute(
            sa.text("SELECT observed_run_id, position_key, origin_class FROM position_provenance")
        ).all()
    engine.dispose()
    assert kept == [("run-v16", "1000:STK:LONG", "FOREIGN")]  # the v16 row survived unchanged
    # chain-relative: the store's version row is what the chain HEAD writes, and SCHEMA_VERSION
    # follows it — never a literal a later migration breaks
    script = ScriptDirectory.from_config(config)
    (head,) = script.get_heads()
    head_module = importlib.import_module(
        f"chronos.persistence.migrations.versions.{Path(script.get_revision(head).path).stem}"
    )
    assert version == SCHEMA_VERSION == head_module._SCHEMA_VERSION
    assert script.get_revision("0016").down_revision == "0015"  # 0016's own parent
    assert "0016" in {r.revision for r in script.iterate_revisions(head, "base")}
    gate = (_ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert f'_MIGRATION_HEAD: Final[str] = "{head}"' in gate
    upgraded = Database(f"sqlite:///{db_path}")  # the fail-closed drift checker accepts it
    try:
        upgraded.initialize()
    finally:
        upgraded.dispose()
    source = (_ROOT / _MIGRATION).read_text(encoding="utf-8")
    revision = Path(_MIGRATION).stem.split("_")[0]  # 0016's own number, from its path
    assert f'revision = "{revision}"' in source and f"_V{int(revision) + 1}_TABLES" in source
    assert "op.drop_table" in source and "DELETE FROM schema_version WHERE version" in source


# --- 2. append-only -------------------------------------------------------------------------


def test_2_acknowledgements_are_appended_never_edited(database: Database) -> None:
    repo = AcknowledgementRepository(database.sessions)
    first = repo.acknowledge(
        position_key=SYM0_KEY,
        note="hedge leg from the desk",
        operator_fingerprint=OPERATOR,
        now=_NOW,
    )
    second = repo.acknowledge(
        position_key=SYM0_KEY, note="re-checked", operator_fingerprint=OPERATOR, now=_NOW
    )
    assert (first, second) == (1, 2)  # a second acknowledge for the same key is a second row
    assert [r.id for r in repo.current()] == [1, 2]
    withdrawal = repo.withdraw(
        acknowledgement_id=first, note="mistaken", operator_fingerprint=OPERATOR, now=_NOW
    )
    rows = _ack_rows(database)
    assert rows == [
        (1, SYM0_KEY, "hedge leg from the desk", OPERATOR, None),  # byte-unchanged
        (2, SYM0_KEY, "re-checked", OPERATOR, None),
        (3, SYM0_KEY, "mistaken", OPERATOR, 1),  # the withdrawal is a NEW row pointing at 1
    ]
    assert withdrawal == 3
    assert [r.id for r in repo.current()] == [2]  # 1 superseded; 3 is a withdrawal, never current
    with pytest.raises(UnknownAcknowledgement):
        repo.withdraw(acknowledgement_id=first, note="twice", operator_fingerprint=OPERATOR)
    with pytest.raises(UnknownAcknowledgement):
        repo.withdraw(acknowledgement_id=99, note="ghost", operator_fingerprint=OPERATOR)
    with pytest.raises(UnknownAcknowledgement):
        repo.withdraw(acknowledgement_id=withdrawal, note="meta", operator_fingerprint=OPERATOR)
    assert _ack_rows(database) == rows  # the refusals wrote nothing
    # the decisions the recorder will carry: one per CURRENT key, the newest id
    assert acknowledgement_decisions(repo.current()) == [
        {"kind": ACKNOWLEDGEMENT_KIND, "position_key": SYM0_KEY, "acknowledgement_id": 2}
    ]


def test_2b_no_update_or_delete_exists_and_the_orm_listeners_refuse_both(
    database: Database,
) -> None:
    source = (_ROOT / _MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {"update", "delete", "merge", "prune", "purge", "truncate"}, names
    docstrings = {
        id(node.body[0].value)
        for node in [tree, *ast.walk(tree)]
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    assert not [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
        and ("UPDATE " in n.value.upper() or "DELETE " in n.value.upper())
    ]
    repo = AcknowledgementRepository(database.sessions)
    row_id = repo.acknowledge(position_key=SYM0_KEY, note="n", operator_fingerprint=OPERATOR)
    with pytest.raises(AcknowledgementImmutable), database.sessions.begin() as session:
        row = session.get(PositionAcknowledgementRow, row_id)
        assert row is not None
        row.note = "edited"
    with pytest.raises(AcknowledgementImmutable), database.sessions.begin() as session:
        row = session.get(PositionAcknowledgementRow, row_id)
        assert row is not None
        session.delete(row)
    assert _ack_rows(database) == [(1, SYM0_KEY, "n", OPERATOR, None)]


def test_2c_shape_is_validated_and_the_raw_account_id_is_refused(database: Database) -> None:
    repo = AcknowledgementRepository(database.sessions)
    with pytest.raises(InvalidAcknowledgement, match="position_key"):
        repo.acknowledge(position_key="SYM0:1000", note="n", operator_fingerprint=OPERATOR)
    with pytest.raises(InvalidAcknowledgement, match="note"):
        repo.acknowledge(position_key=SYM0_KEY, note="two\nlines", operator_fingerprint=OPERATOR)
    with pytest.raises(InvalidAcknowledgement, match="note"):
        repo.acknowledge(position_key=SYM0_KEY, note="", operator_fingerprint=OPERATOR)
    with pytest.raises(InvalidAcknowledgement, match="fingerprint"):
        repo.acknowledge(position_key=SYM0_KEY, note="n", operator_fingerprint="kevin")
    with pytest.raises(ValueError, match="raw broker account"):
        repo.acknowledge(
            position_key=SYM0_KEY, note=f"for {DEMO_ACCOUNT_ID}", operator_fingerprint=OPERATOR
        )
    assert _ack_rows(database) == []
    assert len(OPERATOR) == 16 and OPERATOR != "kevin" and "kevin" not in OPERATOR
    assert operator_fingerprint(user="kevin", host="flow") == OPERATOR  # stable per seat
    assert operator_fingerprint(user="kevin", host="mickey") != OPERATOR


# --- 3. timing, end to end, authority, record-only ------------------------------------------


def test_3_the_recorder_carries_current_acknowledgements_after_the_latch_published(
    database: Database,
) -> None:
    repo = AcknowledgementRepository(database.sessions)
    first = repo.acknowledge(
        position_key=SYM0_KEY, note="desk hedge", operator_fingerprint=OPERATOR
    )
    readiness = ReconciliationReadiness(session_id="recon-ap1b")
    generation = readiness.begin_reconciliation("parity")
    seen_at_read: list[ReconciliationStatus] = []
    recorder = ReconciliationRunRecorder(database.sessions)
    original_current = recorder._acknowledgements.current

    def probe() -> Any:
        seen_at_read.append(readiness.snapshot().status)
        return original_current()

    recorder._acknowledgements.current = probe  # type: ignore[method-assign]
    # BP-3's order: the latch publishes, THEN the recorder is called (runtime.py:185-209)
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="parity",
        reconciled_at=_NOW,
    )
    assert recorder.record(_snapshot("periodic-00000001-run")) is True
    assert seen_at_read == [ReconciliationStatus.RECONCILED]  # the read never preceded the latch
    decisions = _decisions(database, "periodic-00000001-run")
    assert decisions[-1] == {
        "kind": ACKNOWLEDGEMENT_KIND,
        "position_key": SYM0_KEY,
        "acknowledgement_id": first,
    }
    # AP-1's classifier (unchanged) records MANUAL from that decision
    assert _provenance(database) == [
        (
            "periodic-00000001-run",
            SYM0_KEY,
            "MANUAL",
            f"{ACKNOWLEDGEMENT_KIND}:periodic-00000001-run",
        )
    ]
    # the source-order pin: the acknowledgement read is inside record(), after the latch site
    source = (_ROOT / "src/chronos/persistence/reconciliation_repository.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("class ReconciliationRunRecorder") :]
    assert body.index("self._with_acknowledgements(snapshot)") < body.index(
        "self._runs.record_run("
    )
    runtime = (_ROOT / "src/chronos/runtime.py").read_text(encoding="utf-8")
    assert runtime.index("self.reconciliation_readiness.complete(") < runtime.index(
        "recorder.record("
    )
    assert "acknowledg" not in runtime  # the runtime never reads them itself


def test_3b_an_acknowledged_foreign_position_becomes_manual_then_foreign_again_after_withdrawal(
    database: Database,
) -> None:
    recorder = ReconciliationRunRecorder(database.sessions)
    repo = AcknowledgementRepository(database.sessions)
    assert recorder.record(_snapshot("periodic-00000001-run", started=_NOW)) is True
    ack = repo.acknowledge(position_key=SYM0_KEY, note="desk hedge", operator_fingerprint=OPERATOR)
    assert (
        recorder.record(
            _snapshot("periodic-00000002-run", generation=2, started=_NOW + timedelta(minutes=2))
        )
        is True
    )
    repo.withdraw(acknowledgement_id=ack, note="closed out", operator_fingerprint=OPERATOR)
    assert (
        recorder.record(
            _snapshot("periodic-00000003-run", generation=3, started=_NOW + timedelta(minutes=4))
        )
        is True
    )
    rows = _provenance(database)
    assert [(r[0], r[2]) for r in rows] == [
        ("periodic-00000001-run", "FOREIGN"),
        ("periodic-00000002-run", "MANUAL"),
        ("periodic-00000003-run", "FOREIGN"),  # a NEW row; the MANUAL row is untouched
    ]
    assert rows[1][3] == f"{ACKNOWLEDGEMENT_KIND}:periodic-00000002-run"
    assert all(r[1] == SYM0_KEY for r in rows)
    assert ACKNOWLEDGEMENT_KIND not in json.dumps(_decisions(database, "periodic-00000003-run"))
    with database.sessions() as session:
        first_seen = {
            row.first_seen_run_id for row in session.scalars(sa.select(PositionProvenanceRow))
        }
    assert first_seen == {"periodic-00000001-run"}


def test_3c_a_read_failure_is_a_typed_event_and_the_run_is_still_written(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: Any) -> Any:
        raise RuntimeError("acknowledgement store unavailable")

    monkeypatch.setattr(ack_module.AcknowledgementRepository, "current", explode)
    recorder = ReconciliationRunRecorder(database.sessions)
    assert recorder.record(_snapshot("periodic-00000001-run")) is True
    decisions = _decisions(database, "periodic-00000001-run")
    assert decisions[-1] == {
        "kind": "acknowledgements_unavailable",
        "acknowledgements_unavailable": True,
    }
    events = ApplicationEventRepository(database.sessions).recent(limit=5)
    assert [e.event_type for e in events] == ["acknowledgements_unavailable"]
    assert "RuntimeError: acknowledgement store unavailable" in events[0].message
    assert [r[2] for r in _provenance(database)] == ["FOREIGN"]  # classified without it


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


def test_3d_nothing_with_authority_reads_the_acknowledgements_and_the_cli_never_touches_a_broker(
    tmp_path: Path,
) -> None:
    outbound = _imports(_ROOT / _MODULE)
    forbidden = (
        "chronos.orders.risk",
        "chronos.orders.submission",
        "chronos.autonomy",
        "chronos.supervisor",
        "chronos.control",
        "chronos.broker",
        "chronos.runtime",
    )
    assert not any(n.startswith(p) for n in outbound for p in forbidden), outbound
    inbound: list[str] = []
    for target in _AUTHORITY:
        paths = (
            [_ROOT / target] if target.endswith(".py") else sorted((_ROOT / target).rglob("*.py"))
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if any(
                n.startswith("chronos.persistence.acknowledgement_repository")
                for n in _imports(path)
            ):
                inbound.append(str(path.relative_to(_ROOT)))
            if "PositionAcknowledgementRow" in text or "position_acknowledgements" in text:
                inbound.append(str(path.relative_to(_ROOT)))
    assert inbound == []
    plant = tmp_path / "planted.py"
    plant.write_text(
        "from chronos.persistence.acknowledgement_repository import AcknowledgementRepository\n",
        encoding="utf-8",
    )
    assert any(
        n.startswith("chronos.persistence.acknowledgement_repository") for n in _imports(plant)
    )
    cli = _imports(_ROOT / "src/chronos/cli/reconciliation_commands.py")
    assert not any(n.startswith("chronos.broker") or n == "chronos.runtime" for n in cli)
    probe = (
        "import sys, chronos.cli.main, chronos.persistence.acknowledgement_repository\n"
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


def test_3e_the_demo_admission_path_is_byte_identical_with_and_without_an_acknowledgement(
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

    foreign_key = position_key(con_id=9001, security_type="STK", quantity=Decimal("5"))

    def admit(acknowledged: bool) -> tuple[dict[str, Any], list[tuple[str, str, str, str]]]:
        db = Database(f"sqlite:///{tmp_path / ('with' if acknowledged else 'without')}.db")
        db.initialize()
        try:
            _seed_filled_opening(db)  # binds the scope the fixture's way
            if acknowledged:
                AcknowledgementRepository(db.sessions).acknowledge(
                    position_key=foreign_key, note="desk", operator_fingerprint=OPERATOR
                )
            with db.sessions.begin() as session:
                session.add(
                    ReconciliationRunRow(
                        id="run-1",
                        trigger="periodic",
                        status="RECONCILED",
                        broker_snapshot={
                            "account_facts": {
                                "masked_account_id": "DU****567",
                                "account_fingerprint": account_fingerprint(_ACCOUNT),
                            },
                            "positions": [
                                {
                                    "contract": {
                                        "con_id": 9001,
                                        "symbol": "ZZZ",
                                        "security_type": "STK",
                                    },
                                    "quantity": "5",
                                    "position_key": "ZZZ:9001",
                                }
                            ],
                            "open_orders": [],
                            "readiness": {"status": "RECONCILED", "reason": "p", "generation": 1},
                            "reasons": [],
                        },
                        decisions=acknowledgement_decisions(
                            AcknowledgementRepository(db.sessions).current()
                        ),
                        started_at=_NOW,
                        completed_at=_NOW,
                    )
                )
            PositionProvenanceRepository(db.sessions).record_run("run-1")
            admission = ManagedPositionAdmission(
                connection=_Runner(_Broker()),
                sessions=db.sessions,
                readiness=_readiness(),
                account_id=_ACCOUNT,
            )
            result = admission.admit_opening(_ORDER_REF, ADMISSION_NOW)
            decision = json.loads(json.dumps(asdict(result), sort_keys=True, default=str))
            return decision, _provenance(db)
        finally:
            db.dispose()

    without, rows_without = admit(False)
    with_ack, rows_with = admit(True)
    assert [r[2] for r in rows_without] == ["FOREIGN"]
    assert [r[2] for r in rows_with] == ["MANUAL"]  # positive control: the ledger DID change
    assert with_ack == without  # the admission decision never saw it
    assert "position_id" in json.dumps(with_ack)


# --- 4. the CLI -----------------------------------------------------------------------------


def test_4_the_cli_writes_one_row_per_invocation_and_lists_them_masked(
    database: Database, tmp_path: Path
) -> None:
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
            [sys.executable, "-m", "chronos.cli", *argv],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def rows() -> list[tuple[int, str, str, str, int | None]]:
        fresh = Database(env["DATABASE_URL"])  # a fresh session sees the subprocess's commit
        try:
            return _ack_rows(fresh)
        finally:
            fresh.dispose()

    first = run("position-acknowledge", "--key", SYM0_KEY, "--note", "desk hedge")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"acknowledgement_id": 1, "position_key": SYM0_KEY}
    assert rows()[0][:3] == (1, SYM0_KEY, "desk hedge")
    assert len(rows()) == 1  # exactly one row per invocation
    bad_key = run("position-acknowledge", "--key", "SYM0:1000", "--note", "x")
    assert bad_key.returncode == 64 and "position_key" in bad_key.stderr
    bad_note = run("position-acknowledge", "--key", SYM0_KEY, "--note", "")
    assert bad_note.returncode == 64 and "note" in bad_note.stderr
    no_key = run("position-acknowledge", "--note", "x")
    assert no_key.returncode == 64
    withdrawn = run("position-acknowledge", "--withdraw", "1", "--note", "closed")
    assert withdrawn.returncode == 0, withdrawn.stderr
    assert json.loads(withdrawn.stdout) == {"acknowledgement_id": 2, "withdraws": 1}
    twice = run("position-acknowledge", "--withdraw", "1", "--note", "again")
    assert twice.returncode == 2 and twice.stderr.strip() == (
        "position-acknowledge: no current acknowledgement 1"
    )
    assert len(rows()) == 2  # the refusals wrote nothing
    listing = run("position-acknowledgements", "--last", "10")
    assert listing.returncode == 0, listing.stderr
    lines = [json.loads(line) for line in listing.stdout.splitlines()]
    assert [(line["id"], line["superseded_by"]) for line in lines] == [(1, None), (2, 1)]
    assert all(len(line["operator_fingerprint"]) == 16 for line in lines)
    assert "kevin" not in listing.stdout and DEMO_ACCOUNT_ID not in listing.stdout
    for argv in (("position-acknowledge",), ("position-acknowledgements",)):
        assert run(*argv, "--help").returncode == 0
    module_form = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.cli.reconciliation_commands",
            "position-acknowledgements",
            "--last",
            "10",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert module_form.returncode == 0 and module_form.stdout == listing.stdout

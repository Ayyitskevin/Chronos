"""BP-3 — account facts persist per reconciliation run: the ``reconciliation_runs`` writer.

Numbered to the packet contract. K1 (a): append-only, no pruning. K2 (a): the raw broker account
id never reaches a row — masked id + fingerprint only. K3 (a): foreign/manual positions are
recorded, never acted on. Every pin runs on the demo broker / synthetic evidence; UNVERIFIED on
live until the M4 read-only session (K4 unanswered).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from tests.support.order_fakes import FakeBroker

from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciliationReport
from chronos.persistence.database import Database
from chronos.persistence.reconciliation_repository import (
    TRIGGERS,
    ReconciliationRepository,
    ReconciliationRunConflict,
    ReconciliationRunRecorder,
    ReconciliationSnapshot,
)
from chronos.persistence.repositories import ApplicationEventRepository
from chronos.persistence.schema import ReconciliationRunRow
from chronos.runtime import AppRuntime
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationPositionView,
    ReconciliationResult,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.utils.identifiers import account_fingerprint

_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'runs.db'}")
    db.initialize()
    db.bind_scope(broker_mode="demo", environment="paper", account_id=DEMO_ACCOUNT_ID)
    yield db
    db.dispose()


def _snapshot_view(*, positions: int = 1, open_orders: int = 0) -> ReconciliationSnapshotView:
    return ReconciliationSnapshotView(
        environment=DisplayEnvironment.PAPER,
        data_quality=DataQuality.DELAYED,
        account=ReconciliationAccountView(
            masked_account_id="DU****567",
            net_liquidation=Decimal("100000.50"),
            total_cash=Decimal("25000.25"),
            buying_power=Decimal("50000.00"),
            currency="USD",
            as_of=NOW,
        ),
        positions=tuple(
            ReconciliationPositionView(
                contract=UnderlyingContract(con_id=1000 + i, symbol=f"SYM{i}"),
                quantity=Decimal("100"),
                average_cost=Decimal("10.00"),
            )
            for i in range(positions)
        ),
        open_orders=(),
        execution_count=0,
        server_time_start=NOW,
        server_time_end=NOW,
        captured_at=NOW - timedelta(seconds=3),
        window_seconds=0.5,
        server_window_seconds=0.5,
    )


def _result(
    *, status: ReconciliationStatus = ReconciliationStatus.RECONCILED, with_snapshot: bool = True
) -> ReconciliationResult:
    return ReconciliationResult(
        status=status,
        snapshot=_snapshot_view() if with_snapshot else None,
        symbols=(
            SymbolReconciliation(
                symbol="SYM0",
                status=status,
                stage=WheelStage.LONG_STOCK,
                stock_shares=Decimal("100"),
                unencumbered_shares=Decimal("100"),
                short_put_contracts=Decimal("0"),
                short_call_contracts=Decimal("0"),
                pending_put_contracts=Decimal("0"),
                pending_call_contracts=Decimal("0"),
                manual_review_required=False,
                reasons=("parity",),
            ),
        ),
        reasons=("full broker/local parity proven",),
    )


def _snapshot(
    run_id: str, *, trigger: str = "periodic", started: datetime = NOW, **overrides: Any
) -> ReconciliationSnapshot:
    return ReconciliationSnapshot.from_result(
        run_id=run_id,
        trigger=cast(Any, trigger),
        portfolio=overrides.get("portfolio", _result()),
        readiness_status=ReconciliationStatus.RECONCILED,
        readiness_reason="full broker/local parity proven with no sent-active order intents",
        generation=overrides.get("generation", 1),
        started_at=started,
        completed_at=started + timedelta(seconds=1),
        account_fingerprint=FINGERPRINT,
    )


def _rows(database: Database) -> list[tuple[str, str, str]]:
    with database.engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                sa.text(
                    "SELECT id, trigger, status FROM reconciliation_runs ORDER BY started_at, id"
                )
            )
        ]


# --- 1. the writer: one row per run id, masked, idempotent, typed conflict ----------------------


def test_1_record_run_writes_one_masked_row_per_run_id_and_never_the_raw_account_id(
    database: Database,
) -> None:
    repo = ReconciliationRepository(database.sessions)
    assert repo.record_run(_snapshot("periodic-00000001-run")) is True
    assert _rows(database) == [("periodic-00000001-run", "periodic", "RECONCILED")]

    with database.engine.connect() as connection:
        broker_snapshot, decisions = connection.execute(
            sa.text("SELECT broker_snapshot, decisions FROM reconciliation_runs")
        ).one()
    persisted = json.loads(broker_snapshot)
    assert DEMO_ACCOUNT_ID not in broker_snapshot and DEMO_ACCOUNT_ID not in decisions
    assert persisted["account_facts"]["masked_account_id"] == "DU****567"
    assert persisted["account_facts"]["account_fingerprint"] == FINGERPRINT
    assert persisted["account_facts"]["net_liquidation"] == "100000.50"
    assert persisted["account_facts"]["total_cash"] == "25000.25"
    assert persisted["account_facts"]["buying_power"] == "50000.00"
    assert persisted["account_facts"]["currency"] == "USD"
    assert persisted["account_facts"]["as_of"] == NOW.isoformat().replace("+00:00", "Z")
    assert persisted["evidence_age_seconds"] == pytest.approx(4.0)
    assert persisted["positions"][0]["position_key"] == "SYM0:1000"  # AP-1's stable reference
    assert persisted["readiness"] == {
        "status": "RECONCILED",
        "reason": "full broker/local parity proven with no sent-active order intents",
        "generation": 1,
    }
    assert json.loads(decisions)[0]["symbol"] == "SYM0"

    # replay: same id, same bytes → idempotent, one row
    assert repo.record_run(_snapshot("periodic-00000001-run")) is False
    assert len(_rows(database)) == 1


def test_1b_the_raw_account_id_is_refused_before_any_write(database: Database) -> None:
    repo = ReconciliationRepository(database.sessions)
    tainted = _snapshot("startup-00000002-run", trigger="startup").model_copy(
        update={"reasons": (f"scope {DEMO_ACCOUNT_ID} observed",)}
    )
    with pytest.raises(ValueError, match="raw broker account IDs"):
        repo.record_run(tainted)
    assert _rows(database) == []


def test_1c_the_same_run_id_with_different_bytes_is_a_typed_refusal(database: Database) -> None:
    repo = ReconciliationRepository(database.sessions)
    assert repo.record_run(_snapshot("periodic-00000003-run"))
    divergent = _snapshot(
        "periodic-00000003-run", portfolio=_result(status=ReconciliationStatus.PENDING)
    )
    with pytest.raises(ReconciliationRunConflict, match="never overwritten"):
        repo.record_run(divergent)
    assert _rows(database) == [("periodic-00000003-run", "periodic", "RECONCILED")]


# --- 2. the call sites: after the decided result, after the latch; failure → application event ---


class _RestartService:
    account_id = DEMO_ACCOUNT_ID

    def __init__(self, report: OrderRestartReconciliationReport) -> None:
        self._report = report

    def reconcile_on_restart_report(self, *, now: datetime) -> OrderRestartReconciliationReport:
        return self._report


class _Portfolio:
    def __init__(
        self, result: ReconciliationResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result or _result()
        self._error = error

    def reconcile(self) -> ReconciliationResult:
        if self._error is not None:
            raise self._error
        return self._result


class _SyncConnection:
    def __init__(self, broker: FakeBroker, account_id: str = DEMO_ACCOUNT_ID) -> None:
        self._broker = broker
        self._account_id = account_id

    def run(self, coroutine: object) -> object:
        import asyncio

        return asyncio.run(cast(Any, coroutine))

    def connection_status(self) -> Any:
        return SimpleNamespace(
            connected=True, state=ConnectionState.CONNECTED, account_id=self._account_id
        )


def _runtime(
    database: Database | None, *, portfolio: _Portfolio | None = None, recorder: Any = None
) -> AppRuntime:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.reconciliation_readiness = ReconciliationReadiness()
    runtime.order_management = cast(
        Any,
        _RestartService(
            OrderRestartReconciliationReport(
                proven=(), unresolved=(), remaining_active=(), applied_updates=()
            )
        ),
    )
    runtime.reconciliation = cast(Any, portfolio or _Portfolio())
    broker = FakeBroker(account_id=DEMO_ACCOUNT_ID)
    runtime.broker = cast(Any, broker)
    runtime.connection = cast(Any, _SyncConnection(broker))
    if recorder is not None:
        runtime.reconciliation_runs = recorder
    elif database is not None:
        runtime.reconciliation_runs = ReconciliationRunRecorder(database.sessions)
    return runtime


def test_2_the_runtime_writes_one_row_after_the_decided_result_with_the_trigger_named(
    database: Database,
) -> None:
    runtime = _runtime(database)
    report = runtime.reconcile_submission_readiness(now=NOW, trigger="startup")
    assert report.readiness.status is ReconciliationStatus.RECONCILED
    rows = _rows(database)
    assert len(rows) == 1 and rows[0][1] == "startup" and rows[0][2] == "RECONCILED"
    assert rows[0][0].startswith("startup-")
    # a second pass is a second run (a new generation → a new id), never an overwrite
    runtime.reconcile_submission_readiness(now=NOW + timedelta(minutes=1), trigger="periodic")
    rows = _rows(database)
    assert [r[1] for r in rows] == ["startup", "periodic"]
    # a caller that names nothing is recorded as unattributed — never a guessed label
    runtime.reconcile_submission_readiness(now=NOW + timedelta(minutes=2))
    assert [r[1] for r in _rows(database)][-1] == "unattributed"


def test_2b_a_decided_pending_result_is_recorded_but_an_undecided_failure_is_not(
    database: Database,
) -> None:
    runtime = _runtime(database, portfolio=_Portfolio(_result(status=ReconciliationStatus.PENDING)))
    report = runtime.reconcile_submission_readiness(now=NOW, trigger="operator")
    assert report.readiness.status is ReconciliationStatus.PENDING
    assert _rows(database) == [(_rows(database)[0][0], "operator", "PENDING")]

    failing = _runtime(database, portfolio=_Portfolio(error=RuntimeError("broker read failed")))
    with pytest.raises(RuntimeError):
        failing.reconcile_submission_readiness(now=NOW + timedelta(minutes=1), trigger="periodic")
    assert len(_rows(database)) == 1  # nothing written for the refused/undecided pass


class _RaisingRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def record(self, snapshot: ReconciliationSnapshot) -> None:
        self.calls += 1
        raise RuntimeError("database gone")


def test_2c_a_persistence_failure_is_a_typed_application_event_and_the_latch_still_publishes(
    database: Database,
) -> None:
    # the recorder wraps a failing repository: the pass returns its decided result, the latch
    # published RECONCILED, and the failure is one application_events row
    recorder = ReconciliationRunRecorder(database.sessions)

    class _Broken:
        def record_run(self, snapshot: ReconciliationSnapshot) -> bool:
            raise RuntimeError("disk full")

    recorder._runs = cast(Any, _Broken())
    runtime = _runtime(None, recorder=recorder)
    report = runtime.reconcile_submission_readiness(now=NOW, trigger="startup")
    assert report.readiness.status is ReconciliationStatus.RECONCILED
    assert runtime.reconciliation_readiness.snapshot().status is ReconciliationStatus.RECONCILED
    events = ApplicationEventRepository(database.sessions).recent(limit=5)
    assert [e.event_type for e in events] == ["reconciliation_run_persist_failed"]
    assert events[0].severity == "WARNING" and "disk full" in events[0].message
    assert events[0].event_data["trigger"] == "startup"
    assert _rows(database) == []


def test_2d_the_latch_publishes_before_the_row_is_written(database: Database) -> None:
    # call-order pin with a probe recorder: at the moment record() runs the latch is already
    # RECONCILED for this generation (persistence never precedes publication — ADR-0020)
    seen: list[ReconciliationStatus] = []
    runtime = _runtime(None)

    class _Probe:
        def record(self, snapshot: ReconciliationSnapshot) -> bool:
            seen.append(runtime.reconciliation_readiness.snapshot().status)
            return True

    runtime.reconciliation_runs = cast(Any, _Probe())
    runtime.reconcile_submission_readiness(now=NOW, trigger="startup")
    assert seen == [ReconciliationStatus.RECONCILED]
    # and the source order in runtime.py says the same: complete(...) precedes recorder.record(...)
    src = (_ROOT / "src/chronos/runtime.py").read_text(encoding="utf-8")
    body = src[
        src.index("def _reconcile_submission_readiness_generation") : src.index("def close(self)")
    ]
    assert (
        body.index("self.reconciliation_readiness.complete(")
        < body.rindex("self.reconciliation_readiness.complete(")
        < body.index("recorder.record(")
    )
    assert "finally" not in body  # never written from a finally


# --- 3. authority line, both ways -----------------------------------------------------------

_AUTHORITY = (
    "src/chronos/orders/risk.py",
    "src/chronos/orders/submission.py",
    "src/chronos/autonomy",
    "src/chronos/supervisor",
    "src/chronos/control",
)


def _imports_module(path: Path, module: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name == module or a.name.startswith(module + ".") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if any(n == module or n.startswith(module + ".") for n in names):
                return True
    return False


def test_3_no_authority_package_imports_the_repository_and_the_scan_detects_a_plant(
    tmp_path: Path,
) -> None:
    module = "chronos.persistence.reconciliation_repository"
    offenders = []
    for target in _AUTHORITY:
        p = _ROOT / target
        for f in [p] if p.is_file() else sorted(p.rglob("*.py")):
            if _imports_module(f, module):
                offenders.append(str(f.relative_to(_ROOT)))
    assert offenders == []
    plant = tmp_path / "planted.py"
    plant.write_text(
        "from chronos.persistence import reconciliation_repository\n", encoding="utf-8"
    )
    assert _imports_module(plant, module)
    plant.write_text(
        "import chronos.persistence.reconciliation_repository as r\n", encoding="utf-8"
    )
    assert _imports_module(plant, module)


_LIVE_ACCOUNT_READS = ("src/chronos/orders/evidence.py", "src/chronos/orders/submission.py")
_BASE_REF_FALLBACKS = ("origin/main", "main")


def _packet_base_ref() -> str:
    """The ref the branch is measured against: env override, else origin/main, else main.

    A frozen sha would fail the moment main is merged into the branch for a change BP-3 never
    made (the r3 lesson); the merge-base with a live ref moves with those merges. A ref that
    does not resolve is a loud failure naming what was tried — never a silent skip.
    """

    import os

    tried: list[str] = []
    for ref in (os.environ.get("CHRONOS_PACKET_BASE_REF"), *_BASE_REF_FALLBACKS):
        if not ref:
            continue
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
        )
        if resolved.returncode == 0:
            return ref
        tried.append(ref)
    pytest.fail(
        "no packet base ref resolves (tried "
        + ", ".join(tried)
        + "); set CHRONOS_PACKET_BASE_REF to the ref this branch is measured against"
    )


def test_3b_the_live_account_summary_reads_are_untouched_against_the_base() -> None:
    """BP-3 touches neither live AccountSummary read — measured against the branch's merge-base
    with its base ref, so merges of main into the branch (other packets' edits to these files)
    do not fail a pin about THIS branch's edits."""

    ref = _packet_base_ref()
    base = subprocess.run(
        ["git", "merge-base", "HEAD", ref], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert base, ref
    for path in _LIVE_ACCOUNT_READS:
        before = subprocess.run(
            ["git", "show", f"{base}:{path}"], cwd=_ROOT, capture_output=True, text=True, check=True
        ).stdout
        after = (_ROOT / path).read_text(encoding="utf-8")
        assert ast.dump(ast.parse(before)) == ast.dump(ast.parse(after)), (
            f"{path} differs from the merge-base {base[:12]} with {ref}: this branch edited a "
            "live AccountSummary read"
        )


def test_3c_the_base_comparison_uses_no_frozen_sha() -> None:
    """The pin's base is a ref, never a 40-hex literal (in its body or via a module constant)."""

    import inspect
    import re

    source = inspect.getsource(
        test_3b_the_live_account_summary_reads_are_untouched_against_the_base
    )
    source += inspect.getsource(_packet_base_ref)
    assert re.search(r"[0-9a-f]{40}", source) is None, "a frozen sha is used as the base again"
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    frozen = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and re.fullmatch(r"[0-9a-f]{40}", node.value.value)
    }
    used = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    assert not (frozen & used), frozen & used
    assert "merge-base" in source and "CHRONOS_PACKET_BASE_REF" in source


def test_3b2_a_missing_base_ref_is_a_loud_failure_never_a_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRONOS_PACKET_BASE_REF", "refs/heads/does-not-exist")
    monkeypatch.setattr(sys.modules[__name__], "_BASE_REF_FALLBACKS", ("refs/heads/nor-this",))
    with pytest.raises(pytest.fail.Exception, match="no packet base ref resolves") as raised:
        _packet_base_ref()
    assert "refs/heads/does-not-exist, refs/heads/nor-this" in str(raised.value)
    assert "CHRONOS_PACKET_BASE_REF" in str(raised.value)
    # and the override wins when it resolves
    monkeypatch.setenv("CHRONOS_PACKET_BASE_REF", "HEAD")
    assert _packet_base_ref() == "HEAD"


def test_3d_the_r3_commit_touched_exactly_this_test_file() -> None:
    """The BP-3 r3 delta is one file — pinned on the commit itself, not on an open range."""

    found = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--grep=(BP-3 r3)"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert found, "the BP-3 r3 commit is not in this branch's history yet"
    touched = subprocess.run(
        ["git", "show", "--name-only", "--format=", found],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert touched == ["tests/unit/test_reconciliation_runs_persist.py"], touched


# --- 4. the restart-recovery drill + the CLI ---------------------------------------------------


def test_4_restart_recovery_drill_rows_survive_a_fresh_session_and_the_cli_reads_them_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "drill.db"
    database = Database(f"sqlite:///{db_path}")
    database.initialize()
    database.bind_scope(broker_mode="demo", environment="paper", account_id=DEMO_ACCOUNT_ID)
    written: list[ReconciliationSnapshot] = []
    runtime = _runtime(database)
    # the four brief trigger kinds + operator through the runtime path; reconnect/order_fill
    # have no caller at this head, so they are written through the repository directly
    for i, trigger in enumerate(("startup", "periodic", "operator", "periodic")):
        runtime.reconcile_submission_readiness(
            now=NOW + timedelta(minutes=i), trigger=cast(Any, trigger)
        )
    repo = ReconciliationRepository(database.sessions)
    for i, trigger in enumerate(("reconnect", "order_fill"), start=10):
        snap = _snapshot(
            f"{trigger}-{i:08d}-drill", trigger=trigger, started=NOW + timedelta(minutes=i)
        )
        assert repo.record_run(snap)
        written.append(snap)
    expected = repo.recent(limit=6)
    assert [r.trigger for r in expected] == [
        "startup",
        "periodic",
        "operator",
        "periodic",
        "reconnect",
        "order_fill",
    ]
    assert all(DEMO_ACCOUNT_ID not in json.dumps(r.broker_snapshot) for r in expected)
    del runtime, repo
    database.dispose()  # process state discarded

    fresh = Database(f"sqlite:///{db_path}")
    try:
        again = ReconciliationRepository(fresh.sessions).recent(limit=6)
    finally:
        fresh.dispose()
    assert (
        again == expected
    )  # a fresh session reads exactly what was written, ordered by started_at
    assert [r.started_at for r in again] == sorted(r.started_at for r in again)

    # the CLI: opens only the database (never the broker), prints one JSON line per run
    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    import os

    full_env = {**os.environ, **env}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.cli.reconciliation_commands",
            "reconciliation-runs",
            "--last",
            "6",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [line["trigger"] for line in lines] == [
        "startup",
        "periodic",
        "operator",
        "periodic",
        "reconnect",
        "order_fill",
    ]
    assert [line["run_id"] for line in lines] == [r.run_id for r in expected]
    assert all(DEMO_ACCOUNT_ID not in line_text for line_text in proc.stdout.splitlines())
    assert lines[-1]["broker_snapshot"] == expected[-1].broker_snapshot
    helped = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.cli.reconciliation_commands",
            "reconciliation-runs",
            "--help",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert helped.returncode == 0 and "--last" in helped.stdout


def test_4b_the_cli_module_never_touches_a_broker() -> None:
    src = (_ROOT / "src/chronos/cli/reconciliation_commands.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("chronos.broker") or m == "chronos.runtime" for m in imported), (
        imported
    )
    assert "build_runtime" not in src and "Broker" not in src.replace("no broker", "").replace(
        "a broker", ""
    )


def test_4c_every_trigger_label_is_typed_and_only_these(database: Database) -> None:
    assert TRIGGERS == (
        "startup",
        "reconnect",
        "order_fill",
        "periodic",
        "operator",
        "unattributed",
    )
    with pytest.raises(ValueError):
        _snapshot("x-00000000-run", trigger="cron")
    # K1 (a): nothing prunes — the repository has no delete path (identifiers and SQL text
    # scanned by AST; docstrings may say "never pruned", code may not prune)
    tree = ast.parse(
        (_ROOT / "src/chronos/persistence/reconciliation_repository.py").read_text(encoding="utf-8")
    )
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not any(n.lower() in {"delete", "prune", "purge", "truncate"} for n in names), names
    sql = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "DELETE" in n.value.upper()
        and "FROM" in n.value.upper()
    ]
    assert sql == []
    assert ReconciliationRunRow.__tablename__ == "reconciliation_runs"


# --- r1. the REAL callers name their trigger; the default stays honest; the CLI is registered ---


def _triggers(database: Database) -> list[str]:
    return [row[1] for row in _rows(database)]


def test_r1_1a_the_startup_path_writes_a_startup_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive the REAL caller: the app lifespan (api/main.py) on a demo boot with a tmp database."""

    from fastapi.testclient import TestClient

    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    url = f"sqlite:///{tmp_path / 'chronos.db'}"
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("ALLOW_ORDER_TRANSMIT", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "kill.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "baseline.json"))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()):
            pass
    finally:
        get_settings.cache_clear()
    fresh = Database(url)
    try:
        records = ReconciliationRepository(fresh.sessions).recent(limit=10)
    finally:
        fresh.dispose()
    # exactly one startup row; the periodic task sleeps its first interval (>= 120 s) before
    # its first cycle, so nothing else can have written inside the lifespan window
    assert [record.trigger for record in records] == ["startup"], records
    assert records[0].run_id.startswith("startup-")
    assert DEMO_ACCOUNT_ID not in json.dumps(records[0].broker_snapshot)


def test_r1_1b_the_operator_route_writes_an_operator_row(database: Database) -> None:
    """Drive the REAL caller: routes/orders.py `reconcile_orders` with the runtime on its state."""

    from chronos.api.routes.orders import reconcile_orders

    runtime = _runtime(database)
    response = reconcile_orders(cast(Any, SimpleNamespace(runtime=runtime)))
    assert response.status == "RECONCILED"
    assert _triggers(database) == ["operator"]


def test_r1_1c_reconcile_once_writes_a_periodic_row(database: Database) -> None:
    """Drive the REAL caller: reconciliation_loop.reconcile_once (the periodic task's one cycle)."""

    from chronos.api.reconciliation_loop import reconcile_once

    runtime = _runtime(database)
    assert reconcile_once(runtime) == (True, False)
    assert _triggers(database) == ["periodic"]


def test_r1_1d_the_no_kwarg_default_is_still_unattributed_never_a_guess(database: Database) -> None:
    import inspect

    parameter = inspect.signature(AppRuntime.reconcile_submission_readiness).parameters["trigger"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "unattributed"
    runtime = _runtime(database)
    runtime.reconcile_submission_readiness()  # a caller that says nothing
    assert _triggers(database) == ["unattributed"]


def test_r1_2_the_command_is_registered_on_the_real_cli_the_add_selection_command_way() -> None:
    env = {
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    import os

    full_env = {**os.environ, **env}
    real = subprocess.run(
        [sys.executable, "-m", "chronos.cli", "reconciliation-runs", "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert real.returncode == 0, real.stderr
    assert "--last" in real.stdout
    module_form = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.cli.reconciliation_commands",
            "reconciliation-runs",
            "--help",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert module_form.returncode == 0, module_form.stderr
    # the registration shape: one import beside `add_selection_command`, one call beside the
    # other top-level `add_*_commands(sub)` calls — and nothing else in cli/main.py mentions it
    source = (_ROOT / "src/chronos/cli/main.py").read_text(encoding="utf-8")
    assert (
        source.count(
            "from chronos.cli.reconciliation_commands import add_reconciliation_runs_command\n"
        )
        == 1
    )
    assert source.count("add_reconciliation_runs_command(sub)\n") == 1
    assert source.count("reconciliation_runs") == 2


def test_r1_4_the_real_caller_set_is_startup_operator_periodic_and_nothing_is_invented() -> None:
    """Every production caller under api/ names its trigger; the set is the three real callers."""

    found: dict[str, str] = {}
    for path in sorted((_ROOT / "src/chronos/api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reconcile_submission_readiness"
            ):
                continue
            labels = [
                kw.value.value
                for kw in node.keywords
                if kw.arg == "trigger" and isinstance(kw.value, ast.Constant)
            ]
            assert len(labels) == 1, f"{path}:{node.lineno} names no literal trigger"
            found[f"{path.relative_to(_ROOT)}:{node.lineno}"] = labels[0]
    assert sorted(found.values()) == ["operator", "periodic", "startup"], found
    assert set(found) == {
        "src/chronos/api/main.py:330",
        "src/chronos/api/reconciliation_loop.py:102",
        "src/chronos/api/routes/orders.py:216",
    }, found
    assert set(found.values()) <= set(TRIGGERS)


# --- r2. the operator CLI registers the command WITHOUT loading chronos.broker ---

_REPOSITORY = "src/chronos/persistence/reconciliation_repository.py"


def test_r2_1_the_cut_is_the_type_checking_import_and_the_docstring_and_nothing_else() -> None:
    after = (_ROOT / _REPOSITORY).read_text(encoding="utf-8")
    tree = ast.parse(after)
    runtime_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "chronos.services.reconciliation"
    ]
    assert runtime_imports == [], "the result type must not be imported at runtime"
    blocks = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    ]
    assert len(blocks) == 1
    assert [
        (node.module, [alias.name for alias in node.names])
        for node in blocks[0].body
        if isinstance(node, ast.ImportFrom)
    ] == [("chronos.services.reconciliation", ["ReconciliationResult"])]
    assert "from __future__ import annotations" in after  # the annotation stays a string
    assert "build_parser" not in after and "import chronos.cli" not in after  # no lazy tricks
    # nothing else in the module moved — stated as a DELTA against the retained BP-3 PASS head
    # (refs/packets/BP-3/2), not as equality to a dead head: outside the run recorder (whose AP-1
    # hook is pinned by test_ap1r1_3) every top-level statement is AST-equal once the r2 hunks
    # (docstring, the result-type imports, the TYPE_CHECKING block) are set aside
    kept_before, _ = _recorder_delta(_git("show", f"{_BP3_HEAD}:{_REPOSITORY}"))
    kept_after, _ = _recorder_delta(after)
    assert kept_after == kept_before
    # the docstring names the three real callers, the honest default and the unemitted vocabulary
    docstring = ast.get_docstring(tree) or ""
    for phrase in ("startup", "operator", "periodic", "unattributed", "reconnect", "order_fill"):
        assert phrase in docstring, phrase
    assert "no caller emits" in docstring


def test_r2_2_importing_the_cli_never_loads_a_broker_module_and_the_probe_can_see_one() -> None:
    """A fresh interpreter (the platform pin's own technique): the CLI entry points leave no
    ``chronos.broker*`` in ``sys.modules``; the positive control proves the probe sees a broker
    module when one IS loaded."""

    import os

    env = {
        **os.environ,
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def loaded_broker_modules(*modules: str) -> list[str]:
        probe = (
            "import sys\n"
            + "".join(f"import {module}\n" for module in modules)
            + "print('|'.join(sorted(m for m in sys.modules if m.startswith('chronos.broker'))))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=True,
        )
        return [m for m in result.stdout.strip().split("|") if m]

    assert (
        loaded_broker_modules(
            "chronos.cli.main",
            "chronos.cli.reconciliation_commands",
            "chronos.persistence.reconciliation_repository",
        )
        == []
    )
    # positive control: the service module DOES pull the broker base, and the probe reports it
    assert "chronos.broker.base" in loaded_broker_modules("chronos.services.reconciliation")


# --- AP-1 r1. the schema bump + the provenance hook in the run recorder ---

_AP1_HEAD = "0413f1c93b3ca7e458aa04264d6dc0aea4b60811"  # pragma: allowlist secret
_BP3_HEAD = "6150839b5e67db7e17733ac24de3bcd675cd8a9b"  # pragma: allowlist secret
_RATIFIED = (
    "scripts/verify_release_artifact.py",
    "src/chronos/persistence/database.py",
    "src/chronos/persistence/reconciliation_repository.py",
    "tests/integration/test_migrations.py",
    "tests/unit/test_database.py",
    "tests/unit/test_reconciliation_runs_persist.py",
)


def _git(*argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_ap1r1_1_the_ratification_patch_is_applied_as_is_and_nothing_else_moves() -> None:
    names = tuple(sorted(_git("diff", "--name-only", f"{_AP1_HEAD}..HEAD").split()))
    assert names == _RATIFIED, names
    numstat = {
        line.split("\t")[2]: (int(line.split("\t")[0]), int(line.split("\t")[1]))
        for line in _git("diff", "--numstat", f"{_AP1_HEAD}..HEAD").splitlines()
    }
    # the five non-test hunks of logs/AP-1-ratification.patch, line for line
    assert numstat["src/chronos/persistence/database.py"] == (1, 1)
    assert numstat["scripts/verify_release_artifact.py"] == (1, 1)
    assert numstat["tests/unit/test_database.py"] == (1, 1)
    assert numstat["tests/integration/test_migrations.py"] == (6, 1)
    assert numstat["src/chronos/persistence/reconciliation_repository.py"] == (7, 1)
    diff = _git("diff", f"{_AP1_HEAD}..HEAD", "--", *_RATIFIED[:5])
    for expected in (
        "-SCHEMA_VERSION = 14\n+SCHEMA_VERSION = 15\n",
        '-_MIGRATION_HEAD: Final[str] = "0013"\n+_MIGRATION_HEAD: Final[str] = "0015"\n',
        '+    "_V15_TABLES": ("0015_position_provenance", "_V15_TABLES"),\n',
        '+    "position_provenance",\n',
        "-    assert version == SCHEMA_VERSION == 14\n+    assert version == SCHEMA_VERSION",
        "-    assert SCHEMA_VERSION == 14\n+    assert SCHEMA_VERSION == 15",
        "+from chronos.portfolio.provenance import record_position_provenance\n",
        "+        self._sessions = sessions\n",
        "+            record_position_provenance(self._sessions, snapshot.run_id)\n",
    ):
        assert expected in diff, expected


def test_ap1r1_2_the_chain_head_is_0015_revising_0013_here_and_the_pins_say_15() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from chronos.persistence.database import SCHEMA_VERSION

    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_ROOT / "src" / "chronos" / "persistence" / "migrations")
    )
    script = ScriptDirectory.from_config(config)
    assert list(script.get_heads()) == ["0015"]
    head = script.get_revision("0015")
    assert head is not None and head.down_revision == "0013"  # BP-2's 0014 is not in this stack
    assert "0014" not in {revision.revision for revision in script.walk_revisions()}
    assert SCHEMA_VERSION == 15
    gate = (_ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert '_MIGRATION_HEAD: Final[str] = "0015"' in gate


def _recorder_delta(source: str) -> tuple[list[str], dict[str, Any]]:
    """Top-level dumps (minus the r2/AP-1 hunks) and the recorder's method bodies, separately."""

    tree = ast.parse(source)
    kept: list[str] = []
    recorder: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the module docstring
        if isinstance(node, ast.ImportFrom) and node.module in (
            "chronos.services.reconciliation",  # r1's runtime import (absent after r2)
            "chronos.portfolio.provenance",  # AP-1's hook import (absent before AP-1)
        ):
            continue
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            node.names = [alias for alias in node.names if alias.name != "TYPE_CHECKING"]
        if isinstance(node, ast.ClassDef) and node.name == "ReconciliationRunRecorder":
            for method in node.body:
                if isinstance(method, ast.FunctionDef):
                    recorder[method.name] = method
            continue
        kept.append(ast.dump(node))
    return kept, recorder


def test_ap1r1_3_the_hook_is_the_only_recorder_change_and_runs_only_after_a_written_row() -> None:
    before = _git("show", f"{_BP3_HEAD}:{_REPOSITORY}")
    after = (_ROOT / _REPOSITORY).read_text(encoding="utf-8")
    kept_before, rec_before = _recorder_delta(before)
    kept_after, rec_after = _recorder_delta(after)
    assert kept_after == kept_before  # every other top-level statement is AST-equal to 6150839
    assert set(rec_after) == set(rec_before) == {"__init__", "record"}
    # __init__: exactly one added statement, `self._sessions = sessions`, first
    init_before = [ast.dump(s) for s in rec_before["__init__"].body]
    init_after = [ast.dump(s) for s in rec_after["__init__"].body]
    assert init_after[1:] == init_before
    assert init_after[0] == ast.dump(ast.parse("self._sessions = sessions").body[0])
    # record(): the try's success line binds `written`; the failure branch is AST-identical;
    # the hook and the return come AFTER the try, never inside it
    body_before, body_after = rec_before["record"].body, rec_after["record"].body
    assert len(body_before) == 1 and isinstance(body_before[0], ast.Try)
    assert [type(s).__name__ for s in body_after] == ["Try", "If", "Return"]
    try_before, try_after = body_before[0], body_after[0]
    assert [ast.dump(h) for h in try_after.handlers] == [ast.dump(h) for h in try_before.handlers]
    assert ast.dump(try_before.body[0]) == ast.dump(
        ast.parse("return self._runs.record_run(snapshot)").body[0]
    )
    assert ast.dump(try_after.body[0]) == ast.dump(
        ast.parse("written = self._runs.record_run(snapshot)").body[0]
    )
    hook = body_after[1]
    assert isinstance(hook, ast.If) and ast.dump(hook.test) == ast.dump(
        ast.Name(id="written", ctx=ast.Load())
    )
    assert [ast.dump(s) for s in hook.body] == [
        ast.dump(ast.parse("record_position_provenance(self._sessions, snapshot.run_id)").body[0])
    ]
    assert hook.orelse == []
    assert ast.dump(body_after[2]) == ast.dump(ast.parse("return written").body[0])
    # the import is top-level (no lazy import inside the recorder)
    assert "from chronos.portfolio.provenance import record_position_provenance\n" in after
    assert "    from chronos.portfolio.provenance" not in after


def test_ap1r1_3b_a_demo_boot_writes_a_provenance_row_for_every_position_through_the_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from chronos.api.main import create_app
    from chronos.config.settings import get_settings
    from chronos.portfolio.provenance import PositionProvenanceRepository, position_key

    url = f"sqlite:///{tmp_path / 'chronos.db'}"
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("ALLOW_ORDER_TRANSMIT", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "kill.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "baseline.json"))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()):
            pass
    finally:
        get_settings.cache_clear()
    fresh = Database(url)
    try:
        (run,) = ReconciliationRepository(fresh.sessions).recent(limit=10)
        rows = PositionProvenanceRepository(fresh.sessions).rows_for_run(run.run_id)
    finally:
        fresh.dispose()
    positions = run.broker_snapshot["positions"]
    assert run.trigger == "startup" and len(positions) > 0
    expected_keys = sorted(
        position_key(
            con_id=entry["contract"]["con_id"],
            security_type=entry["contract"]["security_type"],
            quantity=Decimal(str(entry["quantity"])),
        )
        for entry in positions
    )
    assert sorted(row.position_key for row in rows) == expected_keys
    assert {row.origin_class for row in rows} == {"FOREIGN"}  # the demo book has no bindings
    assert all(row.observed_run_id == run.run_id == row.first_seen_run_id for row in rows)
    assert DEMO_ACCOUNT_ID not in json.dumps([row.account_fingerprint for row in rows])


def test_ap1r1_3c_a_failing_provenance_write_never_escapes_the_recorder(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chronos.portfolio import provenance as provenance_module

    def explode(self: Any, run_id: str) -> int:
        raise RuntimeError("provenance store unavailable")

    monkeypatch.setattr(provenance_module.PositionProvenanceRepository, "record_run", explode)
    recorder = ReconciliationRunRecorder(database.sessions)
    assert recorder.record(_snapshot("startup-00000001-run", trigger="startup")) is True
    assert _rows(database) == [("startup-00000001-run", "startup", "RECONCILED")]
    events = ApplicationEventRepository(database.sessions).recent(limit=5)
    assert [event.event_type for event in events] == ["position_provenance_persist_failed"]
    assert "RuntimeError: provenance store unavailable" in events[0].message


def test_ap1r1_3d_a_failed_run_write_never_calls_the_provenance_hook(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chronos.persistence.reconciliation_repository as recorder_module

    calls: list[str] = []
    monkeypatch.setattr(
        recorder_module, "record_position_provenance", lambda sessions, run_id: calls.append(run_id)
    )
    recorder = ReconciliationRunRecorder(database.sessions)
    monkeypatch.setattr(
        recorder._runs,
        "record_run",
        lambda snapshot: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert recorder.record(_snapshot("startup-00000001-run", trigger="startup")) is None
    assert calls == []  # the failure branch alone ran
    assert _rows(database) == []
    # and a successful write calls it exactly once with the run id
    monkeypatch.setattr(recorder._runs, "record_run", lambda snapshot: True)
    assert recorder.record(_snapshot("startup-00000002-run", trigger="startup")) is True
    assert calls == ["startup-00000002-run"]


def test_ap1r1_4_none_of_the_ratified_files_is_a_current_state_input() -> None:
    builder = (_ROOT / "scripts/build_current_state.py").read_text(encoding="utf-8")
    for path in _RATIFIED:
        assert path not in builder, path

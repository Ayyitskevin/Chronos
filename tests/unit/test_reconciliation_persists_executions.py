"""BP-1b: broker executions persist from the reconciliation seam.

``ReconciliationCoordinator`` takes an optional ``ExecutionRepository``; once a pass's broker
observation is accepted and the result is decided, every execution the two snapshots carried is
recorded once by ``execution_id`` (the union of both snapshots), with the local draft's
``correlation_id`` when the execution's ``order_ref`` maps to a submitted order and ``None``
otherwise. A refusal or an unexpected error from the writer becomes one typed log line and the
pass completes with its report unchanged. Numbered to the packet contract: 1x injection, 2x the
seam and idempotency, 3x correlation, 4x the typed refusal at the seam (the repository-level pin is
``tests/unit/test_execution_repository.py::test_bp1b_4a_…``), 5x never raise, 6x the authority
line. Every broker here is scripted or the demo broker; no gateway, no network.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from chronos.broker.base import Broker
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    OptionRight,
    OrderSide,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    ConnectionStatus,
    LocalReconciliationEvidence,
    OptionContract,
)
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import (
    ExecutionConflict,
    ExecutionRepository,
    UnknownCorrelation,
)
from chronos.persistence.schema import CommissionRow, FillRow, OrderDraftRow, SubmittedOrderRow
from chronos.services import reconciliation as reconciliation_module
from chronos.services.reconciliation import ReconciliationCoordinator, ReconciliationResult

_T = TypeVar("_T")
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "chronos"
ACCOUNT_ID = "DU1234567"
NOW = datetime(2026, 9, 19, 13, 0, tzinfo=UTC)
FORBIDDEN_PREFIXES = (
    "chronos.orders.risk",
    "chronos.orders.submission",
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.control",
    "chronos.execution",
)
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


# ------------------------------------------------------------------ fixtures and helpers


def _option(symbol: str = "AAPL") -> OptionContract:
    return OptionContract(
        con_id=2002,
        symbol=symbol,
        expiration=date(2026, 10, 16),
        strike=Decimal("150"),
        right=OptionRight.PUT,
        multiplier=Decimal("100"),
        trading_class=symbol,
        local_symbol=f"{symbol}-261016-P-150",
        currency="USD",
    )


def _execution(
    execution_id: str = "0000e0d5.64f1a2b3.01.01",
    *,
    order_ref: str | None = "CHR-FOREIGN-1",
    account_id: str = ACCOUNT_ID,
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=account_id,
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        order_ref=order_ref,
        contract=_option(),
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("2.00"),
        timestamp=NOW,
        commission=Decimal("0.65"),
        commission_currency="USD",
    )


def _status(account_id: str) -> ConnectionStatus:
    return ConnectionStatus(
        state=ConnectionState.CONNECTED,
        environment=DisplayEnvironment.PAPER,
        connected=True,
        account_id=account_id,
        data_quality=DataQuality.LIVE,
        last_successful_sync=NOW,
    )


class _ScriptedBroker:
    """Two identical snapshots by default: the accepted-evidence shape the seam persists from."""

    def __init__(
        self,
        executions: tuple[tuple[BrokerExecution, ...], tuple[BrokerExecution, ...]],
    ) -> None:
        account = AccountSummary(
            account_id=ACCOUNT_ID,
            net_liquidation=Decimal("250000"),
            total_cash=Decimal("125000"),
            buying_power=Decimal("240000"),
            as_of=NOW,
        )
        self._accounts = (account, account)
        self._statuses = (_status(ACCOUNT_ID), _status(ACCOUNT_ID))
        self._times = (NOW, NOW + timedelta(seconds=1))
        self._executions = executions
        self.calls = {"status": 0, "time": 0, "account": 0, "executions": 0}

    async def connection_status(self) -> ConnectionStatus:
        index = self.calls["status"]
        self.calls["status"] += 1
        return self._statuses[index]

    async def server_time(self) -> datetime:
        index = self.calls["time"]
        self.calls["time"] += 1
        return self._times[index]

    async def account_summary(self) -> AccountSummary:
        index = self.calls["account"]
        self.calls["account"] += 1
        return self._accounts[index]

    async def positions(self) -> tuple[()]:
        return ()

    async def open_orders(self) -> tuple[()]:
        return ()

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        assert since is None
        index = self.calls["executions"]
        self.calls["executions"] += 1
        return self._executions[index]


class _Runner:
    def __init__(self, broker: object) -> None:
        self.broker = cast(Broker, broker)

    def run(self, coroutine: Coroutine[Any, Any, _T], *, timeout: float | None = None) -> _T:
        return asyncio.run(coroutine)


class _Reader:
    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        return LocalReconciliationEvidence(complete=True)


class _Ticking:
    """An injected monotonic clock: identical across runs, so two reports can be compared."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


class _Spy:
    """Wraps the real repository; records what the seam asked and what came back."""

    def __init__(self, inner: ExecutionRepository) -> None:
        self.inner = inner
        self.recorded: list[tuple[str, str | None, bool]] = []

    def linked_correlation_id(self, order_ref: str | None) -> str | None:
        return self.inner.linked_correlation_id(order_ref)

    def record(self, execution: BrokerExecution, *, correlation_id: str | None) -> bool:
        result = self.inner.record(execution, correlation_id=correlation_id)
        self.recorded.append((execution.execution_id, correlation_id, result))
        return result


class _Raising:
    """A repository that refuses (or blows up) on the FIRST execution it sees, then behaves."""

    def __init__(self, inner: ExecutionRepository, error: BaseException) -> None:
        self.inner = inner
        self.error = error
        self.seen = 0

    def linked_correlation_id(self, order_ref: str | None) -> str | None:
        return None

    def record(self, execution: BrokerExecution, *, correlation_id: str | None) -> bool:
        self.seen += 1
        if self.seen == 1:
            raise self.error
        return self.inner.record(execution, correlation_id=correlation_id)


class _Records(logging.Handler):
    """Attached to the module logger directly: the chronos logger does not propagate to caplog."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Any:
    handler = _Records()
    logger = reconciliation_module._LOGGER
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _database(tmp_path: Path, account_id: str) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    db.bind_scope(broker_mode="demo", environment="paper", account_id=account_id)
    return db


def _rows(
    database: Database,
) -> tuple[list[tuple[str, str | None]], list[tuple[str, Decimal, str]]]:
    with database.sessions() as session:
        fills = [
            (r.execution_id, r.correlation_id)
            for r in session.scalars(sa.select(FillRow).order_by(FillRow.execution_id))
        ]
        commissions = [
            (r.execution_id, Decimal(r.amount), r.currency)
            for r in session.scalars(sa.select(CommissionRow).order_by(CommissionRow.execution_id))
        ]
    return fills, commissions


def _coordinator(
    broker: object,
    *,
    repository: object | None = None,
    monotonic: Callable[[], float] | None = None,
    symbols: tuple[str, ...] = ("AAPL",),
) -> ReconciliationCoordinator:
    extra: dict[str, Any] = {}
    if monotonic is not None:
        extra["monotonic"] = monotonic
    if repository is not None:
        extra["execution_repository"] = repository
    return ReconciliationCoordinator(_Runner(broker), _Reader(), symbols, **extra)


def _events(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    return [r for r in records if getattr(r, "event", None) == event]


# ------------------------------------------------------------------ 1. injection


def test_1a_a_coordinator_without_a_repository_writes_nothing_and_the_kwarg_is_optional(
    tmp_path: Path,
) -> None:
    parameter = inspect.signature(ReconciliationCoordinator.__init__).parameters[
        "execution_repository"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None
    database = _database(tmp_path, ACCOUNT_ID)
    try:
        execution = _execution()
        result = _coordinator(_ScriptedBroker(((execution,), (execution,)))).reconcile()
        assert isinstance(result, ReconciliationResult)
        assert _rows(database) == ([], [])
    finally:
        database.dispose()


def test_1b_the_runtime_wiring_supplies_the_repository_at_the_one_construction_site() -> None:
    source = (SRC / "runtime.py").read_text(encoding="utf-8")
    assert "from chronos.persistence.execution_repository import ExecutionRepository" in source
    call = source[source.index("reconciliation = ReconciliationCoordinator(") :]
    call = call[: call.index("\n        )")]
    assert "execution_repository=ExecutionRepository(database.sessions)" in call
    assert source.count("ReconciliationCoordinator(") == 1, "exactly one construction site"


# ------------------------------------------------------------------ 2. the seam, idempotent


def test_2a_a_demo_pass_persists_one_fill_and_one_commission_and_a_second_pass_writes_nothing(
    tmp_path: Path, records: list[logging.LogRecord]
) -> None:
    database = _database(tmp_path, DEMO_ACCOUNT_ID)
    try:
        broker = DemoBroker()
        asyncio.run(broker.connect())
        spy = _Spy(ExecutionRepository(database.sessions))
        symbols = ("MSFT", "AMD", "TSLA")
        first = _coordinator(broker, repository=spy, symbols=symbols).reconcile()
        assert first.snapshot is not None, first.reasons
        assert _rows(database) == (
            [("DEMO-EXEC-0001", None)],
            [("DEMO-EXEC-0001", Decimal("0.65"), "USD")],
        )
        assert spy.recorded == [("DEMO-EXEC-0001", None, True)]
        second = _coordinator(broker, repository=spy, symbols=symbols).reconcile()
        assert second.snapshot is not None, second.reasons
        assert _rows(database) == (
            [("DEMO-EXEC-0001", None)],
            [("DEMO-EXEC-0001", Decimal("0.65"), "USD")],
        )
        assert spy.recorded == [("DEMO-EXEC-0001", None, True), ("DEMO-EXEC-0001", None, False)]
        recorded = _events(records, "reconciliation_execution_recorded")
        assert [(r.execution_id, r.recorded) for r in recorded] == [  # type: ignore[attr-defined]
            ("DEMO-EXEC-0001", True),
            ("DEMO-EXEC-0001", False),
        ]
        assert all(DEMO_ACCOUNT_ID not in r.getMessage() for r in records)
        assert all(DEMO_ACCOUNT_ID not in str(getattr(r, "account", "")) for r in recorded)
    finally:
        database.dispose()


def test_2b_one_record_per_execution_id_and_rejected_evidence_is_never_persisted(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, ACCOUNT_ID)
    try:
        one, two = _execution("EX-1"), _execution("EX-2")
        spy = _Spy(ExecutionRepository(database.sessions))
        _coordinator(_ScriptedBroker(((one, two), (one, two))), repository=spy).reconcile()
        assert [r[0] for r in spy.recorded] == ["EX-1", "EX-2"], (
            "once per id, not once per snapshot"
        )
        assert [r[2] for r in spy.recorded] == [True, True]
        assert [f[0] for f in _rows(database)[0]] == ["EX-1", "EX-2"]
        # snapshots that disagree are rejected evidence (the pass locks) — nothing is persisted
        three = _execution("EX-3")
        spy2 = _Spy(ExecutionRepository(database.sessions))
        result = _coordinator(
            _ScriptedBroker(((three,), (three, one))), repository=spy2
        ).reconcile()
        assert result.snapshot is None and spy2.recorded == []
        assert [f[0] for f in _rows(database)[0]] == ["EX-1", "EX-2"]
    finally:
        database.dispose()


# ------------------------------------------------------------------ 3. correlation


def test_3a_a_submitted_order_links_the_drafts_correlation_id_and_a_foreign_ref_stores_none(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, ACCOUNT_ID)
    try:
        with database.sessions.begin() as session:
            session.add(
                OrderDraftRow(
                    correlation_id="draft-1",
                    account_id_masked="masked",
                    symbol="AAPL",
                    contract_id=2002,
                    intent="SELL_PUT",
                    quantity=1,
                    limit_price=Decimal("2.00"),
                    lifecycle="SUBMITTED",
                )
            )
            session.flush()
            session.add(
                SubmittedOrderRow(
                    correlation_id="draft-1",
                    broker_order_id=7001,
                    permanent_id=9001,
                    client_id=17,
                    order_ref="CHR-BP1B-1",
                    lifecycle="SUBMITTED",
                    submitted_at=NOW,
                )
            )
        repository = ExecutionRepository(database.sessions)
        assert repository.linked_correlation_id("CHR-BP1B-1") == "draft-1"
        assert repository.linked_correlation_id("CHR-NOBODY") is None
        assert repository.linked_correlation_id(None) is None
        matched, foreign = _execution("EX-M", order_ref="CHR-BP1B-1"), _execution("EX-F")
        spy = _Spy(repository)
        _coordinator(
            _ScriptedBroker(((matched, foreign), (matched, foreign))), repository=spy
        ).reconcile()
        assert _rows(database)[0] == [("EX-F", None), ("EX-M", "draft-1")]
        assert sorted(spy.recorded) == [("EX-F", None, True), ("EX-M", "draft-1", True)]
    finally:
        database.dispose()


# ------------------------------------------------- 4. the typed refusal, seen from the seam


def test_4a_an_unknown_correlation_at_the_seam_is_one_typed_line_and_zero_rows(
    tmp_path: Path, records: list[logging.LogRecord]
) -> None:
    database = _database(tmp_path, ACCOUNT_ID)
    try:

        class _Mislinking(_Spy):
            def linked_correlation_id(self, order_ref: str | None) -> str | None:
                return "not-a-draft"

        execution = _execution("EX-BAD")
        repo = _Mislinking(ExecutionRepository(database.sessions))
        result = _coordinator(
            _ScriptedBroker(((execution,), (execution,))), repository=repo
        ).reconcile()
        assert result.snapshot is not None
        assert _rows(database) == ([], [])
        refused = _events(records, "reconciliation_execution_persist_refused")
        assert [(r.execution_id, r.refusal) for r in refused] == [  # type: ignore[attr-defined]
            ("EX-BAD", "UnknownCorrelation")
        ]
    finally:
        database.dispose()


# ------------------------------------------------------------------ 5. never raise


@pytest.mark.parametrize(
    "error",
    [
        ExecutionConflict("execution EX-1 was already recorded with different facts"),
        UnknownCorrelation("EX-1", "ghost"),
        RuntimeError("Chronos database must contain exactly one bound broker scope"),
        ValueError("Application events must not contain raw broker account IDs"),
        KeyError("something nobody typed"),
    ],
    ids=["conflict", "unknown_correlation", "scope", "raw_id", "unexpected"],
)
def test_5a_a_raising_repository_never_raises_out_of_the_pass_and_the_report_is_unchanged(
    tmp_path: Path, records: list[logging.LogRecord], error: BaseException
) -> None:
    database = _database(tmp_path, ACCOUNT_ID)
    try:
        one, two = _execution("EX-1"), _execution("EX-2")
        baseline = _coordinator(
            _ScriptedBroker(((one, two), (one, two))), monotonic=_Ticking()
        ).reconcile()
        raising = _Raising(ExecutionRepository(database.sessions), error)
        result = _coordinator(
            _ScriptedBroker(((one, two), (one, two))), repository=raising, monotonic=_Ticking()
        ).reconcile()
        assert result == baseline, "the writer changed the report"
        assert raising.seen == 2, "the pass continued past the refusal to the second execution"
        assert [f[0] for f in _rows(database)[0]] == ["EX-2"]
        event = (
            "reconciliation_execution_persist_failed"
            if isinstance(error, KeyError)
            else "reconciliation_execution_persist_refused"
        )
        lines = _events(records, event)
        assert [(r.execution_id, r.refusal) for r in lines] == [  # type: ignore[attr-defined]
            ("EX-1", type(error).__name__)
        ]
        assert all(ACCOUNT_ID not in r.getMessage() for r in records)
        assert ACCOUNT_ID not in str(lines[0].account)  # type: ignore[attr-defined]
    finally:
        database.dispose()


# ------------------------------------------------------------------ 6. the authority line


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def test_6a_only_service_and_runtime_import_the_repository_and_no_authority_reads_rows() -> None:
    target = "chronos.persistence.execution_repository"
    importers = sorted(
        p.relative_to(ROOT).as_posix()
        for p in SRC.rglob("*.py")
        if any(n == target or n.startswith(target + ".") for n in _imports(p))
    )
    assert importers == ["src/chronos/runtime.py", "src/chronos/services/reconciliation.py"]
    readers: list[str] = []
    for prefix in ("orders", "autonomy", "supervisor", "control", "execution"):
        for path in (SRC / prefix).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "FillRow" in text or "CommissionRow" in text or "execution_repository" in text:
                readers.append(path.relative_to(ROOT).as_posix())
    assert readers == [], readers
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, chronos.services.reconciliation; "
            f"print(sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIXES!r})))",
        ],
        cwd=ROOT,
        env={**os.environ, **SAFE_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "[]", probe.stdout
    assert IntegrityError is not None  # the name is imported so the 4a pin below can name it


# ------------------------------------------------------------------ r2. Daybreak's HOLD at a0e83cc


class _ExplodingAtResolve:
    """Daybreak's probe clock: capture completes, then the first read inside ``_resolve`` raises.

    A pass reads the clock for ``started_at``, for ``broker_elapsed_seconds`` after the capture,
    and then for ``evidence_elapsed_seconds`` inside ``_resolve``: the third read.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("resolution clock failed")
        return 1_000.0 + self.calls / 1_000


def test_r2_1a_a_resolution_failure_escapes_and_persists_nothing(tmp_path: Path) -> None:
    """P1: the writer runs only after a result exists; a raise in ``_resolve`` writes no rows."""
    database = _database(tmp_path, ACCOUNT_ID)
    try:
        one = _execution("EX-1")
        spy = _Spy(ExecutionRepository(database.sessions))
        failing = _ScriptedBroker(((one,), (one,)))
        clock = _ExplodingAtResolve()
        with pytest.raises(RuntimeError) as raised:
            _coordinator(failing, repository=spy, monotonic=clock).reconcile()
        assert type(raised.value) is RuntimeError
        assert str(raised.value) == "resolution clock failed"
        assert failing.calls["executions"] == 2, "both snapshots were captured before the raise"
        assert clock.calls == 3, "the raise was the first clock read inside _resolve"
        assert spy.recorded == [], "the writer ran before any result was decided"
        assert _rows(database) == ([], [])
        # positive control: the same evidence with a healthy clock persists exactly once
        healthy = _ScriptedBroker(((one,), (one,)))
        _coordinator(healthy, repository=spy, monotonic=_Ticking()).reconcile()
        assert spy.recorded == [("EX-1", None, True)]
        assert [f[0] for f in _rows(database)[0]] == ["EX-1"]
    finally:
        database.dispose()


RECONCILE_CONTRACT = (
    "Read broker and local evidence without placing or changing any order.\n"
    "\n"
    "The broker is only ever read. Local state is written in exactly one case: when an\n"
    "``ExecutionRepository`` was injected, the executions the accepted observation carried\n"
    "are persisted as local evidence after a result is decided (never on a resolution\n"
    "failure), and that write can neither change the result nor raise out of the pass."
)


def test_r2_2a_the_reconcile_contract_says_what_the_pass_writes_and_when() -> None:
    """P2: the public method contract no longer claims a no-write pass."""
    flat = " ".join(RECONCILE_CONTRACT.split())  # the pinned text wraps; the meaning does not
    assert "without writing state" not in flat
    for phrase in (
        "without placing or changing any order",
        "``ExecutionRepository`` was injected",
        "after a result is decided",
        "never on a resolution failure",
    ):
        assert phrase in flat, phrase
    assert inspect.getdoc(ReconciliationCoordinator.reconcile) == RECONCILE_CONTRACT

"""DRAIN-2: durable claim state for the proposal drain (Kevin's K-a, option (c)).

The claim is a committed state (``PENDING -> CLAIMED``) written by ``claim_batch``
inside the tick's claim transaction, which commits before ``_drain`` evaluates
anything. A crash or raise after that commit leaves the row ``CLAIMED``, and nothing
ever moves it back to ``PENDING`` or re-presents it: interrupted claims are LISTED
(``list_interrupted_claims``) and ALERTED (one static alert), never auto-resolved.

Every test uses a real file-backed SQLite database through the real ``Database``;
every child process and join is bounded.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.domain.models import UnderlyingContract
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyOwnerAlertRow, AutonomyProposalQueueRow
from chronos.supervisor import alerts, durable, proposals, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import HandoffResult
from chronos.supervisor.loop import CycleFacts
from chronos.supervisor.runtime import AutonomyRuntime, RuntimeConfig
from chronos.supervisor.sizing import AccountEvidence

NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
SRC_ROOT = Path(proposals.__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CHILD_S = 20.0
INTERRUPTED_KIND = "proposals.interrupted_claims"
CLAIMED = "CLAIMED"  # the new status value, pinned against proposals below
INTERRUPTED_SUMMARY = (
    "one or more pre-existing proposal claims require inspection; "
    "no automatic resolution is permitted"
)


# ------------------------------------------------------------------ helpers (also used by children)


def database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def open_database(path: Path) -> Database:
    database = Database(database_url(path))
    database.initialize()
    return database


def identity() -> queue.HarnessIdentity:
    return queue.HarnessIdentity(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
    )


def mandate() -> AutonomyMandate:
    return AutonomyMandate(
        mandate_id="m-1",
        mandate_version=1,
        account_fingerprint=FINGERPRINT,
        mode=AutonomyMode.PAPER_AUTONOMOUS,
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        effective_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        versions=VersionPins(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        capital=CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        owner_authorization_ref="owner-1",
        authored_at=NOW,
    )


def facts(now: datetime) -> CycleFacts:
    return CycleFacts(
        account_fingerprint=FINGERPRINT,
        account_id="DU1234567",
        now=now,
        process_generation=7,
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
        market_data=MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        account=AccountEvidence(
            net_liquidation_usd=Decimal(100_000),
            total_cash_usd=Decimal(60_000),
            buying_power_usd=Decimal(60_000),
            symbol_exposure_usd=Decimal(0),
            gross_exposure_usd=Decimal(0),
            net_exposure_usd=Decimal(0),
            position_notional_usd=Decimal(0),
            maintenance_margin_usd=Decimal(0),
            deployed_capital_usd=Decimal(0),
        ),
        quote=QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02")),
        contract=UnderlyingContract(con_id=111, symbol="SPY"),
        reference_price=Decimal(400),
    )


def submittable_payload() -> str:
    return json.dumps(
        {
            "kind": "OPEN",
            "asset_class": "EQUITY",
            "symbol": "SPY",
            "requested_strategy": "LONG_EQUITY",
            "requested_quantity": "10",
            "evidence": [
                {
                    "evidence_id": "ev-1",
                    "kind": "quote",
                    "as_of": NOW.isoformat(),
                    "digest": "c" * 64,
                }
            ],
            "invalidation_conditions": ["closes below 400"],
        }
    )


REFUSED_PAYLOAD = "{ not json"  # refused at INGRESS: a handled refusal, no handoff


class NullSink:
    name = "null"

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        return True


def activate(sessions: sessionmaker[Session]) -> AutonomyMandate:
    active = mandate()
    with sessions.begin() as session:
        durable.activate(
            session,
            account_fingerprint=FINGERPRINT,
            mandate=active,
            owner_event_id="owner-event-1",
            now=NOW,
            process_generation=7,
        )
    return active


def runtime(
    sessions: sessionmaker[Session],
    *,
    active: AutonomyMandate | None = None,
    submit: Any = None,
    config: RuntimeConfig | None = None,
    gather: Any = facts,
) -> AutonomyRuntime:
    return AutonomyRuntime(
        sessions=sessions,
        config=config or RuntimeConfig(account_fingerprint=FINGERPRINT),
        identity=identity(),
        mandate_source=lambda: active,
        gather_facts=gather,
        sinks=(NullSink(),),
        submit=submit,
    )


def enqueue(sessions: sessionmaker[Session], payload: str = REFUSED_PAYLOAD) -> int:
    with sessions.begin() as session:
        outcome = proposals.enqueue(
            session, account_fingerprint=FINGERPRINT, payload=payload, now=NOW
        )
        assert outcome.queued
        return outcome.queue_id


def statuses(sessions: sessionmaker[Session]) -> dict[int, str]:
    with sessions.begin() as session:
        rows = session.scalars(
            select(AutonomyProposalQueueRow).order_by(AutonomyProposalQueueRow.id)
        )
        return {row.id: row.status for row in rows}


def interrupted_alerts(sessions: sessionmaker[Session]) -> list[AutonomyOwnerAlertRow]:
    with sessions.begin() as session:
        rows = session.scalars(
            select(AutonomyOwnerAlertRow).where(AutonomyOwnerAlertRow.kind == INTERRUPTED_KIND)
        ).all()
        session.expunge_all()
        return list(rows)


def claim(sessions: sessionmaker[Session], limit: int = 10) -> list[int]:
    with sessions.begin() as session:
        return [
            item.id
            for item in proposals.claim_batch(session, account_fingerprint=FINGERPRINT, limit=limit)
        ]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "chronos.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    database = open_database(db_path)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def sessions(db: Database) -> sessionmaker[Session]:
    return db.sessions


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([str(SRC_ROOT), str(HERE)]),
        PYTHONDONTWRITEBYTECODE="1",
        BROKER_MODE="demo",
        ALLOW_ORDER_TRANSMIT="false",
        ALLOW_LIVE_TRADING="false",
    )
    return env


# TEARDOWN-1: every child this module starts is reaped on EVERY path. It runs in its own
# process group (start_new_session) and the _children() context kills that group and waits
# in a finally (success, assertion failure, timeout, KeyboardInterrupt). Each child also
# carries a watchdog prelude, so a SIGKILLed pytest (no finally runs) cannot strand it: it
# exits when its parent changes or a bounded total deadline passes (the race workers used to
# keep looping on a deleted temp database). The marker comment lets a leftover census find
# this module's children by command line.
_CHILD_MARK = "# chronos-test-child:test_proposal_claim_state"
_CHILD_DEADLINE_S = 300.0


def _child_code(code: str) -> str:
    return (
        f"{_CHILD_MARK}\n"
        "import os as _os, threading as _threading, time as _time\n"
        # The spawner's pid, not getppid(): a parent that died before this line runs
        # would make getppid() already the reaper's pid and blind the watchdog.
        "_PARENT = int(_os.environ.get('CHRONOS_TEST_PARENT_PID', _os.getppid()))\n"
        f"_DEADLINE = _time.monotonic() + {_CHILD_DEADLINE_S}\n"
        "def _orphan_watchdog():\n"
        "    while _os.getppid() == _PARENT and _time.monotonic() < _DEADLINE:\n"
        "        _time.sleep(0.1)\n"
        "    _os._exit(97)\n"
        # TEARDOWN-1r1 (the DBLOCK-2r3 mechanism): a sentinel that keeps this process group
        # non-empty (so its id is never recycled) until the spawner closes the pipe's write
        # end, or dies. It is the pipe's ONLY reader (the leader closes its copy), forked
        # before any thread or user code.
        "_GROUP_FD = _os.environ.pop('CHRONOS_TEST_GROUP_FD', None)\n"
        "if _GROUP_FD is not None:\n"
        "    if _os.fork() == 0:\n"
        "        try:\n"
        "            _null = _os.open(_os.devnull, _os.O_RDWR)\n"
        "            for _std in (0, 1, 2):\n"
        "                _os.dup2(_null, _std)\n"
        "            while _os.read(int(_GROUP_FD), 4096):\n"
        "                pass\n"
        "        finally:\n"
        "            _os._exit(0)\n"
        "    _os.close(int(_GROUP_FD))\n"
        "_threading.Thread(target=_orphan_watchdog, daemon=True).start()\n" + textwrap.dedent(code)
    )


class _Children:
    """Children started in their own process groups; reap() kills and waits for them all.

    Each group holds a sentinel (see _child_code) that is the only reader of a pipe whose write
    end only this process holds. While a 1-byte write to that pipe succeeds, the sentinel is a
    live member of the group, so the group exists and Linux cannot hand its id to anyone else:
    killpg on the recorded id reaches only our group, even after its leader has been reaped.
    """

    def __init__(self) -> None:
        self._procs: list[tuple[subprocess.Popen[str], int]] = []

    def popen(self, code: str, *args: str) -> subprocess.Popen[str]:
        env = _child_env()
        env["CHRONOS_TEST_PARENT_PID"] = str(os.getpid())
        read_fd, write_fd = os.pipe()
        env["CHRONOS_TEST_GROUP_FD"] = str(read_fd)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", _child_code(code), *args],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                pass_fds=(read_fd,),
            )
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        os.set_blocking(write_fd, False)
        self._procs.append((proc, write_fd))
        return proc

    def run(self, code: str, *args: str) -> subprocess.CompletedProcess[str]:
        proc = self.popen(code, *args)
        out, err = proc.communicate(timeout=CHILD_S)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def reap(self) -> None:
        for proc, write_fd in self._procs:
            try:
                os.write(write_fd, b"\0")
                pinned = True
            except BrokenPipeError:  # no reader: the sentinel is gone, the id may be recycled
                pinned = False
            except BlockingIOError:  # a full pipe still has its reader
                pinned = True
            # An unreaped leader also pins its pid (and so the group id) until it is waited for.
            if pinned or proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=CHILD_S)
            os.close(write_fd)
            if pinned:  # the killed members are reaped by init; signal 0 only probes
                deadline = time.monotonic() + CHILD_S
                while _group_alive(proc.pid) and time.monotonic() < deadline:
                    time.sleep(0.01)


@contextlib.contextmanager
def _children() -> Iterator[_Children]:
    children = _Children()
    try:
        yield children
    finally:
        children.reap()


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    with _children() as children:
        return children.run(code, *args)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_alive(pid: int) -> bool:
    """Alive and not a zombie (an orphan's zombie is reaped by init, not by us)."""

    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


# ------------------------------------------- the gap, the order, the crash matrix


def test_daybreak_gap_a_refused_claim_is_not_reclaimed_after_a_crash_before_its_marker(
    sessions: sessionmaker[Session],
) -> None:
    """P1-NEW-22: claim, refuse, crash before the marker commits -> never re-claimed."""

    queued = enqueue(sessions)
    first_claim_ids = claim(sessions)
    assert first_claim_ids == [queued]
    # The refusal was decided, but the process died before mark_processed committed.
    reclaimed_after_refusal_marker_gap = claim(sessions)
    assert reclaimed_after_refusal_marker_gap == [], reclaimed_after_refusal_marker_gap
    assert statuses(sessions) == {queued: CLAIMED}


def test_a_crash_before_the_claim_commit_leaves_the_row_pending(
    sessions: sessionmaker[Session],
) -> None:
    queued = enqueue(sessions)
    with pytest.raises(RuntimeError, match="crash before commit"), sessions.begin() as session:
        assert [
            item.id
            for item in proposals.claim_batch(session, account_fingerprint=FINGERPRINT, limit=10)
        ] == [queued]
        raise RuntimeError("crash before commit")
    assert statuses(sessions) == {queued: proposals.STATUS_PENDING}
    assert claim(sessions) == [queued]
    assert claim(sessions) == []


def test_the_claim_is_committed_before_the_drain_evaluates(
    sessions: sessionmaker[Session], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing order: conditional claim -> durable commit -> evaluation."""

    queued = enqueue(sessions)
    observed: list[str] = []
    observer = open_database(db_path)

    def recording_cycle(*_args: Any, **_kwargs: Any) -> Any:
        with observer.sessions.begin() as other:
            observed.append(
                other.scalar(
                    select(AutonomyProposalQueueRow.status).where(
                        AutonomyProposalQueueRow.id == queued
                    )
                )
            )
        raise RuntimeError("stop after observing")

    monkeypatch.setattr("chronos.supervisor.runtime.run_cycle", recording_cycle)
    try:
        report = runtime(sessions).run_tick(NOW)
    finally:
        observer.dispose()
    assert observed == [CLAIMED], "evaluation began before the claim committed"
    assert report.failure == "tick raised RuntimeError"
    assert statuses(sessions) == {queued: CLAIMED}


CHILD_CRASH = """
import os, signal, sys
from pathlib import Path
import test_proposal_claim_state as h
from chronos.supervisor import proposals
from chronos.supervisor.handoff import HandoffResult

point, path = sys.argv[1], Path(sys.argv[2])
database = h.open_database(path)
sessions = database.sessions

def die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)

if point == "before_handoff":
    active = h.activate(sessions)
    h.enqueue(sessions, h.submittable_payload())
    runtime = h.runtime(sessions, active=active, submit=die)
elif point == "refusal_before_marker":
    h.enqueue(sessions, h.REFUSED_PAYLOAD)
    real = proposals.mark_processed
    def marker(session, **kwargs):
        assert kwargs["refusal"], "the refusal must be handled before the marker"
        die()
    proposals.mark_processed = marker
    runtime = h.runtime(sessions)
else:  # after_terminal_commit
    h.enqueue(sessions, h.REFUSED_PAYLOAD)
    runtime = h.runtime(sessions)
report = runtime.run_tick(h.NOW)
if point == "after_terminal_commit":
    assert report.proposals_judged == 1, report.failure
    die()
print("UNREACHABLE", report, flush=True)
"""


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ("before_handoff", CLAIMED),
        ("refusal_before_marker", CLAIMED),
        ("after_terminal_commit", proposals.STATUS_PROCESSED),
    ],
)
def test_a_sigkill_at_each_crash_boundary_never_re_presents_the_row(
    db_path: Path, point: str, expected: str
) -> None:
    """A real child SIGKILL, then a fresh process reopens: no row is selected or run again."""

    result = _run_child(CHILD_CRASH, point, str(db_path))
    assert result.returncode == -signal.SIGKILL, (result.returncode, result.stdout, result.stderr)
    reopened = open_database(db_path)
    try:
        assert list(statuses(reopened.sessions).values()) == [expected]
        handed_off: list[Any] = []
        restarted = runtime(
            reopened.sessions,
            active=mandate(),
            submit=lambda intent: handed_off.append(intent) or HandoffResult.submitted(),
        )
        for index in range(3):
            report = restarted.run_tick(NOW + timedelta(minutes=index + 1))
            assert report.proposals_judged == 0, report
        assert handed_off == [], "a claimed or terminal row was executed again"
        assert claim(reopened.sessions) == []
        assert list(statuses(reopened.sessions).values()) == [expected]
    finally:
        reopened.dispose()


def test_a_power_loss_after_the_handoff_leaves_the_row_claimed_and_never_handed_off_again(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_autonomy_runtime's ADR-0052 power-loss shape, with the queue row asserted."""

    active = activate(sessions)
    queued = enqueue(sessions, submittable_payload())
    handed_off: list[Any] = []

    def die(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("power lost before the terminal marker")

    with monkeypatch.context() as patch:
        patch.setattr(proposals, "mark_processed", die)
        report = runtime(
            sessions,
            active=active,
            submit=lambda intent: handed_off.append(intent) or HandoffResult.submitted(),
        ).run_tick(NOW)
    assert len(handed_off) == 1 and report.failure == "tick raised RuntimeError"
    assert statuses(sessions) == {queued: CLAIMED}
    again = runtime(
        sessions,
        active=active,
        submit=lambda intent: handed_off.append(intent) or HandoffResult.submitted(),
    ).run_tick(NOW + timedelta(minutes=1))
    assert again.proposals_judged == 0
    assert len(handed_off) == 1, "the interrupted proposal was handed off a second time"


def test_a_raise_on_one_item_strands_the_unevaluated_rest_of_the_batch(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disclosed availability cost: the whole batch is claimed before the drain."""

    for _ in range(4):
        enqueue(sessions)

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the first item raises")

    monkeypatch.setattr("chronos.supervisor.runtime.run_cycle", explode)
    runtime(sessions).run_tick(NOW)
    assert set(statuses(sessions).values()) == {CLAIMED}


# ------------------------------------------------------------------ competing workers (a real race)


CHILD_RACE = """
import os, sys, time
from pathlib import Path
import test_proposal_claim_state as h

path, go, journal, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
database = h.open_database(path)
deadline = time.monotonic() + 15
while not go.exists():
    if time.monotonic() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.001)
started = time.monotonic()
fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
claimed = 0
empty = 0
while empty < 3:
    ids = h.claim(database.sessions, limit=10)
    if not ids:
        empty += 1
        continue
    empty = 0
    for row_id in ids:
        os.write(fd, f"handoff {row_id} {name}\\n".encode())
    claimed += len(ids)
finished = time.monotonic()
os.close(fd)
print(f"{name} {claimed} {started} {finished}", flush=True)
"""


def test_competing_workers_claim_every_row_exactly_once(db_path: Path, tmp_path: Path) -> None:
    """Three processes, separate connections, one barrier, looping until the queue is empty."""

    seed = open_database(db_path)
    try:
        for _ in range(500):
            enqueue(seed.sessions)
    finally:
        seed.dispose()
    go = tmp_path / "go"
    journal = tmp_path / "handoffs.log"
    summaries = []
    with _children() as children:
        workers = [
            children.popen(CHILD_RACE, str(db_path), str(go), str(journal), f"w{index}")
            for index in range(3)
        ]
        time.sleep(1.0)  # let every worker reach the barrier
        go.touch()
        for worker in workers:
            out, err = worker.communicate(timeout=CHILD_S * 3)
            assert worker.returncode == 0, err
            name, claimed, started, finished = out.split()
            summaries.append((name, int(claimed), float(started), float(finished)))
    handoffs = [int(line.split()[1]) for line in journal.read_text().splitlines()]
    assert len(handoffs) == len(set(handoffs)) == 500, "a row was claimed (handed off) twice"
    # Real overlap: at least two workers' claim loops ran concurrently.
    windows = sorted((started, finished) for _, _, started, finished in summaries)
    assert any(windows[i + 1][0] < windows[i][1] for i in range(len(windows) - 1)), windows
    assert sum(claimed for _, claimed, _, _ in summaries) == 500


# ------------------------------------------- no reset, no reclaim, no other writer


def _queue_status_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """Every write of the queue row's status in a module: (line, value expression)."""

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id == "AutonomyProposalQueueRow":
                for keyword in node.keywords:
                    if keyword.arg == "status":
                        found.append((node.lineno, ast.unparse(keyword.value)))
            if isinstance(target, ast.Attribute) and target.attr == "values":
                for keyword in node.keywords:
                    if keyword.arg == "status":
                        found.append((node.lineno, ast.unparse(keyword.value)))
        if isinstance(node, ast.Assign):
            for assigned in node.targets:
                if isinstance(assigned, ast.Attribute) and assigned.attr == "status":
                    found.append((node.lineno, ast.unparse(node.value)))
    return found


def test_no_status_update_on_the_queue_outside_proposals() -> None:
    package = SRC_ROOT / "chronos"
    offenders = []
    for path in package.rglob("*.py"):
        source = path.read_text()
        if "AutonomyProposalQueueRow" not in source or path.name == "proposals.py":
            continue
        if path.parent.name in {"persistence", "migrations", "versions"}:
            continue  # schema and migration definitions, not runtime writers
        if _queue_status_writes(ast.parse(source)):
            offenders.append(str(path.relative_to(package)))
    assert offenders == []


def test_no_startup_timer_or_lease_path_moves_claimed_to_pending(
    sessions: sessionmaker[Session], db_path: Path
) -> None:
    """Static: the only write of PENDING is enqueue's insert. Dynamic: restart + ticks."""

    tree = ast.parse(Path(proposals.__file__).read_text())
    writes = _queue_status_writes(tree)
    pending_writes = [value for _, value in writes if value == "STATUS_PENDING"]
    assert pending_writes == ["STATUS_PENDING"], writes
    enqueue_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "enqueue"
    )
    assert [value for _, value in _queue_status_writes(enqueue_node)] == ["STATUS_PENDING"]

    queued = enqueue(sessions)
    assert claim(sessions) == [queued]
    restarted = open_database(db_path)  # a restart: a fresh engine and pool
    try:
        fresh = runtime(restarted.sessions)
        for index in range(3):
            fresh.run_tick(NOW + timedelta(minutes=index + 1))
        assert statuses(restarted.sessions) == {queued: CLAIMED}
    finally:
        restarted.dispose()


def test_mark_processed_refuses_a_row_that_is_not_claimed(sessions: sessionmaker[Session]) -> None:
    queued = enqueue(sessions)
    with pytest.raises(proposals.ClaimStateError, match="CLAIMED"), sessions.begin() as session:
        proposals.mark_processed(session, queue_id=queued, stage="INGRESS", refusal="X", now=NOW)
    assert statuses(sessions) == {queued: proposals.STATUS_PENDING}
    assert claim(sessions) == [queued]
    with sessions.begin() as session:
        proposals.mark_processed(session, queue_id=queued, stage="INGRESS", refusal="X", now=NOW)
    with pytest.raises(proposals.ClaimStateError, match="CLAIMED"), sessions.begin() as session:
        proposals.mark_processed(session, queue_id=queued, stage="INGRESS", refusal="Y", now=NOW)
    with sessions.begin() as session:
        row = session.get(AutonomyProposalQueueRow, queued)
        assert row is not None
        assert (row.status, row.refusal) == (proposals.STATUS_PROCESSED, "X")


# ------------------------------------------------------------------ the static alert and the census


def test_a_clean_tick_raises_no_interrupted_claim_alert(sessions: sessionmaker[Session]) -> None:
    """Rows claimed by this invocation never raise it (the clean control)."""

    enqueue(sessions)
    report = runtime(sessions).run_tick(NOW)
    assert report.proposals_judged == 1
    runtime(sessions).run_tick(NOW + timedelta(minutes=1))
    # DRAIN-2r1: the no-facts arm runs the same helper; a terminal row never alerts.
    no_facts = runtime(sessions, gather=lambda _now: None)
    assert no_facts.run_tick(NOW + timedelta(minutes=2)).proposals_judged == 0
    assert interrupted_alerts(sessions) == []


def test_the_interrupted_claim_alert_is_static_and_never_a_census(
    sessions: sessionmaker[Session],
) -> None:
    first = enqueue(sessions)
    assert claim(sessions) == [first]  # crash after the durable claim
    assert interrupted_alerts(sessions) == [], "the claiming invocation raised the alert"
    claim(sessions)  # the next invocation finds a PRE-EXISTING claim
    raised = interrupted_alerts(sessions)
    assert len(raised) == 1
    assert raised[0].summary == INTERRUPTED_SUMMARY
    assert raised[0].detail == {}
    second = enqueue(sessions)
    assert claim(sessions) == [second]  # strand a second row
    claim(sessions)
    folded = interrupted_alerts(sessions)
    assert len(folded) == 1 and folded[0].occurrences == 3
    assert folded[0].detail == {} and folded[0].summary == INTERRUPTED_SUMMARY
    # DRAIN-2r1: the no-facts arm folds into the same static alert, still no census.
    no_facts = runtime(sessions, gather=lambda _now: None)
    no_facts.run_tick(NOW + timedelta(minutes=1))
    folded = interrupted_alerts(sessions)
    assert len(folded) == 1 and folded[0].occurrences == 4
    assert folded[0].detail == {} and folded[0].summary == INTERRUPTED_SUMMARY
    for alert in (raised[0], folded[0]):
        rendered = json.dumps([alert.summary, alert.detail])
        assert str(first) not in rendered and str(second) not in rendered
        assert "claim:" not in rendered
    with sessions.begin() as session:
        assert alerts.acknowledge(
            session, account_fingerprint=FINGERPRINT, alert_id=folded[0].id, note="seen", now=NOW
        )
    claim(sessions)
    assert len(interrupted_alerts(sessions)) == 2, "an acknowledged alert must re-raise"


def test_list_interrupted_claims_is_read_only_and_the_sole_census(
    sessions: sessionmaker[Session], db_path: Path
) -> None:
    first = enqueue(sessions)
    second = enqueue(sessions)
    processed = enqueue(sessions)
    assert claim(sessions, limit=1) == [first]
    assert claim(sessions, limit=1) == [second]
    assert claim(sessions, limit=1) == [processed]
    with sessions.begin() as session:
        proposals.mark_processed(session, queue_id=processed, stage="INGRESS", refusal="X", now=NOW)
    pending = enqueue(sessions)
    # Reader P2-1: a PENDING and a PROCESSED row sit beside the two CLAIMED rows,
    # so a census that dropped its status filter would list them.
    assert statuses(sessions) == {
        first: CLAIMED,
        second: CLAIMED,
        processed: proposals.STATUS_PROCESSED,
        pending: proposals.STATUS_PENDING,
    }
    with sessions.begin() as session:
        raw = session.connection().connection.dbapi_connection
        assert raw is not None
        changes_before = raw.total_changes
        listed = proposals.list_interrupted_claims(session, account_fingerprint=FINGERPRINT)
        assert raw.total_changes == changes_before, "the read-only query wrote to the database"
        assert not session.new and not session.dirty and not session.deleted
    assert [item.id for item in listed] == [first, second]
    assert pending not in {item.id for item in listed}
    assert processed not in {item.id for item in listed}
    assert all(item.claim_token.startswith("claim:") for item in listed)
    assert all(item.received_at == NOW for item in listed)
    fields = set(proposals.InterruptedClaim.__dataclass_fields__)
    assert fields == {"id", "claim_token", "received_at"}
    assert not fields & {"claimed_at", "claim_age", "age", "stale", "alive"}
    doc = proposals.InterruptedClaim.__doc__ or ""
    assert "queue receipt time" in doc and "not the claim time" in doc
    source = ast.parse(Path(proposals.__file__).read_text())
    function = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "list_interrupted_claims"
    )
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }
    assert not calls & {"update", "insert", "delete", "add", "execute", "values", "flush"}


def test_an_interrupted_claim_alerts_even_when_facts_are_unavailable(
    sessions: sessionmaker[Session],
) -> None:
    """Daybreak P2-DRAIN-2-1: a no-facts tick still alerts on an interrupted claim.

    It alerts only: the row stays CLAIMED (no claim, reset, resolve or requeue).
    """

    queued = enqueue(sessions)
    assert claim(sessions) == [queued]  # durable claim, then the process dies
    assert interrupted_alerts(sessions) == [], "the claiming invocation raised the alert"
    restarted = runtime(sessions, gather=lambda _now: None)
    report = restarted.run_tick(NOW + timedelta(minutes=1))
    assert report.proposals_judged == 0
    assert statuses(sessions) == {queued: CLAIMED}
    raised = interrupted_alerts(sessions)
    assert len(raised) == 1
    assert raised[0].kind == INTERRUPTED_KIND
    assert raised[0].summary == INTERRUPTED_SUMMARY
    assert raised[0].detail == {}


# ------------------------------------------------------------------ capacity


def test_claimed_rows_count_toward_the_queue_capacity(sessions: sessionmaker[Session]) -> None:
    for _ in range(proposals.MAX_PENDING):
        enqueue(sessions)
    assert len(claim(sessions, limit=proposals.MAX_PENDING)) == proposals.MAX_PENDING
    with sessions.begin() as session:
        assert proposals.pending_depth(session, account_fingerprint=FINGERPRINT) == 0
        overflow = proposals.enqueue(
            session, account_fingerprint=FINGERPRINT, payload="{}", now=NOW
        )
    assert overflow.queued is False
    assert str(proposals.MAX_PENDING) in overflow.refusal


def test_mixed_claimed_and_pending_rows_share_the_cap_and_the_display_stays_pending_only(
    sessions: sessionmaker[Session],
) -> None:
    enqueue(sessions)
    claim(sessions, limit=1)
    enqueue(sessions)
    with sessions.begin() as session:
        assert proposals.outstanding_depth(session, account_fingerprint=FINGERPRINT) == 2
        outcome = proposals.enqueue(session, account_fingerprint=FINGERPRINT, payload="{}", now=NOW)
        assert outcome.queued and outcome.pending_depth == 2
        assert proposals.pending_depth(session, account_fingerprint=FINGERPRINT) == 2
        assert proposals.outstanding_depth(session, account_fingerprint=FINGERPRINT) == 3
    with sessions.begin() as session:
        for _ in range(proposals.MAX_PENDING - 3):
            assert proposals.enqueue(
                session, account_fingerprint=FINGERPRINT, payload="{}", now=NOW
            ).queued
        at_cap = proposals.enqueue(session, account_fingerprint=FINGERPRINT, payload="{}", now=NOW)
        assert at_cap.queued is False
        assert at_cap.pending_depth == proposals.MAX_PENDING - 1  # PENDING only, truthful


# ------------------------------------------------------------------ terminal, resubmission, brake


def test_a_refused_row_is_terminal_and_a_resubmission_is_a_new_row(
    sessions: sessionmaker[Session],
) -> None:
    refused = enqueue(sessions)
    assert runtime(sessions).run_tick(NOW).proposals_judged == 1
    assert runtime(sessions).run_tick(NOW + timedelta(minutes=1)).proposals_judged == 0
    resubmitted = enqueue(sessions)
    assert resubmitted != refused
    assert runtime(sessions).run_tick(NOW + timedelta(minutes=2)).proposals_judged == 1
    assert statuses(sessions) == {
        refused: proposals.STATUS_PROCESSED,
        resubmitted: proposals.STATUS_PROCESSED,
    }


def _always_raise(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("every proposal raises")


def test_the_failure_brake_still_trips_on_a_continuous_backlog(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader P3-4: 5 failed ticks of 10 claims each strand 50 rows, then the brake stops it."""

    monkeypatch.setattr("chronos.supervisor.runtime.run_cycle", _always_raise)
    for _ in range(60):
        enqueue(sessions)
    backlog = runtime(sessions)
    for index in range(6):
        backlog.run_tick(NOW + timedelta(minutes=index))
    assert backlog.stopped is True
    counted = list(statuses(sessions).values())
    assert counted.count(CLAIMED) == 50
    assert counted.count(proposals.STATUS_PENDING) == 10


def test_a_trickle_of_raising_proposals_does_not_trip_the_brake(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader P3-4: the deliberate weakening, visible: each raise strands once, the next
    (empty) tick succeeds and resets the count, so the brake never trips."""

    monkeypatch.setattr("chronos.supervisor.runtime.run_cycle", _always_raise)
    trickle = runtime(sessions)
    for index in range(6):
        enqueue(sessions)
        assert trickle.run_tick(NOW + timedelta(minutes=2 * index)).failure
        assert trickle.run_tick(NOW + timedelta(minutes=2 * index + 1)).ok
    assert trickle.stopped is False
    assert list(statuses(sessions).values()) == [CLAIMED] * 6


def test_the_claimed_status_value_is_pinned() -> None:
    assert proposals.STATUS_CLAIMED == CLAIMED
    assert len(CLAIMED) <= 16  # schema.py: status String(16), no CHECK, no migration


def test_claim_tokens_fit_the_stage_column_and_identify_the_process() -> None:
    token = proposals.CLAIM_TOKEN
    assert token.startswith("claim:") and len(token) <= 32
    threading.Thread(target=lambda: None).start()  # tokens are per process, not per thread
    assert token == proposals.CLAIM_TOKEN


# ------------------------------------------------------------------ TEARDOWN-1: positive controls

_HANG = "import time\ntime.sleep(600)\n"
_INTERMEDIATE = """
import os, subprocess, sys, time
env = dict(os.environ, CHRONOS_TEST_PARENT_PID=str(os.getpid()))  # as any spawner does
child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], start_new_session=True, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
time.sleep(600)
"""


def test_teardown_control_a_failing_body_still_reaps_its_children() -> None:
    """A failure inside the context (here a forced assertion) leaves no child alive."""

    pgids: list[int] = []
    try:
        with pytest.raises(AssertionError, match="forced"), _children() as children:
            for _ in range(3):
                pgids.append(children.popen(_HANG).pid)
            raise AssertionError("forced failure inside the reaping context")
        assert [pgid for pgid in pgids if _group_alive(pgid)] == []
    finally:
        for pgid in pgids:  # never let a broken reaper leak the hung children
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)


def test_teardown_control_an_orphaned_child_exits_on_its_own() -> None:
    """A child whose parent is SIGKILLed (no finally runs there) exits via its watchdog."""

    grandchild = 0
    try:
        with _children() as children:
            parent = children.popen(_INTERMEDIATE, _child_code(_HANG))
            assert parent.stdout is not None
            grandchild = int(parent.stdout.readline())
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(CHILD_S)
        deadline = time.monotonic() + 10
        while _pid_alive(grandchild):
            assert time.monotonic() < deadline, "the orphaned child outlived its parent"
            time.sleep(0.05)
    finally:
        if grandchild:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(grandchild, signal.SIGKILL)


_LEADER_WITH_DESCENDANT = """
import subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
"""


def test_c1_a_reaped_leader_does_not_exempt_its_same_group_descendant() -> None:
    """TEARDOWN-1r1 C1 (Daybreak TEARDOWN-1 P1): the leader exits 0 after starting a same-group
    hung descendant and the caller reaps the leader; leaving the context leaves no survivor."""

    descendant = 0
    group = 0
    try:
        with _children() as children:
            leader = children.popen(_LEADER_WITH_DESCENDANT)
            group = leader.pid
            out, err = leader.communicate(timeout=CHILD_S)
            assert leader.returncode == 0, err
            descendant = int(out.strip())
            assert _pid_alive(descendant), "the descendant must reach the cleanup boundary"
            assert os.getpgid(descendant) == group
        assert not _pid_alive(descendant), "a reaped leader exempted its descendant"
        assert not _group_alive(group)
    finally:
        if descendant and _pid_alive(descendant):  # never leak it from a broken reaper
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant, signal.SIGKILL)


def test_c1_a_group_outlives_its_reaped_leader_inside_the_context() -> None:
    """The recorded pgid stays pinned to OUR group after its leader is reaped (the sentinel), so
    the closing killpg cannot reach a recycled id."""

    with _children() as children:
        leader = children.popen("pass")
        leader.communicate(timeout=CHILD_S)
        assert leader.returncode == 0
        assert _group_alive(leader.pid), "nothing pins the group once its leader is reaped"
    assert not _group_alive(leader.pid)


def test_c1_reap_never_signals_a_group_the_body_already_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-unrelated-process invariant: once the body has killed and reaped a group (its sentinel
    gone, so its id may be recycled), reap() sends that id no SIGKILL."""

    with _children() as children:
        leader = children.popen(_HANG)
        os.killpg(leader.pid, signal.SIGKILL)
        leader.communicate(timeout=CHILD_S)
        deadline = time.monotonic() + CHILD_S
        while _group_alive(leader.pid):
            assert time.monotonic() < deadline, "the killed group never went away"
            time.sleep(0.01)
        sent: list[tuple[int, int]] = []
        real_killpg = os.killpg

        def recording_killpg(pgid: int, sig: int) -> None:
            sent.append((pgid, sig))
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", recording_killpg)
    assert (leader.pid, signal.SIGKILL) not in sent, sent

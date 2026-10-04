"""M2 — a committed proposal claim remains durably stranded and is not replayed after a crash.

Milestone 2 of the Chronos hardening milestone (Kevin, 2026-10-04): one end-to-end
workflow on the integrated tree, inside existing authorization. Design: M2-PROPOSE-r1
(reviewed PASS by Daybreak and kimi). Everything here is synthetic and demo-only:
``BROKER_MODE=demo``, ``ALLOW_ORDER_TRANSMIT=false``, ``ALLOW_LIVE_TRADING=false``, a SHADOW
mandate, one synthetic proposer credential, HOLD proposals, the demo broker's canned facts.

The real backend (``create_app`` under uvicorn) runs as a separate child process configured
ONLY through environment variables and files, and receives proposals over loopback TCP.
The one test-only seam replaces ``chronos.supervisor.proposals.mark_processed`` INSIDE the
child, before the app is built, and SIGKILLs the child when the drain is about to mark the
targeted queue row: the claim has committed and the terminal marker has not. The seam is
written to a temp file at runtime; no production module carries a kill switch.

What this proves:
- a synthetic credential and proposals traverse real TCP and env/file settings sources into
  the real backend, and one SHADOW refusal commits;
- a deterministic SIGKILL between the claim commit and the terminal marker leaves a
  committed, durably stranded claim;
- a backend restarted after that crash runs READ-ONLY while the dead writer's lease lives,
  then (lease expired) as the writer drains new work, raises the interrupted-claims alert
  once (one row) and delivers it once (one alert-file line), and never re-presents the
  stranded row.

What this does NOT prove: duplicate or replay rejection at HTTP ingress; DBLOCK (P1-NEW-23)
or LEDGER-2 (P1-NEW-24), which keep their own barrier tests; the fork token; any operator
listing (none exists); any trade, handoff, reservation (#151) or broker truth; guard
enforcement (unwired), R9, sustained operation or production readiness. The proposal is
never resumed or completed: "stranded", not "survives".
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.safety.test_database_lock_integrity import (
    _Children,
    _children,
    _group_alive,
    _pid_alive,
)

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    FamilyPromotion,
    InstrumentScope,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.supervisor.proposals import INTERRUPTED_CLAIMS_ALERT_KIND
from chronos.supervisor.proposers import ProposerRegistration
from chronos.utils.identifiers import account_fingerprint

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process groups and /proc")

_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)
_DECISIONS = f"autonomy.decisions:{_FINGERPRINT}"
_CYCLES = f"autonomy.cycles:{_FINGERPRINT}"
_PROPOSER_HEADER = "X-Chronos-Proposer-Token"
_PROPOSAL = {
    "kind": "HOLD",
    "asset_class": "EQUITY",
    "symbol": "SPY",
    "direction": "NEUTRAL",
    "thesis": "m2 end-to-end synthetic proposal",
}
_READY_S = 60.0
_DRAIN_S = 30.0

# The child: the real backend, configured from env and files, plus the test-only seam.
_CHILD_SOURCE = """
import json, os, signal, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
log = os.open(cfg["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(log, 1)
os.dup2(log, 2)
os.environ.update(cfg["env"])
target = cfg["kill_queue_id"]
if target is not None:
    from chronos.supervisor import proposals
    real_mark_processed = proposals.mark_processed
    def mark_processed(*args, **kwargs):
        if kwargs.get("queue_id") == target:
            os.kill(os.getpid(), signal.SIGKILL)
        return real_mark_processed(*args, **kwargs)
    proposals.mark_processed = mark_processed
import uvicorn
from chronos.api.main import create_app
uvicorn.run(create_app(), host="127.0.0.1", port=cfg["port"], log_level="warning")
"""

# _Children runs ``python -c <prelude + code>``; the code runs the seam file from disk.
_RUN_CHILD_FILE = """
import runpy, sys
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@dataclass
class _Backend:
    proc: Any
    base: str
    log: Path

    def events(self) -> set[str]:
        text = self.log.read_text(encoding="utf-8", errors="replace") if self.log.exists() else ""
        found: set[str] = set()
        for line in text.splitlines():
            try:
                event = json.loads(line).get("event")
            except (ValueError, AttributeError):
                continue
            if event:
                found.add(event)
        return found


def _free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_grants(work: Path) -> str:
    """A SHADOW mandate and one proposer registration (Phase B's shapes); returns the token."""

    now = datetime.now(UTC)
    versions = VersionPins(
        provider="local",
        model_id="m2-e2e",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
    )
    mandate = AutonomyMandate(
        mandate_id="m2-e2e",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=AutonomyMode.SHADOW,
        promotions=(
            FamilyPromotion(asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.SHADOW),
        ),
        effective_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        versions=versions,
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        owner_authorization_ref="synthetic-m2",
        authored_at=now,
    )
    token = os.urandom(32).hex()
    registration = ProposerRegistration(
        proposer_id="m2-e2e",
        secret_sha256=hashlib.sha256(token.encode()).hexdigest(),
        **versions.model_dump(),
        expires_at=now + timedelta(hours=1),
        enabled=True,
    )
    document = registration.model_dump(mode="json")
    document["expires_at"] = registration.expires_at.astimezone(UTC).isoformat()
    for name, content in (
        ("mandate.json", mandate.model_dump_json()),
        ("proposers.json", json.dumps({"schema_version": 1, "proposers": [document]})),
    ):
        path = work / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    (work / "m2_child.py").write_text(_CHILD_SOURCE, encoding="utf-8")
    return token


def _env(work: Path) -> dict[str, str]:
    return {
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "DEMO_PROFILE": "empty_account",
        "DATABASE_URL": f"sqlite:///{work / 'chronos.db'}",
        "LIVE_KILL_SWITCH_FILE": str(work / "kill.json"),
        "SESSION_BASELINE_FILE": str(work / "baseline.json"),
        "LOG_FILE": str(work / "chronos.log"),
        "BACKEND_TOKEN_FILE": str(work / "backend_api_token"),
        "AUTONOMY_MANDATE_FILE": str(work / "mandate.json"),
        "AUTONOMY_PROPOSERS_FILE": str(work / "proposers.json"),
        "AUTONOMY_ALERT_FILE": str(work / "owner_alerts.jsonl"),
        "CLOCK_HEALTH_PROVIDER": "disabled",
        # RuntimeConfig requires idle >= minimum; ingress never calls note_event, so the
        # idle interval is the cadence that drains a queued proposal.
        "AUTONOMY_MIN_INTERVAL_SECONDS": "1",
        "AUTONOMY_IDLE_INTERVAL_SECONDS": "2",
    }


def _spawn(children: _Children, work: Path, name: str, kill_queue_id: int | None) -> _Backend:
    port = _free_port()
    log = work / f"{name}.log"
    config = work / f"{name}.json"
    config.write_text(
        json.dumps(
            {"env": _env(work), "port": port, "log": str(log), "kill_queue_id": kill_queue_id}
        ),
        encoding="utf-8",
    )
    proc = children.popen(_RUN_CHILD_FILE, str(work / "m2_child.py"), str(config))
    return _Backend(proc=proc, base=f"http://127.0.0.1:{port}", log=log)


def _until(predicate: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f"timed out after {seconds}s waiting for {what}"
        time.sleep(0.2)


def _wait_ready(backend: _Backend) -> None:
    def ready() -> bool:
        assert backend.proc.poll() is None, f"the backend exited: {backend.proc.returncode}"
        try:
            response = httpx.get(backend.base + "/health/ready", timeout=2)
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and response.json().get("state") == "READY"

    _until(ready, _READY_S, f"/health/ready READY at {backend.base}")


def _post(backend: _Backend, token: str) -> httpx.Response:
    return httpx.post(
        backend.base + "/autonomy/proposals",
        json=_PROPOSAL,
        headers={_PROPOSER_HEADER: token},
        timeout=10,
    )


def _stop(backend: _Backend) -> None:
    backend.proc.send_signal(signal.SIGTERM)
    backend.proc.wait(timeout=30)


def _rows(work: Path, sql: str, *args: object) -> list[dict[str, Any]]:
    """Committed state, read through a fresh READ-ONLY connection of the test's own."""

    with closing(sqlite3.connect(f"file:{work / 'chronos.db'}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def _queue(work: Path) -> dict[int, dict[str, Any]]:
    rows = _rows(work, "SELECT id, status, cycle_stage FROM autonomy_proposal_queue")
    return {row["id"]: row for row in rows}


def _journal(work: Path, stream: str) -> list[dict[str, Any]]:
    return [
        json.loads(row["payload_json"])
        for row in _rows(
            work,
            "SELECT payload_json FROM hash_chain_records WHERE stream = ? ORDER BY sequence",
            stream,
        )
    ]


def _interrupted_alert_rows(work: Path) -> list[dict[str, Any]]:
    return _rows(
        work, "SELECT * FROM autonomy_owner_alerts WHERE kind = ?", INTERRUPTED_CLAIMS_ALERT_KIND
    )


def _alert_file_kinds(work: Path) -> list[str]:
    path = work / "owner_alerts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line).get("kind") for line in path.read_text().splitlines() if line]


def _processed(work: Path, queue_id: int) -> bool:
    row = _queue(work).get(queue_id)
    return row is not None and row["status"] == "PROCESSED"


@pytest.fixture()
def work(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


def _assert_reaped(backends: list[_Backend]) -> None:
    """A8: no child (nor its group: the sentinel and any descendant) outlives the context."""

    for backend in backends:
        assert not _pid_alive(backend.proc.pid), f"backend pid {backend.proc.pid} survived"
        assert not _group_alive(backend.proc.pid), f"group {backend.proc.pid} survived"


def test_m2_a_committed_claim_stays_stranded_and_is_not_replayed_after_a_crash(
    work: Path,
) -> None:
    token = _write_grants(work)
    spawned: list[_Backend] = []
    with _children() as children:
        # S1 / A1: the real backend, from env and files only, ready and autonomous.
        p1 = _spawn(children, work, "p1", kill_queue_id=2)  # B is row 2 on a fresh db
        spawned.append(p1)
        _wait_ready(p1)
        assert {"autonomy_mandate_activated", "autonomy_started"} <= p1.events(), p1.events()

        # S2 / A2: proposal A, received as a queue receipt, then judged SHADOW on a tick.
        receipt = _post(p1, token)
        assert receipt.status_code == 202, receipt.text
        assert receipt.json()["stage"] == "QUEUED", receipt.text
        _until(lambda: _processed(work, 1), _DRAIN_S, "proposal A to be PROCESSED")
        assert _queue(work)[1]["cycle_stage"] == "ADMISSION"
        decisions = _journal(work, _DECISIONS)
        assert len(decisions) == 1 and len(_journal(work, _CYCLES)) == 1
        assert decisions[0]["admitted"] is False
        assert decisions[0]["refusal"] == "MODE_CANNOT_SUBMIT"
        counters = _rows(work, "SELECT orders_submitted FROM autonomy_session_counters")
        assert all(row["orders_submitted"] == 0 for row in counters), counters

        # S3 / A3: proposal B; the seam SIGKILLs P1 inside mark_processed(queue_id=2).
        receipt = _post(p1, token)
        assert receipt.status_code == 202, receipt.text
        _until(lambda: p1.proc.poll() is not None, _DRAIN_S, "the seam to kill P1")
        assert p1.proc.returncode == -signal.SIGKILL, p1.proc.returncode
        stranded = _queue(work)[2]
        assert stranded["status"] == "CLAIMED", stranded
        assert stranded["cycle_stage"].startswith("claim:"), stranded
        token_of_p1 = stranded["cycle_stage"]
        assert len(_journal(work, _DECISIONS)) == 1 and len(_journal(work, _CYCLES)) == 1

        # S4 / A4: an immediate restart finds the dead writer's lease and runs READ-ONLY.
        p2a = _spawn(children, work, "p2a", kill_queue_id=None)
        spawned.append(p2a)
        _until(lambda: "backend_read_only" in p2a.events(), _READY_S, "P2a to boot read-only")
        assert "autonomy_started" not in p2a.events(), p2a.events()
        assert _queue(work)[2] == stranded
        _stop(p2a)

        # S5 / A5 / A6: once the lease has expired, P2 is the writer; it drains new work and
        # never re-presents the stranded row.
        (lease,) = _rows(work, "SELECT expires_at FROM writer_lease WHERE id = 1")
        expires = datetime.fromisoformat(lease["expires_at"])
        _until(
            lambda: datetime.now(UTC) > expires + timedelta(seconds=1),
            45,
            "the dead writer's lease to expire",
        )
        p2 = _spawn(children, work, "p2", kill_queue_id=None)
        spawned.append(p2)
        _wait_ready(p2)
        assert "autonomy_started" in p2.events(), p2.events()
        receipt = _post(p2, token)
        assert receipt.status_code == 202, receipt.text
        _until(lambda: _processed(work, 3), _DRAIN_S, "proposal C to be PROCESSED by P2")

        # Every claim_batch raises the folded alert while the row stays CLAIMED, so its
        # occurrence count is the tick counter: wait for >= 3 P2 ticks.
        _until(
            lambda: any(r["occurrences"] >= 3 for r in _interrupted_alert_rows(work)),
            _DRAIN_S,
            "three P2 ticks over the stranded claim",
        )
        assert _queue(work)[2] == {"id": 2, "status": "CLAIMED", "cycle_stage": token_of_p1}
        assert len(_journal(work, _DECISIONS)) == 2, "B was judged: it was re-presented"
        assert len(_journal(work, _CYCLES)) == 2
        assert len(_interrupted_alert_rows(work)) == 1
        assert _alert_file_kinds(work).count(INTERRUPTED_CLAIMS_ALERT_KIND) == 1
        _stop(p2)
    _assert_reaped(spawned)


def test_m2_negative_control_without_the_seam_the_second_proposal_completes(
    work: Path,
) -> None:
    """A7: the same backend and proposals with no seam target: B reaches PROCESSED and no
    interrupted-claims alert exists, so it is the seam, nothing else, that strands B."""

    token = _write_grants(work)
    spawned: list[_Backend] = []
    with _children() as children:
        p1 = _spawn(children, work, "p1", kill_queue_id=None)
        spawned.append(p1)
        _wait_ready(p1)
        for queue_id in (1, 2):
            receipt = _post(p1, token)
            assert receipt.status_code == 202, receipt.text
            _until(lambda q=queue_id: _processed(work, q), _DRAIN_S, f"row {queue_id}")
        assert p1.proc.poll() is None
        assert len(_journal(work, _DECISIONS)) == 2
        assert _interrupted_alert_rows(work) == []
        assert INTERRUPTED_CLAIMS_ALERT_KIND not in _alert_file_kinds(work)
        _stop(p1)
    _assert_reaped(spawned)

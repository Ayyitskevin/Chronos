"""N3-2: the cutover archive command (strategy (iii)) — flatten gate, envelope, move-aside.

Every store here is a synthetic pre-cutover store under ``tmp_path``: created by the
repository's own initializer, bound to a synthetic fingerprint, then set ONE schema version
behind the running code (``schema_version = SCHEMA_VERSION - 1``) — the shape the command
meets on cutover day, when N3-1's code (schema 18) archives the schema-17 store. Age keys are
generated at run time by ``age-keygen`` (the restore drill's own fixture pattern); no key, no
account id and no 64-hex literal appears in this file (digests are derived with ``hashlib``).

Numbered: (1) dry-run, (2) the flatten gate on bindings, (3) on the latest reconciliation run,
(4) on the service posture and the store's version, (5) ``--apply`` (envelope → VERIFIED →
move-aside; a failed drill or an existing target leaves the live store untouched), (6) the
pins (never reads ``CHRONOS_PERSISTENCE_KEY``; imports no broker/orders module itself; no
socket is ever opened), (7) the runbook contract and the escrow tool, (8) G1 — the drill's
``ArchiveBinding`` seam (a distinct ``ARCHIVE_VERIFIED`` verdict), with the control that proves
the gap it closes, (9) r1 — one bound source capability across gate, backup and move (Daybreak's
A/B substitution and the independent main-file, sidecar and manifest swaps), r2 — writer
exclusion by kernel read lease and the move's final-syscall truth (9f-9l), the archive admission
only the gate can mint (8h), the escrow cleanup bound to the file this run created (7g-7h); r3 —
(10) the live namespace is never vacant: sentinel directories exchanged into every live name
(Daybreak's post-rename planting, the owner-uid vacate, every name until the owner's rmdir, a
non-admitted name occupied), the archive drill that runs the gate itself (8a-8h: every seam on a
refused store is tried), the escrow tool that deletes nothing after a post-create failure (7g, 7i).

"Zero changes" is asserted EXACTLY, never through a filter: the store's bytes are unchanged and
the only entries a run may add beside a WAL-mode store are SQLite's reader bookkeeping — an empty
``chronos.db-wal`` and a ``chronos.db-shm`` — which ``--apply`` moves aside with the store.

The managed-position binding row is inserted with foreign-key enforcement OFF (a plain
SQLAlchemy engine, never ``Database``): its ``order_intents``/``risk_decisions`` parents are
not the gate's input — the binding and its position-management stream are.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import inspect
import io
import itertools
import json
import os
import pickle
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
from collections.abc import Iterator, MutableMapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from chronos.domain.enums import DataQuality
from chronos.operations import cutover_archive as cutover
from chronos.operations import restore_drill as drill
from chronos.operations import restore_drill as restore_drill_module
from chronos.operations.restore_drill import BackupManifest, Clock, run_drill
from chronos.persistence import database as database_module
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.schema import (
    DatabaseScopeRow,
    HashChainRow,
    ManagedPositionBindingRow,
    PositionProvenanceRow,
    ReconciliationRunRow,
    WriterLeaseRow,
)
from chronos.supervisor.position_management import (
    DirectiveOutcome,
    DirectiveResolution,
    PositionObservation,
    build_qqq_five_tool_paper_plan,
    evaluate_position,
    record_directive_resolution,
    register_position,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "chronos" / "operations" / "cutover_archive.py"
RUNBOOK = ROOT / "docs" / "ops" / "N3-CUTOVER.md"
OPS_README = ROOT / "docs" / "ops" / "README.md"
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}
AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")
pytestmark = pytest.mark.skipif(
    AGE is None or AGE_KEYGEN is None, reason=drill.AGE_INSTALL_SENTENCE
)


def _chain_problem(path: Path) -> str | None:
    """A test-side statement of the protected-path precondition (r5), independent of the module
    under test: every directory from ``path`` up to ``/`` is a real directory (no symlink) owned
    by root or this uid with no group or other write bit — a /tmp-style sticky world-writable
    directory included, by the rule's own choice."""

    euid = os.geteuid()
    for ancestor in (path, *path.parents):
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return f"{ancestor} is not a plain directory"
        if st.st_uid not in (0, euid):
            return f"{ancestor} is owned by uid {st.st_uid}"
        if st.st_mode & 0o022:
            return f"{ancestor} has mode {stat.S_IMODE(st.st_mode):04o}"
    return None


def _private_root() -> Path:
    """Where this module keeps its stores: the first candidate whose whole ancestor chain is
    protected. pytest's own basetemp lives under ``/tmp`` — sticky and world-writable by design
    — which the cutover refuses (``PATH_NOT_PROTECTED``, r5), so the stores go under
    ``$XDG_RUNTIME_DIR``, ``~/.cache`` or ``~`` instead, in a 0700 directory of their own."""

    candidates = [os.environ.get("XDG_RUNTIME_DIR"), Path.home() / ".cache", Path.home()]
    problems: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        base = Path(candidate)
        if not base.is_dir():
            continue
        problem = _chain_problem(base)
        if problem is None:
            root = base / "chronos-n3-2-tests"
            root.mkdir(mode=0o700, exist_ok=True)
            os.chmod(root, 0o700)
            return root
        problems.append(problem)
    pytest.fail(f"no protected directory chain for the test stores: {problems}")


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Overrides pytest's ``tmp_path`` for this module: a 0700 directory under
    :func:`_private_root`, removed after the test (see there for why not ``/tmp``)."""

    path = Path(tempfile.mkdtemp(prefix="n3-2-", dir=_private_root()))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _hex(seed: str) -> str:
    """A 64-hex digest derived at run time — never a literal."""

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


FINGERPRINT = _hex("n3-2 synthetic account fingerprint")
POSITION_ID = "CHR-POS-" + _hex("n3-2 position")[:32]
OPENING_ORDER = "CHR-ORD-" + _hex("n3-2 opening order")[:32]
RUN_ID = "run-n3-2-0001"
CON_ID = 320227571


# ------------------------------------------------------------- fixtures (the drill's pattern)


@pytest.fixture(autouse=True)
def _a_standalone_process_repair_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """DBLOCK: the command runs as its own process, where the drill's acceptance construction
    is the first file-backed Database in the process (see test_ops_restore_drill)."""

    monkeypatch.setattr(database_module, "_REPAIR_WINDOW_OPEN", True)
    real_database = restore_drill_module.Database

    class AcceptInAFreshProcess(real_database):  # type: ignore[misc, valid-type]
        def __init__(self, url: str) -> None:
            monkeypatch.setattr(database_module, "_REPAIR_WINDOW_OPEN", True)
            super().__init__(url)

    monkeypatch.setattr(restore_drill_module, "Database", AcceptInAFreshProcess)


@dataclass(frozen=True)
class KeyMaterial:
    identity: Path
    recipients: Path
    host_public_key: str


def _keygen(path: Path) -> str:
    assert AGE_KEYGEN is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([AGE_KEYGEN, "-o", str(path)], check=True, capture_output=True)
    path.chmod(0o600)
    derived = subprocess.run([AGE_KEYGEN, "-y", str(path)], check=True, capture_output=True)
    return derived.stdout.decode().strip()


@pytest.fixture(autouse=True)
def age_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KeyMaterial:
    """A throwaway host identity (0600) and the two recipients, named by the drill's env vars."""

    keys = tmp_path / "keys"
    identity = keys / "backup-host.age"
    host_public_key = _keygen(identity)
    recipients = keys / "backup-recipients.txt"
    recipients.write_text(
        f"{host_public_key}\n{drill.KEVIN_RECOVERY_RECIPIENT}\n", encoding="utf-8"
    )
    monkeypatch.setenv(drill.IDENTITY_ENV, str(identity))
    monkeypatch.setenv(drill.RECIPIENTS_ENV, str(recipients))
    return KeyMaterial(identity=identity, recipients=recipients, host_public_key=host_public_key)


# ------------------------------------------------------------- the synthetic pre-cutover store


@dataclass(frozen=True)
class Store:
    db: Path
    now: datetime  # the frozen wall clock the command runs under

    @property
    def stamp(self) -> str:
        return self.now.strftime("%Y%m%dT%H%M%SZ")


def _plan(*, opened_at: datetime) -> Any:
    return build_qqq_five_tool_paper_plan(
        position_id=POSITION_ID,
        opening_order_ref=OPENING_ORDER,
        account_fingerprint=FINGERPRINT,
        entry_fill_ids=("exec-open-1",),
        opening_fill_evidence_digest=_hex("opening fill evidence"),
        entry_risk_evidence_digest=_hex("entry risk evidence"),
        opened_at=opened_at,
        quantity=Decimal(9),
        entry_price=Decimal(100),
        initial_stop_price=Decimal(99),
        signal_time_risk_distance_usd=Decimal(1),
        strategy_nav_usd=Decimal(3000),
        unit_exposure_cvar_loss_fraction=Decimal("0.05"),
    )


def _observation(*, as_of: datetime, last_price: Decimal) -> PositionObservation:
    return PositionObservation(
        observation_id="obs-1",
        account_fingerprint=FINGERPRINT,
        as_of=as_of,
        evidence_digest=_hex("observation evidence"),
        data_quality=DataQuality.LIVE,
        last_price=last_price,
        marked_strategy_nav_usd=Decimal(3000),
        broker_position_quantity=Decimal(9),
        reconciliation_generation=7,
        reconciliation_session_id="recon-7",
    )


def _position_entry(quantity: str = "9") -> dict[str, Any]:
    return {
        "contract": {"con_id": CON_ID, "symbol": "QQQ", "security_type": "STK"},
        "quantity": quantity,
        "average_cost": "100.00",
        "position_key": f"QQQ:{CON_ID}",
    }


def _snapshot(positions: list[dict[str, Any]], readiness_status: str) -> dict[str, Any]:
    return {
        "account_facts": {
            "account_fingerprint": FINGERPRINT,
            "net_liquidation": "100000.50",
            "total_cash": "50000.00",
            "buying_power": "200000.00",
            "currency": "USD",
            "as_of": "2026-10-01T14:00:00+00:00",
        },
        "positions": positions,
        "open_orders": [],
        "evidence_age_seconds": 1.0,
        "execution_count": 0,
        "readiness": {"status": readiness_status, "reason": "parity", "generation": 1},
        "reasons": [],
    }


def _binding_row(admitted_at: datetime) -> ManagedPositionBindingRow:
    return ManagedPositionBindingRow(
        account_fingerprint=FINGERPRINT,
        opening_order_ref=OPENING_ORDER,
        position_id=POSITION_ID,
        risk_decision_id="risk-n3-2-0001",
        broker_order_id=1,
        permanent_id=1,
        opening_fill_evidence_digest=_hex("opening fill evidence"),
        entry_risk_evidence_digest=_hex("entry risk evidence"),
        candidate_spec_sha256=_hex("candidate spec"),
        management_policy_sha256=_hex("management policy"),
        reconciliation_session_id="recon-7",
        reconciliation_generation=7,
        admitted_at=admitted_at,
    )


def _checkpoint_and_drop_sidecars(db: Path) -> None:
    with closing(sqlite3.connect(db)) as connection:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert result is not None and result[0] == 0, result
    db.with_name(db.name + "-wal").unlink(missing_ok=True)
    db.with_name(db.name + "-shm").unlink(missing_ok=True)


def _store(
    tmp_path: Path,
    *,
    version_offset: int = -1,
    bound: bool = True,
    run: str | None = "clean",
    run_age: timedelta = timedelta(seconds=30),
    binding: str | None = None,
    positions: tuple[dict[str, Any], ...] = (),
    provenance: tuple[str, ...] = (),
    lease_expires_in: timedelta | None = None,
    journal_mode: str = "wal",
) -> Store:
    """A pre-cutover store. ``run``: ``clean`` | ``manual_review`` | ``readiness_pending`` |
    ``None``; ``binding``: ``closed`` | ``open`` | ``orphan`` (no stream) | ``tampered``
    (stream payload edited) | ``None``; ``provenance``: origin classes written for the run;
    ``journal_mode``: ``wal`` (the repository's) or ``delete`` (a rollback-journal store: a
    read-only connection then leaves NO bookkeeping beside it, so the admitted set is the store
    alone)."""

    data = tmp_path / "data"
    made = [d for d in (data, *data.parents) if not d.exists()]
    data.mkdir(parents=True, exist_ok=True)
    for directory in (data, *made):  # the owner-private data/ (r4) and, umask-proof, every
        os.chmod(directory, 0o700)  # directory this helper made under the fixture root (r5)
    db = data / "chronos.db"
    database = Database(f"sqlite:///{db}")
    try:
        database.initialize()
    finally:
        database.dispose()
    # the frozen clock the command runs under: after every default *_at the rows below carry
    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)
    opened_at = now - timedelta(hours=2)
    engine = create_engine(f"sqlite:///{db}")  # plain engine: no WAL pragma, no FK enforcement
    try:
        with Session(engine) as session, session.begin():
            if bound:
                session.add(
                    DatabaseScopeRow(
                        id=1,
                        broker_mode="demo",
                        environment="paper",
                        account_fingerprint=FINGERPRINT,
                    )
                )
            if run is not None:
                status = "MANUAL_REVIEW" if run == "manual_review" else "RECONCILED"
                readiness = "PENDING" if run == "readiness_pending" else "RECONCILED"
                completed = now - run_age
                session.add(
                    ReconciliationRunRow(
                        id=RUN_ID,
                        trigger="periodic",
                        status=status,
                        broker_snapshot=_snapshot(list(positions), readiness),
                        decisions=[],
                        started_at=completed - timedelta(seconds=30),
                        completed_at=completed,
                    )
                )
                for origin in provenance:
                    session.add(
                        PositionProvenanceRow(
                            account_fingerprint=FINGERPRINT,
                            position_key=f"{CON_ID}:STK:LONG",
                            origin_class=origin,
                            evidence_ref=POSITION_ID if origin == "MANAGED" else f"{RUN_ID}:x",
                            first_seen_run_id=RUN_ID,
                            observed_run_id=RUN_ID,
                            quantity=Decimal(9),
                            recorded_at=completed,
                        )
                    )
            if binding is not None:
                session.add(_binding_row(opened_at + timedelta(seconds=1)))
                if binding != "orphan":
                    register_position(
                        session,
                        plan=_plan(opened_at=opened_at),
                        recorded_at=opened_at + timedelta(seconds=1),
                    )
                if binding in ("closed", "tampered"):
                    evaluated_at = now - timedelta(hours=1)
                    directive = evaluate_position(
                        session,
                        position_id=POSITION_ID,
                        observation=_observation(as_of=evaluated_at, last_price=Decimal("98.90")),
                        evaluated_at=evaluated_at,
                    ).directive
                    assert directive is not None
                    record_directive_resolution(
                        session,
                        position_id=POSITION_ID,
                        account_fingerprint=FINGERPRINT,
                        resolution=DirectiveResolution(
                            directive_ref=directive.directive_ref,
                            outcome=DirectiveOutcome.FILLED,
                            occurred_at=evaluated_at + timedelta(seconds=1),
                            evidence_digest=_hex("resolution evidence"),
                            execution_id="exec-close-1",
                            fill_quantity=Decimal(9),
                            fill_price=Decimal("98.90"),
                        ),
                    )
            if lease_expires_in is not None:
                session.add(
                    WriterLeaseRow(
                        id=1,
                        token=_hex("lease token")[:32],
                        holder="backend:127.0.0.1:8000",
                        acquired_at=(now - timedelta(seconds=10)).isoformat(),
                        expires_at=(now + lease_expires_in).isoformat(),
                    )
                )
        if binding == "tampered":
            with Session(engine) as session, session.begin():
                row = session.query(HashChainRow).filter_by(kind="POSITION_EVALUATED").one()
                row.payload_json = row.payload_json.replace("98.90", "98.91", 1)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "UPDATE schema_version SET version = ?", (SCHEMA_VERSION + version_offset,)
        )
    _checkpoint_and_drop_sidecars(db)
    if journal_mode == "delete":
        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert _entries(db.parent) == ["chronos.db"]
    return Store(db=db, now=now)


def _clock(now: datetime) -> Clock:
    ticks = itertools.count(0.0, 0.25)
    return Clock(wall=lambda: now, monotonic=lambda: next(ticks))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


READER_BOOKKEEPING = ("chronos.db-wal", "chronos.db-shm")
#: the store's whole live namespace — the four names the repository's R-21 admission checks
#: (`database._SQLITE_SIDECAR_SUFFIXES`); after ARCHIVED each is a sentinel directory (r3)
LIVE_NAMES = ("chronos.db", "chronos.db-wal", "chronos.db-shm", "chronos.db-journal")


def _is_sentinel(path: Path) -> bool:
    """A 0500 directory owned by this user, empty, by lstat: what the move leaves at a live name."""

    st = os.lstat(path)
    return (
        stat.S_ISDIR(st.st_mode)
        and (st.st_mode & 0o777) == 0o500
        and st.st_uid == os.geteuid()
        and os.listdir(path) == []
    )


def _rmdir_sentinels(db: Path) -> None:
    """The owner's step 7: the four live names are vacated by hand, nothing else."""

    for name in LIVE_NAMES:
        os.rmdir(db.with_name(name))


def _entries(directory: Path) -> list[str]:
    """Every directory entry, unfiltered."""

    return sorted(p.name for p in directory.iterdir())


def _assert_only_reader_bookkeeping(store: Store, before: list[str], before_sha: str) -> None:
    """The exact allowed delta of a dry-run or a refusal: the store's bytes are unchanged, no
    entry disappeared, and the only entries that may have appeared are SQLite's reader
    bookkeeping beside a WAL-mode store — an empty ``chronos.db-wal`` and a ``chronos.db-shm``.
    Nothing else; no filtering."""

    assert _sha256(store.db) == before_sha
    after = _entries(store.db.parent)
    assert set(before) <= set(after), (before, after)
    assert set(after) - set(before) <= set(READER_BOOKKEEPING), (before, after)
    wal = store.db.with_name("chronos.db-wal")
    if wal.exists():
        assert wal.stat().st_size <= cutover.WAL_HEADER_BYTES, wal.stat().st_size


def _identity(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _run(
    store: Store, tmp_path: Path, *args: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    argv = [
        "--db",
        str(store.db),
        "--out",
        str(tmp_path / "backups"),
        "--restore-into",
        str(tmp_path / "restored"),
        *args,
    ]
    code = cutover.main(argv, clock=_clock(store.now))
    out = capsys.readouterr().out
    return code, json.loads(out)


# ------------------------------------------------------------- (1) dry-run


def test_1a_dry_run_passes_the_gate_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, capsys=capsys)
    assert code == 0, report
    assert report["verdict"] == "DRY_RUN" and report["dry_run"] is True
    assert report["gate"]["bindings"] == {"total": 1, "closed": 1}
    assert report["gate"]["positions_observed"] == 0
    assert report["gate"]["schema_version"] == SCHEMA_VERSION - 1
    assert report["planned"]["envelope"].endswith(f"chronos-{store.stamp}")
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)
    assert not (tmp_path / "backups").exists() and not (tmp_path / "restored").exists()


def test_1c_the_gate_s_only_directory_effect_is_sqlite_s_reader_bookkeeping_and_the_claim_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P2-1: a read-only connection on a WAL-mode store leaves an empty ``-wal`` and a ``-shm``
    beside it (measured on this host, SQLite 3.53); the module and the runbook say exactly that,
    never "creates nothing" about the directory."""

    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, capsys=capsys)
    assert code == 0, report
    after = _entries(store.db.parent)
    assert after == ["chronos.db", "chronos.db-shm", "chronos.db-wal"], after
    assert store.db.with_name("chronos.db-wal").stat().st_size == 0
    assert _sha256(store.db) == before
    # the refusal path leaves the same exact delta and nothing else
    refused = _store(tmp_path / "refused", binding="open")
    code, report = _run(refused, tmp_path / "refused", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "OPEN_BINDING"
    assert _entries(refused.db.parent) == ["chronos.db", "chronos.db-shm", "chronos.db-wal"]
    for text in (MODULE.read_text(encoding="utf-8"), RUNBOOK.read_text(encoding="utf-8")):
        assert "creates nothing" not in text
        assert "reader bookkeeping" in text


def test_1b_the_cli_dry_run_exits_0_with_the_json_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    argv = [
        sys.executable,
        "-m",
        "chronos.operations.cutover_archive",
        "--db",
        str(store.db),
        "--out",
        str(tmp_path / "backups"),
        "--restore-into",
        str(tmp_path / "restored"),
    ]
    completed = subprocess.run(
        argv, cwd=ROOT, env={**os.environ, **SAFE_ENV}, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["verdict"] == "DRY_RUN"
    assert not (tmp_path / "backups").exists()


# ------------------------------------------------------------- (2) the gate: bindings


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_2a_an_open_binding_refuses_with_zero_changes(
    tmp_path: Path, apply: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="open")
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, *(["--apply"] if apply else []), capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "OPEN_BINDING"
    assert "1 of 1" in report["failures"][0]
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)
    assert not (tmp_path / "backups").exists() and not (tmp_path / "restored").exists()


def test_2b_a_closed_binding_passes_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    code, report = _run(store, tmp_path, capsys=capsys)
    assert code == 0 and report["gate"]["bindings"] == {"total": 1, "closed": 1}


@pytest.mark.parametrize("shape", ["orphan", "tampered"])
def test_2c_a_binding_whose_stream_is_missing_or_broken_refuses_as_unreadable(
    tmp_path: Path, shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding=shape)
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "BINDING_UNREADABLE", report
    assert not (tmp_path / "backups").exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


# ------------------------------------------------------------- (3) the gate: reconciliation


def test_3a_no_reconciliation_run_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, run=None)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "NO_RECONCILIATION_RUN", report
    assert not (tmp_path / "backups").exists()


def test_3b_a_reconciliation_older_than_the_bound_refuses_and_one_inside_it_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert timedelta(hours=1) == cutover.MAX_RECONCILIATION_AGE
    stale = _store(tmp_path / "stale", run_age=timedelta(minutes=61))
    code, report = _run(stale, tmp_path / "stale", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "RECONCILIATION_STALE", report
    assert not (tmp_path / "stale" / "backups").exists()
    fresh = _store(tmp_path / "fresh", run_age=timedelta(minutes=59))
    code, report = _run(fresh, tmp_path / "fresh", capsys=capsys)
    assert code == 0 and report["gate"]["reconciliation"]["age_s"] == pytest.approx(59 * 60)


@pytest.mark.parametrize("shape", ["manual_review", "readiness_pending"])
def test_3c_an_unclean_reconciliation_refuses(
    tmp_path: Path, shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, run=shape)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "RECONCILIATION_NOT_CLEAN", report
    assert not (tmp_path / "backups").exists()


def test_3d_a_position_in_the_latest_run_refuses_with_the_origin_class_breakdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    classified = _store(
        tmp_path / "classified",
        binding="closed",
        positions=(_position_entry(),),
        provenance=("MANAGED",),
    )
    code, report = _run(classified, tmp_path / "classified", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "POSITION_OBSERVED", report
    assert "MANAGED 1" in report["failures"][0] and "FOREIGN 0" in report["failures"][0]
    assert not (tmp_path / "classified" / "backups").exists()
    # the run reported a position but the provenance hook wrote nothing: still a refusal
    unclassified = _store(tmp_path / "unclassified", positions=(_position_entry(),))
    code, report = _run(unclassified, tmp_path / "unclassified", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "PROVENANCE_MISSING", report


# ------------------------------------------------------------- (4) the gate: service posture


def test_4a_a_live_writer_lease_refuses_and_an_expired_one_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = _store(tmp_path / "live", lease_expires_in=timedelta(seconds=20))
    code, report = _run(live, tmp_path / "live", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "WRITER_LEASE_LIVE", report
    assert "backend:127.0.0.1:8000" in report["failures"][0]
    assert not (tmp_path / "live" / "backups").exists()
    expired = _store(tmp_path / "expired", lease_expires_in=timedelta(seconds=-1))
    code, report = _run(expired, tmp_path / "expired", capsys=capsys)
    assert code == 0 and report["gate"]["writer_lease"]["live"] is False


WRITE_A_FRAME = (
    "import sqlite3, sys, time\n"
    "c = sqlite3.connect(sys.argv[1])\n"
    "c.execute('PRAGMA wal_autocheckpoint=0')\n"
    'c.execute("INSERT INTO application_events (event_type, severity, message, event_data,'
    " occurred_at) VALUES ('n3', 'INFO', 'frame', '{}', '2026-10-01 00:00:00.000000')\")\n"
    "c.commit()\n"
    "print('committed', flush=True)\n"
    "time.sleep(120)\n"
)


def test_4b_write_ahead_frames_beside_the_store_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A writer that committed and is STILL OPEN is refused by the r2 writer exclusion before
    the gate (WRITER_OPEN — the lease is refused while it holds the store); a writer that died
    without checkpointing (killed) leaves its frames and nothing open: the gate's own
    WAL_FRAMES_PRESENT. Either way no envelope exists."""

    store = _store(tmp_path)
    wal = store.db.with_name("chronos.db-wal")
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITE_A_FRAME, str(store.db)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert writer.stdout is not None and writer.stdout.readline().strip() == "committed"
        assert wal.stat().st_size > cutover.WAL_HEADER_BYTES
        code, report = _run(store, tmp_path, "--apply", capsys=capsys)
        assert code == 2 and report["code"] == "WRITER_OPEN", report
        assert report["gate"] is None
    finally:
        writer.kill()
        writer.wait()
    assert wal.stat().st_size > cutover.WAL_HEADER_BYTES  # the frames outlived the writer
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "WAL_FRAMES_PRESENT", report
    assert not (tmp_path / "backups").exists()


def test_4c_a_store_already_at_the_running_schema_version_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, version_offset=0)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "SCHEMA_ALREADY_CURRENT", report
    assert str(SCHEMA_VERSION) in report["failures"][0]
    assert not (tmp_path / "backups").exists()


def test_4d_an_unbound_store_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(tmp_path, bound=False)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "NO_SCOPE_ROW", report
    assert not (tmp_path / "backups").exists()


# ------------------------------------------------------------- (5) --apply


class _NoSocket:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the cutover archive opened a socket")


def test_5a_apply_archives_verified_then_moves_the_store_aside_under_the_envelope_stamp(
    tmp_path: Path,
    age_keys: KeyMaterial,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, binding="closed")
    original = _sha256(store.db)
    source_identity = _identity(store.db)
    with closing(sqlite3.connect(f"{store.db.as_uri()}?mode=ro", uri=True)) as connection:
        chain_before = connection.execute(
            "SELECT stream, sequence, record_hash FROM hash_chain_records ORDER BY stream, sequence"
        ).fetchall()
    monkeypatch.setattr(socket, "socket", _NoSocket)  # no broker, no network: proven, not said
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0, report
    assert report["verdict"] == "ARCHIVED" and report["dry_run"] is False
    assert report["drill"]["verdict"] == "ARCHIVE_VERIFIED"  # distinct from repository VERIFIED
    assert report["drill"]["acceptance"] == f"archive:schema_version:{SCHEMA_VERSION - 1}"
    envelope = Path(report["envelope"])
    assert envelope.name == f"chronos-{store.stamp}" and envelope.parent == tmp_path / "backups"
    manifest_path = envelope / drill.MANIFEST_NAME
    assert report["manifest_path"] == str(manifest_path)
    assert report["manifest_sha256"] == _sha256(manifest_path)
    manifest = BackupManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert report["cleartext_sha256"] == manifest.sha256
    assert report["ciphertext_sha256"] == manifest.ciphertext_sha256
    assert report["schema_head"] == manifest.schema_head == f"schema_version:{SCHEMA_VERSION - 1}"
    # the manifest carries the binding: the envelope is of the gated inode, which is the moved one
    assert tuple(manifest.source_identity) == source_identity
    # moved aside by rename, bound to the envelope's stamp; the main file's bytes are the original
    moved = {entry["from"]: entry["to"] for entry in report["moved_aside"]}
    archived = store.db.with_name(f"chronos.db.archived-{store.stamp}")
    assert moved[str(store.db)] == str(archived)
    assert not store.db.is_file() and _sha256(archived) == original
    assert _identity(archived) == source_identity
    # the admitted set — the store and the reader bookkeeping the gate's own connection left —
    # is exactly what was moved; every live name of the store's namespace now holds a sentinel
    # directory this run made (r3): nothing can create a fresh store there until the owner's rmdir
    assert _entries(store.db.parent) == sorted(
        [
            *LIVE_NAMES,
            *(f"{name}.archived-{store.stamp}" for name in ("chronos.db", *READER_BOOKKEEPING)),
        ]
    )
    assert all(_is_sentinel(store.db.with_name(name)) for name in LIVE_NAMES)
    assert "left_in_place" not in report  # retired: an occupied live name refuses the move
    assert [s["name"] for s in report["sentinels"]] == [
        str(store.db.with_name(name)) for name in LIVE_NAMES
    ]
    assert all(s["at"] == s["name"] for s in report["sentinels"])
    assert any("rmdir" in step for step in report["next_manual_steps"])
    # the original content is recoverable: by-hand age -d, then the drill's own restore
    assert AGE is not None
    by_hand = tmp_path / "by-hand"
    by_hand.mkdir()
    subprocess.run(
        [
            AGE,
            "-d",
            "-i",
            str(age_keys.identity),
            "-o",
            str(by_hand / "chronos.db"),
            str(envelope / "chronos.db.age"),
        ],
        check=True,
        capture_output=True,
    )
    assert _sha256(by_hand / "chronos.db") == manifest.sha256
    with closing(sqlite3.connect(f"{(by_hand / 'chronos.db').as_uri()}?mode=ro", uri=True)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        chain_after = c.execute(
            "SELECT stream, sequence, record_hash FROM hash_chain_records ORDER BY stream, sequence"
        ).fetchall()
        assert c.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION - 1,)]
    assert chain_after == chain_before and len(chain_before) == 3
    # a later re-verification under the running code: every verifier runs and the ONLY failure
    # is the repository's refusal of the retired version — ARCHIVE_VERIFIED exists only on
    # cutover day, from the gate; no caller-made value re-creates it (r2, P1-2)
    again = drill.restore(manifest, tmp_path / "again")
    assert not again.verified and again.acceptance == "repository"
    assert again.sha256_ok and again.row_counts_ok and not again.schema_head_ok
    assert [f.split(":", 1)[0] for f in again.failures] == ["schema_acceptance"], again.failures
    assert report["restore_dir"] == str(tmp_path / "restored")
    assert any("CHRONOS_PERSISTENCE_KEY" in step for step in report["next_manual_steps"])
    assert any("token_hex(32)" in step for step in report["next_manual_steps"])


def test_5b_a_drill_that_is_not_verified_leaves_the_live_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    # evidence dated after the (frozen) snapshot: the drill's rpo refuses → verdict FAILED
    with closing(sqlite3.connect(store.db)) as connection, connection:
        connection.execute(
            "INSERT INTO application_events (event_type, severity, message, event_data,"
            " occurred_at) VALUES ('n3', 'INFO', 'future', '{}', ?)",
            ((store.now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
    _checkpoint_and_drop_sidecars(store.db)
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "ARCHIVE_NOT_VERIFIED", report
    assert report["drill"]["verdict"] == "FAILED"
    assert any(f.startswith("rpo: refused") for f in report["drill"]["failures"])
    assert store.db.exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)
    assert "moved_aside" not in report or report["moved_aside"] == []
    assert Path(report["envelope"]).is_dir()  # the envelope exists; the JSON says where


def test_5c_an_existing_target_name_refuses_the_move_and_the_live_store_stays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    target = store.db.with_name(f"chronos.db.archived-{store.stamp}")
    target.write_bytes(b"not the store")
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "TARGET_EXISTS", report
    assert store.db.exists() and _sha256(store.db) == before
    assert target.read_bytes() == b"not the store"
    assert report["moved_aside"] == []
    assert Path(report["envelope"]).is_dir()


def test_5d_a_restore_target_that_is_not_fresh_refuses_before_any_envelope_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    (tmp_path / "restored").mkdir()
    (tmp_path / "restored" / "leftover").write_bytes(b"x")
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "RESTORE_TARGET_NOT_FRESH", report
    assert not (tmp_path / "backups").exists()  # no orphan envelope
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


# ------------------------------------------------------------- (6) pins


class _RecordingEnviron(MutableMapping[str, str]):
    """``os.environ`` with a record of every key that was LOOKED UP (get/[]), so a test can
    prove the command never asks for the persistence key — while the drill's own age-key
    variables are still answered."""

    def __init__(self, real: MutableMapping[str, str]) -> None:
        self._real = real
        self.looked_up: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.looked_up.append(key)
        return self._real[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._real[key] = value

    def __delitem__(self, key: str) -> None:
        del self._real[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._real)

    def __len__(self) -> int:
        return len(self._real)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.looked_up.append(key)
        return self._real.get(key, default)

    def copy(self) -> dict[str, str]:
        return dict(self._real)


def test_6a_the_module_never_reads_the_persistence_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # the variable's NAME may appear only inside a string the owner reads (the next-steps
    # text); never as an identifier, attribute or environment lookup
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            identifier = node.id if isinstance(node, ast.Name) else node.attr
            assert "PERSISTENCE" not in identifier.upper(), ast.unparse(node)
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv"):
            raise AssertionError(f"the module touches the environment: {ast.unparse(node)}")
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(
                ("chronos.config", "chronos.api", "chronos.broker", "chronos.orders")
            )
        ):
            raise AssertionError(f"the module imports {node.module}")
        if isinstance(node, ast.Import) and any(
            alias.name.startswith(("chronos.config", "chronos.api", "chronos.broker"))
            for alias in node.names
        ):
            raise AssertionError(ast.unparse(node))
    # and at run time, through the whole --apply path, the key is never looked up
    monkeypatch.setenv("CHRONOS_PERSISTENCE_KEY", "sentinel-never-read")
    store = _store(tmp_path, binding="closed")
    recorder = _RecordingEnviron(os.environ)
    monkeypatch.setattr(os, "environ", recorder)  # from here on, every lookup is recorded
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0, report
    assert "CHRONOS_PERSISTENCE_KEY" not in recorder.looked_up
    assert drill.IDENTITY_ENV in recorder.looked_up  # the drill's own variables were read
    assert "sentinel-never-read" not in json.dumps(report)


def test_6b_control_the_module_itself_imports_no_broker_orders_or_config_module() -> None:
    """A control: true at the RED stub and after the fix (the rehydration import pulls
    ``chronos.supervisor``, whose own imports reach the demo broker; the module never names
    a broker, orders or config module, and test_5a proves no socket is opened)."""

    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offending = [
        name
        for name in imported
        if name.startswith(("chronos.broker", "chronos.orders", "chronos.config", "chronos.api"))
    ]
    assert offending == [], offending


def test_6c_the_module_only_replays_the_position_ledger_and_never_writes_it() -> None:
    """The safety contract admits this module as the second importer of
    ``position_management`` by name (tests/safety/test_paper_position_management.py): it may
    only REPLAY (rehydrate) a binding's stream to prove the position closed — never register,
    evaluate, resolve, or append to the chain."""

    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "chronos.supervisor.position_management"
        ):
            names += [alias.name for alias in node.names]
    assert sorted(names) == ["PositionManagementError", "rehydrate_position"], names
    source = MODULE.read_text(encoding="utf-8")
    for writer in (
        "register_position",
        "evaluate_position",
        "record_directive_resolution",
        "hash_chain.append",
        "session.add(",
        "session.commit(",
        "session.execute(",
    ):
        assert writer not in source, writer


# ------------------------------------------------------------- (7) the runbook


def test_7a_the_runbook_names_the_steps_and_carries_no_example_key() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "python -m chronos.operations.cutover_archive",
        "--apply",
        '"verdict": "ARCHIVED"',
        "secrets.token_hex(32)",
        "CHRONOS_PERSISTENCE_KEY",
        "0600",
        "escrow",
        "90 days",
        "systemctl --user is-active",
        "Rollback",
    ):
        assert needle in text, needle
    assert re.search(r"[0-9a-f]{64}", text) is None, "a 64-hex string reads as an example key"
    assert "ALLOW_ORDER_TRANSMIT=true" not in text and "ALLOW_LIVE_TRADING=true" not in text


ESCROW_MODULE = ROOT / "src" / "chronos" / "operations" / "persistence_key_escrow.py"


def _escrow(
    tmp_path: Path, key_text: str, *extra: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir(exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        "chronos.operations.persistence_key_escrow",
        "--out",
        str(out),
        "--key-from-stdin",
        *extra,
    ]
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env={**os.environ, **SAFE_ENV},
        input=key_text,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, out


def test_7c_the_escrow_tool_encrypts_the_key_it_is_given_and_proves_it_decrypts_back(
    tmp_path: Path, age_keys: KeyMaterial
) -> None:
    """P1-3: the escrow step is a pinned tool. A TEST key (derived at run time) goes in on
    stdin, never through the environment or argv; the tool encrypts it to the drill's two
    validated recipients, decrypts the artifact back through the host identity in a private
    scratch, compares in process, and reports — never printing the key or a digest of it."""

    key = _hex("n3-2 r1 TEST persistence key — never a real one")
    completed, out = _escrow(tmp_path, key + "\n")
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["verdict"] == "ESCROWED" and report["verified"] is True
    assert report["out"] == str(out) and out.is_file()
    assert sorted(report["recipients"]) == sorted(
        [age_keys.host_public_key, drill.KEVIN_RECOVERY_RECIPIENT]
    )
    assert report["ciphertext_sha256"] == _sha256(out)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert key not in completed.stdout + completed.stderr
    without_digest = completed.stdout.replace(report["ciphertext_sha256"], "")
    assert re.search(r"[0-9a-f]{64}", without_digest) is None  # only the ciphertext digest
    assert AGE is not None
    decrypted = subprocess.run(
        [AGE, "-d", "-i", str(age_keys.identity), str(out)], check=True, capture_output=True
    )
    assert decrypted.stdout == (key + "\n").encode()
    assert not list((tmp_path / "escrow").glob(".chronos-escrow-*"))  # the scratch is gone


@pytest.mark.parametrize(
    "shape", ["empty", "newline-only", "63-hex", "65-hex", "uppercase", "not-hex", "two-lines"]
)
def test_7d_the_escrow_tool_fails_closed_on_anything_but_one_64_lowercase_hex_line(
    tmp_path: Path, shape: str
) -> None:
    """P1-3: an unset variable expands to nothing — here nothing is written and the exit is 2."""

    good = _hex("n3-2 r1 TEST key for the refusals")
    text = {
        "empty": "",
        "newline-only": "\n",
        "63-hex": good[:63] + "\n",
        "65-hex": good + "0\n",
        "uppercase": good.upper() + "\n",
        "not-hex": "g" + good[1:] + "\n",
        "two-lines": good + "\n" + good + "\n",
    }[shape]
    completed, out = _escrow(tmp_path, text)
    assert completed.returncode == 2, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["verdict"] == "REFUSED" and report["code"] == "KEY_MALFORMED"
    assert not out.exists() and _entries(out.parent) == []
    assert good not in completed.stdout + completed.stderr


def test_7e_the_escrow_tool_never_reads_the_key_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The root cause of P1-3 was an environment expansion; the tool has no such path: its
    source never names the variable or the environment, and a recorder proves no lookup."""

    from chronos.operations import persistence_key_escrow as escrow

    tree = ast.parse(ESCROW_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            identifier = node.id if isinstance(node, ast.Name) else node.attr
            assert "PERSISTENCE" not in identifier.upper(), ast.unparse(node)
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv"):
            raise AssertionError(f"the tool touches the environment: {ast.unparse(node)}")
    monkeypatch.setenv("CHRONOS_PERSISTENCE_KEY", "sentinel-never-read")
    recorder = _RecordingEnviron(os.environ)
    monkeypatch.setattr(os, "environ", recorder)
    out = tmp_path / "chronos-persistence-key.age"
    monkeypatch.setattr(
        sys, "stdin", __import__("io").StringIO(_hex("n3-2 r1 TEST key, in-process") + "\n")
    )
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    assert code == 0, capsys.readouterr()
    assert "CHRONOS_PERSISTENCE_KEY" not in recorder.looked_up
    assert drill.IDENTITY_ENV in recorder.looked_up
    assert "sentinel-never-read" not in capsys.readouterr().out


def test_7f_the_runbook_states_the_lost_key_boundary_and_promises_no_refusing_command() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    rollback = text[text.index("## Rollback") :]
    assert "runs again from step 1" not in rollback
    assert "SCHEMA_ALREADY_CURRENT" in rollback  # the command refuses the keyed store, said plainly
    assert "the escrow copy is the only supported recovery" in rollback
    assert "separately reviewed" in rollback
    # P1-3: step 6 drives the tool, never a shell expansion of the variable
    step6 = text[text.index("6. **Escrow") : text.index("7. **")]
    assert "python -m chronos.operations.persistence_key_escrow" in step6
    assert '"$CHRONOS_PERSISTENCE_KEY"' not in step6 and "printf" not in step6
    assert "decrypt" in step6 and "compare" in step6


@pytest.mark.parametrize(
    "shape",
    [
        "swap-then-decrypt-fails",
        "swap-then-decrypt-succeeds",
        "decrypt-fails",
    ],
)
def test_7g_escrow_failure_cleanup_deletes_nothing_and_names_the_file_this_run_created(
    tmp_path: Path,
    age_keys: KeyMaterial,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    """Daybreak P1-3 (r1, r2): after the artifact is created, its name is swapped (the created
    file renamed aside, a foreign file planted at the name) and/or the decrypt-and-compare fails.
    The tool verifies through the descriptor it holds, proves the name still resolves to it
    before success, and after a post-create failure DELETES NOTHING (r3: Linux has no
    unlink-by-descriptor, so any unlink is by name and can hit a swapped-in foreign entry): the
    foreign entry survives, the created ciphertext stays — at its name, or under the renamed
    entry — and the report names it by device:inode for the owner to shred."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    aside = out.with_name("artifact-aside")
    foreign = b"not the file this run created"
    real_decrypt = escrow._decrypts_back_to

    def hooked(*args: Any, **kwargs: Any) -> None:
        monkeypatch.setattr(escrow, "_decrypts_back_to", real_decrypt)
        if shape.startswith("swap"):
            os.rename(out, aside)
            out.write_bytes(foreign)
        if shape.endswith("fails"):
            raise escrow.EscrowRefused("AGE_FAILED", "forced: the decrypt step failed")
        real_decrypt(*args, **kwargs)

    monkeypatch.setattr(escrow, "_decrypts_back_to", hooked)
    key = _hex(f"n3-2 r2 TEST key for {shape} — never a real one")
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["verdict"] == "REFUSED", report
    expected_code = "TARGET_SWAPPED" if shape == "swap-then-decrypt-succeeds" else "AGE_FAILED"
    assert report["code"] == expected_code, report  # succeeds: the decrypt read the HELD inode
    created = report["created_artifact"]
    assert created["path"] == str(out)
    assert created["removed"] is False and "shred" in created["note"]  # the tool deletes nothing
    if shape.startswith("swap"):
        assert out.read_bytes() == foreign  # the foreign entry survives
        assert _entries(out.parent) == ["artifact-aside", out.name]
        assert "renamed entry" in created["note"]
        ciphertext = aside
    else:
        assert _entries(out.parent) == [out.name]  # the created file stays at its name
        ciphertext = out
    assert created["identity"] == "{}:{}".format(*_identity(ciphertext))
    assert AGE is not None
    decrypted = subprocess.run(
        [AGE, "-d", "-i", str(age_keys.identity), str(ciphertext)], check=True, capture_output=True
    )
    assert decrypted.stdout == (key + "\n").encode()  # the created ciphertext, intact
    assert key not in json.dumps(report)


def test_7h_the_runbook_states_the_r2_boundaries() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    step6 = text[text.index("6. **Escrow") : text.index("7. **")]
    assert "TARGET_SWAPPED" in step6 and "renamed entry" in step6
    before = text[text.index("## Before you start") : text.index("## Steps")]
    assert "lease" in before and "owner" in before  # the kernel lease needs the store's owner
    step3 = text[text.index("3. **Apply") : text.index("4. **Remove")]
    assert "foreign_renamed" in step3
    table = text[text.index("## What the gate's refusal codes mean") :]
    assert "`WRITER_OPEN`" in table and "`WRITER_EXCLUSION_UNAVAILABLE`" in table


def test_7j_the_runbook_states_the_r3_boundaries() -> None:
    """r3: the sentinel directories, the owner's rmdir as step 7's first line, the rollback order
    (rmdir, then mv -n), the new refusal row; the escrow step says the tool deletes nothing."""

    text = RUNBOOK.read_text(encoding="utf-8")
    step3 = text[text.index("3. **Apply") : text.index("4. **Remove")]
    assert "sentinel" in step3 and "rmdir" in step3 and "left_in_place" not in step3
    assert "LIVE_NAME_OCCUPIED" in step3
    step7 = text[text.index("7. **") : text.index("8. **Bind")]
    assert (
        "rmdir data/chronos.db data/chronos.db-wal data/chronos.db-shm data/chronos.db-journal"
        in step7
    )
    assert "non-regular" in step7  # what every writer says until then
    rollback = text[text.index("## Rollback") :]
    assert rollback.index("rmdir") < rollback.index("mv -n")
    table = text[text.index("## What the gate's refusal codes mean") :]
    assert "`LIVE_NAME_OCCUPIED`" in table


def test_7k_the_runbook_says_the_escrow_tool_deletes_nothing_after_a_failure() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    step6 = text[text.index("6. **Escrow") : text.index("7. **")]
    assert "deletes nothing" in step6 and "shred" in step6 and "removed: true" not in step6


def test_7b_the_ops_readme_indexes_the_runbook() -> None:
    assert "`N3-CUTOVER.md`" in OPS_README.read_text(encoding="utf-8")


# ------------------------------------------------------------- (8) G1: the drill's archived head


def _drill_store(tmp_path: Path) -> Store:
    return _store(tmp_path, binding="closed")


def _archive_drill(store: Store, tmp_path: Path, tag: str = "") -> tuple[Any, Any]:
    """The r3 archive path: the drill opens nothing by itself here — it is handed the bound
    capability and runs the REAL flatten gate over it inside its own frame; the admission is a
    value local to that frame. Returns ``(gate, report)``; the caller's capability is closed."""

    source = drill.open_bound_source(store.db)
    try:
        return drill.run_archive_drill(
            source,
            tmp_path / f"out{tag}",
            tmp_path / f"restored{tag}",
            now=store.now,
            clock=_clock(store.now),
        )
    finally:
        source.close()


#: a parameter by any of these names would let a caller select or carry an acceptance (r4)
ACCEPTANCE_PARAMETERS = frozenset(
    {
        "admission",
        "archive",
        "binding",
        "archived_schema_head",
        "acceptance",
        "accepted",
        "verifier",
    }
)


def _module_functions(module: Any) -> list[tuple[str, Any]]:
    """Every function defined at module level in ``module`` — underscore-prefixed included; a
    leading underscore is a convention, not an authority boundary (Daybreak r3)."""

    return sorted(
        (name, member)
        for name, member in inspect.getmembers(module, inspect.isfunction)
        if member.__module__ == module.__name__
    )


def _verification_offences(result: Any) -> list[str]:
    """What, in a call's result, would amount to verifying a store as an archive: a
    ``RestoreReport`` that is verified or carries an ``archive:`` acceptance, a ``DrillReport``
    with ``ARCHIVE_VERIFIED``, or any ``acceptance`` value starting with ``archive:`` in a dict,
    tuple or list — recursively."""

    found: list[str] = []
    if isinstance(result, drill.RestoreReport):
        if result.verified or result.acceptance.startswith("archive:"):
            found.append(f"RestoreReport verified={result.verified} {result.acceptance}")
    elif isinstance(result, drill.DrillReport):
        if result.verdict == drill.ARCHIVE_VERIFIED:
            found.append("DrillReport ARCHIVE_VERIFIED")
        found += _verification_offences(result.restore)
    elif isinstance(result, dict):
        acceptance = result.get("acceptance")
        if isinstance(acceptance, str) and acceptance.startswith("archive:"):
            found.append(f"acceptance {acceptance}")
    elif isinstance(result, tuple | list):
        for item in result:
            found += _verification_offences(item)
    return found


def _gate_claim(source: Any, version: int) -> Any:
    """A GateReport a substituted gate could return (test-only: the real gate never lies)."""

    return cutover.GateReport(
        schema_version=version,
        schema_head=f"schema_version:{version}",
        source_identity=source.identity,
        writer_lease={},
        wal={},
        bindings={},
        reconciliation={},
        positions_observed=0,
        checks_passed=(),
    )


def test_8a_the_archive_drill_runs_the_gate_itself_and_verifies_a_retired_store_by_that_run(
    tmp_path: Path,
) -> None:
    store = _drill_store(tmp_path)
    head = drill.schema_head(store.db)
    assert head == f"schema_version:{SCHEMA_VERSION - 1}"
    source_identity = _identity(store.db)
    gate, report = _archive_drill(store, tmp_path)
    assert (gate.schema_head, gate.schema_version) == (head, SCHEMA_VERSION - 1)
    assert gate.source_identity == source_identity
    assert report.verdict == "ARCHIVE_VERIFIED", report.failures  # never repository VERIFIED
    assert report.restore is not None
    assert report.restore.acceptance == f"archive:{head}"
    restored = report.restore
    assert restored.schema_head_ok and restored.sha256_ok and restored.row_counts_ok
    assert tuple(report.manifest.source_identity) == source_identity


def test_8b_control_without_the_seam_the_drill_refuses_the_retired_schema(
    tmp_path: Path,
) -> None:
    """The gap G1 is real at this base: a store one version behind the running code can never
    be VERIFIED by the drill as shipped, because the repository's acceptance refuses it."""

    store = _drill_store(tmp_path)
    report = run_drill(store.db, tmp_path / "out", tmp_path / "restored", clock=_clock(store.now))
    assert report.verdict == "FAILED"
    prefix = "schema_acceptance: Unsupported Chronos schema version"
    assert any(f.startswith(prefix) for f in report.failures), report.failures


def test_8c_an_admitted_head_that_is_not_the_copy_s_head_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drill checks the admission it built: a gate run that returned another retired head
    than the copy really has (a test-only substitution of the real gate — no library seam) verifies
    as nothing; the exact head is compared on the restored copy."""

    store = _drill_store(tmp_path)
    lie = SCHEMA_VERSION - 2
    real_gate = cutover.flatten_gate

    def lying_gate(source: Any, *, now: datetime) -> Any:
        truth = real_gate(source, now=now)
        return replace(truth, schema_head=f"schema_version:{lie}", schema_version=lie)

    monkeypatch.setattr(cutover, "flatten_gate", lying_gate)
    gate, report = _archive_drill(store, tmp_path)
    assert gate.schema_version == lie
    assert report.verdict == "FAILED"
    assert any(f.startswith("schema_head:") for f in report.failures), report.failures
    assert (
        report.restore is not None and report.restore.acceptance == f"archive:schema_version:{lie}"
    )


def test_8e_daybreak_s_counterexample_a_store_at_the_running_version_is_never_an_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2 (r1): a store AT the running SCHEMA_VERSION. The archive drill's path IS the gate's:
    the real gate refuses (SCHEMA_ALREADY_CURRENT) and nothing is created; a substituted gate that
    claims the running version is refused by the drill's own version bound; one that lies the
    version DOWN is caught by the exact head on the copy."""

    store = _store(tmp_path, version_offset=0)
    source = drill.open_bound_source(store.db)
    try:
        with pytest.raises(cutover.CutoverRefused) as refused:
            drill.run_archive_drill(
                source,
                tmp_path / "out",
                tmp_path / "restored",
                now=store.now,
                clock=_clock(store.now),
            )
        assert refused.value.code == "SCHEMA_ALREADY_CURRENT"
        assert not (tmp_path / "out").exists() and not (tmp_path / "restored").exists()
        monkeypatch.setattr(
            cutover, "flatten_gate", lambda s, *, now: _gate_claim(s, SCHEMA_VERSION)
        )
        with pytest.raises(drill.DrillRefused, match="not a retired schema"):
            drill.run_archive_drill(
                source,
                tmp_path / "out",
                tmp_path / "restored",
                now=store.now,
                clock=_clock(store.now),
            )
        assert not (tmp_path / "out").exists()
        monkeypatch.setattr(
            cutover, "flatten_gate", lambda s, *, now: _gate_claim(s, SCHEMA_VERSION - 1)
        )
        gate, report = drill.run_archive_drill(
            source, tmp_path / "out", tmp_path / "restored", now=store.now, clock=_clock(store.now)
        )
    finally:
        source.close()
    assert gate.schema_version == SCHEMA_VERSION - 1
    assert report.verdict == "FAILED", report.verdict
    assert any(f.startswith("schema_head:") for f in report.failures), report.failures
    assert report.restore is not None and not report.restore.verified


def test_8f_an_envelope_of_another_object_is_not_verified_as_the_admitted_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _drill_store(tmp_path)
    other = _identity(store.db.parent)  # a real inode that is not the store
    real_backup = drill.backup

    def lying_backup(*args: Any, **kwargs: Any) -> Any:
        return replace(real_backup(*args, **kwargs), source_identity=other)

    monkeypatch.setattr(drill, "backup", lying_backup)
    _gate, report = _archive_drill(store, tmp_path)
    assert report.verdict == "FAILED"
    assert any("source identity" in f for f in report.failures), report.failures
    assert report.restore is not None and report.restore.acceptance.startswith("archive:")


def test_8g_the_bare_string_seam_the_public_binding_and_the_capability_mint_are_gone() -> None:
    for function in (drill.run_drill, drill.restore, drill.verify_restored):
        parameters = inspect.signature(function).parameters
        assert "archived_schema_head" not in parameters, function
        assert "archive" not in parameters, function
    assert not hasattr(drill, "ArchiveBinding")
    source = MODULE.read_text(encoding="utf-8")
    assert "archived_schema_head" not in source and "ArchiveBinding" not in source
    # r3: no admission state or method on the capability; only run_drill still takes a source
    # (for the backup's identity binding) and it can yield VERIFIED or FAILED alone
    assert not hasattr(drill.BoundSource, "_admit_archive")
    assert not hasattr(drill.BoundSource, "archive_admission")
    assert "_archive" not in drill.BoundSource.__slots__
    for function in (drill.restore, drill.verify_restored):
        assert "source" not in inspect.signature(function).parameters, function
    assert "admission" not in inspect.signature(drill.run_archive_drill).parameters
    assert "_admit_archive" not in source and "archive_admission" not in source
    # r4: no module-level admission value, and no module-level consumer of one
    for gone in ("_ArchiveAdmission", "_restore_verified", "_admission_from"):
        assert not hasattr(drill, gone), gone
    assert "admission" not in inspect.signature(drill._verify_in).parameters


def test_8h_daybreak_s_counterexample_every_seam_on_a_refused_store_is_tried_and_none_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P1-2 (r1, r2): a retired store WITHOUT a reconciliation run. The command and the gate refuse
    it. Then every callable seam is actively tried on the refused capability — the r2 method, an
    attribute set, ``object.__setattr__``, a ``__class__`` swap to a subclass that carries a forged
    admission, a copy, a pickle — and the archive drill itself over the refused store; the public
    surface is swept for any parameter that admits an archive. Nothing reaches ARCHIVE_VERIFIED:
    the only producer runs the real gate in its own frame, and the gate refuses."""

    store = _store(tmp_path, run=None)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "NO_RECONCILIATION_RUN", report
    assert not (tmp_path / "backups").exists()
    head = drill.schema_head(store.db)
    source = drill.open_bound_source(store.db)
    drilled: list[Any] = []
    try:
        with pytest.raises(cutover.CutoverRefused) as refused:
            cutover.flatten_gate(source, now=store.now)
        assert refused.value.code == "NO_RECONCILIATION_RUN"
        forged = object()  # any value: nothing on the capability may accept one
        # (1) the r2 mint is gone; (2) no attribute on the capability accepts an admission
        with pytest.raises(AttributeError):
            source._admit_archive(head, SCHEMA_VERSION - 1)
        for attribute in ("archive_admission", "_archive", "admission"):
            with pytest.raises(AttributeError):
                setattr(source, attribute, forged)
            with pytest.raises(AttributeError):
                object.__setattr__(source, attribute, forged)
            assert not hasattr(source, attribute)
        # (3) a subclass that carries the forged value under every r2 name: never read
        with pytest.raises(cutover.CutoverRefused):  # the archive drill: the gate's own refusal
            drill.run_archive_drill(
                source,
                tmp_path / "out-c",
                tmp_path / "restored-c",
                now=store.now,
                clock=_clock(store.now),
            )
        assert not (tmp_path / "out-c").exists()

        class Forging(drill.BoundSource):  # type: ignore[misc]
            __slots__ = ()
            archive_admission = forged
            _archive = forged

            def _admit_archive(self, *args: object) -> None:
                return None

        source.__class__ = Forging
        assert (
            source.archive_admission is forged
        )  # the forgery is in place, for the drill to ignore
        drilled.append(
            run_drill(
                store.db,
                tmp_path / "out-a",
                tmp_path / "restored-a",
                clock=_clock(store.now),
                source=source,
            )
        )
        with pytest.raises(cutover.CutoverRefused):
            drill.run_archive_drill(
                source,
                tmp_path / "out-d",
                tmp_path / "restored-d",
                now=store.now,
                clock=_clock(store.now),
            )
        # (4) a copy carries nothing either; a pickle is impossible (the connection, the descriptor)
        copied = copy.copy(source)  # shares the descriptor and connection; never closed here
        drilled.append(
            run_drill(
                store.db,
                tmp_path / "out-b",
                tmp_path / "restored-b",
                clock=_clock(store.now),
                source=copied,
            )
        )
        with pytest.raises((TypeError, pickle.PicklingError, AttributeError)):
            pickle.dumps(source)
        # (5) the whole module surface, underscore names included (r4): no function anywhere
        # takes a parameter that selects or carries an archive acceptance
        for name, member in _module_functions(drill):
            parameters = set(inspect.signature(member).parameters)
            assert not parameters & ACCEPTANCE_PARAMETERS, (name, parameters)
        assert not hasattr(drill, "ArchiveBinding")
        assert not hasattr(drill, "_ArchiveAdmission")
        # (6) the plain drill over the store: the repository's own refusal
        drilled.append(
            run_drill(
                store.db, tmp_path / "out-e", tmp_path / "restored-e", clock=_clock(store.now)
            )
        )
    finally:
        source.close()
    prefix = "schema_acceptance: Unsupported Chronos schema version"
    for result in drilled:
        assert result.verdict == "FAILED", result.verdict
        assert result.restore is not None and result.restore.acceptance == "repository"
        assert any(f.startswith(prefix) for f in result.failures), result.failures


def test_8i_every_module_level_callable_is_tried_on_a_refused_store_and_none_verifies_it(
    tmp_path: Path,
) -> None:
    """Daybreak r3 P1-2, the exact counterexample and its generalisation. A retired store the gate
    refuses (no reconciliation run) gets an envelope through the PUBLIC ``backup``; then the
    module is plain-imported and EVERY module-level function — underscore names included — is
    called with arguments bound by parameter name from a pool built around that envelope (fresh
    target paths per call; a function whose required parameters the pool cannot supply is listed
    and swept by signature only). None may return a verified report, an ``ARCHIVE_VERIFIED``
    verdict, or any ``acceptance`` value that starts with ``archive:``; ``_ArchiveAdmission`` must
    not exist. (Constructing a ``RestoreReport`` by hand is forging, not verifying — the sweep is
    over functions, which is where verification happens.)"""

    import importlib

    module = importlib.import_module("chronos.operations.restore_drill")
    assert module is drill and not hasattr(module, "_ArchiveAdmission")
    store = _store(tmp_path, run=None)
    source = drill.open_bound_source(store.db)
    opened: list[int] = []
    try:
        with pytest.raises(cutover.CutoverRefused) as refused:
            cutover.flatten_gate(source, now=store.now)
        assert refused.value.code == "NO_RECONCILIATION_RUN"
        head = drill.schema_head(store.db)
        manifest = drill.backup(store.db, tmp_path / "out", clock=_clock(store.now), source=source)
        envelope = Path(manifest.backup_path).parent
        restored = drill.restore(manifest, tmp_path / "restored-once")
        assert not restored.verified and restored.acceptance == "repository"
        counter = itertools.count()

        def fresh(prefix: str) -> Path:
            return tmp_path / f"{prefix}-{next(counter)}"

        def fd_of(path: Path, flags: int = os.O_RDONLY) -> int:
            fd = os.open(path, flags)
            opened.append(fd)
            return fd

        def pool() -> dict[str, Any]:
            target = Path(restored.target_dir)
            return {
                "manifest": manifest,
                "source": source,
                "clock": _clock(store.now),
                "keys": None,
                "now": store.now,
                "db_path": store.db,
                "path": store.db,
                "named": store.db.parent,
                "directory": store.db.parent,
                "target": fresh("target"),
                "target_dir": fresh("target"),
                "restore_into": fresh("restored"),
                "restored": target / drill.DB_NAME,
                "restored_audit_log": target / drill.AUDIT_LOG_NAME,
                "out_dir": fresh("out"),
                "target_fd": fd_of(target, os.O_RDONLY | os.O_DIRECTORY),
                "env_fd": fd_of(envelope, os.O_RDONLY | os.O_DIRECTORY),
                "out_fd": fd_of(tmp_path / "out", os.O_RDONLY | os.O_DIRECTORY),
                "dfd": fd_of(target, os.O_RDONLY | os.O_DIRECTORY),
                "fd": fd_of(target / drill.DB_NAME),
                "db_fd": fd_of(target / drill.DB_NAME),
                "src_fd": fd_of(target / drill.DB_NAME),
                "target_abs": target,
                "parent_abs": target,
                "has_audit": True,
                "schema_head": head,
                "head": head,
                "restored_head": head,
                "schema_version": SCHEMA_VERSION - 1,
                "version": SCHEMA_VERSION - 1,
                "identity": source.identity,
                "what": "sweep",
                # no "subject": _lease_read(fd, subject) on a pool descriptor would make a
                # later writable open wait out the kernel's lease break; the openers that take
                # one are swept by signature
                "name": drill.DB_NAME,
                "db_name": drill.DB_NAME,
                "encrypted_name": Path(manifest.backup_path).name,
                "temp_name": f".{fresh('temp').name}",
                "final_name": fresh("final").name,
                "temp": fresh("temp"),
                "final": fresh("final"),
                "stamp": store.stamp,
                "argv": ["--help"],
                "create": False,
                "payload": b"",
                "raw": None,
                "value": None,
                "data": {},
                "identity_path": None,
                "anchor_path": store.db.parent / drill.AUDIT_ANCHOR_NAME,
                "connection": source.connection,
                "audit_text": None,
                "table_name": "schema_version",
                "column_name": "version",
                "identifier": "schema_version",
                "error": OSError(1, "sweep"),
                "component": "x",
                "mode": 0o600,
                "flags": os.O_RDONLY,
            }

        attempted: list[str] = []
        unattempted: list[str] = []
        offences: list[str] = []
        for name, function in _module_functions(drill):
            signature = inspect.signature(function)
            assert not set(signature.parameters) & ACCEPTANCE_PARAMETERS, (name, signature)
            values = pool()
            kwargs: dict[str, Any] = {}
            missing = []
            for parameter in signature.parameters.values():
                if (
                    parameter.kind is parameter.VAR_POSITIONAL
                    or parameter.kind is parameter.VAR_KEYWORD
                ):
                    continue
                if parameter.name in values:
                    kwargs[parameter.name] = values[parameter.name]
                elif parameter.default is parameter.empty:
                    missing.append(parameter.name)
            if missing:
                unattempted.append(f"{name}({', '.join(missing)})")
                continue
            attempted.append(name)
            try:
                result = function(**kwargs)
            except BaseException as error:  # a refusal or an error is not a verification
                if isinstance(error, KeyboardInterrupt):
                    raise
                continue
            for offence in _verification_offences(result):
                offences.append(f"{name}: {offence}")
        assert offences == [], offences
        for must in ("restore", "verify_restored", "run_drill", "run_archive_drill", "_verify_in"):
            assert must in attempted, (must, unattempted)
        # the ones the pool could not drive are listed by signature: a reviewer can extend it
        assert all("(" in entry for entry in unattempted), unattempted
    finally:
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)
        source.close()


def test_8j_a_hostile_clock_walking_the_frames_retains_no_verifier_for_a_mutated_store(
    tmp_path: Path,
) -> None:
    """Daybreak r4 P1-2, the exact attack: the caller supplies the ``Clock``; every callback walks
    ``f_back`` to ``run_archive_drill``'s frame and retains everything in its locals. One valid
    archive drill runs. Then the store is mutated after the gate (``reconciliation_runs`` emptied)
    so that the real gate refuses it, its envelope is made through the public ``backup`` and
    staged with the module's repository-only staging — and every retained callable is applied to
    it in the shapes a verifier could take. r5: nothing callable is defined inside the drill's
    frame (no nested verifier exists to retain), so no retained object archive-verifies the
    mutated store; what the frame holds are plain values. (Same-process code is trusted — it
    could replace ``flatten_gate`` itself; the guarantee is only that no verifier object outlives
    or escapes the drill.)"""

    store = _drill_store(tmp_path)
    retained: dict[str, Any] = {}

    def harvest() -> None:
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "run_archive_drill":
                for name, value in frame.f_locals.items():
                    retained.setdefault(name, value)
            frame = frame.f_back

    ticks = itertools.count(0.0, 0.25)

    def wall() -> datetime:
        harvest()
        return store.now

    def monotonic() -> float:
        harvest()
        return next(ticks)

    source = drill.open_bound_source(store.db)
    try:
        _gate, report = drill.run_archive_drill(
            source,
            tmp_path / "out",
            tmp_path / "restored",
            now=store.now,
            clock=Clock(wall=wall, monotonic=monotonic),
        )
    finally:
        source.close()
    assert report.verdict == "ARCHIVE_VERIFIED" and retained, (report.verdict, retained.keys())
    retained_callables = {name: value for name, value in retained.items() if callable(value)}
    nested = [
        name
        for name, value in retained_callables.items()
        if getattr(value, "__qualname__", "").startswith("run_archive_drill.<locals>")
    ]
    assert nested == [], nested  # structurally: nothing defined inside the frame was retained
    # the store is mutated after the gate: the real gate refuses it now
    with closing(sqlite3.connect(store.db)) as connection, connection:
        connection.execute("DELETE FROM reconciliation_runs")
    _checkpoint_and_drop_sidecars(store.db)
    mutated = drill.open_bound_source(store.db)
    try:
        with pytest.raises(cutover.CutoverRefused) as refused:
            cutover.flatten_gate(mutated, now=store.now)
        assert refused.value.code == "NO_RECONCILIATION_RUN"
        manifest = drill.backup(
            store.db, tmp_path / "out-b", clock=_clock(store.now), source=mutated
        )
        later = drill.restore(manifest, tmp_path / "restored-b")
        assert not later.verified and later.acceptance == "repository"
        offences: list[str] = []
        keys = drill.load_age_keys()
        with drill._staged_restore(manifest, tmp_path / "restored-c", keys) as staged:
            shapes: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
                ((manifest, staged.target_fd, staged.target_abs, staged.has_audit), {}),
                ((manifest, drill.schema_head(store.db)), {}),
                ((manifest,), {}),
                ((mutated,), {"now": store.now}),
                ((), {}),
            ]
            for name, value in retained_callables.items():
                for args, kwargs in shapes:
                    try:
                        result = value(*args, **kwargs)
                    except BaseException as error:  # a refusal or an error is not a verification
                        if isinstance(error, KeyboardInterrupt):
                            raise
                        continue
                    offences += [f"{name}: {o}" for o in _verification_offences(result)]
        assert offences == [], offences
    finally:
        mutated.close()


def test_8k_run_archive_drill_defines_no_nested_callable() -> None:
    """The structural half of 8j: the gate-bound archive checks and the report construction are
    inlined in ``run_archive_drill``'s body — no function, lambda or class is defined inside it
    (by AST, and by the compiled code object's constants), so there is no verifier object to
    retain through any callback."""

    module = ROOT / "src" / "chronos" / "operations" / "restore_drill.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_archive_drill"
    ]
    assert len(functions) == 1
    nested = [
        type(node).__name__
        for node in ast.walk(functions[0])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef)
        and node is not functions[0]
    ]
    assert nested == [], nested
    code_constants = [
        c for c in drill.run_archive_drill.__code__.co_consts if isinstance(c, types.CodeType)
    ]
    assert code_constants == [], [c.co_name for c in code_constants]
    source = module.read_text(encoding="utf-8")
    assert "same Python process" in source and "outlives" in source  # the trust root, stated


def test_8d_the_restore_report_defaults_to_repository_acceptance(tmp_path: Path) -> None:
    store = _store(tmp_path, version_offset=0)  # a store AT the running version: accepted as before
    report = run_drill(store.db, tmp_path / "out", tmp_path / "restored", clock=_clock(store.now))
    assert report.verdict == "VERIFIED", report.failures
    assert report.restore is not None and report.restore.acceptance == "repository"
    assert report.restore.to_dict()["acceptance"] == "repository"


# ------------------------------------------------------------- (9) r1: one bound source capability


def _swap_at_next_connect(monkeypatch: pytest.MonkeyPatch, victim: Path, impostor: Path) -> None:
    """Daybreak's substitution, placed at the seam that matters: the FIRST pathname open after
    the command has recorded the victim's inode. ``victim`` is renamed away and ``impostor`` is
    put at its name, then the real connect runs; the hook removes itself."""

    real = sqlite3.connect

    def swapping(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        monkeypatch.setattr(sqlite3, "connect", real)
        os.rename(victim, victim.with_name("victim-away"))
        os.rename(impostor, victim)
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", swapping)


def test_9a_daybreak_s_a_b_substitution_is_refused_before_any_fact_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Store A cannot pass the gate (no reconciliation run); store B can. B is put at A's name
    between the no-follow open and the read-only connect: the connection is proven NOT to be the
    held inode and the command refuses SOURCE_SWAPPED with nothing read and nothing created."""

    a = _store(tmp_path / "a", run=None)
    b = _store(tmp_path / "b", binding="closed")
    _swap_at_next_connect(monkeypatch, a.db, b.db)
    code, report = _run(a, tmp_path / "a", "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "SOURCE_SWAPPED", report
    assert report["gate"] is None  # no fact of B's was ever reported as A's
    assert not (tmp_path / "a" / "backups").exists()
    assert a.db.exists() and a.db.with_name("victim-away").exists()  # both stores untouched


def test_9b_a_main_file_swap_after_the_gate_cannot_reach_the_envelope_or_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The independent main-file swap: B is put at A's name AFTER the gate admitted A. The
    envelope is still of A (the backup runs through the bound connection, its manifest names A's
    identity) and the move refuses because the name no longer is A; nothing is renamed."""

    a = _store(tmp_path / "a", binding="closed")
    b = _store(tmp_path / "b", binding="closed")
    a_identity, a_sha = _identity(a.db), _sha256(a.db)
    real_backup = drill.backup

    def swapping_backup(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(drill, "backup", real_backup)
        os.rename(a.db, a.db.with_name("victim-away"))
        os.rename(b.db, a.db)
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(drill, "backup", swapping_backup)
    code, report = _run(a, tmp_path / "a", "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "SOURCE_SWAPPED"
    assert report["moved_aside"] == [] and report["not_moved"] == []
    envelope = Path(report["envelope"])
    manifest = BackupManifest.from_dict(
        json.loads((envelope / drill.MANIFEST_NAME).read_text(encoding="utf-8"))
    )
    assert tuple(manifest.source_identity) == a_identity  # the envelope is of A, not of B
    assert manifest.sha256 != _sha256(a.db)  # B sits at the name; its bytes are not the archive's
    assert _sha256(a.db.with_name("victim-away")) == a_sha
    assert not any(name.startswith("chronos.db.archived-") for name in _entries(a.db.parent))


def test_9c_a_sidecar_swapped_or_planted_after_admission_stops_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two independent sidecar attacks, each with nothing renamed: (i) the admitted ``-wal`` is
    replaced by another inode of the same name; (ii) a ``-wal`` with FRAMES is planted after
    admission — it is never selected for rename, and with frames it refuses the move."""

    real_move = cutover._move_aside

    def swap_wal(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(cutover, "_move_aside", real_move)
        wal = store.db.with_name("chronos.db-wal")
        planted = store.db.with_name("planted")
        planted.write_bytes(b"")
        os.rename(planted, wal)  # same name, another inode
        return real_move(*args, **kwargs)

    store = _store(tmp_path / "swap", binding="closed")
    before = _sha256(store.db)
    monkeypatch.setattr(cutover, "_move_aside", swap_wal)
    code, report = _run(store, tmp_path / "swap", "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "SOURCE_SWAPPED", report
    assert report["moved_aside"] == [] and _sha256(store.db) == before
    assert not any(".archived-" in name for name in _entries(store.db.parent))

    def plant_frames(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(cutover, "_move_aside", real_move)
        wal = planted_store.db.with_name("chronos.db-wal")
        wal.unlink()  # the admitted empty one goes away; a foreign one with frames appears
        wal.write_bytes(b"x" * 64)
        return real_move(*args, **kwargs)

    planted_store = _store(tmp_path / "plant", binding="closed")
    before = _sha256(planted_store.db)
    monkeypatch.setattr(cutover, "_move_aside", plant_frames)
    code, report = _run(planted_store, tmp_path / "plant", "--apply", capsys=capsys)
    assert code == 2 and report["code"] in ("SOURCE_SWAPPED", "WAL_FRAMES_PRESENT"), report
    assert report["moved_aside"] == [] and _sha256(planted_store.db) == before
    assert planted_store.db.with_name("chronos.db-wal").read_bytes() == b"x" * 64  # never touched


def test_9d_the_move_compares_the_manifest_s_source_identity_with_the_held_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    real_drill = cutover.run_archive_drill

    def lying_drill(*args: Any, **kwargs: Any) -> Any:
        gate, report = real_drill(*args, **kwargs)
        other = _identity(store.db.parent)
        return gate, replace(report, manifest=replace(report.manifest, source_identity=other))

    monkeypatch.setattr(cutover, "run_archive_drill", lying_drill)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "SOURCE_SWAPPED", report
    assert "manifest" in report["failures"][0]
    assert report["moved_aside"] == [] and _sha256(store.db) == before and store.db.exists()


def test_9e_the_drill_s_own_backup_is_bound_to_its_no_follow_open_and_records_the_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _store(tmp_path / "a", binding="closed")
    b = _store(tmp_path / "b", binding="closed")
    _swap_at_next_connect(monkeypatch, a.db, b.db)
    with pytest.raises(drill.DrillRefused, match="swapped"):
        drill.backup(a.db, tmp_path / "out", clock=_clock(a.now))
    assert not (tmp_path / "out").exists() or _entries(tmp_path / "out") == []
    # the honest path: every manifest now carries the identity of the object it backed up
    os.rename(a.db, b.db)
    os.rename(a.db.with_name("victim-away"), a.db)
    manifest = drill.backup(a.db, tmp_path / "out", clock=_clock(a.now))
    assert tuple(manifest.source_identity) == _identity(a.db)
    assert BackupManifest.from_dict(manifest.to_dict()).source_identity == manifest.source_identity
    legacy = BackupManifest.from_dict({**manifest.to_dict(), "source_identity": None})
    assert legacy.source_identity is None


# ------------------------------------------------- (9, r2) writer exclusion, the move's truth


def _append_frames(wal: Path) -> None:
    """Sixty-four bytes beyond the header into the SAME inode (Daybreak's probe): an open for
    append on the name — never a new file, never a rename."""

    with open(wal, "ab") as handle:
        handle.write(b"\0" * 64)


SIDECARS = ("chronos.db-wal", "chronos.db-shm")


def test_9f_daybreak_s_counterexample_an_admitted_wal_that_gains_frames_in_place_is_never_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The admitted ``-wal`` (held, identity pinned) is written IN PLACE after the drill, just
    before the move: the same inode, so every identity check passes — the move must still refuse
    with nothing renamed, the store's bytes unchanged and the frames left where they were."""

    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    wal = store.db.with_name("chronos.db-wal")
    real_move = cutover._move_aside

    def grow_then_move(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(cutover, "_move_aside", real_move)
        identity = _identity(wal)
        _append_frames(wal)
        assert _identity(wal) == identity  # the same inode, grown
        return real_move(*args, **kwargs)

    monkeypatch.setattr(cutover, "_move_aside", grow_then_move)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] != "ARCHIVED", report
    assert report["code"] == "WAL_FRAMES_PRESENT", report
    assert report["moved_aside"] == [] and report["not_moved"] == []
    assert store.db.exists() and _sha256(store.db) == before
    assert wal.stat().st_size == 64  # never touched
    assert not any(".archived-" in name for name in _entries(store.db.parent))
    assert Path(report["envelope"]).is_dir()


def test_9g_frames_appended_to_the_admitted_wal_during_the_drill_refuse_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    wal = store.db.with_name("chronos.db-wal")
    real_drill = cutover.run_archive_drill

    def grow_then_drill(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(cutover, "run_archive_drill", real_drill)
        _append_frames(wal)
        return real_drill(*args, **kwargs)

    monkeypatch.setattr(cutover, "run_archive_drill", grow_then_drill)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] != "ARCHIVED", report
    assert report["code"] == "WAL_FRAMES_PRESENT", report
    assert report["moved_aside"] == [] and _sha256(store.db) == before and store.db.exists()
    assert wal.stat().st_size == 64


def test_9h_a_process_holding_the_store_open_for_writing_refuses_before_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Writer exclusion (P1-1): the command takes a kernel READ LEASE on the held store. While any
    process has the store open for writing — a writable SQLite connection, idle or not — the lease
    is refused and so is the command, before any fact is read: WRITER_OPEN, gate None, no envelope.
    With nothing holding it open for writing, the same command archives."""

    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    holder = sqlite3.connect(store.db)  # a writable connection: the store is open O_RDWR
    try:
        holder.execute("SELECT count(*) FROM sqlite_master").fetchone()
        code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    finally:
        holder.close()
    assert code == 2 and report["code"] == "WRITER_OPEN", report
    assert report["gate"] is None and report["envelope"] is None
    assert not (tmp_path / "backups").exists() and _sha256(store.db) == before
    assert "open for writing" in report["failures"][0]
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report


def test_9i_a_write_open_during_the_archive_is_refused_by_the_lease_and_stops_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the gate, during the drill, a process tries to open the store for writing (a
    non-blocking open, so the test does not wait out the lease break): the kernel refuses it
    (EAGAIN) because the command holds the read lease — and that attempt breaks the lease, which
    the command reads before the first rename: WRITER_OPEN, nothing moved. On return the command
    has released its leases, so a writer may open the store again."""

    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    real_backup = drill.backup
    attempts: list[str] = []

    def attempt_write_open(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(drill, "backup", real_backup)
        try:
            fd = os.open(store.db, os.O_RDWR | os.O_NONBLOCK)
        except BlockingIOError:
            attempts.append("EAGAIN")
        else:
            os.close(fd)
            attempts.append("OPENED")
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(drill, "backup", attempt_write_open)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert attempts == ["EAGAIN"], attempts  # the lease held: the writer was refused
    assert code == 2 and report["code"] == "WRITER_OPEN", report
    assert report["moved_aside"] == [] and _sha256(store.db) == before and store.db.exists()
    assert not any(".archived-" in name for name in _entries(store.db.parent))
    assert Path(report["envelope"]).is_dir()
    fd = os.open(store.db, os.O_RDWR | os.O_NONBLOCK)  # released on return
    os.close(fd)


@pytest.mark.parametrize(
    "shape",
    [
        "file-at-live-name",
        "dir-over-sentinel",
    ],
)
def test_9j_a_main_file_swap_at_the_final_syscall_is_exchanged_back_and_reported_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    """Daybreak's final-syscall probe: a name is exchanged AFTER every precheck, immediately before
    the syscall. ``file-at-live-name``: a foreign store is put at the live name, so the exchange
    moves it to the archive name. ``dir-over-sentinel`` (r3, owner-uid only): a foreign empty
    directory is renamed over this run's sentinel at the archive name, so the exchange puts the
    store at the archive name and the foreign directory at the live name. Either way the post-proof
    (archive name == held inode AND live name == held sentinel) fails, the exchange is reversed
    EXACTLY (one more exchange, never a rename over anything), and the report is exact: nothing of
    the admitted set moved, every admitted name is in ``not_moved``, the foreign entry is listed
    with where it is now, the held store's identity is named, and the sentinels this run created
    are listed where they are (the command deletes nothing, a sentinel included)."""

    a = _store(tmp_path / "a", binding="closed")
    b = _store(tmp_path / "b", binding="closed")
    a_identity, a_sha = _identity(a.db), _sha256(a.db)
    b_identity, b_sha = _identity(b.db), _sha256(b.db)
    archived = a.db.with_name(f"chronos.db.archived-{a.stamp}")
    foreign_dir = a.db.with_name("foreign-dir")
    real_rename = cutover._renameat2

    def swap_then_rename(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
        if src == "chronos.db":
            monkeypatch.setattr(cutover, "_renameat2", real_rename)
            if shape == "file-at-live-name":
                os.rename(a.db, a.db.with_name("victim-away"))
                os.rename(b.db, a.db)
            else:
                foreign_dir.mkdir()
                os.rename(foreign_dir, archived)  # over the empty sentinel: the owner's uid can
        real_rename(src_dir_fd, src, dst_dir_fd, dst, flags)

    monkeypatch.setattr(cutover, "_renameat2", swap_then_rename)
    code, report = _run(a, tmp_path / "a", "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED", report  # nothing of ours moved
    assert f"{a_identity[0]}:{a_identity[1]}" in report["failures"][0]
    assert report["moved_aside"] == []
    assert report["not_moved"] == [str(a.db), *(str(a.db.with_name(n)) for n in SIDECARS)]
    sentinels = {s["name"]: s for s in report["sentinels"]}
    journal = a.db.with_name("chronos.db-journal")
    wal_archived = a.db.with_name(f"chronos.db-wal.archived-{a.stamp}")
    shm_archived = a.db.with_name(f"chronos.db-shm.archived-{a.stamp}")
    assert sentinels[str(journal)]["at"] == str(journal) and _is_sentinel(journal)
    assert sentinels[str(a.db.with_name("chronos.db-wal"))]["at"] == str(wal_archived)
    assert sentinels[str(a.db.with_name("chronos.db-shm"))]["at"] == str(shm_archived)
    assert _is_sentinel(wal_archived) and _is_sentinel(shm_archived)
    if shape == "file-at-live-name":
        assert report["code"] == "SOURCE_SWAPPED"
        assert report["foreign_renamed"] == [
            {
                "renamed_to": str(archived),
                "restored_to": str(a.db),
                "identity": f"{b_identity[0]}:{b_identity[1]}",
            }
        ]
        assert sentinels[str(a.db)]["at"] == str(archived) and _is_sentinel(archived)
        assert _entries(a.db.parent) == sorted(
            [
                "chronos.db",
                *SIDECARS,
                "victim-away",
                journal.name,
                archived.name,
                wal_archived.name,
                shm_archived.name,
            ]
        )
        assert (
            _sha256(a.db) == b_sha and _identity(a.db) == b_identity
        )  # the foreign, back at its name
        assert _sha256(a.db.with_name("victim-away")) == a_sha  # the store, where the swap put it
    else:
        assert report["code"] == "LIVE_NAME_OCCUPIED"
        assert report["foreign_renamed"] == [
            {
                "renamed_to": str(a.db),
                "restored_to": str(archived),
                "identity": "{}:{}".format(*_identity(archived)),
            }
        ]
        assert sentinels[str(a.db)]["at"] is None  # replaced by the foreign directory
        assert stat.S_ISDIR(
            os.lstat(archived).st_mode
        )  # the foreign directory, back where it was put
        assert _entries(a.db.parent) == sorted(
            [
                "chronos.db",
                *SIDECARS,
                journal.name,
                archived.name,
                wal_archived.name,
                shm_archived.name,
            ]
        )
        assert (
            _sha256(a.db) == a_sha and _identity(a.db) == a_identity
        )  # the store, back at its name


def test_9k_a_sidecar_swap_at_the_final_syscall_after_the_main_file_moved_is_reported_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    wal = store.db.with_name("chronos.db-wal")
    shm = store.db.with_name("chronos.db-shm")
    admitted: list[tuple[int, int]] = []
    real_rename = cutover._renameat2

    def swap_wal_then_rename(
        src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int
    ) -> None:
        if src == "chronos.db-wal":
            monkeypatch.setattr(cutover, "_renameat2", real_rename)
            admitted.append(_identity(wal))
            os.rename(wal, wal.with_name("wal-away"))
            planted = wal.with_name("planted")
            planted.write_bytes(b"")
            os.rename(planted, wal)
        real_rename(src_dir_fd, src, dst_dir_fd, dst, flags)

    monkeypatch.setattr(cutover, "_renameat2", swap_wal_then_rename)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "MOVE_INCOMPLETE", report
    assert report["code"] == "SOURCE_SWAPPED"
    archived_db = store.db.with_name(f"chronos.db.archived-{store.stamp}")
    archived_wal = wal.with_name(f"chronos.db-wal.archived-{store.stamp}")
    assert report["moved_aside"] == [{"from": str(store.db), "to": str(archived_db)}]
    assert report["not_moved"] == [str(wal), str(shm)]
    assert report["foreign_renamed"] == [
        {
            "renamed_to": str(archived_wal),
            "restored_to": str(wal),
            "identity": "{}:{}".format(*_identity(wal)),
        }
    ]
    journal = store.db.with_name("chronos.db-journal")
    archived_shm = shm.with_name(f"chronos.db-shm.archived-{store.stamp}")
    assert _entries(store.db.parent) == sorted(
        [*LIVE_NAMES, archived_db.name, archived_wal.name, archived_shm.name, "wal-away"]
    )
    assert _is_sentinel(store.db) and _is_sentinel(journal)  # the live name the store left
    assert _is_sentinel(archived_wal) and _is_sentinel(archived_shm)  # exchanged back / never used
    assert {s["name"]: s["at"] for s in report["sentinels"]} == {
        str(store.db): str(store.db),
        str(wal): str(archived_wal),
        str(shm): str(archived_shm),
        str(journal): str(journal),
    }
    assert _identity(wal.with_name("wal-away")) == admitted[0]  # the admitted sidecar, intact
    # the runbook's rollback is exact: the sentinels go first (rmdir — the owner's hand, never
    # the command's), then the one moved name renames back by name
    for sentinel in (store.db, journal, archived_wal, archived_shm):
        os.rmdir(sentinel)
    os.rename(archived_db, store.db)
    assert _entries(store.db.parent) == sorted(["chronos.db", *SIDECARS, "wal-away"])


def test_9l_an_archived_report_proves_the_admitted_set_quiet_and_the_live_names_sealed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, binding="closed")
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    exclusion = report["exclusion"]
    assert exclusion["mechanism"].startswith("kernel read lease")
    assert exclusion["leased"] == ["chronos.db", *SIDECARS]
    assert exclusion["intact_after_move"] is True
    assert exclusion["wal_bytes_after_move"] <= cutover.WAL_HEADER_BYTES
    # r3: the live-name proof — every name of the namespace IS this run's sentinel by identity
    assert exclusion["live_names_sealed"] is True
    for entry in report["sentinels"]:
        assert entry["at"] == entry["name"]
        assert entry["identity"] == "{}:{}".format(*_identity(Path(entry["name"])))
        assert _is_sentinel(Path(entry["name"]))
    archived = store.db.with_name(f"chronos.db.archived-{store.stamp}")
    fd = os.open(archived, os.O_RDWR | os.O_NONBLOCK)  # every lease released on return
    os.close(fd)


# ------------------------------------------------- (7, r3) the escrow tool deletes nothing


def test_7i_daybreak_s_race_a_swap_inside_the_last_identity_check_deletes_nothing(
    tmp_path: Path,
    age_keys: KeyMaterial,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Daybreak r2 P1-3, the exact probe: the identity predicate confirms the created artifact
    and, before it returns, the created file is renamed aside and a foreign file planted at the
    name. r2 unlinked the name after the predicate — the foreign entry. r3: after a post-create
    failure the tool deletes nothing, so there is no interval to race — the only ``unlink`` the
    module performs is its own scratch cleartext (recorded here: none on this path, where the
    decrypt is forced to fail before the scratch exists), the predicate is not consulted for a
    cleanup, the foreign entry (wherever the probe put one) survives, and the created ciphertext
    is reported by device:inode for the owner to shred."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    aside = out.with_name("artifact-aside")
    foreign = b"not the file this run created"

    def failing_decrypt(*args: Any, **kwargs: Any) -> None:
        raise escrow.EscrowRefused("AGE_FAILED", "forced: the decrypt step failed")

    monkeypatch.setattr(escrow, "_decrypts_back_to", failing_decrypt)
    real_predicate = escrow._entry_is_real_file
    verdicts: list[bool] = []

    def swapping_predicate(dfd: int, name: str, identity: tuple[int, int]) -> bool:
        verdict = real_predicate(dfd, name, identity)
        verdicts.append(verdict)
        if len(verdicts) > 1 and verdict:  # a cleanup's own check, if one exists: swap after it
            os.rename(out, aside)
            out.write_bytes(foreign)
        return verdict

    monkeypatch.setattr(escrow, "_entry_is_real_file", swapping_predicate)
    unlinked: list[tuple[str, int | None]] = []
    real_unlink = os.unlink

    def recording_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        unlinked.append((os.fspath(path), kwargs.get("dir_fd")))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", recording_unlink)
    key = _hex("n3-2 r3 TEST key for the predicate race — never a real one")
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["code"] == "AGE_FAILED", report
    assert unlinked == [], unlinked  # nothing was unlinked on the failure path
    assert verdicts == [True]  # the one post-create check; no cleanup consults the name
    created = report["created_artifact"]
    assert created["removed"] is False and "shred" in created["note"]
    ciphertext = aside if aside.exists() else out
    if aside.exists():
        assert out.read_bytes() == foreign  # the foreign entry survives
    assert _entries(out.parent) == sorted(p.name for p in (out, aside) if p.exists())
    assert created["identity"] == "{}:{}".format(*_identity(ciphertext))
    assert AGE is not None
    decrypted = subprocess.run(
        [AGE, "-d", "-i", str(age_keys.identity), str(ciphertext)], check=True, capture_output=True
    )
    assert decrypted.stdout == (key + "\n").encode()  # the created ciphertext, intact
    assert key not in json.dumps(report)
    # and statically: the module's only deletions are its private scratch (the cleartext the
    # proof wrote, and the 0700 directory it made) — never the artifact, never by name
    tree = ast.parse(ESCROW_MODULE.read_text(encoding="utf-8"))
    deleters = sorted(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("unlink", "remove", "rmdir", "rmtree", "unlink_created")
    )
    assert deleters == ["os.rmdir(scratch)", "os.unlink('key', dir_fd=scratch_fd)"], deleters


# ------------------------------------------- (10, r3) the live namespace is never vacant


def _hook_post_move_quiet_check(monkeypatch: pytest.MonkeyPatch, action: Any) -> list[int]:
    """Daybreak's r2 hook, exactly: the SECOND ``_not_quiet`` call — after the last rename,
    before the report. ``action`` runs there, then the real check."""

    real = cutover._not_quiet
    calls: list[int] = []

    def hooked(*args: Any, **kwargs: Any) -> Any:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(cutover, "_not_quiet", hooked)
    return calls


def test_10a_daybreak_s_counterexample_a_store_planted_at_the_vacated_live_name_cannot_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Daybreak r2 P1-1, the exact hook and the exact attack: after the last rename, before the
    report, plant a valid database at the live name. r3: the live name was never vacant — the
    final syscall EXCHANGED the store with a 0500 sentinel directory this run made — so every way
    of creating a file there fails (rename over it: EISDIR; O_CREAT|O_EXCL: EEXIST; O_CREAT:
    EISDIR), every writer path refuses the directory (SQLite: unable to open; the repository's
    R-21 admission: non-regular path), the planted store stays where it was, and ARCHIVED stands
    with the sentinel at the name."""

    store = _store(tmp_path / "live", binding="closed")
    planted = _store(tmp_path / "planted", version_offset=0)  # a valid store at the running version
    planted_sha = _sha256(planted.db)

    def plant() -> None:
        assert _is_sentinel(store.db)  # not vacant: the exchange put the sentinel there
        with pytest.raises(IsADirectoryError):
            os.rename(planted.db, store.db)
        with pytest.raises(FileExistsError):
            os.open(store.db, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with pytest.raises(IsADirectoryError):
            os.open(store.db, os.O_WRONLY | os.O_CREAT)
        with pytest.raises(sqlite3.OperationalError, match="unable to open"):
            sqlite3.connect(store.db).execute("SELECT 1")
        with pytest.raises(RuntimeError, match="non-regular"):
            Database(f"sqlite:///{store.db}")

    calls = _hook_post_move_quiet_check(monkeypatch, plant)
    code, report = _run(store, tmp_path / "live", "--apply", capsys=capsys)
    assert calls == [1, 2]
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    assert _is_sentinel(store.db) and _sha256(planted.db) == planted_sha
    assert _identity(store.db.with_name(f"chronos.db.archived-{store.stamp}")) == tuple(
        BackupManifest.from_dict(
            json.loads((Path(report["envelope"]) / drill.MANIFEST_NAME).read_text(encoding="utf-8"))
        ).source_identity
    )


def test_10b_an_owner_uid_actor_that_vacates_a_live_name_before_the_report_is_never_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The trust root, stated: the store owner's uid is the one actor that CAN vacate a sentinel
    (rmdir). Should it, and plant a valid store at the name before the report-time proof, the
    proof fails: MOVE_INCOMPLETE with ``moved_aside`` exact, never ARCHIVED; the planted store is
    untouched (the command deletes nothing); the archive names hold the admitted set; the other
    sentinels stand."""

    store = _store(tmp_path / "live", binding="closed")
    source_identity = _identity(store.db)
    planted = _store(tmp_path / "planted", version_offset=0)
    planted_sha = _sha256(planted.db)

    def vacate_then_plant() -> None:
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(store.db)
        os.rename(planted.db, store.db)

    _hook_post_move_quiet_check(monkeypatch, vacate_then_plant)
    code, report = _run(store, tmp_path / "live", "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "MOVE_INCOMPLETE", report
    assert report["code"] == "LIVE_NAME_OCCUPIED"
    archived = {
        name: store.db.with_name(f"{name}.archived-{store.stamp}")
        for name in ("chronos.db", *SIDECARS)
    }
    assert report["moved_aside"] == [
        {"from": str(store.db.with_name(name)), "to": str(path)} for name, path in archived.items()
    ]
    assert report["not_moved"] == []
    assert _sha256(store.db) == planted_sha and _identity(archived["chronos.db"]) == source_identity
    assert _entries(store.db.parent) == sorted([*LIVE_NAMES, *(p.name for p in archived.values())])
    assert all(_is_sentinel(store.db.with_name(name)) for name in LIVE_NAMES[1:])
    assert {s["name"]: s["at"] for s in report["sentinels"]}[str(store.db)] is None


def test_10c_every_live_name_stays_a_sentinel_until_the_owner_s_rmdir_then_a_fresh_store_is_made(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """After ARCHIVED, for as long as the command's process is gone and until the owner's hand:
    each of the four names of the store's namespace is a 0500 empty directory owned by this user,
    the report names each by device:inode, no file can be created or renamed there, SQLite
    cannot open it, and the repository refuses to construct a Database over it — a lone
    ``-journal`` sentinel included (R-21 checks all four names). The owner's rmdir of the four
    (step 7) vacates the namespace; only then does the backend's construction create a fresh
    store at the running schema."""

    store = _store(tmp_path, binding="closed")
    source_identity = _identity(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    foreign = store.db.with_name("foreign")
    foreign.write_bytes(b"x")
    assert [s["name"] for s in report["sentinels"]] == [
        str(store.db.with_name(n)) for n in LIVE_NAMES
    ]
    for entry in report["sentinels"]:
        path = Path(entry["name"])
        assert _is_sentinel(path)
        assert entry == {
            "name": str(path),
            "at": str(path),
            "identity": "{}:{}".format(*_identity(path)),
        }
        with pytest.raises(FileExistsError):
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with pytest.raises(IsADirectoryError):
            os.rename(foreign, path)
        with pytest.raises(IsADirectoryError):
            os.open(path, os.O_RDWR)
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        sqlite3.connect(store.db).execute("SELECT 1")
    with pytest.raises(RuntimeError, match="non-regular"):
        Database(f"sqlite:///{store.db}")
    assert cutover.LIVE_NAME_SUFFIXES == database_module._SQLITE_SIDECAR_SUFFIXES
    for name in LIVE_NAMES[:-1]:
        os.rmdir(store.db.with_name(name))
    with pytest.raises(RuntimeError, match="non-regular"):  # the -journal sentinel alone refuses
        Database(f"sqlite:///{store.db}")
    # … but R-21 creates a missing store BEFORE it checks the namespace, so once chronos.db's
    # own sentinel is gone a refused writer still leaves an EMPTY file there: the four rmdirs
    # are one line in the runbook, and the chronos.db sentinel is the one that guards creation
    assert store.db.is_file() and store.db.stat().st_size == 0
    os.rmdir(store.db.with_name(LIVE_NAMES[-1]))
    database = Database(f"sqlite:///{store.db}")  # the owner's step 7
    try:
        database.initialize()
    finally:
        database.dispose()
    assert store.db.is_file() and _identity(store.db) != source_identity
    with closing(sqlite3.connect(f"{store.db.as_uri()}?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (SCHEMA_VERSION,)
        ]


@pytest.mark.parametrize(
    "shape",
    [
        "journal",
        "wal-not-admitted",
        "shm-not-admitted",
    ],
)
def test_10d_a_non_admitted_entry_at_a_live_name_refuses_the_move_with_nothing_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    """A live name of the namespace that is NOT in the admitted set must be absent at the move, so
    that a sentinel can take it: a frame-free ``-wal``/``-shm`` that appeared after admission (a
    rollback-journal store, whose read-only connection leaves no bookkeeping, so the admitted set
    is the store alone) or a ``-journal`` is ``LIVE_NAME_OCCUPIED`` — nothing moved, no sentinel
    created, the planted entry untouched (this retires r2's ``left_in_place``: an occupied name
    is a refusal, not a report line)."""

    journal_mode = "wal" if shape == "journal" else "delete"
    store = _store(tmp_path, binding="closed", journal_mode=journal_mode)
    before = _sha256(store.db)
    name = {
        "journal": "chronos.db-journal",
        "wal-not-admitted": "chronos.db-wal",
        "shm-not-admitted": "chronos.db-shm",
    }[shape]
    planted = store.db.with_name(name)
    real_move = cutover._move_aside

    def plant_then_move(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(cutover, "_move_aside", real_move)
        assert not planted.exists()
        planted.write_bytes(b"")  # frame-free, regular: r2 would have left it in place
        return real_move(*args, **kwargs)

    monkeypatch.setattr(cutover, "_move_aside", plant_then_move)
    expected_before = _entries(store.db.parent)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "LIVE_NAME_OCCUPIED" and name in report["failures"][0]
    assert report["moved_aside"] == [] and report["sentinels"] == []
    assert store.db.is_file() and _sha256(store.db) == before
    assert planted.read_bytes() == b""  # never touched
    # the planted name stays; on a WAL store the reader bookkeeping may sit beside it
    after = _entries(store.db.parent)
    bookkeeping = set(READER_BOOKKEEPING) - {name}
    assert sorted(set(after) - bookkeeping) == sorted([*expected_before, name])
    assert set(after) - set(expected_before) - {name} <= bookkeeping
    assert Path(report["envelope"]).is_dir()
    if shape == "journal":
        # present BEFORE the run, a -journal refuses at the gate: no envelope, nothing created
        early = _store(tmp_path / "early", binding="closed")
        early.db.with_name("chronos.db-journal").write_bytes(b"")
        code, report = _run(early, tmp_path / "early", "--apply", capsys=capsys)
        assert code == 2 and report["code"] == "LIVE_NAME_OCCUPIED" and report["gate"] is None
        assert not (tmp_path / "early" / "backups").exists()


# ----------------------------------------- (11, r4) the parent-directory boundary of the sentinels


def _may_remove_entry(
    parent: tuple[int, int, int], actor_uid: int, actor_gids: frozenset[int], entry_uid: int
) -> bool:
    """POSIX, as Linux implements it: ``unlink``/``rmdir`` of an entry is authorized by WRITE and
    SEARCH permission on the entry's PARENT for the actor's class (owner, group, other) — the
    entry's own mode is irrelevant — and, with the parent's sticky bit set, a non-owner of the
    parent may remove only entries it owns. A simulated authorization check (Daybreak r3 P1-1)."""

    parent_uid, parent_gid, parent_mode = parent
    if actor_uid == parent_uid:
        bits = (parent_mode >> 6) & 0o7
    elif parent_gid in actor_gids:
        bits = (parent_mode >> 3) & 0o7
    else:
        bits = parent_mode & 0o7
    if bits & 0o3 != 0o3:
        return False
    return not (parent_mode & 0o1000 and actor_uid not in (parent_uid, entry_uid))


OWNER = 1000
GROUP_MEMBER = (1001, frozenset({1000}))  # another uid in the owner's group
OTHER = (1002, frozenset({1002}))
PRIVATE_MODES = (0o700, 0o750, 0o755, 0o711, 0o500)
WRITABLE_MODES = (0o775, 0o770, 0o707, 0o777, 0o2775, 0o1775, 0o1777)


@pytest.mark.parametrize("mode", WRITABLE_MODES, ids=[f"{mode:04o}" for mode in WRITABLE_MODES])
def test_11a_a_group_or_other_writable_database_parent_is_refused_before_anything_is_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    """Daybreak r3 P1-1: the sentinels are only as private as their PARENT — removing an empty
    directory is authorized by write permission on the parent, not by the directory's own 0500.
    The documented deployment's `mkdir -p data` under umask 002 leaves `data/` 0775: group
    members could rmdir a sentinel. r4: the command refuses a database parent that is writable by
    its group or by others (the sticky bit changes nothing: still refused), or not owned by the
    effective uid — PARENT_NOT_PRIVATE, dry-run and apply alike, before the source is even
    opened: no envelope, no reader bookkeeping, nothing. Owner-private again, it archives."""

    store = _store(tmp_path, binding="closed")
    os.chmod(store.db.parent, mode)
    before, entries = _sha256(store.db), _entries(store.db.parent)
    for args in ((), ("--apply",)):
        code, report = _run(store, tmp_path, *args, capsys=capsys)
        assert code == 2 and report["code"] == "PARENT_NOT_PRIVATE", report
        assert report["gate"] is None and report["envelope"] is None
        assert f"{mode & 0o7777:04o}" in report["failures"][0]
        assert "chmod 0700" in report["failures"][0]
    assert not (tmp_path / "backups").exists()
    assert _sha256(store.db) == before and _entries(store.db.parent) == entries
    os.chmod(store.db.parent, 0o700)
    # owned by another uid (the identity source substituted; the directory is this test's)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: OWNER + 1 if os.getuid() != OWNER + 1 else OWNER)
        code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    # r5: the ancestor chain is walked top-down with the substituted uid, so the first directory
    # owned by neither root nor it (here: the home directory) refuses as PATH_NOT_PROTECTED
    # before data/ is reached; under a root-owned chain the leaf's PARENT_NOT_PRIVATE would
    assert code == 2 and report["code"] in ("PARENT_NOT_PRIVATE", "PATH_NOT_PROTECTED"), report
    assert "owned by" in report["failures"][0]
    assert _entries(store.db.parent) == entries
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    assert report["exclusion"]["parent"]["mode"] == "0700"
    assert report["exclusion"]["parent"]["owner_is_effective_uid"] is True


def test_11b_the_private_parent_rule_is_exactly_what_denies_a_non_owner_the_rmdir(
    tmp_path: Path,
) -> None:
    """The simulated authorization check (a second uid is not reachable here — see 11c): for every
    parent mode the command accepts, no actor but the owner is authorized to remove an entry,
    whatever groups it holds; for the documented 0775 shape a group member IS (Daybreak's fact —
    the positive control that the model has teeth), and the sticky bit on a writable parent would
    only narrow it, which the rule does not rely on (0o1775 is refused all the same). The rule
    itself is pinned: owner == effective uid, no group/other write bit, a directory."""

    sentinel_owner = OWNER
    for mode in PRIVATE_MODES:
        parent = (OWNER, OWNER, mode)
        assert cutover._parent_problem(_fake_stat(mode, OWNER), OWNER) is None, f"{mode:04o}"
        for actor_uid, gids in (GROUP_MEMBER, OTHER):
            assert not _may_remove_entry(parent, actor_uid, gids, sentinel_owner), f"{mode:04o}"
        assert _may_remove_entry(parent, OWNER, frozenset({OWNER}), sentinel_owner) == bool(
            (mode >> 6) & 0o3 == 0o3
        )
    for mode in WRITABLE_MODES:
        problem = cutover._parent_problem(_fake_stat(mode, OWNER), OWNER)
        assert problem is not None and f"{mode & 0o7777:04o}" in problem, f"{mode:04o}"
    # the positive control: on the documented 0775 parent a group member may rmdir the sentinel
    assert _may_remove_entry((OWNER, OWNER, 0o775), *GROUP_MEMBER, sentinel_owner)
    assert not _may_remove_entry((OWNER, OWNER, 0o775), *OTHER, sentinel_owner)
    assert _may_remove_entry((OWNER, OWNER, 0o777), *OTHER, sentinel_owner)
    assert not _may_remove_entry((OWNER, OWNER, 0o1775), *GROUP_MEMBER, sentinel_owner)  # sticky
    # not this uid's, or not a directory: refused by owner / by kind
    assert "owned by" in str(cutover._parent_problem(_fake_stat(0o700, OWNER + 1), OWNER))
    assert "not a directory" in str(
        cutover._parent_problem(_fake_stat(0o700, OWNER, directory=False), OWNER)
    )
    # and the real thing, as this uid: an empty 0500 directory inside a 0700 parent is removable
    # by the parent's owner only because of the PARENT's bits — its own bits never mattered
    parent = tmp_path / "parent"
    parent.mkdir(0o700)
    child = parent / "sentinel"
    child.mkdir(0o500)
    os.rmdir(child)
    assert not child.exists()


def _fake_stat(mode: int, uid: int, *, directory: bool = True) -> os.stat_result:
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    return os.stat_result((kind | mode, 1, 1, 1, uid, uid, 0, 0, 0, 0))


SUBORDINATE_UID = 100000  # the first subordinate uid /etc/subuid grants on this host, if any


def _second_uid_recipe() -> list[str] | None:
    """A command prefix that runs its argument as a DIFFERENT real uid without privilege — an
    unprivileged user namespace mapping ns-uid 0 to this uid and ns-uid 1 to a subordinate uid
    (``newuidmap``/``newgidmap`` from /etc/subuid) — when the kernel and the LSM allow the
    namespace's capabilities to be used; ``None`` when they do not (this host: AppArmor's
    ``apparmor_restrict_unprivileged_userns`` denies setuid inside the namespace — see
    build/N3-2/evidence/second-uid-probe-r4.txt) or when ``sudo`` would be the only route."""

    unshare = shutil.which("unshare")
    if unshare is None or not Path("/etc/subuid").exists():
        return None
    prefix = [
        unshare,
        "-U",
        f"--map-users=0:{os.getuid()}:1",
        f"--map-users=1:{SUBORDINATE_UID}:1",
        f"--map-groups=0:{os.getgid()}:1",
        f"--map-groups=1:{SUBORDINATE_UID}:1",
        "--",
    ]
    probe = subprocess.run(
        [*prefix, sys.executable, "-c", "import os; os.setgroups([0]); os.setgid(1); os.setuid(1)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return prefix if probe.returncode == 0 else None


AS_SECOND_UID = """
import json, os, sys
FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
data, open_data = sys.argv[1], sys.argv[2]
os.setgroups([0])  # the owner's group: Daybreak's "second uid in the writable group"
os.setgid(1)
os.setuid(1)
out = {"uid": os.getuid(), "groups": os.getgroups(), "sealed": {}, "open": {}}
for name in ("chronos.db", "chronos.db-wal", "chronos.db-shm", "chronos.db-journal"):
    path = os.path.join(data, name)
    attempts = {}
    for label, fn in (
        ("rmdir", lambda: os.rmdir(path)),
        ("rename-over", lambda: os.rename(os.path.join(data, "foreign"), path)),
        ("create-in", lambda: os.close(os.open(os.path.join(data, name + ".x"), FLAGS))),
    ):
        try:
            fn()
            attempts[label] = "SUCCEEDED"
        except OSError as error:
            attempts[label] = type(error).__name__
    out["sealed"][name] = attempts
try:
    os.rmdir(os.path.join(open_data, "chronos.db"))
    out["open"]["rmdir"] = "SUCCEEDED"
except OSError as error:
    out["open"]["rmdir"] = type(error).__name__
print(json.dumps(out))
"""


def test_11c_a_second_uid_in_the_owner_s_group_cannot_remove_or_replace_a_sentinel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Daybreak r3 P1-1's attacker, real: a different uid holding the owner's group tries, after
    ARCHIVED, to rmdir / rename over / create beside every live-name sentinel inside the 0700
    `data/` the command now requires — every attempt must fail (EACCES: no write or search on the
    parent) — and, as the positive control, rmdirs a 0500 sentinel inside a 0775 parent of the
    same owner, which SUCCEEDS (the documented deployment's shape, the finding's fact). Skipped
    where no second uid can be reached without privilege (this host; the reason is printed)."""

    recipe = _second_uid_recipe()
    if recipe is None:
        pytest.skip(
            "no second uid reachable without privilege here (unprivileged user namespaces cannot "
            "use their capabilities under this host's LSM policy; sudo is fenced) — the parent "
            "rule is pinned by 11a/11b; see evidence/second-uid-probe-r4.txt"
        )
    scratch = Path(tempfile.mkdtemp(prefix="n3-2-r4-second-uid-", dir=_private_root()))
    try:
        os.chmod(scratch, 0o711)  # traversable by the second uid; nothing inside is writable to it
        store = _store(scratch, binding="closed")
        assert oct(os.lstat(store.db.parent).st_mode & 0o777) == "0o700"
        code, report = _run(store, scratch, "--apply", capsys=capsys)
        assert code == 0 and report["verdict"] == "ARCHIVED", report
        (store.db.parent / "foreign").write_bytes(b"x")
        open_data = scratch / "open-data"
        open_data.mkdir(0o775)
        os.chmod(open_data, 0o775)
        (open_data / "chronos.db").mkdir(0o500)
        completed = subprocess.run(
            [*recipe, sys.executable, "-c", AS_SECOND_UID, str(store.db.parent), str(open_data)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        observed = json.loads(completed.stdout)
        assert observed["uid"] == 1 and 0 in observed["groups"]
        for name, attempts in observed["sealed"].items():
            assert set(attempts.values()) == {"PermissionError"}, (name, attempts)
            assert _is_sentinel(store.db.parent / name)
        assert observed["open"]["rmdir"] == "SUCCEEDED"  # the positive control: 0775 is open
        assert not (open_data / "chronos.db").exists()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_11d_the_deployment_and_the_runbook_make_the_database_parent_owner_private() -> None:
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "chmod 0700 data" in deployment
    text = RUNBOOK.read_text(encoding="utf-8")
    before = text[text.index("## Before you start") : text.index("## Steps")]
    assert "chmod 0700 data" in before and "PARENT_NOT_PRIVATE" in before
    assert "its parent" in before  # the authorization fact, stated
    step3 = text[text.index("3. **Apply") : text.index("4. **Remove")]
    assert "owner-private" in step3 and "its parent" in step3
    table = text[text.index("## What the gate's refusal codes mean") :]
    assert "`PARENT_NOT_PRIVATE`" in table
    module = MODULE.read_text(encoding="utf-8")
    assert "owner-private" in module and "PARENT_NOT_PRIVATE" in module


# ------------------------------------------- (12, r5) the protected path: the ancestor chain


ANCESTOR_SHAPES = ("group-writable", "other-writable", "sticky-world-writable", "symlink")


@pytest.mark.parametrize("shape", ANCESTOR_SHAPES)
def test_12a_a_writable_sticky_or_linked_ancestor_is_refused_before_anything_is_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    """Daybreak r4 P1-1: the final parent's privacy means nothing if a uid can write an ANCESTOR
    — it renames the checked ``data/`` away and plants a replacement. r5 narrows the guarantee to
    an explicitly protected trust root: every directory from ``/`` down to ``data/`` must be a
    real directory owned by root or the effective uid with no group or other write bit; a
    /tmp-style sticky world-writable directory is refused too (the sticky bit limits renaming to
    an entry's owner, but a path through such a directory is outside the supported deployment —
    the refusal says so); a symlink component is refused. Checked before the source is opened:
    ``PATH_NOT_PROTECTED``, dry-run and apply alike, nothing created. Fixed, it archives, and
    ``exclusion.protected_path`` lists the whole chain it held through the report."""

    store = _store(tmp_path / "live", binding="closed")
    ancestor = tmp_path / "live"  # the directory that contains data/
    db_path = store.db
    if shape == "group-writable":
        os.chmod(ancestor, 0o775)
    elif shape == "other-writable":
        os.chmod(tmp_path, 0o707)  # two levels up
    elif shape == "sticky-world-writable":
        os.chmod(ancestor, 0o1777)
    else:
        os.symlink(ancestor, tmp_path / "link")
        db_path = tmp_path / "link" / "data" / "chronos.db"
    before, entries = _sha256(store.db), _entries(store.db.parent)
    for args in ((), ("--apply",)):
        argv = [
            "--db",
            str(db_path),
            "--out",
            str(tmp_path / "backups"),
            "--restore-into",
            str(tmp_path / "restored"),
            *args,
        ]
        code = cutover.main(argv, clock=_clock(store.now))
        report = json.loads(capsys.readouterr().out)
        assert code == 2 and report["code"] == "PATH_NOT_PROTECTED", report
        assert report["gate"] is None and report["envelope"] is None
        message = report["failures"][0]
        if shape == "sticky-world-writable":
            assert "sticky" in message and "1777" in message
        elif shape == "symlink":
            assert "symlink" in message or "not a directory" in message
        else:
            assert "writable" in message
    assert not (tmp_path / "backups").exists()
    assert _sha256(store.db) == before and _entries(store.db.parent) == entries
    os.chmod(ancestor, 0o700)
    os.chmod(tmp_path, 0o700)
    code, report = _run(store, tmp_path / "live", "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    protected = report["exclusion"]["protected_path"]
    chain = protected["chain"]
    assert chain[0]["path"] == "/" and chain[-1]["path"] == str(store.db.parent)
    for entry in chain:
        assert entry["owner_uid"] in (0, os.geteuid()) and not int(entry["mode"], 8) & 0o022
    assert protected["held_through_report"] is True and protected["linked_at_report"] is True


@pytest.mark.parametrize("when", ["after-the-move-check", "after-the-exchanges"])
def test_12b_daybreak_s_counterexample_the_checked_directory_displaced_after_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], when: str
) -> None:
    """Daybreak r4 P1-1, the exact injection: right after the move's parent check (or, the
    second shape, after the exchanges), the checked ``data/`` is renamed to ``data-displaced``
    and a replacement 0700 ``data/`` with a foreign ``chronos.db`` is planted (here by the owner —
    the actor the precondition excludes is another uid with a writable ancestor, which 12a
    refuses up front). r5 holds every ancestor open and re-proves, on the move path and at
    report time, that each path component still names the held directory: ``PATH_DISPLACED``
    — nothing moved in the first shape; ``MOVE_INCOMPLETE`` with ``moved_aside`` exact in the
    second, the sentinels standing in the displaced directory, the foreign store untouched, and
    never ``ARCHIVED`` at a lexical path that no longer names the checked directory."""

    store = _store(tmp_path / "live", binding="closed")
    data = store.db.parent
    displaced = data.with_name("data-displaced")
    source_identity, before = _identity(store.db), _sha256(store.db)
    foreign = b"not the store the gate saw"

    def displace() -> None:
        os.rename(data, displaced)
        data.mkdir(0o700)
        os.chmod(data, 0o700)
        (data / "chronos.db").write_bytes(foreign)

    if when == "after-the-move-check":
        real = cutover._private_parent_facts
        calls: list[int] = []

        def hooked(*args: Any, **kwargs: Any) -> Any:
            facts = real(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:  # the move's own check, on the walked descriptor
                displace()
            return facts

        monkeypatch.setattr(cutover, "_private_parent_facts", hooked)
    else:
        _hook_post_move_quiet_check(monkeypatch, displace)
    code, report = _run(store, tmp_path / "live", "--apply", capsys=capsys)
    assert code == 2 and report["verdict"] != "ARCHIVED", report
    assert report["code"] == "PATH_DISPLACED", report["failures"]
    assert (data / "chronos.db").read_bytes() == foreign  # the planted replacement, untouched
    held = _identity(displaced)
    assert f"{held[0]}:{held[1]}" in report["failures"][0]  # the held directory, named by inode
    if when == "after-the-move-check":
        assert report["verdict"] == "REFUSED" and report["moved_aside"] == []
        assert _identity(displaced / "chronos.db") == source_identity
        assert _sha256(displaced / "chronos.db") == before
        assert report["sentinels"] == []
    else:
        assert report["verdict"] == "MOVE_INCOMPLETE"
        archived = displaced / f"chronos.db.archived-{store.stamp}"
        assert _identity(archived) == source_identity and _sha256(archived) == before
        assert [m["from"] for m in report["moved_aside"]] == [
            str(store.db),
            *(str(store.db.with_name(n)) for n in SIDECARS),
        ]
        assert all(_is_sentinel(displaced / name) for name in LIVE_NAMES)


def test_12c_the_docs_and_the_module_state_the_protected_path_and_the_trust_root() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    before = text[text.index("## Before you start") : text.index("## Steps")]
    assert "ls -ld" in before and "PATH_NOT_PROTECTED" in before and "ancestor" in before
    assert "sticky" in before and "/tmp" in before
    table = text[text.index("## What the gate's refusal codes mean") :]
    assert "`PATH_NOT_PROTECTED`" in table and "`PATH_DISPLACED`" in table
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "ls -ld" in deployment and "ancestor" in deployment
    module = MODULE.read_text(encoding="utf-8")
    assert "PATH_NOT_PROTECTED" in module and "PATH_DISPLACED" in module
    for statement in ("same Python process", "owner's uid", "ancestor"):
        assert statement in module, statement

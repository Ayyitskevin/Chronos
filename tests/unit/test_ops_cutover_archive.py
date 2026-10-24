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
import errno
import hashlib
import inspect
import io
import itertools
import json
import os
import pickle
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
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
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY_HEX

from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.cli.main import main as cli_main
from chronos.config.settings import get_settings
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
from chronos.utils.identifiers import PersistenceKey, account_fingerprint

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


@pytest.mark.parametrize("mode", [0o700, 0o777], ids=["own-0700", "0777"])
def test_5e_fix_rt_a_pre_existing_empty_restore_target_refuses_before_any_envelope_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    """FIX-RT (Daybreak §7 (ii)): the cutover's target leaf must be ABSENT — even an empty, owner-
    only one refuses; the command creates it 0700 itself. A hostile pre-created 0777 one refuses
    the same way."""

    store = _store(tmp_path, binding="closed")
    target = tmp_path / "restored"
    target.mkdir()
    os.chmod(target, mode)
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "RESTORE_TARGET_NOT_FRESH", report
    assert "must not exist" in report["failures"][0], report
    assert not (tmp_path / "backups").exists()  # no envelope
    assert list(target.iterdir()) == []
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


@pytest.mark.parametrize("where", ["sticky-child", "sticky-grandchild"])
def test_5f_fix_rt_a_restore_target_under_a_sticky_dir_refuses_before_any_envelope_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    """A sticky, world-writable directory (``/tmp``-style, mode 1777) is not part of a protected
    chain (Daybreak), whether the restore target is its child or sits in a private 0700 directory
    inside it. The test makes that directory itself, under this module's protected 0700 root, so
    it does not depend on what the host's ``/tmp`` is (a sandbox mounts its own, 0755), and
    asserts the refusal names THAT directory as the component it refused. Two rules refuse it:
    as the target's direct parent it fails the owner-private rule on the leaf directory; as a
    higher ancestor (the grandchild) it fails the sticky/writable-ancestor rule."""

    store = _store(tmp_path, binding="closed")
    sticky = tmp_path / "sticky-world-writable"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)  # explicit: umask must not decide the premise
    assert stat.S_IMODE(os.lstat(sticky).st_mode) == 0o1777
    if where == "sticky-grandchild":
        private = sticky / "private"
        private.mkdir()
        os.chmod(private, 0o700)  # owned by this uid, owner-only: only its parent is at fault
        target = private / "verify"
    else:
        target = sticky / "verify"  # absent; the sticky directory itself is its parent
    before = _sha256(store.db)
    argv = [
        "--db",
        str(store.db),
        "--out",
        str(tmp_path / "backups"),
        "--restore-into",
        str(target),
        "--apply",
    ]
    code = cutover.main(argv, clock=_clock(store.now))
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["code"] == "RESTORE_TARGET_NOT_PROTECTED", report
    # the component walked and refused is the sticky directory (the --restore-into text also
    # starts with its path, so pin the "<walked> <reason>" pair the walk emits)
    reason = (
        "is a sticky, group/other-writable directory (mode 1777"
        if where == "sticky-grandchild"
        else "is writable by its group or by others (mode 1777)"
    )
    assert f"{sticky} {reason}" in report["failures"][0], report
    assert not target.exists() and not (tmp_path / "backups").exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


def test_5g_fix_rt_the_runbook_target_is_out_of_tmp_and_the_stop_is_in_the_document() -> None:
    """Muse's condition 3: a reader of N3-CUTOVER months from now meets the stop in the document —
    near the top (before the steps) AND at step 1, where the cutover is scheduled and begun."""

    text = RUNBOOK.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    stop = (
        "Do not schedule or run the cutover until FIX-RT (restore-target hardening) AND N3-3 "
        "(`chronos.operations.backup_pruner`) have both merged to main."
    )
    steps = flat.index("## Steps")
    step1, step2 = flat.index("1. **", steps), flat.index("2. **", steps)
    assert stop in flat[:steps], "the stop must stand before the steps"
    assert stop in flat[step1:step2], "the stop must stand at step 1, where the cutover is begun"
    assert flat.index(stop) < flat.index("## Before you start")
    assert "/tmp/chronos-n3-verify" not in text
    assert "--restore-into /var/backups/chronos/n3-verify" in text
    assert "`RESTORE_TARGET_NOT_PROTECTED`" in text and "`RESTORE_TARGET_NOT_FRESH`" in text
    # r1 (Daybreak P2-1): the check must be TIP-sensitive — a historical `git log -- <path>` still
    # prints after the path is removed from main, so it could false-pass after a revert of N3-3
    pruner_check = (
        "git grep -n '^def prune(' origin/main -- src/chronos/operations/backup_pruner.py"
    )
    assert f"`{pruner_check}`" in text
    assert "`git fetch origin main`" in text and "exit zero" in flat
    assert flat.index("`git fetch origin main`") < flat.index(pruner_check)
    assert not re.search(r"git log[^`\n]*backup_pruner\.py", text), "historical check form"
    module = MODULE.read_text(encoding="utf-8")
    assert "/tmp/chronos-n3-verify" not in module
    assert "--restore-into /var/backups/chronos/n3-verify" in module


def test_5h_b3_the_stop_banner_names_n3_4_and_the_escrow_change_and_their_greps() -> None:
    """B3 (A2 review): the banner's condition is four-fold. A later edit that drops either new
    marker (N3-4 restore-copy removal, recovery-key-only escrow) from the STOP — the banner, or
    either echo — fails here, and so does a marker the named source no longer carries."""

    text = RUNBOOK.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    banner = text[: text.index("\n\n", text.index("> **STOP"))]
    banner_flat = " ".join(banner.replace("\n> ", " ").split())
    added = (
        "**And do not schedule or run it until N3-4 (the self-cleaning restore drill, P2-4 = B) "
        "AND the recovery-key-only escrow change have merged to main too.**"
    )
    c1f = (
        "**And do not schedule or run it until C1-F (the step-8 and rollback fixes from the C1 "
        "rehearsal) has merged to main too.**"
    )
    assert added in banner_flat, "the banner must carry the N3-4 + escrow condition"
    assert c1f in banner_flat, "the banner must carry the C1-F condition"
    assert "K-20261023-002" in banner and "K-20261023-003" in banner  # Kevin's two rulings
    assert "K-20261024-001" in banner  # the ruling that admitted C1-F
    assert "all six" in banner_flat and "If any of them prints nothing" in banner_flat
    # the two original checks stay
    for old in (
        "git grep -n '^def prune(' origin/main -- src/chronos/operations/backup_pruner.py",
        "git grep -n RESTORE_TARGET_NOT_PROTECTED origin/main -- "
        "src/chronos/operations/cutover_archive.py",
    ):
        assert f"`{old}`" in banner_flat
    # the two new checks: each grep's pattern must exist in the named file at this tree
    marks = {
        "src/chronos/operations/restore_drill.py": "^RESTORE_COPY_NOT_REMOVED = ",
        "src/chronos/operations/persistence_key_escrow.py": (
            "recipient = restore_drill.KEVIN_RECOVERY_RECIPIENT"
        ),
    }
    for path, pattern in marks.items():
        assert f"`git grep -n '{pattern}' origin/main -- {path}`" in banner_flat, (path, pattern)
        regex = re.compile(pattern, re.MULTILINE)
        assert regex.search((ROOT / path).read_text(encoding="utf-8")), (path, pattern)
    # the C1-F checks: two independent markers in the runbook, one per reviewed fix. Each reads the
    # runbook itself, so a bracket keeps the banner's own text from matching it. The rollback marker
    # is the atomic-link fallback (fix round 1); the step-8 marker is the literal `.env` read (fix
    # round 2). A half-landed C1-F (one fix without the other) must fail at least one of them.
    c1f_path = "docs/ops/N3-CUTOVER.md"
    c1f_patterns = (
        "ln -[T] data/chronos.db.archived-",
        'dotenv_values(".env", interpolate=[F]alse)',
    )
    # git grep's default regex is basic: a parenthesis is literal. Python's is extended.
    rx = {pat: pat.replace("(", r"\(").replace(")", r"\)") for pat in c1f_patterns}
    for c1f_pattern in c1f_patterns:
        grep_line = f"`git grep -n '{c1f_pattern}' origin/main -- {c1f_path}`"
        assert grep_line in banner_flat, c1f_pattern
        assert re.search(rx[c1f_pattern], text), "a C1-F marker must match the fixed runbook"
        assert not re.search(rx[c1f_pattern], banner), "the banner must not satisfy its own marker"

    def clears(runbook: str) -> bool:
        """Would every runbook-targeted grep in the banner print something on this runbook text?"""
        return all(re.search(rx[pat], runbook) for pat in c1f_patterns)

    assert clears(text)
    # the half-landed mutants: the rollback fix without step 8's literal read (fix round 1's step 8,
    # and the original `env -i` form), and step 8 fixed with the rollback fix missing
    no_literal = text.replace('dotenv_values(".env", interpolate=False)', 'dotenv_values(".env")')
    assert no_literal != text and not clears(no_literal)
    env_i = no_literal.replace("python -I -c '", "env -i python -c '")
    assert env_i != no_literal and not clears(env_i)
    no_link = text.replace("ln -T data/chronos.db.archived-", "mv data/chronos.db.archived-")
    assert no_link != text and not clears(no_link)
    assert not re.search(r"<[^>\n]*(marker|ruling)[^>\n]*>", text), "an unfilled placeholder"
    # both echoes say the same, and both still point at the STOP
    before = flat[flat.index("## Before you start") : flat.index("## Steps")]
    steps = flat.index("## Steps")
    step1 = flat[flat.index("1. **", steps) : flat.index("2. **", steps)]
    for echo in (before, step1):
        assert added in echo and c1f in echo and "STOP at the top of this page" in echo


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


def _age_decrypt(identity: Path, artifact: Path) -> subprocess.CompletedProcess[bytes]:
    assert AGE is not None
    return subprocess.run(
        [AGE, "-d", "-i", str(identity), str(artifact)], capture_output=True, check=False
    )


def _x25519_stanzas(artifact: Path) -> int:
    """How many recipient stanzas the artifact's own age header carries (age v1: one
    ``-> X25519 <share>`` line per recipient) — read from the file, independent of the report."""

    header = artifact.read_bytes().split(b"\n---", 1)[0]
    assert header.startswith(b"age-encryption.org/v1\n"), header[:32]
    return sum(1 for line in header.split(b"\n") if line.startswith(b"-> X25519 "))


@dataclass(frozen=True)
class RecoveryIdentity:
    identity: Path
    public_key: str


@pytest.fixture
def recovery_identity(
    tmp_path: Path, age_keys: KeyMaterial, monkeypatch: pytest.MonkeyPatch
) -> RecoveryIdentity:
    """A THROWAWAY recovery identity substituted for the pinned recipient at its one home,
    ``restore_drill.KEVIN_RECOVERY_RECIPIENT`` (``setattr`` raises if the constant ever moves),
    so a test can open what the escrow tool wrote to the recovery recipient. Kevin's real key is
    untouched: its private half exists on no fleet host. The recipients file names the
    substitute beside the host key only so that code which still validates that file (the
    drill; the escrow tool before K-20261023-003) reaches encryption under the substitution."""

    identity = tmp_path / "keys" / "recovery-throwaway.age"
    public_key = _keygen(identity)
    monkeypatch.setattr(drill, "KEVIN_RECOVERY_RECIPIENT", public_key)
    age_keys.recipients.write_text(f"{age_keys.host_public_key}\n{public_key}\n", encoding="utf-8")
    return RecoveryIdentity(identity=identity, public_key=public_key)


def test_7c_the_escrow_tool_encrypts_the_key_to_the_recovery_recipient_alone(
    tmp_path: Path, age_keys: KeyMaterial
) -> None:
    """K-20261023-003 (escrow = recovery-key-only): the artifact is age-encrypted to exactly ONE
    recipient, the pinned ``KEVIN_RECOVERY_RECIPIENT``; the host identity is not a recipient and
    cannot open it. A TEST key (derived at run time) goes in on stdin, never through the
    environment or argv; the report names the one recipient, claims no decrypt (no key on the
    host could prove one — that proof is Kevin's, off-host, runbook step 6), and never prints
    the key or a digest of it."""

    key = _hex("n3-2 r1 TEST persistence key — never a real one")
    completed, out = _escrow(tmp_path, key + "\n")
    assert completed.returncode == 0, completed.stderr
    by_host = _age_decrypt(age_keys.identity, out)
    assert by_host.returncode != 0 and by_host.stdout == b"", "the host identity opened it"
    assert b"no identity matched" in by_host.stderr, by_host.stderr
    assert _x25519_stanzas(out) == 1
    report = json.loads(completed.stdout)
    assert report["recipients"] == [drill.KEVIN_RECOVERY_RECIPIENT]
    assert report["verdict"] == "ESCROWED" and report["decrypt_verified"] is False
    assert "verified" not in report  # no field that reads as a decrypt proof
    assert report["out"] == str(out) and out.is_file()
    assert report["ciphertext_sha256"] == _sha256(out)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert key not in completed.stdout + completed.stderr
    without_digest = completed.stdout.replace(report["ciphertext_sha256"], "")
    assert re.search(r"[0-9a-f]{64}", without_digest) is None  # only the ciphertext digest


def test_7l_the_escrow_artifact_opens_with_the_recovery_identity_and_nothing_else(
    tmp_path: Path,
    age_keys: KeyMaterial,
    recovery_identity: RecoveryIdentity,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The single recipient, pinned by decryption: with a throwaway recovery identity substituted
    for the pinned recipient, the artifact decrypts through it to exactly the typed line; the
    host identity — present, valid, 0600 — cannot open it; the header carries one stanza."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    key = _hex("b2 TEST key for the single-recipient pin — never a real one")
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    by_host = _age_decrypt(age_keys.identity, out)
    assert by_host.returncode != 0 and by_host.stdout == b"", "the host identity opened it"
    by_recovery = _age_decrypt(recovery_identity.identity, out)
    assert by_recovery.returncode == 0, by_recovery.stderr
    assert by_recovery.stdout == (key + "\n").encode()
    assert _x25519_stanzas(out) == 1
    assert report["recipients"] == [recovery_identity.public_key]
    assert key not in json.dumps(report)


def test_7m_an_empty_artifact_is_refused_and_named_never_reported_escrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no decrypt on the host, age exiting 0 is not the whole claim: the tool proves the
    artifact it holds is non-empty before it reports. An encrypt step that leaves an empty file
    (forced: the real age runs, then the file is truncated) is ``ARTIFACT_EMPTY``, and — a
    post-create failure — the file is left in place and named for the owner to shred."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    real_encrypt = escrow._encrypt

    def truncating(*args: Any, **kwargs: Any) -> None:
        real_encrypt(*args, **kwargs)
        os.truncate(out, 0)

    monkeypatch.setattr(escrow, "_encrypt", truncating)
    key = _hex("b2 TEST key for the empty artifact — never a real one")
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "ARTIFACT_EMPTY", report
    created = report["created_artifact"]
    assert created["path"] == str(out) and created["removed"] is False
    assert created["identity"] == "{}:{}".format(*_identity(out))
    assert _entries(out.parent) == [out.name] and out.stat().st_size == 0
    assert key not in json.dumps(report)


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
    # K-20261023-003: the host's key files are outside the escrow's trust domain — not looked up
    assert drill.IDENTITY_ENV not in recorder.looked_up
    assert drill.RECIPIENTS_ENV not in recorder.looked_up
    assert "PATH" in recorder.looked_up  # the recorder sees lookups: age is resolved on PATH
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
        "swap-then-digest-fails",
        "swap-then-digest-succeeds",
        "digest-fails",
    ],
)
def test_7g_escrow_failure_cleanup_deletes_nothing_and_names_the_file_this_run_created(
    tmp_path: Path,
    recovery_identity: RecoveryIdentity,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    """Daybreak P1-3 (r1, r2): after the artifact is created, its name is swapped (the created
    file renamed aside, a foreign file planted at the name) and/or the step between the tool's
    two name checks fails (B2: the ciphertext digest — the host-decrypt proof that sat there is
    gone). The tool reads through the descriptor it holds, proves the name still resolves to it
    before success, and after a post-create failure DELETES NOTHING (r3: Linux has no
    unlink-by-descriptor, so any unlink is by name and can hit a swapped-in foreign entry): the
    foreign entry survives, the created ciphertext stays — at its name, or under the renamed
    entry — and the report names it by device:inode for the owner to shred."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    aside = out.with_name("artifact-aside")
    foreign = b"not the file this run created"
    real_digest = escrow._sha256_fd

    def hooked(fd: int) -> str:
        monkeypatch.setattr(escrow, "_sha256_fd", real_digest)
        if shape.startswith("swap"):
            os.rename(out, aside)
            out.write_bytes(foreign)
        if shape.endswith("fails"):
            raise escrow.EscrowRefused("FORCED", "forced: the digest step failed")
        return real_digest(fd)

    monkeypatch.setattr(escrow, "_sha256_fd", hooked)
    key = _hex(f"n3-2 r2 TEST key for {shape} — never a real one")
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    code = escrow.main(["--out", str(out), "--key-from-stdin"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["verdict"] == "REFUSED", report
    expected_code = "TARGET_SWAPPED" if shape == "swap-then-digest-succeeds" else "FORCED"
    assert report["code"] == expected_code, report  # succeeds: the digest read the HELD inode
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
    decrypted = _age_decrypt(recovery_identity.identity, ciphertext)
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
    (rmdir, then the rename back), the new refusal row; the escrow step says the tool deletes
    nothing."""

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
    assert rollback.index("rmdir") < rollback.index("mv --update=none-fail")  # C1 F3's form
    table = text[text.index("## What the gate's refusal codes mean") :]
    assert "`LIVE_NAME_OCCUPIED`" in table


def test_7k_the_runbook_says_the_escrow_tool_deletes_nothing_after_a_failure() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    step6 = text[text.index("6. **Escrow") : text.index("7. **")]
    assert "deletes nothing" in step6 and "shred" in step6 and "removed: true" not in step6


def test_7n_the_runbook_moves_the_decrypt_proof_off_host_before_any_copy_is_deleted() -> None:
    """K-20261023-003: the host cannot decrypt the escrow, so the decrypt-and-compare is Kevin's,
    off-host, through his recovery identity — a required step BEFORE any local ciphertext copy is
    deleted; the page no longer says the host identity can open the artifact."""

    text = RUNBOOK.read_text(encoding="utf-8")
    step6 = text[text.index("6. **Escrow") : text.index("7. **")]
    prose = " ".join(step6.split())
    assert '"decrypt_verified": false' in step6 and '"verified": true' not in step6
    assert "off-host" in prose and "recovery identity" in prose
    compare = step6.index("age -d -i <your recovery identity>")
    assert "ssh <host>" in step6 and "MATCH" in step6
    # the .env value never leaves the host: what crosses ssh is its digest
    remote = re.search(r'ssh <host> "([^"]*)"', step6)
    assert remote is not None and remote.group(1).endswith("| sha256sum"), step6
    assert compare < step6.index("shred -u")  # the proof comes before the deletion
    assert "host identity can also open" not in prose
    assert "decrypts the new artifact through the host identity" not in prose
    rollback = " ".join(text[text.index("## Rollback") :].split())
    assert "(or the host identity)" not in rollback


# B2-FIX1 (C1 F2): the key has ONE home, the checkout's .env, and a counted check proves it is
# nowhere else the units or the shell hand the backend; (C1 F9) the artifact is not a repo file.

#: what each cited line must say (the runbook states the precedence by file:line)
_ONE_HOME_CITATIONS = {
    "src/chronos/config/settings.py": ('env_file=".env"', "case_sensitive=False"),
    "docs/ops/chronos-backend.service": ("WorkingDirectory=", "EnvironmentFile="),
    "docs/ops/chronos-worker.service": ("WorkingDirectory=", "EnvironmentFile="),
}


def _step(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _one_home_block() -> str:
    step5 = _step(RUNBOOK.read_text(encoding="utf-8"), "5. **Generate", "6. **Escrow")
    blocks = [b for b in re.findall(r"```bash\n(.*?)```", step5, re.S) if "ONE HOME: OK" in b]
    assert len(blocks) == 1, "step 5 carries exactly one one-home check block"
    return textwrap.dedent(blocks[0])


def test_7o_the_runbook_names_one_home_for_the_key_and_its_citations_hold() -> None:
    """C1 F2: step 5 names the checkout's .env as the key's only home and says why, by file:line;
    every cited line says what the page claims, and every claim is cited."""

    step5 = _step(RUNBOOK.read_text(encoding="utf-8"), "5. **Generate", "6. **Escrow")
    prose = " ".join(step5.split())
    assert "One home: `<checkout>/.env`, and only there." in prose
    assert "beats" in prose and "case-insensitively" in prose  # the precedence, stated
    assert "settings_customise_sources" in prose  # the library's order, named
    covered: set[tuple[str, str]] = set()
    for path, number in re.findall(r"((?:src|docs)/[\w/.-]+\.(?:py|service)):(\d+)", step5):
        assert path in _ONE_HOME_CITATIONS, path
        line = (ROOT / path).read_text(encoding="utf-8").splitlines()[int(number) - 1]
        said = [needle for needle in _ONE_HOME_CITATIONS[path] if needle in line]
        assert said, f"{path}:{number} says {line!r}"
        covered.update((path, needle) for needle in said)
    wanted = {(path, n) for path, needles in _ONE_HOME_CITATIONS.items() for n in needles}
    assert covered == wanted, wanted - covered


def test_7p_the_process_environment_beats_the_dotenv_file_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precedence the one-home rule rests on (C1 probe P1), pinned so a change to it sends
    someone back to step 5: a value in the process environment — where a unit's EnvironmentFile=
    puts it — wins over the dotenv file, and a lower-case name counts."""

    from chronos.config.settings import Settings

    dotenv = tmp_path / "dotenv-format"  # never a .env: the file Settings is pointed at
    in_file = _hex("b2-fix1 TEST value in the dotenv file — never a real key")
    in_environment = _hex("b2-fix1 TEST value in the process environment — never a real key")
    dotenv.write_text(f"CHRONOS_PERSISTENCE_KEY={in_file}\n", encoding="utf-8")
    monkeypatch.delenv("CHRONOS_PERSISTENCE_KEY", raising=False)
    loaded = Settings(_env_file=dotenv).chronos_persistence_key
    assert loaded is not None and loaded.get_secret_value() == in_file
    monkeypatch.setenv("chronos_persistence_key", in_environment)
    loaded = Settings(_env_file=dotenv).chronos_persistence_key
    assert loaded is not None and loaded.get_secret_value() == in_environment
    # inside the dotenv file a later second form of the name wins over the exact line the
    # step-6 compare reads — why the check also counts every line that names the key
    monkeypatch.delenv("chronos_persistence_key")
    for second in (
        f"chronos_persistence_key={in_environment}",
        f"export CHRONOS_PERSISTENCE_KEY={in_environment}",
    ):
        dotenv.write_text(f"CHRONOS_PERSISTENCE_KEY={in_file}\n{second}\n", encoding="utf-8")
        loaded = Settings(_env_file=dotenv).chronos_persistence_key
        assert loaded is not None and loaded.get_secret_value() == in_environment, second


def _run_one_home_check(
    tmp_path: Path,
    *,
    dotenv: str,
    backend_env: str | None = "",
    worker_env: str = "",
    unit_text: str = "",
    manager_env: str = "",
    shell_env: dict[str, str] | None = None,
    drop_in_env_files: dict[str, str] | None = None,
    units_installed: bool = True,
    shell: str = "bash",
) -> subprocess.CompletedProcess[str]:
    """The step-5 block, run as written with two substitutions: ``<checkout>/.env`` is a file
    named ``dotenv-substitute`` (no .env is ever created) and ``<PRIVATE_ENV_DIR>`` is a temp
    directory; ``systemctl`` is a stub that prints the unit text, the manager environment and
    the units' ``EnvironmentFiles`` in the format systemd 259 prints for ``show -p
    EnvironmentFiles --value`` (measured: one ``<path> (ignore_errors=yes|no)`` line per file, a
    blank line between units; a unit that does not exist prints nothing and exits 0).
    ``drop_in_env_files`` adds files at a third path to the backend's list, as a drop-in
    ``EnvironmentFile=`` would; ``units_installed=False`` lists nothing."""

    checkout, private, stub = tmp_path / "checkout", tmp_path / "private", tmp_path / "bin"
    for directory in (checkout, private, stub):
        directory.mkdir()
    (checkout / "dotenv-substitute").write_text(dotenv, encoding="utf-8")
    if backend_env is not None:
        (private / "chronos-backend.env").write_text(backend_env, encoding="utf-8")
    (private / "chronos-worker.env").write_text(worker_env, encoding="utf-8")
    (tmp_path / "units.txt").write_text(unit_text, encoding="utf-8")
    (tmp_path / "manager.txt").write_text(manager_env, encoding="utf-8")
    backend_files = [private / "chronos-backend.env"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name, content in (drop_in_env_files or {}).items():
        (elsewhere / name).write_text(content, encoding="utf-8")
        backend_files.append(elsewhere / name)
    listing = "".join(f"{path} (ignore_errors=no)\n" for path in backend_files)
    listing += f"\n{private / 'chronos-worker.env'} (ignore_errors=no)\n"
    (tmp_path / "envfiles.txt").write_text(listing if units_installed else "", encoding="utf-8")
    systemctl = stub / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        f'case "$2" in cat) cat {tmp_path / "units.txt"} ;; '
        f"show-environment) cat {tmp_path / 'manager.txt'} ;; "
        f'show) [ "$3 $4 $5" = "-p EnvironmentFiles --value" ] || exit 64; '
        f"cat {tmp_path / 'envfiles.txt'} ;; *) exit 64 ;; esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    block = re.sub(r"(?<![\w/.-])\.env\b", "dotenv-substitute", _one_home_block())
    block = block.replace("<PRIVATE_ENV_DIR>", str(private))
    assert re.search(r"<[A-Z_]+>", block) is None, block
    env = {"PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path), **(shell_env or {})}
    return subprocess.run(
        [shell, "-c", block], cwd=checkout, env=env, capture_output=True, text=True, check=False
    )


_FIX1_KEY = _hex("b2-fix1 TEST key for the one-home check — never a real one")
_FIX1_OTHER = _hex("b2-fix1 TEST other key — never a real one")


@pytest.mark.parametrize(
    ("case", "kwargs", "ok"),
    [
        pytest.param(*row, id=row[0])
        for row in [
            ("only-in-dotenv", {"dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"}, True),
            (
                "a-comment-elsewhere-is-not-a-key-line",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "backend_env": "# CHRONOS_PERSISTENCE_KEY lives in the checkout's .env\n",
                },
                True,
            ),
            (
                "wrong-file-only-in-backend-env",
                {"dotenv": "", "backend_env": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"},
                False,
            ),
            (
                "empty-dotenv-line-key-in-backend-env",
                {
                    "dotenv": "CHRONOS_PERSISTENCE_KEY=\n",
                    "backend_env": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                },
                False,
            ),
            (
                "both-with-different-values",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "backend_env": f"CHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n",
                },
                False,
            ),
            (
                "lower-case-name-in-worker-env",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "worker_env": f"chronos_persistence_key={_FIX1_OTHER}\n",
                },
                False,
            ),
            (
                "two-lines-in-dotenv",
                {
                    "dotenv": (
                        f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\nCHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n"
                    ),
                },
                False,
            ),
            (
                "exact-line-plus-a-lower-case-line-in-dotenv",
                {
                    "dotenv": (
                        f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"
                        f"chronos_persistence_key={_FIX1_OTHER}\n"
                    )
                },
                False,
            ),
            (
                "exact-line-plus-an-export-line-in-dotenv",
                {
                    "dotenv": (
                        f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"
                        f"export CHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n"
                    )
                },
                False,
            ),
            (
                "export-form-in-dotenv",
                {"dotenv": f"export CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"},
                False,
            ),
            (
                "environment-line-in-a-unit",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "unit_text": f"[Service]\nEnvironment=CHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n",
                },
                False,
            ),
            (
                "in-the-user-manager-environment",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "manager_env": f"CHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n",
                },
                False,
            ),
            (
                "exported-in-this-shell",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "shell_env": {"CHRONOS_PERSISTENCE_KEY": _FIX1_KEY},
                },
                False,
            ),
            (
                "a-private-env-file-that-is-not-there",
                {"dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n", "backend_env": None},
                False,
            ),
            # B2-FIX2 (kimi delta r1 P3-1): the block enumerates the units' environment files
            (
                "a-drop-in-third-environment-file-carries-the-key",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "drop_in_env_files": {"other.env": f"CHRONOS_PERSISTENCE_KEY={_FIX1_OTHER}\n"},
                },
                False,
            ),
            (
                "a-drop-in-third-environment-file-without-the-key",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "drop_in_env_files": {"other.env": "OTHER=1\n"},
                },
                False,
            ),
            (
                "the-units-are-not-installed",
                {"dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n", "units_installed": False},
                False,
            ),
            # kimi delta r1's other break attempts, kept as they behaved there
            (
                "a-leading-space-before-the-key-line",
                {"dotenv": f" CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"},
                False,
            ),
            (
                "spaces-around-the-equals-sign",
                {"dotenv": f"CHRONOS_PERSISTENCE_KEY = {_FIX1_KEY}\n"},
                False,
            ),
            (
                "an-inline-comment-naming-the-key-in-a-private-file",
                {
                    "dotenv": f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n",
                    "backend_env": "OTHER=1  # CHRONOS_PERSISTENCE_KEY is in the .env\n",
                },
                False,
            ),
            (
                # passes the check; the step-6 compare then reads the quotes and prints NO MATCH
                "a-quoted-value-passes-the-check",
                {"dotenv": f'CHRONOS_PERSISTENCE_KEY="{_FIX1_KEY}"\n'},
                True,
            ),
        ]
    ],
)
def test_7q_the_one_home_check_passes_only_with_the_key_in_the_dotenv_alone(
    tmp_path: Path, case: str, kwargs: dict[str, Any], ok: bool
) -> None:
    """C1 F2, executed: the step-5 block ends ``ONE HOME: OK`` only when the exact key line is in
    the checkout's .env once and no other source the units or this shell hand the backend names
    it — the wrong-file case (the key only in chronos-backend.env) stops the operator before the
    compare. It prints counts, never the value."""

    completed = _run_one_home_check(tmp_path, **kwargs)
    last = completed.stdout.strip().splitlines()[-1]
    assert last == ("ONE HOME: OK" if ok else "STOP: the key is not in exactly one place"), (
        case,
        completed.stdout,
        completed.stderr,
    )
    for value in (_FIX1_KEY, _FIX1_OTHER):
        assert value not in completed.stdout + completed.stderr, case


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
@pytest.mark.parametrize(
    ("ok", "where"),
    [(True, "dotenv"), (False, "backend_env"), (False, "drop_in")],
    ids=["only-in-dotenv", "wrong-file", "a-drop-in-third-environment-file"],
)
def test_7q_zsh_the_one_home_check_reads_the_same_in_zsh(
    tmp_path: Path, ok: bool, where: str
) -> None:
    line = f"CHRONOS_PERSISTENCE_KEY={_FIX1_KEY}\n"
    kwargs: dict[str, Any] = {
        "dotenv": {"dotenv": line},
        "backend_env": {"dotenv": "", "backend_env": line},
        "drop_in": {"dotenv": line, "drop_in_env_files": {"other.env": line}},
    }[where]
    completed = _run_one_home_check(tmp_path, shell="zsh", **kwargs)
    expected = "ONE HOME: OK" if ok else "STOP: the key is not in exactly one place"
    assert completed.stdout.strip().splitlines()[-1] == expected, completed


def test_7r_step6_runs_the_one_home_check_before_the_escrow_and_before_any_deletion() -> None:
    """C1 F2: the check stands before the escrow command and again between MATCH and the first
    deletion; the compare reads the one home, the file the check counted."""

    step6 = _step(RUNBOOK.read_text(encoding="utf-8"), "6. **Escrow", "7. **")
    checks = [m.start() for m in re.finditer("one-home check", step6)]
    escrow_command = step6.index("python -m chronos.operations.persistence_key_escrow")
    assert any(at < escrow_command for at in checks), "no check before the escrow"
    match, deletion = step6.index("echo MATCH"), step6.index("shred -u")
    assert any(match < at < deletion for at in checks), "no check between MATCH and deletion"
    remote = re.search(r'ssh <host> "([^"]*)"', step6)
    assert remote is not None and "<checkout>/.env" in remote.group(1), step6


def test_7s_the_escrow_artifact_is_written_outside_the_checkout_in_a_private_directory() -> None:
    """C1 F9: ``--out`` names a 0700 directory under the home, made fresh just before the tool
    runs — never a bare name, which lands in the checkout (not ignored; writable by the backend
    unit); the copy, the compare and the shred all use that one path."""

    step6 = _step(RUNBOOK.read_text(encoding="utf-8"), "6. **Escrow", "7. **")
    command = re.search(r"persistence_key_escrow --out (\S+)", step6)
    assert command is not None
    out = command.group(1)
    directory, name = out.rsplit("/", 1) if "/" in out else ("", out)
    assert directory.startswith("~/") and name == "chronos-persistence-key.age", out
    made = step6.index(f"mkdir -m 0700 {directory}")
    assert made < command.start()
    assert f"scp <host>:{directory[2:]}/{name} ." in step6
    assert f"shred -u {out}" in step6 and f"rmdir {directory}" in step6
    assert "<checkout>/chronos-persistence-key.age" not in step6


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
    tmp_path: Path, age_keys: KeyMaterial
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
        # N3-4: the drill removed that copy and its directory; the pool below needs a cleartext
        # copy of the refused store at that path — the by-hand decrypt (the runbook's step 4)
        assert AGE is not None and not Path(restored.target_dir).exists()
        Path(restored.target_dir).mkdir(0o700)
        subprocess.run(
            [
                AGE,
                "-d",
                "-i",
                str(age_keys.identity),
                "-o",
                str(Path(restored.target_dir) / drill.DB_NAME),
                manifest.backup_path,
            ],
            check=True,
            capture_output=True,
        )
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
    recovery_identity: RecoveryIdentity,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Daybreak r2 P1-3, the exact probe: the identity predicate confirms the created artifact
    and, before it returns, the created file is renamed aside and a foreign file planted at the
    name. r2 unlinked the name after the predicate — the foreign entry. r3: after a post-create
    failure the tool deletes nothing, so there is no interval to race — the module performs no
    ``unlink`` at all (B2: the host-decrypt proof and its private scratch are gone; recorded here,
    with the step between the two name checks — the ciphertext digest — forced to fail), the
    predicate is not consulted for a cleanup, the foreign entry (wherever the probe put one)
    survives, and the created ciphertext is reported by device:inode for the owner to shred."""

    from chronos.operations import persistence_key_escrow as escrow

    out = tmp_path / "escrow" / "chronos-persistence-key.age"
    out.parent.mkdir()
    aside = out.with_name("artifact-aside")
    foreign = b"not the file this run created"

    def failing_digest(fd: int) -> str:
        raise escrow.EscrowRefused("FORCED", "forced: the digest step failed")

    monkeypatch.setattr(escrow, "_sha256_fd", failing_digest)
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
    assert code == 2 and report["code"] == "FORCED", report
    assert unlinked == [], unlinked  # nothing was unlinked on the failure path
    assert verdicts == [True]  # the one post-create check; no cleanup consults the name
    created = report["created_artifact"]
    assert created["removed"] is False and "shred" in created["note"]
    ciphertext = aside if aside.exists() else out
    if aside.exists():
        assert out.read_bytes() == foreign  # the foreign entry survives
    assert _entries(out.parent) == sorted(p.name for p in (out, aside) if p.exists())
    assert created["identity"] == "{}:{}".format(*_identity(ciphertext))
    decrypted = _age_decrypt(recovery_identity.identity, ciphertext)
    assert decrypted.stdout == (key + "\n").encode()  # the created ciphertext, intact
    assert key not in json.dumps(report)
    # and statically: the module deletes nothing at all — no scratch since B2 removed the
    # host-decrypt proof, never the artifact, never by name
    tree = ast.parse(ESCROW_MODULE.read_text(encoding="utf-8"))
    deleters = sorted(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("unlink", "remove", "rmdir", "rmtree", "unlink_created")
    )
    assert deleters == [], deleters


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


@pytest.mark.parametrize("alias", ["symlink-dotdot", "plain-dotdot"])
def test_12d_daybreak_s_counterexample_a_dotdot_alias_is_refused_before_anything_is_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], alias: str
) -> None:
    """Daybreak r5 P1-1, the exact counterexample: POSIX resolves ``safe/link/../data/chronos.db``
    THROUGH the link (``safe/link -> unsafe/child``, then ``..`` is ``unsafe``), reaching the
    real retired store under a world-writable ``unsafe/``, while a lexical ``normpath`` collapses
    the string to ``safe/data/chronos.db`` — an owner-private decoy chain. r6: nothing is
    normalised away; a ``..`` component is refused outright (``PATH_NOT_PROTECTED``, naming it),
    before the source is opened — dry-run and apply alike: no gate, no envelope, no output or
    restore directory, not even reader bookkeeping beside either store, and no exception out of
    the command (the plain ``..`` alias, harmless in itself, is refused the same way: give the
    real path)."""

    unsafe = _store(tmp_path / "unsafe", binding="closed")  # the real store, under a 0777 dir
    os.chmod(tmp_path / "unsafe", 0o777)
    (tmp_path / "unsafe" / "child").mkdir(0o700)
    decoy = _store(tmp_path / "safe", binding="closed")  # an owner-private chain with a store
    if alias == "symlink-dotdot":
        os.symlink(tmp_path / "unsafe" / "child", tmp_path / "safe" / "link")
        db_path = tmp_path / "safe" / "link" / ".." / "data" / "chronos.db"
        assert os.path.realpath(db_path) == str(unsafe.db)  # the kernel's view: the unsafe store
    else:
        (tmp_path / "safe" / "x").mkdir(0o700)
        db_path = tmp_path / "safe" / "x" / ".." / "data" / "chronos.db"
        assert os.path.realpath(db_path) == str(decoy.db)
    assert ".." in db_path.parts
    before = (_sha256(unsafe.db), _sha256(decoy.db))
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
        code = cutover.main(argv, clock=_clock(unsafe.now))  # returns; never raises
        report = json.loads(capsys.readouterr().out)
        assert code == 2 and report["code"] == "PATH_NOT_PROTECTED", report
        assert report["gate"] is None and report["envelope"] is None
        assert ".." in report["failures"][0]
    assert not (tmp_path / "backups").exists() and not (tmp_path / "restored").exists()
    assert (_sha256(unsafe.db), _sha256(decoy.db)) == before
    assert _entries(unsafe.db.parent) == ["chronos.db"]  # not even reader bookkeeping
    assert _entries(decoy.db.parent) == ["chronos.db"]


def test_12e_the_move_never_re_walks_the_path_by_name() -> None:
    """The held chain is the one descriptor set the command uses: the source is opened relative
    to its leaf and the move works through that same leaf, so no re-walk by name exists in the
    module — and no late, untyped refusal can arise from one."""

    source = MODULE.read_text(encoding="utf-8")
    assert "_walk_directory" not in source
    assert "dir_fd=protected.leaf_fd" in source  # the source open, relative to the held leaf


# ------------------------------------------------------------- N3-4: the drill's copy is gone
# Kevin's ruling K-20261023-002 (P2-4 option B): the verify copy under --restore-into does not
# outlive the command. The drill removes it through the descriptors it held; the command reports
# it (``drill.restore_copy_removed``, ``drill.removed``) and, when the drill could not, refuses
# before anything moves — the envelope stays and is named.


def _refuse_removal_unlink_once(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[str]:
    """``os.unlink`` as the drill sees it refuses ``chronos.db`` (EPERM) the first time its
    ``dir_fd`` designates ``target`` — the removal's own unlink; everything else, the rpo scratch
    included, keeps working. Returns the list the refusal is recorded in."""

    real_unlink = os.unlink
    refused: list[str] = []

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        if dir_fd is not None and path == "chronos.db" and not refused and target.exists():
            held, by_path = os.fstat(dir_fd), os.stat(target)
            if (held.st_dev, held.st_ino) == (by_path.st_dev, by_path.st_ino):
                refused.append(path)
                raise PermissionError(errno.EPERM, "Operation not permitted")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(drill.os, "unlink", unlink)
    return refused


def test_n34_1_apply_leaves_no_restore_copy_and_the_report_and_the_next_steps_say_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, binding="closed")
    monkeypatch.setattr(socket, "socket", _NoSocket)  # no broker, no network: proven, not said
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and report["verdict"] == "ARCHIVED", report
    assert report["drill"]["verdict"] == "ARCHIVE_VERIFIED"
    assert report["drill"]["restore_copy_removed"] is True
    assert report["drill"]["removed"] == ["chronos.db"]  # no audit pair travels with this store
    assert report["restore_dir"] == str(tmp_path / "restored")  # where it was
    assert not (tmp_path / "restored").exists()
    assert Path(report["envelope"]).is_dir()  # the envelope stays
    first = report["next_manual_steps"][0]
    assert "restore_copy_removed" in first and "ls -ld" in first
    assert not any("rm -r" in step for step in report["next_manual_steps"])


def test_n34_2_a_copy_the_drill_could_not_remove_refuses_typed_before_anything_moves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, binding="closed")
    before = _sha256(store.db)
    target = tmp_path / "restored"
    refused = _refuse_removal_unlink_once(monkeypatch, target)
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert refused == ["chronos.db"]
    assert code == 2 and report["verdict"] == "REFUSED", report
    assert report["code"] == "RESTORE_COPY_NOT_REMOVED"
    assert len(report["failures"]) == 1
    assert report["failures"][0].startswith("drill: RESTORE_COPY_NOT_REMOVED: chronos.db")
    # the archive is good and named, the drill's verifiers passed, the copy is still there
    assert Path(report["envelope"]).is_dir() and report["manifest_sha256"]
    assert report["drill"]["acceptance"] == f"archive:schema_version:{SCHEMA_VERSION - 1}"
    assert report["drill"]["restore_copy_removed"] is False and report["drill"]["removed"] == []
    assert sorted(p.name for p in target.iterdir()) == ["chronos.db"]
    # nothing moved, no sentinel, the live store untouched
    assert report["moved_aside"] == [] and report["sentinels"] == []
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)
    # the runbook's instruction: remove the copy by hand, re-run — the next run makes a new
    # envelope and stamp, the leaf being absent again
    shutil.rmtree(target)
    later = store.now + timedelta(minutes=1)
    argv = [
        "--db",
        str(store.db),
        "--out",
        str(tmp_path / "backups"),
        "--restore-into",
        str(target),
        "--apply",
    ]
    code = cutover.main(argv, clock=_clock(later))
    again = json.loads(capsys.readouterr().out)
    assert code == 0 and again["verdict"] == "ARCHIVED", again
    assert again["drill"]["restore_copy_removed"] is True and not target.exists()
    envelopes = sorted(
        p.name for p in (tmp_path / "backups").iterdir() if not p.name.startswith(".")
    )
    assert len(envelopes) == 2 and Path(report["envelope"]).name in envelopes


def test_n34_3_the_cutover_runbook_confirms_the_removal_instead_of_asking_for_an_rm() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    steps = text.split("## Steps", 1)[1]
    step4 = steps.split("\n4. **", 1)[1].split("\n5. **", 1)[0]
    assert "restore_copy_removed" in step4 and "ls -ld /var/backups/chronos/n3-verify" in step4
    assert "rm -r /var/backups/chronos/n3-verify" not in step4
    assert "the command never deletes" not in step4
    assert "`RESTORE_COPY_NOT_REMOVED`" in steps  # step 3's bullet and the refusal table
    table = text.split("## What the gate's refusal codes mean", 1)[1]
    assert "| `RESTORE_COPY_NOT_REMOVED` |" in table
    # the "Before you start" rule says the copy is removed before the report
    before = text.split("## Before you start", 1)[1].split("## Steps", 1)[0]
    assert "restore_copy_removed" in before


# ------------------------------------------------------------- (12) C1-F: the rehearsal's findings
# C1 (out/C1/REHEARSAL.md, 2026-10-24) walked the runbook on synthetic state and found nine things;
# F2 and F9 went into B2. These pin F1, F3-F8: the runbook says what the host really does, and the
# dry run proves what a clean dry run is read as proving.


def _coreutils_version() -> tuple[int, ...] | None:
    """``mv --version``'s first line — ``mv (GNU coreutils) 9.7`` → ``(9, 7)`` — or None when
    ``mv`` is not GNU coreutils (the runbook's forms are GNU's)."""

    completed = subprocess.run(["mv", "--version"], capture_output=True, text=True, check=False)
    first = completed.stdout.splitlines()[0] if completed.stdout else ""
    if "GNU coreutils" not in first:
        return None
    return tuple(int(part) for part in first.rsplit(" ", 1)[-1].split(".") if part.isdigit())


def _rollback() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    return text[text.index("## Rollback") : text.index("## What the gate's refusal codes mean")]


def test_c1f_f3_the_rollback_renames_back_with_a_loud_refusal_and_checks_after() -> None:
    """C1 F3 (P2): GNU ``mv -n`` onto an existing regular file declines SILENTLY with exit 0
    (measured on coreutils 9.7, RB2), so a rollback run after the backend has created the keyed
    store looks done and is not. The runbook's rename is ``mv --update=none-fail`` (coreutils
    9.5+: ``not replacing``, exit 1), names that version assumption and what an older coreutils
    does with the flag, and makes the operator check ``ls -la data/`` after the moves."""

    rollback = _rollback()
    assert "`mv --update=none-fail data/chronos.db.archived-<stamp> data/chronos.db`" in rollback
    assert "mv -n data/" not in rollback, "the silent form must not be the instruction"
    assert "not replacing" in rollback  # what the refusal prints
    assert "coreutils" in rollback and "9.5" in rollback  # the version assumption, stated
    assert "mv --version" in rollback  # and how to check it
    assert "no `.archived-<stamp>` name" in rollback  # the after-check
    assert rollback.index("rmdir") < rollback.index("mv --update=none-fail")  # r3's order holds


def test_c1f_f3_measured_the_chosen_mv_form_refuses_an_occupied_live_name_and_the_old_one_did_not(
    tmp_path: Path,
) -> None:
    """The premise, re-measured where the suite runs: ``mv -n`` onto an existing file is silent
    and exits 0; ``mv --update=none-fail`` prints ``not replacing`` and exits 1; both leave
    both files untouched; the chosen form still moves when the live name is free."""

    version = _coreutils_version()
    if version is None or version < (9, 5):
        pytest.skip(f"--update=none-fail needs GNU coreutils 9.5+ (mv --version: {version})")
    data = tmp_path / "data"
    data.mkdir()
    live, archived = data / "chronos.db", data / "chronos.db.archived-X"
    live.write_bytes(b"keyed store the backend created")
    archived.write_bytes(b"the pre-cutover store")

    silent = subprocess.run(
        ["mv", "-n", str(archived), str(live)], capture_output=True, text=True, check=False
    )
    assert silent.returncode == 0 and silent.stderr == "" and silent.stdout == ""  # F3
    assert live.read_bytes() == b"keyed store the backend created"
    assert archived.read_bytes() == b"the pre-cutover store"

    loud = subprocess.run(
        ["mv", "--update=none-fail", str(archived), str(live)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert loud.returncode == 1 and "not replacing" in loud.stderr, loud
    assert live.read_bytes() == b"keyed store the backend created"
    assert archived.read_bytes() == b"the pre-cutover store"

    live.unlink()  # the live name is free (the sentinel was rmdir'ed, nothing started)
    moved = subprocess.run(
        ["mv", "--update=none-fail", str(archived), str(live)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert moved.returncode == 0 and moved.stderr == "", moved
    assert live.read_bytes() == b"the pre-cutover store" and not archived.exists()


def _step8() -> tuple[str, str]:
    """Step 8's text and its one ```bash block."""

    step8 = _step(RUNBOOK.read_text(encoding="utf-8"), "8. **Bind", "9. **")
    blocks = re.findall(r"```bash\n(.*?)```", step8, re.S)
    assert len(blocks) == 1, "step 8 carries one check block"
    # the fence sits inside a numbered list, so the raw lines carry the list's indent; a reader
    # copies the rendered block, which is dedented (the one-home pin reads step 5 the same way)
    return step8, textwrap.dedent(blocks[0])


def _step8_fingerprint_body(*, dotenv_name: str) -> str:
    """The body of step 8's ``python -I -c '…' DU1234567`` line, with the runbook's ``.env``
    renamed to ``dotenv_name`` (no test creates a ``.env``) and this tree's ``src`` put first on
    the import path (``-I`` ignores PYTHONPATH; the venv's editable install may point elsewhere).
    Nothing else changes: the key handling under test is the runbook's own."""

    block = _step8()[1]
    match = re.search(r"python -I -c '(.*?)' " + re.escape(DEMO_ACCOUNT_ID), block, re.S)
    assert match, block
    body = match.group(1)
    assert body.count('".env"') == 2, body
    body = body.replace('".env"', f'"{dotenv_name}"')
    return f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r})\n" + body


def _run_step8_fingerprint(
    cwd: Path, *, isolated: bool, env: dict[str, str], dotenv_name: str = "dotenv-format"
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, *(["-I"] if isolated else []), "-c"]
    argv += [_step8_fingerprint_body(dotenv_name=dotenv_name), DEMO_ACCOUNT_ID]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_c1f_f1_step_8_proves_the_bind_from_the_file_key_by_construction(tmp_path: Path) -> None:
    """C1 F1 (P2) under the code owner's constraint (out/KEYSRC-ASSESS.md §3) and kimi's P1-1
    (C1-F review): step 8 is the end-to-end proof that the store bound under the ESCROWED key —
    the fingerprint computed from ``<checkout>/.env``'s key alone, compared with the bound
    ``database_scope`` row. On a demo host ``b1-preflight`` cannot give it (it refuses before its
    scope line, and its scope CLEAR is ``("ibkr", "paper", …)`` by construction,
    ``b1_preflight.py:564``). ``env -i`` was not a file-only proof either: Python startup code
    (a ``.pth``, ``sitecustomize``) can set the key in the process environment AFTER ``env`` has
    emptied it, and Settings then prefers that value (kimi's counterexample). So step 8's demo
    check reads the file BY CONSTRUCTION: a ``python -I -c`` command over the repo's own
    functions that loads ``.env`` with ``dotenv_values``, takes the one exact
    ``CHRONOS_PERSISTENCE_KEY=`` line, computes ``account_fingerprint`` and never consults the
    environment. The runbook states the trust root (the checkout's venv and source are the code
    the backend runs). No "or the operator CLI you use"; a mismatch after the first start is named
    for what it is with the one remedy."""

    step8, block = _step8()
    assert "or the operator CLI you use" not in step8
    assert "mode=ro" in block and "FROM database_scope" in block
    assert "python -I -c" in block and "dotenv_values" in block and "account_fingerprint(" in block
    assert "CHRONOS_PERSISTENCE_KEY=" in block and "STOP" in block
    # kimi delta r1 P2-1: dotenv_values interpolates `${VAR}` from os.environ by default, so the
    # read must be literal; the 64-hex check then refuses a `${…}` line instead of an env value
    assert 'dotenv_values(".env", interpolate=False)' in block, block
    assert "FileNotFoundError" in block  # a missing .env is a STOP line, not a traceback
    for shell_source in ("env -i", "mandate fingerprint", "b1-preflight", "os.environ", "getenv"):
        assert shell_source not in block, shell_source
    assert "CLEAR [scope] already bound to this paper account" in step8  # the IBKR-paper check
    assert "DEMO_ACCOUNT_ID" in step8 and "src/chronos/broker/demo.py" in step8
    assert "IB_ACCOUNT_ID" in step8
    flat = " ".join(step8.split())
    assert ".pth" in flat and "sitecustomize" in flat and "process environment" in flat
    assert "bound under a key other than the escrowed one" in flat
    assert "remove the extra source" in flat and "re-archive the fresh store" in flat
    assert "Never change `DATABASE_URL`" in flat
    demo_module = (ROOT / "src" / "chronos" / "broker" / "demo.py").read_text(encoding="utf-8")
    assert f'DEMO_ACCOUNT_ID = "{DEMO_ACCOUNT_ID}"' in demo_module  # the citation holds
    scope_check = (ROOT / "src" / "chronos" / "cli" / "b1_preflight.py").read_text("utf-8")
    assert 'tuple(row) == ("ibkr", "paper", account_fingerprint(account, key=key))' in scope_check
    # the SQL the step gives is valid against the real schema and reads exactly the scope row
    match = re.search(r'"(SELECT [^"]*FROM database_scope[^"]*)"', block)
    assert match, block
    store = _store(tmp_path, binding="closed")
    with closing(sqlite3.connect(f"{store.db.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(match.group(1)).fetchall()
    assert len(rows) == 1 and rows[0][0] == "demo" and len(rows[0][2]) == 64, rows
    sqlite3_cli = shutil.which("sqlite3")
    if sqlite3_cli is not None:  # the exact shell line, with the store's path substituted
        line = next(entry.strip() for entry in block.splitlines() if "sqlite3 " in entry)
        argv = shlex.split(line.replace("data/chronos.db", str(store.db)))
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert completed.returncode == 0 and completed.stdout.startswith("demo|"), completed


def test_c1f_f1_the_step_8_fingerprint_comes_from_the_file_whatever_the_environment_holds(
    tmp_path: Path,
) -> None:
    """kimi P1-1's counterexample, pinned: a key set in the process environment — by the shell,
    or by Python startup code after ``env -i`` — must not change the fingerprint step 8 prints.
    (1) ``-I`` and an exported other key: the file's fingerprint. (2) The exact startup vector
    kimi used, a ``sitecustomize`` that sets the key (here on PYTHONPATH, without ``-I``, since no
    test may write into the venv's site-packages): still the file's fingerprint — while the OLD
    form (``mandate fingerprint``) under the same injection prints the INJECTED key's, the hole.
    (3) STOP cases: no key line, a doubled line, a second form, a malformed value — exit 1,
    "STOP" on stderr, nothing on stdout, and the key never printed. (4) kimi delta r1 P2-1: an
    exact line whose value is ``${VAR}`` must STOP, not print the fingerprint of the environment's
    VAR — whether VAR comes from the shell or from startup code (``dotenv_values`` interpolates
    from ``os.environ`` unless told not to). (5) A missing ``.env`` is a ``STOP:`` line, exit 1,
    never a traceback."""

    file_key = _hex("c1f-fix1: the key the .env holds — never a real key")
    other_key = _hex("c1f-fix1: a key injected into the process environment — never real")
    expected = account_fingerprint(DEMO_ACCOUNT_ID, key=PersistenceKey.from_hex(file_key))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    dotenv = checkout / "dotenv-format"  # never a .env: the file the command is pointed at
    dotenv.write_text(f"CHRONOS_PERSISTENCE_KEY={file_key}\n", encoding="utf-8")
    base_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CHRONOS_")}
    base_env.update(SAFE_ENV)
    # (1) the shell exports another key; -I; the printed fingerprint is the FILE's
    exported = _run_step8_fingerprint(
        checkout, isolated=True, env={**base_env, "CHRONOS_PERSISTENCE_KEY": other_key}
    )
    assert exported.returncode == 0, exported
    assert exported.stdout.splitlines() == [expected], exported
    # (2) kimi's vector: startup code sets the key after the environment was emptied
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        f'import os\nos.environ["CHRONOS_PERSISTENCE_KEY"] = "{other_key}"\n', encoding="utf-8"
    )
    injected_env = {**base_env, "PYTHONPATH": f"{site}{os.pathsep}{ROOT / 'src'}"}
    injected = _run_step8_fingerprint(checkout, isolated=False, env=injected_env)
    assert injected.returncode == 0 and injected.stdout.splitlines() == [expected], injected
    old_argv = [sys.executable, "-m", "chronos.cli", "mandate", "fingerprint"]
    old_argv += ["--account-id", DEMO_ACCOUNT_ID]
    old_form = subprocess.run(  # the control: the form the review broke takes the injected key
        old_argv,
        cwd=checkout,
        env=injected_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert old_form.returncode == 0, old_form
    assert old_form.stdout.splitlines()[0] == account_fingerprint(
        DEMO_ACCOUNT_ID, key=PersistenceKey.from_hex(other_key)
    )
    # (3) STOP cases — exit 1, STOP on stderr, nothing printed, the key never echoed
    stops = {
        "missing": "OTHER=1\n",
        "doubled": f"CHRONOS_PERSISTENCE_KEY={file_key}\nCHRONOS_PERSISTENCE_KEY={file_key}\n",
        "second-form": (
            f"CHRONOS_PERSISTENCE_KEY={file_key}\nexport CHRONOS_PERSISTENCE_KEY={other_key}\n"
        ),
        "lower-case": f"CHRONOS_PERSISTENCE_KEY={file_key}\nchronos_persistence_key={other_key}\n",
        "malformed": f"CHRONOS_PERSISTENCE_KEY={file_key[:63]}\n",
    }
    for case, text in stops.items():
        dotenv.write_text(text, encoding="utf-8")
        stopped = _run_step8_fingerprint(checkout, isolated=True, env=base_env)
        assert stopped.returncode == 1, (case, stopped)
        assert stopped.stdout == "" and stopped.stderr.startswith("STOP"), (case, stopped)
        assert file_key[:20] not in stopped.stderr and other_key[:20] not in stopped.stderr, case
    # (4) interpolation: the file's exact line is `${VAR}`; VAR holds a well-formed key in the
    # environment (the shell, then kimi's startup vector) — the file is read literally, so STOP
    dotenv.write_text("CHRONOS_PERSISTENCE_KEY=${C1F_TEST_VAR}\n", encoding="utf-8")
    interpolated = _run_step8_fingerprint(
        checkout, isolated=True, env={**base_env, "C1F_TEST_VAR": other_key}
    )
    assert interpolated.returncode == 1, interpolated
    assert interpolated.stdout == "" and interpolated.stderr.startswith("STOP"), interpolated
    (site / "sitecustomize.py").write_text(
        f'import os\nos.environ["C1F_TEST_VAR"] = "{other_key}"\n', encoding="utf-8"
    )
    via_startup = _run_step8_fingerprint(checkout, isolated=False, env=injected_env)
    assert via_startup.returncode == 1, via_startup
    assert via_startup.stdout == "" and via_startup.stderr.startswith("STOP"), via_startup
    # (5) no .env at all: a STOP line, exit 1, no traceback (fail-closed AND clear)
    dotenv.unlink()
    absent = _run_step8_fingerprint(checkout, isolated=True, env=base_env)
    assert absent.returncode == 1 and absent.stdout == "", absent
    assert absent.stderr.startswith("STOP") and "Traceback" not in absent.stderr, absent


def test_c1f_f4_the_sentinel_refusal_wording_matches_what_mv_prints(tmp_path: Path) -> None:
    """C1 F4 (P3): a ``mv`` onto a sentinel directory does not say ``Is a directory``: ``mv``
    aims INSIDE the directory (``data/chronos.db/chronos.db.archived-<stamp>``) and the
    sentinel's 0500 mode refuses it with ``Permission denied`` (RB1a). The runbook says what
    ``mv`` really prints — in step 3 and in Rollback — and the measurement is repeated here."""

    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Is a directory" not in text
    assert "Permission denied" in _rollback()
    assert "Permission denied" in text[text.index("3. **Apply") : text.index("4. **Remove")]
    if os.geteuid() == 0:
        pytest.skip("root bypasses the sentinel's 0500 mode")
    version = _coreutils_version()
    if version is None or version < (9, 5):
        pytest.skip(f"the runbook's mv form needs GNU coreutils 9.5+ (mv --version: {version})")
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "chronos.db"
    sentinel.mkdir()
    os.chmod(sentinel, 0o500)
    archived = data / "chronos.db.archived-X"
    archived.write_bytes(b"the pre-cutover store")
    try:
        refused = subprocess.run(
            ["mv", "--update=none-fail", str(archived), str(sentinel)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.chmod(sentinel, 0o700)
    assert refused.returncode == 1, refused
    assert "Permission denied" in refused.stderr and "Is a directory" not in refused.stderr
    assert f"{sentinel}/chronos.db.archived-X" in refused.stderr  # it aimed inside the directory
    assert archived.read_bytes() == b"the pre-cutover store" and list(sentinel.iterdir()) == []


def test_c1f_f5_the_dry_run_refuses_an_unprotected_restore_target_as_apply_does_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C1 F5 (P3): the dry run returned ``DRY_RUN`` before the ``--restore-into`` check, so a
    ``/tmp`` target passed step 2 and only ``--apply`` refused it (S2-ctl vs S2-ctl-apply): a
    clean dry run said nothing about the target. The dry run now runs the same read-only check
    (``_require_protected_absent_target``) and refuses the same way, before anything exists."""

    store = _store(tmp_path, binding="closed")
    sticky = tmp_path / "sticky-world-writable"  # /tmp-style, made here (see test_5f for why)
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    target = sticky / "verify"
    before = _sha256(store.db)
    argv = [
        "--db",
        str(store.db),
        "--out",
        str(tmp_path / "backups"),
        "--restore-into",
        str(target),
    ]  # no --apply: the dry run
    code = cutover.main(argv, clock=_clock(store.now))
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["verdict"] == "REFUSED" and report["dry_run"] is True, report
    assert report["code"] == "RESTORE_TARGET_NOT_PROTECTED", report
    assert f"{sticky} is writable by its group or by others (mode 1777)" in report["failures"][0]
    assert not target.exists() and not (tmp_path / "backups").exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)
    # --apply refuses identically (the control the rehearsal had): one check, both modes
    code = cutover.main([*argv, "--apply"], clock=_clock(store.now))
    applied = json.loads(capsys.readouterr().out)
    assert code == 2 and applied["code"] == "RESTORE_TARGET_NOT_PROTECTED", applied
    assert applied["failures"] == report["failures"] and applied["dry_run"] is False
    assert not target.exists() and not (tmp_path / "backups").exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


def test_c1f_f5_the_dry_run_refuses_a_restore_target_that_exists_and_leaves_it_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The leaf must be ABSENT (FIX-RT) — for the dry run too. It is read-only: the pre-existing
    directory keeps its mode and stays empty; nothing under ``--out`` appears."""

    store = _store(tmp_path, binding="closed")
    target = tmp_path / "restored"  # where _run points --restore-into
    target.mkdir()
    os.chmod(target, 0o700)
    before = _sha256(store.db)
    code, report = _run(store, tmp_path, capsys=capsys)
    assert code == 2 and report["verdict"] == "REFUSED" and report["dry_run"] is True, report
    assert report["code"] == "RESTORE_TARGET_NOT_FRESH", report
    assert "must not exist" in report["failures"][0]
    assert list(target.iterdir()) == [] and stat.S_IMODE(target.stat().st_mode) == 0o700
    assert not (tmp_path / "backups").exists()
    _assert_only_reader_bookkeeping(store, ["chronos.db"], before)


def test_c1f_f5_the_module_and_the_runbook_say_the_dry_run_proves_the_restore_target() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    step2 = _step(text, "2. **Dry-run", "3. **Apply")
    assert "`RESTORE_TARGET_NOT_PROTECTED`" in step2 and "`RESTORE_TARGET_NOT_FRESH`" in step2
    before = text[text.index("## Before you start") : text.index("## Steps")]
    assert "dry run and `--apply` alike, before any envelope exists" in before
    module = MODULE.read_text(encoding="utf-8")
    assert "Gate → restore-target check → (dry-run stops here)" in module


def test_c1f_f6_the_dry_run_s_next_steps_say_what_apply_will_do_not_what_it_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C1 F6 (P3): the DRY_RUN report's ``next_manual_steps`` spoke in the past tense of an apply
    ("this run removed the CLEARTEXT copy it verified…", "printed above") while its ``drill``
    and ``envelope`` were null (S2-dryrun). A dry run's steps describe what ``--apply`` WILL do;
    the apply report keeps the wording the N3-4 pin reads; the tense-free steps are shared."""

    store = _store(tmp_path, binding="closed")
    code, report = _run(store, tmp_path, capsys=capsys)
    assert code == 0 and report["verdict"] == "DRY_RUN", report
    assert report["drill"] is None and report["envelope"] is None
    steps = report["next_manual_steps"]
    assert len(steps) == 7, steps
    assert steps[0].startswith("--apply will"), steps[0]
    for step in steps:
        assert "this run" not in step and "printed above" not in step, step
        assert "above is true" not in step and "rm -r" not in step, step
    assert "ls -ld" in steps[0] and str(tmp_path / "restored") in steps[0]
    assert "--apply will" in steps[1] and "record" in steps[1]
    assert any("rmdir" in step and "--apply will leave" in step for step in steps), steps
    assert "--apply makes" in steps[6]
    # the apply report says what it did, as test_n34_1 pins; the tense-free steps are the same
    code, applied = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0 and applied["verdict"] == "ARCHIVED", applied
    done = applied["next_manual_steps"]
    assert len(done) == len(steps)
    assert "this run removed" in done[0] and "restore_copy_removed" in done[0]
    assert all("--apply will" not in step for step in done), done
    assert done[2:4] == steps[2:4] and done[5] == steps[5]


def test_c1f_f7_step_9_names_the_account_id_argument_and_where_its_value_comes_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C1 F7 (P3): on a demo host ``mandate fingerprint`` without ``--account-id`` stops with
    ``no account id available; pass --account-id or set IB_ACCOUNT_ID`` (S9a); with the demo
    account id it prints the keyed value that equals the bound scope row (S9b). Step 9 names the
    argument for both commands and where its value comes from on each broker; the premise is
    re-measured in-process against the CLI."""

    text = RUNBOOK.read_text(encoding="utf-8")
    step9 = _step(text, "9. **Re-author", "10. **Retention")
    flat = " ".join(step9.split())
    assert f"mandate fingerprint --account-id {DEMO_ACCOUNT_ID}" in flat
    assert f"mandate check --file <mandate> --account-id {DEMO_ACCOUNT_ID}" in flat
    assert "DEMO_ACCOUNT_ID" in step9 and "src/chronos/broker/demo.py" in step9
    assert "IB_ACCOUNT_ID" in step9  # the IBKR-paper source, and what the bare command reads
    assert "no account id available; pass --account-id or set IB_ACCOUNT_ID" in step9
    assert "account-match" in step9 and "0 blocking" in step9 and "STATUS" in step9
    # the premise: the bare command fails on a host without IB_ACCOUNT_ID; --account-id works
    monkeypatch.setenv("CHRONOS_PERSISTENCE_KEY", TEST_PERSISTENCE_KEY_HEX)
    monkeypatch.delenv("IB_ACCOUNT_ID", raising=False)
    get_settings.cache_clear()
    try:
        assert cli_main(["mandate", "fingerprint"]) == 1
        bare = capsys.readouterr().out
        assert "no account id available; pass --account-id or set IB_ACCOUNT_ID" in bare
        assert cli_main(["mandate", "fingerprint", "--account-id", DEMO_ACCOUNT_ID]) == 0
        printed = capsys.readouterr().out.splitlines()
    finally:
        get_settings.cache_clear()
    assert re.fullmatch(r"[0-9a-f]{64}", printed[0]), printed
    assert printed[1].startswith("# derived from --account-id")
    assert DEMO_ACCOUNT_ID not in printed[1]  # the id is masked, never echoed whole


_ROLLBACK_FALLBACK = (
    "ln -T data/chronos.db.archived-<stamp> data/chronos.db && rm data/chronos.db.archived-<stamp>"
)


def test_c1f_fix1_the_rollback_has_no_check_then_move_and_its_fallback_links_atomically(
    tmp_path: Path,
) -> None:
    """kimi P1-2 (C1-F review): the older-coreutils fallback ``test ! -e … && mv …`` was a
    check-then-move — a live name created between the test and the move is overwritten by the
    plain ``mv`` (kimi's interleaving). The Rollback carries no check-then-move anywhere now: the
    primary form stays ``mv --update=none-fail`` (one ``renameat2(RENAME_NOREPLACE)``) and the
    fallback is ``ln -T <archived> <live> && rm <archived>`` — ``link(2)`` refuses an existing
    name inside the one call, nothing is renamed or unlinked over the live name, ``-T`` refuses a
    directory at the live name instead of linking inside it, and the ``&&`` keeps the archived
    name on any refusal. Measured here with the runbook's own line."""

    rollback = _rollback()
    for check in ("test ! -e", "test -e", "[ ! -e", "[ -e", "-e data/chronos.db ]"):
        assert check not in rollback, check
    assert f"`{_ROLLBACK_FALLBACK}`" in rollback
    assert "RENAME_NOREPLACE" in rollback and "link(2)" in rollback
    data = tmp_path / "data"
    data.mkdir()
    live, archived = data / "chronos.db", data / "chronos.db.archived-X"
    line = _ROLLBACK_FALLBACK.replace("<stamp>", "X")
    # kimi's interleaving: the live name exists by the time the operation runs — after any check
    # it could have made — so the link refuses, the && keeps the archived name, nothing changes
    live.write_bytes(b"fresh keyed store")
    archived.write_bytes(b"the pre-cutover store")
    refused = subprocess.run(
        ["bash", "-c", line], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert refused.returncode != 0 and "exists" in refused.stderr.lower(), refused
    assert live.read_bytes() == b"fresh keyed store"
    assert archived.read_bytes() == b"the pre-cutover store"
    # a sentinel directory at the live name: refused, never linked inside, archived name kept
    live.unlink()
    live.mkdir()
    os.chmod(live, 0o500)
    try:
        sentinel = subprocess.run(
            ["bash", "-c", line], cwd=tmp_path, capture_output=True, text=True, check=False
        )
    finally:
        os.chmod(live, 0o700)
    assert sentinel.returncode != 0, sentinel
    assert list(live.iterdir()) == [] and archived.read_bytes() == b"the pre-cutover store"
    # the live name free: linked, then the archived name removed — one file, the archived bytes
    live.rmdir()
    moved = subprocess.run(
        ["bash", "-c", line], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert moved.returncode == 0 and moved.stderr == "", moved
    assert sorted(entry.name for entry in data.iterdir()) == ["chronos.db"]
    assert live.read_bytes() == b"the pre-cutover store"


def test_c1f_fix1_measured_both_rollback_forms_are_one_no_clobber_kernel_call(
    tmp_path: Path,
) -> None:
    """Why neither rollback form can overwrite: on an occupied live name ``mv --update=none-fail``
    issues ONE ``renameat2`` with ``RENAME_NOREPLACE`` and ``ln -T`` ONE ``link``/``linkat`` —
    the kernel refuses the existing name inside that call (EEXIST) and neither issues a rename
    or unlink of the live name, so there is no check a concurrent creator could race. Measured
    with strace; skipped where strace or coreutils 9.5+ is absent."""

    strace = shutil.which("strace")
    if strace is None:
        pytest.skip("strace is not on PATH")
    version = _coreutils_version()
    if version is None or version < (9, 5):
        pytest.skip(f"--update=none-fail needs GNU coreutils 9.5+ (mv --version: {version})")
    data = tmp_path / "data"
    data.mkdir()
    live, archived = data / "chronos.db", data / "chronos.db.archived-X"
    live.write_bytes(b"fresh keyed store")
    archived.write_bytes(b"the pre-cutover store")
    calls = "trace=rename,renameat,renameat2,link,linkat,unlink,unlinkat"

    def traced(*argv: str) -> list[str]:
        trace = tmp_path / "trace"
        subprocess.run(
            [strace, "-f", "-qq", "-e", calls, "-o", str(trace), *argv],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return [
            entry
            for entry in trace.read_text(encoding="utf-8").splitlines()
            if "chronos.db" in entry
        ]

    mv_calls = traced("mv", "--update=none-fail", str(archived), str(live))
    assert len(mv_calls) == 1, mv_calls
    assert "renameat2(" in mv_calls[0] and "RENAME_NOREPLACE" in mv_calls[0], mv_calls
    assert "EEXIST" in mv_calls[0], mv_calls
    ln_calls = traced("ln", "-T", str(archived), str(live))
    assert len(ln_calls) == 1 and "link" in ln_calls[0] and "EEXIST" in ln_calls[0], ln_calls
    assert live.read_bytes() == b"fresh keyed store"
    assert archived.read_bytes() == b"the pre-cutover store"


# ------------------------------------------------- C1R-F (the C1 re-run's runbook findings)

#: the last schema-17 commit on main: the first parent of #309's merge (N3-1 brought schema 18)
_LAST_SCHEMA17_MAIN = "8093176"
_SCHEMA18_MERGE = "f0a3446"
#: "Before you start"'s check that the recorded rollback commit is schema-17 code: it parses the
#: file and never runs it, and prints the line only for ONE plain top-level ``= 17`` (fix round 1)
_SCHEMA17_CHECK = """\
git -C <checkout> show <pre-cutover sha>:src/chronos/persistence/database.py | python -I -c '
import ast, sys
tree = ast.parse(sys.stdin.buffer.read())
name = "SCHEMA_VERSION"
sets = [  # every node that may set it: bind, delete, import, keyword, string, import *
    n for n in ast.walk(tree)
    if name in (getattr(n, "id", None), getattr(n, "attr", None))
    and not isinstance(n.ctx, ast.Load)
    or name in (getattr(n, "name", None), getattr(n, "asname", None), getattr(n, "arg", None))
    or name in (getattr(n, "rest", None), *getattr(n, "names", ()))
    or isinstance(n, ast.Constant) and isinstance(n.value, str) and name in n.value
    or isinstance(n, ast.alias) and n.name == "*"
]
top = [s for s in tree.body if isinstance(s, ast.Assign) and s.targets == sets]
if len(top) != 1:
    sys.exit(f"STOP: {len(sets)} place(s) may set SCHEMA_VERSION, not one plain assignment.")
value = top[0].value
if not (isinstance(value, ast.Constant) and type(value.value) is int and value.value == 17):
    sys.exit(f"STOP: SCHEMA_VERSION = {ast.unparse(value)}, not 17.")
print("SCHEMA_VERSION = 17")
'
"""
#: the 10486a9 form (kimi C1R-F HOLD P1): it tests that one LINE is present, not the value the
#: module ends with, so ``SCHEMA_VERSION = 17`` then ``SCHEMA_VERSION = 18`` passed it
_SCHEMA17_LINE_CHECK = (
    "git -C <checkout> show <pre-cutover sha>:src/chronos/persistence/database.py"
    " | grep -x 'SCHEMA_VERSION = 17'"
)
_DATABASE_PY = "src/chronos/persistence/database.py"
#: the pre-C1R-F instruction: HEAD of a checkout this page requires to be post-N3 already
_N1_OLD_RECORD = (
    "Write down the pre-cutover commit of the running code before you change anything — the "
    "Rollback section needs it: `git -C <checkout> rev-parse HEAD` → `<pre-cutover sha>`."
)


def _section(text: str, start: str, end: str) -> str:
    return " ".join(text[text.index(start) : text.index(end)].split())


def _schema17_checks(runbook: str) -> list[str]:
    """Every rollback-commit check under "Before you start", as an operator copies it: each
    fenced ``bash`` block dedented as a renderer shows it, and each inline ``git -C <checkout>
    show <pre-cutover sha>:…`` span (the 10486a9 form, so a revert to it is still found and run)."""

    before = runbook[runbook.index("## Before you start") : runbook.index("## Steps")]
    fenced = re.findall(r"^( *)```bash\n(.*?)^\1```$", before, re.S | re.M)
    inline = re.findall(r"`(git -C <checkout> show <pre-cutover sha>:[^`]*)`", before)
    return [textwrap.dedent(block) for _, block in fenced] + inline


def _the_runbook_s_schema17_check() -> str:
    checks = _schema17_checks(RUNBOOK.read_text(encoding="utf-8"))
    assert len(checks) == 1, checks
    return checks[0]


def _n1_offences(runbook: str) -> list[str]:
    """What "Before you start" and the Rollback must say about the rollback commit (C1R N1)."""

    before = _section(runbook, "## Before you start", "## Steps")
    rollback = _section(runbook, "## Rollback", "## What the gate's refusal codes mean")
    wanted_before = (
        "The code on this host is the post-N3 checkout (`SCHEMA_VERSION` 18). The Rollback "
        "section needs the commit the host ran **before** the post-N3 deploy, not the post-N3 "
        "`HEAD`: only schema-17 code starts the restored schema-17 store, and the post-N3 code "
        "refuses it, by design (`Unsupported Chronos schema version 17; expected 18`).",
        "Write that commit down as `<pre-cutover sha>`: before the deploy it is "
        "`git -C <checkout> rev-parse HEAD`; after the deploy, it is the entry "
        "`git -C <checkout> reflog` shows just before the deploy.",
        f"If you cannot establish it, use `{_LAST_SCHEMA17_MAIN}`, the last schema-17 commit on "
        f"main (`{_SCHEMA18_MERGE}^1`, the first parent of the merge that brought schema 18).",
        "Then check it. The check parses that commit's `database.py` with `ast` and never "
        "imports or runs it: it prints `SCHEMA_VERSION = 17` only when the file sets "
        "`SCHEMA_VERSION` in exactly one place, a plain top-level assignment of 17.",
        "Two assignments, an annotated or augmented one, a binding anywhere else, a string or a "
        "keyword argument that names it, or a `*` import each print a line beginning `STOP:` "
        "instead (C1R-F fix round 1, kimi P1).",
        "This must print exactly one line, `SCHEMA_VERSION = 17`; anything else — a `STOP:` "
        "line, an error, or nothing — means that commit is not the rollback code, so stop:",
    )
    offences = [f"Before you start lacks: {w}" for w in wanted_before if w not in before]
    checks = _schema17_checks(runbook)
    if checks != [_SCHEMA17_CHECK]:
        offences.append(f"Before you start's check is not exactly the pinned one: {checks!r}")
    if _N1_OLD_RECORD in before:
        offences.append("Before you start still records the post-N3 HEAD as the rollback commit")
    wanted_rollback = (
        "`git checkout <pre-cutover sha>`, the schema-17 commit you wrote down and checked under "
        '"Before you start" — never the post-N3 `HEAD`'
    )
    if wanted_rollback not in rollback:
        offences.append(f"Rollback lacks: {wanted_rollback}")
    return offences


def test_c1rf_n1_the_rollback_commit_is_the_pre_deploy_schema_17_one_and_is_checked() -> None:
    """C1R N1 (P2): "Before you start" had Kevin record ``git rev-parse HEAD`` of a checkout the
    same bullet requires to be post-N3 already, so ``<pre-cutover sha>`` was a schema-18 commit
    and the Rollback's ``git checkout <pre-cutover sha>`` started code that refuses the restored
    schema-17 store (rehearsed: ``REFUSE … version 17; expected 18``, rc 78). The page now asks
    for the commit the host ran BEFORE the post-N3 deploy, names 8093176 as the fallback, checks
    the recorded commit is schema-17 code, and the Rollback checks out that commit."""

    text = RUNBOOK.read_text(encoding="utf-8")
    assert _n1_offences(text) == []
    # negative controls: the check removed, the fallback removed, the old record restored, the
    # Rollback's pointer back to "the sha you wrote down before step 1" — each is caught
    block = "  ```bash\n" + textwrap.indent(_SCHEMA17_CHECK, "  ") + "  ```\n"
    assert text.count(block) == 1
    no_check = _n1_offences(text.replace(block, ""))
    assert no_check == ["Before you start's check is not exactly the pinned one: []"]
    # the 10486a9 one-liner back in its place (kimi P1) is found, and is not the pinned check
    line_check = _n1_offences(text.replace(block, f"  `{_SCHEMA17_LINE_CHECK}`\n"))
    assert line_check == [
        f"Before you start's check is not exactly the pinned one: {[_SCHEMA17_LINE_CHECK]!r}"
    ]
    no_fallback = text.replace(f"`{_LAST_SCHEMA17_MAIN}`", "`<a commit>`")
    assert no_fallback != text and _n1_offences(no_fallback)
    old = _n1_offences(text.replace("## Steps", f"- {_N1_OLD_RECORD}\n\n## Steps", 1))
    assert old == ["Before you start still records the post-N3 HEAD as the rollback commit"]
    assert "the sha you wrote down before step 1" not in " ".join(_rollback().split())


def _git(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *argv], capture_output=True, text=True, check=False
    )


def _python_on_path(tmp_path: Path) -> dict[str, str]:
    """The environment for a pasted check: ``python`` on PATH is this test's interpreter."""

    shim = tmp_path / "bin" / "python"
    if not shim.exists():
        shim.parent.mkdir()
        shim.symlink_to(sys.executable)
    return {**os.environ, "PATH": f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"}


def _run_schema17_check(
    command: str, checkout: Path, sha: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """``command`` exactly as the runbook gives it, ``<checkout>`` and ``<pre-cutover sha>``
    filled in the way the operator fills them."""

    line = command.replace("<checkout>", str(checkout)).replace("<pre-cutover sha>", sha)
    return subprocess.run(
        ["bash", "-c", line], capture_output=True, text=True, check=False, env=env
    )


def test_c1rf_n1_measured_the_check_passes_the_fallback_and_refuses_the_post_n3_code(
    tmp_path: Path,
) -> None:
    """The runbook's own check, run against this repository's history: it prints exactly
    ``SCHEMA_VERSION = 17`` for 8093176 (the named fallback) and nothing on stdout, exit 1, and a
    ``STOP:`` line on stderr, for this tree's HEAD (post-N3, schema 18) and for #309's merge.
    8093176 is that merge's first parent, so it is the last schema-17 commit on main's
    first-parent line. Independent of the check, git itself shows the two SCHEMA_VERSION lines."""

    head = _git("rev-parse", "HEAD").stdout.strip()
    assert _git("rev-parse", f"{_SCHEMA18_MERGE}^1").stdout == (
        _git("rev-parse", _LAST_SCHEMA17_MAIN).stdout
    ), "8093176 must be the first parent of the merge that brought schema 18"
    assert _git("merge-base", "--is-ancestor", _SCHEMA18_MERGE, head).returncode == 0
    path = "src/chronos/persistence/database.py"
    versions = {
        rev: re.findall(r"^SCHEMA_VERSION = (\d+)$", _git("show", f"{rev}:{path}").stdout, re.M)
        for rev in (_LAST_SCHEMA17_MAIN, _SCHEMA18_MERGE, head)
    }
    assert versions == {_LAST_SCHEMA17_MAIN: ["17"], _SCHEMA18_MERGE: ["18"], head: ["18"]}

    command, env = _the_runbook_s_schema17_check(), _python_on_path(tmp_path)

    def check(sha: str) -> subprocess.CompletedProcess[str]:
        return _run_schema17_check(command, ROOT, sha, env)

    passed = check(_LAST_SCHEMA17_MAIN)
    assert (passed.returncode, passed.stdout) == (0, "SCHEMA_VERSION = 17\n"), passed
    for refused_sha in (head, _SCHEMA18_MERGE):
        refused = check(refused_sha)
        assert (refused.returncode, refused.stdout) == (1, ""), (refused_sha, refused)
        assert refused.stderr == "STOP: SCHEMA_VERSION = 18, not 17.\n", (refused_sha, refused)


_TEXT_OUT: dict[str, Any] = {"capture_output": True, "text": True, "check": True}


def _synthetic_checkout(tmp_path: Path, *sources: str | None) -> tuple[Path, list[str]]:
    """A throwaway repository with one commit per source: ``database.py`` holds that source, or
    is absent for ``None``. Returns the repository and the commit shas, in order."""

    repo = tmp_path / "checkout"
    git = ("git", "-C", str(repo), "-c", "user.name=Chronos Test")
    git += ("-c", "user.email=chronos-test@example.invalid", "-c", "commit.gpgsign=false")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    target = repo / _DATABASE_PY
    shas = []
    for number, source in enumerate(sources):
        if source is None:
            target.unlink(missing_ok=True)
            (repo / "README").write_text(f"commit {number}\n", encoding="utf-8")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "--no-verify", "-m", f"c{number}"], check=True)
        shas.append(subprocess.run([*git, "rev-parse", "HEAD"], **_TEXT_OUT).stdout.strip())
    return repo, shas


#: (base commit, the one exact text replaced — None appends —, the replacement, accepted?)
_SCHEMA17_VARIANTS: dict[str, tuple[str, str | None, str, bool]] = {
    # kimi's negative control: a schema-18 module that still carries the schema-17 line
    "kimi_dual_17_then_18": (
        "HEAD",
        "SCHEMA_VERSION = 18\n",
        "SCHEMA_VERSION = 17\nSCHEMA_VERSION = 18\n",
        False,
    ),
    "annotated_18": ("HEAD", "SCHEMA_VERSION = 18\n", "SCHEMA_VERSION: int = 18\n", False),
    "no_space_18": ("HEAD", "SCHEMA_VERSION = 18\n", "SCHEMA_VERSION=18\n", False),
    # fail closed: 17, but not in the one plain form the check reads
    "annotated_17": (
        _LAST_SCHEMA17_MAIN,
        "SCHEMA_VERSION = 17\n",
        "SCHEMA_VERSION: int = 17\n",
        False,
    ),
    "17_then_rebound_inside_an_if": (
        _LAST_SCHEMA17_MAIN,
        None,
        "if True:\n    SCHEMA_VERSION = 18\n",
        False,
    ),
    "17_then_augmented": (_LAST_SCHEMA17_MAIN, None, "SCHEMA_VERSION += 1\n", False),
    "17_then_set_through_a_string": (
        _LAST_SCHEMA17_MAIN,
        None,
        'globals()["SCHEMA_VERSION"] = 18\n',
        False,
    ),
    "17_then_a_star_import": (_LAST_SCHEMA17_MAIN, None, "from os.path import *\n", False),
    "17_then_set_through_a_keyword": (
        _LAST_SCHEMA17_MAIN,
        None,
        "globals().update(SCHEMA_VERSION=18)\n",
        False,
    ),
    "17_then_imported_under_its_name": (
        _LAST_SCHEMA17_MAIN,
        None,
        "from os import sep as SCHEMA_VERSION\n",
        False,
    ),
    "17_then_declared_global_in_a_function": (
        _LAST_SCHEMA17_MAIN,
        None,
        "def _rebind():\n    global SCHEMA_VERSION\n",
        False,
    ),
    # accepted: the fallback's own bytes, and the same value without the spaces
    "8093176_unchanged": (_LAST_SCHEMA17_MAIN, None, "", True),
    "no_space_17": (_LAST_SCHEMA17_MAIN, "SCHEMA_VERSION = 17\n", "SCHEMA_VERSION=17\n", True),
}


def _variant(base: str, old: str | None, new: str) -> str:
    source = _git("show", f"{base}:{_DATABASE_PY}").stdout
    assert source.count("\nSCHEMA_VERSION = ") == 1, base
    if old is None:
        return source + new
    assert source.count(old) == 1, (base, old)
    return source.replace(old, new)


@pytest.mark.parametrize("variant", list(_SCHEMA17_VARIANTS))
def test_c1rf_fix1_n1_the_check_reads_the_effective_value_and_stops_on_an_ambiguous_one(
    tmp_path: Path, variant: str
) -> None:
    """kimi C1R-F HOLD P1: the 10486a9 check (``… | grep -x 'SCHEMA_VERSION = 17'``) tested that
    one line was present, so a schema-18 commit that ALSO carried that line passed it, and the
    Rollback would start code that refuses the restored schema-17 store. The runbook's check,
    run verbatim on a synthetic commit of each variant, prints ``SCHEMA_VERSION = 17`` (rc 0,
    nothing on stderr) only for one plain top-level assignment of 17; every other variant
    prints nothing on stdout, a ``STOP:`` line on stderr, and exits non-zero."""

    base, old, new, accepted = _SCHEMA17_VARIANTS[variant]
    repo, (sha,) = _synthetic_checkout(tmp_path, _variant(base, old, new))
    result = _run_schema17_check(
        _the_runbook_s_schema17_check(), repo, sha, _python_on_path(tmp_path)
    )
    if accepted:
        assert (result.returncode, result.stdout, result.stderr) == (
            0,
            "SCHEMA_VERSION = 17\n",
            "",
        ), result
    else:
        assert result.returncode != 0 and result.stdout == "", result
        assert result.stderr.startswith("STOP: ") and result.stderr.count("\n") == 1, result


def test_c1rf_fix1_n1_control_the_10486a9_line_check_passed_kimi_s_dual_assignment(
    tmp_path: Path,
) -> None:
    """Positive control for the variant test: the synthetic dual-assignment commit really is
    the P1 — the 10486a9 one-liner prints ``SCHEMA_VERSION = 17``, rc 0, for it — and that
    commit's module really ends at 18 (git shows the two lines; the last one wins)."""

    repo, (sha,) = _synthetic_checkout(
        tmp_path, _variant(*_SCHEMA17_VARIANTS["kimi_dual_17_then_18"][:3])
    )
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{_DATABASE_PY}"], **_TEXT_OUT
    ).stdout
    assert re.findall(r"^SCHEMA_VERSION = (\d+)$", shown, re.M) == ["17", "18"]
    old = _run_schema17_check(_SCHEMA17_LINE_CHECK, repo, sha, _python_on_path(tmp_path))
    assert (old.returncode, old.stdout) == (0, "SCHEMA_VERSION = 17\n"), old


def test_c1rf_fix1_n1_the_check_stops_on_a_bad_sha_and_a_missing_path(tmp_path: Path) -> None:
    """The check fails closed when git has nothing to show: a sha that names no commit, and a
    commit without ``database.py``. Each prints nothing on stdout, git's own ``fatal:`` line and
    a ``STOP:`` line on stderr, and exits non-zero; the commit that has the fallback's bytes, in
    the same repository, passes (so the refusals are not the harness)."""

    fallback = _git("show", f"{_LAST_SCHEMA17_MAIN}:{_DATABASE_PY}").stdout
    repo, (with_file, without_file) = _synthetic_checkout(tmp_path, fallback, None)
    command, env = _the_runbook_s_schema17_check(), _python_on_path(tmp_path)
    passed = _run_schema17_check(command, repo, with_file, env)
    assert (passed.returncode, passed.stdout, passed.stderr) == (0, "SCHEMA_VERSION = 17\n", "")
    for sha in ("0" * 40, without_file):
        refused = _run_schema17_check(command, repo, sha, env)
        assert refused.returncode != 0 and refused.stdout == "", (sha, refused)
        lines = refused.stderr.splitlines()
        assert lines[0].startswith("fatal: ") and lines[-1].startswith("STOP: "), (sha, refused)


def _fence_offences(text: str) -> list[str]:
    """CommonMark's backtick fences, line by line: a fence opens with three or more backticks
    (an info string may follow) and closes with at least as many backticks and NOTHING else on
    the line but spaces. A backtick run followed by text inside a block is not a closing fence:
    the block runs on (C1R N2). A block still open at the end of the file is unbalanced."""

    offences: list[str] = []
    opening: tuple[int, int] | None = None  # (line number, backtick count)
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        run = len(stripped) - len(stripped.lstrip("`"))
        if run < 3:
            continue
        if opening is None:
            opening = (number, run)
        elif run < opening[1]:
            continue  # a shorter backtick run is the block's content
        elif re.fullmatch(r"`+", stripped):
            opening = None
        else:
            offences.append(f"line {number}: text after a closing fence: {stripped!r}")
            opening = None
    if opening is not None:
        offences.append(f"line {opening[0]}: a fence that never closes")
    return offences


def test_c1rf_n2_every_fence_closes_alone_on_its_line() -> None:
    """C1R N2 (P3): step 6's shred block closed with ```` ``` One escrow copy, exactly; …````, which
    CommonMark does not read as a closing fence, so the rendered block ran on into the prose and
    a whole-block copy left an unmatched backtick at the shell. Every fence in the runbook is now
    balanced and closes alone on its line, and the shred block is the one command."""

    text = RUNBOOK.read_text(encoding="utf-8")
    assert _fence_offences(text) == []
    step6 = _step(text, "6. **Escrow", "7. **Vacate")
    shred = [b for b in re.findall(r"```bash\n(.*?)```", step6, re.S) if "shred -u" in b]
    assert [textwrap.dedent(b) for b in shred] == [
        "shred -u ~/chronos-escrow/chronos-persistence-key.age && rmdir ~/chronos-escrow\n"
    ]
    assert "One escrow copy, exactly; the `.env` on the host is the only working copy." in (
        " ".join(step6.split())
    )
    # negative controls: the N2 defect, a dropped closing fence, an over-short closing fence
    n2 = "   ```\n   One escrow copy"
    assert n2 in text
    run_on = _fence_offences(text.replace(n2, "   ``` One escrow copy", 1))
    assert len(run_on) == 1 and "text after a closing fence: '``` One escrow copy" in run_on[0]
    unclosed = text.rstrip("\n") + "\n```bash\necho never closed\n"
    last = len(text.rstrip("\n").splitlines()) + 1
    assert _fence_offences(unclosed) == [f"line {last}: a fence that never closes"]
    assert _fence_offences("````bash\nx\n```\n") == ["line 1: a fence that never closes"]


def test_c1rf_n2_a_commonmark_renderer_ends_the_shred_block_at_its_one_command() -> None:
    """The same claim through an independent CommonMark parser (markdown-it-py, locked in
    requirements-dev.lock): no rendered fence in the runbook holds a line that starts with
    backticks, and the rendered shred block is the one command."""

    markdown_it = pytest.importorskip("markdown_it")
    text = RUNBOOK.read_text(encoding="utf-8")
    fences = [t for t in markdown_it.MarkdownIt("commonmark").parse(text) if t.type == "fence"]
    assert fences, "the runbook carries fenced blocks"
    for fence in fences:
        assert not any(line.lstrip().startswith("```") for line in fence.content.splitlines())
    shred = [f.content for f in fences if "shred -u" in f.content]
    assert shred == [
        "shred -u ~/chronos-escrow/chronos-persistence-key.age && rmdir ~/chronos-escrow\n"
    ]


def test_c1rf_n3_step_6_says_the_units_can_read_the_escrow_directory_not_write_it() -> None:
    """C1R N3 (P3): ``ProtectHome=read-only`` makes ``/home`` read-only for the units, not
    invisible, and each unit's ``ReadWritePaths=`` names only its checkout (and the backend's
    state directory), never the escrow directory. So the units can READ ``~/chronos-escrow`` but
    cannot write it; step 6 said they were kept "out of" it. Checked against both shipped units."""

    step6 = " ".join(
        _step(RUNBOOK.read_text(encoding="utf-8"), "6. **Escrow", "7. **Vacate").split()
    )
    assert "keeps the units out of" not in step6
    assert (
        "`ProtectHome=read-only` lets the units read `~/chronos-escrow` but not write it "
        "(no `ReadWritePaths=` entry covers it); what they could read is ciphertext that only "
        "your recovery key opens. `mkdir` fails if the directory already exists"
    ) in step6
    for unit in ("chronos-backend.service", "chronos-worker.service"):
        lines = (ROOT / "docs" / "ops" / unit).read_text(encoding="utf-8").splitlines()
        # read-only, not hidden: exactly this ProtectHome=, and nothing that would hide ~
        assert [line for line in lines if line.startswith("ProtectHome=")] == [
            "ProtectHome=read-only"
        ], unit
        assert not any(
            line.startswith(("InaccessiblePaths=", "TemporaryFileSystem=")) for line in lines
        ), unit
        writable = [
            path
            for line in lines
            if line.startswith("ReadWritePaths=")
            for path in line.removeprefix("ReadWritePaths=").split()
        ]
        assert writable and all(re.fullmatch(r"%h/<CHRONOS_[A-Z_]+>", p) for p in writable), (
            unit,
            writable,
        )

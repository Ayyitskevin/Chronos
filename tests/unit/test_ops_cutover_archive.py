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
socket is ever opened), (7) the runbook contract, (8) G1 — the drill's ``archived_schema_head``
seam, with the control that proves the gap it closes.

The managed-position binding row is inserted with foreign-key enforcement OFF (a plain
SQLAlchemy engine, never ``Database``): its ``order_intents``/``risk_decisions`` parents are
not the gate's input — the binding and its position-management stream are.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import closing
from dataclasses import dataclass
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
) -> Store:
    """A pre-cutover store. ``run``: ``clean`` | ``manual_review`` | ``readiness_pending`` |
    ``None``; ``binding``: ``closed`` | ``open`` | ``orphan`` (no stream) | ``tampered``
    (stream payload edited) | ``None``; ``provenance``: origin classes written for the run."""

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
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
    return Store(db=db, now=now)


def _clock(now: datetime) -> Clock:
    ticks = itertools.count(0.0, 0.25)
    return Clock(wall=lambda: now, monotonic=lambda: next(ticks))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entries(directory: Path) -> list[str]:
    """Directory names without sqlite's reader bookkeeping (``-wal``/``-shm`` may appear beside a
    WAL-mode store that a read-only connection opened — a directory write, never a store write)."""

    return sorted(
        p.name
        for p in directory.iterdir()
        if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
    )


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
    assert _sha256(store.db) == before
    assert _entries(store.db.parent) == ["chronos.db"]
    assert not (tmp_path / "backups").exists() and not (tmp_path / "restored").exists()


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
    assert _sha256(store.db) == before
    assert _entries(store.db.parent) == ["chronos.db"]
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
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 2 and report["code"] == "BINDING_UNREADABLE", report
    assert not (tmp_path / "backups").exists()
    assert _entries(store.db.parent) == ["chronos.db"]


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


def test_4b_write_ahead_frames_beside_the_store_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    # a connection that committed and is still open: the frames sit in -wal, uncheckpointed
    holder = sqlite3.connect(store.db)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute(
            "INSERT INTO application_events (event_type, severity, message, event_data,"
            " occurred_at) VALUES ('n3', 'INFO', 'frame', '{}', '2026-10-01 00:00:00.000000')"
        )
        holder.commit()
        assert store.db.with_name("chronos.db-wal").stat().st_size > 32
        code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    finally:
        holder.close()
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
    with closing(sqlite3.connect(f"{store.db.as_uri()}?mode=ro", uri=True)) as connection:
        chain_before = connection.execute(
            "SELECT stream, sequence, record_hash FROM hash_chain_records ORDER BY stream, sequence"
        ).fetchall()
    monkeypatch.setattr(socket, "socket", _NoSocket)  # no broker, no network: proven, not said
    code, report = _run(store, tmp_path, "--apply", capsys=capsys)
    assert code == 0, report
    assert report["verdict"] == "ARCHIVED" and report["dry_run"] is False
    assert report["drill"]["verdict"] == "VERIFIED"
    envelope = Path(report["envelope"])
    assert envelope.name == f"chronos-{store.stamp}" and envelope.parent == tmp_path / "backups"
    manifest_path = envelope / drill.MANIFEST_NAME
    assert report["manifest_path"] == str(manifest_path)
    assert report["manifest_sha256"] == _sha256(manifest_path)
    manifest = BackupManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert report["cleartext_sha256"] == manifest.sha256
    assert report["ciphertext_sha256"] == manifest.ciphertext_sha256
    assert report["schema_head"] == manifest.schema_head == f"schema_version:{SCHEMA_VERSION - 1}"
    # moved aside by rename, bound to the envelope's stamp; the main file's bytes are the original
    moved = {entry["from"]: entry["to"] for entry in report["moved_aside"]}
    archived = store.db.with_name(f"chronos.db.archived-{store.stamp}")
    assert moved[str(store.db)] == str(archived)
    assert not store.db.exists() and _sha256(archived) == original
    assert all(name.endswith(f".archived-{store.stamp}") for name in _entries(store.db.parent))
    assert not any(
        name in ("chronos.db-wal", "chronos.db-shm") for name in os.listdir(store.db.parent)
    )
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
    again = drill.restore(manifest, tmp_path / "again", archived_schema_head=manifest.schema_head)
    assert again.verified, again.failures
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
    assert store.db.exists() and _sha256(store.db) == before
    assert _entries(store.db.parent) == ["chronos.db"]
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
    assert _sha256(store.db) == before and _entries(store.db.parent) == ["chronos.db"]


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


def test_7b_the_ops_readme_indexes_the_runbook() -> None:
    assert "`N3-CUTOVER.md`" in OPS_README.read_text(encoding="utf-8")


# ------------------------------------------------------------- (8) G1: the drill's archived head


def _drill_store(tmp_path: Path) -> Store:
    return _store(tmp_path, binding="closed")


def test_8a_run_drill_with_the_archived_head_verifies_a_store_one_version_behind(
    tmp_path: Path,
) -> None:
    store = _drill_store(tmp_path)
    head = drill.schema_head(store.db)
    assert head == f"schema_version:{SCHEMA_VERSION - 1}"
    report = run_drill(
        store.db,
        tmp_path / "out",
        tmp_path / "restored",
        clock=_clock(store.now),
        archived_schema_head=head,
    )
    assert report.verdict == "VERIFIED", report.failures
    assert report.restore is not None
    assert report.restore.acceptance == f"archived:{head}"
    restored = report.restore
    assert restored.schema_head_ok and restored.sha256_ok and restored.row_counts_ok


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


def test_8c_an_archived_head_that_is_not_the_store_s_head_is_not_verified(
    tmp_path: Path,
) -> None:
    store = _drill_store(tmp_path)
    report = run_drill(
        store.db,
        tmp_path / "out",
        tmp_path / "restored",
        clock=_clock(store.now),
        archived_schema_head=f"schema_version:{SCHEMA_VERSION - 2}",
    )
    assert report.verdict == "FAILED"
    assert any(f.startswith("schema_head:") for f in report.failures), report.failures
    # default reports say the repository accepted the copy; this one says what was named instead
    assert report.restore is not None and report.restore.acceptance.startswith("archived:")


def test_8d_the_restore_report_defaults_to_repository_acceptance(tmp_path: Path) -> None:
    store = _store(tmp_path, version_offset=0)  # a store AT the running version: accepted as before
    report = run_drill(store.db, tmp_path / "out", tmp_path / "restored", clock=_clock(store.now))
    assert report.verdict == "VERIFIED", report.failures
    assert report.restore is not None and report.restore.acceptance == "repository"
    assert report.restore.to_dict()["acceptance"] == "repository"

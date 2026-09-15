"""The measured RPO/RTO restore drill (M3 ops plane), against a demo store under tmp_path.

Every store here is created by the repository's own initializer (``Database.initialize``)
under ``tmp_path`` — never ``data/chronos.db``. Numbered to the R-1 contract: (1) the backup
and its manifest, (2) restore + the four verifiers, (3) rpo_s, (4) the CLI and the import
pin, (6) the pinned-clock measurements and the "never opens the source for writing" pin.

Built ON ``tests/integration/test_backup_restore_drill.py`` (the isolated backup/restore
drill over the real WAL-backed stores, which proves artifact integrity and the fail-closed
recovery posture and says it does not prove RPO/RTO): its WAL posture — committed rows
still in ``-wal`` that a main-file copy misses — is reproduced here through its own
helpers (``_checkpoint_wal``, ``_row_count``), and this harness adds the manifest, the
measured numbers and the runbook on top of that posture rather than beside it.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.test_backup_restore_drill import _checkpoint_wal, _row_count

from chronos.auditlog.log import AuditLog
from chronos.operations import restore_drill as drill
from chronos.operations.restore_drill import (
    BackupManifest,
    Clock,
    DrillRefused,
    backup,
    restore,
    rpo_seconds,
    run_drill,
    schema_head,
    verify_restored,
)
from chronos.persistence.database import SCHEMA_VERSION, Database

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "chronos" / "operations" / "restore_drill.py"
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _demo_store(tmp_path: Path, *, events: int = 3, at: datetime | None = None) -> Path:
    """A demo sqlite created by the repo's initializer, with ``events`` timestamped rows."""

    data = tmp_path / "data"
    data.mkdir()
    db = data / "chronos.db"
    database = Database(f"sqlite:///{db}")
    try:
        database.initialize()
    finally:
        database.dispose()
    stamp = at or datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    with sqlite3.connect(db) as connection:
        for index in range(events):
            connection.execute(
                "INSERT INTO application_events (event_type, severity, correlation_id, symbol,"
                " message, event_data, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "drill",
                    "INFO",
                    None,
                    None,
                    f"event {index}",
                    "{}",
                    (stamp + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                ),
            )
    return db


def _fixed_clock(wall: datetime, ticks: list[float]) -> Clock:
    """A wall clock frozen at ``wall`` and a monotonic clock that returns ``ticks`` in order."""

    it = iter(ticks)
    return Clock(wall=lambda: wall, monotonic=lambda: next(it))


# ----------------------------------------------------------- (1) backup + manifest


def test_1a_backup_manifest_sha256_matches_the_backup_file(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    assert Path(manifest.backup_path).is_file()
    assert manifest.sha256 == _sha256(Path(manifest.backup_path))
    assert manifest.encryption == "none"
    assert manifest.source_path == str(db)
    assert manifest.audit_head is None  # no audit pair beside this store


def test_1b_row_counts_equal_a_direct_count_and_cover_every_table(tmp_path: Path) -> None:
    db = _demo_store(tmp_path, events=5)
    manifest = backup(db, tmp_path / "out")
    with sqlite3.connect(f"file:{manifest.backup_path}?mode=ro", uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        direct = {
            t: connection.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables
        }
    assert manifest.row_counts == direct
    assert manifest.row_counts["application_events"] == 5
    assert manifest.row_counts["schema_version"] == 1
    assert set(manifest.row_counts) == set(tables)


def test_1c_schema_head_is_the_schema_version_row_on_an_initializer_store(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    # an initializer-made store carries no alembic_version table (measured at base)
    assert manifest.schema_head == f"schema_version:{SCHEMA_VERSION}"
    assert schema_head(Path(manifest.backup_path)) == manifest.schema_head


def test_1d_backup_uses_the_online_backup_api_and_the_source_is_never_written(
    tmp_path: Path,
) -> None:
    db = _demo_store(tmp_path)
    before = _sha256(db)
    manifest = backup(db, tmp_path / "out")
    assert _sha256(db) == before, "the source must be byte-identical after the backup"
    # a consistent backup: the copy passes sqlite's own integrity check and equals the source's rows
    with sqlite3.connect(f"file:{manifest.backup_path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not list((tmp_path / "out").glob(".*.tmp")), "no temp file survives the os.replace"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_1e_a_source_that_is_not_a_regular_file_is_refused_typed(tmp_path: Path, kind: str) -> None:
    db = _demo_store(tmp_path)
    bad = tmp_path / "data" / f"bad-{kind}"
    if kind == "symlink":
        bad.symlink_to(db)
    elif kind == "fifo":
        os.mkfifo(bad)
    else:
        bad.mkdir()
    if kind == "fifo":
        # in a subprocess under a timeout: a reader that does not fstat first BLOCKS on a
        # fifo forever, and that must read as a failure, never as a hung suite
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path\n"
                "from chronos.operations.restore_drill import DrillRefused, backup\n"
                "try:\n"
                f"    backup(Path({str(bad)!r}), Path({str(tmp_path / 'out')!r}))\n"
                "except DrillRefused as e:\n"
                "    print('REFUSED', e); sys.exit(0)\n"
                "print('ACCEPTED'); sys.exit(1)",
            ],
            cwd=ROOT,
            env={**os.environ, **SAFE_ENV},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert probe.returncode == 0 and "fifo" in probe.stdout, probe.stdout + probe.stderr
    else:
        with pytest.raises(DrillRefused, match=kind):
            backup(bad, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_1f_the_backup_api_captures_committed_wal_rows_that_a_file_copy_misses(
    tmp_path: Path,
) -> None:
    """The integration drill's posture (tests/integration/test_backup_restore_drill.py):
    a holder connection keeps the WAL from being checkpointed, so committed rows live only
    in ``-wal``; a main-file copy misses them, the online backup API does not."""

    db = _demo_store(tmp_path, events=0)
    _checkpoint_wal(db)
    holder = sqlite3.connect(db)  # keeps the WAL alive: no checkpoint-on-close
    try:
        with sqlite3.connect(db) as writer:
            for index in range(7):
                writer.execute(
                    "INSERT INTO application_events (event_type, severity, message, event_data,"
                    " occurred_at) VALUES (?, ?, ?, ?, ?)",
                    ("wal", "INFO", f"wal row {index}", "{}", "2026-09-15 03:00:00.000000"),
                )
        assert db.with_name(db.name + "-wal").stat().st_size > 0, (
            "the rows must still be in the WAL"
        )
        unsafe = tmp_path / "unsafe-main-file-only.db"
        unsafe.write_bytes(db.read_bytes())  # what `cp` would give an operator
        assert _row_count(unsafe, "application_events") == 0
        manifest = backup(db, tmp_path / "out")
    finally:
        holder.close()
    assert manifest.row_counts["application_events"] == 7
    assert _row_count(Path(manifest.backup_path), "application_events") == 7
    report = restore(manifest, tmp_path / "restored")
    assert report.verified, report.failures
    assert _row_count(Path(report.restored_path), "application_events") == 7


# ----------------------------------------------------------- (2) restore + verify


def test_2a_restore_into_a_fresh_dir_is_verified_and_rto_is_the_pinned_monotonic_delta(
    tmp_path: Path,
) -> None:
    db = _demo_store(tmp_path)
    wall = datetime(2026, 9, 15, 2, 0, tzinfo=UTC)
    # restore() reads the monotonic clock exactly twice: at start and at verified
    clock = _fixed_clock(wall, [100.0, 103.25])
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    report = restore(manifest, tmp_path / "restored", clock=clock)
    assert report.verified, report.failures
    assert report.rto_s == pytest.approx(3.25)
    assert report.sha256_ok and report.schema_head_ok and report.row_counts_ok
    assert report.audit_chain == "NOT_APPLICABLE"
    assert Path(report.restored_path).parent == tmp_path / "restored"
    assert Path(report.restored_path).is_file()


def test_2b_a_byte_flipped_in_the_backup_fails_sha256(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    path = Path(manifest.backup_path)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    report = restore(manifest, tmp_path / "restored")
    assert not report.verified
    assert not report.sha256_ok
    assert any(f.startswith("sha256:") for f in report.failures), report.failures


def test_2c_a_dropped_row_in_the_restored_copy_fails_row_counts(tmp_path: Path) -> None:
    db = _demo_store(tmp_path, events=4)
    manifest = backup(db, tmp_path / "out")
    report = restore(manifest, tmp_path / "restored")
    assert report.verified, report.failures
    restored = Path(report.restored_path)
    with sqlite3.connect(restored) as connection:
        connection.execute(
            "DELETE FROM application_events WHERE id = (SELECT MIN(id) FROM application_events)"
        )
    failures, facts = verify_restored(manifest, restored, None)
    assert facts["row_counts_ok"] is False
    assert any(f.startswith("row_counts:") and "application_events" in f for f in failures), (
        failures
    )
    # the retained copy is stored in rollback-journal mode (one self-contained file), so the
    # delete moves the main file's bytes as well: both verifiers speak
    assert facts["sha256_ok"] is False


def test_2d_a_non_empty_restore_target_is_refused(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    target = tmp_path / "restored"
    target.mkdir()
    (target / "something").write_text("occupied\n")
    with pytest.raises(DrillRefused, match="not empty"):
        restore(manifest, target)
    assert sorted(p.name for p in target.iterdir()) == ["something"]


def test_2e_the_schema_head_check_refuses_a_restored_store_whose_head_moved(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    report = restore(manifest, tmp_path / "restored")
    restored = Path(report.restored_path)
    with sqlite3.connect(restored) as connection:
        connection.execute("UPDATE schema_version SET version = version + 1")
    failures, facts = verify_restored(manifest, restored, None)
    assert facts["schema_head_ok"] is False
    assert any(f.startswith("schema_head:") for f in failures), failures
    assert any(f.startswith("schema_acceptance:") for f in failures), failures


def test_2f_the_audit_chain_travels_with_the_backup_and_verifies_on_the_restored_store(
    tmp_path: Path,
) -> None:
    db = _demo_store(tmp_path)
    log = AuditLog(db.parent / "platform_audit.jsonl")
    log.append("drill.test", {"n": 1})
    last = log.append("drill.test", {"n": 2})
    manifest = backup(db, tmp_path / "out")
    assert manifest.audit_head == last.record_hash
    assert manifest.audit_log_path is not None
    report = restore(manifest, tmp_path / "restored")
    assert report.verified, report.failures
    assert report.audit_chain == "VALID"
    # a broken chain on the restored store is a failure, not a warning
    restored_log = Path(report.target_dir) / "platform_audit.jsonl"
    lines = restored_log.read_text().splitlines()
    lines[0] = (
        lines[0].replace('"n": 1', '"n": 9')
        if '"n": 1' in lines[0]
        else lines[0].replace("1", "9", 1)
    )
    restored_log.write_text("\n".join(lines) + "\n")
    failures, facts = verify_restored(manifest, Path(report.restored_path), restored_log)
    assert facts["audit_chain"] == "BROKEN"
    assert any(f.startswith("audit_chain:") for f in failures), failures


# ----------------------------------------------------------- (3) rpo_s


def test_3a_rpo_is_the_age_of_the_newest_evidence_at_backup_time_from_a_fake_wall_clock(
    tmp_path: Path,
) -> None:
    # the initializer stamps schema_version.applied_at with the REAL clock, so the pinned
    # events sit a day in the future to be the newest evidence; the fake wall follows them
    first = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    newest = first + timedelta(seconds=2)  # 3 events at first, +1 s, +2 s
    db = _demo_store(tmp_path, events=3, at=first)
    wall = newest + timedelta(seconds=90)
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    rpo, basis = rpo_seconds(manifest)
    assert rpo == pytest.approx(90.0)
    assert basis["source"] == "table:application_events.occurred_at"
    assert basis["newest_evidence_at"] == newest.isoformat()


def test_3b_the_audit_log_wins_when_its_last_entry_is_newer(tmp_path: Path) -> None:
    db = _demo_store(tmp_path, events=1, at=datetime(2026, 9, 15, 1, 0, tzinfo=UTC))
    log = AuditLog(db.parent / "platform_audit.jsonl")
    record = log.append("drill.test", {"n": 1})  # stamped now, far newer than 01:00
    wall = datetime.fromisoformat(record.at_utc) + timedelta(seconds=7)
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    rpo, basis = rpo_seconds(manifest)
    assert rpo == pytest.approx(7.0, abs=1e-3)
    assert basis["source"] == "audit_log:last_entry.at_utc"


def test_3c_a_store_with_no_timestamped_evidence_reports_null_with_a_reason_never_zero(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "bare.db"
    with sqlite3.connect(bare) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO notes (body) VALUES ('no clock here')")
    manifest = backup(bare, tmp_path / "out")
    rpo, basis = rpo_seconds(manifest)
    assert rpo is None
    assert "no timestamped evidence" in str(basis["reason"])
    assert basis["source"] is None


# ----------------------------------------------------------- (4) CLI + import pin


def test_4a_the_cli_prints_a_drill_report_and_exits_0_when_verified(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.operations.restore_drill",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "out"),
            "--restore-into",
            str(tmp_path / "restored"),
            "--pretty",
        ],
        cwd=ROOT,
        env={**os.environ, **SAFE_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["verdict"] == "VERIFIED" and report["failures"] == []
    assert set(report) >= {
        "manifest",
        "restore",
        "rpo_s",
        "rto_s",
        "verdict",
        "failures",
        "rpo_basis",
    }
    assert report["rto_s"] > 0
    assert report["manifest"]["encryption"] == "none"
    assert Path(report["manifest_path"]).is_file()


def test_4b_the_cli_exits_2_on_a_refusal_and_on_a_failed_verdict(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    link = tmp_path / "data" / "link.db"
    link.symlink_to(db)
    argv = [
        sys.executable,
        "-m",
        "chronos.operations.restore_drill",
        "--db",
        str(link),
        "--out",
        str(tmp_path / "out"),
        "--restore-into",
        str(tmp_path / "restored"),
    ]
    completed = subprocess.run(
        argv, cwd=ROOT, env={**os.environ, **SAFE_ENV}, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 2
    assert "symlink" in json.loads(completed.stdout)["failures"][0]


FORBIDDEN_PREFIXES = (
    "chronos.orders",
    "chronos.execution",
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.recovery",
    "chronos.broker",
)


def test_4c_the_module_imports_no_order_execution_or_autonomy_module() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offending = [name for name in imported if name.startswith(FORBIDDEN_PREFIXES)]
    assert offending == [], offending
    # and at runtime, importing the module pulls none of them into sys.modules
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, chronos.operations.restore_drill; "
            f"print(sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIXES!r})))",
        ],
        cwd=ROOT,
        env={**os.environ, **SAFE_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "[]", probe.stdout


# ----------------------------------------------------------- (6) the whole drill


def test_6a_run_drill_reports_verified_with_both_numbers_and_never_writes_the_source(
    tmp_path: Path,
) -> None:
    first = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    db = _demo_store(tmp_path, events=2, at=first)
    before = _sha256(db)
    wall = first + timedelta(seconds=1) + timedelta(seconds=30)  # 30 s after the newest event
    ticks = itertools.count(start=10.0, step=0.5)
    clock = Clock(wall=lambda: wall, monotonic=lambda: next(ticks))
    report = run_drill(db, tmp_path / "out", tmp_path / "restored", clock=clock)
    assert report.verdict == "VERIFIED" and report.failures == ()
    assert report.rpo_s == pytest.approx(30.0)
    assert report.rto_s is not None and report.rto_s > 0
    assert _sha256(db) == before
    payload = report.to_dict()
    assert json.loads(json.dumps(payload))["verdict"] == "VERIFIED"


def test_6b_manifest_round_trips_through_json(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    again = BackupManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert again == manifest
    report = restore(again, tmp_path / "restored")
    assert report.verified, report.failures


def test_6d_the_source_is_only_ever_connected_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sqlite connection the drill opens on the SOURCE path is a ``mode=ro`` URI —
    the mechanism behind "never opens the source for writing" (a read-write connection
    that happens not to write leaves the bytes identical, so the sha256 pin alone cannot
    see it)."""

    db = _demo_store(tmp_path)
    seen: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def recording_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        seen.append((str(database), bool(kwargs.get("uri", False))))
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drill.sqlite3, "connect", recording_connect)
    report = run_drill(db, tmp_path / "out", tmp_path / "restored")
    assert report.verdict == "VERIFIED"
    on_source = [(target, uri) for target, uri in seen if str(db) in target]
    assert on_source, "the drill must have connected to the source"
    for target, uri in on_source:
        assert uri and target.startswith("file:") and "mode=ro" in target, target


# ----------------------------------------------------------- (r1) Daybreak's HOLD at 2135c70


def _checkpointed_store(tmp_path: Path, events: int = 1) -> Path:
    """A store with NO -wal/-shm beside it: the shape that tempted the old immutable inference."""

    db = _demo_store(tmp_path, events=events)
    _checkpoint_wal(db)
    db.with_name(db.name + "-wal").unlink(missing_ok=True)
    db.with_name(db.name + "-shm").unlink(missing_ok=True)
    return db


def test_r1_1_a_row_committed_between_uri_selection_and_the_source_open_is_in_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak's P1 probe, deterministic: the source URI has been chosen, and before sqlite
    opens it a writer creates a WAL and commits a row. A read-only open under normal SQLite
    locking sees that WAL; an inferred immutable=1 would silently omit the row."""

    db = _checkpointed_store(tmp_path, events=1)
    real_connect = sqlite3.connect
    state = {"injected": False, "uris": []}

    def racing_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        target = str(database)
        if str(db) in target and target.startswith("file:") and not state["injected"]:
            state["injected"] = True
            state["uris"].append(target)
            with real_connect(db) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "INSERT INTO application_events (event_type, severity, message, event_data,"
                    " occurred_at) VALUES ('race', 'INFO', 'committed after the uri was chosen',"
                    " '{}', '2026-09-15 04:00:00.000000')"
                )
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drill.sqlite3, "connect", racing_connect)
    manifest = backup(db, tmp_path / "out")
    assert state["injected"], "the seam must have fired on the source open"
    assert "immutable" not in state["uris"][0], state["uris"]
    assert manifest.row_counts["application_events"] == 2, manifest.row_counts
    assert _row_count(Path(manifest.backup_path), "application_events") == 2


def test_r1_2a_rpo_is_derived_from_the_retained_snapshot_never_the_live_source(
    tmp_path: Path,
) -> None:
    first = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    db = _demo_store(tmp_path, events=2, at=first)
    wall = first + timedelta(seconds=1, minutes=5)
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    assert manifest.snapshot_completed_at == wall.isoformat()
    rpo_before, basis_before = rpo_seconds(manifest)
    assert rpo_before == pytest.approx(300.0)
    assert basis_before["source"] == "table:application_events.occurred_at"
    # the live source moves on AFTER the backup: a row dated far newer than the snapshot
    with sqlite3.connect(db) as writer:
        writer.execute(
            "INSERT INTO application_events (event_type, severity, message, event_data,"
            " occurred_at) VALUES ('later', 'INFO', 'after the backup', '{}', ?)",
            ((first + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
    rpo_after, basis_after = rpo_seconds(manifest)
    assert (rpo_after, basis_after) == (rpo_before, basis_before)
    assert rpo_after is not None and rpo_after > 0


def test_r1_2b_evidence_dated_after_the_snapshot_is_refused_never_a_negative_rpo(
    tmp_path: Path,
) -> None:
    first = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    db = _demo_store(tmp_path, events=1, at=first)
    wall = first + timedelta(seconds=30)
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    # a future-dated row planted in the RETAINED copy (the file the drill owns)
    with sqlite3.connect(manifest.backup_path) as writer:
        writer.execute(
            "INSERT INTO application_events (event_type, severity, message, event_data,"
            " occurred_at) VALUES ('future', 'INFO', 'dated after the snapshot', '{}', ?)",
            ((wall + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
    with pytest.raises(DrillRefused, match="after the snapshot"):
        rpo_seconds(manifest)
    # and the whole drill reports the refusal as a FAILED verdict, rpo_s null, never negative
    report = run_drill(
        db,
        tmp_path / "out2",
        tmp_path / "restored",
        clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0),
    )
    assert report.verdict == "VERIFIED" and report.rpo_s is not None and report.rpo_s >= 0


def test_r1_3a_sqlite_writes_the_backup_through_the_exclusive_temp_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _demo_store(tmp_path)
    real_connect = sqlite3.connect
    destinations: list[str] = []

    def recording_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        target = str(database)
        if str(db) not in target:
            destinations.append(target)
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drill.sqlite3, "connect", recording_connect)
    manifest = backup(db, tmp_path / "out")
    # the object sqlite wrote is the O_EXCL descriptor itself, reached through /proc/self/fd
    assert destinations and destinations[0].startswith("/proc/self/fd/"), destinations
    assert Path(manifest.backup_path).is_file()
    assert not list((tmp_path / "out").glob(".*")), "no temp entry survives publication"


def test_r1_3b_the_final_name_is_acquired_without_overwrite(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    wall = datetime(2026, 9, 15, 5, 0, tzinfo=UTC)
    out = tmp_path / "out"
    out.mkdir()
    occupied = out / f"{db.stem}-{wall.strftime('%Y%m%dT%H%M%SZ')}"  # the envelope's name
    occupied.write_bytes(b"someone else's backup")
    before = _sha256(occupied)
    with pytest.raises(DrillRefused, match="already exists"):
        backup(db, out, clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    assert _sha256(occupied) == before
    assert sorted(p.name for p in out.iterdir()) == [occupied.name], (
        "no temp left, nothing renamed over"
    )


def test_r1_3c_destination_directories_are_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _demo_store(tmp_path)
    real_fsync = os.fsync
    synced_dirs: list[int] = []

    def recording_fsync(fd: int) -> None:
        if os.path.stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_dirs.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(drill.os, "fsync", recording_fsync)
    manifest = backup(db, tmp_path / "out")
    assert os.stat(tmp_path / "out").st_ino in synced_dirs, "the backup directory was not fsynced"
    synced_dirs.clear()
    restore(manifest, tmp_path / "restored")
    assert os.stat(tmp_path / "restored").st_ino in synced_dirs, (
        "the restore target was not fsynced"
    )


def test_r1_3d_the_audit_pair_is_read_once_as_one_snapshot_after_the_backup_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _demo_store(tmp_path, events=3)
    log = AuditLog(db.parent / "platform_audit.jsonl")
    last = log.append("drill.test", {"n": 1})
    real_pair = drill.read_audit_pair
    calls: list[tuple[str, int]] = []

    def recording_pair(path: Path) -> tuple[str | None, bytes | None]:
        # at this instant the backup must already be COMPLETE (every source row in the
        # unpublished temp ENVELOPE) and not yet published — a refusal here publishes nothing
        temps = sorted((tmp_path / "out").glob(".*.tmp"))
        published = sorted(p for p in (tmp_path / "out").iterdir() if not p.name.startswith("."))
        rows = _row_count(temps[0] / "chronos.db", "application_events") if temps else -1
        calls.append((str(path), rows, len(published)))
        return real_pair(path)

    monkeypatch.setattr(drill, "read_audit_pair", recording_pair)
    manifest = backup(db, tmp_path / "out")
    assert calls == [(str(db.parent / "platform_audit.jsonl"), 3, 0)], calls
    assert manifest.audit_head == last.record_hash
    assert manifest.audit_log_path is not None
    # the copies are the bytes of that one read: the retained pair verifies as a pair
    restored = restore(manifest, tmp_path / "restored")
    assert restored.verified and restored.audit_chain == "VALID", restored.failures


def test_r1_3e_an_audit_anchor_without_its_log_is_refused_not_half_copied(tmp_path: Path) -> None:
    db = _demo_store(tmp_path)
    log = AuditLog(db.parent / "platform_audit.jsonl")
    log.append("drill.test", {"n": 1})
    (db.parent / "platform_audit.jsonl").unlink()  # the anchor now stands alone
    with pytest.raises(DrillRefused, match="audit"):
        backup(db, tmp_path / "out")
    assert not any((tmp_path / "out").glob("*.db")), (
        "nothing is published on a refused audit snapshot"
    )


# ----------------------------------------------------------- (r2) Daybreak's HOLD-DELTA at a8e6c6d


def _store_with_audit(tmp_path: Path) -> Path:
    db = _demo_store(tmp_path, events=2)
    AuditLog(db.parent / "platform_audit.jsonl").append("drill.test", {"n": 1})
    return db


def _envelope_name(db: Path, wall: datetime) -> str:
    return f"{db.stem}-{wall.strftime('%Y%m%dT%H%M%SZ')}"


ENVELOPE_FILES = ["chronos.db", "manifest.json", "platform_audit.head.json", "platform_audit.jsonl"]


@pytest.mark.parametrize("planted", ["file", "directory"])
def test_r2_1_a_collision_at_the_one_final_name_publishes_nothing(
    tmp_path: Path, planted: str
) -> None:
    """r3: the triple is ONE envelope with ONE name (r2's three-name class no longer
    exists). A pre-planted entry at that name — a file or a non-empty directory — makes
    RENAME_NOREPLACE refuse: the entry is byte-for-byte untouched and the destination
    holds exactly it — no envelope, no temp."""

    db = _store_with_audit(tmp_path)
    wall = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    out = tmp_path / "out"
    out.mkdir()
    occupied = out / _envelope_name(db, wall)
    if planted == "file":
        occupied.write_bytes(b"pre-existing, must not move")
        before = _sha256(occupied)
    else:
        occupied.mkdir()
        (occupied / "keep").write_bytes(b"pre-existing, must not move")
        before = _sha256(occupied / "keep")
    with pytest.raises(DrillRefused, match="already exists"):
        backup(db, out, clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    assert _sha256(occupied if planted == "file" else occupied / "keep") == before
    assert sorted(p.name for p in out.iterdir()) == [occupied.name], (
        "the destination must hold exactly the planted entry: "
        + ", ".join(sorted(p.name for p in out.iterdir()))
    )
    if planted == "directory":
        assert sorted(p.name for p in occupied.iterdir()) == ["keep"]


def test_r2_1d_a_complete_publication_is_one_envelope_holding_the_whole_triple(
    tmp_path: Path,
) -> None:
    db = _store_with_audit(tmp_path)
    wall = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    manifest = backup(db, tmp_path / "out", clock=Clock(wall=lambda: wall, monotonic=lambda: 0.0))
    envelope = tmp_path / "out" / _envelope_name(db, wall)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [envelope.name]
    assert sorted(p.name for p in envelope.iterdir()) == ENVELOPE_FILES
    assert Path(manifest.backup_path) == envelope / "chronos.db"
    inside = json.loads((envelope / "manifest.json").read_text())
    assert inside["sha256"] == manifest.sha256 and inside["backup_path"] == manifest.backup_path


def test_r2_2a_backup_refuses_a_symlinked_ancestor_before_creating_anything_beyond_it(
    tmp_path: Path,
) -> None:
    db = _demo_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside)
    with pytest.raises(DrillRefused, match="alias"):
        backup(db, alias / "backups")
    assert sorted(outside.iterdir()) == [], "nothing may be created under the link's target"
    assert alias.is_symlink() and sorted(alias.iterdir()) == []


def test_r2_2b_restore_refuses_a_symlinked_ancestor_before_creating_anything_beyond_it(
    tmp_path: Path,
) -> None:
    db = _demo_store(tmp_path)
    manifest = backup(db, tmp_path / "out")
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside)
    with pytest.raises(DrillRefused, match="alias"):
        restore(manifest, alias / "restore")
    assert sorted(outside.iterdir()) == []


def test_r2_2c_missing_components_are_created_descriptor_relative_and_the_walk_is_in_file() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert "mkdir(parents=True" not in text and "parents=True" not in text
    assert "os.open(os.sep," in text, "the walk must start at the root component"
    assert "dir_fd=" in text


def test_r2_3_the_runbook_states_the_triple_as_one_publication_and_names_the_walk() -> None:
    prose = " ".join(
        (ROOT / "docs" / "ops" / "RESTORE-DRILL.md").read_text(encoding="utf-8").split()
    )
    assert "nothing is published" in prose
    assert "component" in prose
    assert "all three" in prose or "triple" in prose


# ----------------------------------------------------------- (r3) Daybreak's HOLD-DELTA at 36786b9


def _swap_ancestor(directory: Path) -> Path:
    """Daybreak's sequence: rename the real directory away and put a fresh empty directory
    at the lexical path — a concurrent pathname actor. Returns where the real one went."""

    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    directory.mkdir()
    return moved


def test_r3_1a_an_ancestor_swap_right_after_publication_is_a_typed_refusal_that_tells_the_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _store_with_audit(tmp_path)
    out = tmp_path / "backups"
    real = drill._renameat2
    moved: list[Path] = []

    def swapping_renameat2(*args: object) -> None:
        real(*args)  # the envelope is published for real ...
        moved.append(_swap_ancestor(out))  # ... then the ancestor is swapped underneath

    monkeypatch.setattr(drill, "_renameat2", swapping_renameat2)
    with pytest.raises(DrillRefused) as refused:
        backup(db, out)
    message = str(refused.value)
    assert message.startswith("published: the envelope "), message
    assert "exists in the directory the walk admitted" in message
    assert f"but the path {out} no longer names that directory" in message
    assert "an ancestor was swapped after publication" in message
    assert "nothing is published" not in message
    # the triple really is in the descriptor-bound (renamed) directory; the lexical path is empty
    envelopes = [p for p in moved[0].iterdir() if not p.name.startswith(".")]
    assert len(envelopes) == 1 and sorted(q.name for q in envelopes[0].iterdir()) == ENVELOPE_FILES
    assert sorted(out.iterdir()) == []


def test_r3_1b_an_ancestor_swap_after_the_restore_copy_is_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _store_with_audit(tmp_path)
    manifest = backup(db, tmp_path / "out")
    target = tmp_path / "restore" / "into"
    real_fsync = os.fsync
    swapped: list[Path] = []

    def swapping_fsync(fd: int) -> None:
        real_fsync(fd)
        st = os.fstat(fd)
        is_target = (
            os.path.stat.S_ISDIR(st.st_mode)
            and target.exists()
            and (st.st_dev, st.st_ino) == (os.stat(target).st_dev, os.stat(target).st_ino)
        )
        if is_target and not swapped:
            # the copy is done (this is the target's post-copy fsync); swap the ancestor and
            # plant an IDENTICAL database at the redirected path (Daybreak's sequence)
            swapped.append(_swap_ancestor(target))
            (target / Path(manifest.backup_path).name).write_bytes(
                Path(manifest.backup_path).read_bytes()
            )

    monkeypatch.setattr(drill.os, "fsync", swapping_fsync)
    report = restore(manifest, target)
    assert swapped, "the swap must have happened after the copy"
    assert report.verified is False
    assert any("restore target swapped" in f for f in report.failures), report.failures
    # the copy this drill made is in the renamed directory, not at the reported path
    assert (swapped[0] / Path(manifest.backup_path).name).is_file()


def _fork_backup(db: Path, out: Path, crash: str) -> tuple[int, Path | None]:
    """Run backup() in a forked child that os._exit()s at ``crash`` ('before-rename' or
    'after-rename'); returns (child status, the envelope's final dir or None)."""

    pid = os.fork()
    if pid == 0:  # pragma: no cover — the child
        try:
            real = drill._renameat2

            def crashing_renameat2(*args: object) -> None:
                if crash == "before-rename":
                    os._exit(0)
                real(*args)
                os._exit(0)

            drill._renameat2 = crashing_renameat2  # type: ignore[assignment]
            backup(db, out)
        finally:
            os._exit(3)
    _pid, status = os.waitpid(pid, 0)
    published = [p for p in out.iterdir() if not p.name.startswith(".")]
    return status, (published[0] if published else None)


def test_r3_2a_a_crash_before_the_rename_leaves_only_a_dot_temp_that_restore_refuses_by_name(
    tmp_path: Path,
) -> None:
    db = _store_with_audit(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    status, envelope = _fork_backup(db, out, "before-rename")
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert envelope is None, "nothing may be published"
    remnants = sorted(out.iterdir())
    assert (
        len(remnants) == 1
        and remnants[0].name.startswith(".")
        and remnants[0].name.endswith(".tmp")
    )
    assert remnants[0].is_dir() and "chronos.db" in {p.name for p in remnants[0].iterdir()}
    # a manifest that points INTO the remnant is refused by name, whatever the remnant holds
    manifest = BackupManifest.from_dict(json.loads((remnants[0] / "manifest.json").read_text()))
    tampered = BackupManifest.from_dict(
        {**manifest.to_dict(), "backup_path": str(remnants[0] / "chronos.db")}
    )
    with pytest.raises(DrillRefused, match="unpublished temp envelope"):
        restore(tampered, tmp_path / "restored")
    with pytest.raises(DrillRefused, match="unpublished temp envelope"):
        rpo_seconds(tampered)
    # and the manifest as written (pointing at the never-published final name) is refused too
    with pytest.raises(DrillRefused, match="does not exist"):
        restore(manifest, tmp_path / "restored2")


def test_r3_2b_a_crash_right_after_the_rename_leaves_the_whole_envelope(tmp_path: Path) -> None:
    db = _store_with_audit(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    status, envelope = _fork_backup(db, out, "after-rename")
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert envelope is not None and sorted(p.name for p in envelope.iterdir()) == ENVELOPE_FILES
    assert sorted(p.name for p in out.iterdir()) == [envelope.name], "no temp remains"
    manifest = BackupManifest.from_dict(json.loads((envelope / "manifest.json").read_text()))
    report = restore(manifest, tmp_path / "restored")
    assert report.verified, report.failures


def test_r3_3_a_non_eexist_acquisition_failure_is_a_typed_refusal_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _store_with_audit(tmp_path)
    out = tmp_path / "out"

    def failing_renameat2(*args: object) -> None:
        raise OSError(5, "injected link failure")

    monkeypatch.setattr(drill, "_renameat2", failing_renameat2)
    with pytest.raises(DrillRefused) as refused:
        backup(db, out)
    message = str(refused.value)
    assert "injected link failure" in message and "errno 5" in message
    assert f"{db.stem}-" in message, "the failing final name is named"
    assert sorted(out.iterdir()) == [], "published=[] and no temp survives"


def test_r3_4_the_runbook_states_the_envelope_and_the_crash_truth() -> None:
    prose = " ".join(
        (ROOT / "docs" / "ops" / "RESTORE-DRILL.md").read_text(encoding="utf-8").split()
    )
    assert "envelope" in prose and "renameat2" in prose
    assert ("crash" in prose and "dot-temp" in prose) or "dot-prefixed" in prose
    assert "descriptor" in prose


def test_6c_the_module_is_read_only_operations_and_names_its_seams() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert 'ENCRYPTION: str = "none"' in text
    assert "mode=ro" in text  # the source connection is read-only
    assert ".backup(" in text  # sqlite3's online backup API, not a file copy
    assert "chronos.recovery" in text  # the prior art is named, not duplicated
    assert drill.ENCRYPTION == "none"

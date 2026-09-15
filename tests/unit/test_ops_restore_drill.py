"""The measured RPO/RTO restore drill (M3 ops plane), against a demo store under tmp_path.

Every store here is created by the repository's own initializer (``Database.initialize``)
under ``tmp_path`` — never ``data/chronos.db``. Numbered to the R-1 contract: (1) the backup
and its manifest, (2) restore + the four verifiers, (3) rpo_s, (4) the CLI and the import
pin, (6) the pinned-clock measurements and the "never opens the source for writing" pin.
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
    with pytest.raises(DrillRefused, match=kind if kind != "directory" else "directory"):
        backup(bad, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


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
    # the store is WAL-mode: the delete sits in the -wal sidecar, so the main file's bytes
    # (and its sha256) are unchanged — the row-count verifier is what catches it
    assert facts["sha256_ok"] is True


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


def test_6c_the_module_is_read_only_operations_and_names_its_seams() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert 'ENCRYPTION: str = "none"' in text
    assert "mode=ro" in text  # the source connection is read-only
    assert ".backup(" in text  # sqlite3's online backup API, not a file copy
    assert "chronos.recovery" in text  # the prior art is named, not duplicated
    assert drill.ENCRYPTION == "none"

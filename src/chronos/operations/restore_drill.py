"""Measured RPO/RTO restore drill for the Chronos sqlite database (M3 ops plane).

One command takes a consistent backup of the application database, records a
manifest, restores it into an ISOLATED location, verifies the restored copy, and
reports two MEASURED numbers as JSON::

    python -m chronos.operations.restore_drill --db data/chronos.db \\
        --out /path/to/backups --restore-into /path/to/fresh-dir [--pretty]

What the numbers are, exactly:

- ``rpo_s`` — ``snapshot_completed_at`` (the instant the backup API returned,
  recorded once) minus the newest committed evidence timestamp read FROM THE
  RETAINED BACKUP and the retained copy of the audit log — never from the live
  source, which keeps moving after the backup. The report names which basis won
  (``rpo_basis``). A store with no timestamped evidence reports ``rpo_s: null``
  with a reason — never ``0``; evidence dated after the snapshot completed is a
  typed refusal — never a negative number.
- ``rto_s`` — wall-clock, monotonic, from the start of the restore to the moment
  the restored copy was verified (copy → sha256 → schema head → row counts →
  audit chain). It is the time this host takes to restore and verify THIS store;
  it is not a recovery objective, and it does not include finding the backup,
  the operator, or the backend boot (which recovers under a hold — ADR-0054).

What the drill does and does not do:

- The backup uses sqlite3's online backup API (``Connection.backup``) from a
  read-only (``mode=ro``) connection under SQLite's normal locking and WAL
  semantics — never ``immutable=1``, never inferred from a sidecar precheck, so a
  WAL that appears after any check is still honoured — never a file copy of a
  live database. The source is never opened for writing.
- The destination is the O_EXCL|O_NOFOLLOW temp DESCRIPTOR itself (sqlite opens
  ``/proc/self/fd/<n>``, identity re-checked by fstat/stat), stored in
  rollback-journal mode so the backup is one self-contained file; the final
  name is acquired with ``link`` (EEXIST → typed refusal, never a rename over an
  existing file) and the directory is fsynced. The audit log and its head
  anchor are read ONCE as a pair after the backup completed (``read_audit_pair``)
  and both copies are written from that read; a half pair is a refusal.
- The restore goes into a fresh directory the caller names; an existing
  non-empty target is refused. Nothing here ever writes to the live path.
- The schema head is the alembic revision when the store carries an
  ``alembic_version`` table, else the ``schema_version`` row (a store created by
  ``Database.initialize()`` carries no alembic table). The head check on the
  restored copy compares that value AND runs the repository's own acceptance
  (``Database.initialize()`` on an already-versioned store verifies the version
  and zero drift and applies no upgrade).
- Encryption is the owner's ask; the manifest's ``encryption`` field is the seam
  and reads ``"none"`` tonight. Off-host placement is likewise not done here.
- ``chronos.recovery`` (``python -m chronos.recovery``) captures the WHOLE data
  directory and observes snapshot age and restore elapsed; this module is the
  database-only drill with per-table verification and a backup-time RPO. It
  imports no order, execution, autonomy or supervisor module (pinned).
- ``tests/integration/test_backup_restore_drill.py`` is the isolated drill over
  the real WAL-backed stores (artifact integrity, fail-closed recovery posture;
  it says it does not prove RPO/RTO). This harness builds on its posture — the
  online backup API over a store whose committed rows still sit in ``-wal`` —
  and adds the manifest, the two measured numbers and the operator runbook.

Every source and backup file is opened ``O_NOFOLLOW|O_NONBLOCK`` and ``fstat``'d
regular before use: a symlink, FIFO or directory at a path is a typed refusal.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, column, func, select, table
from sqlalchemy.dialects import sqlite as sqlite_dialect

from chronos.auditlog.log import (
    AuditLogCorruptionError,
    ChainState,
    read_audit_pair,
    verify_chain,
)
from chronos.persistence.database import Database

ENCRYPTION: str = "none"
AUDIT_LOG_NAME = "platform_audit.jsonl"
AUDIT_ANCHOR_NAME = "platform_audit.head.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CHUNK = 1 << 20


class DrillRefused(RuntimeError):
    """A typed refusal: the message is the whole diagnosis. Nothing was written."""


@dataclass(frozen=True, slots=True)
class Clock:
    """Wall clock (for ``taken_at`` and RPO) and monotonic clock (for RTO)."""

    wall: Callable[[], datetime]
    monotonic: Callable[[], float]


def system_clock() -> Clock:
    return Clock(wall=lambda: datetime.now(UTC), monotonic=time.monotonic)


@dataclass(frozen=True, slots=True)
class BackupManifest:
    taken_at: str
    snapshot_completed_at: str
    source_path: str
    backup_path: str
    sha256: str
    schema_head: str
    row_counts: dict[str, int]
    audit_head: str | None
    audit_log_path: str | None
    encryption: str = ENCRYPTION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> BackupManifest:
        return cls(
            taken_at=str(data["taken_at"]),
            snapshot_completed_at=str(data["snapshot_completed_at"]),
            source_path=str(data["source_path"]),
            backup_path=str(data["backup_path"]),
            sha256=str(data["sha256"]),
            schema_head=str(data["schema_head"]),
            row_counts={str(k): int(v) for k, v in dict(data["row_counts"]).items()},  # type: ignore[call-overload]
            audit_head=None if data.get("audit_head") is None else str(data["audit_head"]),
            audit_log_path=None
            if data.get("audit_log_path") is None
            else str(data["audit_log_path"]),
            encryption=str(data.get("encryption", ENCRYPTION)),
        )


@dataclass(frozen=True, slots=True)
class RestoreReport:
    target_dir: str
    restored_path: str
    sha256_ok: bool
    schema_head_ok: bool
    row_counts_ok: bool
    audit_chain: str  # VALID | BROKEN | ABSENT | NOT_APPLICABLE
    failures: tuple[str, ...]
    rto_s: float

    @property
    def verified(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["failures"] = list(self.failures)
        data["verified"] = self.verified
        return data


@dataclass(frozen=True, slots=True)
class DrillReport:
    manifest: BackupManifest
    restore: RestoreReport | None
    rpo_s: float | None
    rpo_basis: dict[str, object]
    rto_s: float | None
    verdict: str
    failures: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.to_dict(),
            "restore": None if self.restore is None else self.restore.to_dict(),
            "rpo_s": self.rpo_s,
            "rpo_basis": dict(self.rpo_basis),
            "rto_s": self.rto_s,
            "verdict": self.verdict,
            "failures": list(self.failures),
        }


# ----------------------------------------------------------------- file discipline


def _open_regular(path: Path, subject: str) -> int:
    """Open ``path`` read-only, no-follow, non-blocking; refuse anything but a regular file."""

    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise DrillRefused(
                f"{subject} {path} is a symlink; a regular file is required"
            ) from None
        if error.errno == errno.ENOENT:
            raise DrillRefused(f"{subject} {path} does not exist") from None
        raise DrillRefused(f"{subject} {path} could not be opened: {error.strerror}") from None
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        os.close(fd)
        kind = (
            "a fifo"
            if stat.S_ISFIFO(mode)
            else "a directory"
            if stat.S_ISDIR(mode)
            else "not a regular file"
        )
        raise DrillRefused(f"{subject} {path} is {kind}; a regular file is required")
    return fd


def _sha256_of(path: Path, subject: str) -> str:
    fd = _open_regular(path, subject)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
    finally:
        pass
    return digest.hexdigest()


def _open_directory(directory: Path, what: str, *, create: bool) -> int:
    """A directory fd (O_DIRECTORY|O_NOFOLLOW, 0700 when created); every create, link,
    unlink and fsync below is relative to it, so a path swapped underneath cannot
    redirect a write."""

    if directory.is_symlink():
        raise DrillRefused(f"{what} {directory} is a symlink; name a real directory")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise DrillRefused(
            f"{what} {directory} could not be opened as a directory: {error.strerror}"
        ) from None


def _create_exclusive_at(
    dfd: int, name: str, mode: int = 0o600, *, flags: int = os.O_WRONLY
) -> int:
    """An O_EXCL|O_NOFOLLOW regular file created by NAME against the directory fd; EEXIST
    is a typed refusal (never a rename or link over an existing entry)."""

    try:
        return os.open(
            name, flags | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=dfd
        )
    except FileExistsError:
        raise DrillRefused(
            f"{name} already exists in the destination; the drill never overwrites"
        ) from None


def _publish(dfd: int, tmp_name: str, final_name: str) -> None:
    """Acquire the final name with ``link`` (EEXIST → refusal), drop the temp name, fsync
    the directory. The published entry is the very inode the temp descriptor wrote."""

    try:
        os.link(tmp_name, final_name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except FileExistsError:
        os.unlink(tmp_name, dir_fd=dfd)
        raise DrillRefused(
            f"{final_name} already exists in the destination; the drill never overwrites"
        ) from None
    os.unlink(tmp_name, dir_fd=dfd)
    os.fsync(dfd)


def _write_bytes_at(dfd: int, name: str, payload: bytes) -> None:
    fd = _create_exclusive_at(dfd, name)
    try:
        with os.fdopen(fd, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        os.unlink(name, dir_fd=dfd)
        raise


def _copy_regular(source: Path, destination_dir: Path, name: str, subject: str) -> Path:
    """Copy a regular file (opened no-follow, fstat'd) into ``destination_dir`` under
    ``name`` — O_EXCL against the directory fd, fsynced, then the directory fsynced."""

    src_fd = _open_regular(source, subject)
    dfd = _open_directory(destination_dir, "destination", create=False)
    try:
        dst_fd = _create_exclusive_at(dfd, name)
        try:
            with os.fdopen(src_fd, "rb") as reader, os.fdopen(dst_fd, "wb") as writer:
                for chunk in iter(lambda: reader.read(_CHUNK), b""):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        except BaseException:
            os.unlink(name, dir_fd=dfd)
            raise
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return destination_dir / name


def _fresh_directory(target: Path, what: str) -> None:
    """The target must be absent or an empty directory (never a symlink); created 0700."""

    if target.is_symlink():
        raise DrillRefused(f"{what} {target} is a symlink; name a real directory")
    if target.exists():
        if not target.is_dir():
            raise DrillRefused(f"{what} {target} exists and is not a directory")
        if any(target.iterdir()):
            raise DrillRefused(
                f"{what} {target} is not empty; the drill never restores over existing files"
            )
        return
    target.mkdir(mode=0o700, parents=True)


# ----------------------------------------------------------------- store inspection


def _readonly(path: Path) -> sqlite3.Connection:
    """A read-only connection under SQLite's normal locking and WAL semantics.

    Never ``immutable=1``: that flag makes SQLite ignore a WAL, and "no ``-wal`` right
    now" is not a fact about the next instant (Daybreak's R-1 probe committed a row
    between such a check and the open). A read-only reader on a WAL-mode file may
    create empty ``-wal``/``-shm`` beside it — a directory write, never a database
    write; the retained copies are stored in rollback-journal mode so readers leave
    nothing beside them.
    """

    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _count_statement(table_name: str) -> str:
    """``SELECT count(*) FROM <table>`` with SQLAlchemy's identifier quoting — the table
    names come from ``sqlite_master``, never from a caller, and no SQL text is assembled
    by hand."""

    statement = select(func.count()).select_from(table(table_name))
    return str(statement.compile(dialect=sqlite_dialect.dialect()))


def _max_statement(table_name: str, column_name: str) -> str:
    statement: Select[Any] = select(func.max(column(column_name))).select_from(table(table_name))
    return str(statement.compile(dialect=sqlite_dialect.dialect()))


def row_counts(path: Path) -> dict[str, int]:
    """Every user table's row count, read-only."""

    with _readonly(path) as connection:
        return {
            name: int(connection.execute(_count_statement(name)).fetchone()[0])
            for name in _user_tables(connection)
        }


def schema_head(path: Path) -> str:
    """The alembic revision when the store carries one, else ``schema_version:<n>``."""

    with _readonly(path) as connection:
        tables = set(_user_tables(connection))
        if "alembic_version" in tables:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if revision is not None:
                return f"alembic:{revision[0]}"
        if "schema_version" in tables:
            version = connection.execute(
                "SELECT version FROM schema_version ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if version is not None:
                return f"schema_version:{version[0]}"
    return "unversioned"


def _parse_utc(value: object) -> datetime | None:
    """SQLAlchemy stores naive ``YYYY-MM-DD HH:MM:SS[.ffffff]`` (UTC by repo convention);
    the audit log stores ISO 8601 with an offset. Both read as aware UTC; junk is None."""

    if not isinstance(value, str) or not value:
        return None
    text = value.replace("T", " ", 1) if "T" in value and value[10:11] == "T" else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def newest_evidence(
    path: Path, audit_log: Path | None
) -> tuple[datetime | None, dict[str, object]]:
    """The newest committed evidence timestamp in the source and which basis produced it."""

    newest: datetime | None = None
    candidates = 0
    basis: dict[str, object] = {"source": None, "newest_evidence_at": None, "candidates": 0}
    with _readonly(path) as connection:
        for table_name in _user_tables(connection):
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quote(table_name)})").fetchall()
                if str(row[1]).endswith("_at")
            ]
            for column_name in columns:
                value = connection.execute(_max_statement(table_name, column_name)).fetchone()[0]
                stamp = _parse_utc(value)
                if stamp is None:
                    continue
                candidates += 1
                if newest is None or stamp > newest:
                    newest, basis["source"] = stamp, f"table:{table_name}.{column_name}"
    if audit_log is not None and audit_log.is_file():
        try:
            text, _anchor = read_audit_pair(audit_log)
        except Exception as error:  # a refused audit read is a basis, not a crash
            basis["audit_log_error"] = str(error)
            text = None
        last = (text or "").rstrip("\n").rsplit("\n", 1)[-1] if text else ""
        if last:
            try:
                stamp = _parse_utc(json.loads(last).get("at_utc"))
            except (ValueError, AttributeError):
                stamp = None
            if stamp is not None:
                candidates += 1
                if newest is None or stamp > newest:
                    newest, basis["source"] = stamp, "audit_log:last_entry.at_utc"
    basis["candidates"] = candidates
    if newest is not None:
        basis["newest_evidence_at"] = newest.isoformat()
    return newest, basis


def _decode_audit_head(raw: bytes, anchor_path: Path) -> str:
    """The head anchor's ``last_hash`` (64 hex) from the anchor's bytes."""

    try:
        decoded = json.loads(raw.decode("utf-8"))
        last_hash = str(decoded["last_hash"])
    except (ValueError, KeyError, TypeError) as error:
        raise DrillRefused(
            f"audit anchor {anchor_path} is not a valid head anchor: {error}"
        ) from None
    return last_hash


def _audit_head(anchor_path: Path) -> str | None:
    """The head anchor's ``last_hash`` (64 hex) or None when there is no anchor."""

    if not anchor_path.is_file():
        return None
    fd = _open_regular(anchor_path, "audit anchor")
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read()
    return _decode_audit_head(raw, anchor_path)


# ----------------------------------------------------------------- backup


def backup(db_path: Path, out_dir: Path, *, clock: Clock | None = None) -> BackupManifest:
    """A consistent online backup of ``db_path`` into ``out_dir`` plus its manifest.

    The source is opened read-only (``mode=ro``, normal SQLite locking) and never
    written. The backup is written into the O_EXCL temp DESCRIPTOR (sqlite opens
    ``/proc/self/fd/<n>``), fsynced, and published with ``link`` — EEXIST is a
    refusal, nothing is ever renamed over an existing file — then the directory is
    fsynced. ``snapshot_completed_at`` is the instant the backup API returned. The
    audit log and its head anchor beside the source are read once as a pair AFTER
    that instant and both copies are written from that read, so the retained chain
    is one stated snapshot; a half pair is a refusal.
    """

    clock = clock or system_clock()
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    fd = _open_regular(db_path, "source database")
    os.close(fd)
    dfd = _open_directory(out_dir, "backup directory", create=True)
    try:
        taken_at = clock.wall()
        stamp = taken_at.strftime("%Y%m%dT%H%M%SZ")
        final_name = f"{db_path.stem}-{stamp}.db"
        if os.path.lexists(os.path.join(out_dir, final_name)):
            raise DrillRefused(
                f"{final_name} already exists in the destination; the drill never overwrites"
            )
        tmp_name = f".{final_name}.{secrets.token_hex(6)}.tmp"
        tmp_fd = _create_exclusive_at(dfd, tmp_name, flags=os.O_RDWR)
        audit_log = db_path.parent / AUDIT_LOG_NAME
        try:
            identity = os.fstat(tmp_fd)
            # sqlite writes THIS descriptor's inode (/proc/self/fd/<n>), never a
            # re-resolved pathname; journal OFF so no sibling journal is needed there
            destination = sqlite3.connect(f"/proc/self/fd/{tmp_fd}")
            try:
                destination.execute("PRAGMA journal_mode=OFF")
                with _readonly(db_path) as source:
                    source.backup(destination)
                snapshot_completed_at = clock.wall()
                # one self-contained file: readers of the retained copy leave no sidecars
                destination.execute("PRAGMA journal_mode=DELETE")
            finally:
                destination.close()
            after = os.stat(tmp_name, dir_fd=dfd)
            if (after.st_dev, after.st_ino) != (identity.st_dev, identity.st_ino):
                raise DrillRefused(f"the temp entry {tmp_name} changed identity during the backup")
            os.fsync(tmp_fd)
            # the audit pair, read ONCE as a pair AFTER the backup completed (anchor before
            # log, no-follow, one capability read each — the audit log's own reader); a
            # refused or half pair unlinks the unpublished backup: nothing is published
            try:
                log_text, anchor_bytes = read_audit_pair(audit_log)
            except AuditLogCorruptionError as error:
                raise DrillRefused(
                    f"audit pair beside the source could not be read as one snapshot: {error}"
                ) from None
            if (log_text is None) != (anchor_bytes is None):
                raise DrillRefused(
                    "audit pair beside the source is incomplete (a log without its anchor or "
                    "an anchor without its log); refusing to retain half a chain"
                )
        except BaseException:
            os.close(tmp_fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dfd)
            raise
        os.close(tmp_fd)
        _publish(dfd, tmp_name, final_name)
        backup_path = out_dir / final_name

        # both audit copies are written from that single read
        audit_head: str | None = None
        audit_copy: Path | None = None
        if log_text is not None and anchor_bytes is not None:
            audit_head = _decode_audit_head(anchor_bytes, audit_log.with_name(AUDIT_ANCHOR_NAME))
            stem = backup_path.stem
            _write_bytes_at(dfd, f"{stem}.{AUDIT_LOG_NAME}", log_text.encode("utf-8"))
            _write_bytes_at(dfd, f"{stem}.{AUDIT_ANCHOR_NAME}", anchor_bytes)
            os.fsync(dfd)
            audit_copy = out_dir / f"{stem}.{AUDIT_LOG_NAME}"
    finally:
        os.close(dfd)

    return BackupManifest(
        taken_at=taken_at.isoformat(),
        snapshot_completed_at=snapshot_completed_at.isoformat(),
        source_path=str(db_path),
        backup_path=str(backup_path),
        sha256=_sha256_of(backup_path, "backup"),
        schema_head=schema_head(backup_path),
        row_counts=row_counts(backup_path),
        audit_head=audit_head,
        audit_log_path=None if audit_copy is None else str(audit_copy),
    )


# ----------------------------------------------------------------- restore + verify


def verify_restored(
    manifest: BackupManifest, restored: Path, restored_audit_log: Path | None
) -> tuple[list[str], dict[str, bool | str]]:
    """The four verifiers against a restored copy; returns (failures, facts)."""

    failures: list[str] = []
    facts: dict[str, bool | str] = {}

    actual = _sha256_of(restored, "restored copy")
    facts["sha256_ok"] = actual == manifest.sha256
    if not facts["sha256_ok"]:
        failures.append(f"sha256: restored copy {actual[:12]}… != manifest {manifest.sha256[:12]}…")

    head = schema_head(restored)
    accepted = True
    try:
        database = Database(f"sqlite:///{restored}")
        try:
            # verifies version + zero drift on a versioned store; applies no upgrade
            database.initialize()
        finally:
            database.dispose()
    except RuntimeError as error:
        accepted = False
        failures.append(f"schema_acceptance: {error}")
    facts["schema_head_ok"] = head == manifest.schema_head and accepted
    if head != manifest.schema_head:
        failures.append(f"schema_head: restored {head} != manifest {manifest.schema_head}")

    counts = row_counts(restored)
    facts["row_counts_ok"] = counts == manifest.row_counts
    if not facts["row_counts_ok"]:
        differing = sorted(
            table
            for table in set(counts) | set(manifest.row_counts)
            if counts.get(table) != manifest.row_counts.get(table)
        )
        failures.append(f"row_counts: differ in {', '.join(differing)}")

    if manifest.audit_head is None:
        facts["audit_chain"] = "NOT_APPLICABLE"
    elif restored_audit_log is None:
        facts["audit_chain"] = "ABSENT"
        failures.append(
            "audit_chain: the manifest records an audit head but no audit log was restored"
        )
    else:
        verdict = verify_chain(restored_audit_log)
        facts["audit_chain"] = verdict.state.value
        if verdict.state is not ChainState.VALID:
            failures.append(f"audit_chain: {verdict.state.value} — {verdict.detail}")
        else:
            head_now = _audit_head(
                restored_audit_log.with_name(
                    restored_audit_log.name.replace(AUDIT_LOG_NAME, AUDIT_ANCHOR_NAME)
                )
            )
            if head_now != manifest.audit_head:
                failures.append(
                    "audit_chain: the restored anchor's last hash differs from the manifest's "
                    "audit_head"
                )
    return failures, facts


def restore(
    manifest: BackupManifest, target_dir: Path, *, clock: Clock | None = None
) -> RestoreReport:
    """Copy the backup into a fresh directory and verify it; rto_s from start to verified."""

    clock = clock or system_clock()
    target_dir = Path(target_dir)
    started = clock.monotonic()
    _fresh_directory(target_dir, "restore target")
    backup_path = Path(manifest.backup_path)
    restored = _copy_regular(backup_path, target_dir, backup_path.name, "backup")
    restored_audit: Path | None = None
    if manifest.audit_log_path is not None:
        audit_src = Path(manifest.audit_log_path)
        anchor_src = audit_src.with_name(audit_src.name.replace(AUDIT_LOG_NAME, AUDIT_ANCHOR_NAME))
        restored_audit = _copy_regular(audit_src, target_dir, AUDIT_LOG_NAME, "audit log")
        _copy_regular(anchor_src, target_dir, AUDIT_ANCHOR_NAME, "audit anchor")
    failures, facts = verify_restored(manifest, restored, restored_audit)
    verified_at = clock.monotonic()
    return RestoreReport(
        target_dir=str(target_dir),
        restored_path=str(restored),
        sha256_ok=bool(facts["sha256_ok"]),
        schema_head_ok=bool(facts["schema_head_ok"]),
        row_counts_ok=bool(facts["row_counts_ok"]),
        audit_chain=str(facts["audit_chain"]),
        failures=tuple(failures),
        rto_s=verified_at - started,
    )


# ----------------------------------------------------------------- the drill


def rpo_seconds(manifest: BackupManifest) -> tuple[float | None, dict[str, object]]:
    """``snapshot_completed_at`` minus the newest committed evidence IN THE RETAINED
    SNAPSHOT (the backup file and the retained audit copy) — never the live source.
    None + reason when the snapshot carries no timestamped evidence; evidence dated
    after the snapshot completed is a typed refusal, never a negative number."""

    retained = Path(manifest.backup_path)
    audit_copy = None if manifest.audit_log_path is None else Path(manifest.audit_log_path)
    newest, basis = newest_evidence(retained, audit_copy)
    basis["evidence_read_from"] = "retained snapshot"
    snapshot = datetime.fromisoformat(manifest.snapshot_completed_at)
    basis["snapshot_completed_at"] = snapshot.isoformat()
    if newest is None:
        basis["reason"] = (
            "no timestamped evidence in the retained snapshot "
            "(no *_at column carries a value and no audit entry)"
        )
        return None, basis
    if newest > snapshot:
        raise DrillRefused(
            f"evidence in the retained snapshot ({basis['source']}, {newest.isoformat()}) is "
            f"dated after the snapshot completed ({snapshot.isoformat()}); refusing to report a "
            "negative rpo_s — check the clocks of the writers and of this host"
        )
    return (snapshot - newest).total_seconds(), basis


def run_drill(
    db_path: Path, out_dir: Path, restore_into: Path, *, clock: Clock | None = None
) -> DrillReport:
    clock = clock or system_clock()
    manifest = backup(db_path, out_dir, clock=clock)
    failures: list[str] = []
    rpo: float | None
    try:
        rpo, basis = rpo_seconds(manifest)
    except DrillRefused as error:
        rpo, basis = None, {"source": None, "refused": str(error)}
        failures.append(f"rpo: refused — {error}")
    report = restore(manifest, restore_into, clock=clock)
    failures.extend(report.failures)
    return DrillReport(
        manifest=manifest,
        restore=report,
        rpo_s=rpo,
        rpo_basis=basis,
        rto_s=report.rto_s,
        verdict="VERIFIED" if not failures else "FAILED",
        failures=tuple(failures),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.restore_drill",
        description=(
            "Backup the Chronos sqlite database, restore it into an isolated directory, "
            "verify, and report measured rpo_s / rto_s."
        ),
    )
    parser.add_argument(
        "--db", required=True, type=Path, help="the source database (never opened for writing)"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="directory that receives the backup + manifest"
    )
    parser.add_argument(
        "--restore-into",
        required=True,
        type=Path,
        help="a FRESH directory for the isolated restore",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_drill(args.db, args.out, args.restore_into)
    except DrillRefused as error:
        refused: dict[str, object] = {"verdict": "FAILED", "failures": [f"refused: {error}"]}
        print(json.dumps(refused, indent=2 if args.pretty else None, sort_keys=True))
        return 2
    payload = report.to_dict()
    manifest_path = Path(report.manifest.backup_path).with_suffix(".manifest.json")
    dfd = _open_directory(manifest_path.parent, "backup directory", create=False)
    try:
        body = json.dumps(report.manifest.to_dict(), indent=2, sort_keys=True) + "\n"
        _write_bytes_at(dfd, manifest_path.name, body.encode("utf-8"))
        os.fsync(dfd)
    finally:
        os.close(dfd)
    payload["manifest_path"] = str(manifest_path)
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report.verdict == "VERIFIED" else 2


if __name__ == "__main__":
    sys.exit(main())

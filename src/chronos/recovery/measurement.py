"""Capture and restore a bounded Chronos recovery snapshot.

The observations emitted here are inputs to an operational RPO/RTO campaign,
not objectives or guarantees.  Source artifacts are opened read-only.  Output
roots must not exist and this module has no deletion, overwrite, broker, or
network capability.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy.exc import SQLAlchemyError

from chronos.auditlog.log import verify_chain
from chronos.control.halt import HaltReason, HaltStore
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.persistence.database import SCHEMA_VERSION, Database

SNAPSHOT_SCHEMA_VERSION: Final = "chronos-recovery-snapshot-v1"
OBSERVATION_SCHEMA_VERSION: Final = "chronos-recovery-observation-v1"
SNAPSHOT_MANIFEST_NAME: Final = "snapshot-manifest.json"
RECOVERY_OBSERVATION_NAME: Final = "recovery-observation.json"

_SQLITE_ARTIFACTS: Final = ("platform_ledger.db", "chronos.db")
_FILE_ARTIFACTS: Final = (
    "platform_halt.json",
    "live_kill_switch.json",
    "platform_audit.jsonl",
)
_REQUIRED_ARTIFACTS: Final = (*_SQLITE_ARTIFACTS, *_FILE_ARTIFACTS)
_PLATFORM_TABLES: Final = frozenset({"schema_info", "intents", "transitions", "fills"})
_PLATFORM_SCHEMA_VERSION: Final = 1
_CLOCK_BASIS: Final = "wall-clock UTC for age; monotonic clock for durations"
_RESIDUALS: Final = (
    "not an operational RPO or RTO objective",
    "snapshot age is meaningful only when wall-clock synchronization is verified",
    "no off-host placement, encryption, retention, or external integrity anchor is verified",
    "no owner mandate, broker state, order reconciliation, or permission to rearm is verified",
)


class RecoveryMeasurementError(RuntimeError):
    """The requested recovery measurement cannot produce trustworthy evidence."""


class RecoveryClock(Protocol):
    """The two clock domains required for honest recovery observations."""

    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class _SystemRecoveryClock:
    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class ArtifactCapture:
    """Digest and capture window for one snapshot member."""

    name: str
    sha256: str
    size_bytes: int
    capture_started_at_utc: datetime
    capture_completed_at_utc: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "capture_started_at_utc": self.capture_started_at_utc.isoformat(),
            "capture_completed_at_utc": self.capture_completed_at_utc.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Content binding for a non-atomic, per-artifact recovery snapshot."""

    schema_version: str
    source_id: str
    capture_started_at_utc: datetime
    capture_completed_at_utc: datetime
    artifact_capture_elapsed_seconds: float
    artifacts: tuple[ArtifactCapture, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "capture_started_at_utc": self.capture_started_at_utc.isoformat(),
            "capture_completed_at_utc": self.capture_completed_at_utc.isoformat(),
            "artifact_capture_elapsed_seconds": self.artifact_capture_elapsed_seconds,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """One isolated local restore observation, deliberately below an SLO claim."""

    schema_version: str
    result: str
    source_id: str
    snapshot_manifest_sha256: str
    recovery_started_at_utc: datetime
    recovery_completed_at_utc: datetime
    oldest_snapshot_age_seconds: float
    snapshot_capture_window_seconds: float
    local_restore_copy_seconds: float
    local_verification_seconds: float
    local_recovery_elapsed_seconds: float
    clock_basis: str
    residuals: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result": self.result,
            "source_id": self.source_id,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "recovery_started_at_utc": self.recovery_started_at_utc.isoformat(),
            "recovery_completed_at_utc": self.recovery_completed_at_utc.isoformat(),
            "oldest_snapshot_age_seconds": self.oldest_snapshot_age_seconds,
            "snapshot_capture_window_seconds": self.snapshot_capture_window_seconds,
            "local_restore_copy_seconds": self.local_restore_copy_seconds,
            "local_verification_seconds": self.local_verification_seconds,
            "local_recovery_elapsed_seconds": self.local_recovery_elapsed_seconds,
            "clock_basis": self.clock_basis,
            "residuals": list(self.residuals),
        }


def capture_snapshot(
    *,
    source_data: Path,
    snapshot_root: Path,
    source_id: str,
    clock: RecoveryClock | None = None,
) -> SnapshotManifest:
    """Capture required recovery artifacts into a new, owner-only directory."""

    selected_clock = clock or _SystemRecoveryClock()
    normalized_source_id = _normalize_source_id(source_id)
    _assert_new_destination(snapshot_root)
    _assert_disjoint(source_data, snapshot_root)
    _verify_recovery_data(
        source_data,
        exact_application_schema=False,
        immutable_databases=False,
    )

    capture_started_at = _now_utc(selected_clock)
    capture_started_ns = selected_clock.monotonic_ns()
    _make_private_directory(snapshot_root)
    captures: list[tuple[str, datetime, datetime]] = []
    for name in _REQUIRED_ARTIFACTS:
        artifact_started_at = _now_utc(selected_clock)
        source = source_data / name
        destination = snapshot_root / name
        if name in _SQLITE_ARTIFACTS:
            _backup_sqlite(source, destination)
        else:
            _copy_new_file(source, destination)
        artifact_completed_at = _now_utc(selected_clock)
        if artifact_completed_at < artifact_started_at:
            raise RecoveryMeasurementError(f"wall clock moved backwards while capturing {name}")
        captures.append((name, artifact_started_at, artifact_completed_at))

    _verify_recovery_data(
        snapshot_root,
        exact_application_schema=True,
        immutable_databases=True,
    )
    artifacts = tuple(
        ArtifactCapture(
            name=name,
            sha256=_sha256(snapshot_root / name),
            size_bytes=(snapshot_root / name).stat().st_size,
            capture_started_at_utc=started_at,
            capture_completed_at_utc=completed_at,
        )
        for name, started_at, completed_at in captures
    )
    capture_completed_at = _now_utc(selected_clock)
    if capture_completed_at < capture_started_at:
        raise RecoveryMeasurementError("wall clock moved backwards during snapshot capture")
    capture_elapsed = _elapsed_seconds(capture_started_ns, selected_clock.monotonic_ns())
    manifest = SnapshotManifest(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        source_id=normalized_source_id,
        capture_started_at_utc=capture_started_at,
        capture_completed_at_utc=capture_completed_at,
        artifact_capture_elapsed_seconds=capture_elapsed,
        artifacts=artifacts,
    )
    _write_new_json(snapshot_root / SNAPSHOT_MANIFEST_NAME, manifest.to_dict())
    _fsync_directory(snapshot_root)
    return manifest


def restore_snapshot(
    *,
    snapshot_root: Path,
    restore_root: Path,
    clock: RecoveryClock | None = None,
) -> RecoveryObservation:
    """Restore and verify a captured snapshot into a new isolated directory."""

    selected_clock = clock or _SystemRecoveryClock()
    _assert_new_destination(restore_root)
    _assert_disjoint(snapshot_root, restore_root)
    manifest_path = snapshot_root / SNAPSHOT_MANIFEST_NAME
    manifest_bytes = _read_regular_file(manifest_path)
    manifest = _load_manifest(manifest_bytes)
    _verify_manifest_artifacts(snapshot_root, manifest)

    recovery_started_at = _now_utc(selected_clock)
    oldest_capture = min(item.capture_started_at_utc for item in manifest.artifacts)
    newest_capture = max(item.capture_completed_at_utc for item in manifest.artifacts)
    if recovery_started_at < newest_capture:
        raise RecoveryMeasurementError(
            "recovery wall clock precedes the snapshot capture window; clock evidence is invalid"
        )

    recovery_started_ns = selected_clock.monotonic_ns()
    _make_private_directory(restore_root)
    restored_data = restore_root / "data"
    _make_private_directory(restored_data)
    for artifact in manifest.artifacts:
        _copy_new_file(snapshot_root / artifact.name, restored_data / artifact.name)
    copy_completed_ns = selected_clock.monotonic_ns()

    _verify_manifest_artifacts(restored_data, manifest)
    _verify_recovery_data(
        restored_data,
        exact_application_schema=True,
        immutable_databases=True,
    )
    _verify_manifest_artifacts(restored_data, manifest)
    _fsync_directory(restored_data)
    verification_completed_ns = selected_clock.monotonic_ns()
    recovery_completed_at = _now_utc(selected_clock)
    if recovery_completed_at < recovery_started_at:
        raise RecoveryMeasurementError("wall clock moved backwards during snapshot restore")

    observation = RecoveryObservation(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        result="PASS",
        source_id=manifest.source_id,
        snapshot_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        recovery_started_at_utc=recovery_started_at,
        recovery_completed_at_utc=recovery_completed_at,
        oldest_snapshot_age_seconds=(recovery_started_at - oldest_capture).total_seconds(),
        snapshot_capture_window_seconds=(newest_capture - oldest_capture).total_seconds(),
        local_restore_copy_seconds=_elapsed_seconds(recovery_started_ns, copy_completed_ns),
        local_verification_seconds=_elapsed_seconds(copy_completed_ns, verification_completed_ns),
        local_recovery_elapsed_seconds=_elapsed_seconds(
            recovery_started_ns, verification_completed_ns
        ),
        clock_basis=_CLOCK_BASIS,
        residuals=_RESIDUALS,
    )
    _write_new_json(restore_root / RECOVERY_OBSERVATION_NAME, observation.to_dict())
    _fsync_directory(restore_root)
    return observation


def _verify_recovery_data(
    data: Path,
    *,
    exact_application_schema: bool,
    immutable_databases: bool,
) -> None:
    for name in _REQUIRED_ARTIFACTS:
        _require_regular_file(data / name)
    _verify_platform_database(data / "platform_ledger.db", immutable=immutable_databases)
    _verify_application_database(
        data / "chronos.db",
        exact_schema=exact_application_schema,
        immutable=immutable_databases,
    )

    kill_state = LiveKillSwitch(data / "live_kill_switch.json").read()
    if not kill_state.engaged:
        raise RecoveryMeasurementError("recovery live kill switch is disengaged")
    if kill_state.engaged_at is None:
        raise RecoveryMeasurementError("recovery kill-switch evidence is corrupt")

    halt_state = HaltStore(data / "platform_halt.json").read()
    if not halt_state.halted:
        raise RecoveryMeasurementError("recovery deterministic platform is rearmed")
    if halt_state.reason is HaltReason.STATE_CORRUPTION:
        raise RecoveryMeasurementError("recovery halt evidence is corrupt")

    audit_path = data / "platform_audit.jsonl"
    if not any(line.strip() for line in _read_regular_file(audit_path).splitlines()):
        raise RecoveryMeasurementError("recovery audit evidence contains no records")
    audit_ok, audit_detail = verify_chain(audit_path)
    if not audit_ok:
        raise RecoveryMeasurementError(f"recovery audit chain failed: {audit_detail}")


def _verify_platform_database(path: Path, *, immutable: bool) -> None:
    with _readonly_sqlite(path, immutable=immutable) as connection:
        _require_integrity(path, connection)
        tables = _user_tables(connection)
        if tables != _PLATFORM_TABLES:
            raise RecoveryMeasurementError(
                f"platform ledger tables differ from the required schema: {sorted(tables)}"
            )
        try:
            row = connection.execute("SELECT version FROM schema_info").fetchone()
        except sqlite3.DatabaseError as error:
            raise RecoveryMeasurementError(
                f"platform ledger schema is unreadable: {error}"
            ) from error
        if row != (_PLATFORM_SCHEMA_VERSION,):
            raise RecoveryMeasurementError(
                f"platform ledger schema version is {row!r}, expected {_PLATFORM_SCHEMA_VERSION}"
            )


def _verify_application_database(path: Path, *, exact_schema: bool, immutable: bool) -> None:
    with _readonly_sqlite(path, immutable=immutable) as connection:
        _require_integrity(path, connection)
        try:
            row = connection.execute(
                "SELECT version FROM schema_version ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise RecoveryMeasurementError(
                f"application database schema is unreadable: {error}"
            ) from error
        if row != (SCHEMA_VERSION,):
            raise RecoveryMeasurementError(
                f"application schema version is {row!r}, expected {SCHEMA_VERSION}"
            )
    if not exact_schema:
        return
    database = Database(f"sqlite:///{path.resolve()}")
    try:
        database.initialize()
    except (RuntimeError, SQLAlchemyError) as error:
        raise RecoveryMeasurementError(
            f"application database schema check failed: {error}"
        ) from error
    finally:
        database.dispose()


def _readonly_sqlite(path: Path, *, immutable: bool) -> closing[sqlite3.Connection]:
    # SQLite's immutable URI mode skips locking and change detection.  It is
    # safe only for the private copies this command just created, never for a
    # live WAL source: https://www.sqlite.org/uri.html#recognized_query_parameters
    parameters = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{path.resolve().as_uri()}?{parameters}"
    try:
        return closing(sqlite3.connect(uri, uri=True))
    except sqlite3.DatabaseError as error:
        raise RecoveryMeasurementError(
            f"cannot open SQLite database {path.name}: {error}"
        ) from error


def _require_integrity(path: Path, connection: sqlite3.Connection) -> None:
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        raise RecoveryMeasurementError(
            f"SQLite integrity check failed for {path.name}: {error}"
        ) from error
    if result != ("ok",):
        raise RecoveryMeasurementError(f"SQLite integrity check failed for {path.name}: {result!r}")


def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise RecoveryMeasurementError(f"SQLite table inventory failed: {error}") from error
    return frozenset(str(row[0]) for row in rows)


def _backup_sqlite(source: Path, destination: Path) -> None:
    """Create a live snapshot using the documented Python 3.12 SQLite API.

    Sources:
    https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup
    https://www.sqlite.org/backup.html
    """

    _require_regular_file(source)
    if os.path.lexists(destination):
        raise RecoveryMeasurementError(f"destination already exists: {destination}")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
            closing(sqlite3.connect(destination)) as destination_connection,
            destination_connection,
        ):
            source_connection.backup(destination_connection)
    except sqlite3.DatabaseError as error:
        raise RecoveryMeasurementError(
            f"SQLite backup failed for {source.name}: {error}"
        ) from error
    os.chmod(destination, 0o600, follow_symlinks=False)
    _fsync_file(destination)


def _copy_new_file(source: Path, destination: Path) -> None:
    _require_regular_file(source)
    source_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as error:
        raise RecoveryMeasurementError(
            f"cannot open source artifact {source.name}: {error}"
        ) from error
    try:
        destination_descriptor = os.open(destination, destination_flags, 0o600)
    except OSError as error:
        os.close(source_descriptor)
        raise RecoveryMeasurementError(
            f"cannot create destination artifact {destination.name}: {error}"
        ) from error
    try:
        with (
            os.fdopen(source_descriptor, "rb", closefd=False) as source_handle,
            os.fdopen(destination_descriptor, "wb", closefd=False) as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_descriptor)
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _verify_manifest_artifacts(root: Path, manifest: SnapshotManifest) -> None:
    expected_names = set(_REQUIRED_ARTIFACTS)
    actual_names = {artifact.name for artifact in manifest.artifacts}
    if actual_names != expected_names or len(manifest.artifacts) != len(expected_names):
        raise RecoveryMeasurementError(
            "snapshot manifest does not name the exact required artifacts"
        )
    for artifact in manifest.artifacts:
        path = root / artifact.name
        _require_regular_file(path)
        if path.stat().st_size != artifact.size_bytes:
            raise RecoveryMeasurementError(f"snapshot size does not match for {artifact.name}")
        if _sha256(path) != artifact.sha256:
            raise RecoveryMeasurementError(f"snapshot digest does not match for {artifact.name}")


def _load_manifest(encoded: bytes) -> SnapshotManifest:
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryMeasurementError(f"snapshot manifest is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise RecoveryMeasurementError("snapshot manifest must be a JSON object")
    expected_keys = {
        "schema_version",
        "source_id",
        "capture_started_at_utc",
        "capture_completed_at_utc",
        "artifact_capture_elapsed_seconds",
        "artifacts",
    }
    if set(payload) != expected_keys:
        raise RecoveryMeasurementError("snapshot manifest fields do not match the v1 schema")
    if payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise RecoveryMeasurementError("snapshot manifest schema version is unsupported")
    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise RecoveryMeasurementError("snapshot manifest artifacts must be a list")
    artifacts = tuple(_load_artifact(item) for item in raw_artifacts)
    manifest = SnapshotManifest(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        source_id=_normalize_source_id(payload["source_id"]),
        capture_started_at_utc=_parse_utc(payload["capture_started_at_utc"], "capture start"),
        capture_completed_at_utc=_parse_utc(
            payload["capture_completed_at_utc"], "capture completion"
        ),
        artifact_capture_elapsed_seconds=_nonnegative_number(
            payload["artifact_capture_elapsed_seconds"], "artifact capture duration"
        ),
        artifacts=artifacts,
    )
    if manifest.capture_completed_at_utc < manifest.capture_started_at_utc:
        raise RecoveryMeasurementError("snapshot capture completion precedes its start")
    for artifact in manifest.artifacts:
        if artifact.capture_started_at_utc < manifest.capture_started_at_utc:
            raise RecoveryMeasurementError("artifact capture precedes the manifest capture start")
        if artifact.capture_completed_at_utc > manifest.capture_completed_at_utc:
            raise RecoveryMeasurementError("artifact capture exceeds the manifest capture window")
        if artifact.capture_completed_at_utc < artifact.capture_started_at_utc:
            raise RecoveryMeasurementError("artifact capture completion precedes its start")
    return manifest


def _load_artifact(value: object) -> ArtifactCapture:
    if not isinstance(value, dict):
        raise RecoveryMeasurementError("snapshot artifact entry must be a JSON object")
    expected_keys = {
        "name",
        "sha256",
        "size_bytes",
        "capture_started_at_utc",
        "capture_completed_at_utc",
    }
    if set(value) != expected_keys:
        raise RecoveryMeasurementError("snapshot artifact fields do not match the v1 schema")
    name = value["name"]
    if not isinstance(name, str) or name not in _REQUIRED_ARTIFACTS:
        raise RecoveryMeasurementError(f"snapshot artifact name is unsupported: {name!r}")
    digest = value["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RecoveryMeasurementError(f"snapshot artifact digest is invalid for {name}")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise RecoveryMeasurementError(f"snapshot artifact size is invalid for {name}")
    return ArtifactCapture(
        name=name,
        sha256=digest,
        size_bytes=size,
        capture_started_at_utc=_parse_utc(value["capture_started_at_utc"], f"{name} start"),
        capture_completed_at_utc=_parse_utc(
            value["capture_completed_at_utc"], f"{name} completion"
        ),
    )


def _normalize_source_id(value: object) -> str:
    if not isinstance(value, str):
        raise RecoveryMeasurementError("source id must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or not normalized.isprintable():
        raise RecoveryMeasurementError("source id must be 1-128 printable characters")
    return normalized


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RecoveryMeasurementError(f"{field} timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RecoveryMeasurementError(f"{field} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryMeasurementError(f"{field} timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _nonnegative_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecoveryMeasurementError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise RecoveryMeasurementError(f"{field} must be finite and non-negative")
    return converted


def _now_utc(clock: RecoveryClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecoveryMeasurementError("recovery clock returned a naive wall-clock timestamp")
    return value.astimezone(UTC)


def _elapsed_seconds(start_ns: int, end_ns: int) -> float:
    if end_ns < start_ns:
        raise RecoveryMeasurementError("recovery monotonic clock moved backwards")
    return (end_ns - start_ns) / 1_000_000_000


def _assert_new_destination(path: Path) -> None:
    if os.path.lexists(path):
        raise RecoveryMeasurementError(f"destination already exists: {path}")


def _assert_disjoint(source: Path, destination: Path) -> None:
    try:
        source_resolved = source.resolve(strict=True)
        destination_resolved = destination.parent.resolve(strict=True) / destination.name
    except OSError as error:
        raise RecoveryMeasurementError(f"cannot resolve recovery paths: {error}") from error
    if destination_resolved == source_resolved or destination_resolved.is_relative_to(
        source_resolved
    ):
        raise RecoveryMeasurementError("destination must not be inside the source tree")
    if source_resolved.is_relative_to(destination_resolved):
        raise RecoveryMeasurementError("source must not be inside the destination tree")


def _make_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as error:
        raise RecoveryMeasurementError(
            f"cannot create recovery directory {path}: {error}"
        ) from error


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RecoveryMeasurementError(
            f"required recovery artifact is missing: {path.name}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RecoveryMeasurementError(f"recovery artifact is not a regular file: {path.name}")


def _read_regular_file(path: Path) -> bytes:
    _require_regular_file(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RecoveryMeasurementError(
            f"cannot open recovery artifact {path.name}: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RecoveryMeasurementError(
            f"cannot hash recovery artifact {path.name}: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RecoveryMeasurementError(
            f"cannot create evidence file {path.name}: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

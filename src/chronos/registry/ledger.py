"""Tamper-evident research registry with a clobber-resistant filesystem boundary.

The JSONL and record hashes remain compatible with :class:`chronos.auditlog.AuditLog`,
but registry I/O is performed relative to verified directory descriptors.  Neither the
ledger, lock, anchor, nor any parent component may be a symlink.  A path capability pins
the nearest existing ancestor and, once created, the registry parent directory, so a
renamed/replaced workspace cannot silently redirect later writes.

The head anchor detects valid-prefix truncation.  It is published through an owner-only
temporary file, fsynced, atomically replaced in the registry directory, and followed by
a directory fsync.  The anchor is not an off-host signature: an actor able to rewrite
both files consistently can still reset history.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from chronos.auditlog.log import AuditLogCorruptionError, AuditRecord

# Record kinds (the ledger's controlled vocabulary).
KIND_RUN = "experiment_run"
KIND_UNLOCK = "holdout_unlock"
KIND_CONSUME = "holdout_consume"

# One process-wide research registry.  The public trial capability freezes this lexical
# path beneath its initial workspace without resolving any symlink component.
CANONICAL_REGISTRY_LEDGER_PATH = Path("research/registry/registry.jsonl")

_GENESIS = "0" * 64
_RECORD_KEYS = frozenset({"sequence", "at_utc", "kind", "payload", "previous_hash", "record_hash"})
_ANCHOR_KEYS = frozenset({"count", "last_hash"})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_ACTIVE_REGISTRY_DIRECTORIES = threading.local()


class RegistryIntegrityError(RuntimeError):
    """The registry path, ledger, or anchor could not be trusted safely."""


def _lexical_absolute(path: Path) -> Path:
    """Make ``path`` absolute without resolving or following symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _hash_record(sequence: int, at_utc: str, kind: str, payload_json: str, prev: str) -> str:
    material = f"{sequence}|{at_utc}|{kind}|{payload_json}|{prev}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _decode_json_without_duplicates(raw: str) -> object:
    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON key {key!r}")
            decoded[key] = value
        return decoded

    def reject_nonfinite(value: str) -> NoReturn:
        raise ValueError(f"non-finite JSON number {value!r}")

    return json.loads(
        raw,
        object_pairs_hook=object_from_pairs,
        parse_constant=reject_nonfinite,
    )


def _decode_record(raw: str) -> dict[str, object]:
    decoded = _decode_json_without_duplicates(raw)
    if not isinstance(decoded, dict):
        raise ValueError("record is not an object")
    if set(decoded) != _RECORD_KEYS:
        raise ValueError(
            "record keys do not match schema; "
            f"missing={sorted(_RECORD_KEYS - set(decoded))}, "
            f"unknown={sorted(set(decoded) - _RECORD_KEYS)}"
        )
    sequence = decoded["sequence"]
    if type(sequence) is not int or sequence < 0:
        raise ValueError("record sequence must be a true integer >= 0")
    for key in ("at_utc", "kind", "previous_hash", "record_hash"):
        if not isinstance(decoded[key], str):
            raise ValueError(f"record {key} must be a string")
    if not isinstance(decoded["payload"], dict):
        raise ValueError("record payload must be an object")
    return decoded


def _decode_anchor(raw: str) -> dict[str, object]:
    decoded = _decode_json_without_duplicates(raw)
    if not isinstance(decoded, dict):
        raise ValueError("head anchor is not an object")
    if set(decoded) != _ANCHOR_KEYS:
        raise ValueError(
            "head anchor keys do not match schema; "
            f"missing={sorted(_ANCHOR_KEYS - set(decoded))}, "
            f"unknown={sorted(set(decoded) - _ANCHOR_KEYS)}"
        )
    count = decoded["count"]
    if type(count) is not int or count < 0:
        raise ValueError("head anchor count must be a true integer >= 0")
    if not isinstance(decoded["last_hash"], str):
        raise ValueError("head anchor last_hash must be a string")
    return decoded


def _require_canonical_json_value(value: object, *, location: str) -> None:
    """Reject values that JSON would coerce, extend, or encode non-portably."""

    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{location} contains a non-finite number")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_canonical_json_value(item, location=f"{location}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{location} contains a non-string object key")
            _require_canonical_json_value(item, location=f"{location}.{key}")
        return
    raise ValueError(f"{location} contains a non-JSON value of type {type(value).__name__}")


def _require_append_inputs(kind: str, payload: dict[str, object]) -> None:
    if type(kind) is not str or not kind:
        raise ValueError("registry record kind must be a non-empty string")
    if type(payload) is not dict:
        raise ValueError("registry record payload must be a dictionary")
    _require_canonical_json_value(payload, location="registry record payload")


def _unsafe_path(path: Path, detail: str, error: BaseException | None = None) -> NoReturn:
    failure = RegistryIntegrityError(f"unsafe registry path {path}: {detail}")
    if error is None:
        raise failure
    raise failure from error


def _open_root() -> int:
    return os.open(os.sep, _DIRECTORY_FLAGS)


def _open_child_directory(parent_fd: int, component: str, display: Path) -> int:
    try:
        return os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        _unsafe_path(display, "parent component is missing, replaced, or a symlink", error)


def _entry_metadata(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_safe_regular_entry(parent_fd: int, name: str, display: Path) -> os.stat_result | None:
    metadata = _entry_metadata(parent_fd, name)
    if metadata is None:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        _unsafe_path(display, "refusing a symlink registry entry")
    if not stat.S_ISREG(metadata.st_mode):
        _unsafe_path(display, "registry entry is not a regular file")
    if metadata.st_uid != os.geteuid():
        _unsafe_path(display, "registry entry is not owned by the current user")
    if metadata.st_nlink != 1:
        _unsafe_path(display, "registry entry has multiple hard links")
    return metadata


class _RegistryPathCapability:
    """Pinned lexical registry location; never resolves a symlink component."""

    __slots__ = (
        "_components",
        "_identity_guard",
        "_parent_identity",
        "_pinned_depth",
        "_pinned_identity",
        "anchor_name",
        "ledger_name",
        "lock_name",
        "parent_path",
        "path",
    )

    def __init__(self, path: Path) -> None:
        self.path = _lexical_absolute(path)
        if self.path.name in {"", ".", ".."}:
            raise ValueError("registry ledger path must name a file")
        self.parent_path = self.path.parent
        self.ledger_name = self.path.name
        self.lock_name = self.path.name + ".lock"
        self.anchor_name = self.path.stem + ".head.json"
        parts = self.parent_path.parts
        if not parts or parts[0] != os.sep:
            raise ValueError("registry ledger path must be absolute after normalization")
        self._components = parts[1:]
        self._identity_guard = threading.Lock()
        self._parent_identity: tuple[int, int] | None = None
        self._pinned_depth, self._pinned_identity = self._inspect_initial_path()

    @property
    def anchor_path(self) -> Path:
        return self.parent_path / self.anchor_name

    def _inspect_initial_path(self) -> tuple[int, tuple[int, int]]:
        descriptor = _open_root()
        depth = 0
        metadata = os.fstat(descriptor)
        try:
            for depth_candidate, component in enumerate(self._components, start=1):
                try:
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    break
                except OSError as error:
                    display = Path(os.sep, *self._components[:depth_candidate])
                    _unsafe_path(display, "parent component is not a real directory", error)
                os.close(descriptor)
                descriptor = child
                depth = depth_candidate
                metadata = os.fstat(descriptor)

            if depth == len(self._components):
                self._require_owned_parent(metadata)
                self._parent_identity = _identity(metadata)
                self._require_all_safe_entries(descriptor)
            return depth, _identity(metadata)
        finally:
            os.close(descriptor)

    def _require_owned_parent(self, metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _unsafe_path(self.parent_path, "registry parent must be an owned real directory")

    def _require_all_safe_entries(self, descriptor: int) -> None:
        for name in (self.ledger_name, self.lock_name, self.anchor_name):
            _require_safe_regular_entry(descriptor, name, self.parent_path / name)

    def _check_pinned_identity(self, depth: int, metadata: os.stat_result) -> None:
        if depth == self._pinned_depth and _identity(metadata) != self._pinned_identity:
            _unsafe_path(self.parent_path, "pinned registry root/ancestor was replaced")

    def open_parent(self, *, create: bool) -> _RegistryDirectory | None:
        """Open and bind the parent through an ``O_NOFOLLOW`` component walk."""

        descriptor = _open_root()
        transferred = False
        try:
            self._check_pinned_identity(0, os.fstat(descriptor))
            for depth, component in enumerate(self._components, start=1):
                publication_needs_fsync = False
                try:
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if depth <= self._pinned_depth:
                        _unsafe_path(self.parent_path, "pinned registry ancestor disappeared")
                    if not create:
                        return None
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        publication_needs_fsync = True
                    except FileExistsError:
                        # A racing creator is safe only after the no-follow open below;
                        # fsync both descriptors ourselves before relying on its entry.
                        publication_needs_fsync = True
                    except OSError as error:
                        display = Path(os.sep, *self._components[:depth])
                        _unsafe_path(display, "could not safely create registry parent", error)
                    child = _open_child_directory(
                        descriptor,
                        component,
                        Path(os.sep, *self._components[:depth]),
                    )
                except OSError as error:
                    display = Path(os.sep, *self._components[:depth])
                    _unsafe_path(display, "parent component is not a real directory", error)
                if publication_needs_fsync:
                    try:
                        os.fchmod(child, 0o700)
                        os.fsync(child)
                        os.fsync(descriptor)
                    except OSError as error:
                        os.close(child)
                        display = Path(os.sep, *self._components[:depth])
                        _unsafe_path(display, "could not durably publish registry parent", error)
                os.close(descriptor)
                descriptor = child
                self._check_pinned_identity(depth, os.fstat(descriptor))

            metadata = os.fstat(descriptor)
            self._require_owned_parent(metadata)
            parent_identity = _identity(metadata)
            with self._identity_guard:
                if self._parent_identity is None:
                    self._parent_identity = parent_identity
                elif self._parent_identity != parent_identity:
                    _unsafe_path(self.parent_path, "registry parent directory was replaced")
            directory = _RegistryDirectory(self, descriptor)
            directory.assert_still_bound()
            directory.assert_all_entries_safe()
            transferred = True
            return directory
        finally:
            if not transferred:
                os.close(descriptor)

    def assert_parent_identity(self, expected: tuple[int, int]) -> None:
        descriptor = _open_root()
        try:
            for depth, component in enumerate(self._components, start=1):
                try:
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    display = Path(os.sep, *self._components[:depth])
                    _unsafe_path(display, "registry parent path was removed or replaced", error)
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            self._require_owned_parent(metadata)
            if _identity(metadata) != expected:
                _unsafe_path(self.parent_path, "registry parent directory was replaced")
        finally:
            os.close(descriptor)


class _RegistryDirectory:
    """Open registry parent used for every leaf operation in one transaction."""

    __slots__ = ("_closed", "capability", "descriptor", "identity")

    def __init__(self, capability: _RegistryPathCapability, descriptor: int) -> None:
        self.capability = capability
        self.descriptor = descriptor
        self.identity = _identity(os.fstat(descriptor))
        self._closed = False

    def __enter__(self) -> _RegistryDirectory:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def assert_still_bound(self) -> None:
        if self._closed:
            raise RegistryIntegrityError("registry directory capability is closed")
        if _identity(os.fstat(self.descriptor)) != self.identity:
            _unsafe_path(self.capability.parent_path, "open registry directory changed identity")
        self.capability.assert_parent_identity(self.identity)

    def assert_all_entries_safe(self) -> None:
        self.assert_still_bound()
        self.capability._require_all_safe_entries(self.descriptor)

    def _open_regular(
        self,
        name: str,
        flags: int,
        *,
        mode: int = 0o600,
        writable: bool,
    ) -> int:
        display = self.capability.parent_path / name
        before = _require_safe_regular_entry(self.descriptor, name, display)
        self.assert_still_bound()
        try:
            descriptor = os.open(name, flags | _FILE_FLAGS, mode, dir_fd=self.descriptor)
        except OSError as error:
            _unsafe_path(display, "refusing unsafe registry file open", error)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
                _unsafe_path(display, "opened registry entry is not an owned regular file")
            if opened.st_nlink != 1:
                _unsafe_path(display, "opened registry entry has multiple hard links")
            if before is not None and _identity(before) != _identity(opened):
                _unsafe_path(display, "registry entry was replaced during open")
            if writable:
                os.fchmod(descriptor, 0o600)
            self.assert_still_bound()
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def read_optional(self, name: str) -> bytes | None:
        display = self.capability.parent_path / name
        if _require_safe_regular_entry(self.descriptor, name, display) is None:
            self.assert_still_bound()
            return None
        descriptor = self._open_regular(name, os.O_RDONLY, writable=False)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            self.assert_still_bound()
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def append_ledger_bytes(self, content: bytes) -> None:
        self.assert_all_entries_safe()
        descriptor = self._open_regular(
            self.capability.ledger_name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            writable=True,
        )
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.assert_still_bound()

    def publish_anchor(self, content: bytes) -> None:
        """Atomically publish a durable anchor without following its destination."""

        self.assert_all_entries_safe()
        temporary = f".{self.capability.anchor_name}.{uuid.uuid4().hex}.tmp"
        descriptor = self._open_regular(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            writable=True,
        )
        published = False
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self.assert_all_entries_safe()
            os.replace(
                temporary,
                self.capability.anchor_name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
            published = True
            os.fsync(self.descriptor)
            self.assert_all_entries_safe()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self.descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("registry write made no progress")
        view = view[written:]


def _thread_lock_for(capability: _RegistryPathCapability) -> threading.Lock:
    key = str(capability.path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _active_directory_stacks() -> dict[str, list[_RegistryDirectory]]:
    stacks = getattr(_ACTIVE_REGISTRY_DIRECTORIES, "stacks", None)
    if stacks is None:
        stacks = {}
        _ACTIVE_REGISTRY_DIRECTORIES.stacks = stacks
    return stacks


def _active_directory_for(path: Path) -> _RegistryDirectory | None:
    stack = _active_directory_stacks().get(str(path), ())
    return stack[-1] if stack else None


@contextmanager
def _publish_active_directory(directory: _RegistryDirectory) -> Iterator[None]:
    key = str(directory.capability.path)
    stacks = _active_directory_stacks()
    stack = stacks.setdefault(key, [])
    stack.append(directory)
    try:
        yield
    finally:
        popped = stack.pop()
        assert popped is directory
        if not stack:
            del stacks[key]


def _coerce_capability(
    source: Path | _RegistryPathCapability,
) -> _RegistryPathCapability:
    if isinstance(source, _RegistryPathCapability):
        return source
    return _RegistryPathCapability(source)


@contextmanager
def registry_lock(
    ledger_path: Path | _RegistryPathCapability,
) -> Iterator[_RegistryDirectory]:
    """Thread + OS lock guarding one descriptor-relative registry transaction.

    Nested calls on the same thread reuse the already-published directory.
    ``threading.Lock`` and a second ``flock`` on a new fd are both
    non-reentrant; without this reuse, ``registered_trial_count`` (outer lock)
    calling ``trial_count`` → ``verified_registry_records`` (inner lock)
    deadlocks the worker thread. That hang cancelled CI at the 10-minute cap.
    """

    capability = _coerce_capability(ledger_path)
    active = _active_directory_for(capability.path)
    if active is not None:
        active.assert_still_bound()
        yield active
        return

    with _thread_lock_for(capability):
        directory = capability.open_parent(create=True)
        assert directory is not None
        with directory:
            # Refuse ledger/anchor symlinks before even creating or touching the lock.
            directory.assert_all_entries_safe()
            lock_descriptor = directory._open_regular(
                capability.lock_name,
                os.O_RDWR | os.O_CREAT,
                writable=True,
            )
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                directory.assert_all_entries_safe()
                try:
                    with _publish_active_directory(directory):
                        yield directory
                finally:
                    directory.assert_still_bound()
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


class RegistryLedger:
    """Append-only hash chain plus independently anchored head."""

    __slots__ = ("_capability", "_last_hash", "_locked_directory", "_records", "_sequence")

    def __init__(self, path: Path) -> None:
        self._locked_directory: _RegistryDirectory | None
        self._records: tuple[AuditRecord, ...] | None
        active = _active_directory_for(_lexical_absolute(path))
        if active is not None:
            self._capability = active.capability
            self._locked_directory = active
            self._records, _ = _require_verified_snapshot(active, active.capability.path)
            self._sequence, self._last_hash = _state_from_records(self._records)
            return
        self._capability = _RegistryPathCapability(path)
        self._locked_directory = None
        self._records = None
        with self._directory(create=False) as directory:
            self._sequence, self._last_hash = _recover(directory, self.path)

    @classmethod
    def _from_locked(
        cls,
        capability: _RegistryPathCapability,
        directory: _RegistryDirectory,
    ) -> RegistryLedger:
        instance = cls.__new__(cls)
        instance._capability = capability
        instance._locked_directory = directory
        instance._records, _ = _require_verified_snapshot(directory, capability.path)
        instance._sequence, instance._last_hash = _state_from_records(instance._records)
        return instance

    @property
    def path(self) -> Path:
        return self._capability.path

    @property
    def anchor_path(self) -> Path:
        return self._capability.anchor_path

    @property
    def _path_capability(self) -> _RegistryPathCapability:
        return self._capability

    @contextmanager
    def _directory(self, *, create: bool) -> Iterator[_RegistryDirectory | None]:
        if self._locked_directory is not None and not self._locked_directory._closed:
            self._locked_directory.assert_still_bound()
            yield self._locked_directory
            return
        self._locked_directory = None
        directory = self._capability.open_parent(create=create)
        if directory is None:
            yield None
            return
        with directory:
            yield directory

    def append(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        """Append through a fresh verified lock, or reuse the caller's active lock."""

        # Validate before acquiring a lock or creating any registry files.  In
        # particular, json.dumps' default NaN/Infinity extension must never reach the
        # durable ledger and turn an input error into a fail-closed corruption DoS.
        _require_append_inputs(kind, payload)
        if self._locked_directory is None or self._locked_directory._closed:
            self._locked_directory = None
            with verified_registry_transaction(self._capability) as fresh:
                record = fresh.append(kind, payload)
            self._sequence = record.sequence + 1
            self._last_hash = record.record_hash
            return record
        return self._append_locked(kind, payload)

    def _append_locked(self, kind: str, payload: dict[str, object]) -> AuditRecord:
        """Raw append path available only to a live locked ledger instance."""

        at = datetime.now(tz=UTC).isoformat()
        _require_append_inputs(kind, payload)
        payload_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        record_hash = _hash_record(
            self._sequence,
            at,
            kind,
            payload_json,
            self._last_hash,
        )
        record = AuditRecord(
            sequence=self._sequence,
            at_utc=at,
            kind=kind,
            payload=payload,
            previous_hash=self._last_hash,
            record_hash=record_hash,
        )
        row: dict[str, object] = {
            "sequence": record.sequence,
            "at_utc": record.at_utc,
            "kind": record.kind,
            "payload": record.payload,
            "previous_hash": record.previous_hash,
            "record_hash": record.record_hash,
        }
        line_text = json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # Keep the writer and verifier on the same exact schema.  This is deliberately
        # before the first filesystem mutation.
        _decode_record(line_text)
        line = line_text.encode("utf-8") + b"\n"
        anchor = (
            json.dumps(
                {"count": record.sequence + 1, "last_hash": record.record_hash},
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with self._directory(create=True) as directory:
            assert directory is not None
            # Check every controlled destination before the ledger can change.
            directory.assert_all_entries_safe()
            directory.append_ledger_bytes(line)
            directory.publish_anchor(anchor)
        self._sequence += 1
        self._last_hash = record_hash
        assert self._records is not None
        self._records += (record,)
        return record

    def records(self) -> tuple[AuditRecord, ...]:
        """Every record in order (empty if the parent/ledger does not yet exist)."""

        if self._locked_directory is not None and not self._locked_directory._closed:
            self._locked_directory.assert_still_bound()
            assert self._records is not None
            return self._records
        with self._directory(create=False) as directory:
            if directory is None:
                return ()
            data = directory.read_optional(self._capability.ledger_name)
        return _parse_records(data, self.path)

    def records_of(self, kind: str) -> tuple[AuditRecord, ...]:
        return tuple(record for record in self.records() if record.kind == kind)

    def verify(self) -> tuple[bool, str]:
        """Verify chain, anchor, and the pinned descriptor-relative path."""

        with self._directory(create=False) as directory:
            if directory is None:
                return True, "empty ledger"
            ledger_data = directory.read_optional(self._capability.ledger_name)
            anchor_data = directory.read_optional(self._capability.anchor_name)
            directory.assert_all_entries_safe()
        ok, detail, _ = _validate_snapshot(ledger_data, anchor_data, self.path)
        return ok, detail


def _recover(
    directory: _RegistryDirectory | None,
    path: Path,
) -> tuple[int, str]:
    if directory is None:
        return 0, _GENESIS
    data = directory.read_optional(directory.capability.ledger_name)
    if data is None:
        return 0, _GENESIS
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuditLogCorruptionError(f"registry ledger is not UTF-8: {path}: {error}") from error
    last_line = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    if not last_line:
        return 0, _GENESIS
    try:
        row = _decode_record(last_line)
        sequence = row["sequence"]
        record_hash = row["record_hash"]
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        assert isinstance(record_hash, str)
        return sequence + 1, record_hash
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
        raise AuditLogCorruptionError(
            f"audit log's last record is unreadable, refusing to append past it: {path}: {error}"
        ) from error


def _parse_records(data: bytes | None, path: Path) -> tuple[AuditRecord, ...]:
    if data is None:
        return ()
    text = data.decode("utf-8")
    out: list[AuditRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = _decode_record(line)
        except (json.JSONDecodeError, ValueError) as error:
            raise AuditLogCorruptionError(
                f"registry record {line_number} is unreadable: {path}: {error}"
            ) from error
        sequence = row["sequence"]
        at_utc = row["at_utc"]
        kind = row["kind"]
        payload = row["payload"]
        previous_hash = row["previous_hash"]
        record_hash = row["record_hash"]
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        assert isinstance(at_utc, str)
        assert isinstance(kind, str)
        assert isinstance(payload, dict)
        assert isinstance(previous_hash, str)
        assert isinstance(record_hash, str)
        out.append(
            AuditRecord(
                sequence=sequence,
                at_utc=at_utc,
                kind=kind,
                payload=payload,
                previous_hash=previous_hash,
                record_hash=record_hash,
            )
        )
    return tuple(out)


def _verify_chain(data: bytes | None, path: Path) -> tuple[bool, str]:
    if data is None:
        return True, "no audit log yet"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return False, f"ledger is not UTF-8: {path}: {error}"
    previous = _GENESIS
    expected_sequence = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = _decode_record(line)
            payload = record["payload"]
            sequence = record["sequence"]
            at_utc = record["at_utc"]
            kind = record["kind"]
            previous_hash = record["previous_hash"]
            record_hash = record["record_hash"]
            assert isinstance(payload, dict)
            assert isinstance(sequence, int) and not isinstance(sequence, bool)
            assert isinstance(at_utc, str)
            assert isinstance(kind, str)
            assert isinstance(previous_hash, str)
            assert isinstance(record_hash, str)
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            recomputed = _hash_record(
                sequence,
                at_utc,
                kind,
                payload_json,
                previous_hash,
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            return False, f"line {line_number}: unreadable record: {error}"
        if sequence != expected_sequence:
            return False, f"line {line_number}: sequence gap"
        if previous_hash != previous:
            return False, f"line {line_number}: chain break"
        if recomputed != record_hash:
            return False, f"line {line_number}: hash mismatch"
        previous = record_hash
        expected_sequence += 1
    return True, f"chain intact ({expected_sequence} records)"


def _validate_snapshot(
    ledger_data: bytes | None,
    anchor_data: bytes | None,
    path: Path,
) -> tuple[bool, str, tuple[AuditRecord, ...]]:
    """Validate and parse one exact ledger/anchor byte snapshot."""

    ok, detail = _verify_chain(ledger_data, path)
    if not ok:
        return False, detail, ()
    try:
        records = _parse_records(ledger_data, path)
    except (AuditLogCorruptionError, UnicodeDecodeError) as error:
        return False, f"registry records unreadable: {error}", ()
    if anchor_data is None:
        if records:
            return (
                False,
                "head anchor missing (possible deletion of registry.head.json)",
                (),
            )
        return True, "empty ledger", records
    try:
        anchor_value = _decode_anchor(anchor_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return False, f"head anchor unreadable: {error}", ()
    expected_count = anchor_value["count"]
    last_hash = anchor_value["last_hash"]
    assert isinstance(expected_count, int) and not isinstance(expected_count, bool)
    assert isinstance(last_hash, str)
    if len(records) != expected_count:
        return (
            False,
            f"tail truncation/rollback: {len(records)} records but anchor expects {expected_count}",
            (),
        )
    if not records:
        return False, "head anchor present but ledger is empty (truncation)", ()
    if records[-1].record_hash != last_hash:
        return False, "head hash mismatch (rollback to an earlier state)", ()
    return True, f"chain + anchor intact ({len(records)} records)", records


def _require_verified_snapshot(
    directory: _RegistryDirectory,
    path: Path,
) -> tuple[tuple[AuditRecord, ...], str]:
    ledger_data = directory.read_optional(directory.capability.ledger_name)
    anchor_data = directory.read_optional(directory.capability.anchor_name)
    directory.assert_all_entries_safe()
    ok, detail, records = _validate_snapshot(ledger_data, anchor_data, path)
    if not ok:
        raise RegistryIntegrityError(f"registry ledger failed verification: {detail}")
    return records, detail


def _state_from_records(records: tuple[AuditRecord, ...]) -> tuple[int, str]:
    if not records:
        return 0, _GENESIS
    return len(records), records[-1].record_hash


@contextmanager
def verified_registry_transaction(
    ledger_path: Path | _RegistryPathCapability,
) -> Iterator[RegistryLedger]:
    """Yield one fresh writer under the complete path/lock/verification boundary."""

    capability = _coerce_capability(ledger_path)
    with registry_lock(capability) as directory:
        ledger = RegistryLedger._from_locked(capability, directory)
        try:
            yield ledger
        finally:
            _require_verified(ledger)


def verified_registry_records(
    ledger_path: Path | _RegistryPathCapability,
) -> tuple[AuditRecord, ...]:
    """Return one verified immutable record snapshot under the registry lock."""

    capability = _coerce_capability(ledger_path)
    with registry_lock(capability) as directory:
        records, _ = _require_verified_snapshot(directory, capability.path)
        return records


def _require_verified(ledger: RegistryLedger) -> None:
    with ledger._directory(create=False) as directory:
        if directory is None:
            records: tuple[AuditRecord, ...] = ()
        else:
            records, _ = _require_verified_snapshot(directory, ledger.path)
    ledger._records = records
    ledger._sequence, ledger._last_hash = _state_from_records(records)

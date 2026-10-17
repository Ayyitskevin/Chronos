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
  live database. The source is never opened for writing. The connection is ONE
  :class:`BoundSource` (N3-2 r1): the source is opened no-follow and held, and the
  connection is proven — by the descriptors this process gained when it connected —
  to have opened that very inode (SQLite resolves even ``/proc/self/fd/<n>`` back to
  a pathname before opening, measured on 3.53); a swap at the name between the two
  opens is a typed refusal before any byte is read. The manifest records that inode
  as ``source_identity`` so a later step can compare what was archived with what it
  holds. A caller may pass its own capability so gate, backup and rename share it.
- The backup is ONE ENVELOPE: a dot-prefixed temp DIRECTORY under ``--out``
  holding the database (written into the O_EXCL temp descriptor via
  ``/proc/self/fd/<n>``, stored in rollback-journal mode), the audit log and
  its head anchor (read ONCE as a pair after the backup completed), and
  ``manifest.json`` — every file fsynced, then the directory — published with
  ONE ``renameat2(RENAME_NOREPLACE)`` to ``<stem>-<stamp>/`` and an fsync of
  ``--out``. A crash before the rename leaves only the dot-temp directory,
  which ``restore()`` refuses by name; a crash after it leaves the whole
  envelope. EEXIST is a typed refusal; any other acquisition error is a typed
  refusal naming the envelope and the errno; either way the temp is removed.
- The directory capability admitted by the no-follow walk is HELD through
  every read: manifest facts (sha256, schema head, row counts) and restore
  verdicts are computed from descendants opened descriptor-relative, and the
  reported paths are bound to it — before returning, the envelope's inode by
  name and the lexical ``--out`` path must still be the directory the walk
  admitted, else a typed refusal that says truthfully what was published.
- The restore goes into a fresh directory the caller names; an existing
  non-empty target is refused. Nothing here ever writes to the live path.
- The schema head is the alembic revision when the store carries an
  ``alembic_version`` table, else the ``schema_version`` row (a store created by
  ``Database.initialize()`` carries no alembic table). The head check on the
  restored copy compares that value AND runs the repository's own acceptance
  (``Database.initialize()`` on an already-versioned store verifies the version
  and zero drift and applies no upgrade). One DISTINCT verdict beside it, never a
  substitute: the N3-2 cutover archive (``chronos.operations.cutover_archive``) backs
  up a store of a RETIRED schema that the running repository refuses by design. Its
  ``--apply`` hands the open :class:`BoundSource` to :func:`run_archive_drill`, which runs
  the cutover's flatten gate ITSELF over that capability (r3, Daybreak P1-2) and keeps that
  run's acceptance — the head it read, that head's numeric version, which must be BELOW the
  running ``SCHEMA_VERSION``, and the gated inode — as a closure inside its own frame (r4):
  the capability carries no admission, no function at module scope takes or carries one
  (``restore``/``verify_restored`` take no capability either; ``run_drill(source=)`` binds
  the backup's identity and yields ``VERIFIED``/``FAILED`` alone; every module-level function
  performs the repository's acceptance only). The drill verifies by that closure: the
  envelope's manifest must record the gated inode (the backup was of the gated object) and
  ``restored head == manifest head == the head the gate read``;
  sha256, row counts and the audit chain verify as always; no repository acceptance runs
  and no T-3 name check either (the copy's bytes are the held descriptor's; head and
  counts come through ``_readonly_fd``'s resolved-pathname open). All good: the verdict
  is ``ARCHIVE_VERIFIED`` with ``acceptance: archive:<head>`` — ``VERIFIED`` is only ever
  the repository's acceptance. A store the gate refuses never reaches a backup there, and
  every public drill path over it ends in the repository's own refusal. A later
  ``restore`` of the envelope under the running code reports every verifier and exactly
  that refusal; the CLI never admits anything.
- The database is encrypted at rest with ``age`` (route A: the age CLI — Muse
  ruling 2026-09-16, delegated by Kevin) to exactly TWO recipients — the host's
  operational public key and Kevin's recovery public key — inside the temp
  envelope, and the cleartext staged copy is removed BEFORE publication: a
  published envelope holds ``chronos.db.age``, never ``chronos.db``. No code
  decides custody: the host identity is the file ``CHRONOS_BACKUP_AGE_IDENTITY``
  names (default ``data/keys/backup-host.age``; regular, 0600, owned by this uid,
  untracked) and the recipients are the two lines of
  ``CHRONOS_BACKUP_AGE_RECIPIENTS`` (default ``data/keys/backup-recipients.txt``),
  one of which must be the host identity's own public key. The manifest's
  ``encryption`` field records the scheme, both recipients and the tool; its
  ``sha256`` is of the CLEARTEXT database and ``ciphertext_sha256`` of the ``.age``
  file. A restore decrypts through the host identity into the isolated target
  and verifies as before. Off-host placement is not done here.
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
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, column, func, select, table
from sqlalchemy.dialects import sqlite as sqlite_dialect

from chronos.auditlog.log import (
    AuditLogCorruptionError,
    ChainState,
    read_audit_pair,
    verify_pair_text,
)
from chronos.persistence.database import SCHEMA_VERSION, Database

if TYPE_CHECKING:  # the module cycle is type-only here; run_archive_drill imports at call time
    from chronos.operations.cutover_archive import GateReport

#: Encryption at rest (R-2): the age CLI, X25519 recipients. ``"none"`` is what a manifest
#: from before R-2 says; the drill no longer produces or restores such an envelope.
AGE_SCHEME = "age-x25519-v1"
ENCRYPTED_DB_SUFFIX = ".age"
IDENTITY_ENV = "CHRONOS_BACKUP_AGE_IDENTITY"
RECIPIENTS_ENV = "CHRONOS_BACKUP_AGE_RECIPIENTS"
DEFAULT_KEY_DIR = Path("data") / "keys"
DEFAULT_IDENTITY = DEFAULT_KEY_DIR / "backup-host.age"
DEFAULT_RECIPIENTS = DEFAULT_KEY_DIR / "backup-recipients.txt"
RECIPIENT_COUNT = 2
#: The decided recovery recipient — Kevin's PUBLIC key as Muse recorded it on 2026-09-16.
#: Pinned in code on purpose (R-2 r1): the recipients file may name it, never choose it —
#: a file that lists the host key plus ANY other key is refused, so an operator (or an
#: attacker with write access to data/keys/) cannot swap the second decrypting party.
#: Rotating this key is a reviewed code change plus a new recipients line, never a file
#: edit alone. Its private half never exists on a fleet host.
KEVIN_RECOVERY_RECIPIENT = "age1ddwtdaexpp2k0dp22fj3rtqfw9y5trr3we77rsmwq9s0y70ase3sgu07kf"
AGE_INSTALL_SENTENCE = (
    "age is not installed: install the distro `age` package or the official release "
    "(https://github.com/FiloSottile/age/releases) so that `age` and `age-keygen` are on PATH "
    "— docs/ops/RESTORE-DRILL.md, section 'Encryption at rest'"
)
#: An age X25519 public key: bech32 ``age1`` + 58 data characters.
_AGE_PUBLIC_KEY_RE = re.compile(r"age1[02-9ac-hj-np-z]{58}")
AUDIT_LOG_NAME = "platform_audit.jsonl"
AUDIT_ANCHOR_NAME = "platform_audit.head.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CHUNK = 1 << 20
MANIFEST_NAME = "manifest.json"
DB_NAME = "chronos.db"
#: The envelope name grammar. Final: ``chronos-<stamp>`` (a fixed prefix — the source's
#: basename never leaks into it, so a dot-prefixed source cannot produce a dot-prefixed
#: envelope). Temp: exactly ``.chronos-<stamp>.<16 hex>.tmp`` — the ONE grammar by which a
#: crash temp is recognised; a leading dot alone means nothing.
TEMP_ENVELOPE_RE = re.compile(r"^\.chronos-\d{8}T\d{6}Z\.[0-9a-f]{16}\.tmp$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
#: renameat2(2) flags: publish only where nothing is (NOREPLACE), or swap two names in one
#: syscall (EXCHANGE — the cutover's move, so a live name is never vacant)
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


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
    encryption: dict[str, object] = field(default_factory=lambda: {"scheme": "none"})
    ciphertext_sha256: str | None = None
    #: ``(device, inode)`` of the source file the backup connection was proven to have opened
    #: (N3-2 r1); ``None`` only in a manifest written before the field existed.
    source_identity: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        if self.source_identity is not None:
            data["source_identity"] = list(self.source_identity)  # as manifest.json writes it
        return data

    @property
    def cleartext_name(self) -> str:
        """The restored database's file name: the envelope's ``chronos.db.age`` → ``chronos.db``."""

        name = Path(self.backup_path).name
        return name.removesuffix(ENCRYPTED_DB_SUFFIX)

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
            encryption=_encryption_block(data.get("encryption")),
            ciphertext_sha256=None
            if data.get("ciphertext_sha256") is None
            else str(data["ciphertext_sha256"]),
            source_identity=_identity_field(data.get("source_identity")),
        )


def _identity_field(raw: object) -> tuple[int, int] | None:
    """``[device, inode]`` as the manifest writes it; absent in a pre-r1 manifest."""

    if raw is None:
        return None
    if isinstance(raw, list | tuple) and len(raw) == 2:
        return int(raw[0]), int(raw[1])
    raise DrillRefused(f"manifest source_identity is not [device, inode]: {raw!r}")


def _encryption_block(raw: object) -> dict[str, object]:
    """A pre-R-2 manifest wrote the string ``"none"``; R-2 writes the block."""

    if raw is None:
        return {"scheme": "none"}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {"scheme": str(raw)}


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
    #: ``repository`` — the running repository accepted the copy (``Database.initialize()``,
    #: version + zero drift); ``archive:<head>`` — the source capability carried the flatten
    #: gate's archive admission (N3-2 cutover archive of a RETIRED schema) and the copy was
    #: verified by exact head and gated source identity instead (see ``restore``).
    acceptance: str = "repository"

    @property
    def verified(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["failures"] = list(self.failures)
        data["verified"] = self.verified
        return data


#: The drill's two verdicts for a verified copy — distinct on purpose (Daybreak P1-2).
VERIFIED = "VERIFIED"
ARCHIVE_VERIFIED = "ARCHIVE_VERIFIED"


@dataclass(frozen=True, slots=True)
class DrillReport:
    manifest: BackupManifest
    restore: RestoreReport | None
    rpo_s: float | None
    rpo_basis: dict[str, object]
    rto_s: float | None
    verdict: str  # VERIFIED | ARCHIVE_VERIFIED | FAILED
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


# ----------------------------------------------------------------- keys (age, two recipients)


@dataclass(frozen=True, slots=True)
class AgeKeys:
    """What a backup or restore needs from the operator's key material, validated ONCE
    before any database is opened — the binaries, the identity file, the two recipients.

    No code decides custody: the identity file and the recipients file are named by the
    environment (defaults under ``data/keys/``), generated and placed by the operator per
    the runbook. Kevin's recovery private key never exists on a fleet host; only his
    PUBLIC key is a recipient line."""

    age: str
    age_keygen: str
    identity_path: Path
    recipients_path: Path
    host_public_key: str
    recipients: tuple[str, ...]
    tool: str

    def encryption_block(self) -> dict[str, object]:
        return {"scheme": AGE_SCHEME, "recipients": list(self.recipients), "tool": self.tool}


def _run_age(argv: list[str], *, what: str, pass_fds: tuple[int, ...] = ()) -> bytes:
    """Run one age binary with an argv list (no shell), stdin closed; a non-zero exit is a
    typed refusal carrying age's stderr; returns stdout."""

    try:
        completed = subprocess.run(  # argv list, no shell; the binary was resolved by which()
            argv,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
    except OSError as error:
        raise DrillRefused(f"{what}: could not run {argv[0]}: {error.strerror}") from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise DrillRefused(f"{what}: age exited {completed.returncode}: {detail}")
    return completed.stdout


def _open_identity(identity_path: Path) -> int:
    """The host identity, opened no-follow and proven a regular file, mode exactly 0600,
    owned by this process's uid; the descriptor is what age reads (``/proc/self/fd/<n>``)."""

    fd = _open_regular(identity_path, "backup host identity")
    try:
        st = os.fstat(fd)
        mode = stat.S_IMODE(st.st_mode)
        if mode != 0o600:
            raise DrillRefused(
                f"backup host identity {identity_path} has mode {mode:04o}; it must be exactly "
                f"0600 (chmod 600 {identity_path}) — a readable identity decrypts every backup"
            )
        if st.st_uid != os.getuid():
            raise DrillRefused(
                f"backup host identity {identity_path} is owned by uid {st.st_uid}, not this "
                f"process's uid {os.getuid()}; the operator's identity must be the operator's file"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def load_age_keys(
    identity: Path | None = None, recipients: Path | None = None, *, environ: Any = None
) -> AgeKeys:
    """Resolve the binaries and validate the operator's key files — every refusal here
    happens BEFORE any source database is opened or any envelope directory is created.

    Order: ``age``/``age-keygen`` on PATH (else the runbook's install sentence); the identity
    file (:func:`_open_identity`) and its public key by ``age-keygen -y`` over the open
    descriptor; the recipients file: exactly ``RECIPIENT_COUNT`` non-blank lines, each an
    ``age1…`` X25519 public key that ``age`` itself accepts as a recipient, and the SET of
    them must EQUAL ``{the host identity's public key, KEVIN_RECOVERY_RECIPIENT}`` — a line
    that is neither is refused by number (R-2 r1: the file names the recipients, it never
    chooses them), and a missing required key is named.
    """

    env = os.environ if environ is None else environ
    age = shutil.which("age")
    age_keygen = shutil.which("age-keygen")
    if age is None or age_keygen is None:
        raise DrillRefused(AGE_INSTALL_SENTENCE)
    version = _run_age([age, "--version"], what="age --version").decode("utf-8", "replace")
    tool = f"age {version.strip()}"
    identity_path = Path(identity or env.get(IDENTITY_ENV) or DEFAULT_IDENTITY)
    recipients_path = Path(recipients or env.get(RECIPIENTS_ENV) or DEFAULT_RECIPIENTS)
    identity_fd = _open_identity(identity_path)
    try:
        derived = _run_age(
            [age_keygen, "-y", f"/proc/self/fd/{identity_fd}"],
            what=f"deriving the public key of {identity_path}",
            pass_fds=(identity_fd,),
        )
    finally:
        os.close(identity_fd)
    host_public_key = derived.decode("utf-8", "replace").strip()
    if not _AGE_PUBLIC_KEY_RE.fullmatch(host_public_key):
        raise DrillRefused(
            f"age-keygen -y over {identity_path} did not yield an age1 public key; the identity "
            "file is not an age X25519 identity"
        )
    recipients_fd = _open_regular(recipients_path, "backup recipients file")
    try:
        with os.fdopen(recipients_fd, "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError as error:
        raise DrillRefused(f"backup recipients file {recipients_path}: {error.strerror}") from None
    lines = [(number, line.strip()) for number, line in enumerate(raw.splitlines(), 1)]
    lines = [(number, text) for number, text in lines if text]
    required = {host_public_key, KEVIN_RECOVERY_RECIPIENT}
    if len(lines) != RECIPIENT_COUNT:
        # r2: the count alone is not a diagnosis — say which required key is absent (by role
        # and the first 12 characters) and which line(s), if any, are neither required key
        present = {text for _number, text in lines}
        missing: list[str] = []
        if host_public_key not in present:
            missing.append(f"the host identity's public key ({host_public_key[:12]}…)")
        if KEVIN_RECOVERY_RECIPIENT not in present:
            missing.append(f"the owner's recovery recipient ({KEVIN_RECOVERY_RECIPIENT[:12]}…)")
        neither = [str(number) for number, text in lines if text not in required]
        raise DrillRefused(
            f"backup recipients file {recipients_path} has {len(lines)} recipient line(s); "
            f"exactly {RECIPIENT_COUNT} are required (the host's operational public key and the "
            "owner's recovery public key — docs/ops/RESTORE-DRILL.md, 'Encryption at rest'); "
            f"missing required key(s): {', '.join(missing) or 'none'}; "
            f"line(s) that are neither required key: {', '.join(neither) or 'none'}"
        )
    for number, text in lines:
        if not _AGE_PUBLIC_KEY_RE.fullmatch(text):
            raise DrillRefused(
                f"backup recipients file {recipients_path} line {number} is not an age X25519 "
                f"public key (age1…): {text!r}"
            )
        # age's own parser (checksum included): encrypt nothing to this recipient alone
        _run_age(
            [age, "-r", text, "-o", "/dev/null"],
            what=f"backup recipients file {recipients_path} line {number} ({text}) rejected by age",
        )
    keys = tuple(text for _number, text in lines)
    if len(set(keys)) != RECIPIENT_COUNT:
        raise DrillRefused(
            f"backup recipients file {recipients_path} lists the same public key twice; the two "
            "recipients must be distinct keys"
        )
    for number, text in lines:
        if text not in required:
            missing = sorted(required - set(keys))
            raise DrillRefused(
                f"backup recipients file {recipients_path} line {number} ({text}) is neither the "
                f"host identity's public key nor the decided recovery recipient "
                f"{KEVIN_RECOVERY_RECIPIENT} (pinned in code; rotating it is a reviewed code "
                f"change); the recipient set must be exactly those two — missing: "
                f"{', '.join(missing)}"
            )
    if host_public_key not in keys:  # unreachable once both lines are required keys; kept explicit
        raise DrillRefused(
            f"backup recipients file {recipients_path} does not contain the host identity's own "
            f"public key {host_public_key}; a backup this host could not restore is refused"
        )
    if KEVIN_RECOVERY_RECIPIENT not in keys:
        raise DrillRefused(
            f"backup recipients file {recipients_path} does not contain the decided recovery "
            f"recipient {KEVIN_RECOVERY_RECIPIENT}; a backup the owner could not recover is refused"
        )
    return AgeKeys(
        age=age,
        age_keygen=age_keygen,
        identity_path=identity_path,
        recipients_path=recipients_path,
        host_public_key=host_public_key,
        recipients=tuple(sorted(keys)),
        tool=tool,
    )


def _require_age_manifest(manifest: BackupManifest) -> None:
    scheme = manifest.encryption.get("scheme")
    if scheme != AGE_SCHEME:
        raise DrillRefused(
            f"manifest records encryption {scheme!r}; this drill restores {AGE_SCHEME} envelopes "
            "only (R-2) — a pre-R-2 cleartext envelope is not a backup this runbook covers"
        )


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
    return _require_regular(fd, subject, str(path))


def _open_regular_at(dfd: int, name: str, subject: str) -> int:
    """Open ``name`` against the directory fd, read-only, no-follow, non-blocking; regular only."""

    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dfd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise DrillRefused(
                f"{subject} {name} is a symlink; a regular file is required"
            ) from None
        if error.errno == errno.ENOENT:
            raise DrillRefused(
                f"{subject} {name} does not exist in the admitted directory"
            ) from None
        raise DrillRefused(f"{subject} {name} could not be opened: {error.strerror}") from None
    return _require_regular(fd, subject, name)


def _require_regular(fd: int, subject: str, name: str) -> int:
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
        raise DrillRefused(f"{subject} {name} is {kind}; a regular file is required")
    return fd


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_of(path: Path, subject: str) -> str:
    fd = _open_regular(path, subject)
    try:
        return _sha256_fd(fd)
    finally:
        os.close(fd)


def _component_problem(dfd: int, component: str, error: OSError) -> str:
    """Name the object at the component: a symlink (ELOOP, or ENOTDIR because O_DIRECTORY
    is judged before O_NOFOLLOW on Linux) is said to be one; the drill never follows it."""

    with contextlib.suppress(OSError):
        if stat.S_ISLNK(os.stat(component, dir_fd=dfd, follow_symlinks=False).st_mode):
            return (
                "is a symlink (refused: the drill never follows a link on its way to a directory)"
            )
    if error.errno == errno.ELOOP:
        return "is a symlink (refused: the drill never follows a link on its way to a directory)"
    if error.errno == errno.ENOTDIR:
        return "is not a directory"
    return error.strerror or str(error)


def _walk_directory(named: Path, what: str, *, create: bool) -> tuple[int, Path]:
    """Reach ``named`` component by component, O_DIRECTORY|O_NOFOLLOW at every step, from
    the root. Missing components are created descriptor-relative (0700) when ``create``;
    a symlinked ancestor or a non-directory component is a typed refusal BEFORE anything
    is created past it. Returns the retained fd and the absolute path (for messages only —
    every later operation is descriptor-relative). The pattern is the watchdog's
    ``_open_evidence_directory`` (W-1), copied rather than imported across branches."""

    absolute = named if named.is_absolute() else Path.cwd() / named
    descriptor = os.open(os.sep, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise DrillRefused(
                        f"{what} {absolute}: component {component!r} does not exist"
                    ) from None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    raise DrillRefused(
                        f"{what} {absolute}: component {component!r} "
                        f"{_component_problem(descriptor, component, error)}"
                    ) from None
            except OSError as error:
                raise DrillRefused(
                    f"{what} {absolute}: component {component!r} "
                    f"{_component_problem(descriptor, component, error)}"
                ) from None
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, absolute


def _open_directory(directory: Path, what: str, *, create: bool) -> int:
    """A directory fd reached by the no-follow walk; every create, rename, unlink, fsync
    and READ below is relative to it."""

    descriptor, _absolute = _walk_directory(directory, what, create=create)
    return descriptor


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


def _dir_identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def _nofollow_identity(path: Path) -> tuple[int, int] | None:
    """The (dev, ino) the absolute ``path`` designates when EVERY component is a real
    directory reached without following a link: from ``/``, each component is lstat'd
    (must be a directory, never a symlink), opened O_DIRECTORY|O_NOFOLLOW, and the opened
    descriptor's identity must equal that lstat. None when any component is missing, a
    symlink, or not a directory — the path is then not trustworthy as a name."""

    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        descriptor = os.open(os.sep, _DIR_FLAGS)
    except OSError:
        return None
    try:
        for component in absolute.parts[1:]:
            try:
                by_name = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(by_name.st_mode):
                    return None
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except OSError:
                return None
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (by_name.st_dev, by_name.st_ino):
                os.close(child)
                return None
            os.close(descriptor)
            descriptor = child
        return _dir_identity(descriptor)
    finally:
        os.close(descriptor)


def _entry_is_real_directory(dfd: int, name: str, identity: tuple[int, int]) -> bool:
    """``name`` under the directory fd is, by lstat, a directory with exactly ``identity``
    (never a symlink to one)."""

    try:
        by_name = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(by_name.st_mode) and (by_name.st_dev, by_name.st_ino) == identity


def _entry_is_real_file(dfd: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        by_name = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(by_name.st_mode) and (by_name.st_dev, by_name.st_ino) == identity


def _renameat2(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
    """renameat2(2) through libc (Linux). Raises OSError with the syscall's errno.
    The watchdog's helper (W-1), copied in-file rather than imported across branches."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.renameat2
    except (OSError, AttributeError) as error:
        raise OSError(errno.ENOSYS, "renameat2 is not available in this libc") from error
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _stage_bytes_at(dfd: int, name: str, payload: bytes) -> None:
    """A complete, fsynced regular file ``name`` under the directory fd."""

    fd = _create_exclusive_at(dfd, name)
    try:
        with os.fdopen(fd, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dfd)
        raise


def _copy_fd_to(src_fd: int, dfd: int, name: str) -> None:
    """Copy an open regular file into the directory fd under ``name`` — O_EXCL, fsynced."""

    dst_fd = _create_exclusive_at(dfd, name)
    try:
        os.lseek(src_fd, 0, os.SEEK_SET)
        with os.fdopen(dst_fd, "wb") as writer:
            while True:
                chunk = os.read(src_fd, _CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dfd)
        raise


def _remove_temp_envelope(out_fd: int, temp_name: str) -> None:
    """Best-effort removal of an unpublished temp envelope (its files, then the directory)."""

    with contextlib.suppress(OSError):
        env_fd = os.open(temp_name, _DIR_FLAGS, dir_fd=out_fd)
        try:
            for entry in os.listdir(env_fd):
                with contextlib.suppress(OSError):
                    os.unlink(entry, dir_fd=env_fd)
        finally:
            os.close(env_fd)
    with contextlib.suppress(OSError):
        os.rmdir(temp_name, dir_fd=out_fd)


def _fresh_directory(target: Path, what: str) -> int:
    """The target must be absent or an empty directory, reached by the no-follow walk (a
    symlinked ancestor or leaf is refused); created 0700 when absent. Returns its fd."""

    absolute = target if target.is_absolute() else Path.cwd() / target
    parent_fd, _parent = _walk_directory(absolute.parent, what, create=True)
    try:
        leaf = absolute.name
        try:
            fd = os.open(leaf, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            fd = os.open(leaf, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise DrillRefused(
                f"{what} {absolute}: component {leaf!r} "
                f"{_component_problem(parent_fd, leaf, error)}"
            ) from None
        try:
            if os.listdir(fd):
                raise DrillRefused(
                    f"{what} {absolute} is not empty; the drill never restores over existing files"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(parent_fd)


# ----------------------------------------------------------------- store inspection


def _descriptor_identities() -> dict[int, tuple[int, int]]:
    """Every open descriptor of this process and the ``(device, inode)`` it refers to."""

    found: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            number = int(name)
            st = os.fstat(number)
        except (ValueError, OSError):
            continue
        found[number] = (st.st_dev, st.st_ino)
    return found


class SourceSwapped(DrillRefused):
    """The name a read-only connection opened no longer designated the object held open."""


class SourceBusy(DrillRefused):
    """Another process holds the file open for writing: the read lease was refused."""


def _lease_read(fd: int, subject: str) -> None:
    """A kernel READ LEASE on an open read-only descriptor (Linux ``fcntl(F_SETLEASE, F_RDLCK)``;
    N3-2 r2, Daybreak P1-1): granted only while NO process has the file open for writing
    (:class:`SourceBusy` otherwise — a writable SQLite connection, idle or not, holds its main
    file O_RDWR); from then on any open for writing, or a truncate, by any process blocks until
    the lease is released or the kernel breaks it (``/proc/sys/fs/lease-break-time``, 45 s by
    default), and :func:`_lease_intact` reports the break. Read-only opens are unaffected. The
    lease's owner is cleared (``F_SETOWN 0``) so a break delivers no SIGIO — whose default action
    would terminate this process — and the holder polls instead. The kernel grants a lease to the
    file's owner (or CAP_LEASE) on a local filesystem; anything else is a typed refusal, never a
    silent fallback. Measured on this host (tmpfs and ext4 alike, SQLite 3.53): our own ``mode=ro``
    connection holds the main file O_RDONLY and does not conflict, but holds the ``-wal`` and
    ``-shm`` O_RDWR, so their leases are possible only after it closes."""

    try:
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
    except OSError as error:
        if error.errno == errno.EAGAIN:
            raise SourceBusy(
                f"{subject} is open for writing by another process (a writable SQLite "
                "connection, idle or not, holds it open read-write): the kernel refused the "
                "read lease, so writers cannot be excluded"
            ) from None
        raise DrillRefused(
            f"the kernel refused a read lease on {subject}: [errno {error.errno}] "
            f"{error.strerror}; writers cannot be excluded here — run as the file's owner, "
            "on a local filesystem"
        ) from None
    fcntl.fcntl(fd, fcntl.F_SETOWN, 0)


def _lease_intact(fd: int) -> bool:
    """True while the read lease taken by :func:`_lease_read` is still whole: no process has
    opened the file for writing since (a pending or completed break reads as F_UNLCK)."""

    return fcntl.fcntl(fd, fcntl.F_GETLEASE) == fcntl.F_RDLCK


class BoundSource:
    """ONE capability on the source for a whole operation (N3-2 r1, Daybreak P1-1).

    ``fd`` is the file opened ``O_RDONLY|O_NOFOLLOW`` and proven regular; ``identity`` is its
    ``(device, inode)``, which cannot be recycled while the descriptor is open. ``connection``
    is a ``mode=ro`` connection under SQLite's normal locking and WAL semantics (never
    ``immutable=1``: "no ``-wal`` right now" is not a fact about the next instant) that was
    PROVEN to have opened that inode: SQLite resolves a pathname — even ``/proc/self/fd/<n>``
    — before it opens (measured, 3.53), so the proof is structural: among the descriptors
    this process gained by connecting and reading once, one refers to ``identity``, else
    :class:`SourceSwapped` and nothing was read. Every later read of the source goes
    through ``connection``; a rename at the end is by name and re-proves ``identity``
    before and after. A read-only reader leaves SQLite's bookkeeping beside a WAL-mode
    store — an empty ``-wal`` and a ``-shm``; a directory entry, never a database write.

    The capability carries NO archive admission (r3, Daybreak P1-2): a retired-schema archive is
    verified only by :func:`run_archive_drill`, which runs the flatten gate itself over the
    capability it is handed and verifies by that run inline in its own body — no closure, no
    verifier object (r5).

    :meth:`exclude_writers` (r2) takes the kernel read lease on ``fd`` (see :func:`_lease_read`):
    from that instant no process can hold or obtain a write-capable open on the store, and
    :attr:`writers_excluded` says whether that has held. The drill's own backups never call it
    (an online backup beside a live backend stays possible); the cutover does, before its gate.
    """

    __slots__ = (
        "_connection_open",
        "_fd_open",
        "_leased",
        "connection",
        "fd",
        "identity",
        "path",
    )

    def __init__(
        self, path: Path, fd: int, identity: tuple[int, int], connection: sqlite3.Connection
    ) -> None:
        self.path = path
        self.fd = fd
        self.identity = identity
        self.connection = connection
        self._connection_open = True
        self._fd_open = True
        self._leased = False

    def exclude_writers(self) -> None:
        """Take the kernel read lease on the held store: refused (:class:`SourceBusy`) while any
        process has it open for writing; afterwards every writer's open blocks and breaks the
        lease, which :attr:`writers_excluded` reports."""

        _lease_read(self.fd, f"source database {self.path}")
        self._leased = True

    @property
    def writers_excluded(self) -> bool:
        """True while the lease from :meth:`exclude_writers` is whole: no process opened the
        store for writing since it was taken."""

        return self._leased and self._fd_open and _lease_intact(self.fd)

    def close_connection(self) -> None:
        if self._connection_open:
            self._connection_open = False
            self.connection.close()

    def close(self) -> None:
        self.close_connection()
        if self._fd_open:
            self._fd_open = False
            os.close(self.fd)


def open_bound_source(path: Path, subject: str = "source database") -> BoundSource:
    """Open ``path`` no-follow (regular file required), hold it, connect read-only by name,
    and prove the connection opened the held inode (see :class:`BoundSource`)."""

    path = Path(path)
    fd = _open_regular(path, subject)
    try:
        st = os.fstat(fd)
        identity = (st.st_dev, st.st_ino)
        absolute = path if path.is_absolute() else Path.cwd() / path
        before = _descriptor_identities()
        connection = sqlite3.connect(f"{absolute.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            gained = {
                number: found
                for number, found in _descriptor_identities().items()
                if number not in before
            }
            if identity not in gained.values():
                others = sorted({f"{dev}:{ino}" for dev, ino in gained.values()})
                raise SourceSwapped(
                    f"{subject} {path} swapped: the read-only connection opened "
                    f"{', '.join(others) or 'no file'} (device:inode), not the object held open "
                    f"no-follow ({identity[0]}:{identity[1]}) — the name changed between the two "
                    "opens; nothing was read"
                )
        except BaseException:
            connection.close()
            raise
    except BaseException:
        os.close(fd)
        raise
    return BoundSource(path, fd, identity, connection)


def _readonly_fd(fd: int) -> contextlib.closing[sqlite3.Connection]:
    """A read-only connection on an already-open descriptor of a RETAINED copy. SQLite resolves
    ``/proc/self/fd/<n>`` to the pathname before opening (measured, 3.53), so this binds by
    name at that instant; the callers re-prove the copy's identity through ``fd`` before and
    after (T-3). Closed on exit: a ``with`` on a Connection is a transaction scope, not a
    closer, and an unclosed connection lingers until the garbage collector runs."""

    return contextlib.closing(sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro", uri=True))


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


def _row_counts_on(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(connection.execute(_count_statement(name)).fetchone()[0])
        for name in _user_tables(connection)
    }


def _schema_head_on(connection: sqlite3.Connection) -> str:
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


def row_counts(path: Path) -> dict[str, int]:
    """Every user table's row count, read-only (the file opened no-follow, read by fd)."""

    fd = _open_regular(path, "store")
    try:
        with _readonly_fd(fd) as connection:
            return _row_counts_on(connection)
    finally:
        os.close(fd)


def schema_head(path: Path) -> str:
    """The alembic revision when the store carries one, else ``schema_version:<n>``."""

    fd = _open_regular(path, "store")
    try:
        with _readonly_fd(fd) as connection:
            return _schema_head_on(connection)
    finally:
        os.close(fd)


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


def newest_evidence_on(
    connection: sqlite3.Connection, audit_text: str | None
) -> tuple[datetime | None, dict[str, object]]:
    """The newest committed evidence timestamp in an open store (plus the audit log's
    text, already read through a descriptor) and which basis produced it."""

    newest: datetime | None = None
    candidates = 0
    basis: dict[str, object] = {"source": None, "newest_evidence_at": None, "candidates": 0}
    if True:
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
    if audit_text:
        last = audit_text.rstrip("\n").rsplit("\n", 1)[-1]
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


def _envelope_names(stamp: str) -> tuple[str, str, str]:
    """(final envelope dir name, temp envelope dir name, database file name) — the fixed
    grammar above; the source's basename never appears in any of them."""

    final = f"chronos-{stamp}"
    temp = f".{final}.{secrets.token_hex(8)}.tmp"
    assert TEMP_ENVELOPE_RE.fullmatch(temp), temp
    return final, temp, DB_NAME


def _read_text_at(dfd: int, name: str, subject: str) -> str:
    fd = _open_regular_at(dfd, name, subject)
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read().decode("utf-8")
    finally:
        pass


def backup(
    db_path: Path,
    out_dir: Path,
    *,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
    source: BoundSource | None = None,
) -> BackupManifest:
    """A consistent online backup of ``db_path`` as ONE envelope under ``out_dir``.

    The source is read through ONE :class:`BoundSource` — the caller's (``source``, so a
    gate, this backup and a later rename share one capability) or this call's own, opened
    before any directory is created and closed before returning — read-only (``mode=ro``,
    normal SQLite locking), proven to be the held inode, never written; the manifest records
    that inode as ``source_identity``. The envelope is staged as a dot-temp directory
    (database through the O_EXCL temp descriptor, audit pair from one ``read_audit_pair``
    after the backup completed, ``manifest.json``), every file and the directory fsynced, then
    published with ONE ``renameat2(RENAME_NOREPLACE)`` — EEXIST or any other error
    is a typed refusal and the temp is removed — then ``out_dir`` is fsynced. The
    directory capability admitted by the walk is held through every manifest read
    and bound to the returned paths before returning.
    """

    clock = clock or system_clock()
    keys = keys or load_age_keys()  # every key refusal precedes the source open
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    owned = source is None
    source = source or open_bound_source(db_path, "source database")
    try:
        return _backup_bound(source, out_dir, clock=clock, keys=keys)
    finally:
        if owned:
            source.close()


def _backup_bound(
    source: BoundSource, out_dir: Path, *, clock: Clock, keys: AgeKeys
) -> BackupManifest:
    out_fd, out_abs = _walk_directory(out_dir, "backup directory", create=True)
    try:
        taken_at = clock.wall()
        stamp = taken_at.strftime("%Y%m%dT%H%M%SZ")
        final_name, temp_name, db_name = _envelope_names(stamp)
        os.mkdir(temp_name, 0o700, dir_fd=out_fd)
        env_fd = os.open(temp_name, _DIR_FLAGS, dir_fd=out_fd)
        try:
            manifest = _stage_envelope(
                env_fd, source, db_name, out_abs / final_name, taken_at, clock
            )
            manifest = _encrypt_staged(env_fd, db_name, manifest, keys)
            _stage_bytes_at(
                env_fd,
                MANIFEST_NAME,
                (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(env_fd)
            try:
                _renameat2(out_fd, temp_name, out_fd, final_name, _RENAME_NOREPLACE)
            except FileExistsError:
                raise DrillRefused(
                    f"{final_name} already exists in the destination; the drill never overwrites"
                ) from None
            except OSError as error:
                raise DrillRefused(
                    f"publication of {final_name} failed: [errno {error.errno}] {error.strerror}; "
                    "the temp envelope was removed, nothing is published"
                ) from None
            os.fsync(out_fd)
            # bind the reported paths to the capability that received the writes — every
            # check no-follow: the final name must BE the envelope directory by lstat (a
            # symlink to the same inode is refused), and every component of the reported
            # absolute path must be a real directory reached without following a link
            admitted = _dir_identity(out_fd)
            envelope = _dir_identity(env_fd)
            if not _entry_is_real_directory(out_fd, final_name, envelope):
                raise DrillRefused(
                    f"published: the envelope was renamed into the directory the walk admitted "
                    f"(device:inode {admitted[0]}:{admitted[1]}), but the name {final_name} there "
                    f"no longer IS that directory by lstat — it is a symlink or a replaced entry; "
                    f"the backup this drill wrote exists, the reported path {out_abs / final_name} "
                    "is not trustworthy, and this manifest is not returned"
                )
            if (
                _nofollow_identity(out_abs) != admitted
                or _nofollow_identity(out_abs / final_name) != envelope
            ):
                raise DrillRefused(
                    f"published: the envelope {final_name} exists in the directory the walk "
                    f"admitted (device:inode {admitted[0]}:{admitted[1]}), "
                    f"but the path {out_abs} no longer names that directory without following "
                    f"a symlink — an ancestor was swapped after publication (renamed away, or "
                    f"replaced by a link); "
                    "the triple is published there, not at the path, and this manifest is not "
                    "returned"
                )
        except BaseException:
            os.close(env_fd)
            _remove_temp_envelope(out_fd, temp_name)  # a no-op once the envelope is published
            raise
        os.close(env_fd)
    finally:
        os.close(out_fd)
    return manifest


def _stage_envelope(
    env_fd: int,
    source: BoundSource,
    db_name: str,
    final_dir: Path,
    taken_at: datetime,
    clock: Clock,
) -> BackupManifest:
    """Fill the temp envelope: the database (through the bound connection — no reopen), the
    audit pair (one read, after the backup completed), and the manifest facts read back
    through the descriptors."""

    tmp_fd = _create_exclusive_at(env_fd, db_name, flags=os.O_RDWR)
    try:
        identity = os.fstat(tmp_fd)
        # sqlite writes THIS descriptor's inode (/proc/self/fd/<n>), never a re-resolved
        # pathname; journal OFF so no sibling journal is needed there
        destination = sqlite3.connect(f"/proc/self/fd/{tmp_fd}")
        try:
            destination.execute("PRAGMA journal_mode=OFF")
            source.connection.backup(destination)
            snapshot_completed_at = clock.wall()
            # one self-contained file: readers of the retained copy leave no sidecars
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
        after = os.stat(db_name, dir_fd=env_fd)
        if (after.st_dev, after.st_ino) != (identity.st_dev, identity.st_ino):
            raise DrillRefused(f"the temp entry {db_name} changed identity during the backup")
        os.fsync(tmp_fd)
        # the audit pair, read ONCE as a pair AFTER the backup completed (anchor before
        # log, no-follow, one capability read each — the audit log's own reader)
        audit_log = source.path.parent / AUDIT_LOG_NAME
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
        audit_head: str | None = None
        audit_log_path: str | None = None
        if log_text is not None and anchor_bytes is not None:
            audit_head = _decode_audit_head(anchor_bytes, audit_log.with_name(AUDIT_ANCHOR_NAME))
            _stage_bytes_at(env_fd, AUDIT_LOG_NAME, log_text.encode("utf-8"))
            _stage_bytes_at(env_fd, AUDIT_ANCHOR_NAME, anchor_bytes)
            audit_log_path = str(final_dir / AUDIT_LOG_NAME)
        # manifest facts, read back through the descriptor that was written
        with _readonly_fd(tmp_fd) as retained:
            head = _schema_head_on(retained)
            counts = _row_counts_on(retained)
        sha256 = _sha256_fd(tmp_fd)
    finally:
        os.close(tmp_fd)
    return BackupManifest(
        taken_at=taken_at.isoformat(),
        snapshot_completed_at=snapshot_completed_at.isoformat(),
        source_path=str(source.path),
        backup_path=str(final_dir / db_name),
        sha256=sha256,
        schema_head=head,
        row_counts=counts,
        audit_head=audit_head,
        audit_log_path=audit_log_path,
        source_identity=source.identity,
    )


def _encrypt_staged(
    env_fd: int, db_name: str, manifest: BackupManifest, keys: AgeKeys
) -> BackupManifest:
    """Encrypt the staged cleartext database inside the temp envelope to the two validated
    recipients (``age -r <key> -r <key>``: the keys this run checked, not a re-read file),
    fsync the ciphertext, then remove the cleartext — BEFORE the envelope is published. The
    child reads and writes through the envelope descriptor (``/proc/self/fd/<n>/…``)."""

    encrypted_name = db_name + ENCRYPTED_DB_SUFFIX
    argv = [keys.age]
    for recipient in keys.recipients:
        argv += ["-r", recipient]
    argv += ["-o", f"/proc/self/fd/{env_fd}/{encrypted_name}", f"/proc/self/fd/{env_fd}/{db_name}"]
    _run_age(argv, what=f"encryption of {db_name}", pass_fds=(env_fd,))
    cipher_fd = _open_regular_at(env_fd, encrypted_name, "encrypted backup")
    try:
        os.fsync(cipher_fd)
        ciphertext_sha256 = _sha256_fd(cipher_fd)
    finally:
        os.close(cipher_fd)
    os.unlink(db_name, dir_fd=env_fd)  # the cleartext never reaches the published name
    os.fsync(env_fd)
    return replace(
        manifest,
        backup_path=str(Path(manifest.backup_path).with_name(encrypted_name)),
        encryption=keys.encryption_block(),
        ciphertext_sha256=ciphertext_sha256,
    )


def _open_envelope(manifest: BackupManifest) -> tuple[int, Path]:
    """The published envelope directory, reached by the no-follow walk; a dot-temp
    (unpublished) envelope is refused BY NAME, whatever it contains."""

    env_dir = Path(manifest.backup_path).parent
    if TEMP_ENVELOPE_RE.fullmatch(env_dir.name):
        raise DrillRefused(
            f"{env_dir} is an unpublished temp envelope (a crash before its rename); "
            "the drill never restores from one"
        )
    return _walk_directory(env_dir, "backup envelope", create=False)


# ----------------------------------------------------------------- restore + verify


def _open_copy(
    manifest: BackupManifest, target_fd: int
) -> tuple[int, list[str], dict[str, bool | str], str, dict[str, int]]:
    """Open the restored copy by name against the target directory descriptor and read what
    every verification needs: its sha256 through that descriptor, its head and row counts
    through ``_readonly_fd`` (a resolved-pathname open; see that function). Returns the OPEN
    descriptor — an acceptance step may bind to it; the caller closes it — with the failures
    and facts so far, the head and the counts. No acceptance happens here."""

    failures: list[str] = []
    facts: dict[str, bool | str] = {}
    fd = _open_regular_at(target_fd, manifest.cleartext_name, "restored copy")
    try:
        actual = _sha256_fd(fd)
        facts["sha256_ok"] = actual == manifest.sha256
        if not facts["sha256_ok"]:
            failures.append(
                f"sha256: restored copy {actual[:12]}… != manifest {manifest.sha256[:12]}…"
            )
        with _readonly_fd(fd) as restored:
            head = _schema_head_on(restored)
            counts = _row_counts_on(restored)
    except BaseException:
        os.close(fd)
        raise
    return fd, failures, facts, head, counts


def _repository_acceptance(
    manifest: BackupManifest, target_fd: int, target_abs: Path, fd: int
) -> list[str]:
    """The running repository's own acceptance of the copy — ``Database.initialize()`` on an
    already-versioned store verifies the version and zero drift and applies no upgrade — a
    pathname open bound by identity to the held descriptor before and after it (T-3: the
    descriptor stays open through the check, so the inode cannot be freed and its number
    handed to a stranger planted at the name meanwhile). The failures, or an empty list. This
    is the ONLY acceptance any module-level function performs (r4, Daybreak r3 P1-2): a retired
    schema is refused here by design, and nothing at module scope can say otherwise — the N3-2
    archive's acceptance lives inside :func:`run_archive_drill`'s frame alone."""

    db_name = manifest.cleartext_name
    lexical = target_abs / db_name

    def copy_identity() -> tuple[int, int]:
        st = os.fstat(fd)
        return st.st_dev, st.st_ino

    if not _entry_is_real_file(target_fd, db_name, copy_identity()) or _nofollow_identity(
        target_abs
    ) != _dir_identity(target_fd):
        return [
            f"restore target swapped: {target_abs} no longer names the directory the walk "
            "admitted, so the repository's acceptance check cannot be bound to the copy"
        ]
    failures: list[str] = []
    try:
        database = Database(f"sqlite:///{lexical}")
        try:
            database.initialize()  # verifies version + zero drift; applies no upgrade
        finally:
            database.dispose()
    except RuntimeError as error:
        failures.append(f"schema_acceptance: {error}")
    if not _entry_is_real_file(target_fd, db_name, copy_identity()):
        failures.append(
            f"restore target swapped: {lexical} changed identity during the acceptance check"
        )
    return failures


def _finish_verification(
    manifest: BackupManifest,
    target_fd: int,
    target_abs: Path,
    has_audit: bool,
    head: str,
    counts: dict[str, int],
    failures: list[str],
    facts: dict[str, bool | str],
) -> None:
    """The verifiers after an acceptance step — the head against the manifest, the row counts,
    the audit chain — appended to the caller's ``failures`` and ``facts``. ``schema_head_ok`` is
    the head comparison alone here; the caller ANDs its own acceptance verdict into it. No
    acceptance happens here."""

    facts["schema_head_ok"] = head == manifest.schema_head
    if head != manifest.schema_head:
        failures.append(f"schema_head: restored {head} != manifest {manifest.schema_head}")

    facts["row_counts_ok"] = counts == manifest.row_counts
    if not facts["row_counts_ok"]:
        differing = sorted(
            t
            for t in set(counts) | set(manifest.row_counts)
            if counts.get(t) != manifest.row_counts.get(t)
        )
        failures.append(f"row_counts: differ in {', '.join(differing)}")

    if manifest.audit_head is None:
        facts["audit_chain"] = "NOT_APPLICABLE"
    elif not has_audit:
        facts["audit_chain"] = "ABSENT"
        failures.append(
            "audit_chain: the manifest records an audit head but no audit log was restored"
        )
    else:
        try:
            log_text = _read_text_at(target_fd, AUDIT_LOG_NAME, "restored audit log")
            anchor_fd = _open_regular_at(target_fd, AUDIT_ANCHOR_NAME, "restored audit anchor")
            try:
                with os.fdopen(anchor_fd, "rb") as handle:
                    anchor_bytes = handle.read()
            finally:
                pass
        except DrillRefused as error:
            facts["audit_chain"] = "ABSENT"
            failures.append(f"audit_chain: {error}")
        else:
            verdict = verify_pair_text(log_text, anchor_bytes)
            facts["audit_chain"] = verdict.state.value
            if verdict.state is not ChainState.VALID:
                failures.append(f"audit_chain: {verdict.state.value} — {verdict.detail}")
            elif (
                _decode_audit_head(anchor_bytes, target_abs / AUDIT_ANCHOR_NAME)
                != manifest.audit_head
            ):
                failures.append(
                    "audit_chain: the restored anchor's last hash differs from the manifest's "
                    "audit_head"
                )


def _verify_in(
    manifest: BackupManifest, target_fd: int, target_abs: Path, has_audit: bool
) -> tuple[list[str], dict[str, bool | str]]:
    """The four verifiers with the REPOSITORY's acceptance, every read through the target
    directory descriptor. This function knows no other acceptance, and neither does any other
    function at module scope (r4, Daybreak r3 P1-2)."""

    fd, failures, facts, head, counts = _open_copy(manifest, target_fd)
    try:
        problems = _repository_acceptance(manifest, target_fd, target_abs, fd)
    finally:
        os.close(fd)  # after the last comparison: no descriptor to the copy outlives restore()
    failures.extend(problems)
    facts["acceptance"] = "repository"
    _finish_verification(manifest, target_fd, target_abs, has_audit, head, counts, failures, facts)
    facts["schema_head_ok"] = bool(facts["schema_head_ok"]) and not problems
    return failures, facts


def verify_restored(
    manifest: BackupManifest,
    restored: Path,
    restored_audit_log: Path | None,
) -> tuple[list[str], dict[str, bool | str]]:
    """The four verifiers against a restored copy named by path (the directory is reached
    by the no-follow walk and every read goes through it), with the repository's acceptance:
    a retired-schema copy is refused here by design (see :func:`run_archive_drill`)."""

    restored = Path(restored)
    target_fd, target_abs = _walk_directory(restored.parent, "restore target", create=False)
    try:
        return _verify_in(manifest, target_fd, target_abs, restored_audit_log is not None)
    finally:
        os.close(target_fd)


def _decrypt_into(
    env_fd: int, encrypted_name: str, target_fd: int, db_name: str, keys: AgeKeys
) -> None:
    """``age -d -i <identity> -o <target>/chronos.db <envelope>/chronos.db.age`` through the
    two directory descriptors and the identity's descriptor (re-proved 0600, this uid). A
    non-zero exit is a typed refusal and any partial output at the target is removed first."""

    identity_fd = _open_identity(keys.identity_path)
    try:
        argv = [
            keys.age,
            "-d",
            "-i",
            f"/proc/self/fd/{identity_fd}",
            "-o",
            f"/proc/self/fd/{target_fd}/{db_name}",
            f"/proc/self/fd/{env_fd}/{encrypted_name}",
        ]
        try:
            _run_age(
                argv,
                what=f"decryption of {encrypted_name}",
                pass_fds=(identity_fd, target_fd, env_fd),
            )
        except DrillRefused:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(db_name, dir_fd=target_fd)  # nothing partial stays in the target
            os.fsync(target_fd)
            raise
    finally:
        os.close(identity_fd)


@dataclass(frozen=True, slots=True)
class _Staged:
    """A restore target after the decrypt and the audit copy, held open for the caller's
    verification (none happened here); ``decrypt_refused`` when the decrypt was refused."""

    target_fd: int
    target_abs: Path
    has_audit: bool
    decrypt_refused: DrillRefused | None


@contextlib.contextmanager
def _staged_restore(manifest: BackupManifest, target_dir: Path, keys: AgeKeys) -> Iterator[_Staged]:
    """Decrypt the envelope's database through the host identity into the FRESH target
    directory and copy the audit pair beside it, every write through the held descriptors;
    yield the held target for the caller's verification — repository or, inside
    :func:`run_archive_drill` alone, that gate run's — and close everything after. A decrypt
    refusal is yielded, not raised (the caller's report records it)."""

    _require_age_manifest(manifest)
    target_dir = Path(target_dir)
    target_abs = target_dir if target_dir.is_absolute() else Path.cwd() / target_dir
    env_fd, _env_abs = _open_envelope(manifest)
    try:
        target_fd = _fresh_directory(target_dir, "restore target")
        try:
            db_name = manifest.cleartext_name
            encrypted_name = Path(manifest.backup_path).name
            try:
                _decrypt_into(env_fd, encrypted_name, target_fd, db_name, keys)
            except DrillRefused as error:
                yield _Staged(target_fd, target_abs, False, error)
                return
            has_audit = manifest.audit_log_path is not None
            if has_audit:
                for name in (AUDIT_LOG_NAME, AUDIT_ANCHOR_NAME):
                    src = _open_regular_at(env_fd, name, "retained audit file")
                    try:
                        _copy_fd_to(src, target_fd, name)
                    finally:
                        os.close(src)
            os.fsync(target_fd)
            yield _Staged(target_fd, target_abs, has_audit, None)
        finally:
            os.close(target_fd)
    finally:
        os.close(env_fd)


def _target_swapped_after(staged: _Staged) -> list[str]:
    """After verification: the reported path must still name the directory the walk admitted."""

    if _nofollow_identity(staged.target_abs) != _dir_identity(staged.target_fd):
        return [
            f"restore target swapped: {staged.target_abs} no longer names the directory the "
            "walk admitted; the copy this drill made is not at the reported path"
        ]
    return []


def restore(
    manifest: BackupManifest,
    target_dir: Path,
    *,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
) -> RestoreReport:
    """Decrypt the envelope's database through the host identity into a fresh directory,
    copy the audit pair beside it, and verify them there — the target descriptor is held
    through the decrypt, the copy AND the verification; rto_s from start to verified. A
    decrypt failure is a typed refusal recorded in the report (``decrypt: refused — …``).

    The acceptance is the repository's — here and in every function at module scope (r4,
    Daybreak r3 P1-2): a RETIRED-schema copy, a schema the running repository refuses by
    design, verifies as refused however it was made. The N3-2 cutover verifies its archive
    through :func:`run_archive_drill` alone, whose acceptance is a closure in its own frame.
    Restoring a retired copy for use needs the code that last ran that schema; nothing here
    makes it acceptable to the running code."""

    clock = clock or system_clock()
    keys = keys or load_age_keys()
    started = clock.monotonic()
    with _staged_restore(manifest, target_dir, keys) as staged:
        if staged.decrypt_refused is not None:
            return RestoreReport(
                target_dir=str(staged.target_abs),
                restored_path=str(staged.target_abs / manifest.cleartext_name),
                sha256_ok=False,
                schema_head_ok=False,
                row_counts_ok=False,
                audit_chain="NOT_APPLICABLE" if manifest.audit_head is None else "ABSENT",
                failures=(f"decrypt: refused — {staged.decrypt_refused}",),
                rto_s=clock.monotonic() - started,
                acceptance="repository",
            )
        failures, facts = _verify_in(
            manifest, staged.target_fd, staged.target_abs, staged.has_audit
        )
        failures.extend(_target_swapped_after(staged))
    return RestoreReport(
        target_dir=str(staged.target_abs),
        restored_path=str(staged.target_abs / manifest.cleartext_name),
        sha256_ok=bool(facts["sha256_ok"]),
        schema_head_ok=bool(facts["schema_head_ok"]),
        row_counts_ok=bool(facts["row_counts_ok"]),
        audit_chain=str(facts["audit_chain"]),
        failures=tuple(failures),
        rto_s=clock.monotonic() - started,
        acceptance="repository",
    )


# ----------------------------------------------------------------- the drill


def rpo_seconds(
    manifest: BackupManifest, *, keys: AgeKeys | None = None
) -> tuple[float | None, dict[str, object]]:
    """``snapshot_completed_at`` minus the newest committed evidence IN THE RETAINED
    ENVELOPE — never the live source. The retained database is ciphertext, so it is
    decrypted through the host identity into a private 0700 scratch directory (a
    descriptor-bound ``/proc/self/fd`` write, removed before returning), its sha256 must be
    the manifest's, and the evidence is read through that descriptor; the audit evidence is
    read from the retained audit copy through the envelope's descriptor, as before. None +
    reason when the snapshot carries no timestamped evidence; evidence dated after the
    snapshot completed is a typed refusal, never a negative number."""

    keys = keys or load_age_keys()
    _require_age_manifest(manifest)
    env_fd, _env_abs = _open_envelope(manifest)
    try:
        audit_text = (
            _read_text_at(env_fd, AUDIT_LOG_NAME, "retained audit log")
            if manifest.audit_log_path is not None
            else None
        )
        db_name = manifest.cleartext_name
        scratch = Path(tempfile.mkdtemp(prefix=".chronos-rpo-"))  # mode 0700
        try:
            scratch_fd = os.open(scratch, _DIR_FLAGS)
            try:
                _decrypt_into(env_fd, Path(manifest.backup_path).name, scratch_fd, db_name, keys)
                db_fd = _open_regular_at(scratch_fd, db_name, "decrypted retained backup")
                try:
                    digest = _sha256_fd(db_fd)
                    if digest != manifest.sha256:
                        raise DrillRefused(
                            f"the decrypted retained backup ({digest[:12]}…) is not the manifest's "
                            f"cleartext (sha256 {manifest.sha256[:12]}…); rpo_s is measured from "
                            "the retained bytes only"
                        )
                    with _readonly_fd(db_fd) as retained:
                        newest, basis = newest_evidence_on(retained, audit_text)
                finally:
                    os.close(db_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(db_name, dir_fd=scratch_fd)
                os.close(scratch_fd)
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(scratch)
    finally:
        os.close(env_fd)
    basis["evidence_read_from"] = "retained snapshot (decrypted, sha256 == manifest)"
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
    db_path: Path,
    out_dir: Path,
    restore_into: Path,
    *,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
    source: BoundSource | None = None,
) -> DrillReport:
    """Backup → rpo → restore + verify, one report. ``source`` is the caller's bound
    capability (the backup then reads through it and its manifest records that inode; left to
    the backup otherwise). The acceptance is the repository's and a clean result is
    ``VERIFIED`` — never ``ARCHIVE_VERIFIED``, which only :func:`run_archive_drill` yields. The
    CLI never passes a capability."""

    clock = clock or system_clock()
    keys = keys or load_age_keys()
    manifest = backup(db_path, out_dir, clock=clock, keys=keys, source=source)
    failures, rpo, basis = _rpo_or_refusal(manifest, keys)
    report = restore(manifest, restore_into, clock=clock, keys=keys)
    failures.extend(report.failures)
    return DrillReport(
        manifest=manifest,
        restore=report,
        rpo_s=rpo,
        rpo_basis=basis,
        rto_s=report.rto_s,
        verdict=VERIFIED if not failures else "FAILED",
        failures=tuple(failures),
    )


def _rpo_or_refusal(
    manifest: BackupManifest, keys: AgeKeys
) -> tuple[list[str], float | None, dict[str, object]]:
    try:
        rpo, basis = rpo_seconds(manifest, keys=keys)
    except DrillRefused as error:
        return [f"rpo: refused — {error}"], None, {"source": None, "refused": str(error)}
    return [], rpo, basis


def _require_retired(schema_head: str, schema_version: int) -> None:
    """A flatten-gate run's head must name a RETIRED schema: ``schema_version`` BELOW the
    running ``SCHEMA_VERSION`` (a current-or-newer store is never an archive), ``schema_head``
    in that version's form. A validator that returns nothing and confers nothing."""

    if schema_version >= SCHEMA_VERSION:
        raise DrillRefused(
            f"archive: schema_version {schema_version} is not a retired schema (the running "
            f"SCHEMA_VERSION is {SCHEMA_VERSION}); a current or newer store is never an archive"
        )
    if schema_head != f"schema_version:{schema_version}":
        raise DrillRefused(
            f"archive: head {schema_head} is not the form schema_version:{schema_version}; an "
            "archive is bound to one numeric retired version"
        )


def run_archive_drill(
    source: BoundSource,
    out_dir: Path,
    restore_into: Path,
    *,
    now: datetime,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
) -> tuple[GateReport, DrillReport]:
    """The N3-2 cutover's drill over an OPEN bound source — the only producer of
    ``ARCHIVE_VERIFIED`` (r3). It runs the REAL flatten gate (``cutover_archive.flatten_gate``)
    over ``source`` here: a store the gate refuses raises the gate's own refusal and nothing is
    created. The acceptance of that run — the envelope must record the gated inode, the restored
    copy must carry exactly the head the gate read, a retired one — and the report are INLINED
    in this body (r5, Daybreak r4 P1-2): no function, lambda or class is defined here, so nothing
    callable closes over the run's values, and nothing a caller-supplied ``Clock`` can retain by
    walking the frames outlives or escapes this call — the frame's locals are plain values.
    ``_verify_in``, ``restore`` and every other module-level function perform the repository's
    acceptance only. Trust root, stated: code running in the same Python process is trusted (it
    could replace ``flatten_gate`` itself); the guarantee against it is only that no verifier
    object outlives or escapes the drill. Then backup → rpo → restore, verified by this run.
    Returns the gate's report with the drill's."""

    from chronos.operations.cutover_archive import flatten_gate  # the cycle is function-local

    clock = clock or system_clock()
    keys = keys or load_age_keys()
    gate = flatten_gate(source, now=now)
    if gate.source_identity != source.identity:
        raise DrillRefused(
            "archive: the gate report names another source than the capability handed in"
        )
    _require_retired(gate.schema_head, gate.schema_version)
    admitted_head = gate.schema_head
    admitted_identity = source.identity
    acceptance = f"archive:{admitted_head}"
    manifest = backup(source.path, out_dir, clock=clock, keys=keys, source=source)
    failures, rpo, basis = _rpo_or_refusal(manifest, keys)
    # the restore, verified by THIS gate run — inline, nothing callable holds the run's values
    started = clock.monotonic()
    restore_failures: list[str]
    facts: dict[str, bool | str]
    with _staged_restore(manifest, restore_into, keys) as staged:
        if staged.decrypt_refused is not None:
            restore_failures = [f"decrypt: refused — {staged.decrypt_refused}"]
            facts = {
                "sha256_ok": False,
                "schema_head_ok": False,
                "row_counts_ok": False,
                "audit_chain": "NOT_APPLICABLE" if manifest.audit_head is None else "ABSENT",
            }
        else:
            # an archive of a RETIRED schema: the running repository refuses that version by
            # design, so this run's acceptance replaces it — nothing is opened by pathname and
            # nothing is initialized on the copy (the copy's bytes are the held descriptor's;
            # head and counts came through _readonly_fd)
            fd, restore_failures, facts, head, counts = _open_copy(manifest, staged.target_fd)
            os.close(fd)
            problems: list[str] = []
            if manifest.source_identity is None or (
                tuple(manifest.source_identity) != tuple(admitted_identity)
            ):
                recorded = (
                    "none"
                    if manifest.source_identity is None
                    else f"{manifest.source_identity[0]}:{manifest.source_identity[1]}"
                )
                problems.append(
                    f"archive: the envelope's source identity {recorded} is not the gated "
                    f"source {admitted_identity[0]}:{admitted_identity[1]} (device:inode); the "
                    "backup was not of the gated object"
                )
            if head != admitted_head:
                problems.append(
                    f"schema_head: restored {head} != archive head {admitted_head}; a "
                    "retired-schema archive is verified by exact head"
                )
            restore_failures.extend(problems)
            facts["acceptance"] = acceptance
            _finish_verification(
                manifest,
                staged.target_fd,
                staged.target_abs,
                staged.has_audit,
                head,
                counts,
                restore_failures,
                facts,
            )
            facts["schema_head_ok"] = bool(facts["schema_head_ok"]) and not problems
            restore_failures.extend(_target_swapped_after(staged))
    report = RestoreReport(
        target_dir=str(staged.target_abs),
        restored_path=str(staged.target_abs / manifest.cleartext_name),
        sha256_ok=bool(facts["sha256_ok"]),
        schema_head_ok=bool(facts["schema_head_ok"]),
        row_counts_ok=bool(facts["row_counts_ok"]),
        audit_chain=str(facts["audit_chain"]),
        failures=tuple(restore_failures),
        rto_s=clock.monotonic() - started,
        acceptance=acceptance,
    )
    failures.extend(report.failures)
    return gate, DrillReport(
        manifest=manifest,
        restore=report,
        rpo_s=rpo,
        rpo_basis=basis,
        rto_s=report.rto_s,
        verdict=ARCHIVE_VERIFIED if not failures else "FAILED",
        failures=tuple(failures),
    )


def publish_envelope(temp: Path, final: Path) -> None:
    """The by-hand publication step, made mechanically exclusive: ``temp`` and ``final`` must
    name entries of the same directory (reached by the no-follow walk); ``temp`` is opened
    ``O_DIRECTORY|O_NOFOLLOW`` and its identity retained — the directory this call CHECKED;
    the rename is ONE renameat2(RENAME_NOREPLACE) by that same name — an existing file OR
    directory at ``final`` is a typed refusal and nothing moves — then the directory is
    fsynced and the final name is re-proved: by lstat it must be a directory with the
    retained identity AND the no-follow walk of the reported final path must reach it;
    otherwise a ``published:`` refusal, never a success line (r5: a link planted at the
    freed temp name between the check and the rename was renamed in the envelope's place)."""

    temp = temp if temp.is_absolute() else Path.cwd() / temp
    final = final if final.is_absolute() else Path.cwd() / final
    if temp.parent != final.parent:
        raise DrillRefused(
            "publish-envelope: <temp> and <final> must be entries of the same directory"
        )
    dfd, _absolute = _walk_directory(temp.parent, "envelope directory", create=False)
    try:
        try:
            temp_fd = os.open(temp.name, _DIR_FLAGS, dir_fd=dfd)
        except NotADirectoryError:
            raise DrillRefused(f"publish-envelope: {temp} is not a directory") from None
        except OSError as error:
            raise DrillRefused(f"publish-envelope: {temp} {error.strerror}") from None
        try:
            checked = _dir_identity(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            _renameat2(dfd, temp.name, dfd, final.name, _RENAME_NOREPLACE)
        except FileExistsError:
            raise DrillRefused(
                f"{final.name} already exists in the destination; the drill never overwrites"
            ) from None
        except OSError as error:
            raise DrillRefused(
                f"publication of {final.name} failed: [errno {error.errno}] {error.strerror}"
            ) from None
        os.fsync(dfd)
        if not _entry_is_real_directory(dfd, final.name, checked) or (
            _nofollow_identity(final) != checked
        ):
            raise DrillRefused(
                f"published: an entry was published at {final} but it is not the checked "
                f"envelope (device:inode {checked[0]}:{checked[1]} by O_NOFOLLOW open before "
                "the rename) — the temp name was replaced between the check and the rename, "
                "or an ancestor was swapped after it; the entry at the final name is NOT a "
                "backup: inspect it and the displaced directory by hand"
            )
    finally:
        os.close(dfd)


def _cli_publish(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.restore_drill publish-envelope",
        description="Rename a staged temp envelope to its final name with ONE exclusive rename.",
    )
    parser.add_argument("temp", type=Path)
    parser.add_argument("final", type=Path)
    args = parser.parse_args(argv)
    try:
        publish_envelope(args.temp, args.final)
    except DrillRefused as error:
        print(json.dumps({"verdict": "FAILED", "failures": [f"refused: {error}"]}, sort_keys=True))
        return 2
    print(json.dumps({"verdict": "PUBLISHED", "envelope": str(args.final)}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["publish-envelope"]:
        return _cli_publish(argv[1:])
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
    payload["manifest_path"] = str(Path(report.manifest.backup_path).with_name(MANIFEST_NAME))
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report.verdict == "VERIFIED" else 2


if __name__ == "__main__":
    sys.exit(main())

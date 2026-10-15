"""Archive / pre-cutover backup pruner (N3-3): 90 days, then delete.

Kevin's ruling 1 (2026-10-15, ``RULINGS-20261015.md``): "The encrypted archive and
pre-cutover backups: 90 days, then delete." The restore-drill harness never deletes
(``docs/ops/RESTORE-DRILL.md``); this is the Kevin-run command that does.

What it deletes, exactly
------------------------
An *envelope*: a directory ``chronos-<stamp>/`` directly under the backup root holding the
ciphertext ``chronos.db.age``, ``manifest.json`` and (optionally) the audit pair. It is
deleted ONLY when its own manifest PROVES it is pre-cutover and it is more than
:data:`RETENTION_DAYS` old:

* class — the manifest's recorded ``schema_head``: ``schema_version:N`` with ``N`` below
  :data:`CUTOVER_SCHEMA` (18) is pre-cutover (the archive of the old schema-17 database is
  one such envelope). ``N >= 18`` is post-cutover and is never touched. Any other head
  (``alembic:<rev>``, ``unversioned``, …) has no documented mapping to a schema number, so it
  is *unclassifiable*: kept and reported.
* age — ``now - snapshot_completed_at`` from the same manifest (a recorded instant, never a
  file mtime). Strictly more than 90 days prunes; exactly 90 is kept. A future-dated snapshot
  is unclassifiable (it means a clock is wrong).

Everything else is kept and reported: crash temps, restore copies, stray files, an envelope
with an unexpected file in it, a malformed manifest. A symlink anywhere in the root (or the
root itself) refuses the WHOLE run before anything is deleted — it is never followed.

The default is a DRY RUN. ``--apply`` deletes. There is no flag for the retention and no
``--now``: a pinned clock exists only as ``main(argv, clock=…)`` for tests.

Output is JSON of counts and names — never manifest contents. Exit codes: 0 nothing to do,
10 dry run found candidates (nothing deleted), 11 pruned, 2 refused.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronos.operations.restore_drill import (
    AUDIT_ANCHOR_NAME,
    AUDIT_LOG_NAME,
    DB_NAME,
    ENCRYPTED_DB_SUFFIX,
    MANIFEST_NAME,
    BackupManifest,
    DrillRefused,
    _open_regular_at,
    _walk_directory,
)

#: Kevin's ruling 1 (RULINGS-20261015.md, 2026-10-15): archive and pre-cutover backups live
#: 90 days. Not configurable: raising or lowering it is a reviewed code change.
RETENTION_DAYS = 90
#: Schema 18 is the first keyed schema (N3-1); a manifest recording a lower schema number is
#: pre-cutover.
CUTOVER_SCHEMA = 18

EXIT_NOTHING_TO_DO = 0
EXIT_REFUSED = 2
EXIT_WOULD_PRUNE = 10
EXIT_PRUNED = 11

_ENVELOPE_RE = re.compile(r"^chronos-\d{8}T\d{6}Z$")
_SCHEMA_HEAD_RE = re.compile(r"^schema_version:(\d+)$")
_ENVELOPE_FILES = frozenset(
    {MANIFEST_NAME, DB_NAME + ENCRYPTED_DB_SUFFIX, AUDIT_LOG_NAME, AUDIT_ANCHOR_NAME}
)
_REQUIRED_FILES = frozenset({MANIFEST_NAME, DB_NAME + ENCRYPTED_DB_SUFFIX})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True, slots=True)
class _Finding:
    name: str
    status: str  # prune | keep | unclassified | refused
    cls: str
    age_days: float | None
    why: str
    identity: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "class": self.cls, "why": self.why}
        if self.age_days is not None:
            out["age_days"] = round(self.age_days, 2)
        return out


def _classify_envelope(
    root_fd: int, name: str, identity: tuple[int, int], now: datetime
) -> _Finding:
    def unclassified(why: str) -> _Finding:
        return _Finding(name, "unclassified", "unknown", None, why)

    try:
        dir_fd = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
    except OSError as error:
        return unclassified(f"could not be opened: {error.strerror}")
    try:
        if (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino) != identity:
            return unclassified("changed while being scanned")
        entries = set(os.listdir(dir_fd))
        if not entries >= _REQUIRED_FILES:
            return unclassified(f"missing {sorted(_REQUIRED_FILES - entries)}")
        if entries - _ENVELOPE_FILES:
            return unclassified(
                f"holds files that are not an envelope's: {sorted(entries - _ENVELOPE_FILES)}"
            )
        for entry in sorted(entries):
            if not stat.S_ISREG(os.stat(entry, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return unclassified(f"{entry} is not a regular file")
        try:
            fd = _open_regular_at(dir_fd, MANIFEST_NAME, "manifest")
            with os.fdopen(fd, "rb") as handle:
                raw = json.loads(handle.read(1 << 20))
            manifest = BackupManifest.from_dict(raw)
            taken = datetime.fromisoformat(manifest.snapshot_completed_at)
        except (DrillRefused, ValueError, KeyError, TypeError, AttributeError, OSError):
            return unclassified("manifest is missing fields or unreadable")
    finally:
        os.close(dir_fd)
    if taken.tzinfo is None:
        return unclassified("snapshot_completed_at carries no timezone")
    if not str(manifest.encryption.get("scheme", "")).startswith("age"):
        return unclassified("manifest does not record an age-encrypted envelope")
    age = now - taken
    age_days = age / timedelta(days=1)
    if age < timedelta(0):
        return _Finding(name, "unclassified", "unknown", age_days, "dated in the future (clock?)")
    match = _SCHEMA_HEAD_RE.match(manifest.schema_head)
    if match is None:
        return _Finding(
            name,
            "unclassified",
            "unknown",
            age_days,
            f"schema head {manifest.schema_head!r} has no documented schema number",
        )
    schema = int(match.group(1))
    if schema >= CUTOVER_SCHEMA:
        return _Finding(
            name, "keep", "post-cutover", age_days, f"schema {schema} is post-cutover: never pruned"
        )
    if age <= timedelta(days=RETENTION_DAYS):
        return _Finding(
            name,
            "keep",
            "pre-cutover",
            age_days,
            f"schema {schema} is pre-cutover but within the {RETENTION_DAYS}-day window",
        )
    return _Finding(
        name,
        "prune",
        "pre-cutover",
        age_days,
        f"schema {schema} is pre-cutover and older than {RETENTION_DAYS} days",
        identity,
    )


def _scan(root_fd: int, now: datetime) -> list[_Finding]:
    findings: list[_Finding] = []
    for name in sorted(os.listdir(root_fd)):
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            findings.append(
                _Finding(name, "refused", "symlink", None, "is a symlink: never followed")
            )
        elif not stat.S_ISDIR(info.st_mode):
            findings.append(_Finding(name, "unclassified", "unknown", None, "not a directory"))
        elif not _ENVELOPE_RE.match(name):
            findings.append(
                _Finding(name, "unclassified", "unknown", None, "not a published envelope name")
            )
        else:
            findings.append(_classify_envelope(root_fd, name, (info.st_dev, info.st_ino), now))
    return findings


def _delete_envelope(root_fd: int, finding: _Finding) -> None:
    """Remove one proved envelope: re-check its identity and file set, unlink its files
    descriptor-relative, then the directory. Raises ``OSError``/``DrillRefused`` if the
    directory is no longer the one that was classified."""

    info = os.stat(finding.name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != finding.identity:
        raise DrillRefused(f"{finding.name} changed after it was classified; left in place")
    dir_fd = os.open(finding.name, _DIR_FLAGS, dir_fd=root_fd)
    try:
        entries = set(os.listdir(dir_fd))
        if entries - _ENVELOPE_FILES:
            raise DrillRefused(
                f"{finding.name} gained files after it was classified; left in place"
            )
        for entry in sorted(entries):
            os.unlink(entry, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    os.rmdir(finding.name, dir_fd=root_fd)
    os.fsync(root_fd)


def prune(root: Path, *, now: datetime, apply: bool) -> dict[str, object]:
    """Classify every entry under ``root``; delete the proved-old pre-cutover envelopes only
    when ``apply``. Returns the JSON-ready report (counts and names; ``exit`` is the code)."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    report: dict[str, object] = {
        "root": str(root),
        "applied": apply,
        "retention_days": RETENTION_DAYS,
        "candidates": [],
        "kept": [],
        "unclassified": [],
        "refused": [],
        "deleted": [],
        "failed": [],
    }
    try:
        root_fd, _absolute = _walk_directory(root, "backup root", create=False)
    except DrillRefused as error:
        report["refused"] = [str(error)]
        return _finish(report, EXIT_REFUSED)
    try:
        findings = _scan(root_fd, now)
        report["kept"] = [f.to_dict() for f in findings if f.status == "keep"]
        report["unclassified"] = [f.to_dict() for f in findings if f.status == "unclassified"]
        report["refused"] = [f"{f.name}: {f.why}" for f in findings if f.status == "refused"]
        candidates = [f for f in findings if f.status == "prune"]
        report["candidates"] = [f.to_dict() for f in candidates]
        if report["refused"]:
            return _finish(report, EXIT_REFUSED)
        if not candidates:
            return _finish(report, EXIT_NOTHING_TO_DO)
        if not apply:
            return _finish(report, EXIT_WOULD_PRUNE)
        deleted: list[str] = []
        failed: list[str] = []
        for finding in candidates:
            try:
                _delete_envelope(root_fd, finding)
            except (OSError, DrillRefused) as error:
                failed.append(f"{finding.name}: {error}")
            else:
                deleted.append(finding.name)
        report["deleted"] = deleted
        report["failed"] = failed
        return _finish(report, EXIT_REFUSED if failed else EXIT_PRUNED)
    finally:
        with contextlib.suppress(OSError):
            os.close(root_fd)


def _finish(report: dict[str, object], code: int) -> dict[str, object]:
    def count(key: str) -> int:
        value = report[key]
        return len(value) if isinstance(value, list) else 0

    report["counts"] = {
        "deleted": count("deleted"),
        "kept": count("kept"),
        "unclassified": count("unclassified"),
        "refused": count("refused") + count("failed"),
    }
    report["exit"] = code
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.backup_pruner",
        description=(
            f"Delete archive / pre-cutover backup envelopes older than {RETENTION_DAYS} days "
            "(Kevin's ruling 1, 2026-10-15). DRY RUN unless --apply."
        ),
    )
    parser.add_argument("--root", required=True, type=Path, help="the backup root directory")
    parser.add_argument(
        "--apply", action="store_true", help="delete (default: list candidates, delete nothing)"
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, clock: Callable[[], datetime] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    now = (clock or (lambda: datetime.now(UTC)))()
    report = prune(args.root, now=now, apply=args.apply)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    code = report["exit"]
    assert isinstance(code, int)
    return code


if __name__ == "__main__":
    sys.exit(main())

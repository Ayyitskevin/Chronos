"""Owner-only permissions on platform state files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from chronos.auditlog.log import AuditLog
from chronos.control.halt import HaltReason, HaltStore
from chronos.execution.sqlite_ledger import SqliteLedger
from chronos.utils.secure_files import secure_owner_only


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_owner_only_sets_0600(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("x", encoding="utf-8")
    os.chmod(path, 0o644)
    secure_owner_only(path)
    assert mode(path) == 0o600


def test_secure_owner_only_missing_file_is_noop(tmp_path: Path) -> None:
    secure_owner_only(tmp_path / "absent")  # must not raise


def test_secure_owner_only_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        secure_owner_only(link)


def test_halt_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "halt.json"
    HaltStore(path).halt(HaltReason.OPERATOR_REQUEST, "x")
    assert mode(path) == 0o600


def test_audit_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append("e", {"n": 1})
    assert mode(path) == 0o600


def test_ledger_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = SqliteLedger(path)
    ledger.close()
    assert mode(path) == 0o600

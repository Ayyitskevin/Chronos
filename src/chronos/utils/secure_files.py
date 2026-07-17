"""Shared owner-only file permission helper for local state files.

Mirrors the safety posture `chronos.utils.logging._secure_existing_log`
already applies to the wheel dashboard's log files, generalized for the
platform's ledger, halt, and audit files (RISK_REGISTER R-13/R-14 note: these
files hold trade intents, symbols, and prices — not credentials — but should
still not be world-readable on a shared machine). Refuses to follow a
symlink and refuses a file not owned by the current process, so a local
attacker cannot redirect the chmod onto an unrelated file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def secure_owner_only(path: Path) -> None:
    """Restrict an existing regular file to owner read/write (0o600)."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"expected a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise ValueError(f"expected an owned regular file: {path}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)

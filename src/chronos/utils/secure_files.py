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

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
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


class UnsafeAuthorityFile(Exception):
    """An authority file failed a safety check. Nothing was read from it."""


@dataclass(frozen=True)
class AuthorityFileContents:
    """Bytes read from one descriptor, and the digest of exactly those bytes."""

    data: bytes
    sha256: str


def _describe(path: Path, label: str, problem: str) -> UnsafeAuthorityFile:
    return UnsafeAuthorityFile(f"{label} at {path}: {problem}")


def read_authority_file(path: Path, *, label: str) -> AuthorityFileContents:
    """Read a file that grants authority, binding every check to one descriptor.

    ``secure_owner_only`` above protects state files whose *contents* are not
    secrets; this is for the files that decide who may act — the local API
    token today, the owner-authored mandate and proposer registry next. Those
    need the stronger contract, because the path-based pattern they use now
    (``path.exists()`` then ``path.read_text()``) has three holes: it follows a
    symlink, it re-resolves the path between the check and the read, and it
    never looks at the mode at all — so a token file left world-readable by an
    earlier install, or replaced by a link to an attacker's file, is read
    without complaint.

    Every check here runs against the descriptor that is then read, so nothing
    can be swapped underneath: ``O_NOFOLLOW`` refuses a symlink at the final
    component, and ``fstat`` on the open descriptor establishes that it is a
    regular file, owned by this effective user, with exactly ``0600`` and a
    single link. The digest is taken over the same bytes the caller receives.

    Raises ``FileNotFoundError`` when the file is absent — that is a state, not
    a fault — and :class:`UnsafeAuthorityFile` for every unsafe shape.
    """

    # O_NONBLOCK matters for exactly one shape: a FIFO planted at the path would
    # otherwise block this open until someone opened the write end, turning a
    # refusal into a hang at startup. It is a no-op for regular files, which is
    # all an authority file is ever allowed to be.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise FileNotFoundError(str(path)) from error
        if error.errno in (errno.ELOOP, errno.EMLINK):
            # O_NOFOLLOW reports ELOOP for a symlink at the final component.
            raise _describe(path, label, "is a symlink") from error
        raise _describe(path, label, f"could not be opened safely: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _describe(path, label, "is not a regular file")
        if opened.st_uid != os.geteuid():
            raise _describe(
                path, label, f"is owned by uid {opened.st_uid}, not this process's effective user"
            )
        mode = stat.S_IMODE(opened.st_mode)
        if mode != 0o600:
            raise _describe(
                path,
                label,
                f"has mode {mode:04o}; authority files must be exactly 0600. Fix it "
                f"deliberately (chmod 600) rather than having the process widen or "
                f"narrow it silently",
            )
        if opened.st_nlink != 1:
            raise _describe(path, label, f"has {opened.st_nlink} links; it must have exactly one")
        data = b""
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(descriptor)
    return AuthorityFileContents(data=data, sha256=hashlib.sha256(data).hexdigest())


def create_authority_file(path: Path, data: bytes, *, label: str) -> AuthorityFileContents:
    """Create an authority file that never exists in a readable-by-others state.

    The old shape wrote the token with the process umask and chmodded it
    afterwards, so the secret existed world-readable for the width of that
    window. ``O_CREAT | O_EXCL`` with mode ``0600`` closes both halves: the file
    is owner-only from its first byte, and an existing file — or a symlink
    planted at the path — makes the create fail instead of being followed.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise _describe(path, label, "already exists; refusing to overwrite it") from error
    except OSError as error:
        raise _describe(path, label, f"could not be created safely: {error}") from error
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return AuthorityFileContents(data=data, sha256=hashlib.sha256(data).hexdigest())

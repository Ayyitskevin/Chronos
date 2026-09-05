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

import enum
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


class AuthorityMode(enum.Enum):
    """What an authority file needs protecting *from*. The caller must say which.

    Conflating these two was the mistake this enum exists to prevent. They are
    not strength levels — they answer different questions:

    ``SECRET``
        The file's *contents* are the credential: the local API token opens
        every writing route on this host. Confidentiality is the property, so
        the mode must be exactly ``0600`` — anything readable by anyone else is
        a leak, and anything wider than the process needs is a mistake.

    ``GRANT``
        The file's contents are an owner-authored *decision* — the autonomy
        mandate, the proposer registry. They are not secrets; they are read as
        authority, so what matters is that nobody else could have **written**
        them. Group- and other-writable are refused; ``0644`` is accepted,
        because a world-readable grant document leaks nothing and refusing it
        would reject every mandate created the way `docs/model_worker.md`
        documents (``chronos.cli mandate template > file``, whose mode comes
        from the operator's umask).

    Demanding ``0600`` of a grant would have been security theatre with a real
    cost: startup refusing a mandate the docs told the owner to create.
    """

    # ``auto()`` rather than string values: the member identity is the whole
    # contract (every comparison below is ``mode is AuthorityMode.X``), and a
    # literal ``= "secret"`` is a Secret Keyword finding in the tracked-file
    # scan on a value that is not one.
    SECRET = enum.auto()
    GRANT = enum.auto()


@dataclass(frozen=True)
class AuthorityFileContents:
    """Bytes read from one descriptor, the digest of exactly those bytes, and
    the same bytes decoded.

    ``text`` is decoded here rather than by each caller because every authority
    file in this system is text by contract — a JSON grant or a token — and
    ``bytes.decode`` raising ``UnicodeDecodeError`` out of a startup path is a
    traceback where a refusal belongs. The decode is part of the read, so an
    unreadable file is one unsafe shape with one exception type.
    """

    data: bytes
    sha256: str
    text: str


def _describe(path: Path, label: str, problem: str) -> UnsafeAuthorityFile:
    return UnsafeAuthorityFile(f"{label} at {path}: {problem}")


def _contents(path: Path, label: str, data: bytes) -> AuthorityFileContents:
    """Bytes, digest, and decoded text — or a refusal naming the file."""

    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _describe(
            path,
            label,
            "is not valid UTF-8, so it cannot be the text this system reads as "
            f"authority ({error.reason} at byte {error.start})",
        ) from error
    return AuthorityFileContents(data=data, sha256=hashlib.sha256(data).hexdigest(), text=decoded)


def read_authority_file(path: Path, *, label: str, mode: AuthorityMode) -> AuthorityFileContents:
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
    regular file, owned by this effective user, with a single link. The digest
    is taken over the same bytes the caller receives.

    ``mode`` selects the permission rule and nothing else — see
    :class:`AuthorityMode`. Every other check above is identical in both,
    because a symlinked, group-writable, or swapped-underneath grant is exactly
    as dangerous as a swapped secret.

    Raises ``FileNotFoundError`` when the file is absent — that is a state, not
    a fault — and :class:`UnsafeAuthorityFile` for every unsafe shape. Nothing
    is ever repaired: startup refuses and says what to fix, because silently
    chmodding an owner-authored file is the process editing a grant.
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
        found = stat.S_IMODE(opened.st_mode)
        if mode is AuthorityMode.SECRET and found != 0o600:
            raise _describe(
                path,
                label,
                f"has mode {found:04o}; a SECRET authority file holds the credential "
                f"itself and must be exactly 0600. Fix it deliberately (chmod 600) "
                f"rather than having the process widen or narrow it silently",
            )
        if mode is AuthorityMode.GRANT and found & 0o022:
            raise _describe(
                path,
                label,
                f"has mode {found:04o}; a GRANT authority file must not be writable by "
                f"group or other, because anyone who can write it can change what this "
                f"system is authorized to do. Fix it deliberately (chmod go-w) rather "
                f"than having the process narrow it silently. Readable is fine: 0644 "
                f"is accepted, 0664 is not",
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
    return _contents(path, label, data)


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
        # The mode passed to open() is masked by the process umask, so `0o600`
        # is a ceiling, not a guarantee: under a umask that clears owner bits
        # (0o200, say) the file is created without owner-write and the very
        # next read refuses it. The old shape chmodded after writing, which
        # made the final mode umask-independent; folding the mode into the open
        # kept the file owner-only from its first byte but gave that property
        # back. This keeps both — narrow at creation, exact afterwards — and it
        # is an fchmod on the descriptor just opened, so there is no path to
        # re-resolve.
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return _contents(path, label, data)

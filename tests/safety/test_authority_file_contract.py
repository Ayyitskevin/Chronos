"""Authority files are read through one descriptor, or not at all (R-67).

The local API token decides who may drive the order-writing service on this
host. It was loaded with ``path.exists()`` then ``path.read_text()``: two
resolutions of the same path, both following symlinks, with no check on the
file's mode at any point. A token left world-readable by an older install was
read without complaint, and a symlink planted at the path was followed to
whatever it named.

These tests pin the replacement contract in
``chronos.utils.secure_files.read_authority_file`` /
``create_authority_file`` — every check runs against the descriptor that is then
read, and an unsafe file is refused rather than repaired. The mandate and
proposer-registry loaders adopt the same helper in a follow-up PR; the shape is
proven here first.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from chronos.api.auth import load_or_create_token
from chronos.utils.secure_files import (
    UnsafeAuthorityFile,
    create_authority_file,
    read_authority_file,
)

_LABEL = "test authority file"


def _write_owner_only(path: Path, data: bytes) -> Path:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    return path


def test_a_safe_file_is_read_with_the_digest_of_exactly_those_bytes(tmp_path: Path) -> None:
    path = _write_owner_only(tmp_path / "grant", b"secret-value\n")

    contents = read_authority_file(path, label=_LABEL)

    assert contents.data == b"secret-value\n"
    assert contents.sha256 == hashlib.sha256(b"secret-value\n").hexdigest()


def test_a_symlink_is_refused_and_its_target_is_never_read(tmp_path: Path) -> None:
    """The exploit the path-based shape allowed: point the read somewhere else."""

    decoy = _write_owner_only(tmp_path / "attacker_token", b"attacker-controlled\n")
    link = tmp_path / "grant"
    link.symlink_to(decoy)

    with pytest.raises(UnsafeAuthorityFile, match="symlink"):
        read_authority_file(link, label=_LABEL)


def test_a_world_readable_file_is_refused_rather_than_chmodded(tmp_path: Path) -> None:
    """Repairing it silently would hide that something put it in that state."""

    path = _write_owner_only(tmp_path / "grant", b"secret-value\n")
    os.chmod(path, 0o644)

    with pytest.raises(UnsafeAuthorityFile, match="mode 0644"):
        read_authority_file(path, label=_LABEL)

    # Refused, not repaired: the file is exactly as the operator left it.
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_a_hard_linked_file_is_refused(tmp_path: Path) -> None:
    """A second link is a second name for the credential, outside this directory."""

    path = _write_owner_only(tmp_path / "grant", b"secret-value\n")
    os.link(path, tmp_path / "second_name")

    with pytest.raises(UnsafeAuthorityFile, match="links"):
        read_authority_file(path, label=_LABEL)


def test_a_non_regular_file_is_refused(tmp_path: Path) -> None:
    """A FIFO would block the read and is not a file anyone granted anything in."""

    fifo = tmp_path / "grant"
    os.mkfifo(fifo, 0o600)

    with pytest.raises(UnsafeAuthorityFile, match="not a regular file"):
        read_authority_file(fifo, label=_LABEL)


def test_an_absent_file_is_a_state_not_a_fault(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_authority_file(tmp_path / "missing", label=_LABEL)


def test_creation_is_owner_only_from_its_first_byte(tmp_path: Path) -> None:
    """The old shape wrote with the umask and chmodded afterwards."""

    path = tmp_path / "grant"
    contents = create_authority_file(path, b"fresh\n", label=_LABEL)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert contents.data == b"fresh\n"
    assert read_authority_file(path, label=_LABEL).sha256 == contents.sha256


def test_creation_refuses_an_existing_file_and_a_planted_symlink(tmp_path: Path) -> None:
    existing = _write_owner_only(tmp_path / "grant", b"already here\n")
    with pytest.raises(UnsafeAuthorityFile, match="already exists"):
        create_authority_file(existing, b"overwrite\n", label=_LABEL)
    assert existing.read_bytes() == b"already here\n"

    decoy = _write_owner_only(tmp_path / "attacker_target", b"attacker\n")
    link = tmp_path / "planted"
    link.symlink_to(decoy)
    with pytest.raises(UnsafeAuthorityFile):
        create_authority_file(link, b"secret\n", label=_LABEL)
    assert decoy.read_bytes() == b"attacker\n"


# --- the wired path: the local API token ------------------------------------


def test_the_api_token_is_created_owner_only_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "backend_api_token"

    token = load_or_create_token(path)

    assert token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == token  # second call reads, never regenerates


def test_a_world_readable_api_token_refuses_startup(tmp_path: Path) -> None:
    """Fail loudly rather than widen or narrow a credential on the owner's behalf."""

    path = tmp_path / "backend_api_token"
    load_or_create_token(path)
    os.chmod(path, 0o640)

    with pytest.raises(UnsafeAuthorityFile, match="0640"):
        load_or_create_token(path)


def test_a_symlinked_api_token_path_never_yields_the_target_token(tmp_path: Path) -> None:
    decoy = _write_owner_only(tmp_path / "attacker_token", b"attacker-token\n")
    link = tmp_path / "backend_api_token"
    link.symlink_to(decoy)

    with pytest.raises(UnsafeAuthorityFile, match="symlink"):
        load_or_create_token(link)


def test_an_empty_api_token_file_is_regenerated(tmp_path: Path) -> None:
    """Present-but-empty is a state no run wrote; it is safe to replace.

    The file has already passed every safety check by the time this happens, so
    replacing it is the first-use path rather than an overwrite of live data.
    """

    path = _write_owner_only(tmp_path / "backend_api_token", b"")

    token = load_or_create_token(path)

    assert token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

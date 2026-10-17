"""Escrow ONE encrypted copy of the persistence key and prove it decrypts back (runbook step 6).

Kevin's custody ruling: he generates the key himself, places it in the host's 0600 ``.env``, and
keeps one age-encrypted escrow copy off-host — his keystrokes only. Daybreak P1-3 (N3-2 review):
the earlier runbook step piped a shell variable into ``age``, and an unset variable silently
escrowed an empty line. This tool closes that: the key is typed into a non-echoing prompt (or,
for a pipe, read from standard input with ``--key-from-stdin``), NEVER taken from the environment
or the command line; anything but exactly one line of 64 lowercase hex characters — what
``secrets.token_hex(32)`` prints — is refused with nothing written; the line is encrypted with
``age`` to the two recipients the restore drill validates (the host identity's public key and the
owner's pinned recovery recipient — the backups' own policy; the host identity is the only key on
the host that can DECRYPT, which the proof below needs); the new artifact is opened ONCE and held
through the proof: it is decrypted THROUGH THAT DESCRIPTOR (``/proc/self/fd/<n>`` names the inode
this run created, whatever the directory entry now says) through the host identity into a private
0700 scratch directory and compared, byte for byte, in this process; the scratch is unlinked; the
output name must still resolve to the held file (``TARGET_SWAPPED`` otherwise); only then does the
tool report. On any failure after the file was created, the tool DELETES NOTHING (r3, Daybreak
P1-3: Linux has no unlink-by-descriptor, every unlink is by name, and a name can be swapped under
the tool in the instant before the syscall — so no cleanup is bound to the file it made): the
report's ``created_artifact`` names the created ciphertext's path and device:inode for the owner
to find and shred, at its name or under a renamed entry. The only things the tool ever removes
are its own private scratch (the cleartext the proof wrote, and the 0700 directory it made). The
report never carries the key, nor a digest of it — the ciphertext's sha256, the output path and
the recipients only::

    python -m chronos.operations.persistence_key_escrow --out chronos-persistence-key.age [--pretty]

Exit 0 with ``"verdict": "ESCROWED"``; exit 2 with a ``code`` otherwise. Runbook:
``docs/ops/N3-CUTOVER.md``, step 6.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from chronos.operations.restore_drill import (
    _DIR_FLAGS,
    AgeKeys,
    DrillRefused,
    _entry_is_real_file,
    _open_identity,
    _open_regular_at,
    _run_age,
    _sha256_fd,
    _walk_directory,
    load_age_keys,
)

Outcome = dict[str, object]

RUNBOOK = "docs/ops/N3-CUTOVER.md"
#: Exactly what ``secrets.token_hex(32)`` prints: 64 lowercase hexadecimal characters.
KEY_RE = re.compile(r"[0-9a-f]{64}")
PROMPT = "Persistence key to escrow (64 hex, not echoed): "


class EscrowRefused(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        #: what became of the file this run created, when one was created before the refusal
        self.created: Outcome | None = None


def _read_key(*, from_stdin: bool) -> str:
    """One line, from the non-echoing prompt or (``--key-from-stdin``) from standard input;
    refused unless it is exactly one 64-lowercase-hex line. The value is never echoed, logged
    or placed in the report."""

    text = sys.stdin.read() if from_stdin else getpass.getpass(PROMPT)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) != 1 or not KEY_RE.fullmatch(lines[0]):
        got = (
            "no line"
            if not lines
            else f"{len(lines)} line(s), first of {len(lines[0])} character(s)"
        )
        raise EscrowRefused(
            "KEY_MALFORMED",
            f"the input is not exactly one line of 64 lowercase hex characters ({got}); nothing "
            f"was written. Generate the key with python -c 'import secrets; "
            f"print(secrets.token_hex(32))' and type that line ({RUNBOOK}, steps 5-6)",
        )
    return lines[0]


def _encrypt(keys: AgeKeys, key: str, dfd: int, name: str) -> None:
    """``age -r <host> -r <recovery> -o <dir fd>/<name>`` with the key on age's standard input
    — never an argument, never a file."""

    argv = [keys.age]
    for recipient in keys.recipients:
        argv += ["-r", recipient]
    argv += ["-o", f"/proc/self/fd/{dfd}/{name}"]
    try:
        completed = subprocess.run(  # argv list, no shell; the binary was resolved by which()
            argv,
            input=(key + "\n").encode("utf-8"),
            capture_output=True,
            check=False,
            pass_fds=(dfd,),
        )
    except OSError as error:
        raise EscrowRefused("AGE_FAILED", f"could not run {argv[0]}: {error.strerror}") from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise EscrowRefused("AGE_FAILED", f"age exited {completed.returncode}: {detail}")


def _decrypts_back_to(keys: AgeKeys, key: str, artifact_fd: int) -> None:
    """Decrypt the new artifact THROUGH THE HELD DESCRIPTOR — ``/proc/self/fd/<n>`` in the
    child names the inode this run created, whatever the directory entry now says — through
    the host identity into a private 0700 scratch (a descriptor-bound write), compare in
    process, unlink the scratch; no printing. A failing decrypt is ``AGE_FAILED``."""

    scratch = Path(tempfile.mkdtemp(prefix=".chronos-escrow-"))  # mode 0700
    try:
        scratch_fd = os.open(scratch, _DIR_FLAGS)
        try:
            try:
                identity_fd = _open_identity(keys.identity_path)
            except DrillRefused as error:
                raise EscrowRefused("KEYS_REFUSED", str(error)) from None
            try:
                try:
                    _run_age(
                        [
                            keys.age,
                            "-d",
                            "-i",
                            f"/proc/self/fd/{identity_fd}",
                            "-o",
                            f"/proc/self/fd/{scratch_fd}/key",
                            f"/proc/self/fd/{artifact_fd}",
                        ],
                        what="decrypting the new escrow artifact through the host identity",
                        pass_fds=(identity_fd, scratch_fd, artifact_fd),
                    )
                    fd = _open_regular_at(scratch_fd, "key", "decrypted escrow")
                except DrillRefused as error:
                    raise EscrowRefused("AGE_FAILED", str(error)) from None
            finally:
                os.close(identity_fd)
            with os.fdopen(fd, "rb") as handle:
                recovered = handle.read()
            if recovered != (key + "\n").encode("utf-8"):
                raise EscrowRefused(
                    "ESCROW_MISMATCH",
                    "the artifact decrypted to something other than the key that was typed — "
                    "do not keep it; created_artifact says whether this run's file was removed",
                )
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink("key", dir_fd=scratch_fd)
            os.close(scratch_fd)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(scratch)


def _report_created(name: str, identity: tuple[int, int], parent_abs: Path) -> Outcome:
    """After a post-create failure the tool DELETES NOTHING (r3, Daybreak P1-3): Linux has no
    unlink-by-descriptor, so any removal would be by name, and a name can be swapped under the
    tool between its last look and the syscall — r2's identity-checked unlink still deleted a
    foreign entry in that interval. The outcome names the ciphertext this run created, by path
    and device:inode, for the owner to find and shred; ``removed`` is always false."""

    where = str(parent_abs / name)
    ident = f"{identity[0]}:{identity[1]}"
    return {
        "path": where,
        "identity": ident,
        "removed": False,
        "note": (
            f"the tool deletes nothing after a failure: the ciphertext this run created "
            f"(device:inode {ident}) is left where it is — at {where}, or under a renamed entry "
            f"in {parent_abs} if the name was swapped; find it by inode (find {parent_abs} -inum "
            f"{identity[1]}) and shred it yourself"
        ),
    }


def escrow(out: Path, *, from_stdin: bool = False) -> dict[str, object]:
    out = Path(out)
    try:
        keys = load_age_keys()  # every key refusal precedes the prompt
    except DrillRefused as error:
        raise EscrowRefused("KEYS_REFUSED", str(error)) from None
    key = _read_key(from_stdin=from_stdin)
    dfd, parent_abs = _walk_directory(out.parent, "escrow directory", create=False)
    try:
        name = out.name
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise EscrowRefused(
                "TARGET_EXISTS", f"{parent_abs / name} already exists; this tool never overwrites"
            )
        _encrypt(keys, key, dfd, name)
        try:
            fd = _open_regular_at(dfd, name, "escrow artifact")  # held through the proof
        except DrillRefused as error:
            raise EscrowRefused(
                "AGE_FAILED", f"age reported success but {parent_abs / name}: {error}"
            ) from None
        try:
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
            try:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                if not _entry_is_real_file(dfd, name, identity):
                    raise EscrowRefused(
                        "TARGET_SWAPPED",
                        f"{parent_abs / name} is not the file age wrote (the name was swapped as "
                        "it was written)",
                    )
                ciphertext_sha256 = _sha256_fd(fd)
                _decrypts_back_to(keys, key, fd)  # the held inode, whatever the name says now
                if not _entry_is_real_file(dfd, name, identity):
                    raise EscrowRefused(
                        "TARGET_SWAPPED",
                        f"{parent_abs / name} no longer names the artifact this run created and "
                        "verified (the name was swapped during the proof)",
                    )
                os.fsync(dfd)
            except BaseException as error:
                outcome = _report_created(name, identity, parent_abs)  # never an unlink
                if isinstance(error, EscrowRefused):
                    error.created = outcome
                raise
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return {
        "verdict": "ESCROWED",
        "verified": True,
        "out": str(parent_abs / name),
        "ciphertext_sha256": ciphertext_sha256,
        "recipients": list(keys.recipients),
        "next": [
            f"Move {parent_abs / name} off this host (password manager attachment, or where the "
            "backup escrow goes), then remove the local copy yourself.",
            f"Clear the terminal. The key was never printed; this report holds none of it "
            f"({RUNBOOK}).",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.persistence_key_escrow",
        description=(
            "Escrow one age-encrypted copy of the persistence key, typed into a non-echoing "
            "prompt, and prove it decrypts back before reporting."
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="the artifact to create (new)")
    parser.add_argument(
        "--key-from-stdin",
        action="store_true",
        help="read the one key line from standard input instead of the prompt (a pipe; tests)",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = escrow(args.out, from_stdin=args.key_from_stdin)
    except EscrowRefused as error:
        report = {
            "verdict": "REFUSED",
            "code": error.code,
            "failures": [f"{error.code}: {error}"],
            "created_artifact": error.created,
        }
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report["verdict"] == "ESCROWED" else 2


if __name__ == "__main__":
    sys.exit(main())

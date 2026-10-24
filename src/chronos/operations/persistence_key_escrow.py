"""Escrow ONE encrypted copy of the persistence key to the owner's recovery key (runbook step 6).

Kevin's custody ruling: he generates the key himself, places it in the host's 0600 ``.env``, and
keeps one age-encrypted escrow copy off-host — his keystrokes only. Daybreak P1-3 (N3-2 review):
the earlier runbook step piped a shell variable into ``age``, and an unset variable silently
escrowed an empty line. This tool closes that: the key is typed into a non-echoing prompt (or,
for a pipe, read from standard input with ``--key-from-stdin``), NEVER taken from the environment
or the command line; anything but exactly one line of 64 lowercase hex characters — what
``secrets.token_hex(32)`` prints — is refused with nothing written.

K-20261023-003 (escrow = recovery-key-only): the line is encrypted with ``age`` to exactly ONE
recipient, the owner's ``KEVIN_RECOVERY_RECIPIENT``, read at call time from the restore drill
where it is pinned in code. The host identity is not a recipient: nothing on this host can open
the artifact, and the tool reads none of the host's key files and decrypts nothing. What it
proves before it reports, and only this: the key line was well formed and non-empty; ``age``
exited 0; the artifact, which age created through the held directory descriptor and which is
opened ONCE and held from then on, is a non-empty regular file (``ARTIFACT_EMPTY`` otherwise);
the output name still resolves to the held file before the report (``TARGET_SWAPPED``
otherwise). It does NOT prove that the artifact decrypts to the key. That proof is the owner's
decrypt-and-compare, off-host, with his recovery identity, before any local copy is deleted
(runbook step 6); the report says so (``decrypt_verified: false``).

On any failure after the file was created, the tool DELETES NOTHING (r3, Daybreak P1-3: Linux
has no unlink-by-descriptor, every unlink is by name, and a name can be swapped under the tool in
the instant before the syscall — so no cleanup is bound to the file it made): the report's
``created_artifact`` names the created ciphertext's path and device:inode for the owner to find
and shred, at its name or under a renamed entry. The tool removes nothing, ever. The report never
carries the key, nor a digest of it — the ciphertext's sha256, the output path and the one
recipient only::

    python -m chronos.operations.persistence_key_escrow --out chronos-persistence-key.age [--pretty]

Exit 0 with ``"verdict": "ESCROWED"``; exit 2 with a ``code`` otherwise. Runbook:
``docs/ops/N3-CUTOVER.md``, step 6.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from chronos.operations import restore_drill
from chronos.operations.restore_drill import (
    AGE_INSTALL_SENTENCE,
    DrillRefused,
    _entry_is_real_file,
    _open_regular_at,
    _sha256_fd,
    _walk_directory,
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


def _encrypt(age: str, recipient: str, key: str, dfd: int, name: str) -> None:
    """``age -r <recovery> -o <dir fd>/<name>`` — the one recipient — with the key on age's
    standard input — never an argument, never a file."""

    argv = [age, "-r", recipient, "-o", f"/proc/self/fd/{dfd}/{name}"]
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
    age = shutil.which("age")  # a missing tool is refused before the prompt
    if age is None:
        raise EscrowRefused("AGE_MISSING", AGE_INSTALL_SENTENCE)
    recipient = restore_drill.KEVIN_RECOVERY_RECIPIENT  # the ONLY recipient (K-20261023-003)
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
        _encrypt(age, recipient, key, dfd, name)
        try:
            fd = _open_regular_at(dfd, name, "escrow artifact")  # held until the report
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
                if st.st_size == 0:
                    raise EscrowRefused(
                        "ARTIFACT_EMPTY",
                        f"age exited 0 but {parent_abs / name} is empty; nothing was escrowed",
                    )
                ciphertext_sha256 = _sha256_fd(fd)  # the held inode, whatever the name says now
                if not _entry_is_real_file(dfd, name, identity):
                    raise EscrowRefused(
                        "TARGET_SWAPPED",
                        f"{parent_abs / name} no longer names the artifact this run created (the "
                        "name was swapped before the report)",
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
        "decrypt_verified": False,
        "out": str(parent_abs / name),
        "ciphertext_sha256": ciphertext_sha256,
        "recipients": [recipient],
        "proved_on_host": [
            "the key line was exactly one line of 64 lowercase hex characters",
            "age exited 0",
            "the artifact is a non-empty regular file that age created through the held "
            "directory descriptor, held open since, and still named by --out",
        ],
        "not_proved": (
            "that the artifact decrypts to the key: its one recipient is your recovery key, "
            "whose identity never exists on this host"
        ),
        "next": [
            f"Copy {parent_abs / name} off this host and, there, decrypt it with your recovery "
            f"identity and compare it with the .env value ({RUNBOOK}, step 6). Delete no copy "
            "of it before that prints MATCH.",
            f"Clear the terminal. The key was never printed; this report holds none of it "
            f"({RUNBOOK}).",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.operations.persistence_key_escrow",
        description=(
            "Escrow one age-encrypted copy of the persistence key, typed into a non-echoing "
            "prompt, to the owner's recovery key alone; the decrypt-and-compare is the owner's, "
            "off-host."
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

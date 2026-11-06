"""Rotate reviewed matrix fingerprints from committed parent baselines only.

The operator fetches origin first, then pins the reviewed record:
    python scripts/rotate_baseline_fingerprints.py \\
        --reviewed "$(git rev-parse origin/main)" --parent HEAD --check
For a merge, supply two --parent arguments. Inventory sources must be indexed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Never

from detect_secrets.core.scan import scan_line
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import transient_settings

MATRIX = "docs/generated/capability-matrix.json"
BASELINE = ".secrets.baseline"
HEX_TYPE = "Hex High Entropy String"
REASON = "SHA-256 source-inventory identity generated from repository bytes"
Identity = tuple[str, str, str]


class RotationError(ValueError):
    """A rotation cannot be proven safe; no file has been written."""


def render(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2) + "\n").encode()


def identity(path: str, entry: dict[str, Any]) -> Identity:
    digest = entry.get("hashed_secret")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{40}", digest) is None:
        raise RotationError(f"invalid fingerprint entry at {path!r}")
    if entry.get("is_secret") is not False or "secret_value" in entry:
        raise RotationError(f"unreviewed entry at {path!r} hash={digest[:8]}")
    kind = entry.get("type")
    if not isinstance(kind, str):
        raise RotationError(f"invalid entry type at {path!r} hash={digest[:8]}")
    return path, kind, digest


def entries(payload: dict[str, Any], group: str) -> dict[Identity, dict[str, Any]]:
    result: dict[Identity, dict[str, Any]] = {}
    try:
        for path, values in payload[group].items():
            for entry in values:
                key = identity(path, entry)
                if key in result:
                    raise RotationError(f"duplicate identity at {path!r} hash={key[2][:8]}")
                result[key] = entry
    except (KeyError, AttributeError, TypeError) as error:
        raise RotationError(f"invalid committed baseline {group}") from error
    return result


def fingerprint(value: str) -> str:
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()


def inventory(raw: bytes) -> dict[str, tuple[str, int, str]]:
    """Map detector hashes to source path, physical line and source SHA-256."""
    try:
        text = raw.decode()
        rows = json.loads(text)["source_fingerprints"]
        lines = text.splitlines()
        start = next(n for n, line in enumerate(lines) if line == '  "source_fingerprints": [')
        end = next(n for n in range(start + 1, len(lines)) if lines[n] in ("  ],", "  ]"))
        locations = [
            (n + 1, re.fullmatch(r'      "sha256": "([0-9a-f]{64})"', lines[n]))
            for n in range(start + 1, end)
        ]
        values = [(n, match.group(1)) for n, match in locations if match is not None]
        if len(values) != len(rows) or not rows:
            raise RotationError("matrix source-inventory layout is not recognized")
        result: dict[str, tuple[str, int, str]] = {}
        for row, (line, value) in zip(rows, values, strict=True):
            if set(row) != {"path", "sha256"} or row["sha256"] != value:
                raise RotationError("matrix source-inventory entry is not recognized")
            path = row["path"]
            if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
                raise RotationError("matrix source path is unsafe")
            result.setdefault(fingerprint(value), (path, line, value))
        allowed_lines = {n for n, _ in values}
        for n, text_line in enumerate(lines, 1):
            if n not in allowed_lines and re.search(
                r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])", text_line
            ):
                raise RotationError(f"outside matrix fingerprints: {MATRIX}:{n}")
        return result
    except (UnicodeError, ValueError, KeyError, TypeError, StopIteration) as error:
        if isinstance(error, RotationError):
            raise
        raise RotationError("matrix source inventory is unreadable") from error


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._matrices: dict[str, dict[str, tuple[str, int, str]]] = {}

    def git(self, *arguments: str, failure: str = "git evidence unavailable") -> bytes:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.commitGraph=false",
                "--no-replace-objects",
                "-C",
                str(self.root),
                *arguments,
            ],
            capture_output=True,
            timeout=60,
            env={**os.environ, "GIT_GRAFT_FILE": os.devnull},
        )
        if result.returncode:
            raise RotationError(failure)
        return result.stdout

    def matrix(self, commit: str) -> dict[str, tuple[str, int, str]]:
        if commit not in self._matrices:
            self._matrices[commit] = inventory(
                self.git(
                    "show",
                    f"{commit}:{MATRIX}",
                    failure="unfindable value: committed matrix missing",
                )
            )
        return self._matrices[commit]

    def commit(self, ref: str, *, failure: str = "baseline commit unavailable") -> str:
        return (
            self.git(
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{ref}^{{commit}}",
                failure=failure,
            )
            .decode()
            .strip()
        )

    def is_ancestor(self, older: str, newer: str) -> bool:
        try:
            self.git("merge-base", "--is-ancestor", older, newer)
        except RotationError:
            return False
        return True

    def baseline(self, ref: str) -> tuple[str, dict[str, Any]]:
        commit = self.commit(ref)
        raw = self.git("show", f"{commit}:{BASELINE}")
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise RotationError("committed baseline is unreadable") from error
        if render(payload) != raw:
            raise RotationError("committed baseline fails writer round-trip")
        entries(payload, "results")
        entries(payload, "history_results")
        return commit, payload

    def prove_new(
        self, key: Identity, parent: str, observed: str, *, reviewed: bool = False
    ) -> str:
        """Find a matrix observation; only unreviewed identities need source-byte proof."""
        label = f"{key[0]!r} hash={key[2][:8]}"
        if key[0] != MATRIX or key[1] != HEX_TYPE:
            raise RotationError(f"new non-matrix identity {label}")
        if re.fullmatch(r"[0-9a-f]{40}", observed) is None:
            raise RotationError(f"unavailable commit for {label}")
        self.git(
            "merge-base",
            "--is-ancestor",
            observed,
            parent,
            failure=f"unavailable commit for {label}",
        )
        candidates = [observed, *self.git("rev-list", parent, "--", MATRIX).decode().splitlines()]
        unproved_source: str | None = None
        for candidate in dict.fromkeys(candidates):
            try:
                values = self.matrix(candidate)
            except RotationError:
                continue
            if key[2] not in values:
                continue
            if reviewed:
                return candidate
            source, _, value = values[key[2]]
            if Path(source).name == ".env":
                raise RotationError(f"unsafe committed source for {label}")
            source_commits = [
                candidate,
                *self.git("rev-list", parent, "--", source).decode().splitlines(),
            ]
            for source_commit in dict.fromkeys(source_commits):
                try:
                    raw = self.git("show", f"{source_commit}:{source}")
                except RotationError:
                    continue
                if hashlib.sha256(raw).hexdigest() == value:
                    return candidate
            unproved_source = source
        if unproved_source is not None:
            raise RotationError(
                f"value not proved from committed source {unproved_source!r}: {label}"
            )
        raise RotationError(f"unfindable value for {label}")

    def note_carried(self, key: Identity, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        """Observation only: the reviewed record remains authoritative and unchanged."""
        observed = entry.get("observed_commit", "")
        found = False
        try:
            if not isinstance(observed, str) or re.fullmatch(r"[0-9a-f]{40}", observed) is None:
                raise RotationError("carried observation unavailable")
            if Path(key[0]).name == ".env":
                raise RotationError("carried observation unavailable")
            if key[0] == MATRIX:
                found = key[2] in self.matrix(observed)
            else:
                raw = self.git("show", f"{observed}:{key[0]}").decode()
                with (
                    transient_settings(scanner_settings(payload)),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    found = any(
                        secret.type == key[1] and secret.secret_hash == key[2]
                        for line in raw.splitlines()
                        for secret in scan_line(line)
                    )
        except (RotationError, UnicodeError):
            pass
        if not found:
            short_commit = observed[:8] if isinstance(observed, str) else "unknown"
            print(
                f"NOTE carried {key[0]!r} hash={key[2][:8]} "
                f"observed={short_commit} lacks value; unchanged"
            )


def scanner_settings(payload: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(payload)
    config["filters_used"] = [
        f
        for f in config["filters_used"]
        if f["path"] != "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
    ]
    return config


def scan(root: Path, payload: dict[str, Any]) -> dict[Identity, int]:
    """Read-only detection; no baseline writer or credential verification/network calls."""
    paths = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            timeout=60,
        )
        .decode()
        .split("\0")
    )
    config = scanner_settings(payload)
    collection = SecretsCollection(root=str(root))
    with transient_settings(config), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        for path in paths:
            if not path or path == BASELINE:
                continue
            if Path(path).name == ".env" or (root / path).is_symlink():
                raise RotationError(f"refusing unsafe scan path {path!r}")
            collection.scan_file(path)
    return {
        (path, secret.type, secret.secret_hash): secret.line_number for path, secret in collection
    }


def plan(root: Path, refs: Sequence[str], reviewed_ref: str) -> bytes:
    repository = Repository(root)
    if re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_ref) is None:
        raise RotationError("--reviewed requires a full 40-hex SHA")
    reviewed_ref = reviewed_ref.lower()
    origin_main = repository.commit(
        "refs/remotes/origin/main", failure="origin/main is absent or unavailable"
    )
    if not repository.is_ancestor(reviewed_ref, origin_main):
        raise RotationError("reviewed commit is not an ancestor of origin/main")
    parent_commits = [repository.commit(ref) for ref in refs]
    if not any(repository.is_ancestor(reviewed_ref, parent) for parent in parent_commits):
        raise RotationError("reviewed commit is not an ancestor of any parent")
    reviewed_commit, reviewed = repository.baseline(reviewed_ref)
    reviewed_current = entries(reviewed, "results")
    reviewed_history = entries(reviewed, "history_results")
    trusted = reviewed_current.keys() | reviewed_history.keys()
    for key, entry in reviewed_history.items():
        repository.note_carried(key, entry, reviewed)
    parents: list[tuple[str, dict[str, Any], dict[str, tuple[str, int, str]]]] = []
    new_history: dict[tuple[str, Identity], dict[str, Any]] = {}
    for ref in parent_commits:
        commit, payload = repository.baseline(ref)
        current = entries(payload, "results")
        history = entries(payload, "history_results")
        for key, entry in history.items():
            if key in reviewed_history:
                continue
            observed = entry.get("observed_commit", "")
            if not isinstance(observed, str):
                raise RotationError(f"unavailable commit at {key[0]!r} hash={key[2][:8]}")
            proven = repository.prove_new(key, commit, observed, reviewed=key in trusted)
            new_history[commit, key] = {**entry, "observed_commit": proven}
        for key in current:
            if key not in trusted and (key[0] != MATRIX or key[1] != HEX_TYPE):
                repository.prove_new(key, commit, commit)
        parents.append((commit, payload, repository.matrix(commit)))

    # Timestamps are provenance, not scanner policy: retain the newest committed timestamp.
    parents.sort(key=lambda parent: parent[1]["generated_at"], reverse=True)
    anchor = parents[0][1]
    policy = {
        k: v for k, v in anchor.items() if k not in ("results", "history_results", "generated_at")
    }
    for _, payload, _ in parents[1:]:
        if {
            k: v
            for k, v in payload.items()
            if k not in ("results", "history_results", "generated_at")
        } != policy:
            raise RotationError("outside matrix fingerprints: parent scanner policy differs")
    if {
        k: v for k, v in reviewed.items() if k not in ("results", "history_results", "generated_at")
    } != policy:
        raise RotationError("outside matrix fingerprints: reviewed scanner policy differs")

    path = root / MATRIX
    raw_matrix = path.read_bytes()
    if re.search(rb"(?m)^(<<<<<<<|=======|>>>>>>>)", raw_matrix):
        raise RotationError("matrix conflict markers")
    current_inventory = inventory(raw_matrix)
    indexed = set(repository.git("ls-files", "--cached", "-z").decode().split("\0"))
    for source_name, _, value in current_inventory.values():
        if source_name not in indexed:
            raise RotationError(f"inventory source is not indexed: {source_name!r}")
        source_path = root / source_name
        if source_path.name == ".env" or source_path.is_symlink():
            raise RotationError(f"unsafe inventory source {source_name!r}")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != value:
            raise RotationError(
                f"inventory not explained by source {source_name!r} hash={fingerprint(value)[:8]}"
            )
    generator = subprocess.run(
        [sys.executable, str(root / "scripts/build_current_state.py"), "--check"],
        cwd=root,
        capture_output=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": f"{root / 'src'}:{root}"},
    )
    if generator.returncode:
        raise RotationError("matrix regeneration check refused the working artifacts")

    def outside(
        payload: dict[str, Any], source: dict[str, tuple[str, int, str]]
    ) -> dict[Identity, dict[str, Any]]:
        return {
            key: reviewed_current.get(key, entry)
            for key, entry in entries(payload, "results").items()
            if not (key[0] == MATRIX and key[1] == HEX_TYPE and key[2] in source)
        }

    expected_outside = outside(reviewed, repository.matrix(reviewed_commit))
    for _, payload, source in parents:
        if outside(payload, source) != expected_outside:
            raise RotationError("outside matrix fingerprints: parent results differ")
    findings = scan(root, anchor)
    expected = {key: entry["line_number"] for key, entry in expected_outside.items()}
    expected.update(
        {(MATRIX, HEX_TYPE, digest): value[1] for digest, value in current_inventory.items()}
    )
    if findings != expected:
        differences = sorted(findings.keys() ^ expected.keys()) or sorted(
            key for key in findings if findings[key] != expected[key]
        )
        key = differences[0]
        raise RotationError(
            f"outside matrix fingerprints: {key[0]!r} type={key[1]} hash={key[2][:8]}"
        )

    result = copy.deepcopy(anchor)
    result["results"] = copy.deepcopy(reviewed["results"])
    matrix_results = []
    for digest, data in current_inventory.items():
        key = MATRIX, HEX_TYPE, digest
        if key in reviewed_current and reviewed_current[key]["line_number"] != data[1]:
            raise RotationError(f"carried result line changed at {MATRIX!r} hash={digest[:8]}")
        matrix_results.append(
            reviewed_current.get(
                key,
                {
                    "type": HEX_TYPE,
                    "filename": MATRIX,
                    "hashed_secret": digest,
                    "is_verified": False,
                    "line_number": data[1],
                    "is_secret": False,
                },
            )
        )
    result["results"][MATRIX] = matrix_results + [
        entry for key, entry in expected_outside.items() if key[0] == MATRIX
    ]
    history_union = dict(reviewed_history)
    for commit, payload, source in parents:
        for key in entries(payload, "history_results"):
            if key not in reviewed_history:
                history_union.setdefault(key, new_history[commit, key])
        for key in entries(payload, "results"):
            if (
                key[0] == MATRIX
                and key[1] == HEX_TYPE
                and key[2] in source
                and key[2] not in current_inventory
                and key not in history_union
            ):
                history_union.setdefault(
                    key,
                    {
                        "type": HEX_TYPE,
                        "hashed_secret": key[2],
                        "is_secret": False,
                        "observed_commit": repository.prove_new(
                            key, commit, commit, reviewed=key in trusted
                        ),
                        "reason": REASON,
                    },
                )
    active = set(entries(result, "results"))
    result["history_results"] = {}
    for key, entry in history_union.items():
        if key in active and key not in reviewed_history:
            continue
        result["history_results"].setdefault(key[0], []).append(entry)
    return render(result)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise RotationError(
            "arguments require --reviewed REF, one or two --parent REF, and optional --check"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate before atomic replacement; crash durability after replace is not guaranteed."""
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--parent", action="append", required=True)
        parser.add_argument("--reviewed", required=True)
        parser.add_argument("--check", action="store_true")
        args = parser.parse_args(argv)
        if len(args.parent) not in (1, 2):
            raise RotationError("one or two parents required")
        root = Path(Repository(Path.cwd()).git("rev-parse", "--show-toplevel").decode().strip())
        candidate = plan(root, args.parent, args.reviewed)
        target = root / BASELINE
        if target.is_symlink():
            raise RotationError("baseline target is a symlink")
        count = sum(len(values) for values in json.loads(candidate)["history_results"].values())
        print(f"history count: {count}")
        print(
            f"# {count} at ROTATE: union committed reviews and retire "
            "generated source-inventory fingerprints."
        )
        changed = not target.exists() or target.read_bytes() != candidate
        if args.check:
            return 1 if changed else 0
        if changed:
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=root, prefix=".baseline-rotate-", delete=False
                ) as output:
                    temporary = Path(output.name)
                    output.write(candidate)
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return 0
    except (RotationError, OSError, ValueError, subprocess.SubprocessError) as error:
        # Do not echo subprocess stderr, JSON content or candidate values.
        message = str(error) if isinstance(error, RotationError) else "input or subprocess failure"
        print(f"rotation refused: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

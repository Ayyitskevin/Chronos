"""Run the fail-closed release dependency, static, and secret scans.

This gate does not compare ``pyproject.toml`` to the hash locks. Manifest/lock coherence is the
release-artifact gate's check (``scripts/verify_release_artifact.py``); ``make gates`` runs both.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

from detect_secrets.core import baseline as detect_secrets_baseline
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import get_settings
from unidiff.errors import UnidiffParseError

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOL_VERSIONS = {
    "bandit": "1.9.4",
    "detect-secrets": "1.5.0",
    "pip": "26.2.1",
    "pip-audit": "2.10.1",
    "unidiff": "1.0.0",
}
GIT_HISTORY_DIFF_LIMIT_BYTES = 128 * 1024 * 1024
_HISTORY_RESULT_KEYS = {
    "hashed_secret",
    "is_secret",
    "observed_commit",
    "reason",
    "type",
}
_LOWER_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")


class SecurityGateError(RuntimeError):
    """A release security invariant could not be established."""


@dataclass(frozen=True, slots=True)
class ScanCommand:
    """One named scanner invocation whose nonzero exit blocks the gate."""

    name: str
    argv: tuple[str, ...]


VersionGetter = Callable[[str], str]
TrackedFilesLoader = Callable[[Path], tuple[str, ...]]
CommandRunner = Callable[[ScanCommand, Path], None]
HistoryScanner = Callable[[Path, Path], None]


def build_scan_commands(
    *,
    python: Path,
    baseline: Path,
    tracked_files: tuple[str, ...],
) -> tuple[ScanCommand, ...]:
    """Bind each scanner to the exact release input and accepted threshold."""

    executable = str(python)
    return (
        ScanCommand(
            name="runtime dependency audit",
            argv=(
                executable,
                "-m",
                "pip_audit",
                "--require-hashes",
                "--disable-pip",
                "--progress-spinner",
                "off",
                "-r",
                "requirements-runtime.lock",
            ),
        ),
        ScanCommand(
            name="Python static analysis",
            argv=(
                executable,
                "-m",
                "bandit",
                "-r",
                "src/chronos",
                "worker",
                "scripts",
                "--severity-level",
                "medium",
                "--confidence-level",
                "medium",
                "--quiet",
            ),
        ),
        ScanCommand(
            name="tracked-file secret scan",
            argv=(
                executable,
                "-m",
                "detect_secrets.pre_commit_hook",
                "--baseline",
                str(baseline),
                "--json",
                "--",
                *tracked_files,
            ),
        ),
    )


def _tracked_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ("git", "-C", str(root), "ls-files", "-z"),
        check=True,
        capture_output=True,
    )
    return tuple(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)


def _run_command(command: ScanCommand, root: Path) -> None:
    print(f"running {command.name}")
    subprocess.run(command.argv, cwd=root, check=True)


def _git_output(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", "--no-replace-objects", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SecurityGateError(
            "Git history preflight could not establish repository state"
        ) from error
    return result.stdout


def _require_complete_supported_history(root: Path) -> Path:
    if _git_output(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise SecurityGateError("Git history secret scan refuses a shallow repository")
    _git_output(root, "rev-parse", "--verify", "HEAD^{commit}")
    if _git_output(root, "rev-list", "--min-parents=3", "--max-count=1", "HEAD").strip():
        raise SecurityGateError(
            "Git history secret scan refuses octopus merges; remerge coverage is two-parent only"
        )
    raw_path = (
        _git_output(
            root,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        )
        .decode("utf-8", errors="strict")
        .strip()
    )
    object_directory = Path(raw_path)
    if not object_directory.is_dir():
        raise SecurityGateError("Git object directory is unavailable")
    return object_directory


def _bounded_history_diff(
    root: Path,
    *,
    object_directory: Path,
    scratch_directory: Path,
    max_diff_bytes: int,
) -> str:
    if max_diff_bytes <= 0:
        raise SecurityGateError("Git history patch limit must be positive")
    scratch_objects = scratch_directory / "objects"
    scratch_objects.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(object_directory),
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OBJECT_DIRECTORY": str(scratch_objects),
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    command = (
        "git",
        "--no-replace-objects",
        "-c",
        "core.quotePath=false",
        "-C",
        str(root),
        "log",
        "--format=",
        "--root",
        "--patch",
        "--text",
        "--full-history",
        "--diff-merges=remerge",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--unified=0",
        "HEAD",
    )
    chunks: list[bytes] = []
    observed = 0
    try:
        with subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            assert process.stdout is not None
            while chunk := process.stdout.read(1024 * 1024):
                observed += len(chunk)
                if observed > max_diff_bytes:
                    process.kill()
                    process.wait()
                    raise SecurityGateError(
                        f"Git history patch exceeded the {max_diff_bytes}-byte safety limit"
                    )
                chunks.append(chunk)
            return_code = process.wait()
    except OSError as error:
        raise SecurityGateError("Git history traversal could not start") from error
    if return_code != 0:
        raise SecurityGateError(f"Git history traversal failed with exit code {return_code}")
    return b"".join(chunks).decode("utf-8", errors="replace")


def _require_false_positive_results(
    results: object,
    *,
    label: str,
) -> Mapping[str, object]:
    if not isinstance(results, dict):
        raise SecurityGateError(f"{label} must be an object")
    for path, raw_entries in results.items():
        if not isinstance(path, str) or not path or not isinstance(raw_entries, list):
            raise SecurityGateError(f"{label} has an invalid path or result list")
        for entry in raw_entries:
            if not isinstance(entry, dict) or entry.get("is_secret") is not False:
                raise SecurityGateError(f"{label} contains an unreviewed result")
            if "secret_value" in entry:
                raise SecurityGateError(f"{label} must never store candidate plaintext")
    return results


def _reviewed_history(
    payload: Mapping[str, object],
) -> tuple[SecretsCollection, frozenset[str]]:
    results = _require_false_positive_results(
        payload.get("history_results"),
        label="historical secret review",
    )
    normalized: dict[str, list[dict[str, object]]] = {}
    identities: set[tuple[str, str, str]] = set()
    observed_commits: set[str] = set()
    for path, raw_entries in results.items():
        assert isinstance(path, str)
        assert isinstance(raw_entries, list)
        entries: list[dict[str, object]] = []
        for raw_entry in raw_entries:
            assert isinstance(raw_entry, dict)
            if set(raw_entry) != _HISTORY_RESULT_KEYS:
                raise SecurityGateError("historical secret review entry schema is invalid")
            secret_type = raw_entry.get("type")
            fingerprint = raw_entry.get("hashed_secret")
            observed_commit = raw_entry.get("observed_commit")
            reason = raw_entry.get("reason")
            if (
                not isinstance(secret_type, str)
                or not secret_type
                or not isinstance(fingerprint, str)
                or _LOWER_HEX_40.fullmatch(fingerprint) is None
                or not isinstance(observed_commit, str)
                or _LOWER_HEX_40.fullmatch(observed_commit) is None
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise SecurityGateError("historical secret review entry values are invalid")
            identity = (path, secret_type, fingerprint)
            if identity in identities:
                raise SecurityGateError("historical secret review contains a duplicate identity")
            identities.add(identity)
            observed_commits.add(observed_commit)
            entries.append(
                {
                    "type": secret_type,
                    "hashed_secret": fingerprint,
                    "is_secret": False,
                }
            )
        normalized[path] = entries
    return (
        SecretsCollection.load_from_baseline({"results": normalized}),
        frozenset(observed_commits),
    )


def _finding_summary(findings: SecretsCollection) -> str:
    rendered = [
        f"{path}:{secret.line_number} {secret.type} {secret.secret_hash}"
        for path, secret in findings
    ]
    preview = "; ".join(rendered[:10])
    suffix = "" if len(rendered) <= 10 else f"; and {len(rendered) - 10} more"
    return f"{len(rendered)} finding(s): {preview}{suffix}"


def verify_git_history_secrets(
    root: Path,
    baseline: Path,
    *,
    max_diff_bytes: int = GIT_HISTORY_DIFF_LIMIT_BYTES,
) -> None:
    """Scan every reachable addition, including two-parent merge resolutions."""

    print("running Git history secret scan")
    object_directory = _require_complete_supported_history(root)
    try:
        payload = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SecurityGateError("reviewed secret baseline is unreadable") from error
    if not isinstance(payload, dict):
        raise SecurityGateError("reviewed secret baseline must be an object")
    _require_false_positive_results(payload.get("results"), label="tracked secret baseline")
    try:
        reviewed_current = detect_secrets_baseline.load(payload, filename=str(baseline))
    except (KeyError, TypeError, ValueError) as error:
        raise SecurityGateError("reviewed secret baseline schema is invalid") from error
    # Historical paths may no longer exist in the checkout. The diff parser supplies
    # their exact added lines, so the ordinary file-existence filter would create a
    # silent history-only blind spot rather than adding safety.
    get_settings().disable_filters("detect_secrets.filters.common.is_invalid_file")
    reviewed_history, observed_commits = _reviewed_history(payload)
    for commit in observed_commits:
        try:
            _git_output(root, "merge-base", "--is-ancestor", commit, "HEAD")
        except SecurityGateError as error:
            raise SecurityGateError(
                f"historical secret review commit is not reachable from HEAD: {commit}"
            ) from error

    with TemporaryDirectory(prefix="chronos-history-objects-") as temp_dir:
        patch = _bounded_history_diff(
            root,
            object_directory=object_directory,
            scratch_directory=Path(temp_dir),
            max_diff_bytes=max_diff_bytes,
        )
    findings = SecretsCollection()
    try:
        findings.scan_diff(patch)
    except (ImportError, NotImplementedError, UnidiffParseError) as error:
        raise SecurityGateError("Git history patch could not be parsed safely") from error

    stale = reviewed_history - findings
    if stale:
        raise SecurityGateError(
            "stale historical secret review; remove or re-review " + _finding_summary(stale)
        )
    unreviewed = findings - reviewed_current - reviewed_history
    if unreviewed:
        raise SecurityGateError(
            "Git history contains unreviewed potential secrets; " + _finding_summary(unreviewed)
        )
    print("Git history secret scan passed")


def _require_exact_tool_versions(version_getter: VersionGetter) -> None:
    for distribution, expected in EXPECTED_TOOL_VERSIONS.items():
        try:
            observed = version_getter(distribution)
        except metadata.PackageNotFoundError as error:
            raise SecurityGateError(f"required scanner is not installed: {distribution}") from error
        if observed != expected:
            raise SecurityGateError(
                f"{distribution} version drift: expected {expected}, observed {observed}"
            )


def verify_release_security(
    *,
    root: Path = ROOT,
    python: Path = Path(sys.executable),
    version_getter: VersionGetter = metadata.version,
    tracked_files_loader: TrackedFilesLoader = _tracked_files,
    command_runner: CommandRunner = _run_command,
    history_scanner: HistoryScanner = verify_git_history_secrets,
) -> None:
    """Run all scanners and refuse missing tools, inputs, or stale baselines."""

    baseline = root / ".secrets.baseline"
    if not baseline.is_file():
        raise SecurityGateError("reviewed secret baseline is missing")

    _require_exact_tool_versions(version_getter)
    tracked_files = tuple(
        path for path in tracked_files_loader(root) if path != ".secrets.baseline"
    )
    if not tracked_files:
        raise SecurityGateError("no tracked files found; refusing an empty secret scan")

    baseline_bytes = baseline.read_bytes()
    with TemporaryDirectory(prefix="chronos-security-gate-") as temp_dir:
        baseline_copy = Path(temp_dir) / baseline.name
        shutil.copyfile(baseline, baseline_copy)
        for command in build_scan_commands(
            python=python,
            baseline=baseline_copy,
            tracked_files=tracked_files,
        ):
            command_runner(command, root)
        history_scanner(root, baseline_copy)

        if baseline_copy.read_bytes() != baseline_bytes:
            raise SecurityGateError(
                "secret baseline is stale; regenerate and review it instead of "
                "mutating the checkout"
            )


def main() -> int:
    try:
        verify_release_security()
    except subprocess.CalledProcessError as error:
        print(f"release security gate failed with exit code {error.returncode}", file=sys.stderr)
        return 1
    except SecurityGateError as error:
        print(f"release security gate failed: {error}", file=sys.stderr)
        return 1

    versions = ", ".join(
        f"{name} {version}" for name, version in sorted(EXPECTED_TOOL_VERSIONS.items())
    )
    print(
        f"release security gate passed ({versions}); manifest/lock coherence is the "
        "release-artifact gate's check (scripts/verify_release_artifact.py), not this one's"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

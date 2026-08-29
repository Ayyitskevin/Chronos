"""Run the fail-closed release dependency, static, and secret scans."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOL_VERSIONS = {
    "bandit": "1.9.4",
    "detect-secrets": "1.5.0",
    "pip-audit": "2.10.1",
}


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
    print(f"release security gate passed ({versions})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

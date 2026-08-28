"""Build and verify the installed Chronos wheel outside the source checkout.

The ordinary test suite runs against an editable install. This gate builds from
the current tracked/untracked source set, installs the wheel into a fresh venv,
and checks the runtime surfaces that an editable checkout can accidentally hide.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.resources
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_ENTRY_POINT: Final[tuple[str, str]] = ("chronos", "chronos.app:main")
_MIGRATION_SUPPORT: Final[tuple[str, ...]] = ("env.py", "script.py.mako")
_MIGRATION_HEAD: Final[str] = "0010"


class ReleaseArtifactError(RuntimeError):
    """The built wheel does not satisfy the release-artifact contract."""


def _terminal_assets(source_root: Path) -> tuple[str, ...]:
    """Discover every non-hidden terminal asset the current source expects to ship."""

    static_root = source_root / "src/chronos/terminal/static"
    assets: list[str] = []
    for path in sorted(static_root.rglob("*")):
        relative = path.relative_to(static_root)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            assets.append(relative.as_posix())
    if not assets:
        raise ReleaseArtifactError("source tree contains no terminal static assets")
    return tuple(assets)


def _module_entrypoints(source_root: Path) -> tuple[str, ...]:
    """Discover every packaged command surface declared by ``__main__.py``."""

    package_root = source_root / "src/chronos"
    modules = tuple(
        ".".join(("chronos", *path.parent.relative_to(package_root).parts))
        for path in sorted(package_root.rglob("__main__.py"))
    )
    if not modules:
        raise ReleaseArtifactError("source tree contains no module entry points")
    return modules


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one visible, fail-fast gate command with source import paths removed."""

    print(f"+ {shlex.join(command)}", flush=True)
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
    )


def _copy_source(destination: Path) -> None:
    """Copy exactly the current non-ignored source set into an isolated build tree."""

    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=_REPO_ROOT,
    )
    relative_paths = sorted(Path(raw.decode()) for raw in listed.split(b"\0") if raw)
    if not relative_paths:
        raise ReleaseArtifactError("git returned no source files to build")
    for relative in relative_paths:
        source = _REPO_ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _artifact_members(source_root: Path) -> dict[str, Path]:
    """Wheel member names and the source bytes they must contain."""

    static_root = source_root / "src/chronos/terminal/static"
    members = {
        f"chronos/terminal/static/{name}": static_root / name
        for name in _terminal_assets(source_root)
    }
    migration_root = source_root / "src/chronos/persistence/migrations"
    members.update(
        {
            f"chronos/persistence/migrations/{name}": migration_root / name
            for name in _MIGRATION_SUPPORT
        }
    )
    version_files = sorted((migration_root / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
    if not version_files:
        raise ReleaseArtifactError("source tree contains no migration revisions")
    members.update(
        {f"chronos/persistence/migrations/versions/{path.name}": path for path in version_files}
    )
    return members


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_archive(wheel: Path, source_root: Path) -> None:
    """Prove required artifact members exist and equal the source bytes."""

    expected = _artifact_members(source_root)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(set(expected) - names)
        if missing:
            raise ReleaseArtifactError(f"wheel is missing required members: {missing}")
        mismatched = [
            name
            for name, source in expected.items()
            if _digest(archive.read(name)) != _digest(source.read_bytes())
        ]
        if mismatched:
            raise ReleaseArtifactError(
                f"wheel members differ from their source bytes: {sorted(mismatched)}"
            )


def _installed_smoke() -> None:
    """Verify public installed surfaces from the clean artifact environment."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import chronos

    package_path = Path(chronos.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    if environment_root not in package_path.parents:
        raise ReleaseArtifactError(
            f"chronos imported from {package_path}, outside clean environment {environment_root}"
        )

    distribution = importlib.metadata.distribution("chronos")
    entry_points = [
        item
        for item in distribution.entry_points
        if item.group == "console_scripts" and item.name == _ENTRY_POINT[0]
    ]
    actual_entry_points = [(item.name, item.value) for item in entry_points]
    if actual_entry_points != [_ENTRY_POINT]:
        raise ReleaseArtifactError(
            f"chronos console entry point drifted: expected {[_ENTRY_POINT]}, "
            f"got {actual_entry_points}"
        )
    if not callable(entry_points[0].load()):
        raise ReleaseArtifactError("chronos console entry point does not load a callable")

    static_assets = _terminal_assets(_REPO_ROOT)
    static_root = importlib.resources.files("chronos.terminal").joinpath("static")
    for name in static_assets:
        resource = static_root.joinpath(*Path(name).parts)
        if not resource.is_file() or not resource.read_bytes():
            raise ReleaseArtifactError(f"installed terminal asset is absent or empty: {name}")

    migration_root = importlib.resources.files("chronos.persistence.migrations")
    for name in _MIGRATION_SUPPORT:
        if not migration_root.joinpath(name).is_file():
            raise ReleaseArtifactError(f"installed migration support file is absent: {name}")
    # Alembic requires a filesystem path. Python 3.12's as_file() supports
    # Traversable directories, including package resources extracted from a zip.
    # Source: https://docs.python.org/3.12/library/importlib.resources.html#importlib.resources.as_file
    with importlib.resources.as_file(migration_root) as migration_path:
        config = Config()
        config.set_main_option("script_location", str(migration_path))
        scripts = ScriptDirectory.from_config(config)
        heads = scripts.get_heads()
    if heads != [_MIGRATION_HEAD]:
        raise ReleaseArtifactError(
            f"installed migration chain must have head {_MIGRATION_HEAD!r}, got {heads}"
        )

    module_entrypoints = _module_entrypoints(_REPO_ROOT)
    for module in module_entrypoints:
        result = subprocess.run(
            [sys.executable, "-I", "-m", module, "--help"],
            check=False,
            cwd=environment_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or "usage:" not in result.stdout:
            raise ReleaseArtifactError(
                f"installed module entry point {module!r} failed --help: "
                f"exit={result.returncode}, stderr={result.stderr.strip()!r}"
            )

    print(
        "Installed artifact smoke passed: package origin, console entry point, "
        f"{len(static_assets)} terminal assets, migration head {_MIGRATION_HEAD}, "
        f"and {len(module_entrypoints)} module entry points."
    )


def verify_release_artifact() -> None:
    """Build, inspect, install, and smoke-test one wheel in a temporary directory."""

    with tempfile.TemporaryDirectory(prefix="chronos-release-") as raw_directory:
        work = Path(raw_directory)
        source_root = work / "source"
        wheel_directory = work / "dist"
        environment_root = work / "venv"
        _copy_source(source_root)
        wheel_directory.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment_root)
        python = environment_root / "bin/python"

        _run(
            [
                str(python),
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-deps",
                "--wheel-dir",
                str(wheel_directory),
                str(source_root),
            ],
            cwd=work,
        )
        wheels = sorted(wheel_directory.glob("chronos-*.whl"))
        if len(wheels) != 1:
            raise ReleaseArtifactError(f"expected one Chronos wheel, found {wheels}")
        wheel = wheels[0]
        _verify_archive(wheel, source_root)

        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "--require-hashes",
                "-r",
                str(source_root / "requirements-dev.lock"),
            ],
            cwd=work,
        )
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "--no-deps",
                str(wheel),
            ],
            cwd=work,
        )
        _run(
            [
                str(python),
                "-I",
                str(source_root / "scripts/verify_release_artifact.py"),
                "--installed-smoke",
            ],
            cwd=work,
        )
        print(f"Release artifact gate passed for {wheel.name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-smoke", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.installed_smoke:
        _installed_smoke()
    else:
        verify_release_artifact()


if __name__ == "__main__":
    main()

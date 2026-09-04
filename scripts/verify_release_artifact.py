"""Build and verify the installed Chronos wheel outside the source checkout.

The ordinary test suite runs against an editable install. This gate builds from
the current tracked/untracked source set twice, proves the wheel bytes reproduce,
installs the wheel into a fresh runtime venv, checks the surfaces that an editable
checkout can accidentally hide, and emits a validated CycloneDX software bill of
materials for that environment.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import venv
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_ENTRY_POINT: Final[tuple[str, str]] = ("chronos", "chronos.app:main")
_MIGRATION_SUPPORT: Final[tuple[str, ...]] = ("env.py", "script.py.mako")
_MIGRATION_HEAD: Final[str] = "0011"
_CYCLONEDX_SCHEMA: Final[str] = "http://cyclonedx.org/schema/bom-1.6.schema.json"
_SOURCE_DATE_EPOCH_MINIMUM: Final[int] = 315532800  # 1980-01-01 00:00:00 UTC
_SOURCE_DATE_EPOCH_MAXIMUM: Final[int] = 4354819199  # 2107-12-31 23:59:59 UTC
# Intentionally independent of the pytest manifest: the artifact gate must carry
# its own frozen legacy contract rather than derive it from another check.
_V2_BASELINE_TABLES: Final[frozenset[str]] = frozenset(
    {
        "application_events",
        "candidate_evaluations",
        "commissions",
        "database_scope",
        "fills",
        "guardrail_decisions",
        "order_drafts",
        "order_previews",
        "reconciliation_runs",
        "rejected_candidate_reasons",
        "schema_version",
        "strategy_basis_entries",
        "strategy_state",
        "submitted_orders",
        "wheel_cycles",
    }
)


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


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one visible, fail-fast gate command with source import paths removed."""

    print(f"+ {shlex.join(command)}", flush=True)
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    if environment_overrides is not None:
        environment.update(environment_overrides)
    return subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
    )


def _repository_source_date_epoch() -> int:
    """Return a bounded, source-derived build epoch for the exact repository HEAD."""

    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%ct", "HEAD"],
            check=False,
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ReleaseArtifactError("cannot read the repository commit timestamp") from error
    if result.returncode != 0:
        raise ReleaseArtifactError(
            f"git could not read the repository commit timestamp: exit={result.returncode}"
        )
    if re.fullmatch(rb"[0-9]+\n?", result.stdout) is None:
        raise ReleaseArtifactError("repository commit timestamp is not one decimal integer")
    epoch = int(result.stdout)
    if epoch > _SOURCE_DATE_EPOCH_MAXIMUM:
        raise ReleaseArtifactError("repository commit timestamp exceeds the ZIP timestamp range")
    return epoch


def _wheel_zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    """Normalize an epoch to setuptools' UTC wheel timestamp and ZIP precision."""

    bounded_epoch = max(source_date_epoch, _SOURCE_DATE_EPOCH_MINIMUM)
    timestamp = time.gmtime(bounded_epoch)[:6]
    return (*timestamp[:5], timestamp[5] - (timestamp[5] % 2))


def _canonical_package_name(name: str) -> str:
    """Normalize a Python distribution name using the PEP 503 spelling rule."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_requirements(path: Path) -> dict[str, str]:
    """Read exact package versions from one uv-generated hash lock."""

    requirements: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().rstrip(" \\")
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ]+)", line)
        if match is None:
            continue
        name = _canonical_package_name(match.group(1))
        if name in requirements:
            raise ReleaseArtifactError(f"duplicate locked requirement {name!r} in {path}")
        requirements[name] = match.group(2)
    if not requirements:
        raise ReleaseArtifactError(f"lock contains no exact requirements: {path}")
    return requirements


def _install_lock(python: Path, lock: Path, *, cwd: Path) -> None:
    """Install one complete hash-verified environment domain."""

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
            str(lock),
        ],
        cwd=cwd,
    )


def _bootstrap_pip(python: Path, source_root: Path, *, cwd: Path) -> None:
    """Install and independently verify the exact pip frontend before other inputs."""

    lock = source_root / "requirements-bootstrap.lock"
    verifier = source_root / "scripts/verify_pip_bootstrap.py"
    _run(
        [
            str(python),
            "-I",
            str(verifier),
            "--lock",
            str(lock),
            "--check-lock-only",
        ],
        cwd=cwd,
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
            "--require-hashes",
            "-r",
            str(lock),
        ],
        cwd=cwd,
    )
    _run(
        [
            str(python),
            "-I",
            str(verifier),
            "--lock",
            str(lock),
        ],
        cwd=cwd,
    )


def _build_wheel(
    python: Path,
    source_root: Path,
    wheel_directory: Path,
    *,
    cwd: Path,
    source_date_epoch: int,
) -> None:
    """Install the hash-locked backend, then build without isolation."""

    _install_lock(python, source_root / "requirements-build.lock", cwd=cwd)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-build-isolation",
            "--check-build-dependencies",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            str(source_root),
        ],
        cwd=cwd,
        environment_overrides={"SOURCE_DATE_EPOCH": str(source_date_epoch)},
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


def _single_wheel(wheel_directory: Path) -> Path:
    """Resolve exactly one Chronos wheel from a build output directory."""

    wheels = sorted(wheel_directory.glob("chronos-*.whl"))
    if len(wheels) != 1:
        raise ReleaseArtifactError(f"expected one Chronos wheel, found {wheels}")
    return wheels[0]


def _verify_reproducible_wheels(
    first_directory: Path,
    second_directory: Path,
    source_date_epoch: int,
) -> tuple[Path, str, tuple[int, int, int, int, int, int]]:
    """Require two isolated wheel builds to have one identity, bytes, and timestamp."""

    first = _single_wheel(first_directory)
    second = _single_wheel(second_directory)
    if first.name != second.name:
        raise ReleaseArtifactError(
            f"wheel filenames are not reproducible: {first.name!r} != {second.name!r}"
        )

    first_bytes = first.read_bytes()
    second_bytes = second.read_bytes()
    first_digest = _digest(first_bytes)
    second_digest = _digest(second_bytes)
    if first_bytes != second_bytes:
        raise ReleaseArtifactError(
            "wheel bytes are not reproducible: "
            f"first_sha256={first_digest}, second_sha256={second_digest}"
        )

    expected_timestamp = _wheel_zip_timestamp(source_date_epoch)
    with zipfile.ZipFile(first) as archive:
        members = archive.infolist()
        if not members:
            raise ReleaseArtifactError("wheel archive contains no members")
        mismatched = sorted(
            member.filename for member in members if member.date_time != expected_timestamp
        )
    if mismatched:
        raise ReleaseArtifactError(
            "wheel members do not use the source-derived ZIP timestamp "
            f"{expected_timestamp}: {mismatched}"
        )
    return first, first_digest, expected_timestamp


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


def _wheel_identity(wheel: Path) -> tuple[str, str, frozenset[str]]:
    """Read the project identity and runtime requirements from wheel metadata."""

    with zipfile.ZipFile(wheel) as archive:
        metadata_members = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_members) != 1:
            raise ReleaseArtifactError(
                f"wheel must contain exactly one distribution METADATA file: {metadata_members}"
            )
        lines = archive.read(metadata_members[0]).decode("utf-8").splitlines()

    fields: dict[str, list[str]] = {}
    for line in lines:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.setdefault(key, []).append(value.strip())
    try:
        name = fields["Name"][0]
        version = fields["Version"][0]
    except (KeyError, IndexError) as error:
        raise ReleaseArtifactError("wheel METADATA lacks Name or Version") from error

    runtime_requirements: set[str] = set()
    for requirement in fields.get("Requires-Dist", []):
        if re.search(r"(?:^|[; ])extra\s*==", requirement):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match is None:
            raise ReleaseArtifactError(f"cannot parse wheel requirement: {requirement!r}")
        runtime_requirements.add(_canonical_package_name(match.group(1)))
    return _canonical_package_name(name), version, frozenset(runtime_requirements)


def _generate_sbom(
    tool_python: Path,
    runtime_python: Path,
    source_root: Path,
    output: Path,
    *,
    cwd: Path,
) -> None:
    """Generate and schema-validate a reproducible CycloneDX runtime SBOM."""

    # Use only the upstream project's stable CLI; its Python API is explicitly private.
    # Source: https://cyclonedx-bom-tool.readthedocs.io/en/latest/usage.html
    _run(
        [
            str(tool_python),
            "-m",
            "cyclonedx_py",
            "environment",
            str(runtime_python),
            "--pyproject",
            str(source_root / "pyproject.toml"),
            "--mc-type",
            "application",
            "--spec-version",
            "1.6",
            "--output-format",
            "JSON",
            "--output-file",
            str(output),
            "--output-reproducible",
            "--validate",
        ],
        cwd=cwd,
    )


def _verify_sbom(
    sbom: Path,
    wheel: Path,
    runtime_lock: Path,
    bootstrap_lock: Path,
    sbom_tool_lock: Path,
) -> int:
    """Cross-check the generated BOM against the wheel and all runtime domains."""

    try:
        payload = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseArtifactError(f"cannot read generated SBOM {sbom}: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseArtifactError("generated SBOM root must be a JSON object")
    if (
        payload.get("$schema") != _CYCLONEDX_SCHEMA
        or payload.get("bomFormat") != "CycloneDX"
        or payload.get("specVersion") != "1.6"
        or payload.get("version") != 1
    ):
        raise ReleaseArtifactError("generated SBOM is not the required CycloneDX 1.6 document")

    project_name, project_version, direct_requirements = _wheel_identity(wheel)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ReleaseArtifactError("generated SBOM lacks metadata")
    root = metadata.get("component")
    expected_root = {
        "name": project_name,
        "version": project_version,
        "type": "application",
    }
    if not isinstance(root, dict) or any(
        root.get(key) != value for key, value in expected_root.items()
    ):
        raise ReleaseArtifactError(
            f"SBOM root component does not match the wheel application: {root!r}"
        )
    root_ref = root.get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        raise ReleaseArtifactError("SBOM root component lacks a bom-ref")

    properties = metadata.get("properties")
    if not isinstance(properties, list) or not any(
        isinstance(item, dict)
        and item.get("name") == "cdx:reproducible"
        and item.get("value") == "true"
        for item in properties
    ):
        raise ReleaseArtifactError("SBOM was not generated in reproducible mode")

    sbom_tool_version = _locked_requirements(sbom_tool_lock).get("cyclonedx-bom")
    tool_data = metadata.get("tools")
    tool_components = tool_data.get("components") if isinstance(tool_data, dict) else None
    if not isinstance(tool_components, list) or not any(
        isinstance(item, dict)
        and item.get("name") == "cyclonedx-py"
        and item.get("version") == sbom_tool_version
        for item in tool_components
    ):
        raise ReleaseArtifactError(
            f"SBOM generator metadata does not match cyclonedx-bom=={sbom_tool_version}"
        )

    raw_components = payload.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ReleaseArtifactError("generated SBOM contains no runtime components")
    components: dict[str, tuple[str, str]] = {}
    refs_to_names: dict[str, str] = {}
    for item in raw_components:
        if not isinstance(item, dict):
            raise ReleaseArtifactError("SBOM component must be an object")
        name = item.get("name")
        version = item.get("version")
        bom_ref = item.get("bom-ref")
        if not all(isinstance(value, str) and value for value in (name, version, bom_ref)):
            raise ReleaseArtifactError(f"SBOM component lacks name, version, or bom-ref: {item!r}")
        canonical_name = _canonical_package_name(name)
        if canonical_name in components or bom_ref in refs_to_names:
            raise ReleaseArtifactError(f"duplicate SBOM component: {name!r}")
        components[canonical_name] = (version, bom_ref)
        refs_to_names[bom_ref] = canonical_name

    if project_name in components:
        raise ReleaseArtifactError("wheel application is duplicated in SBOM components")
    runtime_requirements = _locked_requirements(runtime_lock)
    bootstrap_requirements = _locked_requirements(bootstrap_lock)
    if set(bootstrap_requirements) != {"pip"}:
        raise ReleaseArtifactError(
            "bootstrap lock must contain exactly the pip frontend for SBOM validation"
        )
    expected_components = runtime_requirements | bootstrap_requirements
    missing = sorted(set(expected_components) - set(components))
    if missing:
        raise ReleaseArtifactError(
            f"SBOM is missing locked runtime/bootstrap components: {', '.join(missing)}"
        )
    mismatched = sorted(
        name for name, version in expected_components.items() if components[name][0] != version
    )
    if mismatched:
        raise ReleaseArtifactError(
            "SBOM component versions differ from the runtime/bootstrap locks: "
            f"{', '.join(mismatched)}"
        )
    unlocked = sorted(set(components) - set(expected_components))
    if unlocked:
        raise ReleaseArtifactError(f"SBOM contains unlocked components: {', '.join(unlocked)}")

    raw_dependencies = payload.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise ReleaseArtifactError("generated SBOM lacks a dependency graph")
    dependency_entries: dict[str, list[str]] = {}
    valid_refs = set(refs_to_names) | {root_ref}
    for item in raw_dependencies:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise ReleaseArtifactError(f"invalid SBOM dependency entry: {item!r}")
        ref = item["ref"]
        depends_on = item.get("dependsOn", [])
        if (
            ref in dependency_entries
            or not isinstance(depends_on, list)
            or not all(isinstance(value, str) for value in depends_on)
        ):
            raise ReleaseArtifactError(f"invalid or duplicate SBOM dependency entry: {item!r}")
        unknown_refs = sorted(({ref} | set(depends_on)) - valid_refs)
        if unknown_refs:
            raise ReleaseArtifactError(
                f"SBOM dependency graph contains unknown refs: {', '.join(unknown_refs)}"
            )
        dependency_entries[ref] = depends_on
    missing_entries = sorted(valid_refs - set(dependency_entries))
    if missing_entries:
        raise ReleaseArtifactError(
            f"SBOM dependency graph lacks component entries: {', '.join(missing_entries)}"
        )
    if root_ref in dependency_entries[root_ref]:
        raise ReleaseArtifactError(
            "SBOM root dependency edge contains an application self-reference"
        )
    root_dependencies = {
        refs_to_names[ref] for ref in dependency_entries[root_ref] if ref in refs_to_names
    }
    if root_dependencies != direct_requirements:
        raise ReleaseArtifactError(
            "SBOM root dependency edge differs from wheel runtime requirements: "
            f"expected {sorted(direct_requirements)}, got {sorted(root_dependencies)}"
        )
    return len(components)


def _exercise_migration_tree(migration_root: Path, database_path: Path) -> int:
    """Upgrade a disposable v2 database with the selected migration tree."""

    from datetime import UTC, datetime

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from chronos.persistence.database import Database
    from chronos.persistence.schema import Base

    missing_baseline_tables = _V2_BASELINE_TABLES - set(Base.metadata.tables)
    if missing_baseline_tables:
        raise ReleaseArtifactError(
            f"installed schema is missing v2 baseline tables: {sorted(missing_baseline_tables)}"
        )

    database_url = f"sqlite:///{database_path.resolve()}"
    engine = sa.create_engine(database_url)
    try:
        Base.metadata.create_all(
            engine,
            tables=[Base.metadata.tables[name] for name in sorted(_V2_BASELINE_TABLES)],
        )
        with engine.begin() as connection:
            connection.execute(
                Base.metadata.tables["schema_version"]
                .insert()
                .values(
                    version=2,
                    applied_at=datetime.now(tz=UTC),
                )
            )
    finally:
        engine.dispose()

    config = Config()
    config.set_main_option("script_location", str(migration_root))
    config.set_main_option("sqlalchemy.url", database_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.stamp(config, "0001")
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    database = Database(database_url)
    try:
        database.initialize()
        inspector = sa.inspect(database.engine)
        model_tables = set(inspector.get_table_names()) - {"alembic_version"}
        with database.engine.connect() as connection:
            applied_heads = set(
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
            )
    finally:
        database.dispose()

    if applied_heads != {_MIGRATION_HEAD}:
        raise ReleaseArtifactError(
            f"installed migration upgrade ended at {sorted(applied_heads)}, "
            f"expected {_MIGRATION_HEAD!r}"
        )
    return len(model_tables)


def _exercise_installed_migrations(database_path: Path) -> int:
    """Resolve and execute only the installed package's migration resource."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

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
        heads = ScriptDirectory.from_config(config).get_heads()
        if heads != [_MIGRATION_HEAD]:
            raise ReleaseArtifactError(
                f"installed migration chain must have head {_MIGRATION_HEAD!r}, got {heads}"
            )
        return _exercise_migration_tree(migration_path, database_path)


def _installed_smoke() -> None:
    """Verify public installed surfaces from the clean artifact environment."""

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

    with tempfile.TemporaryDirectory(prefix="chronos-installed-migration-") as raw_directory:
        migration_table_count = _exercise_installed_migrations(
            Path(raw_directory) / "chronos-v2-upgrade.db"
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
        f"{len(static_assets)} terminal assets, migration head {_MIGRATION_HEAD} "
        f"after a v2 upgrade across {migration_table_count} model tables, "
        f"and {len(module_entrypoints)} module entry points."
    )


def verify_release_artifact(output_directory: Path) -> None:
    """Rebuild, inspect, install, inventory, and publish one verified release pair."""

    with tempfile.TemporaryDirectory(prefix="chronos-release-") as raw_directory:
        work = Path(raw_directory)
        source_root = work / "source-a"
        second_source_root = work / "source-b"
        wheel_directory = work / "dist-a"
        second_wheel_directory = work / "dist-b"
        builder_root = work / "builder-venv"
        runtime_root = work / "runtime-venv"
        _copy_source(source_root)
        _copy_source(second_source_root)
        wheel_directory.mkdir()
        second_wheel_directory.mkdir()
        venv.EnvBuilder(with_pip=True).create(builder_root)
        venv.EnvBuilder(with_pip=True).create(runtime_root)
        builder_python = builder_root / "bin/python"
        runtime_python = runtime_root / "bin/python"
        source_date_epoch = _repository_source_date_epoch()

        _bootstrap_pip(builder_python, source_root, cwd=work)
        _bootstrap_pip(runtime_python, source_root, cwd=work)

        _build_wheel(
            builder_python,
            source_root,
            wheel_directory,
            cwd=work,
            source_date_epoch=source_date_epoch,
        )
        _build_wheel(
            builder_python,
            second_source_root,
            second_wheel_directory,
            cwd=work,
            source_date_epoch=source_date_epoch,
        )
        wheel, wheel_digest, wheel_timestamp = _verify_reproducible_wheels(
            wheel_directory,
            second_wheel_directory,
            source_date_epoch,
        )
        _verify_archive(wheel, source_root)

        _install_lock(runtime_python, source_root / "requirements-runtime.lock", cwd=work)
        _run(
            [
                str(runtime_python),
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
                str(runtime_python),
                "-I",
                str(source_root / "scripts/verify_release_artifact.py"),
                "--installed-smoke",
            ],
            cwd=work,
        )

        _install_lock(builder_python, source_root / "requirements-sbom.lock", cwd=work)
        project_name, project_version, _ = _wheel_identity(wheel)
        sbom = wheel_directory / f"{project_name}-{project_version}.cdx.json"
        _generate_sbom(builder_python, runtime_python, source_root, sbom, cwd=work)
        component_count = _verify_sbom(
            sbom,
            wheel,
            source_root / "requirements-runtime.lock",
            source_root / "requirements-bootstrap.lock",
            source_root / "requirements-sbom.lock",
        )

        output_directory.mkdir(parents=True, exist_ok=True)
        published: list[Path] = []
        for artifact in (wheel, sbom):
            destination = output_directory / artifact.name
            if destination.is_symlink():
                raise ReleaseArtifactError(f"refusing to overwrite artifact symlink: {destination}")
            shutil.copy2(artifact, destination)
            published.append(destination)
        print(
            f"Release artifact gate passed for reproducible {wheel.name} "
            f"(sha256={wheel_digest}, SOURCE_DATE_EPOCH={source_date_epoch}, "
            f"ZIP timestamp={wheel_timestamp}) with a validated CycloneDX 1.6 SBOM covering "
            f"{component_count} runtime components. Published "
            f"{', '.join(str(path) for path in published)}."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=_REPO_ROOT / "dist",
        help="directory that receives the verified wheel and CycloneDX SBOM",
    )
    arguments = parser.parse_args()
    if arguments.installed_smoke:
        _installed_smoke()
    else:
        verify_release_artifact(arguments.output_directory)


if __name__ == "__main__":
    main()

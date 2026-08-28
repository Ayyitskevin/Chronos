"""Focused tests for source-driven release-artifact inventory."""

import importlib.resources
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from scripts.verify_release_artifact import (
    _build_wheel,
    _exercise_installed_migrations,
    _exercise_migration_tree,
    _module_entrypoints,
    _terminal_assets,
)

from chronos.persistence.schema import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETUPTOOLS_84_HASHES = frozenset(
    {
        "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670",
        "f4695c21257f0d9b537ec2692c941d02ee143b7cc1276941349a546573b2ef73",
    }
)


def test_build_backend_input_and_lock_match_exact_published_requirement() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_input = tuple(
        line
        for raw_line in (_REPO_ROOT / "requirements-build.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )
    lock_text = (_REPO_ROOT / "requirements-build.lock").read_text(encoding="utf-8")
    locked_requirements = tuple(
        line.rstrip(" \\")
        for raw_line in lock_text.splitlines()
        if (line := raw_line.strip()) and not line.startswith(("#", "--")) and "==" in line
    )
    locked_hashes = tuple(re.findall(r"--hash=sha256:([0-9a-f]{64})", lock_text))

    assert config["build-system"]["requires"] == list(build_input)
    assert all("==" in requirement for requirement in build_input)
    assert locked_requirements == build_input
    assert len(locked_hashes) == len(_SETUPTOOLS_84_HASHES)
    assert frozenset(locked_hashes) == _SETUPTOOLS_84_HASHES


def test_ci_editable_install_uses_the_same_locked_build_backend() -> None:
    workflow = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    install_step = next(
        step
        for step in workflow["jobs"]["quality"]["steps"]
        if step.get("name") == "Install pinned, hash-verified dependencies"
    )
    commands = tuple(line.strip() for line in install_step["run"].splitlines() if line.strip())

    assert commands == (
        "python -m pip install --upgrade pip",
        "pip install --require-hashes -r requirements-build.lock",
        "pip install --require-hashes -r requirements-dev.lock",
        "pip install -e . --no-deps --no-build-isolation --check-build-dependencies",
    )


def test_wheel_builder_requests_hashed_backend_before_disabling_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    wheel_directory = tmp_path / "dist"
    python = tmp_path / "venv/bin/python"
    commands: list[tuple[list[str], Path]] = []

    def record(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("scripts.verify_release_artifact._run", record)

    _build_wheel(python, source_root, wheel_directory, cwd=tmp_path)

    assert commands == [
        (
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "--require-hashes",
                "-r",
                str(source_root / "requirements-build.lock"),
            ],
            tmp_path,
        ),
        (
            [
                str(python),
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-build-isolation",
                "--check-build-dependencies",
                "--no-deps",
                "--wheel-dir",
                str(wheel_directory),
                str(source_root),
            ],
            tmp_path,
        ),
    ]


def test_terminal_asset_inventory_discovers_every_file_at_any_depth(tmp_path: Path) -> None:
    static_root = tmp_path / "src/chronos/terminal/static"
    nested_root = static_root / "images/icons"
    nested_root.mkdir(parents=True)
    (static_root / "index.html").write_text("terminal", encoding="utf-8")
    (nested_root / "mark.svg").write_text("<svg />", encoding="utf-8")

    assert _terminal_assets(tmp_path) == ("images/icons/mark.svg", "index.html")


def test_terminal_asset_inventory_matches_setuptools_hidden_file_semantics(tmp_path: Path) -> None:
    static_root = tmp_path / "src/chronos/terminal/static"
    hidden_root = static_root / ".generated"
    hidden_root.mkdir(parents=True)
    (static_root / ".gitkeep").write_text("", encoding="utf-8")
    (hidden_root / "bundle.js").write_text("generated", encoding="utf-8")
    (static_root / "terminal.js").write_text("client", encoding="utf-8")

    assert _terminal_assets(tmp_path) == ("terminal.js",)


def test_terminal_package_data_contract_is_extension_and_depth_agnostic() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["setuptools"]["package-data"]["chronos.terminal"] == [
        "static/*",
        "static/**/*",
    ]


def test_module_entrypoint_inventory_discovers_every_main_module(tmp_path: Path) -> None:
    package_root = tmp_path / "src/chronos"
    for module_root in (package_root, package_root / "bridge", package_root / "ops/worker"):
        module_root.mkdir(parents=True, exist_ok=True)
        (module_root / "__main__.py").write_text("", encoding="utf-8")

    assert _module_entrypoints(tmp_path) == (
        "chronos",
        "chronos.bridge",
        "chronos.ops.worker",
    )


def test_installed_migration_drill_upgrades_v2_without_using_ambient_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_root = _REPO_ROOT / "src/chronos/persistence/migrations"
    ambient_database = tmp_path / "ambient.db"
    drill_database = tmp_path / "drill.db"
    ambient_url = f"sqlite:///{ambient_database}"
    monkeypatch.setenv("DATABASE_URL", ambient_url)

    table_count = _exercise_migration_tree(migration_root, drill_database)

    assert table_count == len(Base.metadata.tables)
    assert drill_database.is_file()
    assert not ambient_database.exists()
    assert os.environ["DATABASE_URL"] == ambient_url


def test_installed_migration_drill_executes_revision_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_REPO_ROOT / "src/chronos/persistence/migrations", migration_root)
    head_revision = migration_root / "versions/0010_managed_position_bindings.py"
    source = head_revision.read_text(encoding="utf-8")
    signature = "def upgrade() -> None:\n"
    assert source.count(signature) == 1
    head_revision.write_text(
        source.replace(
            signature,
            signature + '    raise RuntimeError("installed migration body executed")\n',
        ),
        encoding="utf-8",
    )
    ambient_url = f"sqlite:///{tmp_path / 'ambient.db'}"
    monkeypatch.setenv("DATABASE_URL", ambient_url)

    with pytest.raises(RuntimeError, match="installed migration body executed"):
        _exercise_migration_tree(migration_root, tmp_path / "drill.db")

    assert os.environ["DATABASE_URL"] == ambient_url
    assert not (tmp_path / "ambient.db").exists()


def test_installed_migration_drill_resolves_the_package_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration_root = tmp_path / "packaged-migrations"
    shutil.copytree(_REPO_ROOT / "src/chronos/persistence/migrations", migration_root)
    head_revision = migration_root / "versions/0010_managed_position_bindings.py"
    source = head_revision.read_text(encoding="utf-8")
    signature = "def upgrade() -> None:\n"
    assert source.count(signature) == 1
    head_revision.write_text(
        source.replace(
            signature,
            signature + '    raise RuntimeError("package resource executed")\n',
        ),
        encoding="utf-8",
    )
    requested_packages: list[str] = []

    def package_files(package: str) -> Path:
        requested_packages.append(package)
        return migration_root

    monkeypatch.setattr(importlib.resources, "files", package_files)

    with pytest.raises(RuntimeError, match="package resource executed"):
        _exercise_installed_migrations(tmp_path / "drill.db")

    assert requested_packages == ["chronos.persistence.migrations"]


def test_migration_drill_rejects_schema_drift_after_reaching_head(tmp_path: Path) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_REPO_ROOT / "src/chronos/persistence/migrations", migration_root)
    head_revision = migration_root / "versions/0010_managed_position_bindings.py"
    source = head_revision.read_text(encoding="utf-8")
    marker = "\n\ndef downgrade() -> None:\n"
    assert source.count(marker) == 1
    head_revision.write_text(
        source.replace(
            marker,
            '\n    op.create_table("release_gate_drift_probe", '
            'sa.Column("id", sa.Integer, primary_key=True))\n' + marker,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unexpected tables: release_gate_drift_probe"):
        _exercise_migration_tree(migration_root, tmp_path / "drill.db")

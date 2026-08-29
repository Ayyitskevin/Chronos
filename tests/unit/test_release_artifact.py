"""Focused tests for source-driven release-artifact inventory."""

import importlib.resources
import json
import os
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from scripts.verify_release_artifact import (
    _build_wheel,
    _exercise_installed_migrations,
    _exercise_migration_tree,
    _generate_sbom,
    _locked_requirements,
    _module_entrypoints,
    _terminal_assets,
    _verify_sbom,
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


def test_ci_retains_exact_main_release_evidence_with_a_pinned_action() -> None:
    workflow = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    upload_step = next(
        step
        for step in workflow["jobs"]["quality"]["steps"]
        if step.get("name") == "Retain exact-main release evidence"
    )

    assert upload_step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert upload_step["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert upload_step["with"]["name"] == "chronos-release-${{ github.sha }}"
    assert upload_step["with"]["path"].splitlines() == [
        "dist/chronos-*.whl",
        "dist/chronos-*.cdx.json",
    ]
    assert upload_step["with"]["if-no-files-found"] == "error"
    assert upload_step["with"]["retention-days"] == 90


def test_runtime_and_sbom_locks_keep_release_environments_separate() -> None:
    dev = _locked_requirements(_REPO_ROOT / "requirements-dev.lock")
    runtime = _locked_requirements(_REPO_ROOT / "requirements-runtime.lock")
    sbom_tool = _locked_requirements(_REPO_ROOT / "requirements-sbom.lock")
    sbom_input = tuple(
        line
        for raw_line in (_REPO_ROOT / "requirements-sbom.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )

    assert runtime
    assert set(runtime) < set(dev)
    assert all(dev[name] == version for name, version in runtime.items())
    assert {
        "hypothesis",
        "mypy",
        "pytest",
        "pytest-asyncio",
        "ruff",
        "types-pyyaml",
    }.isdisjoint(runtime)
    assert sbom_input == ("cyclonedx-bom==7.3.1",)
    assert sbom_tool["cyclonedx-bom"] == "7.3.1"


def test_sbom_generator_targets_only_the_installed_runtime_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_python = tmp_path / "builder/bin/python"
    runtime_python = tmp_path / "runtime/bin/python"
    source_root = tmp_path / "source"
    output = tmp_path / "dist/chronos-0.1.0.cdx.json"
    commands: list[tuple[list[str], Path]] = []

    def record(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("scripts.verify_release_artifact._run", record)

    _generate_sbom(tool_python, runtime_python, source_root, output, cwd=tmp_path)

    assert commands == [
        (
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
            tmp_path,
        )
    ]


def _write_sbom_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    wheel = tmp_path / "chronos-0.1.0-py3-none-any.whl"
    metadata = """Metadata-Version: 2.4
Name: chronos
Version: 0.1.0
Requires-Dist: fastapi<1,>=0.115
Requires-Dist: pytest<9,>=8.3; extra == \"dev\"
"""
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("chronos-0.1.0.dist-info/METADATA", metadata)

    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_text("fastapi==0.139.2\nstarlette==0.52.1\n", encoding="utf-8")
    sbom_tool_lock = tmp_path / "requirements-sbom.lock"
    sbom_tool_lock.write_text("cyclonedx-bom==7.3.1\n", encoding="utf-8")
    sbom = tmp_path / "chronos-0.1.0.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {
                    "component": {
                        "bom-ref": "root-component",
                        "name": "chronos",
                        "type": "application",
                        "version": "0.1.0",
                    },
                    "properties": [{"name": "cdx:reproducible", "value": "true"}],
                    "tools": {
                        "components": [
                            {
                                "name": "cyclonedx-py",
                                "type": "application",
                                "version": "7.3.1",
                            }
                        ]
                    },
                },
                "components": [
                    {
                        "bom-ref": "fastapi==0.139.2",
                        "name": "fastapi",
                        "type": "library",
                        "version": "0.139.2",
                    },
                    {
                        "bom-ref": "starlette==0.52.1",
                        "name": "starlette",
                        "type": "library",
                        "version": "0.52.1",
                    },
                    {
                        "bom-ref": "pip==26.0.1",
                        "name": "pip",
                        "type": "library",
                        "version": "26.0.1",
                    },
                ],
                "dependencies": [
                    {"ref": "root-component", "dependsOn": ["fastapi==0.139.2"]},
                    {"ref": "fastapi==0.139.2", "dependsOn": ["starlette==0.52.1"]},
                    {"ref": "starlette==0.52.1", "dependsOn": []},
                    {"ref": "pip==26.0.1", "dependsOn": []},
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return sbom, wheel, runtime_lock, sbom_tool_lock


def test_sbom_verifier_accepts_the_exact_locked_runtime_environment(tmp_path: Path) -> None:
    sbom, wheel, runtime_lock, sbom_tool_lock = _write_sbom_fixture(tmp_path)

    component_count = _verify_sbom(sbom, wheel, runtime_lock, sbom_tool_lock)

    assert component_count == 3


def test_sbom_verifier_rejects_a_dev_component_outside_the_runtime_lock(tmp_path: Path) -> None:
    sbom, wheel, runtime_lock, sbom_tool_lock = _write_sbom_fixture(tmp_path)
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    payload["components"].append(
        {
            "bom-ref": "pytest==8.4.2",
            "name": "pytest",
            "type": "library",
            "version": "8.4.2",
        }
    )
    payload["dependencies"].append({"ref": "pytest==8.4.2", "dependsOn": []})
    sbom.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unlocked components: pytest"):
        _verify_sbom(sbom, wheel, runtime_lock, sbom_tool_lock)


def test_sbom_verifier_rejects_an_application_self_dependency(tmp_path: Path) -> None:
    sbom, wheel, runtime_lock, sbom_tool_lock = _write_sbom_fixture(tmp_path)
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    root = payload["metadata"]["component"]["bom-ref"]
    root_edge = next(item for item in payload["dependencies"] if item["ref"] == root)
    root_edge["dependsOn"].append(root)
    sbom.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="application self-reference"):
        _verify_sbom(sbom, wheel, runtime_lock, sbom_tool_lock)


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

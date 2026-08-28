"""Focused tests for source-driven release-artifact inventory."""

import importlib.resources
import os
import shutil
import tomllib
from pathlib import Path

import pytest
from scripts.verify_release_artifact import (
    _exercise_installed_migrations,
    _exercise_migration_tree,
    _module_entrypoints,
    _terminal_assets,
)

from chronos.persistence.schema import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]


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

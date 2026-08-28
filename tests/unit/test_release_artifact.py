"""Focused tests for source-driven release-artifact inventory."""

import tomllib
from pathlib import Path

from scripts.verify_release_artifact import _module_entrypoints, _terminal_assets

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

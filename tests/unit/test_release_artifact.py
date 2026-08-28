"""Focused tests for source-driven release-artifact inventory."""

from pathlib import Path

from scripts.verify_release_artifact import _module_entrypoints, _terminal_assets


def test_terminal_asset_inventory_discovers_every_file_at_any_depth(tmp_path: Path) -> None:
    static_root = tmp_path / "src/chronos/terminal/static"
    nested_root = static_root / "images/icons"
    nested_root.mkdir(parents=True)
    (static_root / "index.html").write_text("terminal", encoding="utf-8")
    (nested_root / "mark.svg").write_text("<svg />", encoding="utf-8")

    assert _terminal_assets(tmp_path) == ("images/icons/mark.svg", "index.html")


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

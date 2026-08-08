"""Certified-data evidence primitives stay isolated from trading and registry authority.

These modules only authenticate ordinary input bytes and preserve content-addressed
evidence.  They must not acquire order, broker, strategy, registry, database, network,
or holdout-guardian authority.  Registry ordering belongs to the separate brokered
trial runner.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.research as research_pkg

_MODULES = ("certified_data", "replay_store")
_FORBIDDEN_PREFIXES = (
    "chronos.api",
    "chronos.autonomy",
    "chronos.broker",
    "chronos.control",
    "chronos.execution",
    "chronos.orders",
    "chronos.persistence",
    "chronos.registry",
    "chronos.risk",
    "chronos.service",
    "chronos.services",
    "chronos.strategy",
    "chronos.strategies",
    "chronos.supervisor",
    "fastapi",
    "httpx",
    "ib_async",
    "ibapi",
    "sqlalchemy",
    "sqlite3",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _module_files() -> tuple[Path, ...]:
    package_dir = Path(research_pkg.__file__).parent
    return tuple(package_dir / f"{name}.py" for name in _MODULES)


def _imported_names(source: str) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return tuple(names)


def test_import_matcher_sees_subpackage_aliases() -> None:
    assert "chronos.registry" in _imported_names("from chronos import registry\n")


def test_certified_evidence_modules_have_no_forbidden_direct_imports() -> None:
    for path in _module_files():
        assert path.exists(), f"expected certified-evidence module missing: {path}"
        for imported in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN_PREFIXES:
                assert not (imported == forbidden or imported.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden authority {imported!r}"
                )


def test_importing_certified_evidence_leaks_no_forbidden_authority() -> None:
    prefixes = repr(_FORBIDDEN_PREFIXES)
    probe = (
        "import chronos.research.certified_data, chronos.research.replay_store, sys; "
        f"bad=[m for m in sys.modules if m.startswith({prefixes})]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], f"certified-evidence import leaked forbidden authority: {leaked}"


def test_canonical_registry_and_replay_evidence_roots_are_gitignored() -> None:
    for runtime_path in (
        "research/registry/registry.jsonl",
        "research/registry/registry.head.json",
        "research/replay_store/objects/sha256/aa/example",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", runtime_path],
            cwd=str(_REPO_ROOT),
            check=False,
        )
        assert result.returncode == 0, f"runtime evidence path is not ignored: {runtime_path}"


def test_private_data_read_has_one_production_call_site() -> None:
    callers: list[str] = []
    source_root = _REPO_ROOT / "src/chronos"
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_read_bytes_for_trial"
            for node in ast.walk(tree)
        ):
            callers.append(path.relative_to(_REPO_ROOT).as_posix())

    assert callers == ["src/chronos/research/trial_runner.py"]

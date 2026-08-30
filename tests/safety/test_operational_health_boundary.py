from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = (
    REPO_ROOT / "src" / "chronos" / "control",
    REPO_ROOT / "src" / "chronos" / "execution",
    REPO_ROOT / "src" / "chronos" / "orders",
    REPO_ROOT / "src" / "chronos" / "portfolio",
    REPO_ROOT / "src" / "chronos" / "supervisor",
    REPO_ROOT / "src" / "chronos" / "risk",
    REPO_ROOT / "src" / "chronos" / "broker",
    REPO_ROOT / "src" / "chronos" / "services",
    REPO_ROOT / "src" / "chronos" / "runtime.py",
)


PROJECTION_MODULES = (
    "chronos.operations.health",
    "chronos.operations.clock",
    "chronos.operations.external_probe",
)


def _is_projection_module(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in PROJECTION_MODULES)


def _imports_operational_projection(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_projection_module(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules = (module, *(f"{module}.{alias.name}" for alias in node.names))
            if any(_is_projection_module(candidate) for candidate in imported_modules):
                return True
    return False


def test_operational_health_cannot_become_an_authority_dependency() -> None:
    offenders: list[str] = []
    for target in FORBIDDEN:
        paths = (target,) if target.is_file() else tuple(target.rglob("*.py"))
        offenders.extend(
            str(path.relative_to(REPO_ROOT))
            for path in paths
            if _imports_operational_projection(path)
        )

    assert offenders == []


def test_external_probe_is_inside_the_authority_import_boundary() -> None:
    assert "chronos.operations.external_probe" in PROJECTION_MODULES


def test_authority_boundary_detects_from_package_imports(tmp_path: Path) -> None:
    candidate = tmp_path / "authority.py"
    candidate.write_text(
        "from chronos.operations import external_probe\n",
        encoding="utf-8",
    )

    assert _imports_operational_projection(candidate)


def test_initial_clock_sample_stays_inside_startup_cleanup_guard() -> None:
    """Cancellation during the bounded sample must not leak runtime or lease."""

    source = (REPO_ROOT / "src" / "chronos" / "api" / "main.py").read_text(encoding="utf-8")
    sample = source.index("await refresh_clock_health(backend_state.clock_health, clock_sampler)")
    cleanup_guard = source.index("except BaseException:", sample)
    monitor_task = source.index("clock_task = asyncio.create_task", cleanup_guard)

    assert sample < cleanup_guard < monitor_task
    guarded_cleanup = source[cleanup_guard:monitor_task]
    assert "lease.release()" in guarded_cleanup
    assert "runtime.close()" in guarded_cleanup

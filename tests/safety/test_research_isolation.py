"""The research evidence plane must never reach the real order/broker path.

The research plane deliberately drives the deterministic backtest engine, which uses the
*simulated* execution broker — so unlike ``registry``/``histdata`` it legitimately reaches
``chronos.execution``/``chronos.control``/``chronos.risk`` transitively. The load-bearing
boundary for C3 is therefore narrower and exact: the new statistics/walk-forward/CV modules
import **no real order-submission or broker-adapter module** (``chronos.orders`` /
``chronos.broker``), and importing them leaks neither into ``sys.modules``.

Mirrors ``tests/safety/test_histdata_isolation.py``: an AST walk over the specific C3
modules plus a subprocess ``sys.modules`` probe.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.research as research_pkg

# The real trading edge C3 must not touch. NOT chronos.execution/control/risk: those are
# reached through the *simulated* backtest engine by design (see module docstring).
_FORBIDDEN = ("chronos.orders", "chronos.broker")
_BROKERED_RUNNER_FORBIDDEN = (
    "chronos.api",
    "chronos.autonomy",
    "chronos.broker",
    "chronos.control",
    "chronos.execution",
    "chronos.orders",
    "chronos.persistence",
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
_C3_MODULES = (
    "stats",
    "walkforward",
    "purged_cv",
    "campaign",
    "repro",
    "certified_data",
    "replay_store",
    "trial_runner",
    # D2 certification: judges an export and freezes it; owns no order path either.
    "certification",
    "dataset_release",
    "holdout_map",
    "data_intake",
    "data_certification",
    "data_assemble",
    "data_check",
    "session_calendar",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _module_files() -> list[Path]:
    package_dir = Path(research_pkg.__file__).parent
    return [package_dir / f"{name}.py" for name in _C3_MODULES]


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_import_matcher_sees_subpackage_aliases() -> None:
    assert "chronos.broker" in _imported_names("from chronos import broker\n")


def test_c3_modules_exist() -> None:
    for path in _module_files():
        assert path.exists(), f"expected C3 module missing: {path}"


def test_c3_modules_have_no_forbidden_ast_imports() -> None:
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_brokered_trial_runner_has_no_trading_database_network_or_promotion_imports() -> None:
    path = Path(research_pkg.__file__).parent / "trial_runner.py"
    for name in _imported_names(path.read_text(encoding="utf-8")):
        for forbidden in _BROKERED_RUNNER_FORBIDDEN:
            assert not (name == forbidden or name.startswith(forbidden + ".")), (
                f"trial_runner.py imports forbidden authority {name!r}"
            )


def test_importing_walkforward_leaks_no_order_or_broker_module() -> None:
    prefixes = repr(_FORBIDDEN)
    probe = (
        "import chronos.research.stats, chronos.research.walkforward, "
        "chronos.research.purged_cv, chronos.research.campaign, "
        "chronos.research.repro, chronos.research.certified_data, "
        "chronos.research.replay_store, chronos.research.trial_runner, sys; "
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
    assert leaked == [], f"research walk-forward import leaked forbidden modules: {leaked}"


# --------------------------------------------------------------------------
# The other direction: the ORDER plane must not import subsystem 2.
#
# Everything above pins research -> orders. README's plane claim is the
# reverse -- "it is separate from, and never imported by, the Live Wheel order
# pipeline in subsystem 1" -- and nothing executed it. A test whose name matches
# a claim is not evidence for the claim until its direction is read; these two
# now sit together so the next reader sees both arrows.
# --------------------------------------------------------------------------

#: The non-trading planes the order plane must not import.
#:
#: `research`, `backtest`, `skb`, `strategies`, `registry` and `specs` are subsystem 2 —
#: the deterministic research/strategy platform README's claim is about. `histdata` is a
#: THIRD plane (the historical-data plane, C1/ADR-0011), included on fable-2's
#: observation: `tests/safety/test_histdata_isolation` pins histdata -> orders, and
#: nothing pinned orders -> histdata, so the reverse direction was unguarded there for
#: the same reason it was unguarded for subsystem 2. The constant is named for what it
#: holds rather than for subsystem 2 alone, because a set whose name overstates its
#: contents is the defect this file's neighbours keep finding.
#:
#: Named exactly, one package per entry, and compared segment-by-segment rather than
#: by substring -- because ``chronos.strategy`` and ``chronos.strategies`` both exist
#: and belong to *different subsystems*. ``chronos.strategy`` (singular) is subsystem
#: 1's own "Deterministic Wheel strategy engines", which ``orders/risk`` imports by
#: design; ``chronos.strategies`` (plural) is subsystem 2. A substring match on
#: "strategy" reports a plane violation that does not exist, which is exactly the
#: false finding this comment exists to prevent.
_PLANES_THE_ORDER_PLANE_MUST_NOT_IMPORT = frozenset(
    {"research", "backtest", "skb", "strategies", "registry", "specs", "histdata"}
)

_ORDER_PLANE = Path(__file__).resolve().parents[2] / "src" / "chronos" / "orders"


def _chronos_imports(root: Path) -> dict[Path, set[str]]:
    """Every ``chronos.<package>`` imported under ``root``, by importing file.

    An ``ast.walk`` rather than a grep, so a function-local import counts: the one
    that motivated this test (``from chronos.strategy.eligibility import evaluate``)
    sits inside a method body, where a line-oriented scan of the import block would
    never look.
    """

    found: dict[Path, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        packages: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import: it cannot name another top-level
                # chronos package, so it is not this test's business.
                names = [node.module] if node.module and not node.level else []
            else:
                continue
            for name in names:
                parts = name.split(".")
                if len(parts) >= 2 and parts[0] == "chronos":
                    packages.add(parts[1])
        if packages:
            found[path] = packages
    return found


def test_the_scan_finds_an_import_nested_in_a_function_body(tmp_path: Path) -> None:
    """The positive control, on a literal source whose ONLY import is nested.

    The first version of this control anchored on ``chronos.strategy`` in the real
    order plane — which ``orders/risk`` imports at module level *as well as* inside a
    method. A walk crippled to top-level imports therefore passed it, so the control
    proved nothing about the property it exists for. fable-2 measured exactly that:
    an injected violation plus a crippled walk left the file green.

    A fixture whose only ``chronos`` import is inside a function body cannot pass
    unless the walk descends, which is the whole claim. Mirrors this file's own
    ``test_import_matcher_sees_subpackage_aliases``: feed literal source, assert the
    matcher's answer.
    """

    module = tmp_path / "nested_only.py"
    module.write_text(
        "import os\n"
        "\n"
        "\n"
        "def loader():\n"
        "    from chronos.strategies import registry\n"
        "    return registry, os\n",
        encoding="utf-8",
    )

    found = _chronos_imports(tmp_path)

    assert found, "the walk found no chronos import at all in the fixture"
    assert found[module] == {"strategies"}, (
        "the only chronos import in the fixture is inside a function body; not finding "
        "it means the walk does not descend, and the isolation check below is blind to "
        "exactly the import that motivated it"
    )


def test_the_order_plane_import_scan_sees_something() -> None:
    """And the real scan reaches the real order plane, so it is not scanning nothing."""

    imports = _chronos_imports(_ORDER_PLANE)
    assert len(imports) > 5, f"only {len(imports)} order-plane modules import chronos at all"


def test_the_order_plane_imports_no_subsystem_two_package() -> None:
    """README: the deterministic platform is never imported by the order pipeline."""

    offenders = [
        f"{path.relative_to(_ORDER_PLANE.parents[2])}: chronos.{package}"
        for path, packages in _chronos_imports(_ORDER_PLANE).items()
        for package in sorted(packages & _PLANES_THE_ORDER_PLANE_MUST_NOT_IMPORT)
    ]
    assert not offenders, (
        "the order plane imports a subsystem-2 package; README says it never does:\n"
        + "\n".join(offenders)
    )

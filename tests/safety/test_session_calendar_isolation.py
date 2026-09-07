"""The research session calendar must never become a trading-authority input.

R-26 is explicit that Chronos does not derive market-open from a weekday-and-clock
calendar: the venue's own ``CLOSED`` is the load-bearing token, because a calendar that
wrongly says "open" opens a gate that should have held. R-34 discloses the same residual
for session counters.

Introducing a calendar for research coverage therefore introduces exactly one new
hazard — that some later change wires it into the authority plane and quietly reverses
that decision. This test is the structural answer, and it runs in **both** directions:
the module reaches nothing, and nothing outside research/histdata reaches *it* except
an explicitly named, independently read-only operator adapter.

Two weaknesses in the first version of this guard were found by cross-seat review
(codex HOLD, GLM concurring) and are fixed here, because a guard with a known bypass is
worse than none — it reports safety it cannot deliver:

- **Import spellings.** Recording only ``node.module`` meant ``from chronos.research
  import session_calendar`` was seen as ``chronos.research`` and walked straight past,
  and relative imports were skipped entirely. All three spellings now resolve.
- **The scanned set.** A hand-maintained authority list omitted ``runtime.py``,
  ``portfolio``, ``cli``, ``ui``, ``persistence``, ``backtest`` and ``terminal`` — and
  would omit whatever package is added next. The direct guard is now a complement:
  every chronos module except research (which owns the calendar) and histdata (whose
  hourly parser needs the session close to cap the final intraday bar, ADR-0029).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos

_CALENDAR_MODULE = "chronos.research.session_calendar"

# The data-intake CLI has three reviewed edges into the research plane: read-only verify,
# certify's local import of the write orchestrator, and assemble's local import of the
# store-to-delivery converter (read-only on the store; it writes only under --out).
# Ignore exactly those edges when
# asking whether another package can acquire the calendar; every other edge remains
# default-denied. Their distinct write boundaries are independently bound by the two
# tests/integration/test_data_intake_*.py suites.
_REVIEWED_RESEARCH_CLI_EDGES = {
    ("chronos.cli.data_commands", "chronos.research.data_intake"),
    ("chronos.cli.data_commands", "chronos.research.data_certification"),
    # Third reviewed edge (Sprint 3 B1): the synthetic-store generator. It uses the
    # calendar in the one direction R-26 is indifferent to — choosing which dates a
    # FIXTURE gets bars on, so the fixture agrees with the expectation certification
    # measures against. It supplies market-open evidence to nothing: it reads no market
    # data, touches no gateway, and writes only to an operator-named --out directory.
    # The hazard R-26 names is a calendar wired INTO the authority plane; this is a
    # generator wired out of it. Bound by tests/unit/test_synth_store.py.
    ("chronos.cli.data_commands", "chronos.research.synth_store"),
    ("chronos.cli.data_commands", "chronos.research.data_assemble"),
    # Fifth reviewed edge (Sprint 4): `data check`, the partial-store dry run. It reads the
    # calendar for the same reason the intake edges do — coverage is measured against the
    # sessions the exchange held — and it is the most read-only route of the five: it opens
    # store files, returns findings, writes nothing and produces no verdict. Market-open
    # evidence flows to no decision from here. Bound by tests/unit/test_data_check.py.
    ("chronos.cli.data_commands", "chronos.research.data_check"),
}

#: The planes allowed to hold the calendar. Everything else is denied by default.
_ALLOWED_PACKAGES = ("research", "histdata")

#: Packages that decide, size, route, or submit anything. Used only by the narrower
#: transitive check — the direct guard below denies every module outside
#: ``_ALLOWED_PACKAGES``, so it cannot be defeated by a package nobody remembered.
_AUTHORITY_PACKAGES = (
    "api",
    "autonomy",
    "broker",
    "control",
    "execution",
    "orders",
    "persistence",
    "portfolio",
    "risk",
    "service",
    "services",
    "strategies",
    "strategy",
    "supervisor",
    "terminal",
    "ui",
)

#: The calendar itself is deliberately stdlib-only: it holds no data source, so it can
#: never disagree with one.
_FORBIDDEN_IMPORTS = ("chronos.", "httpx", "ib_async", "ibapi", "sqlalchemy", "sqlite3")

_PACKAGE_ROOT = Path(chronos.__file__).parent


def _module_name_for(path: Path) -> str:
    """Dotted module name of a file inside the chronos package."""

    relative = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(["chronos", *parts])


def _imported_names(source: str, *, module_name: str = "chronos") -> tuple[str, ...]:
    """Every module an import statement can name, in every spelling.

    ``import a.b.c``; ``from a.b import c`` (which needs the alias appended to the
    recorded module); and ``from . import c`` / ``from ..x import c``, which record no
    absolute module at all and must be resolved against the importing file's package.
    """

    tree = ast.parse(source)
    names: list[str] = []
    package_parts = module_name.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                kept = len(package_parts) - (node.level - 1)
                base_parts = package_parts[: max(kept, 0)]
                if node.module:
                    base_parts = [*base_parts, node.module]
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            if not base:
                continue
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return tuple(names)


def _modules_outside(allowed: tuple[str, ...]) -> tuple[Path, ...]:
    """Every chronos module that is not inside one of ``allowed``."""

    allowed_dirs = {_PACKAGE_ROOT / name for name in allowed}
    return tuple(
        path
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if not any(parent in allowed_dirs for parent in path.parents)
    )


def _names_in(path: Path) -> tuple[str, ...]:
    return _imported_names(path.read_text(), module_name=_module_name_for(path))


def _mentions(names: tuple[str, ...], target: str) -> bool:
    return any(name == target or name.startswith(f"{target}.") for name in names)


def test_the_calendar_imports_nothing_but_the_standard_library() -> None:
    source = (_PACKAGE_ROOT / "research" / "session_calendar.py").read_text()
    for name in _imported_names(source, module_name=_CALENDAR_MODULE):
        for forbidden in _FORBIDDEN_IMPORTS:
            assert not name.startswith(forbidden), f"session_calendar imports {name}"


def test_nothing_outside_research_and_histdata_imports_the_calendar() -> None:
    """The R-26 guard. A single new import anywhere else fails CI rather than shipping."""

    offenders = [
        str(path.relative_to(_PACKAGE_ROOT))
        for path in _modules_outside(_ALLOWED_PACKAGES)
        if _mentions(_names_in(path), _CALENDAR_MODULE)
    ]
    assert offenders == [], (
        f"the research session calendar reached {offenders}. R-26 keeps market-open "
        "evidence on the venue's own CLOSED token; a derived calendar must never "
        "supply it."
    )


def test_the_scanned_set_actually_covers_the_authority_plane() -> None:
    """Guard the guard: the complement must include the modules a list would miss.

    These are the exact files the previous hand-maintained list omitted. If the
    complement ever stops covering them, this fails before the guard above can pass
    vacuously.
    """

    scanned = {str(path.relative_to(_PACKAGE_ROOT)) for path in _modules_outside(_ALLOWED_PACKAGES)}
    for expected in ("runtime.py", "portfolio", "cli", "ui", "persistence", "backtest"):
        assert any(entry == expected or entry.startswith(f"{expected}/") for entry in scanned), (
            f"{expected} is not scanned by the calendar guard"
        )
    assert len(scanned) > 100, "the complement scan collapsed to a suspiciously small set"


def test_every_import_spelling_is_recognised() -> None:
    """The bypasses codex's HOLD named: dotted-from and relative imports."""

    direct = _imported_names("import chronos.research.session_calendar", module_name="chronos.risk")
    dotted_from = _imported_names(
        "from chronos.research import session_calendar", module_name="chronos.risk"
    )
    symbol_from = _imported_names(
        "from chronos.research.session_calendar import SessionCalendar",
        module_name="chronos.risk",
    )
    relative = _imported_names(
        "from ..research import session_calendar", module_name="chronos.risk.engine"
    )
    for names in (direct, dotted_from, symbol_from, relative):
        assert _mentions(names, _CALENDAR_MODULE), names


def _chronos_modules() -> dict[str, Path]:
    return {_module_name_for(path): path for path in sorted(_PACKAGE_ROOT.rglob("*.py"))}


def _import_graph() -> dict[str, set[str]]:
    """Every chronos module mapped to the chronos modules it imports.

    Names that resolve to a symbol rather than a module (``from x.y import z``
    where ``z`` is a function) are dropped by intersecting with the real module
    set, so the graph carries only edges that actually load code.
    """

    modules = _chronos_modules()
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        imported = set(_imported_names(path.read_text(), module_name=name))
        graph[name] = {other for other in imported if other in modules}
    return graph


def _reaches(graph: dict[str, set[str]], start: str, target: str) -> list[str] | None:
    """Shortest import path from ``start`` to ``target``, or None."""

    queue: list[tuple[str, list[str]]] = [(start, [start])]
    seen = {start}
    while queue:
        current, trail = queue.pop(0)
        for nxt in sorted(graph.get(current, ())):
            if nxt == target:
                return [*trail, nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, [*trail, nxt]))
    return None


def _without_reviewed_research_cli_edges(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    """Remove only the named intake edges before authority reachability checks."""

    guarded = {name: set(imports) for name, imports in graph.items()}
    for source, target in _REVIEWED_RESEARCH_CLI_EDGES:
        assert target in guarded.get(source, set()), (
            f"missing reviewed research CLI edge {source}->{target}"
        )
        guarded[source].remove(target)
    return guarded


def test_no_unreviewed_module_outside_research_can_reach_the_calendar_transitively() -> None:
    """Default-deny reachability, with one exact read-only intake edge removed.

    Denying every importer of ``chronos.histdata`` outright would be wrong and would
    fail today: ``chronos.cli`` and ``chronos.registry`` legitimately import
    ``histdata.store``/``holdout``, neither of which loads the calendar. Only
    ``histdata.official_client`` holds a module-level ``SessionCalendar`` (its hourly
    parser needs the session close). The honest invariant is therefore reachability,
    not a package list and not a single hop. Removing the single reviewed edge before
    computing reachability keeps any second route from the intake CLI visible.
    """

    graph = _without_reviewed_research_cli_edges(_import_graph())
    offenders: list[str] = []
    for name in sorted(graph):
        if name.startswith(("chronos.research", "chronos.histdata")):
            continue
        trail = _reaches(graph, name, _CALENDAR_MODULE)
        if trail is not None:
            offenders.append(" -> ".join(trail))
    assert offenders == [], (
        "these modules can load the research session calendar by following imports:\n"
        + "\n".join(offenders)
        + "\nR-26 keeps market-open evidence on the venue's own CLOSED token."
    )


def test_the_reachability_graph_is_real() -> None:
    """Guard the guard: an all-empty graph would make the check above vacuous."""

    graph = _import_graph()
    assert len(graph) > 200, "the module scan collapsed"
    assert graph["chronos.histdata.official_client"] >= {_CALENDAR_MODULE}
    # A known-good multi-hop path exists, so BFS is genuinely traversing edges.
    assert _reaches(graph, "chronos.histdata.holdout", "chronos.marketdata.quality")
    # Pinned as a SHAPE, not as one exact path: BFS returns whichever reviewed research
    # module it reaches first, which is a lexicographic accident of the module names and
    # changed the day a fifth edge was added. What the control is for is that a real
    # multi-hop path is traversed, and that the middle hop is one of the edges the list
    # above claims to be removing.
    path = _reaches(graph, "chronos.cli.data_commands", _CALENDAR_MODULE)
    assert path is not None and len(path) == 3
    assert path[0] == "chronos.cli.data_commands" and path[-1] == _CALENDAR_MODULE
    assert path[1] in {target for _, target in _REVIEWED_RESEARCH_CLI_EDGES}
    # and the intake edge specifically is still there, whichever one BFS happened to find
    assert _reaches(graph, "chronos.research.data_intake", _CALENDAR_MODULE)
    guarded = _without_reviewed_research_cli_edges(graph)
    # The reviewed inspection edge is removed; cli/registry have no other route.
    for benign in (
        "chronos.cli.__main__",
        "chronos.cli.main",
        "chronos.cli.data_commands",
        "chronos.registry.holdout_guardian",
    ):
        assert _reaches(guarded, benign, _CALENDAR_MODULE) is None


def test_importing_the_calendar_pulls_in_no_trading_module() -> None:
    """AST covers the source; this covers whatever the import machinery actually does."""

    probe = (
        "import sys;"
        f"import {_CALENDAR_MODULE};"
        "leaked=sorted(m for m in sys.modules "
        "if m.startswith('chronos.') and any(m.startswith('chronos.'+p+'.') "
        f"or m=='chronos.'+p for p in {_AUTHORITY_PACKAGES!r}));"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", f"leaked into sys.modules: {result.stdout.strip()}"

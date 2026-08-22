"""The research session calendar must never become a trading-authority input.

R-26 is explicit that Chronos does not derive market-open from a weekday-and-clock
calendar: the venue's own ``CLOSED`` is the load-bearing token, because a calendar that
wrongly says "open" opens a gate that should have held. R-34 discloses the same residual
for session counters.

Introducing a calendar for research coverage therefore introduces exactly one new
hazard — that some later change wires it into the authority plane and quietly reverses
that decision. This test is the structural answer, and it runs in **both** directions:
the module reaches nothing, and nothing outside research/histdata reaches *it*.

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


def test_no_authority_module_imports_histdata() -> None:
    """The transitive route, opened when the hourly parser needed the session close.

    ``chronos.histdata.official_client`` holds a module-level ``SessionCalendar``. An
    authority module importing ``chronos.histdata`` would load the calendar into the
    authority plane while the direct guard stayed green.
    """

    offenders = [
        str(path.relative_to(_PACKAGE_ROOT))
        for path in _modules_outside(_ALLOWED_PACKAGES)
        if any(
            _module_name_for(path).startswith(f"chronos.{package}")
            for package in _AUTHORITY_PACKAGES
        )
        and _mentions(_names_in(path), "chronos.histdata")
    ]
    assert offenders == [], (
        "the authority plane imported chronos.histdata, which transitively carries the "
        f"research session calendar: {offenders}. R-26 keeps market-open evidence on "
        "the venue's own CLOSED token."
    )


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

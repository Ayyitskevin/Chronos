"""The research session calendar must never become a trading-authority input.

R-26 is explicit that Chronos does not derive market-open from a weekday-and-clock
calendar: the venue's own ``CLOSED`` is the load-bearing token, because a calendar that
wrongly says "open" opens a gate that should have held. R-34 discloses the same residual
for session counters.

Introducing a calendar for research coverage therefore introduces exactly one new
hazard — that some later change wires it into the authority plane and quietly reverses
that decision. This test is the structural answer, and it runs in **both** directions:
the module reaches nothing, and, more importantly, the trading plane reaches *it*
nowhere. A docstring cannot fail CI; this can.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos

_CALENDAR_MODULE = "chronos.research.session_calendar"

#: Packages that decide, size, route, or submit anything. None of them may import the
#: calendar, transitively or directly.
_AUTHORITY_PACKAGES = (
    "api",
    "autonomy",
    "broker",
    "control",
    "execution",
    "orders",
    "risk",
    "service",
    "services",
    "strategies",
    "strategy",
    "supervisor",
)

#: The calendar itself is deliberately stdlib-only: it holds no data source, so it can
#: never disagree with one.
_FORBIDDEN_IMPORTS = ("chronos.", "httpx", "ib_async", "ibapi", "sqlalchemy", "sqlite3")

_PACKAGE_ROOT = Path(chronos.__file__).parent


def _imported_names(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return tuple(names)


def test_the_calendar_imports_nothing_but_the_standard_library() -> None:
    source = (_PACKAGE_ROOT / "research" / "session_calendar.py").read_text()
    for name in _imported_names(source):
        for forbidden in _FORBIDDEN_IMPORTS:
            assert not name.startswith(forbidden), f"session_calendar imports {name}"


def test_no_authority_module_imports_the_calendar() -> None:
    """The R-26 guard. A single new import here fails CI rather than shipping."""

    offenders: list[str] = []
    for package in _AUTHORITY_PACKAGES:
        package_dir = _PACKAGE_ROOT / package
        if not package_dir.is_dir():
            continue
        for path in package_dir.rglob("*.py"):
            names = _imported_names(path.read_text())
            if any(
                name == _CALENDAR_MODULE or name.startswith(f"{_CALENDAR_MODULE}.")
                for name in names
            ):
                offenders.append(str(path.relative_to(_PACKAGE_ROOT)))
    assert offenders == [], (
        "the research session calendar reached the authority plane: "
        f"{offenders}. R-26 keeps market-open evidence on the venue's own CLOSED token; "
        "a derived calendar must never supply it."
    )


def test_no_authority_module_reaches_the_calendar_through_histdata() -> None:
    """The transitive route, opened when the hourly parser needed the session close.

    ``chronos.histdata.official_client`` holds a module-level ``SessionCalendar``
    (it needs the official close to cap the final intraday bar). The direct-import
    guard above cannot see an authority module that imports ``chronos.histdata``
    instead — it would load the calendar into the authority plane with every test
    still green, quietly reversing R-26. histdata is the read-only data plane and
    the authority plane has no business importing it either way.
    """

    offenders: list[str] = []
    for package in _AUTHORITY_PACKAGES:
        package_dir = _PACKAGE_ROOT / package
        if not package_dir.is_dir():
            continue
        for path in package_dir.rglob("*.py"):
            names = _imported_names(path.read_text())
            if any(
                name == "chronos.histdata" or name.startswith("chronos.histdata.") for name in names
            ):
                offenders.append(str(path.relative_to(_PACKAGE_ROOT)))
    assert offenders == [], (
        "the authority plane imported chronos.histdata, which now transitively "
        f"carries the research session calendar: {offenders}. R-26 keeps market-open "
        "evidence on the venue's own CLOSED token."
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

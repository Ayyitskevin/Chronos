"""Repository-wide inventory of every transmit-enabling site (ADR-0016 §8, R-28).

``tests/safety/test_single_transmit_site.py`` proves there is exactly one
``transmit=True`` **keyword argument** inside ``chronos.orders``. That is the
guarantee ADR-0009 made, and it holds — but the M0 autonomy audit showed it is
narrower than the sentence people quote from it ("the one and only
``transmit=True``"). Two gaps:

1. it scans only ``chronos.orders``, and
2. it matches only keyword arguments.

``chronos/execution/brokers/ibkr_paper.py`` sits outside both: it enables
transmission with an *attribute assignment*, ``order.transmit = True``. It is a
fully functional ``placeOrder``/``cancelOrder`` adapter that no production path
constructs — the only ``ExecutionEngine`` wiring passes ``NullExecutionBroker``
— but nothing structurally stopped a future wiring change from turning it into a
second broker path with none of the ADR-0009 gates.

This module closes the gap the way the directive asks: a **complete** inventory
across ``src/chronos``, matching both spellings, pinned to an explicit expected
set. A new transmit site anywhere in the repository fails here, whatever package
it lives in and whichever syntax it uses.

The dormant adapter is *quarantined* rather than retired: it keeps its tests and
its history, but constructing it now demands an explicit acknowledgement that no
production module passes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chronos.broker.base import BrokerSafetyError
from chronos.control.modes import ExecutionCapability, ModeLock, TradingMode
from chronos.execution.brokers.ibkr_paper import IBKRPaperExecutionAdapter

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "chronos"

#: Every place in the repository that can set a broker order's transmit flag to
#: True, as ``(relative path, symbol)``. Adding a site is a deliberate,
#: reviewable act: update this set and say why in the ADR.
_EXPECTED_TRANSMIT_SITES: set[tuple[str, str]] = {
    # The single audited live boundary (ADR-0009). Keyword argument.
    ("orders/submission.py", "keyword"),
    # QUARANTINED second site (R-28): attribute assignment in the dormant
    # deterministic-plane paper adapter, constructed nowhere in production.
    ("execution/brokers/ibkr_paper.py", "attribute"),
}

#: Modules that may construct the quarantined adapter. Empty: none may.
_PERMITTED_CONSTRUCTORS: set[str] = set()

#: The actual broker-mutating calls, as ``(relative path, method)``. The M2
#: review pointed out that a file named "broker mutation inventory" which pins
#: only transmit *flags* inventories no mutation: the flag says an order is live,
#: the call is what reaches the venue. Both are pinned now.
_EXPECTED_MUTATION_SITES: set[tuple[str, str]] = {
    # The production adapter (ADR-0009). whatIf preview + the gated send + cancel.
    ("broker/official_ibkr.py", "placeOrder"),
    ("broker/official_ibkr.py", "cancelOrder"),
    # QUARANTINED deterministic-plane adapter (R-28), constructed nowhere.
    ("execution/brokers/ibkr_paper.py", "placeOrder"),
    ("execution/brokers/ibkr_paper.py", "cancelOrder"),
}

#: Names that mutate broker state. `exerciseOptions` and `reqGlobalCancel` are
#: listed although Chronos implements neither — if one ever appears it must fail
#: here rather than arrive unnoticed.
_MUTATING_METHODS = frozenset({"placeOrder", "cancelOrder", "exerciseOptions", "reqGlobalCancel"})


def _source_files() -> list[Path]:
    return sorted(path for path in _SRC.rglob("*.py") if "egg-info" not in str(path))


def _transmit_sites() -> set[tuple[str, str]]:
    """Find every transmit-enabling site, both spellings."""

    found: set[tuple[str, str]] = set()
    for path in _source_files():
        relative = str(path.relative_to(_SRC))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `foo(transmit=True)`
            if isinstance(node, ast.keyword) and node.arg == "transmit":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    found.add((relative, "keyword"))
            # `order.transmit = True` / `x.transmit: T = True`
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None or not (isinstance(value, ast.Constant) and value.value is True):
                    continue
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "transmit":
                        found.add((relative, "attribute"))
    return found


def test_the_complete_transmit_inventory_is_exactly_as_expected() -> None:
    actual = _transmit_sites()
    unexpected = actual - _EXPECTED_TRANSMIT_SITES
    missing = _EXPECTED_TRANSMIT_SITES - actual
    assert not unexpected, (
        f"NEW transmit site(s) appeared: {sorted(unexpected)}. chronos.orders is the single "
        "canonical execution plane (ADR-0016 §8) — a second path to a broker must be "
        "justified in an ADR before this set grows."
    )
    assert not missing, (
        f"expected transmit site(s) disappeared: {sorted(missing)} — update this inventory "
        "deliberately if a boundary genuinely moved."
    )


def test_only_the_orders_boundary_transmits_via_keyword() -> None:
    keyword_sites = {path for path, kind in _transmit_sites() if kind == "keyword"}
    assert keyword_sites == {"orders/submission.py"}


def _mutation_sites() -> set[tuple[str, str]]:
    """Every call to a broker-mutating method, anywhere in the package."""

    found: set[tuple[str, str]] = set()
    for path in _source_files():
        relative = str(path.relative_to(_SRC))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _MUTATING_METHODS:
                found.add((relative, name))
    return found


def test_the_complete_broker_mutation_inventory_is_exactly_as_expected() -> None:
    """The directive's 'complete broker-mutation inventory', enforced.

    A transmit flag marks an order live; the *call* is what reaches the venue.
    Pinning only the flag would let a new placeOrder site appear silently.
    """

    actual = _mutation_sites()
    unexpected = actual - _EXPECTED_MUTATION_SITES
    missing = _EXPECTED_MUTATION_SITES - actual
    assert not unexpected, (
        f"NEW broker-mutating call site(s): {sorted(unexpected)}. chronos.orders is the "
        "single canonical execution plane (ADR-0016 §8); a second path to a venue needs "
        "an ADR before this set grows."
    )
    assert not missing, (
        f"expected broker-mutating call site(s) disappeared: {sorted(missing)} — update "
        "this inventory deliberately if an adapter genuinely moved."
    )


def test_no_exercise_or_global_cancel_capability_exists() -> None:
    """Chronos implements neither; both would be unbounded-risk operations."""

    dangerous = {
        site for site in _mutation_sites() if site[1] in {"exerciseOptions", "reqGlobalCancel"}
    }
    assert dangerous == set(), f"an exercise/global-cancel capability appeared: {dangerous}"


def test_no_production_module_constructs_the_quarantined_adapter() -> None:
    """Structural isolation: the dormant second path has no runtime caller."""

    constructors: list[str] = []
    for path in _source_files():
        relative = str(path.relative_to(_SRC))
        if relative in _PERMITTED_CONSTRUCTORS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "IBKRPaperExecutionAdapter":
                constructors.append(f"{relative}:{node.lineno}")
    assert constructors == [], (
        f"the quarantined adapter is constructed in production code: {constructors}"
    )


def test_the_quarantined_adapter_refuses_construction_without_acknowledgement() -> None:
    """An accidental wiring must fail loudly here, not transmit quietly."""

    lock = ModeLock(
        mode=TradingMode.PAPER,
        capability=ExecutionCapability.PAPER_SUBMISSION,
        paper_account_id="DU1234567",
        denial_reasons=(),
    )
    with pytest.raises(BrokerSafetyError, match="QUARANTINED"):
        IBKRPaperExecutionAdapter(ib=object(), mode_lock=lock, port=7497)  # type: ignore[arg-type]

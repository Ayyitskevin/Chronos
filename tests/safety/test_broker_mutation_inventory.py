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
of the shipped tree, matching both spellings, pinned to an explicit expected set.
A new transmit site fails here whatever package it lives in and whichever syntax
it uses.

Two corrections from the M2 adversarial review, both of which had left the word
"complete" doing more work than the code did:

1. **Scope.** The scan covered only ``src/chronos``. ``scripts/`` ships in the
   repository and can import the broker stack, so a transmit-enabling line there
   was invisible. Both trees are scanned now.
2. **Values.** The matcher required a literal ``True``, so ``transmit=flag`` or
   ``order.transmit = not dry_run`` passed silently — the exact spelling someone
   reaching for a configurable send switch would write. Anything that is not a
   literal ``False`` is now inventoried as a *computed* site and must be declared,
   because a value the AST cannot evaluate is a value this test cannot clear.

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

_REPO = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO / "src" / "chronos"

#: Trees scanned for transmit and mutation sites. ``scripts/`` is here because it
#: ships with the repository and can import the broker stack; the M2 review found
#: the "repository-wide" claim covered only ``src/chronos``. Tests are excluded
#: deliberately — they construct fakes and assert on transmit flags, and a test
#: cannot reach a venue (no test may send an order, enforced elsewhere).
_SCANNED_TREES: tuple[Path, ...] = (_SRC, _REPO / "scripts")

#: Sites that ORIGINATE transmit authority — where a literal ``True`` enters the
#: system. This is the set ADR-0009's guarantee is about, and it must stay tiny.
#: Adding one is a deliberate, reviewable act: update this set and say why in the
#: ADR.
_ORIGINATING_TRANSMIT_SITES: set[tuple[str, str]] = {
    # The single audited live boundary (ADR-0009). Keyword argument.
    ("orders/submission.py", "keyword"),
    # QUARANTINED second site (R-28): attribute assignment in the dormant
    # deterministic-plane paper adapter, constructed nowhere in production.
    ("execution/brokers/ibkr_paper.py", "attribute"),
}

#: Sites that PROPAGATE an existing transmit value — a parameter passed through,
#: a broker-observed order mapped back into a domain model, a persisted record
#: rebuilt. None of these can turn a non-transmitting order into a transmitting
#: one; each carries a value some originating site already decided.
#:
#: These were invisible until the M2 review: the matcher required a literal
#: ``True``, so the entire chain between the boundary and the wire was unpinned.
#: They are inventoried rather than ignored because "propagates" is a claim about
#: the value's provenance, and a future edit could quietly make one of them
#: originate — which now fails here as an undeclared site.
_PROPAGATING_TRANSMIT_SITES: set[tuple[str, str]] = {
    # `to_order_request(transmit: bool = False)` — the parameter the single
    # originating keyword site above passes through. Defaults to False.
    ("orders/intent.py", "computed-keyword"),
    # Mapping a BROKER-OBSERVED order into a domain model during reconciliation:
    # reading what the venue already has, never authorizing a send.
    ("broker/ibkr.py", "computed-keyword"),
    ("broker/official_ibkr.py", "computed-keyword"),
    # `order.transmit = bool(request.transmit) and not what_if` — the one mapping
    # from request to IB order. Deliberately never a literal True, and `what_if`
    # forces it False so a preview cannot transmit.
    ("broker/official_ibkr.py", "computed-attribute"),
    # Rebuilding a persisted intent record; the value came from storage.
    ("services/reconciliation.py", "computed-keyword"),
}

#: The complete inventory: what originates plus what propagates.
_EXPECTED_TRANSMIT_SITES: set[tuple[str, str]] = (
    _ORIGINATING_TRANSMIT_SITES | _PROPAGATING_TRANSMIT_SITES
)

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
    """Every shipped Python file in the scanned trees.

    ``egg-info`` is filtered because an editable install writes generated Python
    into ``src/chronos.egg-info``; it is a build artifact, not source, and
    including it would make the inventory depend on how the package was installed.
    """

    files: list[Path] = []
    for tree in _SCANNED_TREES:
        if not tree.exists():
            continue
        files.extend(
            path
            for path in tree.rglob("*.py")
            if "egg-info" not in str(path) and "__pycache__" not in str(path)
        )
    return sorted(files)


def _relative(path: Path) -> str:
    return str(path.relative_to(_SRC if _SRC in path.parents else _REPO))


def _classify(value: ast.expr | None, spelling: str) -> str | None:
    """Name the kind of transmit site, or ``None`` when it provably sends nothing.

    A literal ``False`` is the only value that clears: anything the AST cannot
    evaluate to ``False`` might be ``True`` at runtime, and an inventory that
    assumed otherwise would be exactly as complete as its narrowest guess.
    """

    if value is None:
        return None
    if isinstance(value, ast.Constant):
        if value.value is False:
            return None
        return spelling if value.value is True else f"computed-{spelling}"
    return f"computed-{spelling}"


def _transmit_sites() -> set[tuple[str, str]]:
    """Find every transmit-enabling site: both spellings, literal or computed."""

    found: set[tuple[str, str]] = set()
    for path in _source_files():
        relative = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `foo(transmit=<anything but literal False>)`
            if isinstance(node, ast.keyword) and node.arg == "transmit":
                kind = _classify(node.value, "keyword")
                if kind is not None:
                    found.add((relative, kind))
            # `order.transmit = <...>` / `x.transmit: T = <...>`
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                kind = _classify(node.value, "attribute")
                if kind is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "transmit":
                        found.add((relative, kind))
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
        relative = _relative(path)
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
        relative = _relative(path)
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


def test_the_matcher_sees_computed_transmit_values() -> None:
    """Guard the guard: `transmit=True` is not the only way to send an order.

    The M2 review found the matcher required a literal `True`, so the spelling a
    developer would most plausibly reach for when adding a configurable send
    switch — `transmit=send_for_real` — was invisible to a test whose docstring
    promised completeness "whichever syntax it uses".
    """

    assert _classify(ast.parse("flag", mode="eval").body, "keyword") == "computed-keyword"
    assert _classify(ast.parse("not dry_run", mode="eval").body, "attribute") == (
        "computed-attribute"
    )
    assert _classify(ast.parse("True", mode="eval").body, "keyword") == "keyword"
    # Only a literal False clears, because only it provably sends nothing.
    assert _classify(ast.parse("False", mode="eval").body, "keyword") is None


def test_the_scan_covers_the_shipped_trees_not_just_the_package() -> None:
    """`scripts/` ships and can import the broker stack, so it is in scope."""

    scanned = {_relative(path) for path in _source_files()}
    assert "orders/submission.py" in scanned  # src/chronos, relative to the package
    assert any(name.startswith("scripts/") for name in scanned), (
        "scripts/ must be scanned: it ships with the repository and can import brokers"
    )
    assert not any("egg-info" in name for name in scanned)


def test_only_two_sites_originate_transmit_authority() -> None:
    """The guarantee ADR-0009 actually makes, stated over literals only.

    Propagating sites carry a decision; originating sites *make* one. Keeping the
    two sets apart is what lets the inventory grow with the plumbing without the
    audited claim quietly widening.
    """

    originating = {site for site in _transmit_sites() if not site[1].startswith("computed-")}
    assert originating == _ORIGINATING_TRANSMIT_SITES
    # And exactly one of them is reachable in production; the other is quarantined.
    assert ("orders/submission.py", "keyword") in originating

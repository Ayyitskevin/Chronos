"""The signal bridge holds nothing, and its restatements do not drift (ADR-0026).

Three separate guarantees, because the bridge buys its safety with three
separate structural choices and each can regress independently:

1. **Isolation.** An AST walk plus a subprocess import probe assert the package
   reaches no order, broker, execution, risk, persistence, supervisor, or
   database module — and, unusually for this repository, no ``chronos.autonomy``
   module either. The bridge emits candidate JSON; the ingress decides whether
   it is a proposal. See ``src/chronos/bridge/__init__.py`` for why the weaker
   non-importing position was chosen over an allowlist entry in
   ``test_autonomy_contracts.py``.

2. **No drift in the restated vocabulary.** Because the bridge cannot import the
   enums, it restates them. This file asserts every restatement is *equal* to
   the real thing, so adding a ``DecisionKind``, renaming a ``StrategyForm``, or
   reclassifying a kind as exposure-creating fails here until the bridge is
   updated with it. Without these, the duplication in
   ``chronos.bridge.vocabulary`` would be a slow-motion inert control: correct
   the day it was written and quietly wrong later.

3. **The backend does not depend on the bridge.** The dependency is one-way. If
   the app plane started importing this package, a webhook listener would have
   become part of the process that holds the broker connection, which is the
   whole thing this design refuses.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import chronos.bridge as bridge_pkg

_FORBIDDEN = (
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.orders",
    "chronos.api",
    "chronos.broker",
    "chronos.execution",
    "chronos.risk",
    "chronos.control",
    "chronos.persistence",
    "chronos.services",
    "chronos.service",
    "chronos.registry",
    "chronos.runtime",
    "chronos.ui",
    "chronos.terminal",
    "sqlalchemy",
    "sqlite3",
    "ib_async",
    "ibapi",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "chronos"


def _module_files() -> list[Path]:
    package_dir = Path(bridge_pkg.__file__).parent
    return sorted(package_dir.glob("*.py"))


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


# --------------------------------------------------------------------------- isolation


def test_the_package_contains_the_modules_this_file_guards() -> None:
    """Guard the guard: an empty glob would make every assertion below vacuous."""

    names = {path.stem for path in _module_files()}
    assert {"alert", "app", "config", "translate", "vocabulary", "__main__"} <= names


def test_bridge_modules_have_no_forbidden_ast_imports() -> None:
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}. The bridge holds no "
                    "Chronos capability; it emits JSON and the ingress judges it."
                )


def test_importing_the_bridge_leaks_no_forbidden_module() -> None:
    probe = (
        "import chronos.bridge, chronos.bridge.app, chronos.bridge.__main__, sys; "
        f"bad=[m for m in sys.modules if m.startswith({_FORBIDDEN!r})]; "
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
    assert leaked == [], f"importing chronos.bridge leaked forbidden modules: {leaked}"


def test_nothing_in_chronos_imports_the_bridge() -> None:
    """The dependency is one-way, and the direction is the safety property."""

    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.is_relative_to(_SRC / "bridge"):
            continue
        for name in _imported_names(path.read_text(encoding="utf-8")):
            if name == "chronos.bridge" or name.startswith("chronos.bridge."):
                importers.append(str(path.relative_to(_SRC)))
    assert importers == [], (
        "the bridge is a separate process that the backend must not depend on, but "
        f"{sorted(set(importers))} import it. A webhook listener inside the process that "
        "holds the broker connection is the design this package exists to avoid."
    )


# --------------------------------------------------------- the restatements do not drift


def test_restated_decision_vocabulary_matches_the_contract() -> None:
    from chronos.autonomy.enums import (
        DecisionDirection,
        DecisionKind,
        StrategyForm,
        TimeHorizon,
    )
    from chronos.bridge import vocabulary

    assert {member.value for member in DecisionKind} == vocabulary.DECISION_KINDS
    assert {member.value for member in DecisionDirection} == vocabulary.DIRECTIONS
    assert {member.value for member in StrategyForm} == vocabulary.STRATEGY_FORMS
    assert {member.value for member in TimeHorizon} == vocabulary.TIME_HORIZONS


def test_restated_kind_classifications_match_the_contract() -> None:
    from chronos.autonomy.enums import (
        EXPOSURE_CREATING_DECISION_KINDS,
        TARGETED_DECISION_KINDS,
    )
    from chronos.bridge import vocabulary

    assert {
        member.value for member in EXPOSURE_CREATING_DECISION_KINDS
    } == vocabulary.EXPOSURE_CREATING_KINDS
    assert {member.value for member in TARGETED_DECISION_KINDS} == vocabulary.TARGETED_KINDS


def test_restated_payload_rules_match_the_contract() -> None:
    """The two private frozensets the contract enforces payload coherence with."""

    from chronos.autonomy import decision as decision_module
    from chronos.bridge import vocabulary

    assert {member.value for member in decision_module._SIZELESS_KINDS} == vocabulary.SIZELESS_KINDS
    assert {member.value for member in decision_module._NO_ENTRY_KINDS} == vocabulary.NO_ENTRY_KINDS


def test_restated_symbol_alphabet_and_reference_pattern_match_the_contract() -> None:
    from chronos.autonomy import decision as decision_module
    from chronos.bridge import vocabulary

    assert vocabulary.SYMBOL_ALPHABET == decision_module._SYMBOL_ALPHABET
    assert vocabulary.CHRONOS_REFERENCE_PATTERN == decision_module._CHRONOS_REFERENCE_PATTERN


def test_the_restated_token_header_matches_the_backend() -> None:
    """A silently renamed header would make every forward 401 forever."""

    from chronos.api import auth
    from chronos.bridge.app import TOKEN_HEADER

    assert TOKEN_HEADER == auth._TOKEN_HEADER


def test_the_restated_proposer_header_matches_the_backend() -> None:
    """Same hazard, one credential over (ADR-0023): a silent rename on either
    side would make every registry-on forward 401 forever, with the bridge
    still green because its tests inject the forwarder."""

    from chronos.api import auth
    from chronos.bridge.app import PROPOSER_HEADER

    assert PROPOSER_HEADER == auth._PROPOSER_HEADER


def test_the_bridge_never_emits_a_naked_short_option_strategy() -> None:
    """ADR-0016 §6, restated where an alert author could otherwise reach it."""

    from chronos.bridge import vocabulary

    assert not [name for name in vocabulary.STRATEGY_FORMS if "NAKED" in name]
    assert not [name for name in vocabulary.STRATEGY_FORMS if name.startswith("SHORT_CALL")]
    assert not [name for name in vocabulary.STRATEGY_FORMS if name.startswith("SHORT_PUT")]


# ------------------------------------------------ narrative is copied, never inspected


def test_the_translator_never_inspects_narrative_content() -> None:
    """The bridge's own extension of ADR-0016 §5's rule.

    ``test_a_narrative_recorder_only_copies_it`` pins ``thesis`` and
    ``rationale``, because those are the names the contract uses. The alert's
    ``invalidation`` field is narrative of exactly the same kind, but it is
    spelled differently from the contract's ``invalidation_conditions``, so the
    name-based guard does not reach it. The gap is closed here rather than by
    renaming the field to fit the other test — the alert schema and the decision
    schema are genuinely different vocabularies, and bending one to match the
    other would hide the check rather than perform it.

    Copying and testing presence are allowed. Reading what the text *says* —
    indexing it, slicing it, comparing it to a string, folding it into
    arithmetic — is the first step of alert prose reaching an order parameter.
    """

    from chronos.bridge import translate

    source = Path(translate.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    narrative_fields = {"thesis", "rationale", "invalidation"}

    def _reads_narrative(node: ast.AST) -> bool:
        return isinstance(node, ast.Attribute) and node.attr in narrative_fields

    offenders: list[str] = []
    for node in ast.walk(tree):
        # Indexing or slicing narrative text.
        if isinstance(node, ast.Subscript) and _reads_narrative(node.value):
            offenders.append(f"line {node.lineno}: subscripts narrative")
        # Arithmetic on narrative text (string concatenation included).
        if isinstance(node, ast.BinOp) and (
            _reads_narrative(node.left) or _reads_narrative(node.right)
        ):
            offenders.append(f"line {node.lineno}: does arithmetic with narrative")
        # Comparing narrative to anything, or testing membership in it. A bare
        # `if alert.thesis:` is a presence guard and is not a Compare, so it
        # stays permitted here exactly as it is in the recorder test.
        if isinstance(node, ast.Compare) and (
            _reads_narrative(node.left) or any(_reads_narrative(item) for item in node.comparators)
        ):
            offenders.append(f"line {node.lineno}: compares narrative content")

    assert offenders == [], (
        "the TradingView translator inspects the content of alert narrative rather than "
        f"copying it: {offenders}. Free-form prose must never become an order parameter."
    )

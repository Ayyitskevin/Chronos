"""docs/limitations.md tells the truth about the autonomy counters and prompt injection.

At 4e7068e two bullets described a pre-M4 state as current: "Nothing counts *for* the
counters yet" (no production caller of ``record_activity``) and "explicit injection tests
are owed by M4" (EvidenceBundles "will be" redacted, versioned and hash-pinned). Since
ADR-0052 ``run_cycle`` durably reserves one order attempt and the sized turnover BEFORE
the order-plane handoff and releases it only on a disposition that proves nothing reached
the wire; ``EvidenceBundle`` is immutable, versioned, content-digested and
redaction-tripwired today, and the injection tests exist in
``tests/safety/test_model_tool_surface.py``. What remains true is named as the residual:
``record_equity`` still has no production caller, and injection is bounded, not prevented.

Pins in both directions, numbered to the DOC-7 contract: (1) the counter bullet and (2) the
injection bullet carry the accepted phrases and the old absolutes are absent from the WHOLE
document; (3) the source facts the prose rests on - the order of the calls inside
``run_cycle`` (by AST line, not by substring), the injection tests as pytest-collected
node ids (not a substring of the file), and the ``record_equity`` residual (no call
anywhere under ``src/``, with ``record_activity`` as the positive control).

(4) The r2 pins (Daybreak P1 + P2): the mandate pins provider/model/prompt/tool-schema/
decision-schema/policy - NOT a bundle version; evidence admission compares the bundle id
and digest the supervisor issued. And the release of the reservation is CONDITIONAL on the
handoff's typed disposition (``not counts_activity_attempt``) - pinned on the guard's own
shape, and behaviourally through the three safety tests the contract now names.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
LOOP = ROOT / "src" / "chronos" / "supervisor" / "loop.py"
ADMISSION = ROOT / "src" / "chronos" / "supervisor" / "admission.py"
MANDATE = ROOT / "src" / "chronos" / "autonomy" / "mandate.py"
SRC = ROOT / "src" / "chronos"
INJECTION_SUITE = "tests/safety/test_model_tool_surface.py"
HANDOFF_SUITE = "tests/safety/test_typed_handoff_outcomes_exercised.py"

COUNTERS_ANCHOR = "- **The activity counters are fed by the cycle; the equity counter is not.**"
INJECTION_ANCHOR = "- **Prompt injection is an open problem.**"

#: The explicit injection tests the bullet cites (R-30), as pytest collects them.
INJECTION_TESTS = (
    "test_injected_text_survives_into_evidence_marked_untrusted",
    "test_an_injected_size_is_still_clamped_by_the_mandate",
    "test_injected_narrative_changes_no_compiled_order_parameter",
    "test_injected_narrative_does_not_even_change_the_decision_id",
    "test_control_characters_in_injected_text_are_refused_at_the_contract",
    "test_no_deterministic_module_reads_a_bundle_text_body",
)

#: The behavioural pins of the typed release (r2, Daybreak P2): each drives run_cycle to
#: the handoff with a fake and asserts what was counted. Named here so the contract's
#: focused argv exercises them; a rename or deletion fails 4c.
HANDOFF_TESTS = (
    "test_a_refusal_before_the_wire_journals_handoff_and_counts_nothing",
    "test_an_ambiguous_send_journals_its_own_stage_counts_and_alerts_critical",
    "test_a_venue_rejection_after_the_send_journals_its_own_stage_and_warns",
)


def _whole_document() -> str:
    return " ".join(LIMITATIONS.read_text(encoding="utf-8").split())


def _bullet(anchor: str) -> str:
    """One markdown bullet, whitespace-collapsed: its anchor up to the next bullet, heading
    or blank line (the file writes each bullet as one paragraph)."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    assert "\n" + anchor in text, f"anchor not found: {anchor}"
    start = text.index("\n" + anchor) + 1
    stop = re.search(r"^(?:- |#{1,6} |$)", text[start + 1 :], re.M)
    assert stop is not None, anchor
    return " ".join(text[start : start + 1 + stop.start()].split())


# ----------------------------------------------------------- (1) the counter bullet


def test_1_counter_bullet_says_the_cycle_reserves_before_the_handoff() -> None:
    bullet = _bullet(COUNTERS_ANCHOR)
    for phrase in (
        "`run_cycle`",
        "`src/chronos/supervisor/loop.py`",
        "`record_activity`",
        "before the order-plane handoff",
        "ADR-0052",
        "`release_activity_reservation`",
        # the residual, in its own sentence
        "`record_equity`",
        "no production caller",
    ):
        assert phrase in bullet, phrase
    whole = _whole_document()
    for old in (
        "Nothing counts *for* the counters yet",
        "no production caller invokes them",
        "not yet *fed* by live trading",
    ):
        assert old not in whole, old


# ----------------------------------------------------------- (2) the injection bullet


def test_2_injection_bullet_is_present_tense_and_cites_the_tests() -> None:
    bullet = _bullet(INJECTION_ANCHOR)
    for phrase in (
        "`EvidenceBundle`",
        "carries a `bundle_version` and is content-digested",
        "compares the bundle id and digest the supervisor issued",
        "`redaction_violations`",
        "`TextualEvidence`",
        "`untrusted`",
        f"`{INJECTION_SUITE}`",
        "`test_injected_narrative_changes_no_compiled_order_parameter`",
        # the residual, in its own sentence
        "The deterministic kernel is the control that holds when injection succeeds",
    ):
        assert phrase in bullet, phrase
    whole = _whole_document()
    for old in (
        "tests are owed by M4",
        "will be redacted, versioned, and hash-pinned",
        # r2: the mandate pins versions, not the bundle
        "`bundle_version` the mandate pins",
    ):
        assert old not in whole, old


# ----------------------------------------------------------- (3) the source facts


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _calls_in(node: ast.AST, names: set[str]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {name: [] for name in names}
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name in found:
                found[name].append(child.lineno)
    return found


def test_3a_run_cycle_reserves_activity_before_the_handoff_and_releases_after() -> None:
    tree = ast.parse(LOOP.read_text(encoding="utf-8"))
    run_cycle = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_cycle"
    )
    calls = _calls_in(run_cycle, {"record_activity", "submit", "release_activity_reservation"})
    # exactly one reservation and exactly one handoff inside the cycle: the order is
    # then a statement about THE reservation and THE handoff, not about some pair
    assert len(calls["record_activity"]) == 1, calls
    assert len(calls["submit"]) == 1, calls
    assert len(calls["release_activity_reservation"]) == 1, calls
    (reserve,) = calls["record_activity"]
    (handoff,) = calls["submit"]
    (release,) = calls["release_activity_reservation"]
    # ADR-0052: the attempt is spent BEFORE the handoff can reach the wire, and the
    # release is judged from the handoff's typed disposition AFTER it answered
    assert reserve < handoff < release, calls


def _collected_node_ids(suite: str) -> set[str]:
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", suite],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    return {line.strip() for line in collected.stdout.splitlines() if "::" in line}


def test_3b_the_injection_tests_exist_as_collected_pytest_items() -> None:
    node_ids = _collected_node_ids(INJECTION_SUITE)
    for name in INJECTION_TESTS:
        assert f"{INJECTION_SUITE}::{name}" in node_ids, (name, sorted(node_ids))


def test_3c_record_equity_has_no_production_caller_and_record_activity_has_one() -> None:
    callers: dict[str, list[str]] = {"record_equity": [], "record_activity": []}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, lines in _calls_in(tree, set(callers)).items():
            callers[name].extend(f"{path.relative_to(ROOT)}:{line}" for line in lines)
    # the residual the bullet names: nothing under src/ feeds the equity counter
    assert callers["record_equity"] == [], callers
    # positive control: the same walk finds the cycle's activity reservation
    assert any(
        caller.startswith("src/chronos/supervisor/loop.py:")
        for caller in callers["record_activity"]
    ), callers


# ----------------------------------------------------------- (4) the r2 pins


def test_4a_the_mandate_pins_versions_not_the_bundle() -> None:
    from chronos.autonomy.mandate import VersionPins

    # the exact pin set: a bundle version is not among them (Daybreak P1)
    assert set(VersionPins.model_fields) == {
        "provider",
        "model_id",
        "model_version",
        "prompt_version",
        "tool_schema_version",
        "decision_schema_version",
        "policy_version",
    }
    # and neither the mandate nor admission names a bundle version anywhere (by AST, not text)
    for path in (MANDATE, ADMISSION):
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        assert "bundle_version" not in names, path


def _compared_attribute_pairs(function: ast.FunctionDef) -> set[tuple[str, str]]:
    """Every ``a != b`` in the function as (left, right) names/attributes."""

    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(function):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
            continue
        if not isinstance(node.ops[0], ast.NotEq):
            continue
        sides = []
        for side in (node.left, node.comparators[0]):
            if isinstance(side, ast.Attribute):
                sides.append(side.attr)
            elif isinstance(side, ast.Name):
                sides.append(side.id)
            else:
                sides.append("?")
        pairs.add((sides[0], sides[1]))
    return pairs


def test_4b_evidence_admission_compares_the_issued_bundle_id_and_digest() -> None:
    tree = ast.parse(ADMISSION.read_text(encoding="utf-8"))
    check = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_check_evidence_bundle"
    )
    pairs = _compared_attribute_pairs(check)
    assert ("evidence_bundle_id", "expected_id") in pairs, pairs
    assert ("evidence_bundle_digest", "expected_digest") in pairs, pairs


def test_4c_the_release_is_guarded_by_not_counts_activity_attempt() -> None:
    tree = ast.parse(LOOP.read_text(encoding="utf-8"))
    run_cycle = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_cycle"
    )
    guards = [
        node
        for node in ast.walk(run_cycle)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Call) and _call_name(inner) == "release_activity_reservation"
            for inner in ast.walk(node)
        )
    ]
    assert len(guards) == 1, "the release must sit under exactly one if"
    test = guards[0].test
    # the guard's own shape: `if not <handoff_result>.counts_activity_attempt:` - the
    # inversion (`if <...>.counts_activity_attempt:`) is the mutation this catches
    assert isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not), ast.dump(test)
    assert (
        isinstance(test.operand, ast.Attribute) and test.operand.attr == "counts_activity_attempt"
    ), ast.dump(test)
    # and the release is the ONLY statement under that guard
    assert len(guards[0].body) == 1 and not guards[0].orelse, ast.dump(guards[0])
    # behaviourally: the three safety tests that drive run_cycle to the handoff and count
    # what was spent are collected under the names the contract cites
    node_ids = _collected_node_ids(HANDOFF_SUITE)
    for name in HANDOFF_TESTS:
        assert f"{HANDOFF_SUITE}::{name}" in node_ids, (name, sorted(node_ids))


# ------------------------------------ 4. the M5 bullet tells the SAME counting story (DOC-9)

M5_ANCHOR = "- **The session counters M3 built are finally fed.**"


def test_4_the_m5_bullet_states_the_same_reserve_before_handoff_rule() -> None:
    """COMPOSE-1 review P1: the composed document told two stories — the M5 bullet said
    "Counting happens at *handoff*" and that a completed cycle advances the counters, while
    the counters bullet (and `run_cycle`) reserve BEFORE the handoff and settle by the typed
    disposition afterwards. One story now, and the old forms appear nowhere."""

    bullet = _bullet(M5_ANCHOR)
    accepted = (
        "reserves one order attempt and the sized turnover *before* the order-plane handoff",
        "typed disposition settles it afterwards",
        "a refusal that proves nothing reached the wire releases the reservation",
        "a raise, an unconfirmed send or a venue rejection keeps it",
        "bounds what the system **attempts**",
        "still consumed an attempt",
        "retry without limit",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert re.search(r"\.py:\d", bullet) is None, "no line numbers in prose"
    whole = _whole_document()
    assert "Counting happens at *handoff*" not in whole, "the at-handoff story must not survive"
    assert re.search(r"completed cycle advances", whole) is None, "the bare advance form is gone"

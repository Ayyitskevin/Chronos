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
SRC = ROOT / "src" / "chronos"
INJECTION_SUITE = "tests/safety/test_model_tool_surface.py"

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
        "`bundle_version`",
        "digest",
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


def test_3b_the_injection_tests_exist_as_collected_pytest_items() -> None:
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            INJECTION_SUITE,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = {line.strip() for line in collected.stdout.splitlines() if "::" in line}
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

"""No Five-Tool contract may carry an undisclosed unread risk/exit/provenance field.

AGENTS.md:29-30: "Every economic-looking field must be mechanically enforced, explicitly
advisory, or forbidden. Inert authority, risk, exit, or protection fields are release
blockers."  The four kernel defects (R-24..R-27) were all fields and controls that looked
enforced and were not.

Merge-time review of ``codex/five-tool-confluence-v36`` found Five-Tool contract fields
that the research plane writes but never reads back: ~~the trace's lookahead-provenance
identifiers, and~~ (**corrected 2026-08-09:** those three identifiers are now read — see
the ``FiveToolTrace`` note below) most of ``PositionPlan``/``QuantityPlan`` — including the
exit field ``initial_stop_price``.  None of them can reach an order: the whole package is
import-isolated from the order plane (``test_five_tool_isolation.py``) and its campaign
refuses all data access (``test_five_tool_holdout_refusal_exercised.py``,
``tests/unit/test_five_tool_trials.py``).  So this is a disclosure obligation, not a live
defect — but the disclosure has to be mechanical, because the failure this repo keeps
repeating is a field everyone assumed something read.

This file pins the exact unread set.  Adding a field that nothing reads fails here until
it is disclosed below; making a disclosed field read also fails here, so the disclosure
cannot quietly go stale.

**Honest residual — read this before trusting a READ verdict.**  The scanner matches
attribute *names* across the research plane, not attribute accesses resolved to a type.
A field named like one read on some other object (``side``, ``quantity``, ``legs``,
``timestamp_utc``) is therefore scored read whether or not anything reads it *on this
contract*.  The scanner over-reports reads, so this test is a floor on the unread set,
never a proof that a field marked read is genuinely consumed.  It exists to stop a NEW
undisclosed field arriving, which is the regression that actually recurs here.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from chronos.research.five_tool.models import FiveToolTrace
from chronos.research.five_tool.planning import (
    PlannedLeg,
    PositionPlan,
    QuantityPlan,
)

_RESEARCH_PLANE = Path(__file__).resolve().parents[2] / "src" / "chronos" / "research"

# Fields the research plane writes and never reads back, as of the five-tool integration.
# Each entry is a disclosure, not an endorsement.
_DISCLOSED_UNREAD: dict[type, frozenset[str]] = {
    # ~~Lookahead-provenance identity carried on every bar's trace.  The preregistration
    # (docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md, "Common campaign tests" item 10) requires a
    # timestamp audit and no-lookahead tests before economic statistics are considered.
    # The engine populates these ids (engine.py, the FiveToolTrace construction) but no
    # code or test reads them, so today they are an audit trail nothing audits.  Whole-
    # trace equality in the parity/determinism tests detects nondeterminism only; two runs
    # of the same code agree on a wrong id just as readily as on a right one.~~
    #
    # **Corrected 2026-08-09 (Track B.3).**  ``primary_sequence_id``,
    # ``benchmark_source_id``, and ``htf_source_id`` LEFT this disclosure because they
    # became read, not because the sentence was edited.  This test fired in its designed
    # second direction when `chronos.research.five_tool.provenance` landed — "FiveToolTrace
    # field(s) ['benchmark_source_id', 'htf_source_id', 'primary_sequence_id'] are now
    # read; remove them from the disclosure so it keeps describing the real state" — and
    # that failure is the evidence the wiring is real rather than announced.
    # ``audit_trace_provenance`` reads all three: it resolves each to a full venue-qualified
    # series, refuses a role whose series changes mid-run, and refuses a companion bar that
    # had not closed when its primary bar did.  Exercised end to end over real engine output
    # in tests/safety/test_five_tool_provenance_audit_exercised.py.  The remaining entries
    # below are unchanged and still unread.
    # **Corrected 2026-08-18.** ``events`` left this disclosure because
    # ``signal_replay`` iterates ``trace.events``.
    #
    # **Corrected 2026-08-21 (un-strand of PRs #74/#75).**  The last four entries —
    # ``bar_index``, ``warmup_blockers``, ``long_setup``, ``short_setup`` — LEFT this
    # disclosure because they became read, not because the sentence was edited.
    # ``research/features/advisory_export.py`` (:87-91), which arrives with the
    # refuse-closed pairing sidecar in ``59e3bd0``, projects all four into the advisory
    # pack.  The guard fired in its designed second direction on that merge —
    # "FiveToolTrace field(s) ['bar_index', 'long_setup', 'short_setup',
    # 'warmup_blockers'] are now read" — and that failure is the evidence the sidecar
    # consumes the trace rather than announcing that it does.
    #
    # ``FiveToolTrace`` now discloses nothing: every field on the contract is read
    # somewhere in the research plane.  The key stays so the guard keeps running in its
    # FIRST direction — a field added later that nothing reads must still be classified
    # and disclosed here rather than passing unnoticed.
    FiveToolTrace: frozenset(),
    # The translated Pine sizing/stop geometry.  ``planning`` is not exported from
    # ``chronos.research.five_tool.__init__``; the only in-package importer,
    # ``validation.py``, imports the ExitReason/LegId/PositionSide enums and nothing else,
    # so these outputs are consumed exclusively by tests/unit/test_five_tool_planning.py.
    # ``initial_stop_price`` / ``risk_distance`` LEFT this disclosure 2026-08-18:
    # ``signal_replay`` now reads both (leg/runner stop and R-multiple). Remaining
    # quantity fields are still unread on this contract by name-scan.
    PositionPlan: frozenset(
        {
            "requested_quantity",
            "planned_quantity",
            "unallocated_quantity",
        }
    ),
    QuantityPlan: frozenset({"risk_quantity", "cap_quantity", "raw_quantity"}),
    PlannedLeg: frozenset(),
}


def _names_read_in_research_plane(extra_source: str = "") -> set[str]:
    """Every attribute name the research plane loads, plus literal ``getattr`` names."""

    names: set[str] = set()
    sources = [path.read_text(encoding="utf-8") for path in sorted(_RESEARCH_PLANE.rglob("*.py"))]
    if extra_source:
        sources.append(extra_source)
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
    return names


@pytest.mark.parametrize("contract", list(_DISCLOSED_UNREAD))
def test_every_unread_contract_field_is_disclosed(contract: type) -> None:
    read_names = _names_read_in_research_plane()
    declared = {field.name for field in dataclasses.fields(contract)}
    observed_unread = {name for name in declared if name not in read_names}
    disclosed = set(_DISCLOSED_UNREAD[contract])

    undisclosed = sorted(observed_unread - disclosed)
    assert not undisclosed, (
        f"{contract.__name__} gained field(s) {undisclosed} that nothing in the research "
        "plane reads. Classify and disclose them here, or remove them (AGENTS.md:29-30)."
    )
    stale = sorted(disclosed - observed_unread)
    assert not stale, (
        f"{contract.__name__} field(s) {stale} are now read; remove them from the "
        "disclosure so it keeps describing the real state."
    )


def test_the_disclosure_covers_only_fields_that_exist() -> None:
    for contract, disclosed in _DISCLOSED_UNREAD.items():
        declared = {field.name for field in dataclasses.fields(contract)}
        assert disclosed <= declared, (
            f"{contract.__name__} disclosure names fields that no longer exist: "
            f"{sorted(disclosed - declared)}"
        )


def test_the_scanner_sees_a_planted_read() -> None:
    """Guard the guard: a scanner that quietly stops finding things is the same defect."""

    planted = "initial_stop_price_probe_that_no_contract_declares"
    assert planted not in _names_read_in_research_plane()
    assert planted in _names_read_in_research_plane(extra_source=f"value = plan.{planted}\n")
    assert planted in _names_read_in_research_plane(
        extra_source=f"value = getattr(plan, {planted!r})\n"
    )

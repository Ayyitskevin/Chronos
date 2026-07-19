"""Deterministic per-script port disposition (AI Quant plan B2).

Assigns every Pine script a port disposition — PORTED / DEFERRED / BLOCKED_ON /
REJECTED — plus a machine-readable :class:`DispositionReason` and a short human
detail. The classification is a pure function of the **clean categoricals** the
forensic audit produced (integrity status, declared classification, trade
direction) and whether a canonical spec already derives the script. It never
reads prose, so it is reproducible and reviewable; a script the rules cannot
place stays UNCLASSIFIED rather than being guessed.

Rules, in priority order:

1. A spec derives it                         → PORTED (ported_to_spec)
2. integrity REQUIRES_REWRITE                → BLOCKED_ON (requires_rewrite)
3. integrity NON_EXECUTABLE_INDICATOR        → REJECTED (non_executable_indicator)
4. (executable, not ported) strategy_addon   → REJECTED (strategy_addon_not_standalone)
5. (executable, not ported) standalone
   strategy with a trade direction           → DEFERRED (executable_strategy_not_yet_ported)
6. anything else executable
   (indicator/study with a direction, …)     → REJECTED (not_a_standalone_strategy)
"""

from __future__ import annotations

from chronos.skb.schema import (
    Classification,
    Direction,
    Disposition,
    DispositionReason,
    IntegrityStatus,
    PineScriptEntry,
)

_REASON_DETAIL: dict[DispositionReason, str] = {
    DispositionReason.PORTED_TO_SPEC: "a canonical spec derives this script",
    DispositionReason.REQUIRES_REWRITE: (
        "the forensic audit flagged REQUIRES_REWRITE — cannot be assessed as a "
        "strategy until rewritten"
    ),
    DispositionReason.NON_EXECUTABLE_INDICATOR: (
        "a non-executable indicator/study/readout — produces signals, not orders"
    ),
    DispositionReason.STRATEGY_ADDON_NOT_STANDALONE: (
        "a strategy add-on (journaling/risk overlay) — augments a strategy, not standalone"
    ),
    DispositionReason.EXECUTABLE_STRATEGY_NOT_YET_PORTED: (
        "an executable standalone strategy with a trade direction; portable, not yet ported"
    ),
    DispositionReason.NOT_A_STANDALONE_STRATEGY: (
        "executable but not a standalone strategy (indicator/study surface)"
    ),
}


def classify_disposition(
    entry: PineScriptEntry, *, is_ported: bool
) -> tuple[Disposition, DispositionReason, str]:
    """Return ``(disposition, reason, detail)`` for one script. Deterministic."""

    reason = _reason(entry, is_ported=is_ported)
    disposition = _DISPOSITION_OF[reason]
    return disposition, reason, _REASON_DETAIL[reason]


_DISPOSITION_OF: dict[DispositionReason, Disposition] = {
    DispositionReason.PORTED_TO_SPEC: Disposition.PORTED,
    DispositionReason.REQUIRES_REWRITE: Disposition.BLOCKED_ON,
    DispositionReason.NON_EXECUTABLE_INDICATOR: Disposition.REJECTED,
    DispositionReason.STRATEGY_ADDON_NOT_STANDALONE: Disposition.REJECTED,
    DispositionReason.EXECUTABLE_STRATEGY_NOT_YET_PORTED: Disposition.DEFERRED,
    DispositionReason.NOT_A_STANDALONE_STRATEGY: Disposition.REJECTED,
}


def _reason(entry: PineScriptEntry, *, is_ported: bool) -> DispositionReason:
    if is_ported:
        return DispositionReason.PORTED_TO_SPEC
    if entry.integrity_status is IntegrityStatus.REQUIRES_REWRITE:
        return DispositionReason.REQUIRES_REWRITE
    if entry.integrity_status is IntegrityStatus.NON_EXECUTABLE_INDICATOR:
        return DispositionReason.NON_EXECUTABLE_INDICATOR
    # Remaining: integrity PASS_WITH_CONSTRAINTS and not ported.
    if entry.classification is Classification.STRATEGY_ADDON:
        return DispositionReason.STRATEGY_ADDON_NOT_STANDALONE
    if entry.classification is Classification.STRATEGY and entry.direction is not Direction.NONE:
        return DispositionReason.EXECUTABLE_STRATEGY_NOT_YET_PORTED
    return DispositionReason.NOT_A_STANDALONE_STRATEGY

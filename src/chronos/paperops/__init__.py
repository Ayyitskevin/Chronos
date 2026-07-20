"""Paper-trading operations: decision ledger, replay, data health, controls.

Research readiness correctly reports paper is not scientifically ready
(INSUFFICIENT_EVIDENCE). This package is the **operational** layer for when a
paper session runs: every decision is recorded, replayable, and reviewable.

Live transmission is never enabled here. Pure functions stay free of broker I/O.
"""

from chronos.paperops.controls import PaperControlDecision, evaluate_paper_controls
from chronos.paperops.data_quality import PaperDataHealth, evaluate_paper_quote
from chronos.paperops.decision import (
    PaperDecisionInput,
    PaperDecisionResult,
    evaluate_paper_decision,
)
from chronos.paperops.ledger import DecisionLedger, DecisionLedgerError, verify_decision_ledger
from chronos.paperops.reasons import DecisionKind, PaperReasonCode
from chronos.paperops.records import DecisionRecord
from chronos.paperops.replay import ReplayReport, replay_ledger
from chronos.paperops.review import OperatorReview, build_operator_review

__all__ = [
    "DecisionKind",
    "DecisionLedger",
    "DecisionLedgerError",
    "DecisionRecord",
    "OperatorReview",
    "PaperControlDecision",
    "PaperDataHealth",
    "PaperDecisionInput",
    "PaperDecisionResult",
    "PaperReasonCode",
    "ReplayReport",
    "build_operator_review",
    "evaluate_paper_controls",
    "evaluate_paper_decision",
    "evaluate_paper_quote",
    "replay_ledger",
    "verify_decision_ledger",
]

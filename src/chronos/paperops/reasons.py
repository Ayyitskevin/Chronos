"""Stable reason codes and event kinds for the paper decision ledger."""

from __future__ import annotations

from enum import StrEnum


class DecisionKind(StrEnum):
    """What kind of paper-plane event is being recorded."""

    CANDIDATE_SIGNAL = "candidate_signal"
    REJECTION = "rejection"
    PROPOSED_ORDER = "proposed_order"
    RISK_DECISION = "risk_decision"
    PAPER_FILL = "paper_fill"
    STATE_TRANSITION = "state_transition"
    DATA_HEALTH = "data_health"
    CONTROL_REFUSAL = "control_refusal"
    SESSION_MARKER = "session_marker"


class PaperReasonCode(StrEnum):
    """Machine-stable reason codes. Do not rephrase existing values."""

    # Outcomes / info
    RECORDED = "RECORDED"
    SIGNAL_CONSIDERED = "SIGNAL_CONSIDERED"
    ORDER_PROPOSED = "ORDER_PROPOSED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_DENIED = "RISK_DENIED"
    FILL_RECORDED = "FILL_RECORDED"
    STATE_CHANGED = "STATE_CHANGED"

    # Data quality (blocks trade-permitting opens)
    DATA_STALE = "DATA_STALE"
    DATA_MISSING = "DATA_MISSING"
    DATA_CROSSED = "DATA_CROSSED"
    DATA_NONSENSICAL = "DATA_NONSENSICAL"
    DATA_INVALID_GREEKS = "DATA_INVALID_IV_OR_GREEKS"
    DATA_CLOCK_ANOMALY = "DATA_CLOCK_ANOMALY"
    DATA_DEGRADED_LABELED = "DATA_DEGRADED_LABELED"
    DATA_QUALITY_UNKNOWN = "DATA_QUALITY_UNKNOWN"
    DATA_OK = "DATA_OK"

    # Portfolio / session controls
    HALTED = "HALTED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    POSITION_LIMIT = "POSITION_LIMIT"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    CONTROLS_OK = "CONTROLS_OK"

    # Ledger / replay integrity
    LEDGER_CORRUPT = "LEDGER_CORRUPT"
    LEDGER_INCOMPLETE = "LEDGER_INCOMPLETE"
    REPLAY_MATCH = "REPLAY_MATCH"
    REPLAY_MISMATCH = "REPLAY_MISMATCH"

    # Live remains blocked from paper ops surface
    LIVE_TRADING_BLOCKED = "LIVE_TRADING_BLOCKED"


class DecisionOutcome(StrEnum):
    """Whether this event authorizes a paper open (or is informational)."""

    ALLOW = "allow"  # trade-permitting only when data+controls+risk all allow
    DENY = "deny"
    INFORMATIONAL = "informational"

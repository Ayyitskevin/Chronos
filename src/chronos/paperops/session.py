"""Paper session helper: evaluate a decision and append it to the ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from chronos.paperops.control_memory import (
    apply_durable_control_memory,
    rehydrate_control_memory,
)
from chronos.paperops.decision import (
    PaperDecisionInput,
    PaperDecisionResult,
    evaluate_paper_decision,
)
from chronos.paperops.ledger import DecisionEvent, DecisionLedger
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.records import DecisionRecord


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    result: PaperDecisionResult
    record: DecisionRecord
    input_used: PaperDecisionInput


def record_paper_decision(
    ledger: DecisionLedger,
    inp: PaperDecisionInput,
    *,
    at_utc: str | None = None,
    extra_payload: dict[str, object] | None = None,
    rehydrate_controls: bool = True,
) -> RecordedDecision:
    """Evaluate inputs, append a ledger row, return result + record.

    When ``rehydrate_controls`` is True (default), durable duplicate/cooldown
    memory is loaded from the ledger and merged into ``inp`` before evaluation
    so a process restart cannot re-authorize a previously recorded open.
    """

    effective = inp
    durable_payload: dict[str, object] | None = None
    if rehydrate_controls:
        memory = rehydrate_control_memory(ledger.path)
        effective = apply_durable_control_memory(inp, memory)
        durable_payload = memory.to_payload()

    result = evaluate_paper_decision(effective)
    payload: dict[str, object] = {
        "may_open": result.may_open,
        "explanations": list(result.explanations),
        "decision_inputs": result.decision_inputs,
        "data_health": result.data_health.to_payload(),
        "controls": result.controls.to_payload(),
        # Always persist the order fingerprint at top level for rehydration
        # even if a future payload shape omits nested decision_inputs fields.
        "order_fingerprint": effective.order_fingerprint
        or result.decision_inputs.get("order_fingerprint")
        or "",
    }
    if durable_payload is not None:
        payload["durable_control_memory_before"] = durable_payload
    if extra_payload:
        payload.update(extra_payload)

    kind = result.kind
    outcome = DecisionOutcome.ALLOW if result.may_open else DecisionOutcome.DENY

    event = DecisionEvent(
        kind=kind,
        reason_code=result.primary_reason,
        outcome=outcome,
        strategy_id=effective.strategy_id,
        strategy_version=effective.strategy_version,
        config_hash=effective.config_hash,
        data_timestamp_utc=result.data_health.data_timestamp_utc,
        data_source=result.data_health.data_source,
        data_quality_label=result.data_health.label,
        decision_inputs=result.decision_inputs,
        payload=payload,
    )
    record = ledger.append(event, at_utc=at_utc or datetime.now(tz=UTC).isoformat())
    return RecordedDecision(result=result, record=record, input_used=effective)


def record_session_marker(
    ledger: DecisionLedger,
    *,
    strategy_id: str,
    strategy_version: str,
    config_hash: str,
    note: str,
    at_utc: str | None = None,
) -> DecisionRecord:
    """Informational session boundary (start/end) for operator review."""

    event = DecisionEvent(
        kind=DecisionKind.SESSION_MARKER,
        reason_code=PaperReasonCode.RECORDED,
        outcome=DecisionOutcome.INFORMATIONAL,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config_hash=config_hash,
        data_timestamp_utc=None,
        data_source="session",
        data_quality_label="N/A",
        decision_inputs={"note": note},
        payload={"note": note},
    )
    return ledger.append(event, at_utc=at_utc)

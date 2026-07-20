"""Paper session helper: evaluate a decision and append it to the ledger."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from chronos.paperops.control_memory import (
    DurableControlMemory,
    apply_durable_control_memory,
    rehydrate_control_memory,
)
from chronos.paperops.decision import (
    PaperDecisionInput,
    PaperDecisionResult,
    bind_effective_order_fingerprint,
    evaluate_paper_decision,
)
from chronos.paperops.ledger import DecisionEvent, DecisionLedger, decision_ledger_lock
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

    Critical section (exclusive ledger lock):
      rehydrate durable control memory → bind effective order fingerprint →
      evaluate → append.

    Holding the lock across the whole path prevents two concurrent callers with
    the same order identity from both observing an empty memory and both
    allowing. Empty ``order_fingerprint`` is bound to the content hash and that
    value is what is persisted for restart rehydration.
    """

    with decision_ledger_lock(ledger.path):
        durable_payload: dict[str, object] | None = None
        effective = inp
        if rehydrate_controls:
            memory = rehydrate_control_memory(ledger.path)
            effective = apply_durable_control_memory(inp, memory)
            durable_payload = memory.to_payload()
        else:
            memory = DurableControlMemory(
                recent_order_fingerprints=(),
                last_order_at_utc=None,
                record_count=0,
            )

        # Bind before evaluate so controls + payload share one durable identity.
        effective = bind_effective_order_fingerprint(effective)
        result = evaluate_paper_decision(effective)
        # Prefer the evaluate result (always non-empty) for persistence.
        durable_fp = (
            result.effective_order_fingerprint
            or effective.order_fingerprint
            or result.inputs_fingerprint
        )

        # Defense in depth: if rehydrate saw this fingerprint mid-flight was
        # empty but evaluate somehow allowed, still fail closed when the
        # fingerprint is already durable (should not happen under the lock).
        if (
            rehydrate_controls
            and result.may_open
            and durable_fp in memory.recent_order_fingerprints
        ):
            # Force a deny re-evaluation with the fingerprint already present.
            forced = replace(
                effective,
                recent_order_fingerprints=tuple(
                    dict.fromkeys(
                        list(memory.recent_order_fingerprints)
                        + list(effective.recent_order_fingerprints)
                        + [durable_fp]
                    )
                ),
            )
            result = evaluate_paper_decision(forced)
            effective = forced
            durable_fp = result.effective_order_fingerprint or durable_fp

        payload: dict[str, object] = {
            "may_open": result.may_open,
            "explanations": list(result.explanations),
            "decision_inputs": result.decision_inputs,
            "data_health": result.data_health.to_payload(),
            "controls": result.controls.to_payload(),
            # Always persist the *effective* control identity (never empty).
            "order_fingerprint": durable_fp,
            "effective_order_fingerprint": durable_fp,
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
        record = ledger.append_under_held_lock(
            event, at_utc=at_utc or datetime.now(tz=UTC).isoformat()
        )
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

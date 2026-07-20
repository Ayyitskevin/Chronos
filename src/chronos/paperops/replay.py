"""Deterministic replay of paper decisions from the decision ledger.

Re-evaluates each recorded ``decision_inputs`` payload through
``evaluate_paper_decision`` and flags mismatches. Incomplete or corrupt
ledgers fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chronos.paperops.decision import PaperDecisionInput, evaluate_paper_decision
from chronos.paperops.ledger import DecisionLedgerError, load_and_verify
from chronos.paperops.reasons import DecisionOutcome, PaperReasonCode
from chronos.paperops.records import DecisionRecord


@dataclass(frozen=True, slots=True)
class ReplayMismatch:
    sequence: int
    field: str
    recorded: object
    replayed: object
    detail: str


@dataclass(frozen=True, slots=True)
class ReplayReport:
    ok: bool
    records_replayed: int
    matches: int
    mismatches: tuple[ReplayMismatch, ...]
    reason_code: PaperReasonCode
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "records_replayed": self.records_replayed,
            "matches": self.matches,
            "mismatch_count": len(self.mismatches),
            "mismatches": [
                {
                    "sequence": m.sequence,
                    "field": m.field,
                    "recorded": m.recorded,
                    "replayed": m.replayed,
                    "detail": m.detail,
                }
                for m in self.mismatches
            ],
            "reason_code": self.reason_code.value,
            "detail": self.detail,
        }


_REPLAYABLE_KINDS = frozenset(
    {
        "candidate_signal",
        "rejection",
        "proposed_order",
        "risk_decision",
        "data_health",
        "control_refusal",
    }
)


def _input_from_payload(payload: dict[str, Any], record: DecisionRecord) -> PaperDecisionInput:
    """Rebuild PaperDecisionInput from a recorded decision_inputs blob."""

    # Prefer nested decision_inputs; fall back to top-level payload fields.
    raw = payload.get("decision_inputs")
    if not isinstance(raw, dict):
        raw = payload
    positions = raw.get("position_notional_by_symbol") or {}
    if not isinstance(positions, dict):
        raise DecisionLedgerError(
            f"sequence {record.sequence}: position_notional_by_symbol must be an object"
        )
    fingerprints = raw.get("recent_order_fingerprints") or ()
    if isinstance(fingerprints, (list, tuple)):
        fingerprints = tuple(str(x) for x in fingerprints)
    else:
        raise DecisionLedgerError(
            f"sequence {record.sequence}: recent_order_fingerprints must be a list"
        )

    # strategy fields may live on the record itself
    strategy_id = str(raw.get("strategy_id") or record.strategy_id)
    strategy_version = str(raw.get("strategy_version") or record.strategy_version)
    config_hash = str(raw.get("config_hash") or record.config_hash)
    if not strategy_id or not strategy_version or not config_hash:
        raise DecisionLedgerError(f"sequence {record.sequence}: incomplete provenance for replay")
    now_utc = str(raw.get("now_utc") or "")
    if not now_utc:
        raise DecisionLedgerError(
            f"sequence {record.sequence}: missing now_utc in decision_inputs (incomplete)"
        )

    return PaperDecisionInput(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config_hash=config_hash,
        symbol=str(raw.get("symbol") or ""),
        side=str(raw.get("side") or "BUY"),
        quantity=int(raw.get("quantity") or 0),
        limit_price=float(raw.get("limit_price") or 0.0),
        bid=raw.get("bid"),
        ask=raw.get("ask"),
        last=raw.get("last"),
        quote_utc=raw.get("quote_utc"),
        data_source=str(raw.get("data_source") or record.data_source),
        quality_label=str(raw.get("quality_label") or record.data_quality_label),
        iv=raw.get("iv"),
        delta=raw.get("delta"),
        gamma=raw.get("gamma"),
        theta=raw.get("theta"),
        vega=raw.get("vega"),
        require_greeks=bool(raw.get("require_greeks") or False),
        max_quote_age_seconds=float(raw.get("max_quote_age_seconds") or 5.0),
        halted=bool(raw.get("halted") or False),
        halt_detail=str(raw.get("halt_detail") or ""),
        kill_switch_engaged=bool(raw.get("kill_switch_engaged") or False),
        kill_switch_detail=str(raw.get("kill_switch_detail") or ""),
        account_equity_usd=float(raw.get("account_equity_usd") or 0.0),
        cash_usd=float(raw.get("cash_usd") or 0.0),
        position_notional_by_symbol={str(k): float(v) for k, v in positions.items()},
        open_position_count=int(raw.get("open_position_count") or 0),
        realized_pnl_today_usd=float(raw.get("realized_pnl_today_usd") or 0.0),
        recent_order_fingerprints=fingerprints,
        last_order_at_utc=raw.get("last_order_at_utc"),
        order_fingerprint=str(raw.get("order_fingerprint") or ""),
        max_aggregate_exposure_usd=float(raw.get("max_aggregate_exposure_usd") or 0.0),
        max_symbol_exposure_fraction=float(raw.get("max_symbol_exposure_fraction") or 0.0),
        max_simultaneous_positions=int(raw.get("max_simultaneous_positions") or 0),
        max_daily_loss_usd=float(raw.get("max_daily_loss_usd") or 0.0),
        cooldown_seconds=float(raw.get("cooldown_seconds") or 0.0),
        now_utc=now_utc,
        risk_approved=raw.get("risk_approved"),
        risk_reason=str(raw.get("risk_reason") or ""),
    )


def replay_ledger(path: Path) -> ReplayReport:
    """Replay all decision records; fail closed on corrupt/incomplete ledger."""

    ok, detail, records = load_and_verify(path)
    if not ok:
        return ReplayReport(
            ok=False,
            records_replayed=0,
            matches=0,
            mismatches=(),
            reason_code=PaperReasonCode.LEDGER_CORRUPT,
            detail=detail,
        )
    if not records:
        return ReplayReport(
            ok=False,
            records_replayed=0,
            matches=0,
            mismatches=(),
            reason_code=PaperReasonCode.LEDGER_INCOMPLETE,
            detail="decision ledger is empty; nothing to replay",
        )

    mismatches: list[ReplayMismatch] = []
    matches = 0
    replayed = 0

    for record in records:
        if record.kind not in _REPLAYABLE_KINDS:
            continue
        if "decision_inputs" not in record.payload and "now_utc" not in record.payload:
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="decision_inputs",
                    recorded=None,
                    replayed=None,
                    detail="incomplete record: no decision_inputs for replay",
                )
            )
            continue
        try:
            inp = _input_from_payload(dict(record.payload), record)
            result = evaluate_paper_decision(inp)
        except (DecisionLedgerError, ValueError, TypeError, KeyError) as error:
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="evaluation",
                    recorded=record.reason_code,
                    replayed=None,
                    detail=f"replay failed closed: {error}",
                )
            )
            continue

        replayed += 1
        # Compare outcome and may_open (from payload) and primary reason.
        recorded_outcome = record.outcome
        recorded_reason = record.reason_code
        recorded_may = record.payload.get("may_open")
        if recorded_may is None:
            recorded_may = recorded_outcome == DecisionOutcome.ALLOW.value

        row_mismatches = 0
        if str(result.outcome.value) != str(recorded_outcome):
            row_mismatches += 1
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="outcome",
                    recorded=recorded_outcome,
                    replayed=result.outcome.value,
                    detail="replayed outcome differs from recorded",
                )
            )
        if bool(result.may_open) != bool(recorded_may):
            row_mismatches += 1
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="may_open",
                    recorded=recorded_may,
                    replayed=result.may_open,
                    detail="replayed may_open differs from recorded",
                )
            )
        if result.primary_reason.value != recorded_reason:
            row_mismatches += 1
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="reason_code",
                    recorded=recorded_reason,
                    replayed=result.primary_reason.value,
                    detail="replayed primary reason differs from recorded",
                )
            )
        if result.inputs_fingerprint != record.inputs_fingerprint:
            # Fingerprint mismatch can happen if payload stored a subset; flag it.
            row_mismatches += 1
            mismatches.append(
                ReplayMismatch(
                    sequence=record.sequence,
                    field="inputs_fingerprint",
                    recorded=record.inputs_fingerprint,
                    replayed=result.inputs_fingerprint,
                    detail="inputs fingerprint diverged on replay",
                )
            )
        if row_mismatches == 0:
            matches += 1

    if mismatches:
        return ReplayReport(
            ok=False,
            records_replayed=replayed,
            matches=matches,
            mismatches=tuple(mismatches),
            reason_code=PaperReasonCode.REPLAY_MISMATCH,
            detail=f"{len(mismatches)} mismatch(es) across {replayed} replayed record(s)",
        )
    return ReplayReport(
        ok=True,
        records_replayed=replayed,
        matches=matches,
        mismatches=(),
        reason_code=PaperReasonCode.REPLAY_MATCH,
        detail=f"all {matches} replayed record(s) match",
    )

"""Combined paper decision evaluation (data health + portfolio controls).

Pure function of explicit inputs — no broker I/O. The result is recordable
on the decision ledger and re-evaluable for deterministic replay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from chronos.paperops.controls import (
    PaperControlDecision,
    PaperControlState,
    evaluate_paper_controls,
)
from chronos.paperops.data_quality import (
    DataDegradation,
    PaperDataHealth,
    QuoteSnapshot,
    evaluate_paper_quote,
)
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.records import inputs_fingerprint


@dataclass(frozen=True, slots=True)
class PaperDecisionInput:
    """Everything needed to re-evaluate a paper open decision (replayable)."""

    strategy_id: str
    strategy_version: str
    config_hash: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    # Quote evidence
    bid: float | None
    ask: float | None
    last: float | None
    quote_utc: str | None  # ISO; None => missing
    data_source: str
    quality_label: str
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    require_greeks: bool = False
    max_quote_age_seconds: float = 5.0
    # Control state
    halted: bool = False
    halt_detail: str = ""
    kill_switch_engaged: bool = False
    kill_switch_detail: str = ""
    account_equity_usd: float = 0.0
    cash_usd: float = 0.0
    position_notional_by_symbol: dict[str, float] | None = None
    open_position_count: int = 0
    realized_pnl_today_usd: float = 0.0
    recent_order_fingerprints: tuple[str, ...] = ()
    last_order_at_utc: str | None = None
    order_fingerprint: str = ""
    max_aggregate_exposure_usd: float = 0.0
    max_symbol_exposure_fraction: float = 0.0
    max_simultaneous_positions: int = 0
    max_daily_loss_usd: float = 0.0
    cooldown_seconds: float = 0.0
    now_utc: str = ""  # ISO, required
    # Optional risk layer already decided by caller
    risk_approved: bool | None = None
    risk_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Normalize nested dict for stable fingerprints.
        positions = d.get("position_notional_by_symbol") or {}
        d["position_notional_by_symbol"] = dict(sorted(positions.items()))
        d["recent_order_fingerprints"] = list(self.recent_order_fingerprints)
        return d

    def fingerprint(self) -> str:
        return inputs_fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class PaperDecisionResult:
    may_open: bool
    outcome: DecisionOutcome
    primary_reason: PaperReasonCode
    kind: DecisionKind
    data_health: PaperDataHealth
    controls: PaperControlDecision
    explanations: tuple[str, ...]
    inputs_fingerprint: str
    decision_inputs: dict[str, Any]

    def to_payload(self) -> dict[str, object]:
        return {
            "may_open": self.may_open,
            "outcome": self.outcome.value,
            "primary_reason": self.primary_reason.value,
            "kind": self.kind.value,
            "explanations": list(self.explanations),
            "data_health": self.data_health.to_payload(),
            "controls": self.controls.to_payload(),
            "inputs_fingerprint": self.inputs_fingerprint,
        }


def _parse_dt(value: str | None, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} is not a valid ISO datetime: {value!r}") from error
    return dt


def evaluate_paper_decision(inp: PaperDecisionInput) -> PaperDecisionResult:
    """Evaluate data health + controls (+ optional risk) for a paper open."""

    decision_inputs = inp.to_dict()
    fingerprint = inputs_fingerprint(decision_inputs)
    now = _parse_dt(inp.now_utc, field="now_utc")
    if now is None:
        # Fail closed: no evaluation clock => no authorization.
        synthetic_health = PaperDataHealth(
            ok=False,
            may_authorize_open=False,
            reason_code=PaperReasonCode.DATA_CLOCK_ANOMALY,
            degradation=DataDegradation.CLOCK_ANOMALY,
            label=inp.quality_label or "UNKNOWN",
            detail="now_utc is required for paper decision evaluation",
            data_timestamp_utc=inp.quote_utc,
            data_source=inp.data_source or "missing",
        )
        controls = PaperControlDecision(
            allowed=False,
            reason_codes=(PaperReasonCode.DATA_CLOCK_ANOMALY,),
            explanations=("now_utc missing",),
        )
        return PaperDecisionResult(
            may_open=False,
            outcome=DecisionOutcome.DENY,
            primary_reason=PaperReasonCode.DATA_CLOCK_ANOMALY,
            kind=DecisionKind.REJECTION,
            data_health=synthetic_health,
            controls=controls,
            explanations=("now_utc is required; failing closed",),
            inputs_fingerprint=fingerprint,
            decision_inputs=decision_inputs,
        )

    quote_utc = _parse_dt(inp.quote_utc, field="quote_utc")
    quote = QuoteSnapshot(
        symbol=inp.symbol,
        bid=inp.bid,
        ask=inp.ask,
        last=inp.last,
        quote_utc=quote_utc,
        source=inp.data_source,
        quality_label=inp.quality_label,
        iv=inp.iv,
        delta=inp.delta,
        gamma=inp.gamma,
        theta=inp.theta,
        vega=inp.vega,
        require_greeks=inp.require_greeks,
    )
    health = evaluate_paper_quote(
        quote,
        now_utc=now,
        max_quote_age_seconds=inp.max_quote_age_seconds,
    )

    last_order = _parse_dt(inp.last_order_at_utc, field="last_order_at_utc")
    notional = abs(float(inp.limit_price) * int(inp.quantity))
    order_fp = inp.order_fingerprint or fingerprint
    control_state = PaperControlState(
        halted=inp.halted,
        halt_detail=inp.halt_detail,
        kill_switch_engaged=inp.kill_switch_engaged,
        kill_switch_detail=inp.kill_switch_detail,
        account_equity_usd=inp.account_equity_usd,
        cash_usd=inp.cash_usd,
        position_notional_by_symbol=dict(inp.position_notional_by_symbol or {}),
        open_position_count=inp.open_position_count,
        realized_pnl_today_usd=inp.realized_pnl_today_usd,
        recent_order_fingerprints=inp.recent_order_fingerprints,
        last_order_at_utc=last_order,
        proposed_order_fingerprint=order_fp,
        proposed_symbol=inp.symbol,
        proposed_notional_usd=notional,
        max_aggregate_exposure_usd=inp.max_aggregate_exposure_usd,
        max_symbol_exposure_fraction=inp.max_symbol_exposure_fraction,
        max_simultaneous_positions=inp.max_simultaneous_positions,
        max_daily_loss_usd=inp.max_daily_loss_usd,
        cooldown_seconds=inp.cooldown_seconds,
        now_utc=now,
    )
    controls = evaluate_paper_controls(control_state)

    explanations: list[str] = []
    may_open = True
    primary = PaperReasonCode.ORDER_PROPOSED
    kind = DecisionKind.PROPOSED_ORDER
    outcome = DecisionOutcome.ALLOW

    if not health.may_authorize_open:
        may_open = False
        primary = health.reason_code
        kind = DecisionKind.DATA_HEALTH if health.ok else DecisionKind.REJECTION
        outcome = DecisionOutcome.DENY
        explanations.append(health.detail)

    if not controls.allowed:
        may_open = False
        if primary in (PaperReasonCode.ORDER_PROPOSED, PaperReasonCode.DATA_OK):
            primary = controls.reason_codes[0]
        kind = DecisionKind.CONTROL_REFUSAL
        outcome = DecisionOutcome.DENY
        explanations.extend(controls.explanations)

    if inp.risk_approved is False:
        may_open = False
        primary = PaperReasonCode.RISK_DENIED
        kind = DecisionKind.RISK_DECISION
        outcome = DecisionOutcome.DENY
        explanations.append(inp.risk_reason or "risk engine denied")
    elif inp.risk_approved is True and may_open:
        explanations.append(inp.risk_reason or "risk engine approved")
        primary = PaperReasonCode.RISK_APPROVED
        kind = DecisionKind.RISK_DECISION

    if may_open and not explanations:
        explanations.append("data health and portfolio controls permit paper open")

    if may_open:
        outcome = DecisionOutcome.ALLOW
        if primary not in (PaperReasonCode.RISK_APPROVED, PaperReasonCode.ORDER_PROPOSED):
            primary = PaperReasonCode.ORDER_PROPOSED
        kind = (
            DecisionKind.RISK_DECISION if inp.risk_approved is True else DecisionKind.PROPOSED_ORDER
        )

    return PaperDecisionResult(
        may_open=may_open,
        outcome=outcome,
        primary_reason=primary,
        kind=kind,
        data_health=health,
        controls=controls,
        explanations=tuple(explanations),
        inputs_fingerprint=fingerprint,
        decision_inputs=decision_inputs,
    )

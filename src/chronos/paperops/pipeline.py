"""Thin adapter: paper order pipeline stages → decision ledger.

Maps already-gathered order-service evidence (intent, risk decision, risk
evidence, submission outcome) into paperops records. Does not contact the
broker. Recording is observational audit — it does not authorize or refuse
orders; the order service remains authoritative for lifecycle transitions.

When a :class:`DecisionLedger` is injected into
:class:`~chronos.orders.service.OrderManagementService`, propose/risk and
submit stages append here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from chronos.config.settings import Settings
from chronos.domain.enums import IBEnvironment, ProductFamily
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.risk import OrderRiskDecision, RiskEvidence
from chronos.orders.submission import SubmissionOutcome, SubmissionRefusalCode
from chronos.paperops.decision import PaperDecisionInput
from chronos.paperops.ledger import DecisionEvent, DecisionLedger
from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode
from chronos.paperops.records import DecisionRecord
from chronos.paperops.session import RecordedDecision, record_paper_decision


def settings_config_hash(settings: Settings) -> str:
    """Stable, secret-free hash of settings fields that bind paper decisions."""

    payload = {
        "broker_mode": settings.broker_mode.value,
        "ib_environment": settings.ib_environment.value,
        "allow_order_transmit": settings.allow_order_transmit,
        "allow_live_trading": settings.allow_live_trading,
        "symbol_allowlist": list(settings.symbol_allowlist),
        "max_open_short_option_contracts": settings.max_open_short_option_contracts,
        "max_opening_orders_per_day": settings.max_opening_orders_per_day,
        "max_gross_assignment_usd": str(settings.max_gross_assignment_usd),
        "max_session_drawdown_usd": str(settings.max_session_drawdown_usd),
        "max_session_drawdown_pct": str(settings.max_session_drawdown_pct),
        "require_live_arming": settings.require_live_arming,
        "require_typed_confirmation": settings.require_typed_confirmation,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _symbol(intent: WheelOrderIntent) -> str:
    return str(intent.contract.symbol).strip().upper()


def _limit_float(intent: WheelOrderIntent) -> float:
    return float(intent.limit_price)


def _quantity_int(intent: WheelOrderIntent) -> int:
    # Options/stocks are whole units; crypto may be fractional — ceil to int
    # for paperops portfolio math (audit layer; not execution quantity).
    q = intent.quantity
    as_int = int(q)
    if Decimal(as_int) == q:
        return as_int
    return max(1, as_int + 1)


def _side(intent: WheelOrderIntent) -> str:
    return intent.intent.value  # OPEN_SHORT_PUT etc. is more precise than BUY/SELL


def build_propose_input(
    *,
    intent: WheelOrderIntent,
    risk: OrderRiskDecision,
    evidence: RiskEvidence,
    settings: Settings,
    now: datetime,
    config_hash: str,
    data_source: str = "paper_pipeline",
    quality_label: str = "LIVE",
) -> PaperDecisionInput:
    """Build a paperops decision input from pipeline propose evidence."""

    equity = float(evidence.account.net_liquidation)
    cash = float(evidence.account.total_cash)
    symbol = _symbol(intent)
    limit = _limit_float(intent)
    qty = _quantity_int(intent)
    # Gross option assignment-style notional for control math when option.
    if intent.product_family is ProductFamily.OPTION:
        strike = float(getattr(intent.contract, "strike", limit) or limit)
        notional_proxy = abs(strike * 100.0 * qty)  # 100-multiplier convention
        # limit_price for order_identity is still the option premium.
        identity_limit = limit
    else:
        notional_proxy = abs(limit * qty)
        identity_limit = limit
    del notional_proxy  # size controls use limit*qty inside evaluate

    # Map settings limits into paperops controls (paper plane only).
    max_exposure = float(settings.max_gross_assignment_usd)
    if max_exposure <= 0:
        max_exposure = max(equity, 1.0)
    max_sym_frac = float(settings.max_symbol_allocation_pct)
    max_pos = int(settings.max_open_short_option_contracts) or 1
    max_daily_loss = float(settings.max_session_drawdown_usd)

    strategy_id = f"wheel:{intent.intent.value}"
    # Intent has no strategy version field — be explicit, never invent edge.
    strategy_version = "unknown"

    quote_ts = now.isoformat()
    return PaperDecisionInput(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config_hash=config_hash,
        symbol=symbol,
        side=_side(intent),
        quantity=qty,
        limit_price=identity_limit,
        bid=limit,
        ask=limit,
        last=limit,
        quote_utc=quote_ts,
        data_source=data_source,
        quality_label=quality_label,
        require_greeks=False,
        max_quote_age_seconds=float(settings.max_quote_age_seconds),
        halted=False,
        kill_switch_engaged=False,
        account_equity_usd=equity if equity > 0 else 1.0,
        cash_usd=cash,
        position_notional_by_symbol={},
        open_position_count=int(evidence.active_short_option_contracts),
        realized_pnl_today_usd=0.0,
        recent_order_fingerprints=(),
        last_order_at_utc=None,
        order_fingerprint=intent.intent_id,  # stable per-intent identity
        max_aggregate_exposure_usd=max_exposure,
        max_symbol_exposure_fraction=max_sym_frac if max_sym_frac > 0 else 1.0,
        max_simultaneous_positions=max_pos,
        max_daily_loss_usd=max_daily_loss if max_daily_loss > 0 else 1e12,
        cooldown_seconds=0.0,
        now_utc=quote_ts,
        risk_approved=bool(risk.approved),
        risk_reason=(
            "order risk PASS"
            if risk.approved
            else "order risk FAIL/UNKNOWN: "
            + "; ".join(
                f"{c.name}={c.status.value}:{c.detail}"
                for c in risk.checks
                if c.status.value != "PASS"
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class PipelineRecorder:
    """Append-only paperops recorder bound to one decision ledger + settings."""

    ledger: DecisionLedger
    settings: Settings
    config_hash: str

    @classmethod
    def create(cls, ledger: DecisionLedger, settings: Settings) -> PipelineRecorder:
        return cls(
            ledger=ledger,
            settings=settings,
            config_hash=settings_config_hash(settings),
        )

    def record_propose(
        self,
        *,
        intent: WheelOrderIntent,
        risk: OrderRiskDecision,
        evidence: RiskEvidence,
        now: datetime,
        environment: IBEnvironment,
    ) -> RecordedDecision:
        """Record propose + risk outcome (deny when risk not approved)."""

        quality = "LIVE" if environment is IBEnvironment.PAPER else "UNKNOWN"
        # Live environment must never look authorizing from paperops audit alone.
        if environment is IBEnvironment.LIVE:
            quality = "UNKNOWN"
        source = (
            "ibkr_paper_pipeline"
            if environment is IBEnvironment.PAPER
            else f"pipeline:{environment.value}"
        )
        inp = build_propose_input(
            intent=intent,
            risk=risk,
            evidence=evidence,
            settings=self.settings,
            now=now,
            config_hash=self.config_hash,
            data_source=source,
            quality_label=quality,
        )
        extra: dict[str, object] = {
            "pipeline_stage": "propose",
            "intent_id": intent.intent_id,
            "correlation_id": intent.correlation_id,
            "product_family": intent.product_family.value,
            "wheel_intent": intent.intent.value,
            "risk_decision_id": risk.decision_id,
            "risk_overall": risk.overall.value,
            "environment": environment.value,
            # Never log raw account id — fingerprint only if needed later.
            "account_fingerprint_present": bool(intent.account_id),
        }
        return record_paper_decision(
            self.ledger,
            inp,
            at_utc=now.isoformat(),
            extra_payload=extra,
            rehydrate_controls=True,
        )

    def record_submit(
        self,
        *,
        intent: WheelOrderIntent,
        outcome: SubmissionOutcome,
        now: datetime,
        environment: IBEnvironment,
    ) -> DecisionRecord:
        """Record submit success or refusal (stage event; not a full re-eval)."""

        submitted = bool(outcome.submitted)
        refusal = outcome.refusal
        if submitted:
            kind = DecisionKind.STATE_TRANSITION
            reason = PaperReasonCode.ORDER_PROPOSED
            dec_outcome = DecisionOutcome.ALLOW
            detail = outcome.detail or "paper order submitted"
        else:
            kind = DecisionKind.REJECTION
            reason = _refusal_to_reason(refusal)
            dec_outcome = DecisionOutcome.DENY
            detail = outcome.detail or refusal.value

        payload: dict[str, object] = {
            "pipeline_stage": "submit",
            "intent_id": intent.intent_id,
            "correlation_id": intent.correlation_id,
            "submitted": submitted,
            "refusal": refusal.value,
            "detail": detail,
            "environment": environment.value,
            "may_open": submitted,
            "order_fingerprint": intent.intent_id,
            "effective_order_fingerprint": intent.intent_id,
            "decision_inputs": {
                "pipeline_stage": "submit",
                "intent_id": intent.intent_id,
                "symbol": _symbol(intent),
                "side": _side(intent),
                "quantity": _quantity_int(intent),
                "limit_price": _limit_float(intent),
                "order_fingerprint": intent.intent_id,
                "strategy_id": f"wheel:{intent.intent.value}",
                "strategy_version": "unknown",
                "config_hash": self.config_hash,
                "now_utc": now.isoformat(),
                "submitted": submitted,
                "refusal": refusal.value,
            },
        }
        event = DecisionEvent(
            kind=kind,
            reason_code=reason,
            outcome=dec_outcome,
            strategy_id=f"wheel:{intent.intent.value}",
            strategy_version="unknown",
            config_hash=self.config_hash,
            data_timestamp_utc=now.isoformat(),
            data_source=(
                "ibkr_paper_pipeline"
                if environment is IBEnvironment.PAPER
                else f"pipeline:{environment.value}"
            ),
            data_quality_label="LIVE" if environment is IBEnvironment.PAPER else "UNKNOWN",
            decision_inputs=payload["decision_inputs"],  # type: ignore[arg-type]
            payload=payload,
        )
        return self.ledger.append(event, at_utc=now.isoformat())

    def record_fill(
        self,
        *,
        intent_id: str,
        strategy_id: str,
        symbol: str,
        filled_quantity: Decimal,
        now: datetime,
    ) -> DecisionRecord:
        """Record a paper fill / terminal partial (when tracker reports fill)."""

        payload: dict[str, object] = {
            "pipeline_stage": "fill",
            "intent_id": intent_id,
            "symbol": symbol,
            "filled_quantity": str(filled_quantity),
            "order_fingerprint": intent_id,
            "effective_order_fingerprint": intent_id,
            "may_open": False,
            "decision_inputs": {
                "pipeline_stage": "fill",
                "intent_id": intent_id,
                "symbol": symbol,
                "order_fingerprint": intent_id,
                "strategy_id": strategy_id,
                "strategy_version": "unknown",
                "config_hash": self.config_hash,
                "now_utc": now.isoformat(),
                "filled_quantity": str(filled_quantity),
            },
        }
        event = DecisionEvent(
            kind=DecisionKind.PAPER_FILL,
            reason_code=PaperReasonCode.FILL_RECORDED,
            outcome=DecisionOutcome.INFORMATIONAL,
            strategy_id=strategy_id,
            strategy_version="unknown",
            config_hash=self.config_hash,
            data_timestamp_utc=now.isoformat(),
            data_source="paper_pipeline",
            data_quality_label="LIVE",
            decision_inputs=payload["decision_inputs"],  # type: ignore[arg-type]
            payload=payload,
        )
        return self.ledger.append(event, at_utc=now.isoformat())


def _refusal_to_reason(code: SubmissionRefusalCode) -> PaperReasonCode:
    mapping: dict[SubmissionRefusalCode, PaperReasonCode] = {
        SubmissionRefusalCode.NOT_REFUSED: PaperReasonCode.ORDER_PROPOSED,
        SubmissionRefusalCode.READ_ONLY_LEASE: PaperReasonCode.HALTED,
        SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE: PaperReasonCode.LIVE_TRADING_BLOCKED,
        SubmissionRefusalCode.MODE_FORBIDS: PaperReasonCode.HALTED,
        SubmissionRefusalCode.ACCOUNT_MISMATCH: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.RISK_NOT_APPROVED: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.RISK_EXPIRED: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.CONFIRMATION_MISSING: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.CONFIRMATION_EXPIRED: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.CONFIRMATION_MISMATCH: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.INTENT_NOT_CONFIRMED: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.BROKER_SUBMIT_FAILED: PaperReasonCode.RISK_DENIED,
        SubmissionRefusalCode.LIVE_DEPENDENCIES_MISSING: PaperReasonCode.LIVE_TRADING_BLOCKED,
        SubmissionRefusalCode.LIVE_GRANT_DENIED: PaperReasonCode.LIVE_TRADING_BLOCKED,
        SubmissionRefusalCode.LIVE_GATE_BLOCKED: PaperReasonCode.LIVE_TRADING_BLOCKED,
        SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND: PaperReasonCode.RISK_DENIED,
    }
    return mapping.get(code, PaperReasonCode.RISK_DENIED)


def pipeline_extra_has_no_secrets(payload: dict[str, Any]) -> bool:
    """Structural helper for tests: reject secret-like keys in pipeline extras."""

    forbidden = ("password", "token", "api_key", "secret", "authorization", "credential")
    blob = json.dumps(payload, sort_keys=True).lower()
    return not any(f in blob for f in forbidden)

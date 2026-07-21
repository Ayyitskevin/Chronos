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

DECISION_SETTINGS_FIELDS: tuple[str, ...] = (
    # Broker/runtime settings that select the evidence and submission boundary.
    "broker_mode",
    "broker_adapter",
    "demo_profile",
    "ib_environment",
    "ib_host",
    "ib_port",
    "ib_client_id",
    "ib_account_id",
    "ib_account_allowlist",
    "allow_order_transmit",
    "allow_live_trading",
    "allow_outside_rth",
    # Human/live safety settings used by service and submission dependencies.
    "require_live_arming",
    "live_arm_ttl_minutes",
    "require_typed_confirmation",
    "order_confirmation_ttl_seconds",
    "live_kill_switch_file",
    "session_baseline_file",
    # State/evidence selection that can change the recorded decision.
    "database_url",
    "symbol_allowlist",
    "crypto_allowlist",
    "crypto_time_in_force",
    "market_timezone",
    "max_quote_age_seconds",
    # Risk/control limits consumed by OrderRiskEngine and paperops evaluation.
    "max_contracts_per_order",
    "max_open_short_option_contracts",
    "max_opening_orders_per_day",
    "max_gross_assignment_usd",
    "min_cash_buffer_usd",
    "min_cash_buffer_pct",
    "max_symbol_allocation_pct",
    "max_total_wheel_allocation_pct",
    "max_crypto_allocation_pct",
    "max_crypto_notional_per_order_usd",
    "max_session_drawdown_usd",
    "max_session_drawdown_pct",
)


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _sensitive_digest(domain: str, value: object) -> str:
    material = {"domain": domain, "value": value}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decision_settings_projection(settings: Settings) -> dict[str, object]:
    """Canonical, secret-safe settings material for recorded OMS decisions.

    The projection covers configuration read by broker selection, risk,
    OrderManagementService, OrderSubmissionBoundary, and the live safety
    dependencies wired into that boundary. Account ids, account allowlists,
    connection targets, database URLs, and state paths are domain-separated
    digests only; raw sensitive values never enter the decision ledger.
    """

    account_id = settings.ib_account_id.strip().upper()
    account_allowlist = sorted(entry.strip().upper() for entry in settings.ib_account_allowlist)
    endpoint = {
        "host": settings.ib_host.strip().lower(),
        "port": int(settings.ib_port),
        "client_id": int(settings.ib_client_id),
    }
    live_state_paths = {
        "kill_switch": str(settings.live_kill_switch_file),
        "session_baseline": str(settings.session_baseline_file),
    }
    return {
        "projection": "chronos.paperops.decision-settings.v1",
        "broker_mode": settings.broker_mode.value,
        "broker_adapter": settings.broker_adapter.value,
        "demo_profile": settings.demo_profile.value,
        "ib_environment": settings.ib_environment.value,
        "broker_endpoint_sha256": _sensitive_digest("broker-endpoint-v1", endpoint),
        "ib_account_id_sha256": _sensitive_digest("ib-account-id-v1", account_id),
        "ib_account_allowlist_sha256": _sensitive_digest(
            "ib-account-allowlist-v1", account_allowlist
        ),
        "allow_order_transmit": settings.allow_order_transmit,
        "allow_live_trading": settings.allow_live_trading,
        "allow_outside_rth": settings.allow_outside_rth,
        "require_live_arming": settings.require_live_arming,
        "live_arm_ttl_minutes": settings.live_arm_ttl_minutes,
        "require_typed_confirmation": settings.require_typed_confirmation,
        "order_confirmation_ttl_seconds": settings.order_confirmation_ttl_seconds,
        "live_state_paths_sha256": _sensitive_digest("live-state-paths-v1", live_state_paths),
        "database_url_sha256": _sensitive_digest(
            "order-database-url-v1", settings.database_url.strip()
        ),
        "symbol_allowlist": sorted(symbol.strip().upper() for symbol in settings.symbol_allowlist),
        "crypto_allowlist": sorted(symbol.strip().upper() for symbol in settings.crypto_allowlist),
        "crypto_time_in_force": settings.crypto_time_in_force,
        "market_timezone": settings.market_timezone,
        "max_quote_age_seconds": settings.max_quote_age_seconds,
        "max_contracts_per_order": settings.max_contracts_per_order,
        "max_open_short_option_contracts": settings.max_open_short_option_contracts,
        "max_opening_orders_per_day": settings.max_opening_orders_per_day,
        "max_gross_assignment_usd": _canonical_decimal(settings.max_gross_assignment_usd),
        "min_cash_buffer_usd": _canonical_decimal(settings.min_cash_buffer_usd),
        "min_cash_buffer_pct": _canonical_decimal(settings.min_cash_buffer_pct),
        "max_symbol_allocation_pct": _canonical_decimal(settings.max_symbol_allocation_pct),
        "max_total_wheel_allocation_pct": _canonical_decimal(
            settings.max_total_wheel_allocation_pct
        ),
        "max_crypto_allocation_pct": _canonical_decimal(settings.max_crypto_allocation_pct),
        "max_crypto_notional_per_order_usd": _canonical_decimal(
            settings.max_crypto_notional_per_order_usd
        ),
        "max_session_drawdown_usd": _canonical_decimal(settings.max_session_drawdown_usd),
        "max_session_drawdown_pct": _canonical_decimal(settings.max_session_drawdown_pct),
    }


def settings_config_hash(settings: Settings) -> str:
    """Stable hash of the canonical secret-safe decision settings projection."""

    payload = decision_settings_projection(settings)
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
    # The OMS exposes an order limit, not broker quote evidence. Preserve the
    # proxy for replay, but label it synthetic so it cannot authorize.
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
        data_source="order_intent_limit_proxy",
        quality_label="SYNTHETIC",
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

        inp = build_propose_input(
            intent=intent,
            risk=risk,
            evidence=evidence,
            settings=self.settings,
            now=now,
            config_hash=self.config_hash,
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
            data_source="order_pipeline",
            data_quality_label="N/A",
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
        lifecycle: str = "FILLED",
        remaining_quantity: Decimal | None = None,
    ) -> DecisionRecord:
        """Record a paper fill or partial-fill (when tracker reports fill)."""

        payload: dict[str, object] = {
            "pipeline_stage": "fill",
            "intent_id": intent_id,
            "symbol": symbol,
            "filled_quantity": str(filled_quantity),
            "remaining_quantity": (
                str(remaining_quantity) if remaining_quantity is not None else None
            ),
            "lifecycle": lifecycle,
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
                "lifecycle": lifecycle,
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
            data_source="order_pipeline",
            data_quality_label="N/A",
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

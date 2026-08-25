"""Durable, default-off PAPER proposal engine for the QQQ Five-Tool candidate.

This module owns *when* an already-open PAPER position should be reduced or
closed.  It deliberately owns neither order construction nor transmission:
an emitted :class:`~chronos.autonomy.decision.ProposedDecision` must re-enter
the existing supervisor, compiler, risk, reconciliation, kill-switch, and
single-transmit boundary exactly like any other proposal.

The lifecycle is event-sourced in Chronos's existing hash-chain table.  Every
restart replays and semantically re-evaluates each observation; a broken hash
or a result that no longer follows from the recorded inputs fails closed.

This module creates no mandate and grants no authority.  Its functions are not
wired into the runtime; callers can only persist broker facts and obtain a
proposal with ``execution_authority="none"``.  Any future activation must use
Chronos's existing authenticated supervisor mandate and order pipeline.

Scope is intentionally narrow:

* IBKR PAPER observations only; no LIVE or transmission path;
* long QQQ only; no short compiler or borrow assumptions;
* the pinned Five-Tool management stack: initial stop, 1R/2R targets,
  breakeven only after an actual complete T1 fill, a 22/3 ATR Chandelier
  runner activated at 1R, adverse confirmed-regime exit, 2%/$60 session-loss
  and 10% drawdown circuit breakers;
* source-default long AVWAP behavior is preserved: it is inactive because the
  dedicated-long-v2 source switch is off.  No SMA, neutral, or time exit is
  introduced.

This source directly imports no broker, order service, submission boundary, or
execution adapter.  Importing its ``chronos.supervisor`` package still inherits
that package's process-level dependency graph; this module constructs, calls,
and holds none of those capabilities.  It can describe a risk-reducing proposal
and cannot send one.

The management-event reference remains separate from the proposal target.  A
risk-reducing proposal targets the existing Chronos position, as required by
the autonomy contract.  Activation therefore also requires a future trusted
queue seam that can authenticate the management-event reference without
turning a model-authored nonce into a replay-protection bypass.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.autonomy import (
    DecisionDirection,
    DecisionKind,
    EvidenceCitation,
    ProposedDecision,
    TradableAssetClass,
)
from chronos.domain.enums import DataQuality, IBEnvironment
from chronos.domain.models import ChronosModel
from chronos.persistence import hash_chain
from chronos.persistence.schema import HashChainRow

QQQ_FIVE_TOOL_CANDIDATE_SHA256 = "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_POSITION_REF = re.compile(r"^CHR-POS-[0-9A-F]{32}$")
_ORDER_REF = re.compile(r"^CHR-ORD-[0-9A-F]{32}$")
_DIRECTIVE_REF = re.compile(r"^CHR-PM-[0-9A-F]{32}$")
_MAX_PRICE_SCALE = -8


class PositionManagementError(RuntimeError):
    """Durable state is corrupt, contradictory, or cannot safely advance."""


class ManagedLegId(StrEnum):
    TARGET_1 = "TARGET_1"
    TARGET_2 = "TARGET_2"
    RUNNER = "RUNNER"


class ManagementReason(StrEnum):
    ENTRY_RISK_BREACH = "ENTRY_RISK_BREACH"
    SESSION_LOSS = "SESSION_LOSS"
    DRAWDOWN = "DRAWDOWN"
    INITIAL_STOP = "INITIAL_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    OPPOSITE_REGIME = "OPPOSITE_REGIME"
    TARGET_1 = "TARGET_1"
    TARGET_2 = "TARGET_2"


class EvaluationRefusal(StrEnum):
    POSITION_CLOSED = "POSITION_CLOSED"
    PENDING_DIRECTIVE = "PENDING_DIRECTIVE"
    AMBIGUOUS_SEND = "AMBIGUOUS_SEND"
    OBSERVATION_REPLAY = "OBSERVATION_REPLAY"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    TEMPORAL_ORDER = "TEMPORAL_ORDER"
    DATA_QUALITY = "DATA_QUALITY"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


class DirectiveOutcome(StrEnum):
    REFUSED_NOT_SENT = "REFUSED_NOT_SENT"
    CANCELLED_NOT_FILLED = "CANCELLED_NOT_FILLED"
    PARTIALLY_FILLED_REMAINDER_CANCELLED = "PARTIALLY_FILLED_REMAINDER_CANCELLED"
    FILLED = "FILLED"
    SENT_AMBIGUOUS = "SENT_AMBIGUOUS"
    RECONCILED_NOT_FILLED = "RECONCILED_NOT_FILLED"
    RECONCILED_PARTIAL_FILL_REMAINDER_CANCELLED = "RECONCILED_PARTIAL_FILL_REMAINDER_CANCELLED"
    RECONCILED_FILLED = "RECONCILED_FILLED"


_INITIAL_OUTCOMES = frozenset(
    {
        DirectiveOutcome.REFUSED_NOT_SENT,
        DirectiveOutcome.CANCELLED_NOT_FILLED,
        DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
        DirectiveOutcome.FILLED,
        DirectiveOutcome.SENT_AMBIGUOUS,
    }
)
_RECONCILED_OUTCOMES = frozenset(
    {
        DirectiveOutcome.RECONCILED_NOT_FILLED,
        DirectiveOutcome.RECONCILED_PARTIAL_FILL_REMAINDER_CANCELLED,
        DirectiveOutcome.RECONCILED_FILLED,
    }
)
_PARTIAL_OUTCOMES = frozenset(
    {
        DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
        DirectiveOutcome.RECONCILED_PARTIAL_FILL_REMAINDER_CANCELLED,
    }
)
_FULL_OUTCOMES = frozenset({DirectiveOutcome.FILLED, DirectiveOutcome.RECONCILED_FILLED})
_NO_FILL_OUTCOMES = frozenset(
    {
        DirectiveOutcome.REFUSED_NOT_SENT,
        DirectiveOutcome.CANCELLED_NOT_FILLED,
        DirectiveOutcome.RECONCILED_NOT_FILLED,
    }
)
_LATCHED_FLATTEN_REASONS = frozenset(
    {
        ManagementReason.ENTRY_RISK_BREACH,
        ManagementReason.SESSION_LOSS,
        ManagementReason.DRAWDOWN,
        ManagementReason.INITIAL_STOP,
        ManagementReason.BREAKEVEN_STOP,
        ManagementReason.TRAILING_STOP,
        ManagementReason.OPPOSITE_REGIME,
    }
)


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _bounded_positive_decimal(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < _MAX_PRICE_SCALE:
        raise ValueError(f"{label} is finer than the 1e-8 persistence scale")
    return value


def _hex_digest(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if _HEX_64.fullmatch(normalized) is None:
        raise ValueError(f"{label} must be a 64-character lowercase hex digest")
    return normalized


def _nonblank_printable(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} must be nonblank printable text")
    return normalized


class QQQFiveToolPaperPolicy(ChronosModel):
    """Exact management choices selected for the current QQQ Confluence cell."""

    schema_version: Literal["chronos-qqq-five-tool-paper-policy-v1"] = (
        "chronos-qqq-five-tool-paper-policy-v1"
    )
    symbol: Literal["QQQ"] = "QQQ"
    long_only: Literal[True] = True
    native_stop_risk_fraction: Decimal = Decimal("0.01")
    native_stop_risk_usd_max: Decimal = Decimal("30")
    cvar_risk_fraction: Decimal = Decimal("0.015")
    cvar_risk_usd_max: Decimal = Decimal("45")
    capital_base_usd_max: Decimal = Decimal("3000")
    target_1_r: Decimal = Decimal("1")
    target_2_r: Decimal = Decimal("2")
    break_even_after_target_1: Literal[True] = True
    chandelier_activation_r: Decimal = Decimal("1")
    chandelier_lookback: Literal[22] = 22
    chandelier_atr_multiple: Decimal = Decimal("3")
    opposite_regime_exit: Literal[True] = True
    long_avwap_exit: Literal[False] = False
    neutral_regime_exit: Literal[False] = False
    sma_exit: Literal[False] = False
    time_exit: Literal[False] = False
    session_loss_fraction: Decimal = Decimal("0.02")
    session_loss_usd_max: Decimal = Decimal("60")
    drawdown_fraction_max: Decimal = Decimal("0.10")
    quote_max_age_seconds: Decimal = Decimal("5")
    permitted_data_qualities: tuple[DataQuality, ...] = (DataQuality.LIVE,)


QQQ_FIVE_TOOL_PAPER_POLICY = QQQFiveToolPaperPolicy()


def policy_sha256(policy: QQQFiveToolPaperPolicy = QQQ_FIVE_TOOL_PAPER_POLICY) -> str:
    payload = policy.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


QQQ_FIVE_TOOL_PAPER_POLICY_SHA256 = policy_sha256()


class ManagedLeg(ChronosModel):
    leg_id: ManagedLegId
    quantity: Decimal
    target_price: Decimal | None = None

    @field_validator("quantity")
    @classmethod
    def _whole_quantity(cls, value: Decimal) -> Decimal:
        _bounded_positive_decimal(value, "leg quantity")
        if value != value.to_integral_value():
            raise ValueError("QQQ leg quantity must be whole shares")
        return value

    @field_validator("target_price")
    @classmethod
    def _target_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            _bounded_positive_decimal(value, "target_price")
        return value


class QQQFiveToolPaperPlan(ChronosModel):
    position_id: str
    opening_order_ref: str
    account_fingerprint: str
    environment: Literal[IBEnvironment.PAPER] = IBEnvironment.PAPER
    symbol: Literal["QQQ"] = "QQQ"
    candidate_spec_sha256: str = QQQ_FIVE_TOOL_CANDIDATE_SHA256
    policy: QQQFiveToolPaperPolicy = Field(default_factory=lambda: QQQ_FIVE_TOOL_PAPER_POLICY)
    management_policy_sha256: str = QQQ_FIVE_TOOL_PAPER_POLICY_SHA256
    entry_fill_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    opening_fill_evidence_digest: str
    entry_risk_evidence_digest: str
    opened_at: AwareDatetime
    quantity: Decimal
    entry_price: Decimal
    initial_stop_price: Decimal
    signal_time_risk_distance_usd: Decimal
    strategy_nav_usd: Decimal
    cvar_budget_usd: Decimal
    unit_exposure_cvar_loss_fraction: Decimal
    cvar_projected_loss_usd: Decimal
    native_stop_risk_usd: Decimal
    legs: tuple[ManagedLeg, ...] = Field(min_length=1, max_length=3)

    @field_validator("position_id")
    @classmethod
    def _position_id(cls, value: str) -> str:
        normalized = value.strip().upper()
        if _POSITION_REF.fullmatch(normalized) is None:
            raise ValueError("position_id must be CHR-POS-<32 hex>")
        return normalized

    @field_validator("opening_order_ref")
    @classmethod
    def _opening_ref(cls, value: str) -> str:
        normalized = value.strip().upper()
        if _ORDER_REF.fullmatch(normalized) is None:
            raise ValueError("opening_order_ref must be CHR-ORD-<32 hex>")
        return normalized

    @field_validator(
        "account_fingerprint",
        "candidate_spec_sha256",
        "management_policy_sha256",
        "opening_fill_evidence_digest",
        "entry_risk_evidence_digest",
    )
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _hex_digest(value, getattr(info, "field_name", "digest"))

    @field_validator("entry_fill_ids")
    @classmethod
    def _bounded_fill_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(fill_id.strip() for fill_id in value)
        if any(
            not fill_id or len(fill_id) > 128 or any(ord(character) < 32 for character in fill_id)
            for fill_id in normalized
        ):
            raise ValueError(
                "entry_fill_ids must be nonblank printable text at most 128 characters"
            )
        return normalized

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, value: Decimal) -> Decimal:
        _bounded_positive_decimal(value, "position quantity")
        if value != value.to_integral_value():
            raise ValueError("QQQ position quantity must be whole shares")
        return value

    @field_validator(
        "entry_price",
        "initial_stop_price",
        "signal_time_risk_distance_usd",
        "strategy_nav_usd",
        "cvar_budget_usd",
        "unit_exposure_cvar_loss_fraction",
        "cvar_projected_loss_usd",
        "native_stop_risk_usd",
    )
    @classmethod
    def _positive_numbers(cls, value: Decimal, info: object) -> Decimal:
        return _bounded_positive_decimal(value, getattr(info, "field_name", "value"))

    @model_validator(mode="after")
    def _exact_geometry_and_risk(self) -> QQQFiveToolPaperPlan:
        if self.candidate_spec_sha256 != QQQ_FIVE_TOOL_CANDIDATE_SHA256:
            raise ValueError("plan does not bind the exact QQQ Five-Tool candidate")
        if self.policy != QQQ_FIVE_TOOL_PAPER_POLICY:
            raise ValueError("plan changes the selected QQQ Five-Tool management policy")
        if self.management_policy_sha256 != policy_sha256(self.policy):
            raise ValueError("management policy digest does not match policy bytes")
        if len(set(self.entry_fill_ids)) != len(self.entry_fill_ids):
            raise ValueError("entry_fill_ids must be unique authoritative fill identities")
        if any(not fill_id.strip() for fill_id in self.entry_fill_ids):
            raise ValueError("entry_fill_ids may not contain blanks")
        if self.initial_stop_price >= self.entry_price:
            raise ValueError("a long QQQ stop must be below the entry fill")
        if self.unit_exposure_cvar_loss_fraction > 1:
            raise ValueError("unit-exposure long loss fraction may not exceed total exposure")

        risk_distance = self.entry_price - self.initial_stop_price
        if risk_distance != self.signal_time_risk_distance_usd:
            raise ValueError("post-fill stop must preserve the frozen signal-time risk distance")
        expected_risk = risk_distance * self.quantity
        if self.native_stop_risk_usd != expected_risk:
            raise ValueError("native_stop_risk_usd must equal quantity times stop distance")
        base = min(self.strategy_nav_usd, self.policy.capital_base_usd_max)
        expected_cvar_budget = min(
            base * self.policy.cvar_risk_fraction,
            self.policy.cvar_risk_usd_max,
        )
        if self.cvar_budget_usd != expected_cvar_budget:
            raise ValueError("CVaR budget must equal the exact selected outer loss budget")
        expected_cvar_loss = (
            self.entry_price * self.quantity * self.unit_exposure_cvar_loss_fraction
        )
        if self.cvar_projected_loss_usd != expected_cvar_loss:
            raise ValueError(
                "cvar_projected_loss_usd must equal filled notional times unit-exposure CVaR"
            )

        if sum((leg.quantity for leg in self.legs), Decimal(0)) != self.quantity:
            raise ValueError("managed legs must sum exactly to the filled position")
        if len({leg.leg_id for leg in self.legs}) != len(self.legs):
            raise ValueError("managed leg identities must be unique")
        expected_t1 = self.entry_price + risk_distance * self.policy.target_1_r
        expected_t2 = self.entry_price + risk_distance * self.policy.target_2_r
        by_id = {leg.leg_id: leg for leg in self.legs}
        expected_order = (
            (ManagedLegId.TARGET_2,)
            if self.quantity < 3
            else (
                ManagedLegId.TARGET_1,
                ManagedLegId.TARGET_2,
                ManagedLegId.RUNNER,
            )
        )
        if tuple(leg.leg_id for leg in self.legs) != expected_order:
            raise ValueError("managed legs must use canonical T1, T2, RUNNER order")
        if self.quantity < 3:
            if set(by_id) != {ManagedLegId.TARGET_2}:
                raise ValueError("a sub-three-share position uses one TARGET_2 leg")
            if by_id[ManagedLegId.TARGET_2].target_price != expected_t2:
                raise ValueError("single-leg target must equal 2R")
        else:
            if set(by_id) != set(ManagedLegId):
                raise ValueError("a splittable position requires T1, T2, and runner legs")
            if by_id[ManagedLegId.TARGET_1].target_price != expected_t1:
                raise ValueError("TARGET_1 leg must equal 1R")
            if by_id[ManagedLegId.TARGET_2].target_price != expected_t2:
                raise ValueError("TARGET_2 leg must equal 2R")
            if by_id[ManagedLegId.RUNNER].target_price is not None:
                raise ValueError("runner may not carry a fixed target")
        return self

    @property
    def risk_distance(self) -> Decimal:
        return self.entry_price - self.initial_stop_price

    @property
    def applicable_capital_base(self) -> Decimal:
        return min(self.strategy_nav_usd, self.policy.capital_base_usd_max)

    @property
    def native_stop_loss_budget_usd(self) -> Decimal:
        return min(
            self.applicable_capital_base * self.policy.native_stop_risk_fraction,
            self.policy.native_stop_risk_usd_max,
        )

    @property
    def risk_envelope_breached(self) -> bool:
        return (
            self.native_stop_risk_usd > self.native_stop_loss_budget_usd
            or self.cvar_projected_loss_usd > self.cvar_budget_usd
            or self.entry_price * self.quantity > self.applicable_capital_base
        )


def build_qqq_five_tool_paper_plan(
    *,
    position_id: str,
    opening_order_ref: str,
    account_fingerprint: str,
    entry_fill_ids: tuple[str, ...],
    opening_fill_evidence_digest: str,
    entry_risk_evidence_digest: str,
    opened_at: datetime,
    quantity: Decimal,
    entry_price: Decimal,
    initial_stop_price: Decimal,
    signal_time_risk_distance_usd: Decimal,
    strategy_nav_usd: Decimal,
    unit_exposure_cvar_loss_fraction: Decimal,
) -> QQQFiveToolPaperPlan:
    """Build source-compatible one- or three-leg geometry from actual fills."""

    _bounded_positive_decimal(quantity, "quantity")
    if quantity != quantity.to_integral_value():
        raise ValueError("QQQ quantity must be whole shares")
    risk_distance = entry_price - initial_stop_price
    if risk_distance <= 0:
        raise ValueError("initial stop must be below a long entry")
    applicable_base = min(
        strategy_nav_usd,
        QQQ_FIVE_TOOL_PAPER_POLICY.capital_base_usd_max,
    )
    cvar_budget_usd = min(
        applicable_base * QQQ_FIVE_TOOL_PAPER_POLICY.cvar_risk_fraction,
        QQQ_FIVE_TOOL_PAPER_POLICY.cvar_risk_usd_max,
    )
    legs: tuple[ManagedLeg, ...]
    if quantity < 3:
        legs = (
            ManagedLeg(
                leg_id=ManagedLegId.TARGET_2,
                quantity=quantity,
                target_price=entry_price + risk_distance * QQQ_FIVE_TOOL_PAPER_POLICY.target_2_r,
            ),
        )
    else:
        first = quantity // 3
        second = first
        runner = quantity - first - second
        legs = (
            ManagedLeg(
                leg_id=ManagedLegId.TARGET_1,
                quantity=first,
                target_price=entry_price + risk_distance * QQQ_FIVE_TOOL_PAPER_POLICY.target_1_r,
            ),
            ManagedLeg(
                leg_id=ManagedLegId.TARGET_2,
                quantity=second,
                target_price=entry_price + risk_distance * QQQ_FIVE_TOOL_PAPER_POLICY.target_2_r,
            ),
            ManagedLeg(leg_id=ManagedLegId.RUNNER, quantity=runner),
        )
    return QQQFiveToolPaperPlan(
        position_id=position_id,
        opening_order_ref=opening_order_ref,
        account_fingerprint=account_fingerprint,
        entry_fill_ids=entry_fill_ids,
        opening_fill_evidence_digest=opening_fill_evidence_digest,
        entry_risk_evidence_digest=entry_risk_evidence_digest,
        opened_at=opened_at,
        quantity=quantity,
        entry_price=entry_price,
        initial_stop_price=initial_stop_price,
        signal_time_risk_distance_usd=signal_time_risk_distance_usd,
        strategy_nav_usd=strategy_nav_usd,
        cvar_budget_usd=cvar_budget_usd,
        unit_exposure_cvar_loss_fraction=unit_exposure_cvar_loss_fraction,
        cvar_projected_loss_usd=(entry_price * quantity * unit_exposure_cvar_loss_fraction),
        native_stop_risk_usd=risk_distance * quantity,
        legs=legs,
    )


class PositionObservation(ChronosModel):
    observation_id: str = Field(min_length=1, max_length=128)
    account_fingerprint: str
    as_of: AwareDatetime
    evidence_digest: str
    data_quality: DataQuality
    last_price: Decimal
    marked_strategy_nav_usd: Decimal
    broker_position_quantity: Decimal
    # Recorded-only provenance until ADR-0035's authenticated PAPER adapter
    # exists. Presence is not freshness or same-session proof.
    reconciliation_generation: int = Field(ge=0)
    reconciliation_session_id: str = Field(min_length=1, max_length=128)
    highest_high_22: Decimal | None = None
    atr14: Decimal | None = None
    opposite_confirmed_regime: bool = False
    long_avwap_failure: bool = False
    session_loss_usd: Decimal = Field(default=Decimal(0), ge=0)
    drawdown_fraction: Decimal = Field(default=Decimal(0), ge=0)

    @field_validator("observation_id", "reconciliation_session_id")
    @classmethod
    def _observation_identifiers(cls, value: str, info: object) -> str:
        return _nonblank_printable(value, getattr(info, "field_name", "identifier"))

    @field_validator("account_fingerprint", "evidence_digest")
    @classmethod
    def _observation_digests(cls, value: str, info: object) -> str:
        return _hex_digest(value, getattr(info, "field_name", "digest"))

    @field_validator("last_price", "marked_strategy_nav_usd")
    @classmethod
    def _positive_observation_value(cls, value: Decimal, info: object) -> Decimal:
        return _bounded_positive_decimal(value, getattr(info, "field_name", "value"))

    @field_validator("broker_position_quantity")
    @classmethod
    def _broker_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ValueError("broker_position_quantity must be finite non-negative whole shares")
        return value

    @field_validator("highest_high_22", "atr14")
    @classmethod
    def _optional_prices(cls, value: Decimal | None, info: object) -> Decimal | None:
        if value is not None:
            _bounded_positive_decimal(value, getattr(info, "field_name", "value"))
        return value

    @model_validator(mode="after")
    def _trail_inputs_travel_together(self) -> PositionObservation:
        if (self.highest_high_22 is None) != (self.atr14 is None):
            raise ValueError("highest_high_22 and atr14 must be supplied together")
        if self.highest_high_22 is not None and self.highest_high_22 < self.last_price:
            raise ValueError("highest_high_22 must be at least last_price")
        if not self.session_loss_usd.is_finite() or not self.drawdown_fraction.is_finite():
            raise ValueError("loss and drawdown observations must be finite")
        return self


class ManagementDirective(ChronosModel):
    directive_ref: str
    issued_at: AwareDatetime
    position_id: str
    opening_order_ref: str
    observation_id: str
    reason: ManagementReason
    leg_id: ManagedLegId | None = None
    quantity: Decimal
    closes_position: bool
    proposal: ProposedDecision
    execution_authority: Literal["none"] = "none"
    required_path: Literal["existing_supervisor_and_order_pipeline"] = (
        "existing_supervisor_and_order_pipeline"
    )

    @field_validator("directive_ref")
    @classmethod
    def _directive_ref(cls, value: str) -> str:
        normalized = value.strip().upper()
        if _DIRECTIVE_REF.fullmatch(normalized) is None:
            raise ValueError("directive_ref must be CHR-PM-<32 hex>")
        return normalized

    @model_validator(mode="after")
    def _proposal_is_the_same_risk_reduction(self) -> ManagementDirective:
        expected_kind = DecisionKind.CLOSE if self.closes_position else DecisionKind.REDUCE
        if self.proposal.kind is not expected_kind:
            raise ValueError("directive proposal kind contradicts its managed scope")
        if self.proposal.requested_quantity != self.quantity:
            raise ValueError("directive proposal quantity must equal the managed quantity")
        if self.proposal.target_client_reference != self.position_id:
            raise ValueError("directive proposal must target its managed Chronos position")
        if (
            self.proposal.symbol != "QQQ"
            or self.proposal.direction is not DecisionDirection.NEUTRAL
        ):
            raise ValueError("directive proposal must be a neutral risk reduction of QQQ")
        return self


class ManagementEvaluation(ChronosModel):
    observation_id: str
    evaluated_at: AwareDatetime
    refusal: EvaluationRefusal | None = None
    detail: str = ""
    prior_leg_stop_price: Decimal
    effective_leg_stop_price: Decimal
    prior_runner_stop_price: Decimal
    effective_runner_stop_price: Decimal
    directive: ManagementDirective | None = None

    @property
    def actionable(self) -> bool:
        return self.refusal is None and self.directive is not None


class DirectiveResolution(ChronosModel):
    directive_ref: str
    outcome: DirectiveOutcome
    occurred_at: AwareDatetime
    evidence_digest: str
    execution_id: str | None = Field(default=None, max_length=128)
    fill_quantity: Decimal = Decimal(0)
    fill_price: Decimal | None = None

    @field_validator("directive_ref")
    @classmethod
    def _resolution_ref(cls, value: str) -> str:
        normalized = value.strip().upper()
        if _DIRECTIVE_REF.fullmatch(normalized) is None:
            raise ValueError("directive_ref must be CHR-PM-<32 hex>")
        return normalized

    @field_validator("evidence_digest")
    @classmethod
    def _resolution_digest(cls, value: str) -> str:
        return _hex_digest(value, "evidence_digest")

    @field_validator("fill_quantity")
    @classmethod
    def _fill_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ValueError("fill_quantity must be finite non-negative whole shares")
        return value

    @field_validator("fill_price")
    @classmethod
    def _fill_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            _bounded_positive_decimal(value, "fill_price")
        return value

    @field_validator("execution_id")
    @classmethod
    def _execution_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("execution_id must be nonblank printable text")
        return normalized

    @model_validator(mode="after")
    def _fill_evidence_matches_outcome(self) -> DirectiveResolution:
        filled = self.outcome in _PARTIAL_OUTCOMES | _FULL_OUTCOMES
        if filled:
            if self.fill_quantity <= 0 or self.fill_price is None or not self.execution_id:
                raise ValueError("filled outcomes require quantity, price, and execution_id")
        elif (
            self.fill_quantity != 0 or self.fill_price is not None or self.execution_id is not None
        ):
            raise ValueError("non-fill outcomes may not carry fill evidence")
        return self


class LegBalance(ChronosModel):
    leg_id: ManagedLegId
    remaining_quantity: Decimal


class PositionManagementState(ChronosModel):
    plan: QQQFiveToolPaperPlan
    balances: tuple[LegBalance, ...]
    leg_stop_price: Decimal
    runner_stop_price: Decimal
    last_event_at: AwareDatetime
    last_observation_as_of: AwareDatetime
    target_1_filled: bool = False
    flatten_latched_reason: ManagementReason | None = None
    pending_directive: ManagementDirective | None = None
    send_ambiguous: bool = False
    seen_observation_ids: frozenset[str] = frozenset()
    seen_execution_ids: frozenset[str] = frozenset()

    @property
    def remaining_quantity(self) -> Decimal:
        return sum((balance.remaining_quantity for balance in self.balances), Decimal(0))

    @property
    def closed(self) -> bool:
        return self.remaining_quantity == 0

    def remaining_for(self, leg_id: ManagedLegId) -> Decimal:
        return next(
            balance.remaining_quantity for balance in self.balances if balance.leg_id is leg_id
        )


def _initial_state(
    plan: QQQFiveToolPaperPlan,
    *,
    registered_at: datetime,
) -> PositionManagementState:
    return PositionManagementState(
        plan=plan,
        balances=tuple(
            LegBalance(leg_id=leg.leg_id, remaining_quantity=leg.quantity) for leg in plan.legs
        ),
        leg_stop_price=plan.initial_stop_price,
        runner_stop_price=plan.initial_stop_price,
        last_event_at=registered_at,
        last_observation_as_of=plan.opened_at,
        flatten_latched_reason=(
            ManagementReason.ENTRY_RISK_BREACH if plan.risk_envelope_breached else None
        ),
    )


def _stream_for(account_fingerprint: str, position_id: str) -> str:
    digest = hashlib.sha256(f"{account_fingerprint}:{position_id}".encode()).hexdigest()
    return "autonomy.position." + digest[:46]


def _directive_ref(
    state: PositionManagementState,
    observation: PositionObservation,
    reason: ManagementReason,
    quantity: Decimal,
) -> str:
    material = json.dumps(
        {
            "evidence_digest": observation.evidence_digest,
            "observation_id": observation.observation_id,
            "position_id": state.plan.position_id,
            "quantity": _canonical_decimal(quantity),
            "reason": reason.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "CHR-PM-" + hashlib.sha256(material.encode()).hexdigest()[:32].upper()


def _build_directive(
    state: PositionManagementState,
    observation: PositionObservation,
    *,
    reason: ManagementReason,
    quantity: Decimal,
    leg_id: ManagedLegId | None,
    issued_at: datetime,
) -> ManagementDirective:
    directive_ref = _directive_ref(state, observation, reason, quantity)
    closes_position = quantity == state.remaining_quantity
    kind = DecisionKind.CLOSE if closes_position else DecisionKind.REDUCE
    proposal = ProposedDecision(
        kind=kind,
        asset_class=TradableAssetClass.EQUITY,
        symbol="QQQ",
        direction=DecisionDirection.NEUTRAL,
        requested_quantity=quantity,
        target_client_reference=state.plan.position_id,
        rationale=(
            f"Deterministic PAPER position-management directive {reason.value}; "
            "must re-enter the existing supervisor and order pipeline."
        ),
        evidence=(
            EvidenceCitation(
                evidence_id=observation.observation_id,
                kind="paper_position_observation",
                as_of=observation.as_of,
                digest=observation.evidence_digest,
            ),
        ),
        invalidation_conditions=(
            "Broker-reconciled QQQ quantity or account identity differs before handoff",
        ),
    )
    return ManagementDirective(
        directive_ref=directive_ref,
        issued_at=issued_at,
        position_id=state.plan.position_id,
        opening_order_ref=state.plan.opening_order_ref,
        observation_id=observation.observation_id,
        reason=reason,
        leg_id=leg_id,
        quantity=quantity,
        closes_position=closes_position,
        proposal=proposal,
    )


def _refused(
    state: PositionManagementState,
    observation: PositionObservation,
    evaluated_at: datetime,
    refusal: EvaluationRefusal,
    detail: str,
) -> ManagementEvaluation:
    return ManagementEvaluation(
        observation_id=observation.observation_id,
        evaluated_at=evaluated_at,
        refusal=refusal,
        detail=detail,
        prior_leg_stop_price=state.leg_stop_price,
        effective_leg_stop_price=state.leg_stop_price,
        prior_runner_stop_price=state.runner_stop_price,
        effective_runner_stop_price=state.runner_stop_price,
    )


def _evaluate_pure(
    state: PositionManagementState,
    observation: PositionObservation,
    *,
    evaluated_at: datetime,
) -> ManagementEvaluation:
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise PositionManagementError("evaluated_at must be timezone-aware")
    if evaluated_at < state.last_event_at:
        raise PositionManagementError("evaluation time predates the durable position state")
    plan = state.plan
    policy = plan.policy
    if state.closed:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.POSITION_CLOSED,
            "the managed position is already flat",
        )
    if observation.observation_id in state.seen_observation_ids:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.OBSERVATION_REPLAY,
            "this observation identity was already evaluated",
        )
    if observation.account_fingerprint != plan.account_fingerprint:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.RECONCILIATION_MISMATCH,
            "observation account fingerprint differs from the position owner",
        )
    if (
        observation.as_of < plan.opened_at
        or observation.as_of < state.last_observation_as_of
        or evaluated_at < plan.opened_at
    ):
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.TEMPORAL_ORDER,
            "a management observation may not predate the authoritative opening fill",
        )
    age_seconds = Decimal(str((evaluated_at - observation.as_of).total_seconds()))
    if age_seconds < 0 or age_seconds > policy.quote_max_age_seconds:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.STALE_OBSERVATION,
            f"observation age {age_seconds}s exceeds {policy.quote_max_age_seconds}s",
        )
    if observation.data_quality not in policy.permitted_data_qualities:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.DATA_QUALITY,
            f"{observation.data_quality.value} cannot drive PAPER management",
        )
    if observation.broker_position_quantity != state.remaining_quantity:
        return _refused(
            state,
            observation,
            evaluated_at,
            EvaluationRefusal.RECONCILIATION_MISMATCH,
            "broker position quantity does not equal replayed managed quantity",
        )
    if state.pending_directive is not None:
        refusal = (
            EvaluationRefusal.AMBIGUOUS_SEND
            if state.send_ambiguous
            else EvaluationRefusal.PENDING_DIRECTIVE
        )
        return _refused(
            state,
            observation,
            evaluated_at,
            refusal,
            "the prior directive must resolve against broker truth before another is emitted",
        )

    leg_stop = state.leg_stop_price
    runner_stop = max(state.runner_stop_price, leg_stop)
    if state.target_1_filled:
        leg_stop = max(leg_stop, plan.entry_price)
        runner_stop = max(runner_stop, leg_stop)
    if observation.highest_high_22 is not None and observation.atr14 is not None:
        activated_at = plan.entry_price + plan.risk_distance * policy.chandelier_activation_r
        if observation.last_price >= activated_at:
            chandelier = (
                observation.highest_high_22 - policy.chandelier_atr_multiple * observation.atr14
            )
            if chandelier > 0:
                runner_stop = max(runner_stop, chandelier)

    total = state.remaining_quantity
    reason: ManagementReason | None = None
    leg_id: ManagedLegId | None = None
    quantity = total
    loss_limit = min(
        min(observation.marked_strategy_nav_usd, policy.capital_base_usd_max)
        * policy.session_loss_fraction,
        policy.session_loss_usd_max,
    )
    if state.flatten_latched_reason is not None:
        reason = state.flatten_latched_reason
    elif observation.session_loss_usd >= loss_limit:
        reason = ManagementReason.SESSION_LOSS
    elif observation.drawdown_fraction >= policy.drawdown_fraction_max:
        reason = ManagementReason.DRAWDOWN
    elif observation.last_price <= leg_stop:
        if leg_stop <= plan.initial_stop_price:
            reason = ManagementReason.INITIAL_STOP
        elif state.target_1_filled and leg_stop == plan.entry_price:
            reason = ManagementReason.BREAKEVEN_STOP
        else:
            reason = ManagementReason.TRAILING_STOP
    else:
        runner_leg_id = ManagedLegId.TARGET_2 if len(plan.legs) == 1 else ManagedLegId.RUNNER
        runner_remaining = state.remaining_for(runner_leg_id)
        if runner_remaining > 0 and observation.last_price <= runner_stop:
            leg_id = runner_leg_id
            quantity = runner_remaining
            reason = (
                ManagementReason.INITIAL_STOP
                if runner_stop <= plan.initial_stop_price
                else ManagementReason.TRAILING_STOP
            )
    if reason is None and policy.opposite_regime_exit and observation.opposite_confirmed_regime:
        reason = ManagementReason.OPPOSITE_REGIME
        leg_id = None
        quantity = total
    elif reason is None:
        # `long_avwap_failure` is deliberately inert under the exact source
        # defaults: dedicated long v2 is off, so its AVWAP close is off too.
        for leg in plan.legs:
            remaining = state.remaining_for(leg.leg_id)
            if remaining <= 0 or leg.target_price is None:
                continue
            if observation.last_price >= leg.target_price:
                leg_id = leg.leg_id
                quantity = remaining
                reason = (
                    ManagementReason.TARGET_1
                    if leg.leg_id is ManagedLegId.TARGET_1
                    else ManagementReason.TARGET_2
                )
                break

    directive = (
        None
        if reason is None
        else _build_directive(
            state,
            observation,
            reason=reason,
            quantity=quantity,
            leg_id=leg_id,
            issued_at=evaluated_at,
        )
    )
    return ManagementEvaluation(
        observation_id=observation.observation_id,
        evaluated_at=evaluated_at,
        detail="no management trigger" if directive is None else directive.reason.value,
        prior_leg_stop_price=state.leg_stop_price,
        effective_leg_stop_price=leg_stop,
        prior_runner_stop_price=state.runner_stop_price,
        effective_runner_stop_price=runner_stop,
        directive=directive,
    )


def _apply_evaluation(
    state: PositionManagementState,
    observation: PositionObservation,
    evaluation: ManagementEvaluation,
) -> PositionManagementState:
    if evaluation.observation_id != observation.observation_id:
        raise PositionManagementError("evaluation and observation identities disagree")
    expected = _evaluate_pure(
        state,
        observation,
        evaluated_at=evaluation.evaluated_at,
    )
    if expected != evaluation:
        raise PositionManagementError(
            "recorded management evaluation no longer follows from its immutable inputs"
        )
    seen = state.seen_observation_ids
    if evaluation.refusal is not EvaluationRefusal.OBSERVATION_REPLAY:
        seen = seen | {observation.observation_id}
    flatten_latched_reason = state.flatten_latched_reason
    if (
        evaluation.directive is not None
        and evaluation.directive.leg_id is None
        and evaluation.directive.reason in _LATCHED_FLATTEN_REASONS
    ):
        flatten_latched_reason = evaluation.directive.reason
    return state.model_copy(
        update={
            "leg_stop_price": evaluation.effective_leg_stop_price,
            "runner_stop_price": evaluation.effective_runner_stop_price,
            "pending_directive": evaluation.directive or state.pending_directive,
            "seen_observation_ids": seen,
            "flatten_latched_reason": flatten_latched_reason,
            "last_event_at": evaluation.evaluated_at,
            "last_observation_as_of": max(
                state.last_observation_as_of,
                observation.as_of,
            ),
        }
    )


def _apply_fill_to_balances(
    state: PositionManagementState,
    directive: ManagementDirective,
    fill_quantity: Decimal,
) -> tuple[LegBalance, ...]:
    remaining_to_allocate = fill_quantity
    balances: list[LegBalance] = []
    for balance in state.balances:
        take = Decimal(0)
        if directive.leg_id is None or directive.leg_id is balance.leg_id:
            take = min(balance.remaining_quantity, remaining_to_allocate)
        balances.append(
            LegBalance(
                leg_id=balance.leg_id,
                remaining_quantity=balance.remaining_quantity - take,
            )
        )
        remaining_to_allocate -= take
    if remaining_to_allocate != 0:
        raise PositionManagementError("fill quantity exceeds the directive's managed scope")
    return tuple(balances)


def _apply_resolution(
    state: PositionManagementState,
    resolution: DirectiveResolution,
) -> PositionManagementState:
    directive = state.pending_directive
    if directive is None or directive.directive_ref != resolution.directive_ref:
        raise PositionManagementError("resolution does not match the pending directive")
    if resolution.occurred_at < state.last_event_at or resolution.occurred_at < directive.issued_at:
        raise PositionManagementError("directive resolution predates its issuance")
    permitted = _RECONCILED_OUTCOMES if state.send_ambiguous else _INITIAL_OUTCOMES
    if resolution.outcome not in permitted:
        raise PositionManagementError("resolution outcome is invalid for the pending send state")
    if resolution.outcome is DirectiveOutcome.SENT_AMBIGUOUS:
        return state.model_copy(
            update={"send_ambiguous": True, "last_event_at": resolution.occurred_at}
        )
    if resolution.outcome in _NO_FILL_OUTCOMES:
        return state.model_copy(
            update={
                "pending_directive": None,
                "send_ambiguous": False,
                "last_event_at": resolution.occurred_at,
            }
        )
    execution_id = resolution.execution_id
    if execution_id is None:
        raise PositionManagementError("a filled outcome is missing its execution_id")
    if execution_id in state.seen_execution_ids:
        raise PositionManagementError("execution_id was already applied to this position")
    if resolution.outcome in _PARTIAL_OUTCOMES:
        if resolution.fill_quantity >= directive.quantity:
            raise PositionManagementError("partial-fill outcome must be smaller than the directive")
    elif resolution.fill_quantity != directive.quantity:
        raise PositionManagementError("full-fill outcome must equal the directive quantity")

    balances = _apply_fill_to_balances(state, directive, resolution.fill_quantity)
    target_1_filled = state.target_1_filled
    if directive.reason is ManagementReason.TARGET_1:
        target_1_remaining = next(
            balance.remaining_quantity
            for balance in balances
            if balance.leg_id is ManagedLegId.TARGET_1
        )
        target_1_filled = target_1_remaining == 0
    leg_stop = (
        max(state.leg_stop_price, state.plan.entry_price)
        if target_1_filled
        else state.leg_stop_price
    )
    runner_stop = max(state.runner_stop_price, leg_stop)
    return state.model_copy(
        update={
            "balances": balances,
            "leg_stop_price": leg_stop,
            "runner_stop_price": runner_stop,
            "target_1_filled": target_1_filled,
            "pending_directive": None,
            "send_ambiguous": False,
            "seen_execution_ids": state.seen_execution_ids | {execution_id},
            "last_event_at": resolution.occurred_at,
        }
    )


def _rows(session: Session, stream: str) -> tuple[HashChainRow, ...]:
    verification = hash_chain.verify(session, stream)
    if not verification.ok:
        raise PositionManagementError(
            f"position-management hash chain is invalid: {verification.detail}"
        )
    return tuple(
        session.scalars(
            select(HashChainRow)
            .where(HashChainRow.stream == stream)
            .order_by(HashChainRow.sequence.asc())
        )
    )


def rehydrate_position(
    session: Session,
    *,
    account_fingerprint: str,
    position_id: str,
) -> PositionManagementState:
    """Replay one position and re-prove every recorded management decision."""

    normalized_fingerprint = _hex_digest(account_fingerprint, "account_fingerprint")
    normalized_position_id = position_id.strip().upper()
    if _POSITION_REF.fullmatch(normalized_position_id) is None:
        raise PositionManagementError("position_id must be CHR-POS-<32 hex>")
    stream = _stream_for(normalized_fingerprint, normalized_position_id)
    rows = _rows(session, stream)
    if not rows:
        raise PositionManagementError("managed position is not registered")
    first = rows[0]
    if first.kind != "POSITION_REGISTERED":
        raise PositionManagementError("position stream does not begin with registration")
    try:
        state = _initial_state(
            QQQFiveToolPaperPlan.model_validate(hash_chain.payload_of(first)["plan"]),
            registered_at=first.recorded_at,
        )
        if first.recorded_at < state.plan.opened_at:
            raise PositionManagementError("position registration predates its opening fill")
        if state.plan.account_fingerprint != normalized_fingerprint:
            raise PositionManagementError("registered position owner does not match stream owner")
        last_recorded_at = first.recorded_at
        for row in rows[1:]:
            if row.recorded_at < last_recorded_at:
                raise PositionManagementError("position events are not chronologically ordered")
            payload = hash_chain.payload_of(row)
            if row.kind == "POSITION_EVALUATED":
                observation = PositionObservation.model_validate(payload["observation"])
                evaluation = ManagementEvaluation.model_validate(payload["evaluation"])
                if row.recorded_at != evaluation.evaluated_at:
                    raise PositionManagementError("evaluation event time contradicts its payload")
                state = _apply_evaluation(state, observation, evaluation)
            elif row.kind == "DIRECTIVE_RESOLVED":
                resolution = DirectiveResolution.model_validate(payload["resolution"])
                if row.recorded_at != resolution.occurred_at:
                    raise PositionManagementError("resolution event time contradicts its payload")
                state = _apply_resolution(
                    state,
                    resolution,
                )
            else:
                raise PositionManagementError(f"unknown position event kind {row.kind!r}")
            last_recorded_at = row.recorded_at
    except (KeyError, TypeError, ValueError) as error:
        raise PositionManagementError(
            "position-management stream contains an invalid semantic envelope"
        ) from error
    return state


def register_position(
    session: Session,
    *,
    plan: QQQFiveToolPaperPlan,
    recorded_at: datetime,
) -> PositionManagementState:
    """Record actual PAPER opening fills; this grants no execution authority."""
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise PositionManagementError("recorded_at must be timezone-aware")
    if recorded_at < plan.opened_at:
        raise PositionManagementError("position registration predates its opening fill")
    stream = _stream_for(plan.account_fingerprint, plan.position_id)
    existing = _rows(session, stream)
    if existing:
        state = rehydrate_position(
            session,
            account_fingerprint=plan.account_fingerprint,
            position_id=plan.position_id,
        )
        if state.plan != plan:
            raise PositionManagementError("position identity is already bound to a different plan")
        return state
    hash_chain.append(
        session,
        stream=stream,
        kind="POSITION_REGISTERED",
        payload={"plan": plan.model_dump(mode="json")},
        recorded_at=recorded_at,
    )
    return _initial_state(plan, registered_at=recorded_at)


def evaluate_position(
    session: Session,
    *,
    position_id: str,
    observation: PositionObservation,
    evaluated_at: datetime,
) -> ManagementEvaluation:
    """Record one observation and emit, at most, a no-authority proposal."""

    state = rehydrate_position(
        session,
        account_fingerprint=observation.account_fingerprint,
        position_id=position_id,
    )
    evaluation = _evaluate_pure(
        state,
        observation,
        evaluated_at=evaluated_at,
    )
    # A replayed id is already in the ledger; do not append a second event.
    if evaluation.refusal is not EvaluationRefusal.OBSERVATION_REPLAY:
        hash_chain.append(
            session,
            stream=_stream_for(state.plan.account_fingerprint, state.plan.position_id),
            kind="POSITION_EVALUATED",
            payload={
                "observation": observation.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            },
            recorded_at=evaluated_at,
        )
    return evaluation


def record_directive_resolution(
    session: Session,
    *,
    position_id: str,
    account_fingerprint: str,
    resolution: DirectiveResolution,
) -> PositionManagementState:
    """Record typed broker truth; recording facts grants no execution authority."""

    normalized_fingerprint = _hex_digest(account_fingerprint, "account_fingerprint")
    state = rehydrate_position(
        session,
        account_fingerprint=normalized_fingerprint,
        position_id=position_id,
    )
    advanced = _apply_resolution(state, resolution)
    hash_chain.append(
        session,
        stream=_stream_for(normalized_fingerprint, state.plan.position_id),
        kind="DIRECTIVE_RESOLVED",
        payload={"resolution": resolution.model_dump(mode="json")},
        recorded_at=resolution.occurred_at,
    )
    return advanced


__all__ = [
    "QQQ_FIVE_TOOL_CANDIDATE_SHA256",
    "QQQ_FIVE_TOOL_PAPER_POLICY",
    "QQQ_FIVE_TOOL_PAPER_POLICY_SHA256",
    "DirectiveOutcome",
    "DirectiveResolution",
    "EvaluationRefusal",
    "LegBalance",
    "ManagedLeg",
    "ManagedLegId",
    "ManagementDirective",
    "ManagementEvaluation",
    "ManagementReason",
    "PositionManagementError",
    "PositionManagementState",
    "PositionObservation",
    "QQQFiveToolPaperPlan",
    "QQQFiveToolPaperPolicy",
    "build_qqq_five_tool_paper_plan",
    "evaluate_position",
    "policy_sha256",
    "record_directive_resolution",
    "register_position",
    "rehydrate_position",
]

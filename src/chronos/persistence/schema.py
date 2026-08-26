"""SQLAlchemy schema for Chronos-owned metadata, never broker truth."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from chronos.utils.time import utc_now


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC safely even on SQLite, which discards timezone information."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Chronos timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class SchemaVersionRow(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class DatabaseScopeRow(Base):
    """Bind one ledger file to one broker environment and pseudonymous account."""

    __tablename__ = "database_scope"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class WheelCycleRow(Base):
    __tablename__ = "wheel_cycles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str | None] = mapped_column(Text)


class StrategyStateRow(Base):
    __tablename__ = "strategy_state"
    __table_args__ = (UniqueConstraint("symbol", name="uq_strategy_state_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    wheel_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("wheel_cycles.id"))
    wheel_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_average_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    strategy_adjusted_basis: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class CandidateEvaluationRow(Base):
    __tablename__ = "candidate_evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    wheel_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("wheel_cycles.id"))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_contract_id: Mapped[int | None] = mapped_column(Integer)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    ranking_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class RejectedCandidateReasonRow(Base):
    __tablename__ = "rejected_candidate_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_evaluations.id"),
        index=True,
        nullable=False,
    )
    contract_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)


class OrderDraftRow(Base):
    __tablename__ = "order_drafts"

    correlation_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    wheel_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("wheel_cycles.id"))
    account_id_masked: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False)
    intent: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class OrderPreviewRow(Base):
    __tablename__ = "order_previews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(
        ForeignKey("order_drafts.correlation_id"),
        index=True,
        nullable=False,
    )
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    estimated_commission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    margin_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    broker_message: Mapped[str] = mapped_column(Text, nullable=False)
    previewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SubmittedOrderRow(Base):
    __tablename__ = "submitted_orders"

    correlation_id: Mapped[str] = mapped_column(
        ForeignKey("order_drafts.correlation_id"),
        primary_key=True,
    )
    broker_order_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    permanent_id: Mapped[int | None] = mapped_column(Integer, index=True)
    client_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_ref: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FillRow(Base):
    __tablename__ = "fills"

    execution_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    correlation_id: Mapped[str | None] = mapped_column(
        ForeignKey("order_drafts.correlation_id"),
        index=True,
    )
    broker_order_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    wheel_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("wheel_cycles.id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False)
    security_type: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CommissionRow(Base):
    __tablename__ = "commissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("fills.execution_id"),
        unique=True,
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BasisEntryRow(Base):
    __tablename__ = "strategy_basis_entries"
    __table_args__ = (
        UniqueConstraint(
            "wheel_cycle_id",
            "entry_type",
            "source_execution_id",
            name="uq_basis_cycle_type_execution",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wheel_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("wheel_cycles.id"),
        index=True,
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(48), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_id: Mapped[int | None] = mapped_column(Integer)
    security_type: Mapped[str | None] = mapped_column(String(8))
    source_side: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    multiplier: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    source_execution_id: Mapped[str | None] = mapped_column(ForeignKey("fills.execution_id"))
    source_note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class ReconciliationRunRow(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApplicationEventRow(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class GuardrailDecisionRow(Base):
    __tablename__ = "guardrail_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    guardrail: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Live-wheel order pipeline (schema v3; docs/LIVE_WHEEL_GAME_PLAN.md M1).
# Populated from Milestone 5 on; created empty at v3 migration time so the
# durable shape exists before any order machinery runs.
# --------------------------------------------------------------------------- #


class OrderIntentRow(Base):
    __tablename__ = "order_intents"

    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    product_family: Mapped[str] = mapped_column(String(16), nullable=False)
    wheel_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("wheel_cycles.id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    con_id: Mapped[int | None] = mapped_column(Integer)
    local_symbol: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    open_close_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False)
    outside_rth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quote_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    risk_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    preview_id: Mapped[str | None] = mapped_column(String(64))
    confirmation_hash: Mapped[str | None] = mapped_column(String(128))
    order_ref: Mapped[str | None] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OrderConfirmationRow(Base):
    __tablename__ = "order_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.intent_id"), nullable=False)
    summary_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    ui_session_id: Mapped[str | None] = mapped_column(String(80))
    quote_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    risk_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LiveArmEventRow(Base):
    __tablename__ = "live_arm_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)  # armed/expired/revoked
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class KillSwitchEventRow(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class CashReservationRow(Base):
    __tablename__ = "cash_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("order_intents.intent_id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ShareReservationRow(Base):
    __tablename__ = "share_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("order_intents.intent_id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shares: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------- #
# Order-management lifecycle (schema v4; docs/LIVE_WHEEL_GAME_PLAN.md M5).
# order_events is the append-only lifecycle log AND the duplicate-callback
# idempotency ledger (event_key is unique); risk_decisions/risk_check_results
# persist the structured tri-state RiskDecision with its expiry. No existing
# table is altered, so the drift checker needs no special-casing.
# --------------------------------------------------------------------------- #


class OrderEventRow(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(
        ForeignKey("order_intents.intent_id"),
        index=True,
        nullable=False,
    )
    # One row per distinct broker/operator event; a duplicate callback replays
    # the same event_key and is rejected by this uniqueness (idempotency).
    event_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_order_id: Mapped[int | None] = mapped_column(Integer, index=True)
    filled_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    remaining_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), index=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    product_family: Mapped[str] = mapped_column(String(16), nullable=False)
    overall_result: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class RiskCheckResultRow(Base):
    __tablename__ = "risk_check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("risk_decisions.decision_id"),
        index=True,
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WriterLeaseRow(Base):
    """Single-writer lease row (see chronos.utils.locking).

    The lease code operates on this table with raw compare-and-swap SQL; the
    model exists so the table is part of the canonical metadata (create_all,
    drift checking, migrations). Exactly one row (id=1) is ever used.
    """

    __tablename__ = "writer_lease"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    holder: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)


# --- Autonomy: the supervisor's durable state (schema v5, M3) ----------------
#
# Everything below exists because M2 shipped a gateway with no memory. Its
# admission checks were pure functions over a `SupervisorState` the caller
# assembled from nowhere, so `LossLimits` and `ActivityLimits` were contract-only
# and the R-31 re-submission counters reset on every restart. These tables are
# where that state lives; `chronos.supervisor.durable` reads and writes them.


class HashChainRow(Base):
    """One tamper-evident record in a named append-only stream.

    See `chronos.persistence.hash_chain` for the design and its honest bound.
    The `(stream, sequence)` unique constraint is load-bearing: it is what makes
    a concurrent double-append fail instead of forking the chain.
    """

    __tablename__ = "hash_chain_records"
    __table_args__ = (UniqueConstraint("stream", "sequence", name="uq_hash_chain_stream_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The canonical JSON text that was hashed. Stored as text, not JSON, so a
    #: round-trip through a JSON column cannot re-order keys and break the
    #: digest — the bytes that were hashed are the bytes that are kept.
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ManagedPositionBindingRow(Base):
    """One durable opening-order identity for one managed position stream."""

    __tablename__ = "managed_position_bindings"
    __table_args__ = (
        UniqueConstraint(
            "account_fingerprint",
            "opening_order_ref",
            name="uq_managed_position_opening_order",
        ),
        UniqueConstraint(
            "account_fingerprint",
            "position_id",
            name="uq_managed_position_identity",
        ),
        UniqueConstraint(
            "account_fingerprint",
            "permanent_id",
            name="uq_managed_position_permanent_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    opening_order_ref: Mapped[str] = mapped_column(
        ForeignKey("order_intents.order_ref"), nullable=False
    )
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_decision_id: Mapped[str] = mapped_column(
        ForeignKey("risk_decisions.decision_id"), nullable=False
    )
    broker_order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    permanent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    opening_fill_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_risk_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    management_policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reconciliation_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AutonomyMandateActivationRow(Base):
    """An owner event that put a mandate in force, or revoked it.

    M2's `MandateActivation` was a value the caller passed in, so "is this
    mandate active" had no durable answer and could not survive a restart —
    which is precisely what `RestartBehavior.REQUIRE_REACTIVATION` is about.
    """

    __tablename__ = "autonomy_mandate_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    mandate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The account this activation is scoped to, pseudonymously.
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    #: Revocation is recorded in place rather than by deleting the activation:
    #: an audit trail that forgets a revocation cannot show when authority ended.
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The process generation this activation was made in.
    process_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AutonomySessionCounterRow(Base):
    """Per-session realized loss and activity counts for one account.

    This is what makes `LossLimits` and `ActivityLimits` enforceable rather than
    decorative. One row per (account, session date); the supervisor increments
    it in the same transaction that records the decision, so a counter cannot
    drift from the journal that explains it.
    """

    __tablename__ = "autonomy_session_counters"
    __table_args__ = (
        UniqueConstraint("account_fingerprint", "session_date", name="uq_autonomy_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: The trading session this row counts, as an ISO date in the market's zone.
    session_date: Mapped[str] = mapped_column(String(10), nullable=False)
    realized_loss_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    peak_equity_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    trough_equity_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    orders_submitted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancellations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replacements: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turnover_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class AutonomyDecisionAttemptRow(Base):
    """How many times one decision id has been admitted or refused (R-31).

    M2 held these counts in an in-memory `SupervisorState`, so a restart reset
    the attempt budget and a model could route around a refusal by waiting for
    one. Durable here.
    """

    __tablename__ = "autonomy_decision_attempts"
    __table_args__ = (
        UniqueConstraint("account_fingerprint", "decision_id", name="uq_autonomy_decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    admitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    refusals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AutonomyOwnerAlertRow(Base):
    """Something the owner must see, durable until they acknowledge it.

    ADR-0016 §8 requires four things when the system degrades: create no new
    exposure, permit deterministic risk reduction, *record the denial*, and
    *alert the owner*. M2 did the first three. This table is the fourth.

    Alerts are acknowledged, never deleted: an alert that can vanish cannot
    prove the owner was told, which is the only thing an alert is for.
    """

    __tablename__ = "autonomy_owner_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raised_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    acknowledged_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: When this alert was successfully handed to at least one delivery sink
    #: (schema v6, M6). NULL means the owner has not been *told* — which is
    #: different from not having *acknowledged*, and the difference is the whole
    #: point of R-32: an alert nobody was told about is not an alert.
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: How many delivery attempts have been made. A durable count so a sink that
    #: is persistently failing is visible rather than silently retrying forever.
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: How many times this same condition recurred before acknowledgement.
    #: Repeats are folded into one row so a degraded loop cannot bury the
    #: alert list under thousands of identical entries -- an alert nobody can
    #: read is an alert nobody receives.
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AutonomyProposalQueueRow(Base):
    """One proposal received from an external worker, awaiting a cycle (v7, M7).

    The route accepts and **stores**; the runtime dequeues and judges. That
    split is deliberate. Running a cycle inside the request would mean an
    external worker's HTTP call drove broker interaction on its own schedule,
    which is precisely the unbounded event-driven shape M7 rejects: the rate
    would be set by the caller rather than by us.

    The **raw payload** is stored, not a parsed object. The ingress is the
    single parsing authority, and re-serializing a parsed proposal would create
    a second representation that could drift from what was actually sent — so
    the bytes that arrived are the bytes that are judged.
    """

    __tablename__ = "autonomy_proposal_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: Exactly as received. Never re-serialized from a parsed object.
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    #: PENDING until a cycle has judged it; then PROCESSED. A queued proposal
    #: authorizes nothing — it has been *received*, which is not the same thing.
    status: Mapped[str] = mapped_column(String(16), index=True, default="PENDING", nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: Where the cycle stopped, and why. Kept on the row so an operator can see
    #: the outcome of a specific submission without correlating streams.
    cycle_stage: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    refusal: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: Which registered proposer's credential authenticated this submission
    #: (ADR-0023). NULL for rows accepted under the pre-registry posture, where
    #: the local API token authenticated and the static identity was stamped.
    #: The value is written by the route from the *verified* registration —
    #: never from the payload — and read at drain time to stamp provenance.
    proposer_id: Mapped[str | None] = mapped_column(String(64))


class AutonomyEvidenceBundleRow(Base):
    """One evidence bundle the backend issued or attested (v9, ADR-0028 Option C).

    This row is the record admission check 9 never had. Through 2026-08-13 that
    check compared ``provenance.evidence_bundle_id``/``_digest`` against
    ``SupervisorState.expected_*`` — and both sides were two reads of the single
    ``INGRESS_IDENTITY`` constant, so the check was a tautology that could not
    refuse in any posture, for any proposer. A durable per-job record is what
    finally gives the comparison a side the payload did not author.

    ``kind`` is the honest label, and the two values never substitute for one
    another:

    - ``backend_served`` — the backend composed a canonical document, digested
      the exact bytes it served, and returned them. It is a **witness**: it can
      recompute what it sent.
    - ``alert_attested`` — a proposer asserted, under its own credential and at
      a recorded time, that it saw bytes with this digest. The backend cannot
      recompute it and does not claim to. This is the only shape available to
      the TradingView bridge, whose evidence is authored outside Chronos and is
      never seen by the backend at all.

    ADR-0028's rule for the ladder is blunt and belongs beside the label: an
    attested bundle may back a proposal; it may **not** back a promotion rung.
    Rendering or reading an ``alert_attested`` row as "evidence the backend
    issued" would be exactly the false-evidence class the ladder exists to
    prevent.

    ``expires_at`` is judged against the **drain's** clock, never the proposer's
    own ``as_of``, so a bundle that expires between enqueue and drain refuses at
    the moment authority is exercised rather than the moment bytes arrived.
    """

    __tablename__ = "autonomy_evidence_bundles"
    __table_args__ = (
        UniqueConstraint("account_fingerprint", "bundle_id", name="uq_autonomy_evidence_bundle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: Backend-generated. A proposer never chooses its own bundle id, for the
    #: same reason it never authors its own provenance.
    bundle_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    #: The registration this bundle was issued *to*. A bundle cited by a
    #: different proposer refuses at STAMP: issuance is per-credential, and a
    #: shared bundle would make attribution ambiguous again.
    proposer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: ``backend_served`` or ``alert_attested`` — see the class docstring.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: SHA-256, lowercase hex, over the exact bytes served (or attested).
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The bundle schema version, so a change to either side's serialization is
    #: a visible, version-pinned break rather than a silent digest disagreement.
    bundle_version: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True, nullable=False)


class AutonomyProposerRevocationRow(Base):
    """One proposer credential the owner has killed mid-session (v10, A3).

    The registry file is a **boot-time snapshot** on both planes: editing it to
    disable a registration is honored at the next restart, which is the wrong
    latency for the event this table exists for — a leaked credential. So
    revocation is durable state the running process consults, exactly as mandate
    revocation is, and the file stays what it has always been: an owner-authored
    grant nothing else writes.

    **Keyed by credential hash, not by proposer id.** The id is recorded for
    legibility, but the check is on ``secret_sha256``, because that is what a
    caller presents and what actually leaked. Keying on the id would burn the
    name forever; keying on the hash means the owner mints a *new* credential
    for the same proposer and it works at the next restart, while the leaked one
    is dead permanently. That is the shape of the real incident.

    **Not account-scoped, deliberately.** A credential is global to the registry
    document, so a revocation that applied to one account would leave a state in
    which a revoked credential still proposes somewhere. There is no account
    column, by construction rather than by omission.
    """

    __tablename__ = "autonomy_proposer_revocations"
    __table_args__ = (UniqueConstraint("secret_sha256", name="uq_proposer_revocation_secret"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: The registration's id at the moment of revocation, for the audit trail.
    #: Never the thing compared: ids are reusable, credentials are not.
    proposer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: SHA-256 of the revoked credential. The registry stores the same hash, so
    #: revoking still never requires anyone to hold the credential itself.
    secret_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: Why. Required, like a mandate revocation's reason and an alert's
    #: acknowledgement note: an act with no stated cause cannot be reviewed.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

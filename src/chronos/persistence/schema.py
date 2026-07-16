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
    ranking_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
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
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
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

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wheel_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("wheel_cycles.id"),
        index=True,
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(48), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
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

"""Append-only evidence and deterministic Strategy-Adjusted Basis projection."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from hashlib import sha256

from pydantic import AwareDatetime, Field, PositiveInt, field_validator, model_validator

from chronos.domain.enums import (
    BasisEntryType,
    OrderSide,
    ReconciliationStatus,
    SecurityType,
)
from chronos.domain.models import BrokerExecution, ChronosModel, OptionContract
from chronos.utils.identifiers import account_fingerprint, normalize_account_fingerprint

STRATEGY_BASIS_LABEL = "Strategy-Adjusted Basis — not tax basis"


def _calculation_context(*values: Decimal | int) -> AbstractContextManager[Context]:
    precision = max(
        50,
        sum(
            len(value.as_tuple().digits) if isinstance(value, Decimal) else len(str(abs(value)))
            for value in values
        )
        + 16,
    )
    return localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN))


_PREMIUM_TYPES = {
    BasisEntryType.OPENING_OPTION_PREMIUM,
    BasisEntryType.CLOSING_OPTION_PREMIUM,
}
_COMMISSION_TYPES = {
    BasisEntryType.COMMISSION_ESTIMATE,
    BasisEntryType.COMMISSION_ACTUAL,
}
_STOCK_EVIDENCE_TYPES = {
    BasisEntryType.ASSIGNMENT_STOCK_FILL,
    BasisEntryType.CALLED_AWAY_STOCK_FILL,
}


class BasisLedgerEntry(ChronosModel):
    id: str
    wheel_cycle_id: str
    symbol: str
    entry_type: BasisEntryType
    amount: Decimal
    account_fingerprint: str
    contract_id: int | None = Field(default=None, gt=0)
    security_type: SecurityType | None = None
    source_side: OrderSide | None = None
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    multiplier: Decimal | None = Field(default=None, gt=0)
    currency: str = "USD"
    provisional: bool = False
    source_execution_id: str | None = None
    source_note: str | None = None
    occurred_at: AwareDatetime
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.RECONCILED

    @field_validator("id", "wheel_cycle_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("ledger identifiers must not be blank")
        return normalized

    @field_validator("symbol", "currency")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol and currency must not be blank")
        return normalized

    @field_validator("account_fingerprint")
    @classmethod
    def validate_account_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)

    @field_validator("source_execution_id")
    @classmethod
    def validate_source_execution_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_execution_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_evidence(self) -> BasisLedgerEntry:
        execution_fields = (
            self.contract_id,
            self.security_type,
            self.source_side,
            self.quantity,
            self.unit_price,
            self.multiplier,
        )
        if self.entry_type is BasisEntryType.MANUAL_ADJUSTMENT:
            if (
                self.source_execution_id is not None
                or any(value is not None for value in execution_fields)
                or not (self.source_note or "").strip()
            ):
                raise ValueError(
                    "Manual adjustments require a note and no broker execution provenance"
                )
            return self

        if self.source_execution_id is None or any(value is None for value in execution_fields):
            raise ValueError("Execution-linked entries require complete broker fill provenance")

        if self.entry_type in _PREMIUM_TYPES:
            if self.security_type is not SecurityType.OPTION:
                raise ValueError("Option premium entries require option security provenance")
            expected_side = (
                OrderSide.SELL
                if self.entry_type is BasisEntryType.OPENING_OPTION_PREMIUM
                else OrderSide.BUY
            )
            if self.source_side is not expected_side:
                raise ValueError("Option premium side does not match opening/closing semantics")
            assert self.quantity is not None
            assert self.unit_price is not None
            assert self.multiplier is not None
            with _calculation_context(self.quantity, self.unit_price, self.multiplier):
                gross = self.quantity * self.unit_price * self.multiplier
                expected = (
                    -gross if self.entry_type is BasisEntryType.OPENING_OPTION_PREMIUM else gross
                )
                if self.amount != expected:
                    raise ValueError(
                        "Option premium amount does not match price x multiplier x quantity"
                    )
        elif self.entry_type in _COMMISSION_TYPES:
            if self.security_type is not SecurityType.OPTION or self.amount < 0:
                raise ValueError("Commission entries require an option fill and nonnegative amount")
            if self.entry_type is BasisEntryType.COMMISSION_ESTIMATE and not self.provisional:
                raise ValueError("Estimated commissions must remain visibly provisional")
            if self.entry_type is BasisEntryType.COMMISSION_ACTUAL and self.provisional:
                raise ValueError("Actual commissions cannot be provisional")
        elif self.entry_type in _STOCK_EVIDENCE_TYPES:
            expected_side = (
                OrderSide.BUY
                if self.entry_type is BasisEntryType.ASSIGNMENT_STOCK_FILL
                else OrderSide.SELL
            )
            if (
                self.security_type is not SecurityType.STOCK
                or self.source_side is not expected_side
                or self.multiplier != 1
                or self.amount != 0
            ):
                raise ValueError(
                    "Stock evidence requires matching side, unit multiplier, and zero adjustment"
                )
        return self


class BasisProjectionInput(ChronosModel):
    wheel_cycle_id: str
    symbol: str
    underlying_con_id: PositiveInt
    account_fingerprint: str
    currency: str = "USD"
    broker_average_cost: Decimal = Field(ge=0)
    eligible_shares: Decimal = Field(ge=0)
    entries: tuple[BasisLedgerEntry, ...]
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING
    partial_assignment: bool = False
    stock_split_warning: bool = False
    symbol_change_warning: bool = False
    manual_trade_warning: bool = False
    unexplained_mismatch: bool = False

    @field_validator("wheel_cycle_id")
    @classmethod
    def validate_cycle_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("wheel_cycle_id must not be blank")
        return normalized

    @field_validator("symbol", "currency")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol and currency must not be blank")
        return normalized

    @field_validator("account_fingerprint")
    @classmethod
    def validate_account_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)


class BasisProjection(ChronosModel):
    wheel_cycle_id: str
    symbol: str
    underlying_con_id: PositiveInt
    account_fingerprint: str
    currency: str
    label: str = STRATEGY_BASIS_LABEL
    broker_average_cost: Decimal
    strategy_adjusted_basis: Decimal | None
    total_adjustment: Decimal
    eligible_shares: Decimal
    provisional_commission: bool
    reconciliation_status: ReconciliationStatus
    reasons: tuple[str, ...]


def project_strategy_basis(values: BasisProjectionInput) -> BasisProjection:
    """Project basis without mutating or replacing the broker average cost."""

    with _calculation_context(
        values.broker_average_cost,
        values.eligible_shares,
        *(entry.amount for entry in values.entries),
    ):
        return _project_strategy_basis(values)


def _project_strategy_basis(values: BasisProjectionInput) -> BasisProjection:
    manual_reasons: list[str] = []
    pending_reasons: list[str] = []
    if values.reconciliation_status is ReconciliationStatus.PENDING:
        pending_reasons.append("Reconciliation status is PENDING.")
    elif values.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW:
        manual_reasons.append("Reconciliation status requires MANUAL_REVIEW.")
    blockers = (
        (values.partial_assignment, "Partial assignment requires manual allocation review."),
        (values.stock_split_warning, "A stock split may have changed share allocation."),
        (values.symbol_change_warning, "A symbol change may have changed cycle ownership."),
        (values.manual_trade_warning, "An unmatched manual trade changed broker state."),
        (values.unexplained_mismatch, "Broker and ledger evidence do not reconcile."),
    )
    manual_reasons.extend(message for active, message in blockers if active)
    if values.eligible_shares <= 0:
        manual_reasons.append(
            "A positive reconciled share allocation is required to compute basis."
        )

    seen_ids: set[str] = set()
    seen_sources: set[tuple[BasisEntryType, str]] = set()
    premium_sources: set[str] = set()
    premium_roles: dict[str, BasisEntryType] = {}
    source_provenance: dict[str, tuple[object, ...]] = {}
    premium_and_manual_adjustment = Decimal("0")
    estimated_commissions: dict[str, Decimal] = {}
    actual_commissions: dict[str, Decimal] = {}
    for entry in values.entries:
        if entry.account_fingerprint != values.account_fingerprint:
            manual_reasons.append("A basis entry belongs to a different pseudonymous account.")
        if entry.id in seen_ids:
            manual_reasons.append(f"Duplicate basis entry ID {entry.id} was supplied.")
        seen_ids.add(entry.id)
        if entry.wheel_cycle_id != values.wheel_cycle_id or entry.symbol != values.symbol:
            manual_reasons.append("A basis entry belongs to a different cycle or symbol.")
        if entry.currency != values.currency:
            manual_reasons.append(
                "Basis entry currency differs; Chronos performs no implicit FX conversion."
            )
        if entry.reconciliation_status is ReconciliationStatus.PENDING:
            pending_reasons.append(f"Basis entry {entry.id} is pending reconciliation.")
        elif entry.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW:
            manual_reasons.append(f"Basis entry {entry.id} requires manual review.")
        if entry.source_execution_id is not None:
            source_key = (entry.entry_type, entry.source_execution_id)
            if source_key in seen_sources:
                manual_reasons.append(
                    "The same execution and basis entry type appeared more than once."
                )
            seen_sources.add(source_key)
            provenance = (
                entry.account_fingerprint,
                entry.contract_id,
                entry.security_type,
                entry.source_side,
                entry.quantity,
                entry.unit_price,
                entry.multiplier,
                entry.currency,
            )
            existing_provenance = source_provenance.get(entry.source_execution_id)
            if existing_provenance is not None and existing_provenance != provenance:
                manual_reasons.append(
                    "One broker execution has conflicting immutable fill provenance."
                )
            source_provenance[entry.source_execution_id] = provenance

        if entry.entry_type in _PREMIUM_TYPES:
            assert entry.source_execution_id is not None
            existing_role = premium_roles.get(entry.source_execution_id)
            if existing_role is not None and existing_role is not entry.entry_type:
                manual_reasons.append(
                    "One broker execution cannot be both opening and closing premium."
                )
            premium_roles[entry.source_execution_id] = entry.entry_type
            premium_sources.add(entry.source_execution_id)
            premium_and_manual_adjustment += entry.amount
        elif entry.entry_type is BasisEntryType.MANUAL_ADJUSTMENT:
            premium_and_manual_adjustment += entry.amount
        elif entry.entry_type is BasisEntryType.COMMISSION_ESTIMATE:
            assert entry.source_execution_id is not None
            estimated_commissions[entry.source_execution_id] = entry.amount
        elif entry.entry_type is BasisEntryType.COMMISSION_ACTUAL:
            assert entry.source_execution_id is not None
            actual_commissions[entry.source_execution_id] = entry.amount
        elif entry.entry_type in _STOCK_EVIDENCE_TYPES:
            manual_reasons.append(
                "Stock-fill evidence requires reconciled share allocation before basis projection."
            )

    commission_sources = set(estimated_commissions) | set(actual_commissions)
    for execution_id in sorted(commission_sources - premium_sources):
        manual_reasons.append(
            f"Commission evidence for {execution_id} has no matching option premium entry."
        )
    for execution_id in sorted(premium_sources - commission_sources):
        pending_reasons.append(
            f"Commission evidence is still pending for premium execution {execution_id}."
        )

    commission_adjustment = sum(
        (
            amount
            for execution_id, amount in actual_commissions.items()
            if execution_id in premium_sources
        ),
        start=Decimal("0"),
    )
    provisional_commission = False
    for execution_id, estimate in estimated_commissions.items():
        if execution_id in premium_sources and execution_id not in actual_commissions:
            commission_adjustment += estimate
            provisional_commission = True
    total_adjustment = premium_and_manual_adjustment + commission_adjustment

    if manual_reasons or pending_reasons:
        status = (
            ReconciliationStatus.MANUAL_REVIEW if manual_reasons else ReconciliationStatus.PENDING
        )
        return BasisProjection(
            wheel_cycle_id=values.wheel_cycle_id,
            symbol=values.symbol,
            underlying_con_id=values.underlying_con_id,
            account_fingerprint=values.account_fingerprint,
            currency=values.currency,
            broker_average_cost=values.broker_average_cost,
            strategy_adjusted_basis=None,
            total_adjustment=total_adjustment,
            eligible_shares=values.eligible_shares,
            provisional_commission=provisional_commission,
            reconciliation_status=status,
            reasons=tuple(dict.fromkeys((*manual_reasons, *pending_reasons))),
        )
    adjusted_basis = values.broker_average_cost + total_adjustment / values.eligible_shares
    projection_reasons = (
        "An estimated commission remains provisional."
        if provisional_commission
        else "Every included commission is actual or no commission applies."
    )
    return BasisProjection(
        wheel_cycle_id=values.wheel_cycle_id,
        symbol=values.symbol,
        underlying_con_id=values.underlying_con_id,
        account_fingerprint=values.account_fingerprint,
        currency=values.currency,
        broker_average_cost=values.broker_average_cost,
        strategy_adjusted_basis=adjusted_basis,
        total_adjustment=total_adjustment,
        eligible_shares=values.eligible_shares,
        provisional_commission=provisional_commission,
        reconciliation_status=ReconciliationStatus.RECONCILED,
        reasons=(projection_reasons,),
    )


def option_premium_entry(
    execution: BrokerExecution,
    *,
    wheel_cycle_id: str,
    opening: bool,
) -> BasisLedgerEntry:
    """Convert an option execution to a stable signed premium entry."""

    if not isinstance(execution.contract, OptionContract):
        raise ValueError("Option premium evidence requires an option execution")
    expected_side = OrderSide.SELL if opening else OrderSide.BUY
    if execution.side is not expected_side:
        raise ValueError("Execution side does not match opening/closing premium semantics")
    entry_type = (
        BasisEntryType.OPENING_OPTION_PREMIUM if opening else BasisEntryType.CLOSING_OPTION_PREMIUM
    )
    with _calculation_context(
        execution.price,
        execution.contract.multiplier,
        execution.quantity,
    ):
        gross = execution.price * execution.contract.multiplier * execution.quantity
        amount = -gross if opening else gross
    return BasisLedgerEntry(
        id=_execution_entry_id(wheel_cycle_id, entry_type, execution.execution_id),
        wheel_cycle_id=wheel_cycle_id,
        symbol=execution.contract.symbol,
        entry_type=entry_type,
        amount=amount,
        account_fingerprint=account_fingerprint(execution.account_id),
        contract_id=execution.contract.con_id,
        security_type=execution.contract.security_type,
        source_side=execution.side,
        quantity=execution.quantity,
        unit_price=execution.price,
        multiplier=execution.contract.multiplier,
        currency=execution.contract.currency,
        source_execution_id=execution.execution_id,
        occurred_at=execution.timestamp,
    )


def commission_entry(
    execution: BrokerExecution,
    *,
    wheel_cycle_id: str,
    amount: Decimal,
    actual: bool,
    occurred_at: datetime | None = None,
) -> BasisLedgerEntry:
    """Create an idempotent estimate or actual commission entry for one fill."""

    if not isinstance(execution.contract, OptionContract):
        raise ValueError("Commission basis evidence is limited to option executions")
    if actual:
        if execution.commission is None or execution.commission_currency is None:
            raise ValueError("Actual commission evidence requires a broker commission report")
        if amount != execution.commission:
            raise ValueError("Actual commission amount must match broker evidence exactly")
        if execution.commission_currency != execution.contract.currency.strip().upper():
            raise ValueError("Actual commission currency must match the option contract currency")
    entry_type = BasisEntryType.COMMISSION_ACTUAL if actual else BasisEntryType.COMMISSION_ESTIMATE
    return BasisLedgerEntry(
        id=_execution_entry_id(wheel_cycle_id, entry_type, execution.execution_id),
        wheel_cycle_id=wheel_cycle_id,
        symbol=execution.contract.symbol,
        entry_type=entry_type,
        amount=amount,
        account_fingerprint=account_fingerprint(execution.account_id),
        contract_id=execution.contract.con_id,
        security_type=execution.contract.security_type,
        source_side=execution.side,
        quantity=execution.quantity,
        unit_price=execution.price,
        multiplier=execution.contract.multiplier,
        currency=execution.contract.currency,
        provisional=not actual,
        source_execution_id=execution.execution_id,
        occurred_at=occurred_at or execution.timestamp,
    )


def stock_fill_evidence_entry(
    execution: BrokerExecution,
    *,
    wheel_cycle_id: str,
    assignment: bool,
) -> BasisLedgerEntry:
    """Record assignment/called-away stock evidence without changing broker basis."""

    if execution.contract.security_type is not SecurityType.STOCK:
        raise ValueError("Stock-fill evidence requires a stock execution")
    expected_side = OrderSide.BUY if assignment else OrderSide.SELL
    if execution.side is not expected_side:
        raise ValueError("Stock execution side does not match assignment semantics")
    entry_type = (
        BasisEntryType.ASSIGNMENT_STOCK_FILL
        if assignment
        else BasisEntryType.CALLED_AWAY_STOCK_FILL
    )
    return BasisLedgerEntry(
        id=_execution_entry_id(wheel_cycle_id, entry_type, execution.execution_id),
        wheel_cycle_id=wheel_cycle_id,
        symbol=execution.contract.symbol,
        entry_type=entry_type,
        amount=Decimal("0"),
        account_fingerprint=account_fingerprint(execution.account_id),
        contract_id=execution.contract.con_id,
        security_type=execution.contract.security_type,
        source_side=execution.side,
        quantity=execution.quantity,
        unit_price=execution.price,
        multiplier=Decimal("1"),
        currency=execution.contract.currency,
        source_execution_id=execution.execution_id,
        occurred_at=execution.timestamp,
    )


def _execution_entry_id(
    wheel_cycle_id: str,
    entry_type: BasisEntryType,
    execution_id: str,
) -> str:
    material = f"{wheel_cycle_id}|{entry_type.value}|{execution_id}".encode()
    return f"BASIS-{sha256(material).hexdigest()[:48]}"

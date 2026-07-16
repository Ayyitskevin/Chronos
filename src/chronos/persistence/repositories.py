"""Small repositories that keep SQLAlchemy details out of services."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import (
    BasisEntryType,
    OrderSide,
    ReconciliationStatus,
    SecurityType,
)
from chronos.persistence.schema import (
    ApplicationEventRow,
    BasisEntryRow,
    CandidateEvaluationRow,
    CommissionRow,
    DatabaseScopeRow,
    FillRow,
    RejectedCandidateReasonRow,
    WheelCycleRow,
)
from chronos.strategy.basis import BasisLedgerEntry
from chronos.strategy.strike_resolver import ResolverContext, StrikeResolution

_RAW_ACCOUNT_ID_PATTERN = re.compile(r"\bD?[UF]\d{4,}\b", flags=re.IGNORECASE)
_RAW_ACCOUNT_EVENT_KEYS = frozenset(
    {
        "account",
        "accountid",
        "accountnumber",
        "acct",
        "acctcode",
        "brokeraccount",
        "brokeraccountid",
    }
)
type _CandidateRejectionEvidence = tuple[str, str, str]


class ApplicationEventRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def append(
        self,
        *,
        event_type: str,
        message: str,
        severity: str = "INFO",
        correlation_id: str | None = None,
        symbol: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> int:
        with self._sessions.begin() as session:
            _require_scope(session)
            _reject_raw_account_event_data(
                persisted_values=(
                    event_type,
                    severity,
                    correlation_id,
                    symbol,
                    message,
                    event_data or {},
                )
            )
            row = ApplicationEventRow(
                event_type=event_type,
                severity=severity,
                correlation_id=correlation_id,
                symbol=symbol,
                message=message,
                event_data=event_data or {},
            )
            session.add(row)
            session.flush()
            return row.id

    def recent(self, *, limit: int = 100) -> Sequence[ApplicationEventRow]:
        with self._sessions() as session:
            _require_scope(session)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ValueError("Application event limit must be an integer from 1 through 1000")
            statement = (
                select(ApplicationEventRow)
                .order_by(ApplicationEventRow.occurred_at.desc())
                .limit(limit)
            )
            return tuple(session.scalars(statement))


class BasisLedgerRepository:
    """Append idempotent basis evidence and expose deterministic cycle history."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def append(self, entry: BasisLedgerEntry) -> bool:
        """Return False for an exact replay; reject conflicting duplicate evidence."""

        with self._sessions.begin() as session:
            scope = _require_scope(session)
            _validate_basis_source(session, scope, entry)
            existing = _find_basis_row(session, entry)
            if existing is not None:
                if _basis_row_to_model(existing) == entry:
                    return False
                raise ValueError("Conflicting basis evidence already exists for this source")
            row = _basis_model_to_row(entry)
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as error:
                concurrent = _find_basis_row(session, entry)
                if concurrent is not None and _basis_row_to_model(concurrent) == entry:
                    return False
                raise ValueError(
                    "Basis evidence conflicted with a concurrent write or missing owner"
                ) from error
            return True

    def for_cycle(self, wheel_cycle_id: str) -> tuple[BasisLedgerEntry, ...]:
        with self._sessions() as session:
            _require_scope(session)
            rows = session.scalars(
                select(BasisEntryRow)
                .where(BasisEntryRow.wheel_cycle_id == wheel_cycle_id)
                .order_by(BasisEntryRow.occurred_at, BasisEntryRow.id)
            )
            return tuple(_basis_row_to_model(row) for row in rows)


class CandidateEvaluationRepository:
    """Persist resolver inputs, rankings, and every rejected reason atomically."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record(
        self,
        *,
        evaluation_id: str,
        context: ResolverContext,
        resolution: StrikeResolution,
        wheel_cycle_id: str | None = None,
    ) -> bool:
        input_snapshot = context.model_dump(mode="json")
        ranking_snapshot = {
            "algorithm_version": resolution.algorithm_version,
            "resolution": resolution.model_dump(mode="json"),
        }
        symbol = context.underlying_quote.contract.symbol
        selected_contract_id = (
            resolution.selected.contract.con_id if resolution.selected is not None else None
        )
        rejection_evidence = _candidate_rejection_evidence(resolution)
        context_cycle_id = context.wheel_cycle_id
        with self._sessions.begin() as session:
            scope = _require_scope(session)
            _validate_candidate_scope_and_cycle(
                session,
                scope,
                context=context,
                requested_wheel_cycle_id=wheel_cycle_id,
                symbol=symbol,
            )
            existing = session.get(CandidateEvaluationRow, evaluation_id)
            if existing is not None:
                if _candidate_row_matches(
                    session,
                    existing,
                    symbol=symbol,
                    wheel_cycle_id=context_cycle_id,
                    outcome=resolution.status.value,
                    selected_contract_id=selected_contract_id,
                    input_snapshot=input_snapshot,
                    ranking_snapshot=ranking_snapshot,
                    rejection_evidence=rejection_evidence,
                ):
                    return False
                raise ValueError("Candidate evaluation ID already has different evidence")
            row = CandidateEvaluationRow(
                id=evaluation_id,
                symbol=symbol,
                wheel_cycle_id=context_cycle_id,
                outcome=resolution.status.value,
                selected_contract_id=selected_contract_id,
                input_snapshot=input_snapshot,
                ranking_snapshot=ranking_snapshot,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as error:
                concurrent = session.get(CandidateEvaluationRow, evaluation_id)
                if concurrent is not None and _candidate_row_matches(
                    session,
                    concurrent,
                    symbol=symbol,
                    wheel_cycle_id=context_cycle_id,
                    outcome=resolution.status.value,
                    selected_contract_id=selected_contract_id,
                    input_snapshot=input_snapshot,
                    ranking_snapshot=ranking_snapshot,
                    rejection_evidence=rejection_evidence,
                ):
                    return False
                raise ValueError(
                    "Candidate evaluation conflicted with a concurrent write or missing owner"
                ) from error
            for rejected in resolution.rejected:
                contract_key = f"{rejected.contract.symbol}:{rejected.contract.con_id}"
                for reason in rejected.rejection_reasons:
                    session.add(
                        RejectedCandidateReasonRow(
                            evaluation_id=evaluation_id,
                            contract_key=contract_key,
                            reason_code=reason.code.value,
                            explanation=reason.explanation,
                        )
                    )
            return True


def _basis_row_to_model(row: BasisEntryRow) -> BasisLedgerEntry:
    return BasisLedgerEntry(
        id=row.id,
        wheel_cycle_id=row.wheel_cycle_id,
        symbol=row.symbol,
        entry_type=BasisEntryType(row.entry_type),
        amount=row.amount,
        account_fingerprint=row.account_fingerprint,
        contract_id=row.contract_id,
        security_type=SecurityType(row.security_type) if row.security_type is not None else None,
        source_side=OrderSide(row.source_side) if row.source_side is not None else None,
        quantity=row.quantity,
        unit_price=row.unit_price,
        multiplier=row.multiplier,
        currency=row.currency,
        provisional=row.provisional,
        reconciliation_status=ReconciliationStatus(row.reconciliation_status),
        source_execution_id=row.source_execution_id,
        source_note=row.source_note,
        occurred_at=row.occurred_at,
    )


def _basis_model_to_row(entry: BasisLedgerEntry) -> BasisEntryRow:
    return BasisEntryRow(
        id=entry.id,
        wheel_cycle_id=entry.wheel_cycle_id,
        symbol=entry.symbol,
        entry_type=entry.entry_type.value,
        amount=entry.amount,
        account_fingerprint=entry.account_fingerprint,
        contract_id=entry.contract_id,
        security_type=entry.security_type.value if entry.security_type is not None else None,
        source_side=entry.source_side.value if entry.source_side is not None else None,
        quantity=entry.quantity,
        unit_price=entry.unit_price,
        multiplier=entry.multiplier,
        currency=entry.currency,
        provisional=entry.provisional,
        reconciliation_status=entry.reconciliation_status.value,
        source_execution_id=entry.source_execution_id,
        source_note=entry.source_note,
        occurred_at=entry.occurred_at,
    )


def _find_basis_row(session: Session, entry: BasisLedgerEntry) -> BasisEntryRow | None:
    existing = session.get(BasisEntryRow, entry.id)
    if existing is None and entry.source_execution_id is not None:
        existing = session.scalar(
            select(BasisEntryRow).where(
                BasisEntryRow.wheel_cycle_id == entry.wheel_cycle_id,
                BasisEntryRow.entry_type == entry.entry_type.value,
                BasisEntryRow.source_execution_id == entry.source_execution_id,
            )
        )
    return existing


def _require_scope(session: Session) -> DatabaseScopeRow:
    scope = session.get(DatabaseScopeRow, 1)
    if scope is None:
        raise RuntimeError(
            "Chronos database must be bound to a broker scope before account-specific access"
        )
    return scope


def _validate_basis_source(
    session: Session,
    scope: DatabaseScopeRow,
    entry: BasisLedgerEntry,
) -> None:
    if entry.account_fingerprint != scope.account_fingerprint:
        raise ValueError("Basis evidence belongs to a different pseudonymous account scope")
    cycle = session.get(WheelCycleRow, entry.wheel_cycle_id)
    if cycle is None:
        raise ValueError("Basis evidence has no persisted Wheel cycle owner")
    if cycle.symbol != entry.symbol:
        raise ValueError("Basis evidence symbol does not match its Wheel cycle owner")
    if entry.source_execution_id is None:
        return

    fill = session.get(FillRow, entry.source_execution_id)
    if fill is None:
        raise ValueError("Basis execution evidence has no persisted source fill")
    assert entry.contract_id is not None
    assert entry.security_type is not None
    assert entry.source_side is not None
    assert entry.quantity is not None
    assert entry.unit_price is not None
    assert entry.multiplier is not None
    mismatches = (
        ("cycle", fill.wheel_cycle_id, entry.wheel_cycle_id),
        ("symbol", fill.symbol, entry.symbol),
        ("contract", fill.contract_id, entry.contract_id),
        ("side", fill.side, entry.source_side.value),
        ("quantity", fill.quantity, entry.quantity),
        ("price", fill.price, entry.unit_price),
        ("multiplier", fill.multiplier, entry.multiplier),
        ("currency", fill.currency, entry.currency),
        ("security_type", fill.security_type, entry.security_type.value),
        ("account_fingerprint", fill.account_fingerprint, entry.account_fingerprint),
    )
    mismatch_names = [name for name, persisted, supplied in mismatches if persisted != supplied]
    if mismatch_names:
        raise ValueError(
            "Basis evidence does not exactly match persisted fill provenance: "
            + ", ".join(mismatch_names)
        )
    if entry.entry_type is BasisEntryType.COMMISSION_ACTUAL:
        commission = session.scalar(
            select(CommissionRow).where(CommissionRow.execution_id == entry.source_execution_id)
        )
        if commission is None:
            raise ValueError("Actual commission evidence has no persisted commission report")
        commission_mismatches = (
            entry.amount != commission.amount,
            entry.currency != commission.currency,
        )
        if any(commission_mismatches):
            raise ValueError(
                "Actual commission evidence does not exactly match the persisted "
                "amount and currency"
            )


def _validate_candidate_scope_and_cycle(
    session: Session,
    scope: DatabaseScopeRow,
    *,
    context: ResolverContext,
    requested_wheel_cycle_id: str | None,
    symbol: str,
) -> None:
    if context.capital.account_fingerprint != scope.account_fingerprint:
        raise ValueError("Candidate evidence belongs to a different pseudonymous account scope")
    if requested_wheel_cycle_id is not None and requested_wheel_cycle_id != context.wheel_cycle_id:
        raise ValueError("Candidate cycle argument does not match the resolver context")
    cycle = session.get(WheelCycleRow, context.wheel_cycle_id)
    if cycle is None:
        raise ValueError("Candidate evidence references no persisted Wheel cycle")
    if cycle.symbol != symbol:
        raise ValueError("Candidate evidence symbol does not match its Wheel cycle owner")


def _candidate_rejection_evidence(
    resolution: StrikeResolution,
) -> tuple[_CandidateRejectionEvidence, ...]:
    return tuple(
        (
            f"{rejected.contract.symbol}:{rejected.contract.con_id}",
            reason.code.value,
            reason.explanation,
        )
        for rejected in resolution.rejected
        for reason in rejected.rejection_reasons
    )


def _candidate_row_matches(
    session: Session,
    row: CandidateEvaluationRow,
    *,
    symbol: str,
    wheel_cycle_id: str | None,
    outcome: str,
    selected_contract_id: int | None,
    input_snapshot: dict[str, Any],
    ranking_snapshot: dict[str, Any],
    rejection_evidence: tuple[_CandidateRejectionEvidence, ...],
) -> bool:
    parent_matches = (
        row.symbol == symbol
        and row.wheel_cycle_id == wheel_cycle_id
        and row.outcome == outcome
        and row.selected_contract_id == selected_contract_id
        and row.input_snapshot == input_snapshot
        and row.ranking_snapshot == ranking_snapshot
    )
    if not parent_matches:
        return False
    persisted_rejections = session.scalars(
        select(RejectedCandidateReasonRow).where(RejectedCandidateReasonRow.evaluation_id == row.id)
    )
    persisted_evidence = Counter(
        (reason.contract_key, reason.reason_code, reason.explanation)
        for reason in persisted_rejections
    )
    return persisted_evidence == Counter(rejection_evidence)


def _reject_raw_account_event_data(
    *,
    persisted_values: Sequence[object],
) -> None:
    if _contains_raw_account_identifier(persisted_values):
        raise ValueError("Application events must not contain raw broker account IDs")


def _contains_raw_account_identifier(value: object) -> bool:
    if isinstance(value, str):
        return _RAW_ACCOUNT_ID_PATTERN.search(value) is not None
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
            collapsed_key = normalized_key.replace("_", "")
            if collapsed_key in _RAW_ACCOUNT_EVENT_KEYS and nested_value not in (None, ""):
                return True
            if _contains_raw_account_identifier(key) or _contains_raw_account_identifier(
                nested_value
            ):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_contains_raw_account_identifier(item) for item in value)
    return False

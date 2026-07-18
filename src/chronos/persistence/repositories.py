"""Small repositories that keep SQLAlchemy details out of services."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import (
    BasisEntryType,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    SecurityType,
    WheelStage,
)
from chronos.domain.models import BrokerOrderIdentity, LocalReconciliationEvidence
from chronos.persistence.schema import (
    ApplicationEventRow,
    BasisEntryRow,
    CandidateEvaluationRow,
    CommissionRow,
    DatabaseScopeRow,
    FillRow,
    OrderDraftRow,
    RejectedCandidateReasonRow,
    StrategyStateRow,
    SubmittedOrderRow,
    WheelCycleRow,
)
from chronos.strategy.basis import BasisLedgerEntry
from chronos.strategy.strike_resolver import ResolverContext, StrikeResolution
from chronos.utils.identifiers import account_fingerprint, normalize_account_fingerprint

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

_ACTIVE_SUBMITTED_ORDER_LIFECYCLES = frozenset(
    {
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.PARTIALLY_FILLED,
        # An in-flight cancel is still a working order at the broker until the
        # cancel confirms; it must count as active for ownership/reconciliation.
        OrderLifecycle.CANCEL_PENDING,
    }
)
_TERMINAL_SUBMITTED_ORDER_LIFECYCLES = frozenset(
    {
        OrderLifecycle.FILLED,
        OrderLifecycle.CANCELLED,
        OrderLifecycle.REJECTED,
    }
)
_PRE_SUBMISSION_ORDER_LIFECYCLES = frozenset(
    {
        OrderLifecycle.DRAFT,
        OrderLifecycle.VALIDATED,
        OrderLifecycle.WHAT_IF_PREVIEWED,
        OrderLifecycle.USER_CONFIRMED,
    }
)
_VALID_WHEEL_CYCLE_STATUSES = frozenset({"OPEN", "CLOSED"})

_LOCAL_CYCLE_ISSUE = "Persisted Wheel-cycle evidence is incomplete or malformed."
_LOCAL_STRATEGY_ISSUE = "Persisted strategy-state evidence is incomplete or malformed."
_LOCAL_ORDER_ISSUE = "Persisted order evidence is incomplete or malformed."
_LOCAL_FILL_ISSUE = "Persisted fill evidence is incomplete or malformed."
_LOCAL_BASIS_ISSUE = "Persisted strategy-basis evidence is incomplete or malformed."
_LOCAL_UNRESOLVED = (
    "Local strategy evidence exists but is conservatively unresolved by this reader."
)


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


class OrderOwnershipRepository:
    """Read exact active Chronos order identities from persisted evidence."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def active_identities(self, current_account_id: str) -> frozenset[BrokerOrderIdentity]:
        """Return active identities only when the caller matches the bound account scope."""

        with self._sessions.begin() as session:
            _require_matching_account_scope(session, current_account_id)
            return _active_identities_in_session(session, current_account_id)


class LocalReconciliationRepository:
    """Read every local ownership-bearing table in one database transaction."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        """Return exact active orders and conservative per-symbol local provenance."""

        with self._sessions.begin() as session:
            scope = _require_matching_account_scope(session, current_account_id)
            cycles = tuple(session.scalars(select(WheelCycleRow)))
            strategy_states = tuple(session.scalars(select(StrategyStateRow)))
            drafts = tuple(session.scalars(select(OrderDraftRow)))
            submitted_orders = tuple(session.scalars(select(SubmittedOrderRow)))
            fills = tuple(session.scalars(select(FillRow)))
            basis_entries = tuple(session.scalars(select(BasisEntryRow)))

            unresolved_symbols: set[str] = set()
            issues: list[str] = []

            cycles_by_id = {cycle.id: cycle for cycle in cycles}
            drafts_by_id = {draft.correlation_id: draft for draft in drafts}
            submitted_by_id = {
                submitted.correlation_id: submitted for submitted in submitted_orders
            }
            fills_by_id = {fill.execution_id: fill for fill in fills}

            for cycle in cycles:
                _collect_local_symbol(
                    cycle.symbol,
                    unresolved_symbols=unresolved_symbols,
                    issues=issues,
                    issue=_LOCAL_CYCLE_ISSUE,
                )
                if (
                    not _is_nonblank_string(cycle.id)
                    or cycle.status not in _VALID_WHEEL_CYCLE_STATUSES
                    or not _is_aware_datetime(cycle.opened_at)
                    or (cycle.status == "OPEN" and cycle.closed_at is not None)
                    or (
                        cycle.status == "CLOSED"
                        and (
                            cycle.closed_at is None
                            or not _is_aware_datetime(cycle.closed_at)
                            or cycle.closed_at < cycle.opened_at
                        )
                    )
                ):
                    _append_issue(issues, _LOCAL_CYCLE_ISSUE)

            for state in strategy_states:
                _collect_local_symbol(
                    state.symbol,
                    unresolved_symbols=unresolved_symbols,
                    issues=issues,
                    issue=_LOCAL_STRATEGY_ISSUE,
                )
                if not _is_enum_value(WheelStage, state.wheel_stage) or not _is_enum_value(
                    ReconciliationStatus, state.reconciliation_status
                ):
                    _append_issue(issues, _LOCAL_STRATEGY_ISSUE)
                if state.wheel_cycle_id is not None:
                    state_cycle = cycles_by_id.get(state.wheel_cycle_id)
                    if (
                        not _is_nonblank_string(state.wheel_cycle_id)
                        or state_cycle is None
                        or state_cycle.symbol != state.symbol
                    ):
                        _append_issue(issues, _LOCAL_STRATEGY_ISSUE)

            for draft in drafts:
                _collect_local_symbol(
                    draft.symbol,
                    unresolved_symbols=unresolved_symbols,
                    issues=issues,
                    issue=_LOCAL_ORDER_ISSUE,
                )
                lifecycle = _optional_enum_value(OrderLifecycle, draft.lifecycle)
                if (
                    not _is_nonblank_string(draft.correlation_id)
                    or lifecycle is None
                    or not _is_enum_value(OrderIntent, draft.intent)
                    or not _is_safe_masked_account(draft.account_id_masked, current_account_id)
                    or draft.contract_id <= 0
                    or draft.quantity <= 0
                    or draft.limit_price <= 0
                ):
                    _append_issue(issues, _LOCAL_ORDER_ISSUE)
                if draft.wheel_cycle_id is not None:
                    draft_cycle = cycles_by_id.get(draft.wheel_cycle_id)
                    if (
                        not _is_nonblank_string(draft.wheel_cycle_id)
                        or draft_cycle is None
                        or draft_cycle.symbol != draft.symbol
                    ):
                        _append_issue(issues, _LOCAL_ORDER_ISSUE)
                if (
                    lifecycle is not None
                    and lifecycle not in _PRE_SUBMISSION_ORDER_LIFECYCLES
                    and draft.correlation_id not in submitted_by_id
                ):
                    _append_issue(issues, _LOCAL_ORDER_ISSUE)

            for submitted in submitted_orders:
                submitted_draft = drafts_by_id.get(submitted.correlation_id)
                lifecycle = _optional_enum_value(OrderLifecycle, submitted.lifecycle)
                if (
                    not _is_nonblank_string(submitted.correlation_id)
                    or submitted_draft is None
                    or lifecycle
                    not in (
                        _ACTIVE_SUBMITTED_ORDER_LIFECYCLES | _TERMINAL_SUBMITTED_ORDER_LIFECYCLES
                    )
                    or (
                        submitted_draft is not None
                        and submitted_draft.lifecycle != submitted.lifecycle
                    )
                ):
                    _append_issue(issues, _LOCAL_ORDER_ISSUE)

            for fill in fills:
                _collect_local_symbol(
                    fill.symbol,
                    unresolved_symbols=unresolved_symbols,
                    issues=issues,
                    issue=_LOCAL_FILL_ISSUE,
                )
                if (
                    not _is_nonblank_string(fill.execution_id)
                    or not _is_enum_value(SecurityType, fill.security_type)
                    or not _is_enum_value(OrderSide, fill.side)
                    or not _is_canonical_code(fill.currency)
                    or fill.contract_id <= 0
                    or fill.quantity <= 0
                    or fill.price <= 0
                    or fill.multiplier <= 0
                    or not _fingerprint_matches(fill.account_fingerprint, scope)
                ):
                    _append_issue(issues, _LOCAL_FILL_ISSUE)
                fill_draft: OrderDraftRow | None = None
                if fill.correlation_id is not None:
                    fill_draft = drafts_by_id.get(fill.correlation_id)
                    fill_submitted = submitted_by_id.get(fill.correlation_id)
                    if (
                        not _is_nonblank_string(fill.correlation_id)
                        or fill_draft is None
                        or fill_submitted is None
                        or fill_draft.symbol != fill.symbol
                        or fill_submitted.broker_order_id != fill.broker_order_id
                    ):
                        _append_issue(issues, _LOCAL_FILL_ISSUE)
                if fill.wheel_cycle_id is not None:
                    fill_cycle = cycles_by_id.get(fill.wheel_cycle_id)
                    if (
                        not _is_nonblank_string(fill.wheel_cycle_id)
                        or fill_cycle is None
                        or fill_cycle.symbol != fill.symbol
                        or (
                            fill_draft is not None
                            and fill_draft.wheel_cycle_id is not None
                            and fill_draft.wheel_cycle_id != fill.wheel_cycle_id
                        )
                    ):
                        _append_issue(issues, _LOCAL_FILL_ISSUE)

            for entry in basis_entries:
                _collect_local_symbol(
                    entry.symbol,
                    unresolved_symbols=unresolved_symbols,
                    issues=issues,
                    issue=_LOCAL_BASIS_ISSUE,
                )
                try:
                    if not _is_canonical_code(entry.currency) or not _fingerprint_matches(
                        entry.account_fingerprint, scope
                    ):
                        raise ValueError
                    model = _basis_row_to_model(entry)
                    _validate_basis_source(session, scope, model)
                    if (
                        model.source_execution_id is not None
                        and model.source_execution_id not in fills_by_id
                    ):
                        raise ValueError
                except (TypeError, ValueError):
                    _append_issue(issues, _LOCAL_BASIS_ISSUE)

            try:
                identities = _active_identities_in_session(session, current_account_id)
            except ValueError:
                identities = frozenset()
                _append_issue(issues, _LOCAL_ORDER_ISSUE)

            reasons = [*issues]
            if unresolved_symbols:
                reasons.append(_LOCAL_UNRESOLVED)
            return LocalReconciliationEvidence(
                active_order_identities=identities,
                unresolved_symbols=frozenset(unresolved_symbols),
                complete=not issues,
                reasons=tuple(reasons),
            )


def _active_identities_in_session(
    session: Session,
    current_account_id: str,
) -> frozenset[BrokerOrderIdentity]:
    statement = select(SubmittedOrderRow, OrderDraftRow).outerjoin(
        OrderDraftRow,
        SubmittedOrderRow.correlation_id == OrderDraftRow.correlation_id,
    )
    identities: set[BrokerOrderIdentity] = set()
    for submitted, draft in session.execute(statement):
        if draft is None:
            raise ValueError("Persisted submitted order has no draft owner")
        lifecycle = _submitted_order_lifecycle(submitted.lifecycle)
        identity = _persisted_order_identity(
            session,
            current_account_id=current_account_id,
            submitted_lifecycle=lifecycle,
            submitted=submitted,
            draft=draft,
        )
        if lifecycle in _ACTIVE_SUBMITTED_ORDER_LIFECYCLES:
            identities.add(identity)
    return frozenset(identities)


def _submitted_order_lifecycle(value: str) -> OrderLifecycle:
    try:
        lifecycle = OrderLifecycle(value)
    except (TypeError, ValueError):
        raise ValueError("Persisted submitted order has an unknown lifecycle") from None
    if lifecycle not in (_ACTIVE_SUBMITTED_ORDER_LIFECYCLES | _TERMINAL_SUBMITTED_ORDER_LIFECYCLES):
        raise ValueError("Persisted submitted order has an invalid pre-submission lifecycle")
    return lifecycle


def _persisted_order_identity(
    session: Session,
    *,
    current_account_id: str,
    submitted_lifecycle: OrderLifecycle,
    submitted: SubmittedOrderRow,
    draft: OrderDraftRow,
) -> BrokerOrderIdentity:
    try:
        draft_lifecycle = OrderLifecycle(draft.lifecycle)
    except (TypeError, ValueError):
        raise ValueError("Persisted order draft has an unknown lifecycle") from None
    if draft_lifecycle is not submitted_lifecycle:
        raise ValueError("Persisted order draft and submitted lifecycles do not agree")

    try:
        intent = OrderIntent(draft.intent)
    except (TypeError, ValueError):
        raise ValueError("Persisted order draft has an unknown intent") from None
    side = _side_for_order_intent(intent)

    cycle_id = draft.wheel_cycle_id
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise ValueError("Persisted order draft has no Wheel cycle owner")
    cycle = session.get(WheelCycleRow, cycle_id)
    if cycle is None:
        raise ValueError("Persisted order draft has no valid Wheel cycle owner")
    if cycle.symbol != draft.symbol:
        raise ValueError("Persisted order draft symbol does not match its Wheel cycle owner")
    if submitted_lifecycle in _ACTIVE_SUBMITTED_ORDER_LIFECYCLES and cycle.status != "OPEN":
        raise ValueError("Persisted active order does not belong to an open Wheel cycle")

    normalized_account_id = current_account_id.strip()
    masked_account_id = draft.account_id_masked
    if (
        not isinstance(masked_account_id, str)
        or not masked_account_id.strip()
        or masked_account_id.strip() == normalized_account_id
    ):
        raise ValueError("Persisted order draft has invalid masked account ownership")
    if not isinstance(draft.symbol, str) or draft.symbol != draft.symbol.strip().upper():
        raise ValueError("Persisted order draft has a malformed symbol")
    if (
        not isinstance(submitted.order_ref, str)
        or submitted.order_ref != submitted.order_ref.strip()
    ):
        raise ValueError("Persisted submitted order has a malformed order reference")
    if not submitted.order_ref:
        raise ValueError("Persisted submitted order has a malformed order reference")

    try:
        quantity = Decimal(draft.quantity)
        return BrokerOrderIdentity(
            account_id=current_account_id,
            client_id=submitted.client_id,
            broker_order_id=submitted.broker_order_id,
            permanent_id=submitted.permanent_id,
            order_ref=submitted.order_ref,
            symbol=draft.symbol,
            contract_id=draft.contract_id,
            side=side,
            quantity=quantity,
        )
    except (TypeError, ValueError):
        raise ValueError("Persisted order ownership evidence is malformed") from None


def _side_for_order_intent(intent: OrderIntent) -> OrderSide:
    if intent in {OrderIntent.OPEN_SHORT_PUT, OrderIntent.OPEN_COVERED_CALL}:
        return OrderSide.SELL
    if intent in {OrderIntent.CLOSE_SHORT_OPTION, OrderIntent.OPEN_LONG_STOCK}:
        return OrderSide.BUY
    if intent is OrderIntent.CLOSE_LONG_STOCK:
        return OrderSide.SELL
    raise ValueError("Persisted order draft has an unsupported intent")


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


def _require_matching_account_scope(
    session: Session,
    current_account_id: str,
) -> DatabaseScopeRow:
    scope = _require_scope(session)
    scopes = tuple(session.scalars(select(DatabaseScopeRow).limit(2)))
    if len(scopes) != 1 or scopes[0].id != 1:
        raise RuntimeError("Chronos database must contain exactly one bound broker scope")
    try:
        normalized_scope_fingerprint = normalize_account_fingerprint(scope.account_fingerprint)
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("Chronos database has a malformed pseudonymous account scope") from None
    if normalized_scope_fingerprint != scope.account_fingerprint:
        raise RuntimeError("Chronos database has a malformed pseudonymous account scope")
    if account_fingerprint(current_account_id) != scope.account_fingerprint:
        raise ValueError(
            "Current broker account does not match the bound pseudonymous database scope"
        )
    return scope


def _collect_local_symbol(
    value: object,
    *,
    unresolved_symbols: set[str],
    issues: list[str],
    issue: str,
) -> None:
    if not _is_canonical_code(value):
        _append_issue(issues, issue)
        return
    assert isinstance(value, str)
    unresolved_symbols.add(value)


def _is_nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _is_canonical_code(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value) and value == value.strip() and value == value.upper()
    )


def _is_aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _optional_enum_value[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: object,
) -> EnumT | None:
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _is_enum_value(enum_type: type[StrEnum], value: object) -> bool:
    return _optional_enum_value(enum_type, value) is not None


def _is_safe_masked_account(value: object, current_account_id: str) -> bool:
    if not _is_nonblank_string(value):
        return False
    assert isinstance(value, str)
    return value != current_account_id.strip() and _RAW_ACCOUNT_ID_PATTERN.search(value) is None


def _fingerprint_matches(value: object, scope: DatabaseScopeRow) -> bool:
    if not isinstance(value, str):
        return False
    try:
        normalized = normalize_account_fingerprint(value)
    except ValueError:
        return False
    return value == normalized == scope.account_fingerprint


def _append_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


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

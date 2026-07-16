"""Session-only retention boundary for explicit portfolio observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from math import isfinite
from typing import Annotated, Literal

from pydantic import Field

from chronos.domain.enums import ReconciliationStatus
from chronos.domain.models import ChronosModel, OptionContract, UnderlyingContract
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationOrderView,
    ReconciliationPositionView,
    ReconciliationResult,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.ui.runtime_scope import RuntimeScopeView, validate_runtime_scope
from chronos.utils.logging import mask_account_identifiers

_MIN_DISPLAY_TIME = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_CURRENCY_CHARACTERS = 8
_MAX_REASON_CHARACTERS = 500
_MAX_SYMBOL_CHARACTERS = 32


class PortfolioObservationSessionRecord(ChronosModel):
    """Historical presentation record that creates no service or order authority."""

    scope_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    result: ReconciliationResult
    historical_display_only: Literal[True] = True
    authority_created: Literal[False] = False
    persistence_recorded: Literal[False] = False
    opening_actions_locked: Literal[True] = True


def retain_portfolio_observation(
    scope: RuntimeScopeView,
    result: ReconciliationResult,
) -> PortfolioObservationSessionRecord:
    """Validate and bind one explicit observation for historical session display."""

    validated_scope = validate_runtime_scope(scope)
    validated_result = _validated_result(result, validated_scope)
    try:
        record = PortfolioObservationSessionRecord(
            scope_digest=_scope_digest(validated_scope),
            result=validated_result,
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("portfolio observation could not be retained safely") from None
    return validate_portfolio_observation_record(record, validated_scope)


def validate_portfolio_observation_record(
    value: object,
    scope: RuntimeScopeView,
) -> PortfolioObservationSessionRecord:
    """Revalidate exact session state and its startup-scope binding before display."""

    validated_scope = validate_runtime_scope(scope)
    if type(value) is not PortfolioObservationSessionRecord or not _has_exact_model_storage(
        value, PortfolioObservationSessionRecord.model_fields
    ):
        raise ValueError("portfolio observation record is unavailable")
    try:
        _validated_result(value.result, validated_scope)
        dumped = PortfolioObservationSessionRecord.model_dump(
            value,
            mode="python",
            warnings=False,
        )
        validated = PortfolioObservationSessionRecord.model_validate(dumped, strict=True)
        canonical = PortfolioObservationSessionRecord.model_dump(
            validated,
            mode="python",
            warnings=False,
        )
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        _validated_result(validated.result, validated_scope)
        if validated.scope_digest != _scope_digest(validated_scope):
            raise ValueError
        serialized = PortfolioObservationSessionRecord.model_dump_json(
            validated,
            warnings=False,
        )
        if mask_account_identifiers(serialized) != serialized:
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("portfolio observation record is unavailable") from None


def _validated_result(
    value: object,
    scope: RuntimeScopeView,
) -> ReconciliationResult:
    if type(value) is not ReconciliationResult or not _has_exact_reconciliation_storage(value):
        raise ValueError("portfolio observation result is unavailable")
    try:
        dumped = ReconciliationResult.model_dump(value, mode="python", warnings=False)
        validated = ReconciliationResult.model_validate(dumped, strict=True)
        canonical = ReconciliationResult.model_dump(validated, mode="python", warnings=False)
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        _require_result_coherence(validated, scope)
        serialized = ReconciliationResult.model_dump_json(validated, warnings=False)
        if mask_account_identifiers(serialized) != serialized:
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("portfolio observation result is unavailable") from None


def _require_result_coherence(
    result: ReconciliationResult,
    scope: RuntimeScopeView,
) -> None:
    if not result.symbols:
        raise ValueError
    symbols = tuple(symbol.symbol for symbol in result.symbols)
    if len(set(symbols)) != len(symbols) or tuple(sorted(symbols)) != symbols:
        raise ValueError
    for symbol in result.symbols:
        if (
            not symbol.symbol
            or len(symbol.symbol) > _MAX_SYMBOL_CHARACTERS
            or symbol.symbol != symbol.symbol.strip()
            or not symbol.symbol.isascii()
            or mask_account_identifiers(symbol.symbol) != symbol.symbol
        ):
            raise ValueError
        if symbol.manual_review_required is (symbol.status is ReconciliationStatus.RECONCILED):
            raise ValueError
        _require_safe_reasons(symbol.reasons)

    statuses = {symbol.status for symbol in result.symbols}
    if result.status is ReconciliationStatus.PENDING:
        if statuses != {ReconciliationStatus.PENDING}:
            raise ValueError
    elif result.status is ReconciliationStatus.MANUAL_REVIEW:
        if ReconciliationStatus.PENDING in statuses or not any(
            symbol.status is ReconciliationStatus.MANUAL_REVIEW for symbol in result.symbols
        ):
            raise ValueError
    elif statuses != {ReconciliationStatus.RECONCILED}:
        raise ValueError
    if result.status is not ReconciliationStatus.RECONCILED and not result.reasons:
        raise ValueError
    _require_safe_reasons(result.reasons)

    snapshot = result.snapshot
    if snapshot is None:
        if result.status is not ReconciliationStatus.PENDING:
            raise ValueError
        return
    if (
        snapshot.environment is not scope.environment
        or snapshot.account.masked_account_id != scope.masked_account_id
        or not _is_canonical_masked_account(snapshot.account.masked_account_id)
        or not _is_canonical_display_code(
            snapshot.account.currency,
            max_characters=_MAX_CURRENCY_CHARACTERS,
        )
        or not isfinite(snapshot.window_seconds)
        or not isfinite(snapshot.server_window_seconds)
    ):
        raise ValueError
    for position in snapshot.positions:
        if not _has_renderable_contract(position.contract):
            raise ValueError
    for order in snapshot.open_orders:
        if not _has_renderable_contract(order.contract):
            raise ValueError
    try:
        captured_at = snapshot.captured_at.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError from None
    if captured_at < _MIN_DISPLAY_TIME:
        raise ValueError


def _require_safe_reasons(reasons: tuple[str, ...]) -> None:
    if len(set(reasons)) != len(reasons):
        raise ValueError
    for reason in reasons:
        if (
            not reason
            or len(reason) > _MAX_REASON_CHARACTERS
            or reason != reason.strip()
            or not reason.isprintable()
            or mask_account_identifiers(reason) != reason
        ):
            raise ValueError


def _is_canonical_masked_account(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    if set(value) == {"•"}:
        return len(value) <= 8
    return (
        len(value) >= 9
        and value[:2].isascii()
        and value[:2].isalnum()
        and value[-4:].isascii()
        and value[-4:].isalnum()
        and set(value[2:-4]) == {"•"}
    )


def _has_renderable_contract(value: UnderlyingContract | OptionContract) -> bool:
    return _is_canonical_display_code(
        value.symbol,
        max_characters=_MAX_SYMBOL_CHARACTERS,
    ) and _is_canonical_display_code(
        value.currency,
        max_characters=_MAX_CURRENCY_CHARACTERS,
    )


def _is_canonical_display_code(value: str, *, max_characters: int) -> bool:
    return (
        0 < len(value) <= max_characters
        and value == value.strip().upper()
        and value.isascii()
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def _scope_digest(scope: RuntimeScopeView) -> str:
    try:
        payload = json.dumps(
            RuntimeScopeView.model_dump(scope, mode="json", warnings=False),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("runtime scope could not be bound to portfolio state") from None
    return hashlib.sha256(payload).hexdigest()


def _has_exact_reconciliation_storage(value: ReconciliationResult) -> bool:
    if not _has_exact_model_storage(value, ReconciliationResult.model_fields):
        return False
    if any(
        type(symbol) is not SymbolReconciliation
        or not _has_exact_model_storage(symbol, SymbolReconciliation.model_fields)
        for symbol in value.symbols
    ):
        return False
    snapshot = value.snapshot
    if snapshot is None:
        return True
    if type(snapshot) is not ReconciliationSnapshotView or not _has_exact_model_storage(
        snapshot, ReconciliationSnapshotView.model_fields
    ):
        return False
    if type(snapshot.account) is not ReconciliationAccountView or not _has_exact_model_storage(
        snapshot.account, ReconciliationAccountView.model_fields
    ):
        return False
    for position in snapshot.positions:
        if type(position) is not ReconciliationPositionView or not _has_exact_model_storage(
            position, ReconciliationPositionView.model_fields
        ):
            return False
        if not _has_exact_instrument_storage(position.contract):
            return False
    for order in snapshot.open_orders:
        if type(order) is not ReconciliationOrderView or not _has_exact_model_storage(
            order, ReconciliationOrderView.model_fields
        ):
            return False
        if not _has_exact_instrument_storage(order.contract):
            return False
    return True


def _has_exact_instrument_storage(value: object) -> bool:
    for model_type in (UnderlyingContract, OptionContract):
        if type(value) is model_type:
            return _has_exact_model_storage(value, model_type.model_fields)
    return False


def _has_exact_model_storage(value: object, fields: Collection[str]) -> bool:
    try:
        return set(vars(value)) == set(fields) and not getattr(value, "__pydantic_extra__", None)
    except (AttributeError, TypeError):
        return False


def _same_typed_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if not isinstance(right, dict) or left.keys() != right.keys():
            return False
        return all(_same_typed_value(left[key], right[key]) for key in left)
    if isinstance(left, tuple):
        return (
            isinstance(right, tuple)
            and len(left) == len(right)
            and all(
                _same_typed_value(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, list):
        return (
            isinstance(right, list)
            and len(left) == len(right)
            and all(
                _same_typed_value(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    return left == right

"""Sanitized process-lifetime scope bound from existing startup observations."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    ConnectionState,
    DataQuality,
    DemoProfile,
    DisplayEnvironment,
)
from chronos.domain.models import AccountSummary, ChronosModel, ConnectionStatus
from chronos.utils.logging import mask_account_id

_MAX_ACCOUNT_ID_CHARACTERS = 64
_MIN_RUNTIME_OBSERVATION = datetime(1970, 1, 1, tzinfo=UTC)


class ScopeBinder(Protocol):
    """Persistence boundary needed to bind validated startup identity."""

    def bind_scope(
        self,
        *,
        broker_mode: str,
        environment: str,
        account_id: str,
    ) -> None: ...


class _RuntimeScopeFacts(ChronosModel):
    """Validated, sanitized startup facts that make no persistence claim."""

    broker_mode: BrokerMode
    demo_profile: DemoProfile | None
    environment: DisplayEnvironment
    data_quality: DataQuality
    startup_connection_state: Literal[ConnectionState.CONNECTED] = ConnectionState.CONNECTED
    masked_account_id: Annotated[str, Field(min_length=1, max_length=64)]
    account_observed_at: AwareDatetime

    @field_validator("masked_account_id")
    @classmethod
    def require_masked_account_id(cls, value: str) -> str:
        if set(value) == {"•"}:
            if len(value) > 8:
                raise ValueError("fully masked runtime account identifier is too long")
            return value
        if (
            len(value) < 9
            or not value[:2].isascii()
            or not value[:2].isalnum()
            or not value[-4:].isascii()
            or not value[-4:].isalnum()
            or set(value[2:-4]) != {"•"}
        ):
            raise ValueError("runtime scope requires a masked account identifier")
        return value

    @field_validator("account_observed_at")
    @classmethod
    def normalize_observation_time(cls, value: datetime) -> datetime:
        try:
            normalized = value.astimezone(UTC)
        except (OverflowError, ValueError):
            raise ValueError("runtime observation time is outside the supported range") from None
        if normalized < _MIN_RUNTIME_OBSERVATION:
            raise ValueError("runtime observation time is outside the supported range")
        return normalized

    @model_validator(mode="after")
    def require_source_coherence(self) -> _RuntimeScopeFacts:
        if self.broker_mode is BrokerMode.DEMO:
            if (
                self.demo_profile is None
                or self.environment is not DisplayEnvironment.DEMO
                or self.data_quality is not DataQuality.DEMO
            ):
                raise ValueError("DEMO runtime scope is internally inconsistent")
        elif (
            self.demo_profile is not None
            or self.environment is DisplayEnvironment.DEMO
            or self.data_quality is not DataQuality.UNKNOWN
        ):
            raise ValueError("IBKR runtime scope is internally inconsistent")
        return self


class RuntimeScopeView(_RuntimeScopeFacts):
    """Historical startup identity context with no raw account or financial state."""

    historical_startup_observation: Literal[True] = True
    account_scope_bound: Literal[True] = True
    runtime_view_persisted: Literal[False] = False
    opening_actions_locked: Literal[True] = True
    submission_locked: Literal[True] = True
    live_trading_locked: Literal[True] = True


def build_bound_runtime_scope(
    scope_binder: ScopeBinder,
    settings: Settings,
    account: AccountSummary,
    status: ConnectionStatus,
) -> RuntimeScopeView:
    """Validate safe display facts, bind persistence scope, then publish bound flags."""

    settings = _validated_settings(settings)
    account = _validated_account(account)
    status = _validated_status(status)

    account_id = account.account_id
    if (
        not account_id
        or len(account_id) > _MAX_ACCOUNT_ID_CHARACTERS
        or account_id != account_id.strip()
        or not account_id.isascii()
        or not account_id.isalnum()
        or status.account_id != account_id
    ):
        raise ValueError("startup observations do not identify one canonical account")
    if not status.connected or status.state is not ConnectionState.CONNECTED:
        raise ValueError("startup connection is not healthy enough to bind runtime scope")
    if status.last_successful_sync is None or status.last_successful_sync != account.as_of:
        raise ValueError("startup observations do not share one capture time")

    expected_environment = (
        DisplayEnvironment.DEMO
        if settings.broker_mode is BrokerMode.DEMO
        else DisplayEnvironment(settings.ib_environment.value.upper())
    )
    if status.environment is not expected_environment:
        raise ValueError("startup environment does not match validated configuration")
    if settings.broker_mode is BrokerMode.DEMO:
        if status.data_quality is not DataQuality.DEMO:
            raise ValueError("DEMO startup scope requires deterministic DEMO data quality")
    elif status.data_quality is not DataQuality.UNKNOWN:
        raise ValueError("IBKR startup scope has no startup market-data observation")

    try:
        facts = _RuntimeScopeFacts(
            broker_mode=settings.broker_mode,
            demo_profile=(
                settings.demo_profile if settings.broker_mode is BrokerMode.DEMO else None
            ),
            environment=status.environment,
            data_quality=status.data_quality,
            masked_account_id=_mask_runtime_account_id(account_id),
            account_observed_at=account.as_of.astimezone(UTC),
        )
    except (OverflowError, TypeError, ValueError):
        raise ValueError("startup observations could not produce a safe runtime scope") from None

    scope_binder.bind_scope(
        broker_mode=settings.broker_mode.value,
        environment=status.environment.value,
        account_id=account_id,
    )
    try:
        return RuntimeScopeView.model_validate(
            _RuntimeScopeFacts.model_dump(facts, mode="python", warnings=False),
            strict=True,
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("validated startup scope could not be materialized") from None


def validate_runtime_scope(value: object) -> RuntimeScopeView:
    """Revalidate an immutable view before rendering any interactive workspace."""

    if type(value) is not RuntimeScopeView or not _has_exact_model_storage(
        value, RuntimeScopeView.model_fields
    ):
        raise ValueError("runtime scope view is unavailable")
    try:
        dumped = RuntimeScopeView.model_dump(value, mode="python", warnings=False)
        validated = RuntimeScopeView.model_validate(dumped, strict=True)
        canonical = RuntimeScopeView.model_dump(validated, mode="python", warnings=False)
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("runtime scope view is unavailable") from None


def _mask_runtime_account_id(account_id: str) -> str:
    if len(account_id) <= 8:
        return "•" * len(account_id)
    return mask_account_id(account_id)


def _validated_settings(value: object) -> Settings:
    if type(value) is not Settings or not _has_exact_model_storage(value, Settings.model_fields):
        raise ValueError("runtime scope requires exact validated settings")
    try:
        dumped = Settings.model_dump(value, mode="python", warnings=False)
        validated = Settings.model_validate(dumped, strict=True)
        canonical = Settings.model_dump(validated, mode="python", warnings=False)
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("runtime scope settings are invalid") from None


def _validated_account(value: object) -> AccountSummary:
    if type(value) is not AccountSummary or not _has_exact_model_storage(
        value, AccountSummary.model_fields
    ):
        raise ValueError("runtime scope requires an exact account observation")
    try:
        dumped = AccountSummary.model_dump(value, mode="python", warnings=False)
        validated = AccountSummary.model_validate(dumped, strict=True)
        canonical = AccountSummary.model_dump(validated, mode="python", warnings=False)
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("runtime scope account observation is invalid") from None


def _validated_status(value: object) -> ConnectionStatus:
    if type(value) is not ConnectionStatus or not _has_exact_model_storage(
        value, ConnectionStatus.model_fields
    ):
        raise ValueError("runtime scope requires an exact connection observation")
    try:
        dumped = ConnectionStatus.model_dump(value, mode="python", warnings=False)
        validated = ConnectionStatus.model_validate(dumped, strict=True)
        canonical = ConnectionStatus.model_dump(validated, mode="python", warnings=False)
        if not _same_typed_value(canonical, dumped):
            raise ValueError
        return validated
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("runtime scope connection observation is invalid") from None


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

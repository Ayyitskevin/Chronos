import traceback
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    ConnectionState,
    DataQuality,
    DemoProfile,
    DisplayEnvironment,
    IBEnvironment,
)
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.ui.runtime_scope import (
    RuntimeScopeView,
    build_bound_runtime_scope,
    validate_runtime_scope,
)

NOW = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
RAW_ACCOUNT_ID = "DU1234567"
VIEW_FIELDS = {
    "broker_mode",
    "demo_profile",
    "environment",
    "data_quality",
    "startup_connection_state",
    "masked_account_id",
    "account_observed_at",
    "historical_startup_observation",
    "account_scope_bound",
    "runtime_view_persisted",
    "opening_actions_locked",
    "submission_locked",
    "live_trading_locked",
}


class _RecordingScopeBinder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def bind_scope(
        self,
        *,
        broker_mode: str,
        environment: str,
        account_id: str,
    ) -> None:
        self.calls.append((broker_mode, environment, account_id))


def build_runtime_scope(
    settings: Settings,
    account: AccountSummary,
    status: ConnectionStatus,
) -> RuntimeScopeView:
    return build_bound_runtime_scope(_RecordingScopeBinder(), settings, account, status)


def _settings(
    *,
    broker_mode: BrokerMode = BrokerMode.DEMO,
    demo_profile: DemoProfile = DemoProfile.SAFETY_CASES,
    ib_environment: IBEnvironment = IBEnvironment.PAPER,
) -> Settings:
    return Settings.model_validate(
        {
            "broker_mode": broker_mode,
            "demo_profile": demo_profile,
            "ib_environment": ib_environment,
            "ib_host": "secret-host.invalid",
            "ib_port": 4002,
            "ib_client_id": 912,
            "ib_account_id": "CONFIGURED987654",
            "database_url": "sqlite:////secret/database/path.db",
            "log_file": "/secret/log/path.log",
        }
    )


def _account(account_id: str = RAW_ACCOUNT_ID, *, as_of: datetime = NOW) -> AccountSummary:
    return AccountSummary(
        account_id=account_id,
        net_liquidation=Decimal("918273.45"),
        total_cash=Decimal("192837.54"),
        buying_power=Decimal("564738.21"),
        currency="USD",
        as_of=as_of,
    )


def _status(
    account_id: str | None = RAW_ACCOUNT_ID,
    *,
    environment: DisplayEnvironment = DisplayEnvironment.DEMO,
    data_quality: DataQuality = DataQuality.DEMO,
    connected: bool = True,
    state: ConnectionState = ConnectionState.CONNECTED,
    last_successful_sync: datetime | None = NOW,
    message: str = "TOPSECRET_STATUS_MESSAGE",
) -> ConnectionStatus:
    return ConnectionStatus(
        state=state,
        environment=environment,
        connected=connected,
        account_id=account_id,
        data_quality=data_quality,
        last_successful_sync=last_successful_sync,
        message=message,
    )


@pytest.mark.parametrize("profile", tuple(DemoProfile))
def test_demo_scope_is_immutable_sanitized_and_historical(profile: DemoProfile) -> None:
    scope = build_runtime_scope(_settings(demo_profile=profile), _account(), _status())

    assert scope.broker_mode is BrokerMode.DEMO
    assert scope.demo_profile is profile
    assert scope.environment is DisplayEnvironment.DEMO
    assert scope.data_quality is DataQuality.DEMO
    assert scope.startup_connection_state is ConnectionState.CONNECTED
    assert scope.masked_account_id == "DU•••4567"
    assert scope.account_observed_at == NOW
    assert scope.account_observed_at.tzinfo is UTC
    assert scope.historical_startup_observation is True
    assert scope.account_scope_bound is True
    assert scope.runtime_view_persisted is False
    assert scope.opening_actions_locked is True
    assert scope.submission_locked is True
    assert scope.live_trading_locked is True
    assert validate_runtime_scope(scope) == scope


@pytest.mark.parametrize(
    ("ib_environment", "display_environment"),
    [
        (IBEnvironment.PAPER, DisplayEnvironment.PAPER),
        (IBEnvironment.LIVE, DisplayEnvironment.LIVE),
    ],
)
def test_ibkr_scope_records_only_unknown_startup_market_quality(
    ib_environment: IBEnvironment,
    display_environment: DisplayEnvironment,
) -> None:
    scope = build_runtime_scope(
        _settings(broker_mode=BrokerMode.IBKR, ib_environment=ib_environment),
        _account(),
        _status(environment=display_environment, data_quality=DataQuality.UNKNOWN),
    )

    assert scope.broker_mode is BrokerMode.IBKR
    assert scope.demo_profile is None
    assert scope.environment is display_environment
    assert scope.data_quality is DataQuality.UNKNOWN
    assert scope.submission_locked is True
    assert scope.live_trading_locked is True


def test_scope_schema_and_serialization_are_allowlisted() -> None:
    settings = _settings()
    account = _account()
    status = _status()
    scope = build_runtime_scope(settings, account, status)
    serialized = scope.model_dump_json()
    rendered = repr(scope)

    assert set(RuntimeScopeView.model_fields) == VIEW_FIELDS
    assert set(scope.model_dump()) == VIEW_FIELDS
    for forbidden in (
        RAW_ACCOUNT_ID,
        "918273.45",
        "192837.54",
        "564738.21",
        status.message,
        settings.ib_host,
        str(settings.ib_port),
        str(settings.ib_client_id),
        settings.ib_account_id,
        settings.database_url,
        str(settings.log_file),
    ):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_scope_binds_only_after_every_display_fact_passes_preflight() -> None:
    binder = _RecordingScopeBinder()

    scope = build_bound_runtime_scope(binder, _settings(), _account(), _status())

    assert scope.account_scope_bound is True
    assert binder.calls == [("demo", "DEMO", RAW_ACCOUNT_ID)]


def test_invalid_display_facts_never_reach_persistent_scope_binding() -> None:
    binder = _RecordingScopeBinder()

    with pytest.raises(ValueError, match="environment does not match"):
        build_bound_runtime_scope(
            binder,
            _settings(broker_mode=BrokerMode.IBKR, ib_environment=IBEnvironment.LIVE),
            _account(),
            _status(environment=DisplayEnvironment.PAPER, data_quality=DataQuality.UNKNOWN),
        )

    assert binder.calls == []


@pytest.mark.parametrize("length", [1, 4, 5, 6, 8, 9, 64])
def test_account_masking_never_reveals_short_ids_and_preserves_long_length(length: int) -> None:
    account_id = "A" * length
    scope = build_runtime_scope(
        _settings(),
        _account(account_id),
        _status(account_id),
    )

    assert len(scope.masked_account_id) == length
    assert account_id not in scope.model_dump_json()
    if length <= 8:
        assert scope.masked_account_id == "•" * length
    else:
        assert scope.masked_account_id == f"AA{'•' * (length - 6)}AAAA"


@pytest.mark.parametrize(
    ("account_id", "status_account_id"),
    [
        ("", ""),
        (" ACCOUNT1", " ACCOUNT1"),
        ("ACCOUNT1 ", "ACCOUNT1 "),
        ("ACC-OUNT1", "ACC-OUNT1"),
        ("ÀCCOUNT1", "ÀCCOUNT1"),
        ("A" * 65, "A" * 65),
        (RAW_ACCOUNT_ID, "DU7654321"),
    ],
)
def test_scope_rejects_noncanonical_or_mismatched_accounts(
    account_id: str,
    status_account_id: str,
) -> None:
    with pytest.raises(ValueError, match="canonical account"):
        build_runtime_scope(
            _settings(),
            _account(account_id),
            _status(status_account_id),
        )


@pytest.mark.parametrize(
    "status",
    [
        _status(connected=False, state=ConnectionState.DISCONNECTED),
        _status(connected=True, state=ConnectionState.DEGRADED),
    ],
)
def test_scope_rejects_unhealthy_startup_status(status: ConnectionStatus) -> None:
    with pytest.raises(ValueError, match="not healthy"):
        build_runtime_scope(_settings(), _account(), status)


@pytest.mark.parametrize(
    "status",
    [
        _status(last_successful_sync=None),
        _status(last_successful_sync=NOW - timedelta(microseconds=1)),
        _status(last_successful_sync=NOW + timedelta(microseconds=1)),
    ],
)
def test_scope_requires_one_exact_startup_capture_time(status: ConnectionStatus) -> None:
    with pytest.raises(ValueError, match="one capture time"):
        build_runtime_scope(_settings(), _account(), status)


@pytest.mark.parametrize(
    ("settings", "status"),
    [
        (
            _settings(),
            _status(environment=DisplayEnvironment.PAPER, data_quality=DataQuality.DEMO),
        ),
        (
            _settings(),
            _status(environment=DisplayEnvironment.DEMO, data_quality=DataQuality.UNKNOWN),
        ),
        (
            _settings(broker_mode=BrokerMode.IBKR),
            _status(environment=DisplayEnvironment.DEMO, data_quality=DataQuality.UNKNOWN),
        ),
        (
            _settings(broker_mode=BrokerMode.IBKR),
            _status(environment=DisplayEnvironment.PAPER, data_quality=DataQuality.LIVE),
        ),
        (
            _settings(broker_mode=BrokerMode.IBKR, ib_environment=IBEnvironment.LIVE),
            _status(environment=DisplayEnvironment.PAPER, data_quality=DataQuality.UNKNOWN),
        ),
    ],
)
def test_scope_rejects_mode_environment_or_quality_mismatch(
    settings: Settings,
    status: ConnectionStatus,
) -> None:
    with pytest.raises(ValueError):
        build_runtime_scope(settings, _account(), status)


def test_scope_rejects_subclasses_for_every_source_model() -> None:
    class SettingsSubclass(Settings):
        pass

    class AccountSubclass(AccountSummary):
        pass

    class StatusSubclass(ConnectionStatus):
        pass

    settings = _settings()
    account = _account()
    status = _status()
    settings_subclass = SettingsSubclass.model_validate(settings.model_dump())
    account_subclass = AccountSubclass.model_validate(account.model_dump())
    status_subclass = StatusSubclass.model_validate(status.model_dump())

    with pytest.raises(ValueError, match="exact validated settings"):
        build_runtime_scope(settings_subclass, account, status)
    with pytest.raises(ValueError, match="exact account observation"):
        build_runtime_scope(settings, account_subclass, status)
    with pytest.raises(ValueError, match="exact connection observation"):
        build_runtime_scope(settings, account, status_subclass)


@pytest.mark.parametrize(
    ("source", "corruption"),
    [
        ("settings", {"broker_mode": "demo"}),
        ("settings", {"ib_port": "SECRET_PORT_VALUE"}),
        ("account", {"total_cash": "TOPSECRET_FINANCIAL_VALUE"}),
        ("account", {"as_of": "2026-07-15T15:30:00Z"}),
        ("account", {"as_of": datetime(2026, 7, 15, 15, 30)}),  # noqa: DTZ001
        ("status", {"connected": 1}),
        ("status", {"state": "CONNECTED"}),
        ("status", {"last_successful_sync": "2026-07-15T15:30:00Z"}),
    ],
)
def test_scope_rejects_coercible_model_copy_corruption(
    source: str,
    corruption: dict[str, object],
) -> None:
    settings = _settings()
    account = _account()
    status = _status()
    if source == "settings":
        settings = settings.model_copy(update=corruption)
    elif source == "account":
        account = account.model_copy(update=corruption)
    else:
        status = status.model_copy(update=corruption)

    with pytest.raises(ValueError):
        build_runtime_scope(settings, account, status)


def test_scope_rejects_missing_or_extra_internal_model_storage() -> None:
    missing_account = AccountSummary.model_construct(account_id=RAW_ACCOUNT_ID)
    extra_status = _status().model_copy(update={"unexpected": "TOPSECRET_EXTRA"})

    with pytest.raises(ValueError, match="exact account observation"):
        build_runtime_scope(_settings(), missing_account, _status())
    with pytest.raises(ValueError, match="exact connection observation"):
        build_runtime_scope(_settings(), _account(), extra_status)


@pytest.mark.parametrize(
    ("source", "secret"),
    [
        ("settings", "TOPSECRET_SETTINGS_VALUE"),
        ("account", "TOPSECRET_FINANCIAL_VALUE"),
        ("status", "TOPSECRET_STATUS_VALUE"),
    ],
)
def test_canonicalization_errors_suppress_source_values(source: str, secret: str) -> None:
    settings = _settings()
    account = _account()
    status = _status(message=secret)
    if source == "settings":
        settings = settings.model_copy(update={"ib_port": secret})
    elif source == "account":
        account = account.model_copy(update={"total_cash": secret})
    else:
        status = status.model_copy(update={"connected": 1})

    with pytest.raises(ValueError) as captured:
        build_runtime_scope(settings, account, status)

    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in str(captured.value)
    assert secret not in formatted
    assert captured.value.__cause__ is None


def test_canonicalization_error_suppresses_raw_account_and_status_message() -> None:
    raw_account = "RAWTOKEN998877"
    secret_message = "STATUSSECRET887766"
    status = _status(raw_account, message=secret_message).model_copy(update={"connected": 1})

    with pytest.raises(ValueError) as captured:
        build_runtime_scope(_settings(), _account(raw_account), status)

    formatted = "".join(traceback.format_exception(captured.value))
    assert raw_account not in formatted
    assert secret_message not in formatted


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=14))),
    ],
)
def test_utc_normalization_overflow_is_sanitized(observed_at: datetime) -> None:
    with pytest.raises(
        ValueError,
        match="could not produce a safe runtime scope",
    ) as captured:
        build_runtime_scope(
            _settings(),
            _account(as_of=observed_at),
            _status(last_successful_sync=observed_at),
        )

    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "corruption",
    [
        {"masked_account_id": RAW_ACCOUNT_ID},
        {"submission_locked": False},
        {"live_trading_locked": False},
        {"broker_mode": "demo"},
        {"account_observed_at": "2026-07-15T15:30:00Z"},
        {"unexpected": "TOPSECRET_EXTRA"},
    ],
)
def test_render_boundary_rejects_forged_runtime_views(corruption: dict[str, object]) -> None:
    scope = build_runtime_scope(_settings(), _account(), _status())
    forged = scope.model_copy(update=corruption)

    with pytest.raises(ValueError, match="view is unavailable") as captured:
        validate_runtime_scope(forged)

    assert RAW_ACCOUNT_ID not in str(captured.value)
    assert "TOPSECRET_EXTRA" not in str(captured.value)


def test_runtime_view_constructor_rejects_raw_account_and_extra_fields() -> None:
    payload = build_runtime_scope(_settings(), _account(), _status()).model_dump()

    with pytest.raises(ValidationError):
        RuntimeScopeView.model_validate({**payload, "masked_account_id": RAW_ACCOUNT_ID})
    with pytest.raises(ValidationError):
        RuntimeScopeView.model_validate({**payload, "unexpected": "value"})


@pytest.mark.parametrize(
    "quality",
    tuple(quality for quality in DataQuality if quality is not DataQuality.UNKNOWN),
)
def test_runtime_view_constructor_rejects_ibkr_nonstartup_quality(
    quality: DataQuality,
) -> None:
    paper_scope = build_runtime_scope(
        _settings(broker_mode=BrokerMode.IBKR),
        _account(),
        _status(environment=DisplayEnvironment.PAPER, data_quality=DataQuality.UNKNOWN),
    )

    with pytest.raises(ValidationError, match="internally inconsistent"):
        RuntimeScopeView.model_validate({**paper_scope.model_dump(), "data_quality": quality})


def test_runtime_view_rejects_time_that_market_display_cannot_convert() -> None:
    with pytest.raises(ValueError, match="could not produce a safe runtime scope"):
        build_runtime_scope(
            _settings(),
            _account(as_of=datetime.min.replace(tzinfo=UTC)),
            _status(last_successful_sync=datetime.min.replace(tzinfo=UTC)),
        )

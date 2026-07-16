from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
)
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode, DemoProfile, IBEnvironment


def test_safe_demo_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.broker_mode is BrokerMode.DEMO
    assert settings.demo_profile is DemoProfile.SAFETY_CASES
    assert settings.ib_environment is IBEnvironment.PAPER
    assert settings.allow_order_transmit is False
    assert settings.allow_live_trading is False
    assert settings.transmission_possible is False
    assert settings.target_abs_delta == Decimal("0.30")
    assert settings.max_strikes_per_expiration == 12
    assert settings.assignment_near_zero_extrinsic == Decimal("0.05")
    assert settings.market_timezone == "America/New_York"


def test_empty_account_demo_profile_parses_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")

    settings = Settings(_env_file=None)

    assert settings.demo_profile is DemoProfile.EMPTY_ACCOUNT


def test_unknown_demo_profile_is_rejected() -> None:
    with pytest.raises(ValidationError, match="demo_profile"):
        Settings(_env_file=None, demo_profile="unknown")


@pytest.mark.parametrize(
    "values",
    [
        {"max_expirations": MAX_CANDIDATE_EXPIRATIONS + 1},
        {"max_strikes_per_expiration": MAX_CANDIDATE_STRIKES_PER_EXPIRATION + 1},
    ],
)
def test_candidate_request_bounds_have_hard_configuration_caps(
    values: dict[str, int],
) -> None:
    with pytest.raises(ValidationError, match="less than or equal"):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("max_expirations", "max_strikes_per_expiration"),
    [(8, 10), (4, 20)],
)
def test_candidate_request_product_accepts_exact_hard_boundary(
    max_expirations: int,
    max_strikes_per_expiration: int,
) -> None:
    settings = Settings(
        _env_file=None,
        max_expirations=max_expirations,
        max_strikes_per_expiration=max_strikes_per_expiration,
    )

    assert settings.max_expirations * settings.max_strikes_per_expiration == 80


def test_candidate_request_product_rejects_within_field_overage() -> None:
    with pytest.raises(ValidationError, match="must not exceed 80"):
        Settings(
            _env_file=None,
            max_expirations=5,
            max_strikes_per_expiration=17,
        )


def test_paper_transmission_requires_ibkr_mode() -> None:
    demo = Settings(_env_file=None, allow_order_transmit=True)
    paper = Settings(
        _env_file=None,
        broker_mode=BrokerMode.IBKR,
        ib_environment=IBEnvironment.PAPER,
        ib_account_id="DU1234567",
        allow_order_transmit=True,
    )

    assert demo.transmission_possible is False
    assert paper.transmission_possible is True


def test_paper_transmission_requires_configured_account() -> None:
    with pytest.raises(ValidationError, match="IB_ACCOUNT_ID"):
        Settings(
            _env_file=None,
            broker_mode=BrokerMode.IBKR,
            ib_environment=IBEnvironment.PAPER,
            allow_order_transmit=True,
        )


def test_live_transmission_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live order transmission is hard-disabled"):
        Settings(
            _env_file=None,
            broker_mode=BrokerMode.IBKR,
            ib_environment=IBEnvironment.LIVE,
            allow_order_transmit=True,
        )


def test_live_trading_flag_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError, match="Live trading is hard-disabled"):
        Settings(_env_file=None, allow_live_trading=True)


def test_live_trading_environment_flag_parses_false_and_rejects_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    assert Settings(_env_file=None).allow_live_trading is False

    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    with pytest.raises(ValidationError, match="Live trading is hard-disabled"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"min_abs_delta": "0.40", "target_abs_delta": "0.30"},
            "TARGET_ABS_DELTA",
        ),
        ({"target_dte": 50, "max_dte": 45}, "TARGET_DTE"),
        ({"symbol_allowlist": ""}, "SYMBOL_ALLOWLIST"),
        (
            {
                "assignment_near_zero_extrinsic": "0.20",
                "assignment_meaningful_extrinsic": "0.10",
            },
            "ASSIGNMENT_NEAR_ZERO_EXTRINSIC",
        ),
        (
            {"assignment_high_dte": 6, "assignment_elevated_dte": 5},
            "ASSIGNMENT_HIGH_DTE",
        ),
        ({"market_timezone": "Mars/Olympus_Mons"}, "MARKET_TIMEZONE"),
    ],
)
def test_inconsistent_resolver_settings_are_rejected(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **values)

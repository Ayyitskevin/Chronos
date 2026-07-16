from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode, IBEnvironment


def test_safe_demo_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.broker_mode is BrokerMode.DEMO
    assert settings.ib_environment is IBEnvironment.PAPER
    assert settings.allow_order_transmit is False
    assert settings.allow_live_trading is False
    assert settings.transmission_possible is False
    assert settings.target_abs_delta == Decimal("0.30")
    assert settings.max_strikes_per_expiration == 12
    assert settings.assignment_near_zero_extrinsic == Decimal("0.05")
    assert settings.market_timezone == "America/New_York"


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
    with pytest.raises(ValidationError):
        Settings(_env_file=None, allow_live_trading=True)


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

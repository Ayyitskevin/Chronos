from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
)
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerAdapter, BrokerMode, DemoProfile, IBEnvironment


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
    assert settings.enable_autonomy_option_selection is False
    assert settings.autonomy_option_resolver_promotion_file is None


@pytest.mark.parametrize(
    "field",
    (
        "target_abs_delta",
        "min_abs_delta",
        "max_abs_delta",
        "max_relative_spread",
        "delta_weight",
        "spread_weight",
        "dte_weight",
        "liquidity_weight",
    ),
)
def test_option_selection_settings_reject_unbounded_decimal_rendering(field: str) -> None:
    with pytest.raises(ValidationError, match="option-selection setting"):
        Settings(_env_file=None, **{field: Decimal("1E-1000000")})


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


def test_live_env_with_transmit_but_no_live_flag_is_ambiguous_and_refused() -> None:
    with pytest.raises(ValidationError, match="ambiguous without"):
        Settings(
            _env_file=None,
            broker_mode=BrokerMode.IBKR,
            ib_environment=IBEnvironment.LIVE,
            allow_order_transmit=True,
        )


def test_live_trading_flag_alone_fails_the_conjunction() -> None:
    # ADR-0009: one flag can never enable live; every unmet conjunct is named.
    with pytest.raises(ValidationError, match="full live conjunction"):
        Settings(_env_file=None, allow_live_trading=True)


def test_live_trading_environment_flag_parses_false_and_rejects_bare_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    assert Settings(_env_file=None).allow_live_trading is False

    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    with pytest.raises(ValidationError, match="full live conjunction"):
        Settings(_env_file=None)


def _live_conjunction_kwargs() -> dict[str, object]:
    """The full, valid ADR-0009 live conjunction (test fixture — fake account)."""

    return {
        "broker_mode": BrokerMode.IBKR,
        "broker_adapter": BrokerAdapter.OFFICIAL_IBKR,
        "ib_environment": IBEnvironment.LIVE,
        "ib_port": 7496,
        "allow_order_transmit": True,
        "allow_live_trading": True,
        "ib_account_id": "U7654321",
        "ib_account_allowlist": ("U7654321",),
    }


def test_full_live_conjunction_is_accepted_and_live_transmission_possible() -> None:
    settings = Settings(_env_file=None, **_live_conjunction_kwargs())
    assert settings.live_transmission_possible is True
    # Structural mutual exclusion: the paper path is impossible on this object.
    assert settings.transmission_possible is False


@pytest.mark.parametrize(
    ("override", "expected_problem"),
    [
        ({"broker_adapter": BrokerAdapter.IB_ASYNC}, "official_ibkr"),
        ({"allow_order_transmit": False}, "ALLOW_ORDER_TRANSMIT"),
        ({"ib_account_id": "DU1234567", "ib_account_allowlist": ("DU1234567",)}, "live account"),
        ({"ib_account_allowlist": ()}, "IB_ACCOUNT_ALLOWLIST"),
        ({"ib_account_allowlist": ("U9999999",)}, "must be on IB_ACCOUNT_ALLOWLIST"),
        ({"require_live_arming": False}, "REQUIRE_LIVE_ARMING"),
        ({"require_typed_confirmation": False}, "REQUIRE_TYPED_CONFIRMATION"),
    ],
)
def test_each_missing_conjunct_refuses_live(
    override: dict[str, object], expected_problem: str
) -> None:
    kwargs = _live_conjunction_kwargs() | override
    with pytest.raises(ValidationError, match=expected_problem):
        Settings(_env_file=None, **kwargs)


def test_live_with_paper_environment_refuses() -> None:
    kwargs = _live_conjunction_kwargs() | {"ib_environment": IBEnvironment.PAPER, "ib_port": 7497}
    with pytest.raises(ValidationError, match="IB_ENVIRONMENT must be 'live'"):
        Settings(_env_file=None, **kwargs)


def test_settings_are_frozen() -> None:
    # ADR-0009: branch selection immutability is a property of the type.
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.allow_live_trading = True  # type: ignore[misc]


def test_live_transmission_possible_rederives_after_model_copy_bypass() -> None:
    # model_copy(update=...) skips validators; the property must not be fooled.
    settings = Settings(_env_file=None)
    tampered = settings.model_copy(update={"allow_live_trading": True})
    assert tampered.live_transmission_possible is False


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


@pytest.mark.parametrize("weight", ("100.0001", "1e60"))
def test_resolver_weights_have_a_hard_numeric_ceiling(weight: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, spread_weight=weight)


def test_resolver_weight_ceiling_is_accepted_exactly() -> None:
    assert Settings(_env_file=None, spread_weight="100").spread_weight == Decimal("100")


class TestLiveWheelMilestone1Settings:
    """New configuration surface (docs/LIVE_WHEEL_GAME_PLAN.md M1).

    The keys exist with safe defaults; the live hard-raise is unchanged until
    the Milestone 6 gate stack replaces it.
    """

    def test_new_keys_have_safe_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.ib_account_allowlist == ()
        assert settings.enable_paper_trading is True
        assert settings.require_live_arming is True
        assert settings.live_arm_ttl_minutes == 15
        assert settings.require_typed_confirmation is True
        assert settings.order_confirmation_ttl_seconds == 20
        assert settings.crypto_allowlist == ()  # crypto family disabled
        assert settings.backend_host == "127.0.0.1"
        assert settings.backend_port == 8765

    def test_live_flag_requires_the_full_conjunction(self) -> None:
        # Live trading is the committed deliverable (owner direction). Since M7
        # the flag is honored — but ONLY under the complete ADR-0009 conjunction;
        # a bare flag still refuses with every unmet conjunct named.
        with pytest.raises(ValidationError, match="full live conjunction"):
            Settings(_env_file=None, allow_live_trading=True)

    def test_backend_host_must_be_loopback(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backend_host="0.0.0.0")
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backend_host="192.168.1.5")

    def test_crypto_allowlist_parses_and_validates(self) -> None:
        settings = Settings(_env_file=None, crypto_allowlist="btc, eth")
        assert settings.crypto_allowlist == ("BTC", "ETH")
        with pytest.raises(ValidationError):
            Settings(_env_file=None, crypto_allowlist="BTC,BTC")
        with pytest.raises(ValidationError):
            Settings(_env_file=None, crypto_allowlist="BTC/USD")

    def test_account_allowlist_validates(self) -> None:
        settings = Settings(_env_file=None, ib_account_allowlist="DU1234567")
        assert settings.ib_account_allowlist == ("DU1234567",)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ib_account_allowlist="DU1,DU1")

    def test_ttls_bounded(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, live_arm_ttl_minutes=0)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, order_confirmation_ttl_seconds=0)

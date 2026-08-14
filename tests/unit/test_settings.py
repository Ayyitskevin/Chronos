from decimal import Decimal
from pathlib import Path

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


class TestAllowlistsParseFromTheEnvironment:
    """The settings SOURCES, not the constructor — where the real defect lived.

    Every other allowlist test in this file builds ``Settings(...)`` directly,
    which bypasses ``pydantic-settings``' sources entirely and hands the raw
    string straight to the validator. That is why they all passed while
    **no documented allowlist value could actually be configured**:
    pydantic-settings JSON-decodes a complex field before validators run, so
    ``IB_ACCOUNT_ALLOWLIST=DU123``, the comma form, and the bare
    ``IB_ACCOUNT_ALLOWLIST=`` that ``.env.example`` shipped all raised
    ``SettingsError`` at import. Only a JSON array ever worked, and nothing said
    so — ``cp .env.example .env``, the README's own setup step, could not boot.

    A control exercised only on a path production never takes is the R-24..R-27
    shape applied to configuration. These tests go through the environment and
    through a real ``.env`` file, because those are the paths an owner uses.
    """

    def test_every_documented_allowlist_form_parses_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cases = {
            "": (),
            "   ": (),
            "DU123": ("DU123",),
            "DU123,DU456": ("DU123", "DU456"),
            "du123, du456": ("DU123", "DU456"),
            # The JSON spelling kept working: anyone who diagnosed the failure
            # will have switched to it, and silently reinterpreting a working
            # ["DU123"] as one garbage symbol would be worse than the bug.
            '["DU123"]': ("DU123",),
            '["DU123", "DU456"]': ("DU123", "DU456"),
            "[]": (),
        }
        for raw, expected in cases.items():
            monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", raw)
            assert Settings(_env_file=None).ib_account_allowlist == expected, raw

    def test_an_empty_allowlist_from_the_environment_denies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty is DENY, and the whole point of it being expressible at all.

        Before the fix an owner who blanked the allowlist to lock the system
        down got a hard crash instead of the deny posture. The crash was
        fail-closed, but it made the safe value unrepresentable — so this pins
        both halves: it parses, and what it parses to refuses.
        """

        monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", "")
        settings = Settings(_env_file=None)

        assert settings.ib_account_allowlist == ()
        assert settings.live_transmission_possible is False

        # And under the live conjunction it is not merely falsy — it is named as
        # the unmet conjunct, so an owner who blanked the allowlist to stand the
        # system down is told exactly that rather than left guessing.
        monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
        with pytest.raises(ValidationError, match="IB_ACCOUNT_ALLOWLIST must not be empty"):
            Settings(_env_file=None)

    def test_a_malformed_json_allowlist_refuses_rather_than_parsing_to_garbage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A value that opens with '[' is a JSON claim, and is held to it.

        Falling back to the comma split would turn ``["DU123"`` into the single
        symbol ``["DU123"``, which the alphanumeric validator would then reject
        anyway — but with a message about the wrong problem. Refusing here names
        the real one.
        """

        for raw in ('["DU123"', "[not json]", '{"account": "DU123"}'):
            monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", raw)
            with pytest.raises(ValidationError):
                Settings(_env_file=None)

    def test_the_existing_validators_still_fire_on_environment_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing weakened: the source changed, the rules did not."""

        monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", "DU123,DU123")
        with pytest.raises(ValidationError, match="must not contain duplicates"):
            Settings(_env_file=None)

        monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", "DU-123")
        with pytest.raises(ValidationError, match="must be alphanumeric"):
            Settings(_env_file=None)

    def test_all_three_allowlist_fields_parse_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect was in the shared annotation, so all three shared it."""

        monkeypatch.setenv("IB_ACCOUNT_ALLOWLIST", "DU123")
        monkeypatch.setenv("CRYPTO_ALLOWLIST", "btc,eth")
        monkeypatch.setenv("SYMBOL_ALLOWLIST", "spy, qqq")
        settings = Settings(_env_file=None)

        assert settings.ib_account_allowlist == ("DU123",)
        assert settings.crypto_allowlist == ("BTC", "ETH")
        assert settings.symbol_allowlist == ("SPY", "QQQ")

    def test_the_shipped_env_example_loads_and_is_demo_safe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`cp .env.example .env` must work, and must boot inert.

        The end-to-end proof of the reported defect: this is the README's
        documented setup step, it raised ``SettingsError`` on the bare
        ``IB_ACCOUNT_ALLOWLIST=`` line, and no test had ever loaded the shipped
        example. Asserting the resulting posture as well, because a setup file
        that loads but is live-capable would be a worse defect than one that
        crashes.
        """

        example = Path(__file__).resolve().parents[2] / ".env.example"
        target = tmp_path / ".env"
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        for name in ("IB_ACCOUNT_ALLOWLIST", "CRYPTO_ALLOWLIST", "SYMBOL_ALLOWLIST"):
            monkeypatch.delenv(name, raising=False)

        settings = Settings(_env_file=target)

        assert settings.ib_account_allowlist == ()
        assert settings.crypto_allowlist == ()
        assert settings.allow_order_transmit is False
        assert settings.allow_live_trading is False
        assert settings.live_transmission_possible is False

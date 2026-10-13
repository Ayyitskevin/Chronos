"""B1 U-c: for ``BROKER_MODE=ibkr``, the connection shape is refused when Settings LOADS.

Before U-c each of these surfaced only when the backend built the official adapter (after the
database had opened) or at the first grant: the port of the other environment, IB's master
client 0, a blank or wrongly shaped account id. Every refusal names the key and the rule and
never echoes the value; DEMO mode is untouched; no ALLOW_* default changes.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from chronos.broker import official_ibkr
from chronos.cli import b1_preflight
from chronos.config.settings import LIVE_API_PORTS, PAPER_API_PORTS, Settings
from chronos.domain.enums import BrokerMode, IBEnvironment

PAPER_ID = "DU7654321"
LIVE_ID = "U7654321"


def _ibkr(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "broker_mode": BrokerMode.IBKR,
        "ib_environment": IBEnvironment.PAPER,
        "ib_port": 7497,
        "ib_client_id": 17,
        "ib_account_id": PAPER_ID,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("overrides", "message", "value"),
    [
        ({"ib_port": 7496}, "IB_ENVIRONMENT=paper requires a paper port", "7496"),
        ({"ib_port": 4001}, "IB_ENVIRONMENT=paper requires a paper port", "4001"),
        ({"ib_port": 8888}, "IB_ENVIRONMENT=paper requires a paper port", "8888"),
        (
            {"ib_environment": IBEnvironment.LIVE, "ib_port": 7497, "ib_account_id": LIVE_ID},
            "IB_ENVIRONMENT=live requires a live port",
            "7497",
        ),
        ({"ib_client_id": 0}, "IB_CLIENT_ID=0 is IB's master client", None),
        ({"ib_account_id": ""}, "IB_ACCOUNT_ID is required for BROKER_MODE=ibkr", None),
        ({"ib_account_id": "   "}, "IB_ACCOUNT_ID is required for BROKER_MODE=ibkr", None),
        ({"ib_account_id": "U1234567"}, "is not a paper account id", "U1234567"),
        ({"ib_account_id": "DU12"}, "is not a paper account id", "DU12"),
        ({"ib_account_id": "CONFIGURED987654"}, "is not a paper account id", "CONFIGURED987654"),
        (
            {"ib_environment": IBEnvironment.LIVE, "ib_port": 7496, "ib_account_id": PAPER_ID},
            "is not a live account id",
            PAPER_ID,
        ),
        (
            {"ib_environment": IBEnvironment.LIVE, "ib_port": 7496, "ib_account_id": "U12"},
            "is not a live account id",
            "U12",
        ),
    ],
)
def test_each_ibkr_shape_rule_refuses_at_load_without_echoing_the_value(
    overrides: dict[str, Any], message: str, value: str | None
) -> None:
    with pytest.raises(ValidationError) as raised:
        _ibkr(**overrides)
    text = str(raised.value.errors()[0]["msg"])
    assert message in text
    assert "refused at load" in text  # the operator learns WHEN, not only what
    if value is not None:
        assert value not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"ib_port": 4002},
        {"ib_account_id": "DF7654321"},
        {"ib_client_id": 1},
        {"ib_environment": IBEnvironment.LIVE, "ib_port": 7496, "ib_account_id": LIVE_ID},
        {"ib_environment": IBEnvironment.LIVE, "ib_port": 4001, "ib_account_id": LIVE_ID},
    ],
)
def test_every_valid_ibkr_shape_still_loads(overrides: dict[str, Any]) -> None:
    settings = _ibkr(**overrides)
    assert settings.broker_mode is BrokerMode.IBKR


def test_demo_mode_is_untouched_by_the_ibkr_rules() -> None:
    """The defaults (DEMO) and DEMO with values the ibkr rules would refuse both still load."""

    assert Settings(_env_file=None).broker_mode is BrokerMode.DEMO  # type: ignore[call-arg]
    odd = Settings(  # type: ignore[call-arg]
        _env_file=None, broker_mode=BrokerMode.DEMO, ib_port=7496, ib_client_id=0, ib_account_id=""
    )
    assert odd.broker_mode is BrokerMode.DEMO


def test_no_allow_flag_default_changes() -> None:
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    loaded = _ibkr()
    for settings in (defaults, loaded):
        assert settings.allow_order_transmit is False
        assert settings.allow_live_trading is False
        assert settings.transmission_possible is False
        assert settings.live_transmission_possible is False


def test_the_more_specific_existing_refusals_keep_their_messages() -> None:
    """Ordering: validate_safety_and_ranges runs first, so paper transmission without an account
    still names the transmission rule, not the U-c shape rule."""

    with pytest.raises(ValidationError, match="required before paper order transmission"):
        _ibkr(ib_account_id="", allow_order_transmit=True)


def test_the_refusal_happens_at_load_before_any_adapter_exists() -> None:
    """A bad shape never reaches OfficialIBKRBroker: Settings itself refuses it."""

    constructed: list[object] = []
    with pytest.raises(ValidationError):
        constructed.append(_ibkr(ib_port=7496))
    assert constructed == []


def test_the_port_sets_have_one_source_of_truth() -> None:
    assert (frozenset({7497, 4002}), frozenset({7496, 4001})) == (PAPER_API_PORTS, LIVE_API_PORTS)
    assert official_ibkr._PAPER_PORTS is PAPER_API_PORTS
    assert official_ibkr._LIVE_PORTS is LIVE_API_PORTS
    assert b1_preflight._PAPER_PORTS is PAPER_API_PORTS
    assert b1_preflight._LIVE_PORTS is LIVE_API_PORTS


@pytest.mark.parametrize(
    ("message", "condition"),
    [
        ("IB_ENVIRONMENT=paper requires a paper port (7497 TWS or 4002 Gateway) ...", "P3"),
        ("IB_ACCOUNT_ID is not a paper account id ...", "P4"),
        ("IB_ACCOUNT_ID is required for BROKER_MODE=ibkr ...", "P4"),
        ("IB_CLIENT_ID=0 is IB's master client ...", "P8"),
        ("IB_DATA_CLIENT_ID must differ from IB_CLIENT_ID ...", "P8"),
        ("IB_ACCOUNT_ID is required before paper order transmission", "P1"),
        ("something else", "P1"),
    ],
)
def test_the_b1_preflight_reports_a_load_refusal_under_its_own_condition(
    message: str, condition: str
) -> None:
    assert b1_preflight._condition_of_load_refusal(message) == condition

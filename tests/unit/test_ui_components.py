from datetime import UTC, datetime
from decimal import Decimal

import pytest

from chronos.domain.enums import (
    BrokerMode,
    DataQuality,
    DemoProfile,
    DisplayEnvironment,
)
from chronos.ui.components import format_money, runtime_safety_notice
from chronos.ui.runtime_scope import RuntimeScopeView


def _runtime_scope(
    *,
    broker_mode: BrokerMode,
    environment: DisplayEnvironment,
) -> RuntimeScopeView:
    return RuntimeScopeView(
        broker_mode=broker_mode,
        demo_profile=(DemoProfile.SAFETY_CASES if broker_mode is BrokerMode.DEMO else None),
        environment=environment,
        data_quality=(DataQuality.DEMO if broker_mode is BrokerMode.DEMO else DataQuality.UNKNOWN),
        masked_account_id="DU•••4567",
        account_observed_at=datetime(2026, 7, 15, 15, 30, tzinfo=UTC),
    )


def test_format_money_uses_explicit_iso_currency_without_dollar_assumption() -> None:
    rendered = format_money(Decimal("1234.5"), "eur")

    assert rendered == "1,234.50 EUR"
    assert "$" not in rendered


def test_format_money_preserves_missing_evidence_label() -> None:
    assert format_money(None, "EUR") == "Unavailable"


def test_format_money_rejects_blank_currency() -> None:
    with pytest.raises(ValueError, match="currency must not be blank"):
        format_money(Decimal("1"), "  ")


@pytest.mark.parametrize(
    ("scope", "source_copy"),
    [
        (
            _runtime_scope(
                broker_mode=BrokerMode.DEMO,
                environment=DisplayEnvironment.DEMO,
            ),
            "deterministic synthetic DEMO data",
        ),
        (
            _runtime_scope(
                broker_mode=BrokerMode.IBKR,
                environment=DisplayEnvironment.PAPER,
            ),
            "read-only IBKR PAPER session",
        ),
        (
            _runtime_scope(
                broker_mode=BrokerMode.IBKR,
                environment=DisplayEnvironment.LIVE,
            ),
            "read-only IBKR LIVE session",
        ),
    ],
)
def test_runtime_safety_notice_is_mode_accurate_and_historical(
    scope: RuntimeScopeView,
    source_copy: str,
) -> None:
    notice = runtime_safety_notice(scope)

    assert source_copy in notice
    assert "Live-money transmission is hard-disabled" in notice
    assert "historical" in notice
    if scope.broker_mode is BrokerMode.IBKR:
        assert "synthetic" not in notice

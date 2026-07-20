"""Explicit LIVE TRADING BLOCKED gate (research-readiness hardening)."""

from __future__ import annotations

import pytest

from chronos.config.settings import Settings
from chronos.orders.live_block import (
    LIVE_TRADING_BLOCKED,
    LiveBlockDecision,
    assert_live_trading_blocked,
    evaluate_live_trading_block,
)


def test_default_settings_are_live_trading_blocked() -> None:
    settings = Settings(_env_file=None)
    decision = evaluate_live_trading_block(settings)
    assert decision.blocked is True
    assert decision.may_transmit_live is False
    assert decision.outcome == LIVE_TRADING_BLOCKED
    assert decision.outcome == "LIVE TRADING BLOCKED"
    assert any("ALLOW_LIVE_TRADING" in r for r in decision.reasons)


def test_assert_live_trading_blocked_passes_on_default() -> None:
    settings = Settings(_env_file=None)
    decision = assert_live_trading_blocked(settings)
    assert isinstance(decision, LiveBlockDecision)
    assert decision.blocked is True


def test_assert_live_trading_blocked_raises_when_conjunction_true() -> None:
    # Full ADR-0009 conjunction — configuration may report transmission
    # possible; the assertion must refuse that state for research/CI callers.
    settings = Settings(
        _env_file=None,
        broker_mode="ibkr",
        broker_adapter="official_ibkr",
        ib_environment="live",
        allow_order_transmit=True,
        allow_live_trading=True,
        ib_account_id="U7654321",
        ib_account_allowlist=("U7654321",),
        require_live_arming=True,
        require_typed_confirmation=True,
    )
    assert settings.live_transmission_possible is True
    with pytest.raises(RuntimeError, match="LIVE TRADING BLOCKED"):
        assert_live_trading_blocked(settings)


def test_research_defaults_cannot_enter_paper_or_live_transmission() -> None:
    """Research-like process defaults: no transmit, no live, demo mode."""

    settings = Settings(
        _env_file=None,
        broker_mode="demo",
        allow_order_transmit=False,
        allow_live_trading=False,
    )
    assert settings.transmission_possible is False
    assert settings.live_transmission_possible is False
    decision = assert_live_trading_blocked(settings)
    assert decision.outcome == LIVE_TRADING_BLOCKED

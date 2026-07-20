"""Paper portfolio controls: cannot bypass via retry/halt/loss/malformed state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chronos.config.settings import Settings
from chronos.orders.live_block import LIVE_TRADING_BLOCKED, assert_live_trading_blocked
from chronos.paperops.controls import PaperControlState, evaluate_paper_controls
from chronos.paperops.decision import PaperDecisionInput, evaluate_paper_decision
from chronos.paperops.reasons import PaperReasonCode

NOW = datetime(2024, 6, 3, 15, 0, 0, tzinfo=UTC)


def _state(**overrides: object) -> PaperControlState:
    base: dict[str, object] = {
        "halted": False,
        "halt_detail": "",
        "kill_switch_engaged": False,
        "kill_switch_detail": "",
        "account_equity_usd": 10000.0,
        "cash_usd": 10000.0,
        "position_notional_by_symbol": {},
        "open_position_count": 0,
        "realized_pnl_today_usd": 0.0,
        "recent_order_fingerprints": (),
        "last_order_at_utc": None,
        "proposed_order_fingerprint": "fp-1",
        "proposed_symbol": "SPY",
        "proposed_notional_usd": 500.0,
        "max_aggregate_exposure_usd": 5000.0,
        "max_symbol_exposure_fraction": 0.5,
        "max_simultaneous_positions": 3,
        "max_daily_loss_usd": 300.0,
        "cooldown_seconds": 60.0,
        "now_utc": NOW,
    }
    base.update(overrides)
    return PaperControlState(**base)  # type: ignore[arg-type]


def test_healthy_controls_allow() -> None:
    decision = evaluate_paper_controls(_state())
    assert decision.allowed is True
    assert PaperReasonCode.CONTROLS_OK in decision.reason_codes


def test_halt_blocks() -> None:
    decision = evaluate_paper_controls(_state(halted=True, halt_detail="operator"))
    assert decision.allowed is False
    assert PaperReasonCode.HALTED in decision.reason_codes


def test_kill_switch_blocks() -> None:
    decision = evaluate_paper_controls(_state(kill_switch_engaged=True, kill_switch_detail="panic"))
    assert decision.allowed is False
    assert PaperReasonCode.KILL_SWITCH_ENGAGED in decision.reason_codes


def test_duplicate_fingerprint_blocks_retry() -> None:
    decision = evaluate_paper_controls(
        _state(recent_order_fingerprints=("fp-1", "fp-other"), proposed_order_fingerprint="fp-1")
    )
    assert decision.allowed is False
    assert PaperReasonCode.DUPLICATE_ORDER in decision.reason_codes


def test_cooldown_blocks() -> None:
    decision = evaluate_paper_controls(
        _state(last_order_at_utc=NOW - timedelta(seconds=10), cooldown_seconds=60.0)
    )
    assert decision.allowed is False
    assert PaperReasonCode.COOLDOWN_ACTIVE in decision.reason_codes


def test_daily_loss_blocks() -> None:
    decision = evaluate_paper_controls(
        _state(realized_pnl_today_usd=-300.0, max_daily_loss_usd=300.0)
    )
    assert decision.allowed is False
    assert PaperReasonCode.DAILY_LOSS_LIMIT in decision.reason_codes


def test_exposure_blocks() -> None:
    decision = evaluate_paper_controls(
        _state(
            position_notional_by_symbol={"QQQ": 4800.0},
            proposed_notional_usd=500.0,
            max_aggregate_exposure_usd=5000.0,
        )
    )
    assert decision.allowed is False
    assert PaperReasonCode.EXPOSURE_LIMIT in decision.reason_codes


def test_concentration_blocks() -> None:
    decision = evaluate_paper_controls(
        _state(
            account_equity_usd=1000.0,
            proposed_notional_usd=600.0,
            max_symbol_exposure_fraction=0.5,
            max_aggregate_exposure_usd=10000.0,
        )
    )
    assert decision.allowed is False
    assert PaperReasonCode.CONCENTRATION_LIMIT in decision.reason_codes


def test_position_limit_blocks() -> None:
    decision = evaluate_paper_controls(
        _state(open_position_count=3, max_simultaneous_positions=3, proposed_symbol="NEW")
    )
    assert decision.allowed is False
    assert PaperReasonCode.POSITION_LIMIT in decision.reason_codes


def test_malformed_negative_notional_blocks() -> None:
    decision = evaluate_paper_controls(_state(proposed_notional_usd=-1.0))
    assert decision.allowed is False
    assert PaperReasonCode.EXPOSURE_LIMIT in decision.reason_codes


def test_zero_equity_fails_closed() -> None:
    decision = evaluate_paper_controls(_state(account_equity_usd=0.0))
    assert decision.allowed is False


def test_decision_path_halt_cannot_be_bypassed_by_good_quote() -> None:
    inp = PaperDecisionInput(
        strategy_id="s",
        strategy_version="1",
        config_hash="c",
        symbol="SPY",
        side="BUY",
        quantity=1,
        limit_price=100.0,
        bid=99.0,
        ask=101.0,
        last=100.0,
        quote_utc=NOW.isoformat(),
        data_source="ibkr",
        quality_label="LIVE",
        max_quote_age_seconds=30.0,
        halted=True,
        halt_detail="locked",
        account_equity_usd=10000.0,
        max_aggregate_exposure_usd=5000.0,
        max_symbol_exposure_fraction=1.0,
        max_simultaneous_positions=5,
        max_daily_loss_usd=1000.0,
        now_utc=(NOW + timedelta(seconds=1)).isoformat(),
        order_fingerprint="x",
    )
    result = evaluate_paper_decision(inp)
    assert result.may_open is False
    assert result.primary_reason is PaperReasonCode.HALTED


def test_live_remains_blocked_under_paper_like_settings() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="demo",
        allow_order_transmit=False,
        allow_live_trading=False,
    )
    assert settings.live_transmission_possible is False
    decision = assert_live_trading_blocked(settings)
    assert decision.outcome == LIVE_TRADING_BLOCKED

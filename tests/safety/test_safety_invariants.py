"""Safety acceptance tests (Phase 16).

Each test maps to an invariant in docs/RISK_POLICY.md and the build brief.
These tests exercise the real components — no mocks of the objects under
test — and prove the platform fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.control.halt import HaltReason, HaltState, HaltStore
from chronos.control.modes import (
    ExecutionCapability,
    TradingMode,
    resolve_mode_lock,
)
from chronos.domain.enums import OrderSide
from chronos.execution.brokers.simulated import SimulatedExecutionBroker
from chronos.execution.engine import ExecutionEngine, SubmissionRefused
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.ledger import MemoryLedger
from chronos.risk.engine import (
    AccountView,
    MarketViewEntry,
    RiskApproval,
    RiskEngine,
    RiskRejectionCode,
)
from chronos.risk.policy import RiskPolicy

NOW = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def make_intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "strategy_id": "strat_a",
        "strategy_version": "1",
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "quantity": 5,
        "limit_price": Decimal("500.10"),
        "stop_price": Decimal("485.00"),
        "time_in_force": TimeInForce.DAY,
        "decision_timestamp_utc": NOW,
        "source_bar_sequence_id": "test:SPY:1d:2024-06-03",
        "proposal_reason": "test",
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


def permissive_policy(**overrides: object) -> RiskPolicy:
    payload: dict[str, object] = {
        "policy_version": "test",
        "allowed_symbols": ("SPY",),
        "allowed_strategy_ids": ("strat_a",),
        "allow_long_entries": True,
        "max_bot_capital_usd": 3000,
        "max_position_notional_usd": 3000,
        "max_aggregate_exposure_usd": 3000,
        "max_symbol_exposure_fraction": 1.0,
        "max_risk_per_trade_fraction": 0.10,
        "max_simultaneous_positions": 1,
        "max_open_orders": 1,
        "max_daily_loss_usd": 300,
        "max_weekly_loss_usd": 600,
        "max_drawdown_fraction": 0.5,
        "max_consecutive_losses": 10,
        "max_quote_age_seconds": 300,
        "max_bar_age_seconds": 5 * 86400,
        "max_price_deviation_fraction": 0.05,
        "max_order_rejections_per_day": 3,
        "allow_overnight_positions": True,
    }
    payload.update(overrides)
    return RiskPolicy.model_validate(payload)


def healthy_account(**overrides: object) -> AccountView:
    payload: dict[str, object] = {
        "account_equity_usd": 3000.0,
        "cash_usd": 3000.0,
        "position_shares": {},
        "position_notional_usd": {},
        "open_order_count": 0,
        "realized_pnl_today_usd": 0.0,
        "realized_pnl_week_usd": 0.0,
        "peak_equity_usd": 3000.0,
        "consecutive_losses": 0,
        "as_of_utc": NOW,
    }
    payload.update(overrides)
    return AccountView(**payload)  # type: ignore[arg-type]


def fresh_market() -> MarketViewEntry:
    return MarketViewEntry(last_price=500.0, bar_close_utc=NOW, quote_utc=NOW)


def armed_halt(tmp_path: Path) -> HaltStore:
    store = HaltStore(tmp_path / "halt.json")
    store.rearm("test arm")
    return store


def sim_lock() -> object:
    return resolve_mode_lock(
        requested_mode=TradingMode.BACKTEST,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )


class TestModeLocks:
    def test_live_modes_are_hard_denied(self) -> None:
        for mode in (TradingMode.CANARY_LIVE, TradingMode.LIVE):
            lock = resolve_mode_lock(
                requested_mode=mode,
                paper_account_allowlist=("DU1234567",),
                broker_reported_account_id="DU1234567",
                broker_reported_environment_is_paper=True,
                order_transmission_enabled=True,
            )
            assert lock.capability is ExecutionCapability.DENIED_LIVE_DISABLED
            assert not lock.may_submit_paper

    def test_shadow_mode_cannot_submit(self) -> None:
        lock = resolve_mode_lock(
            requested_mode=TradingMode.SHADOW,
            paper_account_allowlist=("DU1234567",),
            broker_reported_account_id="DU1234567",
            broker_reported_environment_is_paper=True,
            order_transmission_enabled=True,
        )
        assert lock.capability is ExecutionCapability.NO_ORDERS

    def test_paper_requires_every_condition(self) -> None:
        base = {
            "requested_mode": TradingMode.PAPER,
            "paper_account_allowlist": ("DU1234567",),
            "broker_reported_account_id": "DU1234567",
            "broker_reported_environment_is_paper": True,
            "order_transmission_enabled": True,
        }
        good = resolve_mode_lock(**base)  # type: ignore[arg-type]
        assert good.capability is ExecutionCapability.PAPER_SUBMISSION

        degraded_cases: list[dict[str, object]] = [
            {**base, "paper_account_allowlist": ()},
            {**base, "broker_reported_account_id": None},
            {**base, "broker_reported_account_id": "U9999999"},
            {**base, "broker_reported_environment_is_paper": False},
            {**base, "broker_reported_environment_is_paper": None},
            {**base, "order_transmission_enabled": False},
            {**base, "broker_reported_account_id": "DX000", "paper_account_allowlist": ("DX000",)},
        ]
        for case in degraded_cases:
            lock = resolve_mode_lock(**case)  # type: ignore[arg-type]
            assert lock.capability is ExecutionCapability.NO_ORDERS, case
            assert lock.denial_reasons, case

    def test_live_account_id_on_paper_allowlist_is_still_denied(self) -> None:
        # A live-looking account id (no 'D' prefix) cannot pass even if the
        # operator mistakenly allowlists it.
        lock = resolve_mode_lock(
            requested_mode=TradingMode.PAPER,
            paper_account_allowlist=("U1234567",),
            broker_reported_account_id="U1234567",
            broker_reported_environment_is_paper=True,
            order_transmission_enabled=True,
        )
        assert lock.capability is ExecutionCapability.NO_ORDERS


class TestHaltPersistence:
    def test_missing_halt_file_fails_closed(self, tmp_path: Path) -> None:
        state = HaltStore(tmp_path / "halt.json").read()
        assert state.halted
        assert state.reason is HaltReason.NEVER_ARMED

    def test_corrupt_halt_file_fails_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "halt.json"
        path.write_text("{not json", encoding="utf-8")
        state = HaltStore(path).read()
        assert state.halted
        assert state.reason is HaltReason.STATE_CORRUPTION

    def test_halt_survives_process_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "halt.json"
        HaltStore(path).halt(HaltReason.DAILY_LOSS_LIMIT, "limit hit")
        state = HaltStore(path).read()  # fresh store simulates restart
        assert state.halted
        assert state.reason is HaltReason.DAILY_LOSS_LIMIT

    def test_rearm_requires_note(self, tmp_path: Path) -> None:
        store = HaltStore(tmp_path / "halt.json")
        with pytest.raises(ValueError):
            store.rearm("   ")
        assert store.read().halted


class TestRiskEngineDenyByDefault:
    def test_all_zero_policy_rejects_everything(self, tmp_path: Path) -> None:
        engine = RiskEngine(RiskPolicy())
        decision = engine.validate(
            make_intent(),
            account=healthy_account(),
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert decision.approval is None
        assert RiskRejectionCode.ZERO_CAPITAL_AUTHORIZED in decision.codes
        assert RiskRejectionCode.SYMBOL_NOT_ALLOWED in decision.codes
        assert RiskRejectionCode.STRATEGY_NOT_ALLOWED in decision.codes

    def test_permissive_policy_approves_clean_intent(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(),
            account=healthy_account(),
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert decision.approved, decision.explanations
        assert decision.approval is not None

    def test_halt_blocks_approval(self, tmp_path: Path) -> None:
        store = armed_halt(tmp_path)
        store.halt(HaltReason.RECONCILIATION_MISMATCH, "mismatch")
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(),
            account=healthy_account(),
            market=fresh_market(),
            halt=store.read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.HALTED in decision.codes

    def test_stale_quote_blocks(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        stale = MarketViewEntry(
            last_price=500.0,
            bar_close_utc=NOW,
            quote_utc=NOW - timedelta(hours=2),
        )
        decision = engine.validate(
            make_intent(),
            account=healthy_account(),
            market=stale,
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.STALE_MARKET_DATA in decision.codes

    def test_missing_account_state_blocks(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(),
            account=None,
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.ACCOUNT_STATE_MISSING in decision.codes

    def test_daily_loss_limit_blocks_entries(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(),
            account=healthy_account(realized_pnl_today_usd=-300.0),
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.DAILY_LOSS_LIMIT in decision.codes

    def test_entry_without_stop_blocks(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(stop_price=None),
            account=healthy_account(),
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.STOP_REQUIRED in decision.codes

    def test_short_sell_blocked_without_position(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        decision = engine.validate(
            make_intent(side=OrderSide.SELL, stop_price=None),
            account=healthy_account(),
            market=fresh_market(),
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.SELL_WITHOUT_POSITION in decision.codes

    def test_duplicate_intent_rejected_second_time(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        halt = armed_halt(tmp_path).read()
        intent = make_intent()
        first = engine.validate(
            intent,
            account=healthy_account(),
            market=fresh_market(),
            halt=halt,
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        second = engine.validate(
            intent,
            account=healthy_account(),
            market=fresh_market(),
            halt=halt,
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert first.approved
        assert not second.approved
        assert RiskRejectionCode.DUPLICATE_INTENT in second.codes

    def test_risk_engine_internal_error_fails_closed(self, tmp_path: Path) -> None:
        engine = RiskEngine(permissive_policy())
        broken_market = MarketViewEntry(
            last_price=500.0, bar_close_utc=NOW, quote_utc=None  # type: ignore[arg-type]
        )
        decision = engine.validate(
            make_intent(),
            account=healthy_account(),
            market=broken_market,
            halt=armed_halt(tmp_path).read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert not decision.approved
        assert RiskRejectionCode.INTERNAL_ERROR_FAIL_CLOSED in decision.codes


def build_engine(tmp_path: Path, **overrides: object) -> tuple[ExecutionEngine, RiskEngine]:
    risk_engine = RiskEngine(permissive_policy())
    engine = ExecutionEngine(
        broker=SimulatedExecutionBroker(),
        risk_engine=risk_engine,
        ledger=MemoryLedger(),
        halt_store=armed_halt(tmp_path),
        mode_lock=sim_lock(),  # type: ignore[arg-type]
        reconciliation_passed=True,
    )
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine, risk_engine


class TestExecutionGate:
    def approved(self, risk_engine: RiskEngine, tmp_path: Path) -> tuple[OrderIntent, object]:
        intent = make_intent()
        decision = risk_engine.validate(
            intent,
            account=healthy_account(),
            market=fresh_market(),
            halt=HaltStore(tmp_path / "halt.json").read(),
            mode_lock=sim_lock(),  # type: ignore[arg-type]
            now_utc=NOW,
        )
        assert decision.approved
        return intent, decision.approval

    def test_no_approval_no_submission(self, tmp_path: Path) -> None:
        engine, _ = build_engine(tmp_path)
        result = engine.submit_approved(make_intent(), None, now_utc=NOW)
        assert not result.submitted
        assert result.refusal is SubmissionRefused.NO_APPROVAL

    def test_forged_approval_refused(self, tmp_path: Path) -> None:
        engine, _ = build_engine(tmp_path)
        intent = make_intent()
        forged = RiskApproval(
            intent_id=intent.intent_id,
            policy_version="test",
            policy_hash="x",
            minted_by=object(),
        )
        result = engine.submit_approved(intent, forged, now_utc=NOW)
        assert not result.submitted
        assert result.refusal is SubmissionRefused.FOREIGN_APPROVAL

    def test_approval_for_other_intent_refused(self, tmp_path: Path) -> None:
        engine, risk_engine = build_engine(tmp_path)
        intent_a, approval_a = self.approved(risk_engine, tmp_path)
        other = make_intent(quantity=6)
        result = engine.submit_approved(other, approval_a, now_utc=NOW)  # type: ignore[arg-type]
        assert not result.submitted
        assert result.refusal is SubmissionRefused.APPROVAL_INTENT_MISMATCH

    def test_halt_blocks_submission_even_with_approval(self, tmp_path: Path) -> None:
        engine, risk_engine = build_engine(tmp_path)
        intent, approval = self.approved(risk_engine, tmp_path)
        engine.halt_store.halt(HaltReason.OPERATOR_REQUEST, "manual halt")
        result = engine.submit_approved(intent, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert not result.submitted
        assert result.refusal is SubmissionRefused.HALTED

    def test_reconciliation_pending_blocks_submission(self, tmp_path: Path) -> None:
        engine, risk_engine = build_engine(tmp_path, reconciliation_passed=False)
        intent, approval = self.approved(risk_engine, tmp_path)
        result = engine.submit_approved(intent, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert not result.submitted
        assert result.refusal is SubmissionRefused.RECONCILIATION_PENDING

    def test_duplicate_intent_blocked_by_ledger(self, tmp_path: Path) -> None:
        engine, risk_engine = build_engine(tmp_path)
        intent, approval = self.approved(risk_engine, tmp_path)
        first = engine.submit_approved(intent, approval, now_utc=NOW)  # type: ignore[arg-type]
        second = engine.submit_approved(intent, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert first.submitted
        assert not second.submitted
        assert second.refusal is SubmissionRefused.DUPLICATE_INTENT

    def test_ledger_failure_halts_and_blocks(self, tmp_path: Path) -> None:
        engine, risk_engine = build_engine(tmp_path)
        intent, approval = self.approved(risk_engine, tmp_path)
        assert isinstance(engine.ledger, MemoryLedger)
        engine.ledger.fail_writes = True
        result = engine.submit_approved(intent, approval, now_utc=NOW)  # type: ignore[arg-type]
        assert not result.submitted
        assert result.refusal is SubmissionRefused.LEDGER_UNAVAILABLE
        assert engine.halt_store.read().halted

    def test_unknown_broker_event_halts(self, tmp_path: Path) -> None:
        engine, _ = build_engine(tmp_path)
        broker = engine.broker
        assert isinstance(broker, SimulatedExecutionBroker)
        # Inject an event for an intent the engine never submitted.
        broker.submit(make_intent(quantity=7))
        engine.pump_events(now_utc=NOW)
        state = engine.halt_store.read()
        assert state.halted
        assert state.reason is HaltReason.UNKNOWN_ORDER


class TestStrategyIsolation:
    def test_strategy_package_does_not_import_brokers(self) -> None:
        import chronos.strategies.base as strategies_base

        source = Path(strategies_base.__file__).read_text(encoding="utf-8")
        for forbidden in ("ib_async", "chronos.broker", "chronos.execution.brokers"):
            assert forbidden not in source

    def test_proposal_has_no_order_fields(self) -> None:
        from chronos.strategies.base import StrategyProposal

        fields = {f for f in StrategyProposal.__dataclass_fields__}
        for forbidden in ("account", "account_id", "quantity", "shares", "broker", "order_id"):
            assert forbidden not in fields

    def test_risk_policy_is_frozen(self) -> None:
        policy = permissive_policy()
        with pytest.raises(Exception):  # pydantic frozen -> ValidationError
            policy.max_bot_capital_usd = 1_000_000  # type: ignore[misc]

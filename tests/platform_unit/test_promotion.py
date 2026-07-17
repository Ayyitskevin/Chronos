"""Promotion gates: live hard-disable, single-step rule, record contents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronos.control.modes import TradingMode
from chronos.control.promotion import GateCheck, evaluate_promotion, write_promotion_record

PASSING = [
    GateCheck(name="backtest_suite", passed=True, detail="all green"),
    GateCheck(name="parity_report", passed=True, detail="reviewed"),
]


class TestLiveHardDisable:
    @pytest.mark.parametrize("proposed", [TradingMode.CANARY_LIVE, TradingMode.LIVE])
    def test_live_promotion_always_fails(self, proposed: TradingMode) -> None:
        current = (
            TradingMode.PAPER if proposed is TradingMode.CANARY_LIVE else TradingMode.CANARY_LIVE
        )
        allowed, augmented = evaluate_promotion(
            current_mode=current, proposed_mode=proposed, checks=list(PASSING)
        )
        assert allowed is False
        hard_disabled = [c for c in augmented if c.name == "live_capability_hard_disabled"]
        assert len(hard_disabled) == 1
        assert hard_disabled[0].passed is False
        # The supplied passing checks are preserved, not replaced.
        assert augmented[: len(PASSING)] == PASSING


class TestSingleStepRule:
    def test_skipping_modes_fails_single_step_gate(self) -> None:
        allowed, augmented = evaluate_promotion(
            current_mode=TradingMode.BACKTEST,
            proposed_mode=TradingMode.PAPER,
            checks=list(PASSING),
        )
        assert allowed is False
        names = [c.name for c in augmented]
        assert "single_step_promotion" in names

    def test_legal_single_step_with_passing_checks_is_allowed(self) -> None:
        allowed, augmented = evaluate_promotion(
            current_mode=TradingMode.BACKTEST,
            proposed_mode=TradingMode.REPLAY,
            checks=list(PASSING),
        )
        assert allowed is True
        assert augmented == PASSING  # nothing appended

    def test_single_step_with_a_failing_check_is_denied(self) -> None:
        checks = [*PASSING, GateCheck(name="ops_runbook", passed=False, detail="missing")]
        allowed, _ = evaluate_promotion(
            current_mode=TradingMode.SHADOW, proposed_mode=TradingMode.PAPER, checks=checks
        )
        assert allowed is False

    def test_demotion_or_same_step_is_not_flagged_as_skip(self) -> None:
        allowed, augmented = evaluate_promotion(
            current_mode=TradingMode.PAPER,
            proposed_mode=TradingMode.SHADOW,
            checks=list(PASSING),
        )
        assert allowed is True
        assert augmented == PASSING


class TestPromotionRecord:
    def test_record_file_written_with_expected_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "promotions" / "record.json"
        record = write_promotion_record(
            path,
            current_mode=TradingMode.SHADOW,
            proposed_mode=TradingMode.PAPER,
            code_commit="abc1234",
            strategy_versions={"regime_trend_v1": "1.0.0"},
            risk_policy_version="7",
            risk_policy_hash="deadbeef",
            config_hash="cafef00d",
            checks=list(PASSING),
            known_limitations=["daily bars only"],
            outstanding_incidents=[],
            owner_approval="klee",
            rollback_plan="set mode back to shadow",
        )
        assert record.all_gates_passed is True
        assert record.current_mode == "shadow"
        assert record.proposed_mode == "paper"

        payload = json.loads(path.read_text(encoding="utf-8"))
        expected_keys = {
            "created_at_utc",
            "current_mode",
            "proposed_mode",
            "code_commit",
            "strategy_versions",
            "risk_policy_version",
            "risk_policy_hash",
            "config_hash",
            "gate_checks",
            "all_gates_passed",
            "known_limitations",
            "outstanding_incidents",
            "owner_approval",
            "rollback_plan",
        }
        assert set(payload) == expected_keys
        assert payload["code_commit"] == "abc1234"
        assert payload["strategy_versions"] == {"regime_trend_v1": "1.0.0"}
        assert payload["all_gates_passed"] is True
        assert payload["gate_checks"] == [
            {"name": "backtest_suite", "passed": True, "detail": "all green"},
            {"name": "parity_report", "passed": True, "detail": "reviewed"},
        ]

    def test_record_for_live_promotion_reports_denial(self, tmp_path: Path) -> None:
        path = tmp_path / "record.json"
        record = write_promotion_record(
            path,
            current_mode=TradingMode.PAPER,
            proposed_mode=TradingMode.CANARY_LIVE,
            code_commit="abc1234",
            strategy_versions={},
            risk_policy_version="7",
            risk_policy_hash="deadbeef",
            config_hash="cafef00d",
            checks=list(PASSING),
            known_limitations=[],
            outstanding_incidents=[],
            owner_approval="klee",
            rollback_plan="n/a",
        )
        assert record.all_gates_passed is False
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["all_gates_passed"] is False
        names = [check["name"] for check in payload["gate_checks"]]
        assert "live_capability_hard_disabled" in names

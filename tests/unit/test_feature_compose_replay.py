from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_five_tool_replay import _inputs, _replay_bars, _settings

from chronos.research.features.compose import compose_pairing_frames
from chronos.research.features.models import (
    FeatureFamily,
    FeaturePolicy,
    FeatureSnapshot,
    VetoStatus,
)
from chronos.research.features.pairing_replay import replay_pairing
from chronos.research.features.rvol import evaluate_daily_rvol
from chronos.research.features.tail_risk import evaluate_tail_risk
from chronos.research.features.veto import apply_vetoes, decide_veto
from chronos.research.five_tool.models import (
    FiveToolTrace,
    SetupFamily,
    Side,
    SignalEvent,
    SignalIntent,
)
from chronos.research.five_tool.replay import FiveToolReplayPolicy, TerminalPositionPolicy


def _trace(
    *,
    index: int,
    intent: SignalIntent,
    sequence: str = "feature_fixture:AAA:1d:2020-01-02",
    timestamp: datetime | None = None,
) -> FiveToolTrace:
    stamp = timestamp or datetime(2020, 1, 2 + index, 21, tzinfo=UTC)
    return FiveToolTrace(
        bar_index=index,
        timestamp_utc=stamp,
        primary_sequence_id=f"{sequence}:{stamp.isoformat()}",
        benchmark_source_id=None,
        htf_source_id=None,
        history_start_utc=datetime(2020, 1, 2, 21, tzinfo=UTC),
        features=(("regime", 1),),
        gates=(("can_long", intent is SignalIntent.ENTER_LONG),),
        warmup_blockers=(),
        long_setup=SetupFamily.NONE,
        short_setup=SetupFamily.NONE,
        intent=intent,
        events=()
        if intent is SignalIntent.NONE
        else (
            SignalEvent(
                event_id=f"evt-{index}",
                kind=intent.value,
                timestamp_utc=stamp,
                side=Side.LONG,
                setup=SetupFamily.NONE,
            ),
        ),
        state_digest="0" * 64,
    )


def _snapshot(
    family: FeatureFamily,
    trace: FiveToolTrace,
    values: dict[str, object],
    *,
    warmup: bool = False,
    missing: tuple[str, ...] = (),
) -> FeatureSnapshot:
    return FeatureSnapshot(
        family=family,
        timestamp_utc=trace.timestamp_utc,
        primary_sequence_id=trace.primary_sequence_id,
        values=tuple(values.items()),  # type: ignore[arg-type]
        warmup=warmup,
        missing_required=missing,
    )


def test_veto_masks_enter_only_and_preserves_exits() -> None:
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    enter = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    exit_long = _trace(index=1, intent=SignalIntent.EXIT_LONG)
    fat = _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "FAT_TAILED"})
    fat_exit = _snapshot(FeatureFamily.TAIL_RISK, exit_long, {"TR_STATE": "FAT_TAILED"})
    blocked = decide_veto(enter.intent, (fat,), policy, symbol="QQQ")
    passed = decide_veto(exit_long.intent, (fat_exit,), policy, symbol="QQQ")
    assert blocked.status is VetoStatus.VETO
    assert blocked.filtered_intent is SignalIntent.NONE
    assert passed.status is VetoStatus.ALLOW
    assert passed.filtered_intent is SignalIntent.EXIT_LONG


def test_missing_companion_outranks_warmup_and_veto() -> None:
    policy = FeaturePolicy(
        enable_tail_risk=False,
        enable_rvol=False,
        enable_iv_regime=True,
        enable_breadth=True,
    )
    enter = _trace(index=0, intent=SignalIntent.ENTER_SHORT)
    snapshots = (
        _snapshot(
            FeatureFamily.IV_REGIME,
            enter,
            {"IVP_STATE": "STRESS", "IVP_BACKWARDATION": False},
            warmup=True,
        ),
        _snapshot(
            FeatureFamily.BREADTH,
            enter,
            {"ALIGN": -1},
            missing=("spy",),
        ),
    )
    decision = decide_veto(enter.intent, snapshots, policy, symbol="QQQ")
    assert decision.status is VetoStatus.MISSING_COMPANION
    assert "breadth:missing_companion" in decision.reasons


def test_compose_leaves_five_tool_traces_identical() -> None:
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    traces = (
        _trace(index=0, intent=SignalIntent.ENTER_LONG),
        _trace(index=1, intent=SignalIntent.NONE),
    )
    snapshots = (
        _snapshot(FeatureFamily.TAIL_RISK, traces[0], {"TR_STATE": "ORDINARY"}),
        _snapshot(FeatureFamily.TAIL_RISK, traces[1], {"TR_STATE": "ORDINARY"}),
    )
    composition = compose_pairing_frames(traces, snapshots, policy, symbol="QQQ")
    assert composition.traces == traces
    assert composition.traces[0] is traces[0]
    assert traces[0].intent is SignalIntent.ENTER_LONG
    assert composition.frames[0].decision.filtered_intent is SignalIntent.ENTER_LONG


def test_apply_vetoes_rvol_and_breadth_and_iv() -> None:
    policy = FeaturePolicy()
    enter = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    snapshots = (
        _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "ELEVATED"}),
        _snapshot(FeatureFamily.RVOL, enter, {"IN_PLAY": False}),
        _snapshot(
            FeatureFamily.IV_REGIME,
            enter,
            {"IVP_STATE": "ELEVATED", "IVP_BACKWARDATION": True},
        ),
        _snapshot(FeatureFamily.BREADTH, enter, {"ALIGN": -1}),
    )
    (decision,) = apply_vetoes((enter,), ((snapshots),), policy, symbol="QQQ")
    assert decision.status is VetoStatus.VETO
    assert "rvol:not_in_play" in decision.reasons
    assert "iv_regime:ELEVATED_backwardation" in decision.reasons
    assert "breadth:ALIGN_divergent" in decision.reasons


def test_gld_ignores_equity_vix_and_breadth_while_qqq_does_not() -> None:
    policy = FeaturePolicy()
    enter = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    snapshots = (
        _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "ORDINARY"}),
        _snapshot(FeatureFamily.RVOL, enter, {"IN_PLAY": True}),
        _snapshot(
            FeatureFamily.IV_REGIME,
            enter,
            {"IVP_STATE": "STRESS", "IVP_BACKWARDATION": False},
        ),
        _snapshot(FeatureFamily.BREADTH, enter, {"ALIGN": -1}),
    )
    gold = decide_veto(enter.intent, snapshots, policy, symbol="GLD")
    equity = decide_veto(enter.intent, snapshots, policy, symbol="QQQ")
    assert gold.status is VetoStatus.ALLOW
    assert gold.filtered_intent is SignalIntent.ENTER_LONG
    assert equity.status is VetoStatus.VETO
    assert "iv_regime:STRESS" in equity.reasons
    assert "breadth:ALIGN_divergent" in equity.reasons
    assert "iv_regime:STRESS" not in gold.reasons
    assert "breadth:ALIGN_divergent" not in gold.reasons


def test_gld_still_vetoes_on_its_own_tail_and_rvol() -> None:
    policy = FeaturePolicy()
    enter = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    fat = decide_veto(
        enter.intent,
        (
            _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "FAT_TAILED"}),
            _snapshot(FeatureFamily.RVOL, enter, {"IN_PLAY": True}),
        ),
        policy,
        symbol="GLD",
    )
    quiet = decide_veto(
        enter.intent,
        (
            _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "ORDINARY"}),
            _snapshot(FeatureFamily.RVOL, enter, {"IN_PLAY": False}),
        ),
        policy,
        symbol="GLD",
    )
    assert fat.status is VetoStatus.VETO
    assert "tail_risk:FAT_TAILED" in fat.reasons
    assert quiet.status is VetoStatus.VETO
    assert "rvol:not_in_play" in quiet.reasons


def test_usd_regime_vetoes_gld_only_when_explicitly_enabled() -> None:
    default = FeaturePolicy()
    treatment = FeaturePolicy(enable_usd_regime=True)
    enter = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    snapshots = (
        _snapshot(FeatureFamily.TAIL_RISK, enter, {"TR_STATE": "ORDINARY"}),
        _snapshot(FeatureFamily.RVOL, enter, {"IN_PLAY": True}),
        _snapshot(FeatureFamily.USD_REGIME, enter, {"USD_STATE": "RISING"}),
    )
    shadow = decide_veto(enter.intent, snapshots, default, symbol="GLD")
    blocked = decide_veto(enter.intent, snapshots, treatment, symbol="GLD")
    equity = decide_veto(enter.intent, snapshots, treatment, symbol="QQQ")
    assert FeatureFamily.USD_REGIME not in default.enabled_families("GLD")
    assert shadow.status is VetoStatus.ALLOW
    assert blocked.status is VetoStatus.VETO
    assert "usd_regime:RISING" in blocked.reasons
    assert FeatureFamily.USD_REGIME not in treatment.enabled_families("QQQ")
    assert "usd_regime:RISING" not in equity.reasons


def test_replay_pairing_preserves_traces_and_can_mask_enters() -> None:
    settings = _settings()
    bars = _replay_bars(_inputs(settings, count=28))
    primary = tuple(item.input.primary for item in bars)
    tail = evaluate_tail_risk(primary, FeaturePolicy(tail_window=20))
    rvol = evaluate_daily_rvol(
        primary, FeaturePolicy(rvol_lookback=5, rvol_min_avg_dollar_vol_millions=0.0)
    )
    snapshots = tuple(item.snapshot for item in tail) + tuple(item.snapshot for item in rvol)
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=True,
        enable_iv_regime=False,
        enable_breadth=False,
        tail_window=20,
        rvol_lookback=5,
        rvol_min_avg_dollar_vol_millions=0.0,
    )
    result = replay_pairing(
        settings,
        bars,
        snapshots,
        policy,
        replay_policy=FiveToolReplayPolicy(
            terminal_position_policy=TerminalPositionPolicy.EXCLUDE_INCOMPLETE
        ),
    )
    assert result.control.traces == result.composition.traces
    assert result.control.traces[0] is result.composition.traces[0]
    assert result.treatment_enter_count <= result.control_enter_count
    for frame in result.composition.frames:
        if frame.original_intent in {SignalIntent.EXIT_LONG, SignalIntent.EXIT_SHORT}:
            assert frame.decision.filtered_intent is frame.original_intent

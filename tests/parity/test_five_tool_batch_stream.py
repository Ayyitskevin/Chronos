from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chronos.marketdata.bars import Bar, BarInterval
from chronos.research.five_tool.checkpoint import state_from_json, state_to_json
from chronos.research.five_tool.engine import FiveToolEngine, evaluate_batch
from chronos.research.five_tool.models import (
    CompanionValue,
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolSettings,
    FiveToolTrace,
)

_START = datetime(2021, 1, 4, 21, tzinfo=UTC)


def _case(count: int = 36) -> tuple[FiveToolSettings, tuple[FiveToolBarInput, ...]]:
    settings = FiveToolSettings.defaults(
        history_start_utc=_START,
        overrides={
            "preset_input": "Custom",
            "lookback_custom": 4,
            "confirm_custom": 2,
            "use_vol_percentile_adjustment": False,
            "use_ema_filter": False,
            "playbook_quality_filter": "Off",
            "min_strength_for_bias": 0.0,
            "mans_len": 4,
            "rs_mode": "Off",
            "use_equity_dd_halt": False,
        },
    )
    inputs: list[FiveToolBarInput] = []
    for index in range(count):
        timestamp = _START + timedelta(days=index)
        close = 50.0 + index * 0.3 + (0.2 if index % 3 == 0 else -0.1)
        bar = Bar(
            symbol="AAA",
            source="internal_spec",
            exchange="NYSE",
            interval=BarInterval.DAY_1,
            session_date=timestamp.date(),
            timestamp_utc=timestamp,
            open=close - 0.1,
            high=close + 0.6,
            low=close - 0.6,
            close=close,
            volume=10_000.0 + index,
        )
        inputs.append(
            FiveToolBarInput(
                primary=bar,
                benchmark=CompanionValue(
                    value=400.0 + index * 0.2,
                    source_timestamp_utc=timestamp,
                    source_sequence_id=f"SPY:{timestamp.isoformat()}",
                ),
            )
        )
    return settings, tuple(inputs)


@given(st.lists(st.integers(min_value=1, max_value=35), unique=True, max_size=8))
def test_arbitrary_serialized_chunk_boundaries_match_batch(boundaries: list[int]) -> None:
    settings, inputs = _case()
    expected = evaluate_batch(settings, inputs)
    engine = FiveToolEngine(settings)
    observed: list[FiveToolTrace] = []
    start = 0
    for stop in sorted({*boundaries, len(inputs)}):
        if stop <= start:
            continue
        observed.extend(engine.step(item) for item in inputs[start:stop])
        engine = FiveToolEngine(
            settings,
            state=state_from_json(state_to_json(engine.checkpoint())),
        )
        start = stop
    assert tuple(observed) == expected


def test_repeated_runs_are_byte_deterministic() -> None:
    settings, inputs = _case()
    first = evaluate_batch(settings, inputs)
    second = evaluate_batch(settings, inputs)
    assert first == second
    assert [trace.state_digest for trace in first] == [trace.state_digest for trace in second]


def test_future_benchmark_source_is_rejected_at_input_boundary() -> None:
    _, inputs = _case(1)
    item = inputs[0]
    assert item.benchmark is not None
    with pytest.raises(FiveToolInputError, match="future"):
        FiveToolBarInput(
            primary=item.primary,
            benchmark=CompanionValue(
                value=item.benchmark.value,
                source_timestamp_utc=item.primary.timestamp_utc + timedelta(seconds=1),
                source_sequence_id="future",
            ),
        )

"""Canonical JSON checkpoints for Five-Tool streaming/replay equivalence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from typing import cast

from chronos.marketdata.bars import Bar, BarInterval, BarStatus
from chronos.research.five_tool.models import (
    AccountSnapshot,
    CompanionValue,
    FiveToolBarInput,
    FiveToolInputError,
    FiveToolState,
    SetupFamily,
    Side,
)

CHECKPOINT_SCHEMA = "five-tool-state-v2"


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"checkpoint value is not JSON serializable: {type(value).__name__}")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def state_to_json(state: FiveToolState) -> str:
    """Serialize state with an integrity digest over the canonical payload."""

    state_payload = asdict(state)
    digest = hashlib.sha256(_canonical(state_payload).encode("utf-8")).hexdigest()
    return _canonical(
        {
            "schema": CHECKPOINT_SCHEMA,
            "state_sha256": digest,
            "state": state_payload,
        }
    )


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FiveToolInputError(f"checkpoint {context} must be an object")
    return cast(dict[str, object], value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(float | int, value))


def _optional_int(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _companion(value: object) -> CompanionValue | None:
    if value is None:
        return None
    raw = _object(value, "companion")
    return CompanionValue(
        value=float(cast(float | int, raw["value"])),
        source_timestamp_utc=datetime.fromisoformat(str(raw["source_timestamp_utc"])),
        source_sequence_id=str(raw["source_sequence_id"]),
    )


def _observation(value: object) -> FiveToolBarInput:
    raw = _object(value, "observation")
    primary_raw = _object(raw["primary"], "observation.primary")
    account_raw = _object(raw["account"], "observation.account")
    primary = Bar(
        symbol=str(primary_raw["symbol"]),
        source=str(primary_raw["source"]),
        exchange=str(primary_raw["exchange"]),
        interval=BarInterval(str(primary_raw["interval"])),
        session_date=date.fromisoformat(str(primary_raw["session_date"])),
        timestamp_utc=datetime.fromisoformat(str(primary_raw["timestamp_utc"])),
        open=float(cast(float | int, primary_raw["open"])),
        high=float(cast(float | int, primary_raw["high"])),
        low=float(cast(float | int, primary_raw["low"])),
        close=float(cast(float | int, primary_raw["close"])),
        volume=float(cast(float | int, primary_raw["volume"])),
        adjusted_close=_optional_float(primary_raw.get("adjusted_close")),
        status=BarStatus(str(primary_raw["status"])),
    )
    return FiveToolBarInput(
        primary=primary,
        benchmark=_companion(raw.get("benchmark")),
        htf_close=_companion(raw.get("htf_close")),
        htf_ema=_companion(raw.get("htf_ema")),
        external_regime=_optional_float(raw.get("external_regime")),
        external_strength=_optional_float(raw.get("external_strength")),
        long_plus_in_session=cast(bool | None, raw.get("long_plus_in_session")),
        short_plus_in_session=cast(bool | None, raw.get("short_plus_in_session")),
        account=AccountSnapshot(
            equity=float(cast(float | int, account_raw["equity"])),
            position=Side(str(account_raw["position"])),
            average_entry_price=_optional_float(account_raw.get("average_entry_price")),
            entry_bar_index=_optional_int(account_raw.get("entry_bar_index")),
            entry_setup=SetupFamily(str(account_raw["entry_setup"])),
            base_pivot_at_entry=_optional_float(account_raw.get("base_pivot_at_entry")),
            long_virtual_equity=_optional_float(account_raw.get("long_virtual_equity")),
            short_virtual_equity=_optional_float(account_raw.get("short_virtual_equity")),
        ),
    )


def _tuple_int(value: object) -> tuple[int, ...]:
    return tuple(int(cast(int, item)) for item in cast(list[object], value))


def _pivot(value: object) -> tuple[int, float, float] | None:
    if value is None:
        return None
    raw = cast(list[object], value)
    if len(raw) != 3:
        raise FiveToolInputError("checkpoint pivot must have three values")
    return (
        int(cast(int, raw[0])),
        float(cast(float | int, raw[1])),
        float(cast(float | int, raw[2])),
    )


def state_from_json(payload: str) -> FiveToolState:
    """Validate checkpoint integrity and rebuild the immutable state."""

    try:
        decoded: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FiveToolInputError("checkpoint is not valid JSON") from exc
    wrapper = _object(decoded, "root")
    if wrapper.get("schema") != CHECKPOINT_SCHEMA:
        raise FiveToolInputError("unsupported Five-Tool checkpoint schema")
    state_object = wrapper.get("state")
    state_raw = _object(state_object, "state")
    observed_digest = hashlib.sha256(_canonical(state_object).encode("utf-8")).hexdigest()
    if wrapper.get("state_sha256") != observed_digest:
        raise FiveToolInputError("Five-Tool checkpoint integrity digest mismatch")

    def bool_field(name: str) -> bool:
        value = state_raw[name]
        if not isinstance(value, bool):
            raise FiveToolInputError(f"checkpoint field {name} must be boolean")
        return value

    def optional_date(name: str) -> date | None:
        value = state_raw.get(name)
        return None if value is None else date.fromisoformat(str(value))

    return FiveToolState(
        settings_digest=str(state_raw["settings_digest"]),
        history_start_utc=datetime.fromisoformat(str(state_raw["history_start_utc"])),
        observations=tuple(
            _observation(item) for item in cast(list[object], state_raw["observations"])
        ),
        candidate_regimes=tuple(
            None if item is None else int(cast(int, item))
            for item in cast(list[object], state_raw["candidate_regimes"])
        ),
        confirmed_core=_optional_int(state_raw.get("confirmed_core")),
        last_regime=int(cast(int, state_raw["last_regime"])),
        have_regime=bool_field("have_regime"),
        previous_selected_regime=_optional_int(state_raw.get("previous_selected_regime")),
        internal_bars_in_regime=int(cast(int, state_raw["internal_bars_in_regime"])),
        active_bars_in_regime=int(cast(int, state_raw["active_bars_in_regime"])),
        dwell_bull=_tuple_int(state_raw["dwell_bull"]),
        dwell_neutral=_tuple_int(state_raw["dwell_neutral"]),
        dwell_bear=_tuple_int(state_raw["dwell_bear"]),
        markov_counts=_tuple_int(state_raw["markov_counts"]),
        markov_rows=_tuple_int(state_raw["markov_rows"]),
        markov_last_regime=_optional_int(state_raw.get("markov_last_regime")),
        markov_last_bar_index=_optional_int(state_raw.get("markov_last_bar_index")),
        external_ok_run=int(cast(int, state_raw["external_ok_run"])),
        external_bad_run=int(cast(int, state_raw["external_bad_run"])),
        external_latched=bool_field("external_latched"),
        external_last=int(cast(int, state_raw["external_last"])),
        avwap_pv=_optional_float(state_raw.get("avwap_pv")),
        avwap_weight=_optional_float(state_raw.get("avwap_weight")),
        avwap_p2v=_optional_float(state_raw.get("avwap_p2v")),
        avwap_on=bool_field("avwap_on"),
        avwap_valid_observations=int(cast(int, state_raw["avwap_valid_observations"])),
        avwap_age=int(cast(int, state_raw["avwap_age"])),
        previous_pivot_low=_pivot(state_raw.get("previous_pivot_low")),
        previous_pivot_high=_pivot(state_raw.get("previous_pivot_high")),
        short_retest_seen=bool_field("short_retest_seen"),
        short_retest_taken=bool_field("short_retest_taken"),
        long_retest_seen=bool_field("long_retest_seen"),
        long_retest_taken=bool_field("long_retest_taken"),
        pending_entry_side=Side(str(state_raw["pending_entry_side"])),
        pending_entry_setup=SetupFamily(str(state_raw["pending_entry_setup"])),
        pending_base_pivot_at_entry=_optional_float(state_raw.get("pending_base_pivot_at_entry")),
        active_entry_side=Side(str(state_raw["active_entry_side"])),
        active_entry_bar_index=_optional_int(state_raw.get("active_entry_bar_index")),
        active_entry_setup=SetupFamily(str(state_raw["active_entry_setup"])),
        active_base_pivot_at_entry=_optional_float(state_raw.get("active_base_pivot_at_entry")),
        equity_peak=_optional_float(state_raw.get("equity_peak")),
        equity_history=tuple(
            float(cast(float | int, item))
            for item in cast(list[object], state_raw["equity_history"])
        ),
        long_equity_peak=_optional_float(state_raw.get("long_equity_peak")),
        long_equity_history=tuple(
            float(cast(float | int, item))
            for item in cast(list[object], state_raw["long_equity_history"])
        ),
        short_equity_peak=_optional_float(state_raw.get("short_equity_peak")),
        short_equity_history=tuple(
            float(cast(float | int, item))
            for item in cast(list[object], state_raw["short_equity_history"])
        ),
        day_start_equity=_optional_float(state_raw.get("day_start_equity")),
        long_day_start_equity=_optional_float(state_raw.get("long_day_start_equity")),
        short_day_start_equity=_optional_float(state_raw.get("short_day_start_equity")),
        day_session=optional_date("day_session"),
        daily_halt_latched=bool_field("daily_halt_latched"),
        long_daily_halt_latched=bool_field("long_daily_halt_latched"),
        short_daily_halt_latched=bool_field("short_daily_halt_latched"),
        previous_position=Side(str(state_raw["previous_position"])),
        last_exit_bar_index=_optional_int(state_raw.get("last_exit_bar_index")),
        long_last_exit_bar_index=_optional_int(state_raw.get("long_last_exit_bar_index")),
        short_last_exit_bar_index=_optional_int(state_raw.get("short_last_exit_bar_index")),
        short_blocked_no_chase=int(cast(int, state_raw["short_blocked_no_chase"])),
        short_blocked_support=int(cast(int, state_raw["short_blocked_support"])),
        short_blocked_squeeze=int(cast(int, state_raw["short_blocked_squeeze"])),
        long_blocked_no_chase=int(cast(int, state_raw["long_blocked_no_chase"])),
        long_blocked_resistance=int(cast(int, state_raw["long_blocked_resistance"])),
        long_blocked_exhaustion=int(cast(int, state_raw["long_blocked_exhaustion"])),
        emitted_event_ids=tuple(
            str(item) for item in cast(list[object], state_raw["emitted_event_ids"])
        ),
    )

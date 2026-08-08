"""Typed, immutable vocabulary for the Five-Tool research-only vertical slice."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chronos.marketdata.bars import Bar, BarStatus
from chronos.research.five_tool.contract import (
    default_input_values,
    input_contract_digest,
    load_contract,
)

type InputValue = bool | int | float | str
type TraceValue = bool | int | float | str | None

_PINE_TIMESTAMP_FORMAT = "%d %b %Y %H:%M %z"
_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


class FiveToolInputError(ValueError):
    """An input would make a supposedly deterministic trace ambiguous."""


def _pine_timestamp_literal_milliseconds(value: str, name: str) -> int:
    """Normalize a frozen Pine ``timestamp(...)`` literal to native milliseconds."""

    try:
        parsed = datetime.strptime(value, _PINE_TIMESTAMP_FORMAT).astimezone(UTC)
    except ValueError as exc:
        raise FiveToolInputError(
            f"input {name!r} has an unsupported Pine timestamp literal: {value!r}"
        ) from exc
    delta = parsed - _UNIX_EPOCH_UTC
    return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000


def pine_timeframe_seconds(value: str) -> int:
    """Return Pine's nominal seconds for a supported time-based timeframe.

    Pine has no hour suffix: hourly resolutions are minute multipliers such as
    ``"60"`` or ``"240"``. Tick resolutions are valid Pine timeframe strings,
    but ``timeframe.in_seconds`` cannot order them against time-based chart bars,
    so this research engine rejects them before any bar is evaluated.
    """

    if not value or value != value.strip():
        raise FiveToolInputError(f"invalid Pine timeframe: {value!r}")
    unit = value[-1] if value[-1].isalpha() else ""
    multiplier_text = value[:-1] if unit else value
    if unit and unit not in {"T", "S", "D", "W", "M"}:
        raise FiveToolInputError(f"invalid Pine timeframe unit in {value!r}")
    if multiplier_text and (not multiplier_text.isascii() or not multiplier_text.isdigit()):
        raise FiveToolInputError(f"invalid Pine timeframe multiplier in {value!r}")
    multiplier = int(multiplier_text) if multiplier_text else 1

    if unit == "T":
        if multiplier not in {1, 10, 100, 1000}:
            raise FiveToolInputError(f"invalid Pine tick timeframe: {value!r}")
        raise FiveToolInputError(
            f"tick timeframe {value!r} cannot be ordered by timeframe.in_seconds"
        )
    if unit == "S":
        if multiplier not in {1, 5, 10, 15, 30, 45}:
            raise FiveToolInputError(f"invalid Pine seconds timeframe: {value!r}")
        return multiplier
    if unit == "":
        if not 1 <= multiplier <= 1440:
            raise FiveToolInputError(f"invalid Pine minutes timeframe: {value!r}")
        return multiplier * 60
    if unit == "D":
        if not 1 <= multiplier <= 365:
            raise FiveToolInputError(f"invalid Pine daily timeframe: {value!r}")
        return multiplier * 24 * 60 * 60
    if unit == "W":
        if not 1 <= multiplier <= 52:
            raise FiveToolInputError(f"invalid Pine weekly timeframe: {value!r}")
        return multiplier * 7 * 24 * 60 * 60
    if not 1 <= multiplier <= 12:
        raise FiveToolInputError(f"invalid Pine monthly timeframe: {value!r}")
    # Pine uses a nominal duration for calendar months when comparing timeframes.
    # Only relative ordering is consumed by this engine.
    return multiplier * 30 * 24 * 60 * 60


class Side(StrEnum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class SignalIntent(StrEnum):
    NONE = "none"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"


class SetupFamily(StrEnum):
    NONE = "none"
    LEGACY_FLIP = "legacy_flip"
    LEGACY_HIDDEN_DIVERGENCE = "legacy_hidden_divergence"
    LEGACY_REGULAR_DIVERGENCE = "legacy_regular_divergence"
    LEGACY_AVWAP_RECLAIM = "legacy_avwap_reclaim"
    LEADER_PULLBACK = "leader_pullback"
    BULL_RETEST = "bull_retest"
    BASE_BREAKOUT = "base_breakout"
    FAILED_AVWAP_RECLAIM = "failed_avwap_reclaim"
    BEAR_FLAG_BREAKDOWN = "bear_flag_breakdown"
    BEAR_RETEST = "bear_retest"


@dataclass(frozen=True, slots=True)
class FiveToolSettings:
    """Frozen input mapping plus research controls that Pine does not provide.

    ``history_start_utc`` is deliberately outside the 219 Pine inputs: it closes
    TradingView's chart-prefix ambiguity for expanding Markov and dwell state.
    """

    history_start_utc: datetime
    inputs: tuple[tuple[str, InputValue], ...]
    contract_digest: str
    exchange_timezone: str = "America/New_York"
    point_value: float = 1.0
    minimum_tick: float = 0.01

    def __post_init__(self) -> None:
        if self.history_start_utc.tzinfo is None or self.history_start_utc.utcoffset() is None:
            raise FiveToolInputError("history_start_utc must be timezone-aware")
        normalized = self.history_start_utc.astimezone(UTC)
        object.__setattr__(self, "history_start_utc", normalized)
        names = [name for name, _ in self.inputs]
        if len(names) != len(set(names)):
            raise FiveToolInputError("Five-Tool input names must be unique")
        contract = load_contract()
        expected_digest = input_contract_digest()
        if self.contract_digest != expected_digest:
            raise FiveToolInputError(
                "contract_digest does not match the source-bound Five-Tool contract"
            )
        expected_names = tuple(item.name for item in contract.inputs)
        if tuple(names) != expected_names:
            raise FiveToolInputError(
                "Five-Tool inputs must contain all 219 contract entries in source order"
            )
        for item, (_, value) in zip(contract.inputs, self.inputs, strict=True):
            if item.pine_type == "bool":
                valid_type = isinstance(value, bool)
            elif item.pine_type in {"int", "time"}:
                valid_type = isinstance(value, int) and not isinstance(value, bool)
            elif item.pine_type == "float":
                valid_type = isinstance(value, int | float) and not isinstance(value, bool)
            else:
                valid_type = isinstance(value, str)
            if not valid_type:
                raise FiveToolInputError(
                    f"input {item.name!r} has incompatible type {type(value).__name__}"
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise FiveToolInputError(f"input {item.name!r} must be finite")
            if item.options is not None and value not in item.options:
                raise FiveToolInputError(
                    f"input {item.name!r} must be one of {item.options!r}, got {value!r}"
                )
            if isinstance(value, int | float) and not isinstance(value, bool):
                minimum = item.minval.value if item.minval is not None else None
                maximum = item.maxval.value if item.maxval is not None else None
                if isinstance(minimum, int | float) and value < minimum:
                    raise FiveToolInputError(
                        f"input {item.name!r} must be >= {minimum}, got {value}"
                    )
                if isinstance(maximum, int | float) and value > maximum:
                    raise FiveToolInputError(
                        f"input {item.name!r} must be <= {maximum}, got {value}"
                    )
        pine_timeframe_seconds(self.text("htf_tf"))
        try:
            ZoneInfo(self.exchange_timezone)
        except ZoneInfoNotFoundError as exc:
            raise FiveToolInputError(
                f"unknown exchange timezone: {self.exchange_timezone}"
            ) from exc
        if not math.isfinite(self.point_value) or self.point_value <= 0.0:
            raise FiveToolInputError("point_value must be finite and positive")
        if not math.isfinite(self.minimum_tick) or self.minimum_tick <= 0.0:
            raise FiveToolInputError("minimum_tick must be finite and positive")

    @classmethod
    def defaults(
        cls,
        *,
        history_start_utc: datetime,
        overrides: Mapping[str, InputValue] | None = None,
        exchange_timezone: str = "America/New_York",
        point_value: float = 1.0,
        minimum_tick: float = 0.01,
    ) -> FiveToolSettings:
        values = default_input_values()
        contract = load_contract()
        for item in contract.inputs:
            if item.pine_type != "time":
                continue
            literal = values[item.name]
            if not isinstance(literal, str):
                raise FiveToolInputError(
                    f"input {item.name!r} has no normalized Pine timestamp literal"
                )
            values[item.name] = _pine_timestamp_literal_milliseconds(literal, item.name)
        for name, value in (overrides or {}).items():
            if name not in values:
                raise FiveToolInputError(f"unknown Five-Tool input override: {name}")
            expected = values[name]
            if isinstance(expected, bool):
                valid_type = isinstance(value, bool)
            elif isinstance(expected, int):
                valid_type = isinstance(value, int) and not isinstance(value, bool)
            elif isinstance(expected, float):
                valid_type = isinstance(value, int | float) and not isinstance(value, bool)
            else:
                valid_type = isinstance(value, str)
            if not valid_type:
                raise FiveToolInputError(
                    f"override {name!r} has incompatible type {type(value).__name__}"
                )
            values[name] = float(value) if isinstance(expected, float) else value
        return cls(
            history_start_utc=history_start_utc,
            inputs=tuple(values.items()),
            contract_digest=input_contract_digest(),
            exchange_timezone=exchange_timezone,
            point_value=point_value,
            minimum_tick=minimum_tick,
        )

    @property
    def digest(self) -> str:
        payload = {
            "history_start_utc": self.history_start_utc.isoformat(),
            "inputs": self.inputs,
            "contract_digest": self.contract_digest,
            "exchange_timezone": self.exchange_timezone,
            "point_value": self.point_value,
            "minimum_tick": self.minimum_tick,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _raw(self, name: str) -> InputValue:
        for key, value in self.inputs:
            if key == name:
                return value
        raise FiveToolInputError(f"missing Five-Tool input: {name}")

    def boolean(self, name: str) -> bool:
        value = self._raw(name)
        if not isinstance(value, bool):
            raise FiveToolInputError(f"input {name!r} is not boolean")
        return value

    def integer(self, name: str) -> int:
        value = self._raw(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise FiveToolInputError(f"input {name!r} is not an integer")
        return value

    def number(self, name: str) -> float:
        value = self._raw(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise FiveToolInputError(f"input {name!r} is not numeric")
        result = float(value)
        if not math.isfinite(result):
            raise FiveToolInputError(f"input {name!r} must be finite")
        return result

    def text(self, name: str) -> str:
        value = self._raw(name)
        if not isinstance(value, str):
            raise FiveToolInputError(f"input {name!r} is not text")
        return value


@dataclass(frozen=True, slots=True)
class CompanionValue:
    """One causally aligned companion value with auditable source identity."""

    value: float
    source_timestamp_utc: datetime
    source_sequence_id: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.value):
            raise FiveToolInputError("companion value must be finite")
        if (
            self.source_timestamp_utc.tzinfo is None
            or self.source_timestamp_utc.utcoffset() is None
        ):
            raise FiveToolInputError("companion timestamp must be timezone-aware")
        object.__setattr__(self, "source_timestamp_utc", self.source_timestamp_utc.astimezone(UTC))
        if not self.source_sequence_id:
            raise FiveToolInputError("companion source_sequence_id is required")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Read-only account context; the signal engine never mutates positions or cash."""

    equity: float = 100_000.0
    position: Side = Side.FLAT
    average_entry_price: float | None = None
    entry_bar_index: int | None = None
    entry_setup: SetupFamily = SetupFamily.NONE
    base_pivot_at_entry: float | None = None
    long_virtual_equity: float | None = None
    short_virtual_equity: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.equity):
            raise FiveToolInputError("equity must be finite")
        if self.average_entry_price is not None and not math.isfinite(self.average_entry_price):
            raise FiveToolInputError("average_entry_price must be finite when supplied")
        if self.entry_bar_index is not None and self.entry_bar_index < 0:
            raise FiveToolInputError("entry_bar_index cannot be negative")
        if self.base_pivot_at_entry is not None and not math.isfinite(self.base_pivot_at_entry):
            raise FiveToolInputError("base_pivot_at_entry must be finite when supplied")
        for name, value in (
            ("long_virtual_equity", self.long_virtual_equity),
            ("short_virtual_equity", self.short_virtual_equity),
        ):
            if value is not None and not math.isfinite(value):
                raise FiveToolInputError(f"{name} must be finite when supplied")


@dataclass(frozen=True, slots=True)
class FiveToolBarInput:
    """All information visible when one confirmed primary bar closes."""

    primary: Bar
    benchmark: CompanionValue | None
    htf_close: CompanionValue | None = None
    htf_ema: CompanionValue | None = None
    external_regime: float | None = None
    external_strength: float | None = None
    long_plus_in_session: bool | None = None
    short_plus_in_session: bool | None = None
    account: AccountSnapshot = AccountSnapshot()

    def __post_init__(self) -> None:
        if self.primary.status is not BarStatus.CLOSED:
            raise FiveToolInputError("Five-Tool consumes closed primary bars only")
        prices = (
            self.primary.open,
            self.primary.high,
            self.primary.low,
            self.primary.close,
            self.primary.volume,
        )
        if not all(math.isfinite(value) for value in prices):
            raise FiveToolInputError("primary OHLCV must be finite")
        if self.primary.volume < 0.0:
            raise FiveToolInputError("primary volume cannot be negative")
        if self.primary.low > min(self.primary.open, self.primary.close):
            raise FiveToolInputError("primary low does not contain open and close")
        if self.primary.high < max(self.primary.open, self.primary.close):
            raise FiveToolInputError("primary high does not contain open and close")
        for name, value in (
            ("external_regime", self.external_regime),
            ("external_strength", self.external_strength),
        ):
            if value is not None and not math.isfinite(value):
                raise FiveToolInputError(f"{name} must be finite when supplied")
        if (
            self.benchmark is not None
            and self.benchmark.source_timestamp_utc > self.primary.timestamp_utc
        ):
            raise FiveToolInputError("benchmark alignment accesses a future bar")
        for companion in (self.htf_close, self.htf_ema):
            if (
                companion is not None
                and companion.source_timestamp_utc >= self.primary.timestamp_utc
            ):
                raise FiveToolInputError("HTF alignment must use a prior completed bar")
        if (
            self.htf_close is not None
            and self.htf_ema is not None
            and (
                self.htf_close.source_timestamp_utc != self.htf_ema.source_timestamp_utc
                or self.htf_close.source_sequence_id != self.htf_ema.source_sequence_id
            )
        ):
            raise FiveToolInputError("HTF close and EMA must identify the same source bar")


@dataclass(frozen=True, slots=True)
class SignalEvent:
    event_id: str
    kind: str
    timestamp_utc: datetime
    side: Side
    setup: SetupFamily


@dataclass(frozen=True, slots=True)
class FiveToolTrace:
    """Inspectible decision evidence for exactly one primary bar."""

    bar_index: int
    timestamp_utc: datetime
    primary_sequence_id: str
    benchmark_source_id: str | None
    htf_source_id: str | None
    history_start_utc: datetime
    features: tuple[tuple[str, TraceValue], ...]
    gates: tuple[tuple[str, bool], ...]
    warmup_blockers: tuple[str, ...]
    long_setup: SetupFamily
    short_setup: SetupFamily
    intent: SignalIntent
    events: tuple[SignalEvent, ...]
    state_digest: str

    def feature(self, name: str) -> TraceValue:
        for key, value in self.features:
            if key == name:
                return value
        raise KeyError(name)

    def gate(self, name: str) -> bool:
        for key, value in self.gates:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class FiveToolState:
    """Serializable causal state needed to resume without replaying hidden history."""

    settings_digest: str
    history_start_utc: datetime
    observations: tuple[FiveToolBarInput, ...] = ()
    candidate_regimes: tuple[int | None, ...] = ()
    confirmed_core: int | None = None
    last_regime: int = 0
    have_regime: bool = False
    previous_selected_regime: int | None = None
    internal_bars_in_regime: int = 0
    active_bars_in_regime: int = 0
    dwell_bull: tuple[int, ...] = ()
    dwell_neutral: tuple[int, ...] = ()
    dwell_bear: tuple[int, ...] = ()
    markov_counts: tuple[int, ...] = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    markov_rows: tuple[int, ...] = (0, 0, 0)
    markov_last_regime: int | None = None
    markov_last_bar_index: int | None = None
    external_ok_run: int = 0
    external_bad_run: int = 0
    external_latched: bool = False
    external_last: int = 0
    avwap_pv: float | None = None
    avwap_weight: float | None = None
    avwap_p2v: float | None = None
    avwap_on: bool = False
    avwap_valid_observations: int = 0
    avwap_age: int = 0
    previous_pivot_low: tuple[int, float, float] | None = None
    previous_pivot_high: tuple[int, float, float] | None = None
    short_retest_seen: bool = False
    short_retest_taken: bool = False
    long_retest_seen: bool = False
    long_retest_taken: bool = False
    pending_entry_side: Side = Side.FLAT
    pending_entry_setup: SetupFamily = SetupFamily.NONE
    pending_base_pivot_at_entry: float | None = None
    active_entry_side: Side = Side.FLAT
    active_entry_bar_index: int | None = None
    active_entry_setup: SetupFamily = SetupFamily.NONE
    active_base_pivot_at_entry: float | None = None
    equity_peak: float | None = None
    equity_history: tuple[float, ...] = ()
    long_equity_peak: float | None = None
    long_equity_history: tuple[float, ...] = ()
    short_equity_peak: float | None = None
    short_equity_history: tuple[float, ...] = ()
    day_start_equity: float | None = None
    long_day_start_equity: float | None = None
    short_day_start_equity: float | None = None
    day_session: date | None = None
    daily_halt_latched: bool = False
    long_daily_halt_latched: bool = False
    short_daily_halt_latched: bool = False
    previous_position: Side = Side.FLAT
    last_exit_bar_index: int | None = None
    long_last_exit_bar_index: int | None = None
    short_last_exit_bar_index: int | None = None
    short_blocked_no_chase: int = 0
    short_blocked_support: int = 0
    short_blocked_squeeze: int = 0
    long_blocked_no_chase: int = 0
    long_blocked_resistance: int = 0
    long_blocked_exhaustion: int = 0
    emitted_event_ids: tuple[str, ...] = ()

    @classmethod
    def initial(cls, settings: FiveToolSettings) -> FiveToolState:
        return cls(
            settings_digest=settings.digest,
            history_start_utc=settings.history_start_utc,
        )

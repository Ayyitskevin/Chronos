"""Typed, immutable vocabulary for the Five-Tool research-only vertical slice."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from chronos.marketdata.bars import Bar, BarStatus
from chronos.research.five_tool.contract import default_input_values, input_contract_digest

type InputValue = bool | int | float | str
type TraceValue = bool | int | float | str | None


class FiveToolInputError(ValueError):
    """An input would make a supposedly deterministic trace ambiguous."""


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
        if not self.contract_digest:
            raise FiveToolInputError("contract_digest is required")
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
    long_virtual_equity: float | None = None
    short_virtual_equity: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.equity):
            raise FiveToolInputError("equity must be finite")
        if self.average_entry_price is not None and not math.isfinite(self.average_entry_price):
            raise FiveToolInputError("average_entry_price must be finite when supplied")
        if self.entry_bar_index is not None and self.entry_bar_index < 0:
            raise FiveToolInputError("entry_bar_index cannot be negative")
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
    avwap_age: int = 0
    previous_pivot_low: tuple[int, float, float] | None = None
    previous_pivot_high: tuple[int, float, float] | None = None
    short_retest_seen: bool = False
    short_retest_taken: bool = False
    long_retest_seen: bool = False
    long_retest_taken: bool = False
    pending_entry_side: Side = Side.FLAT
    pending_entry_setup: SetupFamily = SetupFamily.NONE
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
    emitted_event_ids: tuple[str, ...] = ()

    @classmethod
    def initial(cls, settings: FiveToolSettings) -> FiveToolState:
        return cls(
            settings_digest=settings.digest,
            history_start_utc=settings.history_start_utc,
        )

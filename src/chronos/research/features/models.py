"""Typed, immutable vocabulary for the Five-Tool pairing feature plane.

This package is research evidence only.  It does not originate orders, size
positions, or attach fields to ``AITradeDecision``.  Pairings are vetoes on an
already-emitted Five-Tool opportunity stream; they do not replace the regime
engine or mutate the frozen Pine identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from chronos.research.five_tool.models import SignalIntent

type FeatureValue = bool | int | float | str | None

FEATURE_POLICY_SCHEMA = "chronos-five-tool-feature-policy-v1"
PAIRING_CAMPAIGN_SCHEMA = "chronos-five-tool-pairing-campaign-v1"
GOLD_PAIRING_CAMPAIGN_SCHEMA = "chronos-five-tool-gold-pairing-campaign-v1"
COMPANION_CATALOG_SCHEMA = "chronos-five-tool-pairing-companion-catalog-v1"
GOLD_PAIRING_SYMBOL = "GLD"


class FeatureInputError(ValueError):
    """A feature, companion, or pairing join would be ambiguous or non-causal."""


class VetoStatus(StrEnum):
    ALLOW = "allow"
    VETO = "veto"
    WARMUP = "warmup"
    MISSING_COMPANION = "missing_companion"


class FeatureFamily(StrEnum):
    TAIL_RISK = "tail_risk"
    RVOL = "rvol"
    IV_REGIME = "iv_regime"
    BREADTH = "breadth"
    USD_REGIME = "usd_regime"


GOLD_INERT_FAMILIES = (FeatureFamily.IV_REGIME, FeatureFamily.BREADTH)


class TailState(StrEnum):
    ORDINARY = "ORDINARY"
    ELEVATED = "ELEVATED"
    FAT_TAILED = "FAT_TAILED"


class IvState(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    STRESS = "STRESS"


class UsdState(StrEnum):
    FALLING = "FALLING"
    FLAT = "FLAT"
    RISING = "RISING"


def _require_aware(timestamp: datetime, name: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise FeatureInputError(f"{name} must be timezone-aware")
    return timestamp.astimezone(UTC)


def canonical_digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """One family's closed-bar exports for exactly one primary bar."""

    family: FeatureFamily
    timestamp_utc: datetime
    primary_sequence_id: str
    values: tuple[tuple[str, FeatureValue], ...]
    warmup: bool
    missing_required: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timestamp_utc", _require_aware(self.timestamp_utc, "timestamp_utc")
        )
        if not self.primary_sequence_id:
            raise FeatureInputError("primary_sequence_id is required")
        names = [name for name, _ in self.values]
        if len(names) != len(set(names)):
            raise FeatureInputError("feature value names must be unique")
        for name, value in self.values:
            if isinstance(value, float) and not math.isfinite(value):
                raise FeatureInputError(f"feature {name!r} must be finite when numeric")

    def value(self, name: str) -> FeatureValue:
        for key, item in self.values:
            if key == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class VetoDecision:
    """Research-only gate on a Five-Tool intent.  Not an order."""

    status: VetoStatus
    original_intent: SignalIntent
    filtered_intent: SignalIntent
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.original_intent not in {
            SignalIntent.NONE,
            SignalIntent.ENTER_LONG,
            SignalIntent.ENTER_SHORT,
            SignalIntent.EXIT_LONG,
            SignalIntent.EXIT_SHORT,
        }:
            raise FeatureInputError(f"unsupported original intent: {self.original_intent}")
        if self.filtered_intent not in {
            SignalIntent.NONE,
            SignalIntent.ENTER_LONG,
            SignalIntent.ENTER_SHORT,
            SignalIntent.EXIT_LONG,
            SignalIntent.EXIT_SHORT,
        }:
            raise FeatureInputError(f"unsupported filtered intent: {self.filtered_intent}")
        if (
            self.original_intent in {SignalIntent.EXIT_LONG, SignalIntent.EXIT_SHORT}
            and self.filtered_intent is not self.original_intent
        ):
            raise FeatureInputError("pairing vetoes may not mask exit intents")
        if self.original_intent in {SignalIntent.ENTER_LONG, SignalIntent.ENTER_SHORT} and (
            self.filtered_intent not in {self.original_intent, SignalIntent.NONE}
        ):
            raise FeatureInputError("pairing vetoes may only replace ENTER with NONE")
        if self.status is VetoStatus.ALLOW and self.filtered_intent is not self.original_intent:
            raise FeatureInputError("ALLOW must preserve the original intent")
        if (
            self.status is not VetoStatus.ALLOW
            and self.original_intent in {SignalIntent.ENTER_LONG, SignalIntent.ENTER_SHORT}
            and self.filtered_intent is not SignalIntent.NONE
        ):
            raise FeatureInputError("a blocking status must mask ENTER to NONE")


@dataclass(frozen=True, slots=True)
class FeaturePolicy:
    """Frozen pairing thresholds.  Changing any field starts a new identity."""

    schema_version: str = FEATURE_POLICY_SCHEMA
    enable_tail_risk: bool = True
    enable_rvol: bool = True
    enable_iv_regime: bool = True
    enable_breadth: bool = True
    enable_usd_regime: bool = False
    tail_window: int = 100
    tail_atr_length: int = 14
    tail_kurtosis_elevated: float = 1.0
    tail_kurtosis_fat: float = 3.0
    tail_skew_fat: float = -1.0
    rvol_lookback: int = 20
    rvol_min_ratio: float = 1.5
    rvol_min_avg_dollar_vol_millions: float = 20.0
    rvol_min_gap_atr: float = 0.0
    rvol_tod_days: int = 20
    rvol_tod_max_bars: int = 400
    rvol_tod_elevated: float = 1.5
    rvol_tod_min_side_n: int = 30
    rvol_tod_tilt_margin: float = 0.02
    iv_percentile_length: int = 252
    iv_cut_low: float = 25.0
    iv_cut_elevated: float = 75.0
    iv_cut_stress: float = 90.0
    iv_crush_exit: float = 70.0
    breadth_slope_lookback: int = 30
    usd_slope_lookback: int = 30
    companion_catalog_status: str = "pending_certified_dataset"
    tradable_symbols: tuple[str, ...] = ("GLD", "IWM", "QQQ")
    companion_only_symbols: tuple[str, ...] = ("RSP", "SPY", "VIX", "VIX3M")
    gold_inert_families: tuple[FeatureFamily, ...] = GOLD_INERT_FAMILIES

    def __post_init__(self) -> None:
        if self.schema_version != FEATURE_POLICY_SCHEMA:
            raise FeatureInputError(f"unsupported feature-policy schema: {self.schema_version}")
        if self.companion_catalog_status != "pending_certified_dataset":
            raise FeatureInputError(
                "companion catalog stays pending until owner-certified bytes exist"
            )
        if self.tradable_symbols != ("GLD", "IWM", "QQQ"):
            raise FeatureInputError("tradable book is locked to GLD, IWM, and QQQ")
        if self.companion_only_symbols != ("RSP", "SPY", "VIX", "VIX3M"):
            raise FeatureInputError("companion-only set is locked to RSP, SPY, VIX, VIX3M")
        if any(name in self.tradable_symbols for name in ("QQQM", "SPY")):
            raise FeatureInputError("QQQM and SPY are not tradable in this book")
        if self.gold_inert_families != GOLD_INERT_FAMILIES:
            raise FeatureInputError(
                "GLD pairing identity keeps iv_regime and breadth inert; "
                "changing that set is a new identity"
            )
        for name, integer in (
            ("tail_window", self.tail_window),
            ("tail_atr_length", self.tail_atr_length),
            ("rvol_lookback", self.rvol_lookback),
            ("rvol_tod_days", self.rvol_tod_days),
            ("rvol_tod_max_bars", self.rvol_tod_max_bars),
            ("rvol_tod_min_side_n", self.rvol_tod_min_side_n),
            ("iv_percentile_length", self.iv_percentile_length),
            ("breadth_slope_lookback", self.breadth_slope_lookback),
            ("usd_slope_lookback", self.usd_slope_lookback),
        ):
            if isinstance(integer, bool) or integer <= 0:
                raise FeatureInputError(f"{name} must be a positive integer")
        for name, number in (
            ("tail_kurtosis_elevated", self.tail_kurtosis_elevated),
            ("tail_kurtosis_fat", self.tail_kurtosis_fat),
            ("rvol_min_ratio", self.rvol_min_ratio),
            ("rvol_min_avg_dollar_vol_millions", self.rvol_min_avg_dollar_vol_millions),
            ("rvol_min_gap_atr", self.rvol_min_gap_atr),
            ("rvol_tod_elevated", self.rvol_tod_elevated),
            ("rvol_tod_tilt_margin", self.rvol_tod_tilt_margin),
            ("iv_cut_low", self.iv_cut_low),
            ("iv_cut_elevated", self.iv_cut_elevated),
            ("iv_cut_stress", self.iv_cut_stress),
            ("iv_crush_exit", self.iv_crush_exit),
        ):
            if isinstance(number, bool) or not math.isfinite(number) or number < 0.0:
                raise FeatureInputError(f"{name} must be a finite non-negative number")
        if self.tail_skew_fat >= 0.0 or not math.isfinite(self.tail_skew_fat):
            raise FeatureInputError("tail_skew_fat must be a finite negative number")
        if not (self.iv_cut_low < self.iv_cut_elevated < self.iv_cut_stress):
            raise FeatureInputError("IV percentile cuts must be strictly increasing")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "enable_tail_risk": self.enable_tail_risk,
                "enable_rvol": self.enable_rvol,
                "enable_iv_regime": self.enable_iv_regime,
                "enable_breadth": self.enable_breadth,
                "enable_usd_regime": self.enable_usd_regime,
                "tail_window": self.tail_window,
                "tail_atr_length": self.tail_atr_length,
                "tail_kurtosis_elevated": self.tail_kurtosis_elevated,
                "tail_kurtosis_fat": self.tail_kurtosis_fat,
                "tail_skew_fat": self.tail_skew_fat,
                "rvol_lookback": self.rvol_lookback,
                "rvol_min_ratio": self.rvol_min_ratio,
                "rvol_min_avg_dollar_vol_millions": self.rvol_min_avg_dollar_vol_millions,
                "rvol_min_gap_atr": self.rvol_min_gap_atr,
                "rvol_tod_days": self.rvol_tod_days,
                "rvol_tod_max_bars": self.rvol_tod_max_bars,
                "rvol_tod_elevated": self.rvol_tod_elevated,
                "rvol_tod_min_side_n": self.rvol_tod_min_side_n,
                "rvol_tod_tilt_margin": self.rvol_tod_tilt_margin,
                "iv_percentile_length": self.iv_percentile_length,
                "iv_cut_low": self.iv_cut_low,
                "iv_cut_elevated": self.iv_cut_elevated,
                "iv_cut_stress": self.iv_cut_stress,
                "iv_crush_exit": self.iv_crush_exit,
                "breadth_slope_lookback": self.breadth_slope_lookback,
                "usd_slope_lookback": self.usd_slope_lookback,
                "companion_catalog_status": self.companion_catalog_status,
                "tradable_symbols": self.tradable_symbols,
                "companion_only_symbols": self.companion_only_symbols,
                "gold_inert_families": [family.value for family in self.gold_inert_families],
            }
        )

    def enabled_families(self, symbol: str) -> tuple[FeatureFamily, ...]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise FeatureInputError("pairing symbol is required")
        selected: list[FeatureFamily] = []
        if self.enable_tail_risk:
            selected.append(FeatureFamily.TAIL_RISK)
        if self.enable_rvol:
            selected.append(FeatureFamily.RVOL)
        if self.enable_iv_regime:
            selected.append(FeatureFamily.IV_REGIME)
        if self.enable_breadth:
            selected.append(FeatureFamily.BREADTH)
        if normalized == GOLD_PAIRING_SYMBOL:
            inert = set(self.gold_inert_families)
            selected = [family for family in selected if family not in inert]
            if self.enable_usd_regime:
                selected.append(FeatureFamily.USD_REGIME)
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class CompanionCatalogDeclaration:
    """Blocked companion-data contract.  Does not open or certify bytes."""

    schema_version: str = COMPANION_CATALOG_SCHEMA
    status: str = "pending_certified_dataset"
    required_symbols: tuple[str, ...] = ("VIX", "VIX3M", "RSP", "SPY", "QQQ")
    optional_symbols: tuple[str, ...] = ("TICK", "ADD", "VOLD")
    dataset_id: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != COMPANION_CATALOG_SCHEMA:
            raise FeatureInputError(f"unsupported companion-catalog schema: {self.schema_version}")
        if self.status != "pending_certified_dataset":
            raise FeatureInputError("companion catalog cannot be marked ready in this slice")
        if self.dataset_id is not None or self.sha256 is not None:
            raise FeatureInputError(
                "companion catalog identities remain unset until owner certification"
            )


@dataclass(frozen=True, slots=True)
class PairingFrame:
    """One primary bar's immutable Five-Tool trace plus sidecar snapshots."""

    timestamp_utc: datetime
    primary_sequence_id: str
    original_intent: SignalIntent
    snapshots: tuple[FeatureSnapshot, ...]
    decision: VetoDecision

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timestamp_utc", _require_aware(self.timestamp_utc, "timestamp_utc")
        )
        if self.original_intent is not self.decision.original_intent:
            raise FeatureInputError("frame intent does not match the veto decision")
        families = [item.family for item in self.snapshots]
        if len(families) != len(set(families)):
            raise FeatureInputError("pairing frame snapshots must be one per family")
        for snapshot in self.snapshots:
            if snapshot.timestamp_utc != self.timestamp_utc:
                raise FeatureInputError("snapshot timestamp drifted from the pairing frame")
            if snapshot.primary_sequence_id != self.primary_sequence_id:
                raise FeatureInputError("snapshot primary identity drifted from the pairing frame")


def snapshot_mapping(snapshot: FeatureSnapshot) -> Mapping[str, FeatureValue]:
    return dict(snapshot.values)

"""Strict TradingView trace fixtures and causal parity comparison.

This module is deliberately research-plane only.  It does not call TradingView,
place orders, or silently reshape exports.  An owner-provided export must first
be normalized to the explicit CSV contract below and accompanied by metadata
that pins the source and every input used to produce it.

Internal specification fixtures exercise the importer and comparator, but they
are not evidence of TradingView parity.  ``ComparisonReport.parity_status`` is
therefore ``UNVERIFIED`` for every internal-spec reference, even on an exact
match.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = "chronos.five_tool_tradingview_trace.v1"
PINNED_CATALOG_NUMBER = "00"
PINNED_PINE_SHA256 = "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f"
PINNED_PINE_INPUT_COUNT = 219

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NULL_TOKENS = frozenset({"", "na", "nan", "null"})

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class FixtureSchemaError(ValueError):
    """The fixture is incomplete, unpinned, non-causal, or otherwise unsafe."""


class FixtureProvenance(StrEnum):
    """Origin of the reference values."""

    GENUINE = "genuine"
    INTERNAL_SPEC = "internal_spec"


class Regime(StrEnum):
    BEAR = "bear"
    NEUTRAL = "neutral"
    BULL = "bull"


class EntryDecision(StrEnum):
    NONE = "none"
    LONG = "long"
    SHORT = "short"


class ExitDecision(StrEnum):
    NONE = "none"
    LONG = "long"
    SHORT = "short"
    ALL = "all"


class PositionSide(StrEnum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class ParityStatus(StrEnum):
    """TradingView-level status, not merely comparison success."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "provenance",
        "catalog_number",
        "pine_sha256",
        "symbol",
        "timeframe",
        "chart_timezone",
        "session",
        "timestamp_semantics",
        "data_source",
        "exported_at_utc",
        "input_count",
        "input_config",
        "input_config_sha256",
        "trace_sha256",
        "row_count",
    }
)


def _canonical_json_sha256(value: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_aware_timestamp(raw: str, field_name: str) -> datetime:
    cleaned = raw.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as error:
        raise FixtureSchemaError(f"{field_name} must be an ISO-8601 timestamp: {raw!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FixtureSchemaError(f"{field_name} must include an explicit UTC offset: {raw!r}")
    return parsed.astimezone(UTC)


def _require_string(document: Mapping[str, object], key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value.strip():
        raise FixtureSchemaError(f"metadata.{key} must be a non-empty string")
    return value.strip()


def _require_nonnegative_int(document: Mapping[str, object], key: str) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FixtureSchemaError(f"metadata.{key} must be a non-negative integer")
    return value


def _validate_json_value(value: object, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FixtureSchemaError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_validate_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise FixtureSchemaError(f"{path} keys must be non-empty strings")
            result[key] = _validate_json_value(item, f"{path}.{key}")
        return result
    raise FixtureSchemaError(f"{path} contains unsupported value type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class FixtureMetadata:
    """Strict provenance and identity for one normalized trace CSV."""

    schema_version: str
    provenance: FixtureProvenance
    catalog_number: str
    pine_sha256: str
    symbol: str
    timeframe: str
    chart_timezone: str
    session: str
    timestamp_semantics: str
    data_source: str
    exported_at_utc: datetime
    input_count: int
    input_config: Mapping[str, JsonValue]
    input_config_sha256: str
    trace_sha256: str
    row_count: int

    @classmethod
    def from_mapping(cls, document: Mapping[str, object]) -> FixtureMetadata:
        present = frozenset(document)
        if present != _METADATA_KEYS:
            missing = sorted(_METADATA_KEYS - present)
            unknown = sorted(present - _METADATA_KEYS)
            raise FixtureSchemaError(
                f"metadata keys do not match schema; missing={missing}, unknown={unknown}"
            )

        schema_version = _require_string(document, "schema_version")
        if schema_version != SCHEMA_VERSION:
            raise FixtureSchemaError(
                f"unsupported metadata schema {schema_version!r}; expected {SCHEMA_VERSION!r}"
            )
        try:
            provenance = FixtureProvenance(_require_string(document, "provenance"))
        except ValueError as error:
            raise FixtureSchemaError(
                "metadata.provenance must be 'genuine' or 'internal_spec'"
            ) from error

        catalog_number = _require_string(document, "catalog_number")
        if catalog_number != PINNED_CATALOG_NUMBER:
            raise FixtureSchemaError(
                "catalog_number must be pinned to "
                f"{PINNED_CATALOG_NUMBER!r}, got {catalog_number!r}"
            )
        pine_sha256 = _require_string(document, "pine_sha256").lower()
        if pine_sha256 != PINNED_PINE_SHA256:
            raise FixtureSchemaError(
                f"pine_sha256 does not match pinned catalog 00 source: {pine_sha256!r}"
            )

        chart_timezone = _require_string(document, "chart_timezone")
        try:
            ZoneInfo(chart_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise FixtureSchemaError(
                f"metadata.chart_timezone is not an IANA timezone: {chart_timezone!r}"
            ) from error

        timestamp_semantics = _require_string(document, "timestamp_semantics")
        if timestamp_semantics != "bar_close":
            raise FixtureSchemaError("metadata.timestamp_semantics must be 'bar_close'")

        input_count = _require_nonnegative_int(document, "input_count")
        raw_config = document["input_config"]
        if not isinstance(raw_config, dict) or not raw_config:
            raise FixtureSchemaError("metadata.input_config must be a non-empty object")
        validated = _validate_json_value(raw_config, "metadata.input_config")
        if not isinstance(validated, dict):  # pragma: no cover - guarded above
            raise FixtureSchemaError("metadata.input_config must be an object")
        if input_count != len(validated):
            raise FixtureSchemaError(
                "metadata.input_count must equal the number of complete input_config entries"
            )
        if provenance is FixtureProvenance.GENUINE and input_count != PINNED_PINE_INPUT_COUNT:
            raise FixtureSchemaError(
                f"genuine catalog 00 exports require all {PINNED_PINE_INPUT_COUNT} inputs; "
                f"got {input_count}"
            )

        input_config_sha256 = _require_string(document, "input_config_sha256").lower()
        if not _SHA256_RE.fullmatch(input_config_sha256):
            raise FixtureSchemaError("metadata.input_config_sha256 must be lowercase SHA-256")
        actual_config_sha256 = _canonical_json_sha256(validated)
        if input_config_sha256 != actual_config_sha256:
            raise FixtureSchemaError(
                "metadata.input_config_sha256 does not match canonical input_config"
            )

        trace_sha256 = _require_string(document, "trace_sha256").lower()
        if not _SHA256_RE.fullmatch(trace_sha256):
            raise FixtureSchemaError("metadata.trace_sha256 must be lowercase SHA-256")

        row_count = _require_nonnegative_int(document, "row_count")
        if row_count == 0:
            raise FixtureSchemaError("metadata.row_count must be positive")

        return cls(
            schema_version=schema_version,
            provenance=provenance,
            catalog_number=catalog_number,
            pine_sha256=pine_sha256,
            symbol=_require_string(document, "symbol").upper(),
            timeframe=_require_string(document, "timeframe"),
            chart_timezone=chart_timezone,
            session=_require_string(document, "session"),
            timestamp_semantics=timestamp_semantics,
            data_source=_require_string(document, "data_source"),
            exported_at_utc=_parse_aware_timestamp(
                _require_string(document, "exported_at_utc"), "metadata.exported_at_utc"
            ),
            input_count=input_count,
            input_config=validated,
            input_config_sha256=input_config_sha256,
            trace_sha256=trace_sha256,
            row_count=row_count,
        )


@dataclass(frozen=True, slots=True)
class TraceRow:
    """One normalized, closed-bar Five-Tool trace observation."""

    timestamp_utc: datetime
    source_timestamp: str
    regime: Regime | None
    regime_flip: bool
    entry_decision: EntryDecision
    exit_decision: ExitDecision
    position_side: PositionSide
    regime_z: float | None
    strength: float | None
    chop_risk: int
    extension: bool
    mansfield: float | None
    avwap: float | None
    long_score: float | None
    short_score: float | None
    equity_dd_pct: float | None
    daily_dd_pct: float | None
    atr_pct: float | None
    markov_transitions: int
    regime_pstay_pct: float | None
    short_review_ok: bool
    short_failed_reclaim: bool
    short_bear_flag_break: bool
    short_bear_flip_retest: bool
    short_sector_laggard: bool
    short_plus_score: float | None
    short_plus_gate: bool
    short_plus_agrade: bool
    short_plus_risk_mult: float | None
    short_plus_score_boost: float | None
    short_no_chase_block: bool
    short_support_block: bool
    short_squeeze_block: bool
    short_struct_stop: float | None
    short_stop_pct: float | None
    short_blocked_no_chase: int
    short_blocked_support: int
    short_blocked_squeeze: int
    long_review_ok: bool
    long_blocked_no_chase: int
    long_blocked_resistance: int
    long_blocked_exhaustion: int
    long_struct_stop: float | None
    long_stop_pct: float | None
    long_virtual_equity: float | None
    short_virtual_equity: float | None
    avwap_force_reset: bool
    avwap_age_bars: int
    avwap_display_hidden: bool

    @property
    def gates(self) -> tuple[str, ...]:
        """True event/gate names in stable order for mismatch diagnostics."""

        names = (
            "regime_flip",
            "extension",
            "short_review_ok",
            "short_failed_reclaim",
            "short_bear_flag_break",
            "short_bear_flip_retest",
            "short_sector_laggard",
            "short_plus_gate",
            "short_plus_agrade",
            "short_no_chase_block",
            "short_support_block",
            "short_squeeze_block",
            "long_review_ok",
            "avwap_force_reset",
            "avwap_display_hidden",
        )
        active = [name for name in names if bool(getattr(self, name))]
        if self.entry_decision is not EntryDecision.NONE:
            active.append(f"entry_{self.entry_decision.value}")
        if self.exit_decision is not ExitDecision.NONE:
            active.append(f"exit_{self.exit_decision.value}")
        return tuple(active)

    @property
    def state_digest(self) -> str:
        """Stable digest of every typed state field except the source spelling."""

        payload: dict[str, JsonValue] = {}
        for definition in fields(self):
            if definition.name == "source_timestamp":
                continue
            value = getattr(self, definition.name)
            if isinstance(value, datetime):
                payload[definition.name] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            elif isinstance(value, StrEnum):
                payload[definition.name] = value.value
            else:
                payload[definition.name] = value
        return _canonical_json_sha256(payload)


CSV_COLUMNS = (
    "timestamp",
    "regime",
    "regime_flip",
    "entry_decision",
    "exit_decision",
    "position_side",
    "regime_z",
    "strength",
    "chop_risk",
    "extension",
    "mansfield",
    "avwap",
    "long_score",
    "short_score",
    "equity_dd_pct",
    "daily_dd_pct",
    "atr_pct",
    "markov_transitions",
    "regime_pstay_pct",
    "short_review_ok",
    "short_failed_reclaim",
    "short_bear_flag_break",
    "short_bear_flip_retest",
    "short_sector_laggard",
    "short_plus_score",
    "short_plus_gate",
    "short_plus_agrade",
    "short_plus_risk_mult",
    "short_plus_score_boost",
    "short_no_chase_block",
    "short_support_block",
    "short_squeeze_block",
    "short_struct_stop",
    "short_stop_pct",
    "short_blocked_no_chase",
    "short_blocked_support",
    "short_blocked_squeeze",
    "long_review_ok",
    "long_blocked_no_chase",
    "long_blocked_resistance",
    "long_blocked_exhaustion",
    "long_struct_stop",
    "long_stop_pct",
    "long_virtual_equity",
    "short_virtual_equity",
    "avwap_force_reset",
    "avwap_age_bars",
    "avwap_display_hidden",
)

EXACT_FIELDS = tuple(
    name
    for name in CSV_COLUMNS
    if name
    not in {
        "timestamp",
        "regime_z",
        "strength",
        "mansfield",
        "avwap",
        "long_score",
        "short_score",
        "equity_dd_pct",
        "daily_dd_pct",
        "atr_pct",
        "regime_pstay_pct",
        "short_plus_score",
        "short_plus_risk_mult",
        "short_plus_score_boost",
        "short_struct_stop",
        "short_stop_pct",
        "long_struct_stop",
        "long_stop_pct",
        "long_virtual_equity",
        "short_virtual_equity",
    }
)

FLOAT_FIELDS = (
    "regime_z",
    "strength",
    "mansfield",
    "avwap",
    "long_score",
    "short_score",
    "equity_dd_pct",
    "daily_dd_pct",
    "atr_pct",
    "regime_pstay_pct",
    "short_plus_score",
    "short_plus_risk_mult",
    "short_plus_score_boost",
    "short_struct_stop",
    "short_stop_pct",
    "long_struct_stop",
    "long_stop_pct",
    "long_virtual_equity",
    "short_virtual_equity",
)


@dataclass(frozen=True, slots=True)
class FloatTolerance:
    """Named numeric contract for one family of exported values."""

    name: str
    abs_tol: float
    rel_tol: float

    def __post_init__(self) -> None:
        if not self.name or self.abs_tol < 0 or self.rel_tol < 0:
            raise ValueError("float tolerances require a name and non-negative limits")


INDICATOR_TOLERANCE = FloatTolerance("indicator", abs_tol=1e-8, rel_tol=1e-9)
PRICE_TOLERANCE = FloatTolerance("price", abs_tol=1e-8, rel_tol=1e-9)
ACCOUNT_TOLERANCE = FloatTolerance("account_value", abs_tol=1e-6, rel_tol=1e-9)

FLOAT_TOLERANCES: Mapping[str, FloatTolerance] = {
    "regime_z": INDICATOR_TOLERANCE,
    "strength": INDICATOR_TOLERANCE,
    "mansfield": INDICATOR_TOLERANCE,
    "avwap": PRICE_TOLERANCE,
    "long_score": INDICATOR_TOLERANCE,
    "short_score": INDICATOR_TOLERANCE,
    "equity_dd_pct": INDICATOR_TOLERANCE,
    "daily_dd_pct": INDICATOR_TOLERANCE,
    "atr_pct": INDICATOR_TOLERANCE,
    "regime_pstay_pct": INDICATOR_TOLERANCE,
    "short_plus_score": INDICATOR_TOLERANCE,
    "short_plus_risk_mult": INDICATOR_TOLERANCE,
    "short_plus_score_boost": INDICATOR_TOLERANCE,
    "short_struct_stop": PRICE_TOLERANCE,
    "short_stop_pct": INDICATOR_TOLERANCE,
    "long_struct_stop": PRICE_TOLERANCE,
    "long_stop_pct": INDICATOR_TOLERANCE,
    "long_virtual_equity": ACCOUNT_TOLERANCE,
    "short_virtual_equity": ACCOUNT_TOLERANCE,
}


def _cell(row: Mapping[str, str], name: str) -> str:
    value = row.get(name)
    if value is None:
        raise FixtureSchemaError(f"CSV cell {name!r} is missing")
    return value.strip()


def _parse_bool(row: Mapping[str, str], name: str) -> bool:
    raw = _cell(row, name).lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise FixtureSchemaError(f"CSV {name} must be exactly true or false, got {raw!r}")


def _parse_int(row: Mapping[str, str], name: str) -> int:
    raw = _cell(row, name)
    if not re.fullmatch(r"-?[0-9]+", raw):
        raise FixtureSchemaError(f"CSV {name} must be an integer, got {raw!r}")
    return int(raw)


def _parse_float(row: Mapping[str, str], name: str) -> float | None:
    raw = _cell(row, name)
    if raw.lower() in _NULL_TOKENS:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise FixtureSchemaError(
            f"CSV {name} must be a finite float or null, got {raw!r}"
        ) from error
    if not math.isfinite(value):
        raise FixtureSchemaError(f"CSV {name} must be finite when present, got {raw!r}")
    return value


def _parse_enum_value[T: StrEnum](
    row: Mapping[str, str], name: str, enum_type: type[T], *, optional: bool = False
) -> T | None:
    raw = _cell(row, name).lower()
    if optional and raw in _NULL_TOKENS:
        return None
    try:
        return enum_type(raw)
    except ValueError as error:
        allowed = ", ".join(member.value for member in enum_type)
        raise FixtureSchemaError(f"CSV {name} must be one of [{allowed}], got {raw!r}") from error


def _trace_row_from_mapping(row: Mapping[str, str], row_number: int) -> TraceRow:
    source_timestamp = _cell(row, "timestamp")
    try:
        regime = _parse_enum_value(row, "regime", Regime, optional=True)
        entry = _parse_enum_value(row, "entry_decision", EntryDecision)
        exit_ = _parse_enum_value(row, "exit_decision", ExitDecision)
        side = _parse_enum_value(row, "position_side", PositionSide)
        assert entry is not None and exit_ is not None and side is not None
        return TraceRow(
            timestamp_utc=_parse_aware_timestamp(
                source_timestamp, f"CSV row {row_number} timestamp"
            ),
            source_timestamp=source_timestamp,
            regime=regime,
            regime_flip=_parse_bool(row, "regime_flip"),
            entry_decision=entry,
            exit_decision=exit_,
            position_side=side,
            regime_z=_parse_float(row, "regime_z"),
            strength=_parse_float(row, "strength"),
            chop_risk=_parse_int(row, "chop_risk"),
            extension=_parse_bool(row, "extension"),
            mansfield=_parse_float(row, "mansfield"),
            avwap=_parse_float(row, "avwap"),
            long_score=_parse_float(row, "long_score"),
            short_score=_parse_float(row, "short_score"),
            equity_dd_pct=_parse_float(row, "equity_dd_pct"),
            daily_dd_pct=_parse_float(row, "daily_dd_pct"),
            atr_pct=_parse_float(row, "atr_pct"),
            markov_transitions=_parse_int(row, "markov_transitions"),
            regime_pstay_pct=_parse_float(row, "regime_pstay_pct"),
            short_review_ok=_parse_bool(row, "short_review_ok"),
            short_failed_reclaim=_parse_bool(row, "short_failed_reclaim"),
            short_bear_flag_break=_parse_bool(row, "short_bear_flag_break"),
            short_bear_flip_retest=_parse_bool(row, "short_bear_flip_retest"),
            short_sector_laggard=_parse_bool(row, "short_sector_laggard"),
            short_plus_score=_parse_float(row, "short_plus_score"),
            short_plus_gate=_parse_bool(row, "short_plus_gate"),
            short_plus_agrade=_parse_bool(row, "short_plus_agrade"),
            short_plus_risk_mult=_parse_float(row, "short_plus_risk_mult"),
            short_plus_score_boost=_parse_float(row, "short_plus_score_boost"),
            short_no_chase_block=_parse_bool(row, "short_no_chase_block"),
            short_support_block=_parse_bool(row, "short_support_block"),
            short_squeeze_block=_parse_bool(row, "short_squeeze_block"),
            short_struct_stop=_parse_float(row, "short_struct_stop"),
            short_stop_pct=_parse_float(row, "short_stop_pct"),
            short_blocked_no_chase=_parse_int(row, "short_blocked_no_chase"),
            short_blocked_support=_parse_int(row, "short_blocked_support"),
            short_blocked_squeeze=_parse_int(row, "short_blocked_squeeze"),
            long_review_ok=_parse_bool(row, "long_review_ok"),
            long_blocked_no_chase=_parse_int(row, "long_blocked_no_chase"),
            long_blocked_resistance=_parse_int(row, "long_blocked_resistance"),
            long_blocked_exhaustion=_parse_int(row, "long_blocked_exhaustion"),
            long_struct_stop=_parse_float(row, "long_struct_stop"),
            long_stop_pct=_parse_float(row, "long_stop_pct"),
            long_virtual_equity=_parse_float(row, "long_virtual_equity"),
            short_virtual_equity=_parse_float(row, "short_virtual_equity"),
            avwap_force_reset=_parse_bool(row, "avwap_force_reset"),
            avwap_age_bars=_parse_int(row, "avwap_age_bars"),
            avwap_display_hidden=_parse_bool(row, "avwap_display_hidden"),
        )
    except FixtureSchemaError as error:
        raise FixtureSchemaError(f"CSV row {row_number}: {error}") from error


@dataclass(frozen=True, slots=True)
class TraceFixture:
    metadata: FixtureMetadata
    rows: tuple[TraceRow, ...]


def load_trace_fixture(csv_path: Path, metadata_path: Path | None = None) -> TraceFixture:
    """Load and validate a normalized fixture without sorting or filling rows."""

    meta_path = metadata_path or csv_path.with_suffix(".meta.json")
    try:
        raw_document: Any = json.loads(
            meta_path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                FixtureSchemaError(f"metadata contains non-JSON constant {value!r}")
            ),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureSchemaError(f"unable to read fixture metadata {meta_path}: {error}") from error
    if not isinstance(raw_document, dict):
        raise FixtureSchemaError("fixture metadata root must be an object")
    metadata = FixtureMetadata.from_mapping(raw_document)

    try:
        actual_trace_sha256 = _file_sha256(csv_path)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            if header != CSV_COLUMNS:
                expected = set(CSV_COLUMNS)
                present = set(header)
                raise FixtureSchemaError(
                    "CSV columns do not match strict ordered schema; "
                    f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}, "
                    f"order_matches={present == expected}"
                )
            rows: list[TraceRow] = []
            prior_timestamp: datetime | None = None
            for row_number, raw_row in enumerate(reader, start=2):
                if None in raw_row:
                    raise FixtureSchemaError(f"CSV row {row_number} contains extra unnamed cells")
                normalized = {key: value or "" for key, value in raw_row.items()}
                parsed = _trace_row_from_mapping(normalized, row_number)
                if prior_timestamp is not None and parsed.timestamp_utc <= prior_timestamp:
                    raise FixtureSchemaError(
                        f"CSV row {row_number} timestamp must be strictly increasing; "
                        "fixtures are never sorted or shifted"
                    )
                prior_timestamp = parsed.timestamp_utc
                rows.append(parsed)
    except OSError as error:
        raise FixtureSchemaError(f"unable to read trace CSV {csv_path}: {error}") from error

    if actual_trace_sha256 != metadata.trace_sha256:
        raise FixtureSchemaError("metadata.trace_sha256 does not match trace CSV bytes")
    if len(rows) != metadata.row_count:
        raise FixtureSchemaError(
            f"metadata.row_count={metadata.row_count} does not match CSV rows={len(rows)}"
        )
    return TraceFixture(metadata=metadata, rows=tuple(rows))


@dataclass(frozen=True, slots=True)
class Divergence:
    """The first mismatch only, with enough state to reproduce the bar."""

    timestamp_utc: datetime | None
    field: str
    expected: object
    actual: object
    expected_state_digest: str | None
    actual_state_digest: str | None
    expected_gates: tuple[str, ...]
    actual_gates: tuple[str, ...]
    tolerance: FloatTolerance | None = None


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    matched: bool
    compared_rows: int
    parity_status: ParityStatus
    first_divergence: Divergence | None


def _metadata_mismatch(reference: FixtureMetadata, candidate: FixtureMetadata) -> str | None:
    for name in (
        "schema_version",
        "catalog_number",
        "pine_sha256",
        "symbol",
        "timeframe",
        "chart_timezone",
        "session",
        "timestamp_semantics",
        "input_count",
        "input_config_sha256",
    ):
        if getattr(reference, name) != getattr(candidate, name):
            return name
    return None


def _divergence(
    expected_row: TraceRow | None,
    actual_row: TraceRow | None,
    field: str,
    expected: object,
    actual: object,
    tolerance: FloatTolerance | None = None,
) -> Divergence:
    row = expected_row or actual_row
    return Divergence(
        timestamp_utc=row.timestamp_utc if row is not None else None,
        field=field,
        expected=expected,
        actual=actual,
        expected_state_digest=expected_row.state_digest if expected_row is not None else None,
        actual_state_digest=actual_row.state_digest if actual_row is not None else None,
        expected_gates=expected_row.gates if expected_row is not None else (),
        actual_gates=actual_row.gates if actual_row is not None else (),
        tolerance=tolerance,
    )


def _float_equal(expected: float | None, actual: float | None, tolerance: FloatTolerance) -> bool:
    if expected is None or actual is None:
        return expected is actual
    return math.isclose(
        expected,
        actual,
        rel_tol=tolerance.rel_tol,
        abs_tol=tolerance.abs_tol,
    )


def compare_trace_fixtures(
    reference: TraceFixture,
    candidate: TraceFixture,
    *,
    tolerances: Mapping[str, FloatTolerance] = FLOAT_TOLERANCES,
) -> ComparisonReport:
    """Compare exact bar identities and state, never nearest-neighbor timestamps.

    ``reference`` is expected to be the TradingView-side trace when genuine
    parity is attempted.  Metadata identity must match before any value is
    compared.  A synthetic reference can prove only that this comparison
    machinery behaves deterministically, never that TradingView agrees.
    """

    mismatch = _metadata_mismatch(reference.metadata, candidate.metadata)
    if mismatch is not None:
        raise FixtureSchemaError(f"fixture metadata mismatch for {mismatch}")
    if frozenset(tolerances) != frozenset(FLOAT_FIELDS):
        missing = sorted(set(FLOAT_FIELDS) - set(tolerances))
        unknown = sorted(set(tolerances) - set(FLOAT_FIELDS))
        raise FixtureSchemaError(
            f"float tolerances must cover the exact schema; missing={missing}, unknown={unknown}"
        )

    compared_rows = 0
    first: Divergence | None = None
    common = min(len(reference.rows), len(candidate.rows))
    for index in range(common):
        expected_row = reference.rows[index]
        actual_row = candidate.rows[index]
        if expected_row.timestamp_utc != actual_row.timestamp_utc:
            first = _divergence(
                expected_row,
                actual_row,
                "timestamp_utc",
                expected_row.timestamp_utc,
                actual_row.timestamp_utc,
            )
            break
        for name in EXACT_FIELDS:
            expected = getattr(expected_row, name)
            actual = getattr(actual_row, name)
            if expected != actual:
                first = _divergence(expected_row, actual_row, name, expected, actual)
                break
        if first is not None:
            break
        for name in FLOAT_FIELDS:
            expected = getattr(expected_row, name)
            actual = getattr(actual_row, name)
            tolerance = tolerances[name]
            if not _float_equal(expected, actual, tolerance):
                first = _divergence(
                    expected_row,
                    actual_row,
                    name,
                    expected,
                    actual,
                    tolerance,
                )
                break
        if first is not None:
            break
        compared_rows += 1

    if first is None and len(reference.rows) != len(candidate.rows):
        tail_expected = reference.rows[common] if common < len(reference.rows) else None
        tail_actual = candidate.rows[common] if common < len(candidate.rows) else None
        first = _divergence(
            tail_expected,
            tail_actual,
            "row_count",
            len(reference.rows),
            len(candidate.rows),
        )

    matched = first is None
    if reference.metadata.provenance is FixtureProvenance.GENUINE:
        parity_status = ParityStatus.VERIFIED if matched else ParityStatus.FAILED
    else:
        parity_status = ParityStatus.UNVERIFIED
    return ComparisonReport(
        matched=matched,
        compared_rows=compared_rows,
        parity_status=parity_status,
        first_divergence=first,
    )


def genuine_reference_present(fixtures: Sequence[TraceFixture]) -> bool:
    """Return true only for an actually loaded owner-exported reference."""

    return any(item.metadata.provenance is FixtureProvenance.GENUINE for item in fixtures)

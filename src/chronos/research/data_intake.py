"""Read-only parsing and verification for owner-supplied market-data deliveries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from chronos.histdata.corporate_actions import CorporateAction
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.marketdata.csv_provider import load_daily_csv_bytes
from chronos.research.certification import (
    CertificationError,
    CertificationReport,
    ClassifiedMove,
    CorporateActionAttestation,
    NoCorporateActionAttestation,
    SymbolWindow,
    certify_export,
)
from chronos.research.holdout_map import HoldoutSpan, HoldoutStatus
from chronos.research.session_calendar import CalendarCoverageError, SessionCalendar

INTAKE_SCHEMA_VERSION = 1
CAMPAIGN_SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
_HEX_DIGITS = frozenset("0123456789abcdef")


class IntakeUnverified(RuntimeError):
    """The delivery could not be bound to the bytes needed for certification."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IntakeProvenance:
    source_id: str
    source_receipt_sha256: str
    retrieved_at: datetime
    retrieval_method: str
    license_note: str


@dataclass(frozen=True, slots=True)
class IntakeDelivery:
    delivery_id: str
    supersedes: str | None
    provenance: IntakeProvenance
    windows: tuple[SymbolWindow, ...]
    series_by_symbol: Mapping[str, BarSeries]
    actions_by_symbol: Mapping[str, tuple[CorporateAction, ...]]
    attestation: CorporateActionAttestation | NoCorporateActionAttestation
    classified_moves: tuple[ClassifiedMove, ...]
    holdout_map: tuple[HoldoutSpan, ...]


def _unverified(path: Path, reason: str) -> IntakeUnverified:
    return IntakeUnverified(path, reason)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as error:
        raise _unverified(path, "file is missing") from error
    except OSError as error:
        raise _unverified(path, f"file is unreadable ({error.__class__.__name__})") from error


def _json_bytes(path: Path, raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _unverified(path, f"invalid JSON ({error.__class__.__name__})") from error


def _mapping(value: object, *, path: Path, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _unverified(path, f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _unverified(path, f"{field} field names must be strings")
    return value


def _array(value: object, *, path: Path, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise _unverified(path, f"{field} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], *, expected: set[str], path: Path, field: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise _unverified(path, f"{field} is missing field(s): {', '.join(missing)}")
    if extra:
        raise _unverified(path, f"{field} has unknown field(s): {', '.join(extra)}")


def _string(value: object, *, path: Path, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise _unverified(path, f"{field} must be {qualifier}")
    return value


def _digest(value: object, *, path: Path, field: str) -> str:
    digest = _string(value, path=path, field=field).lower()
    if len(digest) != 64 or any(character not in _HEX_DIGITS for character in digest):
        raise _unverified(path, f"{field} must be a 64-character lowercase SHA-256")
    return digest


def _count(value: object, *, path: Path, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _unverified(path, f"{field} must be a non-negative integer")
    return value


def _date(value: object, *, path: Path, field: str) -> date:
    try:
        return date.fromisoformat(_string(value, path=path, field=field))
    except ValueError as error:
        raise _unverified(path, f"{field} must be an ISO date") from error


def _timestamp(value: object, *, path: Path, field: str) -> datetime:
    text = _string(value, path=path, field=field)
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise _unverified(path, f"{field} must be an ISO timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(None):
        raise _unverified(path, f"{field} must be timezone-aware UTC")
    return timestamp


def _window(value: object, *, path: Path, field: str, symbol: str) -> SymbolWindow:
    item = _mapping(value, path=path, field=field)
    _exact_keys(item, expected={"start", "end"}, path=path, field=field)
    try:
        return SymbolWindow(
            symbol=symbol,
            start=_date(item["start"], path=path, field=f"{field}.start"),
            end=_date(item["end"], path=path, field=f"{field}.end"),
        )
    except ValueError as error:
        raise _unverified(path, f"{field} is invalid ({error})") from error


def _provenance(value: object, *, path: Path) -> IntakeProvenance:
    item = _mapping(value, path=path, field="provenance")
    _exact_keys(
        item,
        expected={
            "source_id",
            "source_receipt_sha256",
            "retrieved_at",
            "retrieval_method",
            "license_note",
        },
        path=path,
        field="provenance",
    )
    return IntakeProvenance(
        source_id=_string(item["source_id"], path=path, field="provenance.source_id"),
        source_receipt_sha256=_digest(
            item["source_receipt_sha256"], path=path, field="provenance.source_receipt_sha256"
        ),
        retrieved_at=_timestamp(item["retrieved_at"], path=path, field="provenance.retrieved_at"),
        retrieval_method=_string(
            item["retrieval_method"], path=path, field="provenance.retrieval_method"
        ),
        license_note=_string(item["license_note"], path=path, field="provenance.license_note"),
    )


def _attestation(
    value: object, *, path: Path
) -> CorporateActionAttestation | NoCorporateActionAttestation:
    item = _mapping(value, path=path, field="corporate_action_attestation")
    kind = _string(item.get("kind"), path=path, field="corporate_action_attestation.kind")
    try:
        if kind == "sampled_actions":
            _exact_keys(
                item,
                expected={"kind", "source_id", "sampled_action_count", "symbols", "note"},
                path=path,
                field="corporate_action_attestation",
            )
            symbols = tuple(
                _string(entry, path=path, field="corporate_action_attestation.symbols[]")
                for entry in _array(
                    item["symbols"], path=path, field="corporate_action_attestation.symbols"
                )
            )
            return CorporateActionAttestation(
                source_id=_string(
                    item["source_id"], path=path, field="corporate_action_attestation.source_id"
                ),
                sampled_action_count=_count(
                    item["sampled_action_count"],
                    path=path,
                    field="corporate_action_attestation.sampled_action_count",
                ),
                symbols=symbols,
                note=_string(
                    item["note"],
                    path=path,
                    field="corporate_action_attestation.note",
                    allow_empty=True,
                ),
            )
        if kind == "reviewed_no_actions":
            _exact_keys(
                item,
                expected={"kind", "source_id", "windows", "note"},
                path=path,
                field="corporate_action_attestation",
            )
            windows: list[SymbolWindow] = []
            for index, raw_window in enumerate(
                _array(item["windows"], path=path, field="corporate_action_attestation.windows")
            ):
                field = f"corporate_action_attestation.windows[{index}]"
                window_item = _mapping(raw_window, path=path, field=field)
                _exact_keys(
                    window_item,
                    expected={"symbol", "start", "end"},
                    path=path,
                    field=field,
                )
                symbol = _string(window_item["symbol"], path=path, field=f"{field}.symbol")
                windows.append(
                    SymbolWindow(
                        symbol=symbol,
                        start=_date(window_item["start"], path=path, field=f"{field}.start"),
                        end=_date(window_item["end"], path=path, field=f"{field}.end"),
                    )
                )
            return NoCorporateActionAttestation(
                source_id=_string(
                    item["source_id"], path=path, field="corporate_action_attestation.source_id"
                ),
                windows=tuple(windows),
                note=_string(
                    item["note"],
                    path=path,
                    field="corporate_action_attestation.note",
                    allow_empty=True,
                ),
            )
    except ValueError as error:
        raise _unverified(path, f"corporate_action_attestation is invalid ({error})") from error
    raise _unverified(path, f"corporate_action_attestation.kind {kind!r} is unsupported")


def _classified_moves(value: object, *, path: Path) -> tuple[ClassifiedMove, ...]:
    moves: list[ClassifiedMove] = []
    for index, raw_move in enumerate(_array(value, path=path, field="classified_moves")):
        field = f"classified_moves[{index}]"
        item = _mapping(raw_move, path=path, field=field)
        _exact_keys(
            item,
            expected={"symbol", "session_date", "reason"},
            path=path,
            field=field,
        )
        try:
            moves.append(
                ClassifiedMove(
                    symbol=_string(item["symbol"], path=path, field=f"{field}.symbol"),
                    session_date=_date(
                        item["session_date"], path=path, field=f"{field}.session_date"
                    ),
                    reason=_string(item["reason"], path=path, field=f"{field}.reason"),
                )
            )
        except ValueError as error:
            raise _unverified(path, f"{field} is invalid ({error})") from error
    return tuple(moves)


def _holdout_map(value: object, *, path: Path) -> tuple[HoldoutSpan, ...]:
    spans: list[HoldoutSpan] = []
    for index, raw_span in enumerate(_array(value, path=path, field="holdout_map")):
        field = f"holdout_map[{index}]"
        item = _mapping(raw_span, path=path, field=field)
        required = {"symbol", "name", "start", "end", "status"}
        missing = sorted(required - item.keys())
        extra = sorted(item.keys() - required - {"reason"})
        if missing:
            raise _unverified(path, f"{field} is missing field(s): {', '.join(missing)}")
        if extra:
            raise _unverified(path, f"{field} has unknown field(s): {', '.join(extra)}")
        try:
            spans.append(
                HoldoutSpan(
                    symbol=_string(item["symbol"], path=path, field=f"{field}.symbol"),
                    name=_string(item["name"], path=path, field=f"{field}.name"),
                    start=_date(item["start"], path=path, field=f"{field}.start"),
                    end=_date(item["end"], path=path, field=f"{field}.end"),
                    status=HoldoutStatus(
                        _string(item["status"], path=path, field=f"{field}.status")
                    ),
                    reason=_string(
                        item.get("reason", ""),
                        path=path,
                        field=f"{field}.reason",
                        allow_empty=True,
                    ),
                )
            )
        except ValueError as error:
            raise _unverified(path, f"{field} is invalid ({error})") from error
    return tuple(spans)


def load_intake(delivery: Path) -> IntakeDelivery:
    """Load and byte-bind a delivery without writing to it or any repository store."""

    manifest_path = delivery / "INTAKE.json"
    document = _mapping(
        _json_bytes(manifest_path, _read_bytes(manifest_path)),
        path=manifest_path,
        field="INTAKE.json",
    )
    _exact_keys(
        document,
        expected={
            "schema_version",
            "delivery_id",
            "supersedes",
            "interval",
            "adjustment_policy",
            "provenance",
            "symbols",
            "corporate_action_attestation",
            "classified_moves",
            "holdout_map",
        },
        path=manifest_path,
        field="INTAKE.json",
    )
    if (
        isinstance(document["schema_version"], bool)
        or not isinstance(document["schema_version"], int)
        or document["schema_version"] != INTAKE_SCHEMA_VERSION
    ):
        raise _unverified(manifest_path, f"schema_version must be {INTAKE_SCHEMA_VERSION}")
    delivery_id = _string(document["delivery_id"], path=manifest_path, field="delivery_id")
    supersedes_raw = document["supersedes"]
    supersedes = (
        None
        if supersedes_raw is None
        else _digest(supersedes_raw, path=manifest_path, field="supersedes")
    )
    if document["interval"] != BarInterval.DAY_1:
        raise _unverified(manifest_path, "interval must be 1d")
    if document["adjustment_policy"] != "unadjusted_as_traded":
        raise _unverified(manifest_path, "adjustment_policy must be unadjusted_as_traded")
    provenance = _provenance(document["provenance"], path=manifest_path)

    symbols = _mapping(document["symbols"], path=manifest_path, field="symbols")
    if set(symbols) != set(CAMPAIGN_SYMBOLS):
        expected = ",".join(sorted(CAMPAIGN_SYMBOLS))
        observed = ",".join(sorted(str(symbol) for symbol in symbols)) or "none"
        raise _unverified(
            manifest_path,
            f"symbols must be exactly {expected} (observed {observed})",
        )

    calendar = SessionCalendar()
    windows: list[SymbolWindow] = []
    series_by_symbol: dict[str, BarSeries] = {}
    actions_by_symbol: dict[str, tuple[CorporateAction, ...]] = {}
    for symbol in CAMPAIGN_SYMBOLS:
        field = f"symbols.{symbol}"
        item = _mapping(symbols[symbol], path=manifest_path, field=field)
        _exact_keys(
            item,
            expected={
                "window",
                "bars_sha256",
                "bar_count",
                "corporate_actions_sha256",
                "corporate_action_count",
            },
            path=manifest_path,
            field=field,
        )
        window = _window(item["window"], path=manifest_path, field=f"{field}.window", symbol=symbol)
        try:
            calendar.sessions(window.start, window.end)
        except CalendarCoverageError as error:
            raise _unverified(manifest_path, f"{symbol} window {error}") from error
        windows.append(window)

        bar_path = delivery / "bars" / f"{symbol}.csv"
        bar_bytes = _read_bytes(bar_path)
        declared_bar_digest = _digest(
            item["bars_sha256"], path=manifest_path, field=f"{field}.bars_sha256"
        )
        observed_bar_digest = hashlib.sha256(bar_bytes).hexdigest()
        if observed_bar_digest != declared_bar_digest:
            raise _unverified(
                bar_path,
                "bars_sha256 mismatch "
                f"(manifest {declared_bar_digest}, recomputed {observed_bar_digest})",
            )
        try:
            loaded = load_daily_csv_bytes(
                bar_bytes,
                path=bar_path,
                symbol=symbol,
                source=provenance.source_id,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _unverified(bar_path, f"daily CSV is unparseable ({error})") from error
        declared_bar_count = _count(
            item["bar_count"], path=manifest_path, field=f"{field}.bar_count"
        )
        if len(loaded.series) != declared_bar_count:
            raise _unverified(
                bar_path,
                f"bar_count mismatch (manifest {declared_bar_count}, parsed {len(loaded.series)})",
            )
        series_by_symbol[symbol] = loaded.series

        action_path = delivery / "corporate_actions" / f"{symbol}.json"
        action_bytes = _read_bytes(action_path)
        declared_action_digest = _digest(
            item["corporate_actions_sha256"],
            path=manifest_path,
            field=f"{field}.corporate_actions_sha256",
        )
        observed_action_digest = hashlib.sha256(action_bytes).hexdigest()
        if observed_action_digest != declared_action_digest:
            raise _unverified(
                action_path,
                "corporate_actions_sha256 mismatch "
                f"(manifest {declared_action_digest}, recomputed {observed_action_digest})",
            )
        raw_actions = _array(
            _json_bytes(action_path, action_bytes), path=action_path, field=f"{symbol} actions"
        )
        actions: list[CorporateAction] = []
        for index, raw_action in enumerate(raw_actions):
            try:
                actions.append(
                    CorporateAction.from_mapping(
                        _mapping(
                            raw_action,
                            path=action_path,
                            field=f"{symbol} actions[{index}]",
                        )
                    )
                )
            except ValueError as error:
                raise _unverified(
                    action_path, f"corporate action at index {index} is invalid"
                ) from error
        declared_action_count = _count(
            item["corporate_action_count"],
            path=manifest_path,
            field=f"{field}.corporate_action_count",
        )
        if len(actions) != declared_action_count:
            raise _unverified(
                action_path,
                "corporate_action_count mismatch "
                f"(manifest {declared_action_count}, parsed {len(actions)})",
            )
        actions_by_symbol[symbol] = tuple(actions)

    return IntakeDelivery(
        delivery_id=delivery_id,
        supersedes=supersedes,
        provenance=provenance,
        windows=tuple(windows),
        series_by_symbol=series_by_symbol,
        actions_by_symbol=actions_by_symbol,
        attestation=_attestation(document["corporate_action_attestation"], path=manifest_path),
        classified_moves=_classified_moves(document["classified_moves"], path=manifest_path),
        holdout_map=_holdout_map(document["holdout_map"], path=manifest_path),
    )


def verify_intake(delivery: Path) -> CertificationReport:
    """Run the existing frozen certification gates over a byte-bound delivery."""

    return certify_loaded_intake(load_intake(delivery), delivery=delivery)


def certify_loaded_intake(intake: IntakeDelivery, *, delivery: Path) -> CertificationReport:
    """Run the frozen gates over an intake that has already been byte-bound."""

    try:
        return certify_export(
            dataset_id=intake.delivery_id,
            windows=intake.windows,
            series_by_symbol=intake.series_by_symbol,
            actions_by_symbol=intake.actions_by_symbol,
            attestation=intake.attestation,
            classified_moves=intake.classified_moves,
            holdout_map=intake.holdout_map,
            interval=BarInterval.DAY_1,
        )
    except CertificationError as error:
        raise _unverified(
            delivery / "INTAKE.json", f"certification request is invalid ({error})"
        ) from error


__all__ = [
    "CAMPAIGN_SYMBOLS",
    "INTAKE_SCHEMA_VERSION",
    "IntakeDelivery",
    "IntakeProvenance",
    "IntakeUnverified",
    "certify_loaded_intake",
    "load_intake",
    "verify_intake",
]

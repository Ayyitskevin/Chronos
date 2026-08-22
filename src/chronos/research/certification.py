"""Certify a historical-data export against the frozen Phase 3 data-quality gates.

``docs/VISION_COMPLETION_PLAN.md`` §8 freezes four starting data-quality gates before
collection: at least 99.5% expected-session coverage; every gap and extreme move
classified with zero unresolved economically material conflicts; corporate actions
independently sampled and reconciled; and a complete, content-addressed holdout map.
Until now those gates had no executable form, so "certified data" was a phrase rather
than a verdict. This module is the verdict.

Three properties are load-bearing.

**It measures against an independent expectation.** Coverage is computed from
``session_calendar``'s expected sessions, not from the delivered rows — a vendor that
silently drops forty sessions ships a file perfectly consistent with itself, and only an
outside expectation can see the hole.

**Both error directions block.** An expected session with no bar is a gap; a bar on a
day the exchange was closed is an unexpected bar. Neither is silently absorbed, which is
also what keeps a wrong calendar entry loud (see ``session_calendar``'s module note).

**It is fail-closed and deterministic.** Every gate must pass affirmatively; anything
unclassified is a blocking finding, never a warning. The report carries no timestamp and
no host detail, so certifying the same bytes twice produces byte-identical evidence and
``certification_digest`` is a pure function of the data it judged.

What this module deliberately does **not** claim: it does not make data trustworthy, and
it cannot perform the "independently sampled" half of the corporate-action gate. Sampling
a second, unrelated source is an owner act; code can only check that the attestation
exists and reconcile the stream it was given against the prices. An export with no
attestation is refused rather than certified on the strength of self-consistency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.marketdata.quality import validate_series
from chronos.research.session_calendar import CalendarCoverageError, SessionCalendar

#: Phase 3, frozen before collection. Not tunable at call sites on purpose: a floor a
#: caller can lower is not a floor.
MINIMUM_SESSION_COVERAGE = 0.995

#: A close-to-close move at or beyond this size must be explained by a corporate action
#: or an owner classification. Unadjusted ETF history makes this a split detector first
#: and a market-event detector second.
MATERIAL_RETURN_THRESHOLD = 0.20

#: How closely an observed split-day return must match the ratio the action stream
#: declares. A 4-for-1 split implies -75%; anything inside this band reconciles.
SPLIT_RECONCILIATION_TOLERANCE = 0.02

CERTIFICATION_SCHEMA_VERSION = "chronos-dataset-certification-v1"


class CertificationError(RuntimeError):
    """Certification could not be attempted as requested."""


class Verdict(StrEnum):
    CERTIFIED = "CERTIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"


class FindingKind(StrEnum):
    MISSING_SESSION = "MISSING_SESSION"
    UNEXPECTED_BAR = "UNEXPECTED_BAR"
    COVERAGE_BELOW_FLOOR = "COVERAGE_BELOW_FLOOR"
    UNCLASSIFIED_MATERIAL_MOVE = "UNCLASSIFIED_MATERIAL_MOVE"
    UNRECONCILED_SPLIT = "UNRECONCILED_SPLIT"
    BLOCKING_QUALITY_ISSUE = "BLOCKING_QUALITY_ISSUE"
    EMPTY_SERIES = "EMPTY_SERIES"
    MISSING_ATTESTATION = "MISSING_ATTESTATION"
    CALENDAR_NOT_COVERED = "CALENDAR_NOT_COVERED"


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason an export is not certified. Every finding blocks; there are no warnings."""

    kind: FindingKind
    symbol: str
    detail: str
    session_date: date | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "symbol": self.symbol,
            "detail": self.detail,
            "session_date": self.session_date.isoformat() if self.session_date else None,
        }


@dataclass(frozen=True, slots=True)
class SymbolWindow:
    """The exact range one symbol is certified over.

    Declared per symbol rather than inferred, so an instrument that did not exist for
    the whole dataset range states its listing date instead of having its head
    truncation read as an acceptable absence.
    """

    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("SymbolWindow.symbol must be non-empty")
        if self.start > self.end:
            raise ValueError(f"{self.symbol}: start must not follow end")


@dataclass(frozen=True, slots=True)
class ClassifiedMove:
    """An owner classification for a material move no corporate action explains.

    This is the documented seam for genuine market events — a crash, a limit move —
    and it is deliberately narrow: one exact symbol and date, with a reason that ends
    up in the certification evidence.
    """

    symbol: str
    session_date: date
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("ClassifiedMove.reason must explain the move")


@dataclass(frozen=True, slots=True)
class CorporateActionAttestation:
    """The owner's record of independently sampling the corporate-action stream.

    Code cannot do this half of the gate. It can refuse to certify without it.
    """

    source_id: str
    sampled_action_count: int
    symbols: tuple[str, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("attestation source_id must name the independent source")
        if self.sampled_action_count < 1:
            raise ValueError("an attestation with no sampled actions attests nothing")
        if not self.symbols:
            raise ValueError("attestation must name the symbols it covers")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "sampled_action_count": self.sampled_action_count,
            "symbols": list(self.symbols),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class SymbolCoverage:
    """Measured coverage for one symbol against its expected sessions."""

    symbol: str
    expected_sessions: int
    observed_bars: int
    missing_sessions: tuple[date, ...]
    unexpected_bars: tuple[date, ...]

    @property
    def coverage(self) -> float:
        if self.expected_sessions == 0:
            return 0.0
        present = self.expected_sessions - len(self.missing_sessions)
        return present / self.expected_sessions

    @property
    def meets_floor(self) -> bool:
        return self.coverage >= MINIMUM_SESSION_COVERAGE

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expected_sessions": self.expected_sessions,
            "observed_bars": self.observed_bars,
            "coverage": round(self.coverage, 6),
            "meets_floor": self.meets_floor,
            "missing_sessions": [day.isoformat() for day in self.missing_sessions],
            "unexpected_bars": [day.isoformat() for day in self.unexpected_bars],
        }


@dataclass(frozen=True, slots=True)
class CertificationReport:
    """The verdict, its evidence, and the digest that identifies both."""

    dataset_id: str
    interval: BarInterval
    coverage: tuple[SymbolCoverage, ...]
    findings: tuple[Finding, ...]
    attestation: CorporateActionAttestation | None
    classified_moves: tuple[ClassifiedMove, ...] = field(default=())

    @property
    def verdict(self) -> Verdict:
        return Verdict.NOT_CERTIFIED if self.findings else Verdict.CERTIFIED

    @property
    def certified(self) -> bool:
        return self.verdict is Verdict.CERTIFIED

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": CERTIFICATION_SCHEMA_VERSION,
            "dataset_id": self.dataset_id,
            "interval": str(self.interval),
            "verdict": str(self.verdict),
            "minimum_session_coverage": MINIMUM_SESSION_COVERAGE,
            "material_return_threshold": MATERIAL_RETURN_THRESHOLD,
            "coverage": [entry.to_mapping() for entry in self.coverage],
            "findings": [finding.to_mapping() for finding in self.findings],
            "attestation": self.attestation.to_mapping() if self.attestation else None,
            "classified_moves": [
                {
                    "symbol": move.symbol,
                    "session_date": move.session_date.isoformat(),
                    "reason": move.reason,
                }
                for move in self.classified_moves
            ],
        }

    def canonical_json(self) -> bytes:
        """Deterministic bytes. No timestamp, no host — the same data certifies the same."""

        return json.dumps(
            self.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def certification_digest(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


# --------------------------------------------------------------------------- internals


def _split_implied_return(ratio: float) -> float:
    """Unadjusted close-to-close return implied by a split of ``ratio``-for-one."""

    if ratio <= 0:
        raise ValueError("split ratio must be positive")
    return (1.0 / ratio) - 1.0


def _material_moves(series: BarSeries) -> tuple[tuple[date, float], ...]:
    """Close-to-close returns at or beyond the material threshold, in order."""

    moves: list[tuple[date, float]] = []
    previous_close: float | None = None
    for bar in series.bars:
        if previous_close is not None and previous_close > 0:
            change = (bar.close / previous_close) - 1.0
            if abs(change) >= MATERIAL_RETURN_THRESHOLD:
                moves.append((bar.session_date, change))
        previous_close = bar.close
    return tuple(moves)


def _splits_by_date(actions: Iterable[CorporateAction]) -> dict[date, CorporateAction]:
    return {action.ex_date: action for action in actions if action.kind is ActionKind.SPLIT}


def _reconciles(observed: float, action: CorporateAction) -> bool:
    return abs(observed - _split_implied_return(action.value)) <= SPLIT_RECONCILIATION_TOLERANCE


# ------------------------------------------------------------------------------ certify


def certify_export(
    *,
    dataset_id: str,
    windows: Sequence[SymbolWindow],
    series_by_symbol: Mapping[str, BarSeries],
    actions_by_symbol: Mapping[str, Sequence[CorporateAction]],
    attestation: CorporateActionAttestation | None,
    classified_moves: Sequence[ClassifiedMove] = (),
    calendar: SessionCalendar | None = None,
    interval: BarInterval = BarInterval.DAY_1,
) -> CertificationReport:
    """Judge one export against the frozen gates and return the verdict with evidence.

    Refuses a non-daily interval outright: the historical-data plane requests ``"1 day"``
    bars only (``histdata/official_client.py``), so an hourly certification would be
    judging an export that no ingestion path can currently produce.
    """

    if not dataset_id.strip():
        raise CertificationError("dataset_id must be non-empty")
    if interval is not BarInterval.DAY_1:
        raise CertificationError(
            f"certification supports {BarInterval.DAY_1} only; the historical-data plane "
            f"ingests daily bars and has no {interval} path to certify"
        )
    if not windows:
        raise CertificationError("certification requires at least one symbol window")

    calendar = calendar or SessionCalendar()
    findings: list[Finding] = []
    coverage: list[SymbolCoverage] = []

    if attestation is None:
        findings.append(
            Finding(
                kind=FindingKind.MISSING_ATTESTATION,
                symbol="*",
                detail=(
                    "no independent corporate-action sample attested; Phase 3 requires "
                    "actions independently sampled and reconciled, and self-consistency "
                    "is not a second source"
                ),
            )
        )

    classified = {(move.symbol, move.session_date): move for move in classified_moves}
    attested_symbols = set(attestation.symbols) if attestation else set()

    for window in sorted(windows, key=lambda item: item.symbol):
        symbol = window.symbol
        series = series_by_symbol.get(symbol)
        if series is None or len(series) == 0:
            findings.append(
                Finding(
                    kind=FindingKind.EMPTY_SERIES,
                    symbol=symbol,
                    detail="no bars supplied for a declared certification window",
                )
            )
            coverage.append(
                SymbolCoverage(
                    symbol=symbol,
                    expected_sessions=0,
                    observed_bars=0,
                    missing_sessions=(),
                    unexpected_bars=(),
                )
            )
            continue

        if attestation is not None and symbol not in attested_symbols:
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_ATTESTATION,
                    symbol=symbol,
                    detail=(
                        f"the corporate-action attestation from {attestation.source_id!r} "
                        "does not cover this symbol"
                    ),
                )
            )

        try:
            expected = set(calendar.sessions(window.start, window.end))
        except CalendarCoverageError as error:
            findings.append(
                Finding(
                    kind=FindingKind.CALENDAR_NOT_COVERED,
                    symbol=symbol,
                    detail=str(error),
                )
            )
            coverage.append(
                SymbolCoverage(
                    symbol=symbol,
                    expected_sessions=0,
                    observed_bars=len(series),
                    missing_sessions=(),
                    unexpected_bars=(),
                )
            )
            continue

        in_window = [bar for bar in series.bars if window.start <= bar.session_date <= window.end]
        observed = {bar.session_date for bar in in_window}
        missing = tuple(sorted(expected - observed))
        unexpected = tuple(sorted(observed - expected))

        entry = SymbolCoverage(
            symbol=symbol,
            expected_sessions=len(expected),
            observed_bars=len(in_window),
            missing_sessions=missing,
            unexpected_bars=unexpected,
        )
        coverage.append(entry)

        for day in missing:
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_SESSION,
                    symbol=symbol,
                    detail="the exchange held a session and the export has no bar for it",
                    session_date=day,
                )
            )
        for day in unexpected:
            reason = calendar.closure_reason(day) or "outside the declared window"
            findings.append(
                Finding(
                    kind=FindingKind.UNEXPECTED_BAR,
                    symbol=symbol,
                    detail=f"bar delivered for a non-session day ({reason})",
                    session_date=day,
                )
            )
        if not entry.meets_floor:
            findings.append(
                Finding(
                    kind=FindingKind.COVERAGE_BELOW_FLOOR,
                    symbol=symbol,
                    detail=(
                        f"coverage {entry.coverage:.4f} is below the frozen floor "
                        f"{MINIMUM_SESSION_COVERAGE}"
                    ),
                )
            )

        report = validate_series(series)
        for issue in report.issues:
            if issue.blocking:
                findings.append(
                    Finding(
                        kind=FindingKind.BLOCKING_QUALITY_ISSUE,
                        symbol=symbol,
                        detail=f"{issue.kind}: {issue.detail}",
                    )
                )

        splits = _splits_by_date(actions_by_symbol.get(symbol, ()))
        moves = {day: change for day, change in _material_moves(series)}

        for day, change in sorted(moves.items()):
            if not (window.start <= day <= window.end):
                continue
            action = splits.get(day)
            if action is not None and _reconciles(change, action):
                continue
            if (symbol, day) in classified:
                continue
            findings.append(
                Finding(
                    kind=FindingKind.UNCLASSIFIED_MATERIAL_MOVE,
                    symbol=symbol,
                    detail=(
                        f"close-to-close move {change:+.4f} is unexplained: no split on "
                        "this ex-date reconciles it and no owner classification covers it"
                    ),
                    session_date=day,
                )
            )

        for day, action in sorted(splits.items()):
            if not (window.start <= day <= window.end):
                continue
            observed_change = moves.get(day)
            implied = _split_implied_return(action.value)
            if observed_change is None or not _reconciles(observed_change, action):
                findings.append(
                    Finding(
                        kind=FindingKind.UNRECONCILED_SPLIT,
                        symbol=symbol,
                        detail=(
                            f"the action stream declares a {action.value:g}-for-1 split "
                            f"(implying {implied:+.4f}) but the prices show "
                            + (
                                f"{observed_change:+.4f}"
                                if observed_change is not None
                                else "no material move"
                            )
                            + " — the bars or the action stream is wrong"
                        ),
                        session_date=day,
                    )
                )

    return CertificationReport(
        dataset_id=dataset_id,
        interval=interval,
        coverage=tuple(coverage),
        findings=tuple(findings),
        attestation=attestation,
        classified_moves=tuple(classified_moves),
    )


__all__ = [
    "CERTIFICATION_SCHEMA_VERSION",
    "MATERIAL_RETURN_THRESHOLD",
    "MINIMUM_SESSION_COVERAGE",
    "SPLIT_RECONCILIATION_TOLERANCE",
    "CertificationError",
    "CertificationReport",
    "ClassifiedMove",
    "CorporateActionAttestation",
    "Finding",
    "FindingKind",
    "SymbolCoverage",
    "SymbolWindow",
    "Verdict",
    "certify_export",
]

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
exists, bind its count to distinct supplied events, commit to their semantics, and reconcile
the stream it was given against the prices. An export with no attestation is refused rather
than certified on the strength of self-consistency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.marketdata.quality import validate_series
from chronos.research.holdout_map import HoldoutMapError, HoldoutSpan, validate_holdout_map
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

# The evidence mapping shape is pinned by a golden-digest test: renaming or adding a
# field must bump this constant, or every recorded digest silently re-identifies.
# v2 added the bar-granular evidence fields for HOUR_1 certification. v3 binds the
# corporate-action semantics the verdict judged and makes typed sample counts
# mechanically consistent with distinct supplied events. Both bumps occurred while
# zero production digests existed (no real release had ever been minted); after the
# first real release this constant moves only with a migration story.
CERTIFICATION_SCHEMA_VERSION = "chronos-dataset-certification-v4"


class CertificationError(RuntimeError):
    """Certification could not be attempted as requested."""


class ProviderPriceBasis(StrEnum):
    """How the provider produced the delivered OHLC prices — a vendor fact, not a contract.

    ``adjustment_policy`` says what the delivery CLAIMS to satisfy; this says how the bytes
    were actually made. One field was carrying both, which is how a split-adjusted capture
    could be labelled ``unadjusted_as_traded`` with nothing objecting (ADR-0059).

    The vocabulary is closed and covers every delivered OHLC price for every symbol and date.
    Mixed sources, mixed bases, another vendor's adjusted format and later normalisations are
    unsupported: they must refuse, never coerce into ``UNADJUSTED_AS_TRADED``.
    """

    UNADJUSTED_AS_TRADED = "unadjusted_as_traded"
    IBKR_TRADES_SPLIT_ADJUSTED = "ibkr_trades_split_adjusted"
    IBKR_ADJUSTED_LAST_SPLIT_AND_DIVIDEND_ADJUSTED = (
        "ibkr_adjusted_last_split_and_dividend_adjusted"
    )


class ProviderPriceBasisRefused(Exception):
    """The declared basis cannot produce as-traded levels, so no verdict about it is possible.

    Raised by :func:`admit_provider_price_basis`. Each entry point maps it to its own failure
    mode — ``UNVERIFIED`` for the intake CLI, a non-zero exit for the owner script — but the
    rule itself lives in one place so an entry point cannot quietly hold a laxer one.
    """

    def __init__(self, basis: ProviderPriceBasis, reason: str) -> None:
        super().__init__(reason)
        self.basis = basis
        self.reason = reason


def admit_provider_price_basis(basis: ProviderPriceBasis) -> None:
    """Option A (ADR-0059): admit as-traded levels and nothing else.

    THE ONLY admission rule for the declared basis. Every path that can certify or freeze a
    delivery must call it before doing either — a path that merely records the field grants
    admission by omission, which is how the owner script came to freeze a release declaring a
    basis the intake CLI refuses.

    Option B — admitting a split-adjusted feed under compensating controls — is a contract
    change requiring the owner's written admission and a reviewed consumer boundary. It is
    deliberately not reachable from here.
    """

    if basis is ProviderPriceBasis.IBKR_ADJUSTED_LAST_SPLIT_AND_DIVIDEND_ADJUSTED:
        raise ProviderPriceBasisRefused(
            basis,
            f"provider_price_basis {basis.value} is adjusted for splits AND dividends and can "
            "never satisfy adjustment_policy unadjusted_as_traded: the dividend adjustment is "
            "not recoverable from the bars, so no declaration rescues it",
        )
    if basis is ProviderPriceBasis.IBKR_TRADES_SPLIT_ADJUSTED:
        raise ProviderPriceBasisRefused(
            basis,
            f"provider_price_basis {basis.value} cannot satisfy adjustment_policy "
            "unadjusted_as_traded: a split after the delivered window rescales the whole "
            "series and no in-window check can see it, so an empty in-window split set is "
            "not evidence of raw levels",
        )


class Verdict(StrEnum):
    CERTIFIED = "CERTIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"


class FindingKind(StrEnum):
    MISSING_SESSION = "MISSING_SESSION"
    MISSING_BAR = "MISSING_BAR"
    UNEXPECTED_BAR = "UNEXPECTED_BAR"
    COVERAGE_BELOW_FLOOR = "COVERAGE_BELOW_FLOOR"
    UNCLASSIFIED_MATERIAL_MOVE = "UNCLASSIFIED_MATERIAL_MOVE"
    UNRECONCILED_SPLIT = "UNRECONCILED_SPLIT"
    BLOCKING_QUALITY_ISSUE = "BLOCKING_QUALITY_ISSUE"
    EMPTY_SERIES = "EMPTY_SERIES"
    MISSING_ATTESTATION = "MISSING_ATTESTATION"
    EMPTY_ACTION_PANEL = "EMPTY_ACTION_PANEL"
    ATTESTATION_EXCEEDS_ACTIONS = "ATTESTATION_EXCEEDS_ACTIONS"
    DUPLICATE_CORPORATE_ACTION = "DUPLICATE_CORPORATE_ACTION"
    NO_ACTION_ATTESTATION_MISMATCH = "NO_ACTION_ATTESTATION_MISMATCH"
    NO_ACTION_ATTESTATION_CONTRADICTED = "NO_ACTION_ATTESTATION_CONTRADICTED"
    CALENDAR_NOT_COVERED = "CALENDAR_NOT_COVERED"


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason an export is not certified. Every finding blocks; there are no warnings.

    ``timestamp_utc`` locates bar-granular findings: for an hourly export, "SPY
    2024-05-06" cannot say WHICH of seven bars is missing, and evidence that
    cannot name the defect is not evidence.
    """

    kind: FindingKind
    symbol: str
    detail: str
    session_date: date | None = None
    timestamp_utc: datetime | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "symbol": self.symbol,
            "detail": self.detail,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "timestamp_utc": self.timestamp_utc.isoformat() if self.timestamp_utc else None,
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
            "kind": "sampled_actions",
            "source_id": self.source_id,
            "sampled_action_count": self.sampled_action_count,
            "symbols": list(self.symbols),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class NoCorporateActionAttestation:
    """Independent-source evidence that exact declared windows contain no actions.

    This is deliberately a separate type from a positive sampled-action count. It
    binds the source review to exact symbol windows, so a free-form note cannot turn
    an unexpectedly empty multi-decade action capture into affirmative evidence.
    """

    source_id: str
    windows: tuple[SymbolWindow, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("no-action attestation source_id must name the independent source")
        if not self.windows:
            raise ValueError("no-action attestation must name the exact windows it covers")
        identities = {(window.symbol, window.start, window.end) for window in self.windows}
        if len(identities) != len(self.windows):
            raise ValueError("no-action attestation windows must not contain duplicates")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({window.symbol for window in self.windows}))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": "reviewed_no_actions",
            "source_id": self.source_id,
            "windows": [
                {
                    "symbol": window.symbol,
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                }
                for window in sorted(
                    self.windows, key=lambda item: (item.symbol, item.start, item.end)
                )
            ],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionEvidence:
    """Content identity and distinct event count for one judged action stream."""

    symbol: str
    count: int
    semantic_sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "count": self.count,
            "semantic_sha256": self.semantic_sha256,
        }


@dataclass(frozen=True, slots=True)
class SymbolCoverage:
    """Measured coverage for one symbol against its expected sessions.

    The session dimension is always present. For an hourly export the bar
    dimension is filled too, and it is the one the floor binds: a session with
    one of its seven bars would count as "present" at session granularity, which
    is exactly the hole bar-level certification exists to see.
    """

    symbol: str
    expected_sessions: int
    observed_bars: int
    missing_sessions: tuple[date, ...]
    unexpected_bars: tuple[date, ...]
    expected_bar_total: int | None = None
    observed_slot_bars: int | None = None
    missing_bar_timestamps: tuple[datetime, ...] = ()
    unexpected_bar_timestamps: tuple[datetime, ...] = ()

    @property
    def coverage(self) -> float:
        if self.expected_bar_total is not None:
            if self.expected_bar_total == 0:
                return 0.0
            return (self.observed_slot_bars or 0) / self.expected_bar_total
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
            "expected_bar_total": self.expected_bar_total,
            "observed_slot_bars": self.observed_slot_bars,
            "missing_bar_timestamps": [ts.isoformat() for ts in self.missing_bar_timestamps],
            "unexpected_bar_timestamps": [ts.isoformat() for ts in self.unexpected_bar_timestamps],
        }


@dataclass(frozen=True, slots=True)
class CertificationReport:
    """The verdict, its evidence, and the digest that identifies both."""

    dataset_id: str
    interval: BarInterval
    coverage: tuple[SymbolCoverage, ...]
    findings: tuple[Finding, ...]
    attestation: CorporateActionAttestation | NoCorporateActionAttestation | None
    corporate_actions: tuple[CorporateActionEvidence, ...]
    #: The provider basis this verdict was reached over. It is RECORDED here and DOES NOT
    #: ENFORCE downstream use: a consumer that reads these bytes is not prevented from
    #: treating them as raw by this field's presence. Enforcement at the reader boundary is
    #: a separate, separately-reviewed slice (ADR-0059 §sequencing).
    provider_price_basis: ProviderPriceBasis
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
            "provider_price_basis": str(self.provider_price_basis),
            "verdict": str(self.verdict),
            "minimum_session_coverage": MINIMUM_SESSION_COVERAGE,
            "material_return_threshold": MATERIAL_RETURN_THRESHOLD,
            "coverage": [entry.to_mapping() for entry in self.coverage],
            "findings": [finding.to_mapping() for finding in self.findings],
            "attestation": self.attestation.to_mapping() if self.attestation else None,
            "corporate_actions": [entry.to_mapping() for entry in self.corporate_actions],
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


def _session_closes(series: BarSeries) -> tuple[tuple[date, float], ...]:
    """One (session_date, close) pair per session — the last bar's close.

    For a daily series this is the identity mapping. For an hourly series it
    recovers the session close, which is the only frame in which a split ratio
    means anything: split-implied returns are daily close-to-close facts, and
    running them over adjacent hourly closes would dilute the discontinuity
    across the ex-date's first hour and key multiple intraday moves onto one
    date, silently masking each other.
    """

    closes: list[tuple[date, float]] = []
    for bar in series.bars:
        if closes and closes[-1][0] == bar.session_date:
            closes[-1] = (bar.session_date, bar.close)
        else:
            closes.append((bar.session_date, bar.close))
    return tuple(closes)


def _material_moves(closes: tuple[tuple[date, float], ...]) -> tuple[tuple[date, float], ...]:
    """Close-to-close returns at or beyond the material threshold, in order."""

    moves: list[tuple[date, float]] = []
    previous_close: float | None = None
    for day, close in closes:
        if previous_close is not None and previous_close > 0:
            change = (close / previous_close) - 1.0
            if abs(change) >= MATERIAL_RETURN_THRESHOLD:
                moves.append((day, change))
        previous_close = close
    return tuple(moves)


def _splits_by_date(actions: Iterable[CorporateAction]) -> dict[date, CorporateAction]:
    return {action.ex_date: action for action in actions if action.kind is ActionKind.SPLIT}


def _reconciles(observed: float, action: CorporateAction) -> bool:
    return abs(observed - _split_implied_return(action.value)) <= SPLIT_RECONCILIATION_TOLERANCE


def _action_identity(action: CorporateAction) -> tuple[str, str, float, str, str]:
    return (
        action.kind.value,
        action.ex_date.isoformat(),
        action.value,
        action.source,
        action.note,
    )


def _action_evidence(
    windows: Sequence[SymbolWindow],
    actions_by_symbol: Mapping[str, Sequence[CorporateAction]],
) -> tuple[tuple[CorporateActionEvidence, ...], tuple[Finding, ...], int]:
    """Bind and count distinct actions inside the exact certification windows."""

    windows_by_symbol: dict[str, list[SymbolWindow]] = {}
    for window in windows:
        windows_by_symbol.setdefault(window.symbol, []).append(window)

    evidence: list[CorporateActionEvidence] = []
    findings: list[Finding] = []
    distinct_total = 0
    for symbol, symbol_windows in sorted(windows_by_symbol.items()):
        actions = tuple(
            action
            for action in actions_by_symbol.get(symbol, ())
            if any(window.start <= action.ex_date <= window.end for window in symbol_windows)
        )
        identities = [_action_identity(action) for action in actions]
        distinct_identities = set(identities)
        duplicate_count = len(identities) - len(distinct_identities)
        if duplicate_count:
            findings.append(
                Finding(
                    kind=FindingKind.DUPLICATE_CORPORATE_ACTION,
                    symbol=symbol,
                    detail=(
                        f"the supplied stream repeats {duplicate_count} corporate-action "
                        "record(s); duplicates cannot increase the attestable sample"
                    ),
                )
            )
        canonical_actions = sorted(
            (action.to_mapping() for action in actions),
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ),
        )
        semantic_bytes = json.dumps(
            canonical_actions, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        distinct_count = len(distinct_identities)
        distinct_total += distinct_count
        evidence.append(
            CorporateActionEvidence(
                symbol=symbol,
                count=distinct_count,
                semantic_sha256=hashlib.sha256(semantic_bytes).hexdigest(),
            )
        )
    return tuple(evidence), tuple(findings), distinct_total


# ------------------------------------------------------------------------------ certify


def certify_export(
    *,
    dataset_id: str,
    windows: Sequence[SymbolWindow],
    series_by_symbol: Mapping[str, BarSeries],
    actions_by_symbol: Mapping[str, Sequence[CorporateAction]],
    attestation: CorporateActionAttestation | NoCorporateActionAttestation | None,
    provider_price_basis: ProviderPriceBasis,
    classified_moves: Sequence[ClassifiedMove] = (),
    holdout_map: Sequence[HoldoutSpan] | None = None,
    calendar: SessionCalendar | None = None,
    interval: BarInterval = BarInterval.DAY_1,
) -> CertificationReport:
    """Judge one export against the frozen gates and return the verdict with evidence.

    ``provider_price_basis`` is REQUIRED and has no default. A default would reintroduce the
    silent assumption ADR-0059 exists to remove: every caller must state how its bytes were
    produced, or fail to compile. It is recorded in the report and does not enforce anything
    downstream.

    DAY_1 coverage is judged at session granularity against the calendar's expected
    sessions. HOUR_1 coverage is judged at BAR granularity against
    ``expected_close_timestamps_utc`` — a session holding one of its seven bars is
    six missing bars, not a covered session — and the 99.5% floor binds the bar
    ratio (D-32 records that reading of the frozen gate). Corporate-action
    reconciliation always runs in the daily close frame: for an hourly export the
    per-session closing series is derived first, because a split ratio implies a
    daily close-to-close return and nothing else. Minute intervals refuse — they
    are vocabulary with no ingestion or certification path.
    """

    if not dataset_id.strip():
        raise CertificationError("dataset_id must be non-empty")
    if interval not in (BarInterval.DAY_1, BarInterval.HOUR_1):
        raise CertificationError(
            f"certification supports {BarInterval.DAY_1} and {BarInterval.HOUR_1}; "
            f"{interval} is interval vocabulary with no ingestion path to certify"
        )
    if not windows:
        raise CertificationError("certification requires at least one symbol window")
    for supplied_symbol, supplied in sorted(series_by_symbol.items()):
        if len(supplied) and supplied.interval is not interval:
            # Judging an hourly export at session granularity marks a session holding
            # one of its seven bars "covered" — the exact blind spot the bar-level
            # path exists to close, minted as a CERTIFIED digest.
            raise CertificationError(
                f"{supplied_symbol}: series interval {supplied.interval} does not match "
                f"the {interval} certification requested — a verdict must judge the bars "
                "it was handed, not a lookalike"
            )

    if holdout_map is not None:
        try:
            validate_holdout_map(
                expected_symbols={window.symbol for window in windows},
                series_by_symbol=series_by_symbol,
                spans=holdout_map,
            )
        except HoldoutMapError as error:
            raise CertificationError(str(error)) from error

    calendar = calendar or SessionCalendar()
    findings: list[Finding] = []
    coverage: list[SymbolCoverage] = []

    action_evidence, action_findings, distinct_action_count = _action_evidence(
        windows, actions_by_symbol
    )
    findings.extend(action_findings)

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
    elif isinstance(attestation, CorporateActionAttestation):
        if distinct_action_count == 0:
            findings.append(
                Finding(
                    kind=FindingKind.EMPTY_ACTION_PANEL,
                    symbol="*",
                    detail=(
                        "a positive sampled-action attestation cannot certify an all-empty "
                        "corporate-action panel; a legitimately action-free panel needs a "
                        "separately reviewed evidence type, not a free-form note"
                    ),
                )
            )
        elif attestation.sampled_action_count > distinct_action_count:
            findings.append(
                Finding(
                    kind=FindingKind.ATTESTATION_EXCEEDS_ACTIONS,
                    symbol="*",
                    detail=(
                        f"the attestation claims {attestation.sampled_action_count} sampled "
                        f"actions but only {distinct_action_count} distinct corporate actions "
                        "were supplied inside the certified windows"
                    ),
                )
            )
    else:
        certified_windows = tuple(
            sorted((window.symbol, window.start, window.end) for window in windows)
        )
        attested_windows = tuple(
            sorted((window.symbol, window.start, window.end) for window in attestation.windows)
        )
        if certified_windows != attested_windows:
            findings.append(
                Finding(
                    kind=FindingKind.NO_ACTION_ATTESTATION_MISMATCH,
                    symbol="*",
                    detail=(
                        "the reviewed no-action attestation does not cover the exact "
                        "symbol windows being certified"
                    ),
                )
            )
        if distinct_action_count:
            findings.append(
                Finding(
                    kind=FindingKind.NO_ACTION_ATTESTATION_CONTRADICTED,
                    symbol="*",
                    detail=(
                        "the reviewed no-action attestation is contradicted by "
                        f"{distinct_action_count} distinct supplied corporate action(s)"
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
            session_days = calendar.sessions(window.start, window.end)
            expected = set(session_days)
            expected_slots: dict[date, tuple[datetime, ...]] = {}
            if interval is BarInterval.HOUR_1:
                expected_slots = {
                    day: calendar.expected_close_timestamps_utc(day) for day in session_days
                }
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

        if interval is BarInterval.HOUR_1:
            observed_by_session: dict[date, set[datetime]] = {}
            for bar in in_window:
                observed_by_session.setdefault(bar.session_date, set()).add(bar.timestamp_utc)
            expected_bar_total = sum(len(slots) for slots in expected_slots.values())
            observed_slot_bars = 0
            missing_bar_ts: list[datetime] = []
            partial_missing: list[tuple[date, datetime]] = []
            for day, slots in expected_slots.items():
                got = observed_by_session.get(day, set())
                for slot in slots:
                    if slot in got:
                        observed_slot_bars += 1
                    else:
                        missing_bar_ts.append(slot)
                        if got:
                            partial_missing.append((day, slot))
            off_slot: list[tuple[date, datetime]] = []
            for day, got in sorted(observed_by_session.items()):
                for ts in sorted(got - set(expected_slots.get(day, ()))):
                    if day in expected:
                        off_slot.append((day, ts))
            entry = SymbolCoverage(
                symbol=symbol,
                expected_sessions=len(expected),
                observed_bars=len(in_window),
                missing_sessions=missing,
                unexpected_bars=unexpected,
                expected_bar_total=expected_bar_total,
                observed_slot_bars=observed_slot_bars,
                missing_bar_timestamps=tuple(sorted(missing_bar_ts)),
                unexpected_bar_timestamps=tuple(ts for _, ts in off_slot),
            )
        else:
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
                    detail=(
                        "the exchange held a session and the export has no bar for it"
                        if interval is BarInterval.DAY_1
                        else "the exchange held a session and the export has no bars for it"
                    ),
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
        if interval is BarInterval.HOUR_1:
            # A fully-empty session is already one MISSING_SESSION finding; per-slot
            # findings are for sessions the export partially covered, so the evidence
            # names the exact bar without seven-fold noise on a wholly missing day.
            for day, slot in partial_missing:
                findings.append(
                    Finding(
                        kind=FindingKind.MISSING_BAR,
                        symbol=symbol,
                        detail="the session ran and the export is missing this bar",
                        session_date=day,
                        timestamp_utc=slot,
                    )
                )
            for day, ts in off_slot:
                findings.append(
                    Finding(
                        kind=FindingKind.UNEXPECTED_BAR,
                        symbol=symbol,
                        detail=(
                            "bar timestamp is not an expected session slot "
                            "(pre/post-market, misaligned, or a phantom extra bar)"
                        ),
                        session_date=day,
                        timestamp_utc=ts,
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
        moves = {day: change for day, change in _material_moves(_session_closes(series))}

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
        corporate_actions=action_evidence,
        provider_price_basis=provider_price_basis,
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
    "CorporateActionEvidence",
    "Finding",
    "FindingKind",
    "NoCorporateActionAttestation",
    "ProviderPriceBasis",
    "ProviderPriceBasisRefused",
    "SymbolCoverage",
    "SymbolWindow",
    "Verdict",
    "admit_provider_price_basis",
    "certify_export",
]

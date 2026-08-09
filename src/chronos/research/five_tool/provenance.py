"""The lookahead-provenance audit: every series a trace consumed is named, and none is future.

The Five-Tool engine has always written three source identifiers onto every bar's trace —
``FiveToolTrace.primary_sequence_id``, ``.benchmark_source_id``, ``.htf_source_id`` — and
until this module nothing read them back.  The merge review of
``codex/five-tool-confluence-v36`` classified them as **disclosed inert** for exactly that
reason (``tests/safety/test_five_tool_inert_fields_disclosed.py``), with the disclosure
saying plainly that they are "an audit trail nothing audits" and that whole-trace equality
in the parity and determinism tests catches nondeterminism only: "two runs of the same code
agree on a wrong identifier exactly as readily as on a right one."

This module is the reader that makes them load-bearing.  The preregistration requires it
before any economic statistic is looked at — ``docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md``,
common campaign test 10: "Deterministic repeat, batch-versus-stream replay, **timestamp
audit, and no-lookahead tests** pass before economic statistics are considered."

**What it proves.**

1. *Attribution.*  Every identifier resolves to a full venue-qualified series identity —
   source, exchange, symbol, interval, session date, and the exact source bar timestamp,
   the shape :func:`chronos.research.five_tool.alignment.source_bar_id` emits.  An empty,
   truncated, or unparseable identifier is refused: a series nobody can name is a series
   nobody can audit.
2. *One series per role.*  Primary, benchmark, and higher-timeframe identifiers each name
   one stable ``(source, exchange, symbol, interval)`` series for the whole trace run.  A
   feed that changes mid-run is refused rather than averaged.
3. *Timestamp consistency.*  Each primary identifier's own timestamp is the bar's
   ``timestamp_utc``; primary bars advance strictly; companion source timestamps never move
   backwards.
4. *No future bytes.*  A benchmark source bar may be at most contemporaneous with its
   primary bar, and a higher-timeframe source bar must be strictly prior — the same
   causality ``FiveToolBarInput`` enforces at construction, re-derived here **from the trace
   alone**.  That independence is the point: a trace is what a campaign keeps, so the audit
   must hold without re-running the aligner that produced it.

**Deliberately stricter than the aligner, in the fail-closed direction.**  The audit also
requires the higher-timeframe series to be the same instrument as the primary at a strictly
longer interval.  ``align_five_tool_inputs`` checks only the interval ordering; Pine's
higher-timeframe request is the same symbol by construction, so an HTF identifier naming a
different instrument is a provenance defect this audit refuses rather than one it inherits.

**What it does not prove.**  It audits the identifiers a run recorded; it cannot see bytes
the engine never attributed, and it says nothing about whether a dataset is certified,
whether a hypothesis is true, or whether any result has economic meaning.  No campaign has
run it: it is the capability common test 10 requires, not evidence that the test passed.

Research only — this module imports the research-plane bar vocabulary and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from chronos.marketdata.bars import BarInterval
from chronos.research.five_tool.alignment import interval_seconds
from chronos.research.five_tool.models import FiveToolTrace

PROVENANCE_AUDIT_SCHEMA_VERSION = "chronos-five-tool-provenance-audit-v1"

_PRIMARY_FIELD = "primary_sequence_id"
_BENCHMARK_FIELD = "benchmark_source_id"
_HTF_FIELD = "htf_source_id"


class ProvenanceAuditError(ValueError):
    """A trace run cannot be shown to be attributed and causal, so it is refused."""


class UnattributedSeries(ProvenanceAuditError):
    """A trace consumed a series it did not name in full venue-qualified form."""


class SeriesAttributionDrift(ProvenanceAuditError):
    """One role's identifiers name more than one series across the run."""


class TimestampInconsistent(ProvenanceAuditError):
    """A recorded source timestamp disagrees with the bar it was recorded on."""


class LookaheadDetected(ProvenanceAuditError):
    """A trace consumed a source bar that had not closed when its primary bar did."""


@dataclass(frozen=True, slots=True)
class SeriesAttribution:
    """The stable identity of one consumed series, independent of any single bar."""

    source: str
    exchange: str
    symbol: str
    interval: str

    def as_payload(self) -> dict[str, str]:
        return {
            "source": self.source,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "interval": self.interval,
        }


@dataclass(frozen=True, slots=True)
class SourceBarIdentity:
    """One parsed source-bar identifier: which series, which session, which instant."""

    series: SeriesAttribution
    session_date: date
    timestamp_utc: datetime


@dataclass(frozen=True, slots=True)
class ProvenanceAuditReport:
    """Content-addressed evidence that one trace run is attributed and causal."""

    bars: int
    first_timestamp_utc: datetime
    last_timestamp_utc: datetime
    primary_series: SeriesAttribution
    benchmark_series: SeriesAttribution | None
    htf_series: SeriesAttribution | None
    attributed_benchmark_bars: int
    attributed_htf_bars: int
    audit_digest: str


def parse_source_bar_id(value: object, *, field: str, bar: int) -> SourceBarIdentity:
    """Resolve one ``source_bar_id`` string, or refuse to guess what it meant."""

    context = f"{field} on bar {bar}"
    if not isinstance(value, str) or not value.strip():
        raise UnattributedSeries(f"{context} is empty; a consumed series must be named")
    parts = value.split(":", 4)
    if len(parts) != 5:
        raise UnattributedSeries(f"{context} is not a venue-qualified source bar id: {value!r}")
    source, exchange, symbol, interval_text, remainder = parts
    for name, part in (("source", source), ("exchange", exchange), ("symbol", symbol)):
        if not part.strip():
            raise UnattributedSeries(f"{context} has an empty {name} segment: {value!r}")
    try:
        interval = BarInterval(interval_text)
    except ValueError as error:
        raise UnattributedSeries(
            f"{context} names an unsupported interval {interval_text!r}"
        ) from error
    session_text, separator, timestamp_text = remainder.partition(":")
    if not separator:
        raise UnattributedSeries(f"{context} has no session date and timestamp: {value!r}")
    try:
        session_date = date.fromisoformat(session_text)
    except ValueError as error:
        raise UnattributedSeries(
            f"{context} has an unparseable session date {session_text!r}"
        ) from error
    try:
        timestamp = datetime.fromisoformat(timestamp_text)
    except ValueError as error:
        raise UnattributedSeries(
            f"{context} has an unparseable source timestamp {timestamp_text!r}"
        ) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise UnattributedSeries(f"{context} source timestamp is not timezone-aware")
    return SourceBarIdentity(
        series=SeriesAttribution(
            source=source,
            exchange=exchange,
            symbol=symbol,
            interval=str(interval),
        ),
        session_date=session_date,
        timestamp_utc=timestamp.astimezone(UTC),
    )


def audit_trace_provenance(traces: Sequence[FiveToolTrace]) -> ProvenanceAuditReport:
    """Audit one trace run's lookahead provenance, refusing on the first defect found."""

    if not traces:
        raise ProvenanceAuditError(
            "a provenance audit requires at least one trace; an empty run proves nothing"
        )

    primary_series: SeriesAttribution | None = None
    benchmark_series: SeriesAttribution | None = None
    htf_series: SeriesAttribution | None = None
    previous_primary: datetime | None = None
    previous_benchmark: datetime | None = None
    previous_htf: datetime | None = None
    benchmark_bars = 0
    htf_bars = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None

    for bar, trace in enumerate(traces):
        primary = parse_source_bar_id(trace.primary_sequence_id, field=_PRIMARY_FIELD, bar=bar)
        if primary_series is None:
            primary_series = primary.series
        elif primary.series != primary_series:
            raise SeriesAttributionDrift(
                f"{_PRIMARY_FIELD} on bar {bar} names {primary.series}, but the run started "
                f"on {primary_series}; one role consumes one series"
            )
        bar_timestamp = trace.timestamp_utc.astimezone(UTC)
        if primary.timestamp_utc != bar_timestamp:
            raise TimestampInconsistent(
                f"{_PRIMARY_FIELD} on bar {bar} is stamped {primary.timestamp_utc.isoformat()} "
                f"but its trace is stamped {bar_timestamp.isoformat()}"
            )
        if previous_primary is not None and primary.timestamp_utc <= previous_primary:
            raise TimestampInconsistent(
                f"{_PRIMARY_FIELD} on bar {bar} does not advance past "
                f"{previous_primary.isoformat()}; primary bars are strictly ordered"
            )
        previous_primary = primary.timestamp_utc
        if first_timestamp is None:
            first_timestamp = primary.timestamp_utc
        last_timestamp = primary.timestamp_utc

        if trace.benchmark_source_id is not None:
            benchmark = parse_source_bar_id(
                trace.benchmark_source_id, field=_BENCHMARK_FIELD, bar=bar
            )
            if benchmark_series is None:
                benchmark_series = benchmark.series
            elif benchmark.series != benchmark_series:
                raise SeriesAttributionDrift(
                    f"{_BENCHMARK_FIELD} on bar {bar} names {benchmark.series}, but the run "
                    f"started on {benchmark_series}; one role consumes one series"
                )
            if benchmark.timestamp_utc > bar_timestamp:
                raise LookaheadDetected(
                    f"{_BENCHMARK_FIELD} on bar {bar} is stamped "
                    f"{benchmark.timestamp_utc.isoformat()}, after its primary bar at "
                    f"{bar_timestamp.isoformat()}; the trace consumed a future bar"
                )
            if previous_benchmark is not None and benchmark.timestamp_utc < previous_benchmark:
                raise TimestampInconsistent(
                    f"{_BENCHMARK_FIELD} on bar {bar} moves back to "
                    f"{benchmark.timestamp_utc.isoformat()} from "
                    f"{previous_benchmark.isoformat()}"
                )
            previous_benchmark = benchmark.timestamp_utc
            benchmark_bars += 1

        if trace.htf_source_id is not None:
            htf = parse_source_bar_id(trace.htf_source_id, field=_HTF_FIELD, bar=bar)
            if htf_series is None:
                _require_higher_timeframe(htf.series, primary.series, bar=bar)
                htf_series = htf.series
            elif htf.series != htf_series:
                raise SeriesAttributionDrift(
                    f"{_HTF_FIELD} on bar {bar} names {htf.series}, but the run started on "
                    f"{htf_series}; one role consumes one series"
                )
            if htf.timestamp_utc >= bar_timestamp:
                raise LookaheadDetected(
                    f"{_HTF_FIELD} on bar {bar} is stamped {htf.timestamp_utc.isoformat()}, "
                    f"not strictly before its primary bar at {bar_timestamp.isoformat()}; a "
                    "higher-timeframe value must come from a prior completed bar"
                )
            if previous_htf is not None and htf.timestamp_utc < previous_htf:
                raise TimestampInconsistent(
                    f"{_HTF_FIELD} on bar {bar} moves back to {htf.timestamp_utc.isoformat()} "
                    f"from {previous_htf.isoformat()}"
                )
            previous_htf = htf.timestamp_utc
            htf_bars += 1

    if (
        primary_series is None or first_timestamp is None or last_timestamp is None
    ):  # pragma: no cover - the empty run is refused before the loop
        raise ProvenanceAuditError("provenance audit observed no attributed primary bar")
    return ProvenanceAuditReport(
        bars=len(traces),
        first_timestamp_utc=first_timestamp,
        last_timestamp_utc=last_timestamp,
        primary_series=primary_series,
        benchmark_series=benchmark_series,
        htf_series=htf_series,
        attributed_benchmark_bars=benchmark_bars,
        attributed_htf_bars=htf_bars,
        audit_digest=_audit_digest(
            bars=len(traces),
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            primary_series=primary_series,
            benchmark_series=benchmark_series,
            htf_series=htf_series,
            benchmark_bars=benchmark_bars,
            htf_bars=htf_bars,
        ),
    )


def _require_higher_timeframe(
    htf: SeriesAttribution,
    primary: SeriesAttribution,
    *,
    bar: int,
) -> None:
    """A higher-timeframe series is the same instrument on a strictly longer interval."""

    if htf.symbol != primary.symbol:
        raise SeriesAttributionDrift(
            f"{_HTF_FIELD} on bar {bar} names instrument {htf.symbol!r}, not the primary "
            f"instrument {primary.symbol!r}; a higher timeframe is the same series"
        )
    if interval_seconds(BarInterval(htf.interval)) <= interval_seconds(
        BarInterval(primary.interval)
    ):
        raise SeriesAttributionDrift(
            f"{_HTF_FIELD} on bar {bar} names interval {htf.interval!r}, which is not longer "
            f"than the primary interval {primary.interval!r}"
        )


def _audit_digest(
    *,
    bars: int,
    first_timestamp: datetime,
    last_timestamp: datetime,
    primary_series: SeriesAttribution,
    benchmark_series: SeriesAttribution | None,
    htf_series: SeriesAttribution | None,
    benchmark_bars: int,
    htf_bars: int,
) -> str:
    payload = {
        "schema_version": PROVENANCE_AUDIT_SCHEMA_VERSION,
        "bars": bars,
        "first_timestamp_utc": first_timestamp.isoformat(),
        "last_timestamp_utc": last_timestamp.isoformat(),
        "primary_series": primary_series.as_payload(),
        "benchmark_series": None if benchmark_series is None else benchmark_series.as_payload(),
        "htf_series": None if htf_series is None else htf_series.as_payload(),
        "attributed_benchmark_bars": benchmark_bars,
        "attributed_htf_bars": htf_bars,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

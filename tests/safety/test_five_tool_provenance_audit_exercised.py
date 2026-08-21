"""The trace's lookahead-provenance identifiers are READ, and they refuse future bytes.

The merge review of ``codex/five-tool-confluence-v36`` found three fields the Five-Tool
engine writes on every bar and nothing read back: ``FiveToolTrace.primary_sequence_id``,
``.benchmark_source_id``, and ``.htf_source_id``. They were classified **disclosed inert**
in ``tests/safety/test_five_tool_inert_fields_disclosed.py`` with the honest reason: they
are "an audit trail nothing audits", and whole-trace equality in the parity and determinism
tests catches nondeterminism only — "two runs of the same code agree on a wrong identifier
exactly as readily as on a right one."

``chronos.research.five_tool.provenance`` is the reader that ends that. The preregistration
requires it before any economic statistic is considered: ``docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md``,
common campaign test 10 — "Deterministic repeat, batch-versus-stream replay, **timestamp
audit, and no-lookahead tests** pass before economic statistics are considered."

Every trace audited below is produced by the **real engine** over aligned bar series, not
hand-assembled, so the identifiers under test are the ones a campaign would actually
record. Each refusal is then driven by doctoring exactly one identifier and repaired, so a
refusal caused by something else cannot pass as this control firing — and doctoring each of
the three fields alone is itself the proof that each of the three is genuinely read.

**Nothing here is a campaign result.** The audit is the capability common test 10 requires;
no hypothesis was tested, no dataset was read, and the bars below are synthetic ramps in a
pytest process.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import align_five_tool_inputs, source_bar_id
from chronos.research.five_tool.engine import evaluate_batch
from chronos.research.five_tool.models import FiveToolSettings, FiveToolTrace
from chronos.research.five_tool.provenance import (
    LookaheadDetected,
    ProvenanceAuditError,
    SeriesAttribution,
    SeriesAttributionDrift,
    TimestampInconsistent,
    UnattributedSeries,
    audit_trace_provenance,
    parse_source_bar_id,
)

_PROVENANCE_FIELDS = ("primary_sequence_id", "benchmark_source_id", "htf_source_id")


def _bars(
    symbol: str,
    timestamps: tuple[datetime, ...],
    interval: BarInterval,
    *,
    source: str = "internal_spec",
    exchange: str | None = None,
) -> BarSeries:
    resolved = exchange or ("AMEX" if symbol == "SPY" else "NYSE")
    return BarSeries(
        symbol=symbol,
        interval=interval,
        bars=tuple(
            Bar(
                symbol=symbol,
                source=source,
                exchange=resolved,
                interval=interval,
                session_date=timestamp.date(),
                timestamp_utc=timestamp,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.5 + index,
                volume=1_000.0,
            )
            for index, timestamp in enumerate(timestamps)
        ),
    )


def _engine_traces() -> tuple[FiveToolTrace, ...]:
    """Hourly primary with a benchmark and a daily higher timeframe, straight from the engine."""

    start = datetime(2024, 1, 2, 15, tzinfo=UTC)
    hourly = tuple(start + timedelta(hours=index) for index in range(30))
    daily = tuple(
        datetime(2024, 1, 1, 21, tzinfo=UTC) + timedelta(days=index) for index in range(3)
    )
    settings = FiveToolSettings.defaults(history_start_utc=hourly[0])
    aligned = align_five_tool_inputs(
        settings,
        _bars("AAA", hourly, BarInterval.HOUR_1),
        _bars("SPY", hourly, BarInterval.HOUR_1),
        higher_timeframe=_bars("AAA", daily, BarInterval.DAY_1),
    )
    return evaluate_batch(settings, aligned)


def _doctored(
    traces: tuple[FiveToolTrace, ...],
    index: int,
    **changes: object,
) -> tuple[FiveToolTrace, ...]:
    return (*traces[:index], dataclasses.replace(traces[index], **changes), *traces[index + 1 :])


# --------------------------------------------------------------------------------------
# The capability: real engine output is attributed, causal, and content-addressed.
# --------------------------------------------------------------------------------------


def test_the_engines_own_traces_are_attributed_and_causal() -> None:
    traces = _engine_traces()
    report = audit_trace_provenance(traces)

    assert report.bars == len(traces) == 30
    assert report.primary_series == SeriesAttribution("internal_spec", "NYSE", "AAA", "1h")
    assert report.benchmark_series == SeriesAttribution("internal_spec", "AMEX", "SPY", "1h")
    assert report.htf_series == SeriesAttribution("internal_spec", "NYSE", "AAA", "1d")
    # Non-vacuity: an audit that passed because nothing was attributed proves nothing.
    assert report.attributed_benchmark_bars == len(traces)
    assert report.attributed_htf_bars == len(traces)
    assert len(report.audit_digest) == 64
    assert report.first_timestamp_utc < report.last_timestamp_utc


def test_the_audit_reads_the_identifiers_rather_than_the_aligned_inputs() -> None:
    """The identifiers are the whole input: a trace is what a campaign keeps."""

    traces = _engine_traces()
    for trace in traces:
        assert trace.primary_sequence_id
        assert trace.benchmark_source_id is not None
        assert trace.htf_source_id is not None
    parsed = parse_source_bar_id(traces[0].primary_sequence_id, field="primary", bar=0)
    assert parsed.timestamp_utc == traces[0].timestamp_utc
    assert parsed.session_date == traces[0].timestamp_utc.date()


def test_the_audit_digest_changes_when_the_attribution_changes() -> None:
    traces = _engine_traces()
    baseline = audit_trace_provenance(traces).audit_digest
    assert audit_trace_provenance(traces).audit_digest == baseline

    shorter = audit_trace_provenance(traces[:-1])
    assert shorter.audit_digest != baseline


def test_an_empty_run_is_refused_rather_than_reported_as_clean() -> None:
    with pytest.raises(ProvenanceAuditError, match="requires at least one trace"):
        audit_trace_provenance(())


# --------------------------------------------------------------------------------------
# Conjunct: an unattributed series is refused — one clause at a time, each field alone.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", _PROVENANCE_FIELDS)
@pytest.mark.parametrize(
    ("shape", "value", "message"),
    [
        ("an empty identifier", "", "is empty"),
        ("whitespace pretending to be an identifier", "   ", "is empty"),
        ("a truncated identifier", "internal_spec:NYSE:AAA", "not a venue-qualified"),
        (
            "an empty venue segment",
            "internal_spec::AAA:1h:2024-01-02:2024-01-02T20:00:00+00:00",
            "has an empty exchange segment",
        ),
        (
            "an interval the repository does not support",
            "internal_spec:NYSE:AAA:3w:2024-01-02:2024-01-02T20:00:00+00:00",
            "names an unsupported interval",
        ),
        (
            "a session date that is not a date",
            "internal_spec:NYSE:AAA:1h:not-a-date:2024-01-02T20:00:00+00:00",
            "unparseable session date",
        ),
        (
            "a source timestamp that is not a timestamp",
            "internal_spec:NYSE:AAA:1h:2024-01-02:not-a-timestamp",
            "unparseable source timestamp",
        ),
        (
            "a naive source timestamp",
            "internal_spec:NYSE:AAA:1h:2024-01-02:2024-01-02T20:00:00",
            "not timezone-aware",
        ),
    ],
)
def test_an_unattributed_identifier_refuses_on_its_own_clause(
    field: str,
    shape: str,
    value: str,
    message: str,
) -> None:
    """Each of the three identifiers is read: doctoring any one of them alone refuses."""

    traces = _engine_traces()
    audit_trace_provenance(traces)  # the control: undoctored engine output passes

    doctored = _doctored(traces, 7, **{field: value})
    with pytest.raises(UnattributedSeries, match=message) as refusal:
        audit_trace_provenance(doctored)
    assert field in str(refusal.value), shape
    assert "bar 7" in str(refusal.value)


@pytest.mark.parametrize("field", ("benchmark_source_id", "htf_source_id"))
def test_a_companion_identifier_that_is_absent_is_allowed_but_never_half_named(
    field: str,
) -> None:
    """Warm-up gaps are legitimate; a present-but-unnameable identifier is not."""

    traces = _engine_traces()
    absent = _doctored(traces, 0, **{field: None})
    report = audit_trace_provenance(absent)
    assert report.bars == len(traces)
    counts = {
        "benchmark_source_id": report.attributed_benchmark_bars,
        "htf_source_id": report.attributed_htf_bars,
    }
    assert counts[field] == len(traces) - 1

    with pytest.raises(UnattributedSeries):
        audit_trace_provenance(_doctored(traces, 0, **{field: "half-named"}))


# --------------------------------------------------------------------------------------
# Conjunct: one role consumes one series, for the whole run.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "field", "replacement"),
    [
        (
            "primary",
            "primary_sequence_id",
            "other_vendor:NYSE:AAA:1h:2024-01-02:2024-01-02T20:00:00+00:00",
        ),
        (
            "benchmark",
            "benchmark_source_id",
            "internal_spec:AMEX:QQQ:1h:2024-01-02:2024-01-02T20:00:00+00:00",
        ),
        (
            "higher timeframe",
            "htf_source_id",
            "internal_spec:NYSE:AAA:1d:2024-01-01:2024-01-01T21:00:00+00:00",
        ),
    ],
)
def test_a_series_that_changes_mid_run_is_refused_rather_than_averaged(
    role: str,
    field: str,
    replacement: str,
) -> None:
    traces = _engine_traces()
    if field == "htf_source_id":
        # The HTF replacement above is the legitimate one, so drift it on the venue.
        replacement = replacement.replace("internal_spec", "other_vendor")
    doctored = _doctored(traces, 9, **{field: replacement})

    with pytest.raises(SeriesAttributionDrift, match="one role consumes one series") as refusal:
        audit_trace_provenance(doctored)
    assert field in str(refusal.value), role

    audit_trace_provenance(traces)


def test_a_higher_timeframe_that_is_not_the_same_instrument_is_refused() -> None:
    """Stricter than the aligner, deliberately: Pine's HTF request is the same series."""

    traces = _engine_traces()
    foreign = "internal_spec:NYSE:BBB:1d:2024-01-01:2024-01-01T21:00:00+00:00"
    with pytest.raises(SeriesAttributionDrift, match="not the primary instrument"):
        audit_trace_provenance(_doctored(traces, 0, htf_source_id=foreign))


def test_a_higher_timeframe_that_is_not_higher_is_refused() -> None:
    traces = _engine_traces()
    same_interval = "internal_spec:NYSE:AAA:1h:2024-01-01:2024-01-01T21:00:00+00:00"
    with pytest.raises(SeriesAttributionDrift, match="not longer than the primary interval"):
        audit_trace_provenance(_doctored(traces, 0, htf_source_id=same_interval))

    shorter = "internal_spec:NYSE:AAA:5m:2024-01-01:2024-01-01T21:00:00+00:00"
    with pytest.raises(SeriesAttributionDrift, match="not longer than the primary interval"):
        audit_trace_provenance(_doctored(traces, 0, htf_source_id=shorter))


# --------------------------------------------------------------------------------------
# Conjunct: timestamp consistency — an identifier must describe the bar it sits on.
# --------------------------------------------------------------------------------------


def test_a_primary_identifier_stamped_for_another_bar_is_refused() -> None:
    traces = _engine_traces()
    borrowed = traces[3].primary_sequence_id
    with pytest.raises(TimestampInconsistent, match="but its trace is stamped"):
        audit_trace_provenance(_doctored(traces, 8, primary_sequence_id=borrowed))

    audit_trace_provenance(traces)


def test_primary_bars_that_do_not_advance_are_refused() -> None:
    traces = _engine_traces()
    stalled = dataclasses.replace(
        traces[5],
        primary_sequence_id=traces[4].primary_sequence_id,
        timestamp_utc=traces[4].timestamp_utc,
    )
    doctored = (*traces[:5], stalled, *traces[6:])
    with pytest.raises(TimestampInconsistent, match="primary bars are strictly ordered"):
        audit_trace_provenance(doctored)


@pytest.mark.parametrize(
    ("field", "index"),
    [("benchmark_source_id", 12), ("htf_source_id", 25)],
)
def test_a_companion_source_that_moves_backwards_is_refused(field: str, index: int) -> None:
    traces = _engine_traces()
    earlier = getattr(traces[0], field)
    assert earlier is not None
    assert getattr(traces[index], field) != earlier, "the control case is vacuous"

    with pytest.raises(TimestampInconsistent, match="moves back to"):
        audit_trace_provenance(_doctored(traces, index, **{field: earlier}))

    audit_trace_provenance(traces)


# --------------------------------------------------------------------------------------
# Conjunct: no future bytes. This is the reason the identifiers exist at all.
# --------------------------------------------------------------------------------------


def test_a_benchmark_bar_from_the_future_is_refused() -> None:
    traces = _engine_traces()
    future = source_bar_id(
        Bar(
            symbol="SPY",
            source="internal_spec",
            exchange="AMEX",
            interval=BarInterval.HOUR_1,
            session_date=traces[10].timestamp_utc.date(),
            timestamp_utc=traces[10].timestamp_utc + timedelta(hours=1),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
    )
    with pytest.raises(LookaheadDetected, match="the trace consumed a future bar"):
        audit_trace_provenance(_doctored(traces, 10, benchmark_source_id=future))

    audit_trace_provenance(traces)


def test_a_higher_timeframe_bar_that_has_not_closed_yet_is_refused() -> None:
    """HTF is strictly prior, not contemporaneous — the aligner's rule, re-derived."""

    traces = _engine_traces()
    contemporaneous = source_bar_id(
        Bar(
            symbol="AAA",
            source="internal_spec",
            exchange="NYSE",
            interval=BarInterval.DAY_1,
            session_date=traces[20].timestamp_utc.date(),
            timestamp_utc=traces[20].timestamp_utc,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
    )
    with pytest.raises(LookaheadDetected, match="must come from a prior completed bar"):
        audit_trace_provenance(_doctored(traces, 20, htf_source_id=contemporaneous))

    # And the benchmark's rule is genuinely the looser one: contemporaneous is allowed.
    same_instant = source_bar_id(
        Bar(
            symbol="SPY",
            source="internal_spec",
            exchange="AMEX",
            interval=BarInterval.HOUR_1,
            session_date=traces[20].timestamp_utc.date(),
            timestamp_utc=traces[20].timestamp_utc,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
    )
    audit_trace_provenance(_doctored(traces, 20, benchmark_source_id=same_instant))


# --------------------------------------------------------------------------------------
# The disclosure this landing had to change, and the honest reason it changed.
# --------------------------------------------------------------------------------------


def test_the_three_identifiers_are_no_longer_disclosed_as_unread() -> None:
    """The inert-fields guard fires in both directions; this pins which direction moved.

    ``test_five_tool_inert_fields_disclosed.py`` failed on this landing with "FiveToolTrace
    field(s) ['benchmark_source_id', 'htf_source_id', 'primary_sequence_id'] are now read",
    which is the guard working: a disclosed field that becomes read must leave the
    disclosure.

    **Corrected 2026-08-18.** ``events`` left the disclosure in the same landing that
    made ``signal_replay`` iterate ``trace.events``; this assertion still required it to
    be present, so it pinned a state the disclosure no longer describes. The remaining
    ``FiveToolTrace`` entries are untouched and still unread.
    """

    from tests.safety.test_five_tool_inert_fields_disclosed import _DISCLOSED_UNREAD

    disclosed = _DISCLOSED_UNREAD[FiveToolTrace]
    assert not (set(_PROVENANCE_FIELDS) & disclosed)
    assert {"bar_index", "warmup_blockers", "long_setup", "short_setup"} <= disclosed

"""Regression pins for the adversarial review of the hourly lane (2026-08-22).

Every test here corresponds to a finding that survived two skeptics. The two that
matter most are self-inflicted: making `Bar.sequence_id` interval-aware silently
disarmed the *accidental* guard that had been keeping hourly series out of the daily
store, and `certify_export` never checked that the bars it was handed were the
interval it was told to judge. Both minted plausible, wrong artifacts.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tests.support.histdata_fakes import FakeHistoricalDataClient

from chronos.histdata.backfill import backfill_hourly_symbol
from chronos.histdata.store import (
    StoreError,
    read_bars,
    write_bars,
    write_hourly_bars,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.marketdata.pacing import PacingController
from chronos.research.certification import (
    CertificationError,
    CorporateActionAttestation,
    SymbolWindow,
    certify_export,
)
from chronos.research.certified_data import (
    CertifiedDataRequest,
    CertifiedDatasetCatalog,
    HoldoutAccessRefused,
)
from chronos.research.dataset_release import (
    DatasetReleaseError,
    HoldoutSpan,
    HoldoutStatus,
    freeze_release,
)
from chronos.research.session_calendar import SessionCalendar

_ET = ZoneInfo("America/New_York")
_CAL = SessionCalendar()
_CAPTURED = "2026-08-22T02:00:00+00:00"
_MON = date(2024, 5, 6)
_FRI = date(2024, 5, 10)


def _hbar(day: date, ts: datetime, price: float = 100.0) -> Bar:
    return Bar(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        source="ibkr",
        exchange="SMART",
        session_date=day,
        timestamp_utc=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        status=BarStatus.CLOSED,
    )


def _hourly(start: date = _MON, end: date = _FRI, *, keep_first_only: bool = False) -> BarSeries:
    bars: list[Bar] = []
    for day in _CAL.sessions(start, end):
        slots = _CAL.expected_close_timestamps_utc(day)
        for slot in slots[:1] if keep_first_only else slots:
            bars.append(_hbar(day, slot))
    return BarSeries("SPY", BarInterval.HOUR_1, tuple(bars))


def _daily(day: date, price: float = 100.0) -> Bar:
    return Bar(
        symbol="SPY",
        interval=BarInterval.DAY_1,
        source="ibkr",
        exchange="SMART",
        session_date=day,
        timestamp_utc=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        status=BarStatus.CLOSED,
    )


def _attest() -> CorporateActionAttestation:
    return CorporateActionAttestation(source_id="probe", sampled_action_count=1, symbols=("SPY",))


# ------------------------------------------------- the guard my own change disarmed


def test_write_bars_refuses_an_hourly_series_explicitly(tmp_path: Path) -> None:
    """Before the interval-aware sequence_id this was refused only by accident.

    All seven bars of a session used to collide into one identifier and trip a
    blocking DUPLICATE_BAR. Distinct intraday identities removed that side effect;
    without an explicit guard the hourly series renders through the daily schema as
    duplicate-date rows and the symbol's daily lane fails on the next read.
    """

    with pytest.raises(StoreError, match="write_bars accepts DAY_1"):
        write_bars(tmp_path, _hourly(_MON, _MON), captured_at=_CAPTURED)
    assert not (tmp_path / "bars" / "SPY.csv").exists()


def test_the_daily_lane_still_reads_after_a_rejected_hourly_write(tmp_path: Path) -> None:
    daily = BarSeries("SPY", BarInterval.DAY_1, (_daily(_MON), _daily(date(2024, 5, 7))))
    write_bars(tmp_path, daily, captured_at=_CAPTURED)
    with pytest.raises(StoreError):
        write_bars(tmp_path, _hourly(_MON, _MON), captured_at=_CAPTURED)
    assert len(read_bars(tmp_path, "SPY")) == 2


# ------------------------------------------- a verdict must judge what it was handed


def test_certification_refuses_an_hourly_series_under_a_daily_verdict() -> None:
    """The blind spot in its purest form: 1 bar of 7 per session, judged as daily.

    Session-granularity coverage marks every session covered, quality passes, and the
    report reads CERTIFIED at 100% over data missing ~86% of its bars.
    """

    with pytest.raises(CertificationError, match="not a lookalike"):
        certify_export(
            dataset_id="chronos-etf-daily-v1",
            windows=[SymbolWindow("SPY", _MON, _FRI)],
            series_by_symbol={"SPY": _hourly(keep_first_only=True)},
            actions_by_symbol={},
            attestation=_attest(),
            calendar=_CAL,
        )


def test_certification_refuses_a_daily_series_under_an_hourly_verdict() -> None:
    daily = BarSeries("SPY", BarInterval.DAY_1, tuple(_daily(d) for d in _CAL.sessions(_MON, _FRI)))
    with pytest.raises(CertificationError, match="does not match"):
        certify_export(
            dataset_id="chronos-etf-hourly-v1",
            windows=[SymbolWindow("SPY", _MON, _FRI)],
            series_by_symbol={"SPY": daily},
            actions_by_symbol={},
            attestation=_attest(),
            calendar=_CAL,
            interval=BarInterval.HOUR_1,
        )


# ------------------------------------------------------- a stored date that lies


def test_a_bar_whose_session_date_contradicts_its_timestamp_is_refused(
    tmp_path: Path,
) -> None:
    """This is the embargo-leak shape: the mask keys on session_date."""

    friday_slot = _CAL.expected_close_timestamps_utc(_FRI)[0]
    liar = _hbar(date(2024, 5, 13), friday_slot)  # claims Monday, trades Friday
    with pytest.raises(StoreError, match="disagreeing date cell"):
        write_hourly_bars(
            tmp_path, BarSeries("SPY", BarInterval.HOUR_1, (liar,)), captured_at=_CAPTURED
        )


def test_honest_hourly_bars_still_write(tmp_path: Path) -> None:
    result = write_hourly_bars(tmp_path, _hourly(_MON, _MON), captured_at=_CAPTURED)
    assert result.rows_added == 7


# ------------------------------------------------------- the bar that is still forming


def test_a_forming_bar_is_never_ingested(tmp_path: Path) -> None:
    """The close cap maps a partial 15:30 bar onto exactly the 16:00 expected slot.

    Fetched mid-session it would certify as the delivered closing bar — the cap
    itself is what hides it, so the coordinator drops bars that have not closed.
    """

    slots = _CAL.expected_close_timestamps_utc(_MON)
    fake = FakeHistoricalDataClient(hourly_by_symbol={"SPY": _hourly(_MON, _MON)})
    fake.connect()
    mid_session = slots[-2]  # the 15:00 close: the 16:00 bar is still forming
    result = backfill_hourly_symbol(
        fake,
        tmp_path,
        "SPY",
        end_date=_MON,
        duration_days=1,
        chunk_days=30,
        pacing=PacingController(),
        now_fn=lambda: mid_session,
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    from chronos.histdata.store import read_hourly_bars

    assert result.rows_added == 6  # the final, unclosed bar is absent
    stored = {bar.timestamp_utc for bar in read_hourly_bars(tmp_path, "SPY").bars}
    assert slots[-1] not in stored
    assert slots[-2] in stored


def test_a_just_closed_bar_is_kept(tmp_path: Path) -> None:
    slots = _CAL.expected_close_timestamps_utc(_MON)
    fake = FakeHistoricalDataClient(hourly_by_symbol={"SPY": _hourly(_MON, _MON)})
    fake.connect()
    result = backfill_hourly_symbol(
        fake,
        tmp_path,
        "SPY",
        end_date=_MON,
        duration_days=1,
        chunk_days=30,
        pacing=PacingController(),
        now_fn=lambda: slots[-1],
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    assert result.rows_added == 7


# ------------------------------------------------------------ empty chunks recorded


def test_empty_chunks_are_recorded_not_merely_skipped(tmp_path: Path) -> None:
    """Three documents claimed this record existed; the code was a bare `continue`."""

    fake = FakeHistoricalDataClient(hourly_by_symbol={"SPY": _hourly(_MON, _FRI)})
    fake.connect()
    result = backfill_hourly_symbol(
        fake,
        tmp_path,
        "SPY",
        end_date=_FRI,
        duration_days=120,
        chunk_days=30,
        pacing=PacingController(),
        now_fn=lambda: datetime(2024, 5, 13, tzinfo=UTC),
        captured_at=_CAPTURED,
        sleep=lambda _: None,
    )
    # Only the newest chunk holds data; the three older ones are named, not silent.
    assert len(result.empty_chunks) == 3
    assert all(isinstance(entry, str) for entry in result.empty_chunks)
    assert result.rows_added == 35


# ------------------------------------------------------------ calendar boundary


@pytest.mark.parametrize("bad", [7, 61, 45])
def test_a_bars_per_hour_that_does_not_divide_sixty_refuses(bad: int) -> None:
    """61 made the step zero: expected_bar_count raised, the slot loop hung forever."""

    with pytest.raises(ValueError, match="divide 60 evenly"):
        _CAL.expected_bar_count(_MON, bars_per_hour=bad)
    with pytest.raises(ValueError, match="divide 60 evenly"):
        _CAL.expected_close_timestamps_utc(_MON, bars_per_hour=bad)


@pytest.mark.parametrize(("per_hour", "count"), [(1, 7), (2, 13), (4, 26), (60, 390)])
def test_divisors_still_work(per_hour: int, count: int) -> None:
    assert _CAL.expected_bar_count(_MON, bars_per_hour=per_hour) == count
    assert len(_CAL.expected_close_timestamps_utc(_MON, bars_per_hour=per_hour)) == count


# ------------------------------------------- the oracle and the parser must agree


def test_parser_output_lands_exactly_on_the_oracle_slots_in_both_dst_regimes() -> None:
    """Two independent derivations of the same closes, with nothing tying them.

    The certification fixtures are generated FROM the oracle, so a shared bug would
    reshape the tests with it. This feeds parser-produced bars to the oracle instead,
    on an EDT date and an EST date — the fixed-offset regression the suite could not
    otherwise see.
    """

    from types import SimpleNamespace

    from chronos.histdata.official_client import _hourly_bar_from_row

    for day, starts in (
        (date(2024, 5, 6), ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]),
        (date(2024, 1, 16), ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]),
        (date(2024, 7, 3), ["09:30", "10:30", "11:30", "12:30"]),  # half-day
    ):
        rows = []
        for start in starts:
            hour, minute = (int(x) for x in start.split(":"))
            dt = datetime.combine(day, datetime.min.time()).replace(
                hour=hour, minute=minute, tzinfo=_ET
            )
            rows.append(
                SimpleNamespace(
                    date=str(int(dt.timestamp())),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1,
                )
            )
        parsed = tuple(_hourly_bar_from_row("SPY", "SMART", r).timestamp_utc for r in rows)
        assert parsed == _CAL.expected_close_timestamps_utc(day), day.isoformat()


def test_an_est_hourly_week_certifies_end_to_end() -> None:
    """Every committed hourly certification date was EDT; this one is winter."""

    start, end = date(2024, 1, 16), date(2024, 1, 19)
    report = certify_export(
        dataset_id="chronos-etf-hourly-v1",
        windows=[SymbolWindow("SPY", start, end)],
        series_by_symbol={"SPY": _hourly(start, end)},
        actions_by_symbol={},
        attestation=_attest(),
        calendar=_CAL,
        interval=BarInterval.HOUR_1,
    )
    assert report.certified is True
    assert report.coverage[0].expected_bar_total == 28  # 4 sessions x 7


# ------------------------------------------------- the hourly holdout actually refuses


def test_an_hourly_clean_span_is_refused_through_the_catalog(tmp_path: Path) -> None:
    """The freeze test read partition bytes off disk, never through the reader seam."""

    report = certify_export(
        dataset_id="chronos-etf-hourly-v1",
        windows=[SymbolWindow("SPY", _MON, _FRI)],
        series_by_symbol={"SPY": _hourly()},
        actions_by_symbol={},
        attestation=_attest(),
        calendar=_CAL,
        interval=BarInterval.HOUR_1,
    )
    release = freeze_release(
        dataset_id="chronos-etf-hourly-v1",
        catalog_id="chronos-etf-hourly-v1-release-001",
        source_id="ibkr-tws-historical",
        source_receipt_sha256="b" * 64,
        certification=report,
        series_by_symbol={"SPY": _hourly()},
        spans=[
            HoldoutSpan("SPY", "train", _MON, date(2024, 5, 8), HoldoutStatus.SEEN),
            HoldoutSpan("SPY", "final-test", date(2024, 5, 9), _FRI, HoldoutStatus.CLEAN),
        ],
        output_root=tmp_path / "release",
    )
    manifest = tmp_path / "catalog.json"
    manifest.write_bytes(release.catalog_manifest_bytes())
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest,
        trusted_manifest_sha256=release.catalog_manifest_sha256,
        dataset_root=tmp_path / "release",
    )
    clean = next(p for p in release.partitions if p.span.status is HoldoutStatus.CLEAN)
    with pytest.raises(HoldoutAccessRefused):
        catalog.resolve_ordinary(
            CertifiedDataRequest(
                dataset_id=clean.dataset_id,
                partition=clean.partition,
                data_version=clean.sha256,
                source_id="ibkr-tws-historical",
                source_receipt_sha256="b" * 64,
            )
        )


def test_a_minute_series_has_no_partition_schema(tmp_path: Path) -> None:
    """certify_export refuses minutes today; the freeze layer must too, so a later
    vocabulary widening cannot mint a release whose bytes cannot round-trip."""

    from chronos.research.dataset_release import _render_partition

    minute = BarSeries(
        "SPY",
        BarInterval.MIN_5,
        (
            Bar(
                symbol="SPY",
                interval=BarInterval.MIN_5,
                source="ibkr",
                exchange="SMART",
                session_date=_MON,
                timestamp_utc=datetime(2024, 5, 6, 14, 35, tzinfo=UTC),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1,
                status=BarStatus.CLOSED,
            ),
        ),
    )
    span = HoldoutSpan("SPY", "s", _MON, _FRI, HoldoutStatus.SEEN)
    with pytest.raises(DatasetReleaseError, match="no partition schema"):
        _render_partition(minute, span)


# ------------------------------------------------------------ evidence shape is pinned


def test_the_v2_evidence_mapping_shape_is_frozen() -> None:
    """A renamed or added field silently re-identifies every future digest.

    This pins the key set, not the values — values move with the data, identity
    must move only with the schema version.
    """

    report = certify_export(
        dataset_id="chronos-etf-hourly-v1",
        windows=[SymbolWindow("SPY", _MON, _FRI)],
        series_by_symbol={"SPY": _hourly()},
        actions_by_symbol={},
        attestation=_attest(),
        calendar=_CAL,
        interval=BarInterval.HOUR_1,
    )
    mapping = json.loads(report.canonical_json())
    assert mapping["schema_version"] == "chronos-dataset-certification-v2"
    assert set(mapping) == {
        "schema_version",
        "dataset_id",
        "interval",
        "verdict",
        "minimum_session_coverage",
        "material_return_threshold",
        "coverage",
        "findings",
        "attestation",
        "classified_moves",
    }
    assert set(mapping["coverage"][0]) == {
        "symbol",
        "expected_sessions",
        "observed_bars",
        "coverage",
        "meets_floor",
        "missing_sessions",
        "unexpected_bars",
        "expected_bar_total",
        "observed_slot_bars",
        "missing_bar_timestamps",
        "unexpected_bar_timestamps",
    }


def test_the_daily_lane_renders_byte_identically(tmp_path: Path) -> None:
    """The merge's ordering key changed (session_date -> timestamp); the daily bytes
    it produces must not have."""

    days = _CAL.sessions(_MON, _FRI)
    series = BarSeries(
        "SPY", BarInterval.DAY_1, tuple(_daily(d, 100.0 + i) for i, d in enumerate(days))
    )
    write_bars(tmp_path, series, captured_at=_CAPTURED)
    rendered = (tmp_path / "bars" / "SPY.csv").read_text()
    assert rendered.splitlines()[0] == "date,open,high,low,close,volume"
    assert rendered.splitlines()[1].startswith("2024-05-06,100.0")
    assert [line.split(",")[0] for line in rendered.splitlines()[1:]] == [
        d.isoformat() for d in days
    ]
    # And a re-write of the same bars is still a pure no-op.
    assert write_bars(tmp_path, series, captured_at=_CAPTURED).rows_added == 0


# ------------------------------------------------------------------ the CLI wiring


def test_the_cli_routes_hourly_to_the_stricter_pacing_window() -> None:
    """Nothing in the suite touched __main__: the --bar-size branch and the 4/min
    choice were pinned by nothing, and a refactor reverting either stays green."""

    from chronos.histdata.__main__ import build_parser

    parser = build_parser()
    hourly = parser.parse_args(["bars", "--symbols", "SPY", "--bar-size", "1h"])
    assert hourly.bar_size == "1h"
    assert hourly.chunk_days == 30
    daily = parser.parse_args(["bars", "--symbols", "SPY"])
    assert daily.bar_size == "1d"

    source = Path("src/chronos/histdata/__main__.py").read_text()
    assert "PacingController(max_per_window=4)" in source, (
        "the hourly branch must keep its stricter window: a chunked backfill at the "
        "6/min default sustains IBKR's ~60/10min ceiling for its whole run"
    )


def test_the_cli_reports_empty_chunks() -> None:
    source = Path("src/chronos/histdata/__main__.py").read_text()
    assert '"empty_chunks"' in source

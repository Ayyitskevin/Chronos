"""Hourly certification judges bars, not sessions — and refuses everything else.

The daily gate's blind spot at hourly granularity is exact: a session holding one
of its seven bars deduplicates to "present" in a session-date set, so a vendor
dropping six bars a day would certify at 100%. Every test here is one direction
of the sharpened gate: the missing bar is named, the off-slot bar is named, the
floor binds the bar ratio, and the split logic runs in the only frame where a
split ratio means anything — derived session closes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.research.certification import (
    CertificationError,
    CorporateActionAttestation,
    FindingKind,
    NoCorporateActionAttestation,
    SymbolWindow,
    Verdict,
    certify_export,
)
from chronos.research.certified_data import CertifiedDatasetCatalog
from chronos.research.dataset_release import (
    DatasetReleaseError,
    HoldoutSpan,
    HoldoutStatus,
    freeze_release,
)
from chronos.research.session_calendar import SessionCalendar

_ET = ZoneInfo("America/New_York")
_CALENDAR = SessionCalendar()
_START = date(2024, 5, 6)
_END = date(2024, 5, 10)  # one full regular week


def _bar(day: date, ts_utc: datetime, price: float) -> Bar:
    return Bar(
        symbol="SPY",
        interval=BarInterval.HOUR_1,
        source="ibkr",
        exchange="SMART",
        session_date=day,
        timestamp_utc=ts_utc,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10_000,
        status=BarStatus.CLOSED,
    )


def _series(
    start: date = _START,
    end: date = _END,
    *,
    drop: set[datetime] | None = None,
    extra: tuple[tuple[date, datetime], ...] = (),
    price_by_day: dict[date, float] | None = None,
) -> BarSeries:
    drop = drop or set()
    prices = price_by_day or {}
    bars: list[Bar] = []
    for day in _CALENDAR.sessions(start, end):
        for slot in _CALENDAR.expected_close_timestamps_utc(day):
            if slot in drop:
                continue
            bars.append(_bar(day, slot, prices.get(day, 100.0)))
    for day, ts in extra:
        bars.append(_bar(day, ts, 100.0))
    bars.sort(key=lambda b: b.timestamp_utc)
    return BarSeries(symbol="SPY", interval=BarInterval.HOUR_1, bars=tuple(bars))


def _no_action_attestation(
    windows: tuple[SymbolWindow, ...],
) -> NoCorporateActionAttestation:
    return NoCorporateActionAttestation(
        source_id="official-sponsor-history-2026-08-26",
        windows=windows,
    )


def _certify(**overrides: object):
    windows = overrides.get("windows", [SymbolWindow("SPY", _START, _END)])
    kwargs: dict[str, object] = {
        "dataset_id": "chronos-etf-hourly-v1",
        "windows": windows,
        "series_by_symbol": {"SPY": _series()},
        "actions_by_symbol": {"SPY": ()},
        "attestation": _no_action_attestation(tuple(windows)),
        "calendar": _CALENDAR,
        "interval": BarInterval.HOUR_1,
    }
    kwargs.update(overrides)
    return certify_export(**kwargs)  # type: ignore[arg-type]


_SLOT = datetime(2024, 5, 7, 16, 30, tzinfo=UTC)  # Tuesday's 12:30 ET close


# ---------------------------------------------------------------------- clean + digest


def test_a_complete_hourly_week_certifies_at_bar_granularity() -> None:
    report = _certify()
    assert report.verdict is Verdict.CERTIFIED
    entry = report.coverage[0]
    assert entry.expected_bar_total == 35  # 5 regular sessions x 7 bars
    assert entry.observed_slot_bars == 35
    assert entry.coverage == 1.0


def test_the_hourly_digest_is_deterministic() -> None:
    assert _certify().certification_digest == _certify().certification_digest


# ------------------------------------------------------------------ the sharpened gate


def test_one_missing_bar_in_a_covered_session_is_named_exactly() -> None:
    """Session-granularity coverage would have called this week complete."""

    report = _certify(series_by_symbol={"SPY": _series(drop={_SLOT})})
    assert report.verdict is Verdict.NOT_CERTIFIED
    missing = [f for f in report.findings if f.kind is FindingKind.MISSING_BAR]
    assert len(missing) == 1
    assert missing[0].timestamp_utc == _SLOT
    assert missing[0].session_date == date(2024, 5, 7)
    entry = report.coverage[0]
    assert entry.observed_slot_bars == 34
    assert entry.missing_bar_timestamps == (_SLOT,)
    # The session set still counts Tuesday as present — which is exactly why the
    # bar dimension, not the session dimension, carries the floor.
    assert entry.missing_sessions == ()


def test_a_wholly_missing_session_reports_once_not_seven_times() -> None:
    tuesday_slots = set(_CALENDAR.expected_close_timestamps_utc(date(2024, 5, 7)))
    report = _certify(series_by_symbol={"SPY": _series(drop=tuesday_slots)})
    session_findings = [f for f in report.findings if f.kind is FindingKind.MISSING_SESSION]
    bar_findings = [f for f in report.findings if f.kind is FindingKind.MISSING_BAR]
    assert [f.session_date for f in session_findings] == [date(2024, 5, 7)]
    assert bar_findings == []
    # ... but every one of the seven bars still counts against the floor.
    assert report.coverage[0].observed_slot_bars == 28
    assert len(report.coverage[0].missing_bar_timestamps) == 7


def test_the_floor_binds_bars_not_sessions() -> None:
    """34/35 bars is ~97.1%: below the frozen 99.5% even though 5/5 sessions have data."""

    report = _certify(series_by_symbol={"SPY": _series(drop={_SLOT})})
    floor = [f for f in report.findings if f.kind is FindingKind.COVERAGE_BELOW_FLOOR]
    assert len(floor) == 1
    assert report.coverage[0].coverage == pytest.approx(34 / 35)


def test_a_pre_market_bar_is_off_slot_with_its_timestamp_named() -> None:
    early = datetime(2024, 5, 7, 13, 0, tzinfo=UTC)  # 09:00 ET — before the open
    report = _certify(series_by_symbol={"SPY": _series(extra=((date(2024, 5, 7), early),))})
    off = [
        f
        for f in report.findings
        if f.kind is FindingKind.UNEXPECTED_BAR and f.timestamp_utc is not None
    ]
    assert len(off) == 1
    assert off[0].timestamp_utc == early
    assert "not an expected session slot" in off[0].detail


def test_a_bar_on_a_weekend_reports_the_closure() -> None:
    saturday = date(2024, 5, 4)
    ts = datetime(2024, 5, 4, 15, 30, tzinfo=UTC)
    report = _certify(
        windows=[SymbolWindow("SPY", saturday, _END)],
        series_by_symbol={"SPY": _series(extra=((saturday, ts),))},
    )
    weekend = [
        f
        for f in report.findings
        if f.kind is FindingKind.UNEXPECTED_BAR and f.session_date == saturday
    ]
    assert len(weekend) == 1
    assert "Weekend" in weekend[0].detail


def test_a_half_day_expects_four_bars_and_a_fifth_is_refused() -> None:
    """July 3, 2024: slots end at 13:00 ET. A 14:00 ET bar is a phantom."""

    day = date(2024, 7, 3)
    phantom = datetime(2024, 7, 3, 18, 0, tzinfo=UTC)  # 14:00 ET
    report = _certify(
        windows=[SymbolWindow("SPY", day, day)],
        series_by_symbol={"SPY": _series(day, day, extra=((day, phantom),))},
    )
    entry = report.coverage[0]
    assert entry.expected_bar_total == 4
    assert entry.observed_slot_bars == 4
    off = [f for f in report.findings if f.timestamp_utc == phantom]
    assert len(off) == 1 and off[0].kind is FindingKind.UNEXPECTED_BAR


def test_an_hourly_window_before_2000_refuses_rather_than_guessing_half_days() -> None:
    """Half-day knowledge is pinned from 2000; a 1999 hourly window cannot be judged."""

    calendar = SessionCalendar(first_covered=date(1998, 1, 1))
    report = _certify(
        calendar=calendar,
        windows=[SymbolWindow("SPY", date(1999, 7, 1), date(1999, 7, 9))],
        series_by_symbol={"SPY": _series()},
    )
    assert [f.kind for f in report.findings] == [FindingKind.CALENDAR_NOT_COVERED]
    assert report.verdict is Verdict.NOT_CERTIFIED


def test_minute_intervals_refuse_as_vocabulary_only() -> None:
    with pytest.raises(CertificationError, match="vocabulary"):
        _certify(interval=BarInterval.MIN_5)


# ------------------------------------------------ corporate actions in the daily frame


def test_a_split_reconciles_against_derived_session_closes() -> None:
    """The ratio implies a close-to-close daily return; hourly closes must first
    be collapsed to one per session or the discontinuity lands on the wrong key."""

    ex_date = date(2024, 5, 8)
    prices = {day: (100.0 if day < ex_date else 25.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(
        series_by_symbol={"SPY": _series(price_by_day=prices)},
        actions_by_symbol={
            "SPY": (
                CorporateAction(kind=ActionKind.SPLIT, ex_date=ex_date, value=4.0, source="ibkr"),
            )
        },
        attestation=CorporateActionAttestation(
            source_id="official-sponsor-history-2026-08-26",
            sampled_action_count=1,
            symbols=("SPY",),
        ),
    )
    assert report.verdict is Verdict.CERTIFIED


def test_an_unexplained_break_in_session_closes_blocks() -> None:
    ex_date = date(2024, 5, 8)
    prices = {day: (100.0 if day < ex_date else 25.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(series_by_symbol={"SPY": _series(price_by_day=prices)})
    moves = [f for f in report.findings if f.kind is FindingKind.UNCLASSIFIED_MATERIAL_MOVE]
    assert [f.session_date for f in moves] == [ex_date]


# --------------------------------------------------------------- freeze and round trip


def _spans() -> list[HoldoutSpan]:
    return [
        HoldoutSpan("SPY", "train", _START, date(2024, 5, 8), HoldoutStatus.SEEN),
        HoldoutSpan("SPY", "final-test", date(2024, 5, 9), _END, HoldoutStatus.CLEAN),
    ]


def test_an_hourly_release_freezes_and_reopens_in_the_reader(tmp_path: Path) -> None:
    report = _certify()
    release = freeze_release(
        dataset_id="chronos-etf-hourly-v1",
        catalog_id="chronos-etf-hourly-v1-release-001",
        source_id="ibkr-tws-historical",
        source_receipt_sha256="b" * 64,
        certification=report,
        series_by_symbol={"SPY": _series()},
        spans=_spans(),
        output_root=tmp_path / "release",
    )
    assert release.interval == "1h"
    assert release.release_document()["interval"] == "1h"

    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_bytes(release.catalog_manifest_bytes())
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=release.catalog_manifest_sha256,
        dataset_root=tmp_path / "release",
    )
    assert catalog.catalog_id == "chronos-etf-hourly-v1-release-001"

    # The frozen partition bytes round-trip through the hourly loader exactly.
    from chronos.marketdata.csv_provider import load_hourly_csv_bytes

    seen = next(p for p in release.partitions if p.span.status is HoldoutStatus.SEEN)
    payload = (tmp_path / "release" / seen.relative_path).read_bytes()
    loaded = load_hourly_csv_bytes(payload, path=tmp_path / "x.csv", symbol="SPY", source="ibkr")
    assert loaded.series.interval is BarInterval.HOUR_1
    assert len(loaded.series) == 21  # 3 sessions x 7 bars in the seen span


def test_a_release_refuses_a_series_that_does_not_match_the_certified_interval(
    tmp_path: Path,
) -> None:
    """Freezing daily bytes over an hourly verdict would mint evidence for a lookalike."""

    daily = BarSeries(
        "SPY",
        BarInterval.DAY_1,
        tuple(
            Bar(
                symbol="SPY",
                interval=BarInterval.DAY_1,
                source="ibkr",
                exchange="SMART",
                session_date=day,
                timestamp_utc=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1,
                status=BarStatus.CLOSED,
            )
            for day in _CALENDAR.sessions(_START, _END)
        ),
    )
    with pytest.raises(DatasetReleaseError, match="never a lookalike"):
        freeze_release(
            dataset_id="chronos-etf-hourly-v1",
            catalog_id="chronos-etf-hourly-v1-release-001",
            source_id="ibkr-tws-historical",
            source_receipt_sha256="b" * 64,
            certification=_certify(),
            series_by_symbol={"SPY": daily},
            spans=_spans(),
            output_root=tmp_path / "release",
        )

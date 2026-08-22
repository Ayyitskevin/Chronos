"""Certification must refuse in every direction it claims to refuse in.

The gates are only worth what their failures are worth, so each test below breaks one
thing and asserts the exact finding — a dropped session, a bar on a closed day, a split
the prices do not show, a price move the action stream does not explain, an export with
no independent sample behind it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.research.certification import (
    MINIMUM_SESSION_COVERAGE,
    CertificationError,
    ClassifiedMove,
    CorporateActionAttestation,
    FindingKind,
    SymbolWindow,
    Verdict,
    certify_export,
)
from chronos.research.session_calendar import SessionCalendar

_START = date(2024, 1, 2)
_END = date(2024, 3, 28)
_CALENDAR = SessionCalendar()


def _bar(symbol: str, session_date: date, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        interval=BarInterval.DAY_1,
        source="ibkr",
        timestamp_utc=datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=21),
        session_date=session_date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
        status=BarStatus.CLOSED,
        exchange="SMART",
    )


def _series(
    symbol: str = "SPY",
    *,
    skip: set[date] | None = None,
    extra: set[date] | None = None,
    closes: dict[date, float] | None = None,
) -> BarSeries:
    skip = skip or set()
    closes = closes or {}
    days = [day for day in _CALENDAR.sessions(_START, _END) if day not in skip]
    days.extend(sorted(extra or set()))
    days.sort()
    return BarSeries(
        symbol=symbol,
        interval=BarInterval.DAY_1,
        bars=tuple(_bar(symbol, day, closes.get(day, 100.0)) for day in days),
    )


def _attestation(symbols: tuple[str, ...] = ("SPY",)) -> CorporateActionAttestation:
    return CorporateActionAttestation(
        source_id="nasdaq-dividend-history-2026-08-21",
        sampled_action_count=12,
        symbols=symbols,
        note="owner sampled 12 actions against a second source",
    )


def _certify(**overrides: object):
    kwargs: dict[str, object] = {
        "dataset_id": "chronos-etf-daily-v1",
        "windows": [SymbolWindow("SPY", _START, _END)],
        "series_by_symbol": {"SPY": _series()},
        "actions_by_symbol": {"SPY": ()},
        "attestation": _attestation(),
        "calendar": _CALENDAR,
    }
    kwargs.update(overrides)
    return certify_export(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- the clean case


def test_a_complete_export_certifies() -> None:
    report = _certify()
    assert report.verdict is Verdict.CERTIFIED
    assert report.certified is True
    assert report.findings == ()
    assert report.coverage[0].coverage == 1.0
    assert report.coverage[0].meets_floor is True


def test_the_digest_is_a_pure_function_of_the_evidence() -> None:
    """No timestamp, no host: certifying the same bytes twice must be identical."""

    first = _certify()
    second = _certify()
    assert first.certification_digest == second.certification_digest
    assert first.canonical_json() == second.canonical_json()


def test_a_different_export_gets_a_different_digest() -> None:
    clean = _certify()
    holed = _certify(series_by_symbol={"SPY": _series(skip={date(2024, 2, 20)})})
    assert clean.certification_digest != holed.certification_digest


# ------------------------------------------------------------------------- coverage


def test_a_dropped_session_is_an_unexplained_gap() -> None:
    missing = date(2024, 2, 20)
    report = _certify(series_by_symbol={"SPY": _series(skip={missing})})
    gaps = [f for f in report.findings if f.kind is FindingKind.MISSING_SESSION]
    assert [f.session_date for f in gaps] == [missing]
    assert report.verdict is Verdict.NOT_CERTIFIED


def test_a_bar_on_a_closed_day_is_refused_and_names_the_closure() -> None:
    """The other error direction — this is what keeps a wrong calendar entry loud."""

    holiday = date(2024, 2, 19)  # Washington's Birthday
    report = _certify(series_by_symbol={"SPY": _series(extra={holiday})})
    unexpected = [f for f in report.findings if f.kind is FindingKind.UNEXPECTED_BAR]
    assert len(unexpected) == 1
    assert unexpected[0].session_date == holiday
    assert "Washington's Birthday" in unexpected[0].detail


def test_coverage_below_the_frozen_floor_blocks() -> None:
    sessions = _CALENDAR.sessions(_START, _END)
    dropped = set(sessions[:5])  # ~8% of a 60-session quarter
    report = _certify(series_by_symbol={"SPY": _series(skip=dropped)})
    floor = [f for f in report.findings if f.kind is FindingKind.COVERAGE_BELOW_FLOOR]
    assert len(floor) == 1
    assert report.coverage[0].coverage < MINIMUM_SESSION_COVERAGE


def test_one_missing_session_in_a_full_year_still_clears_the_floor() -> None:
    """99.5% is a floor, not perfection — but the gap is still reported as a finding.

    The floor only tolerates a gap at realistic dataset length: one missing day out of
    251 is 99.6%, while the same single gap in a 61-session quarter is 98.4% and fails.
    """

    year_start, year_end = date(2024, 1, 2), date(2024, 12, 31)
    days = [day for day in _CALENDAR.sessions(year_start, year_end) if day != date(2024, 2, 20)]
    series = BarSeries(
        symbol="SPY",
        interval=BarInterval.DAY_1,
        bars=tuple(_bar("SPY", day, 100.0) for day in days),
    )
    report = _certify(
        windows=[SymbolWindow("SPY", year_start, year_end)],
        series_by_symbol={"SPY": series},
    )
    assert report.coverage[0].expected_sessions == 252  # 2024 had 252; the export has 251
    assert report.coverage[0].coverage > MINIMUM_SESSION_COVERAGE
    assert report.coverage[0].meets_floor is True
    assert [f.kind for f in report.findings] == [FindingKind.MISSING_SESSION]


def test_an_empty_series_is_refused_not_scored_as_perfect() -> None:
    report = _certify(series_by_symbol={"SPY": BarSeries("SPY", BarInterval.DAY_1, ())})
    assert [f.kind for f in report.findings] == [FindingKind.EMPTY_SERIES]
    assert report.coverage[0].coverage == 0.0


# ---------------------------------------------------------- corporate-action reconciliation


def test_a_split_that_the_prices_confirm_reconciles() -> None:
    ex_date = date(2024, 2, 20)
    closes = {day: (100.0 if day < ex_date else 25.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(
        series_by_symbol={"SPY": _series(closes=closes)},
        actions_by_symbol={
            "SPY": (
                CorporateAction(kind=ActionKind.SPLIT, ex_date=ex_date, value=4.0, source="ibkr"),
            )
        },
    )
    assert report.verdict is Verdict.CERTIFIED


def test_a_declared_split_the_prices_do_not_show_blocks() -> None:
    """Either the bars are already adjusted or the action stream is wrong. Both matter."""

    ex_date = date(2024, 2, 20)
    report = _certify(
        actions_by_symbol={
            "SPY": (
                CorporateAction(kind=ActionKind.SPLIT, ex_date=ex_date, value=4.0, source="ibkr"),
            )
        },
    )
    unreconciled = [f for f in report.findings if f.kind is FindingKind.UNRECONCILED_SPLIT]
    assert len(unreconciled) == 1
    assert unreconciled[0].session_date == ex_date
    assert "no material move" in unreconciled[0].detail


def test_a_price_break_with_no_action_behind_it_blocks() -> None:
    ex_date = date(2024, 2, 20)
    closes = {day: (100.0 if day < ex_date else 25.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(series_by_symbol={"SPY": _series(closes=closes)})
    moves = [f for f in report.findings if f.kind is FindingKind.UNCLASSIFIED_MATERIAL_MOVE]
    assert len(moves) == 1
    assert moves[0].session_date == ex_date


def test_a_split_whose_ratio_disagrees_with_the_prices_blocks() -> None:
    """A 4-for-1 implies -75%. Prices showing -50% mean the two records disagree."""

    ex_date = date(2024, 2, 20)
    closes = {day: (100.0 if day < ex_date else 50.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(
        series_by_symbol={"SPY": _series(closes=closes)},
        actions_by_symbol={
            "SPY": (
                CorporateAction(kind=ActionKind.SPLIT, ex_date=ex_date, value=4.0, source="ibkr"),
            )
        },
    )
    kinds = {f.kind for f in report.findings}
    assert FindingKind.UNRECONCILED_SPLIT in kinds
    assert FindingKind.UNCLASSIFIED_MATERIAL_MOVE in kinds


def test_an_owner_classification_explains_a_genuine_market_move() -> None:
    crash = date(2024, 2, 20)
    closes = {day: (100.0 if day < crash else 70.0) for day in _CALENDAR.sessions(_START, _END)}
    report = _certify(
        series_by_symbol={"SPY": _series(closes=closes)},
        classified_moves=[
            ClassifiedMove(symbol="SPY", session_date=crash, reason="synthetic crash fixture")
        ],
    )
    assert report.verdict is Verdict.CERTIFIED
    assert report.to_mapping()["classified_moves"][0]["reason"] == "synthetic crash fixture"


def test_a_classification_must_actually_explain_something() -> None:
    with pytest.raises(ValueError, match="must explain"):
        ClassifiedMove(symbol="SPY", session_date=date(2024, 2, 20), reason="   ")


# ------------------------------------------------------------------------ attestation


def test_an_export_with_no_independent_sample_is_refused() -> None:
    report = _certify(attestation=None)
    assert [f.kind for f in report.findings] == [FindingKind.MISSING_ATTESTATION]
    assert "self-consistency is not a second source" in report.findings[0].detail


def test_an_attestation_that_skips_a_symbol_does_not_cover_it() -> None:
    report = _certify(attestation=_attestation(symbols=("QQQ",)))
    missing = [f for f in report.findings if f.kind is FindingKind.MISSING_ATTESTATION]
    assert len(missing) == 1
    assert missing[0].symbol == "SPY"


def test_an_attestation_must_carry_a_source_and_a_sample() -> None:
    with pytest.raises(ValueError, match="independent source"):
        CorporateActionAttestation(source_id="  ", sampled_action_count=5, symbols=("SPY",))
    with pytest.raises(ValueError, match="attests nothing"):
        CorporateActionAttestation(source_id="nasdaq", sampled_action_count=0, symbols=("SPY",))
    with pytest.raises(ValueError, match="name the symbols"):
        CorporateActionAttestation(source_id="nasdaq", sampled_action_count=5, symbols=())


# ------------------------------------------------------------- refusals and boundaries


def test_hourly_certification_is_refused_because_nothing_ingests_hourly() -> None:
    with pytest.raises(CertificationError, match="ingests daily bars"):
        _certify(interval=BarInterval.HOUR_1)


def test_certification_needs_a_dataset_id_and_a_window() -> None:
    with pytest.raises(CertificationError, match="dataset_id"):
        _certify(dataset_id="   ")
    with pytest.raises(CertificationError, match="at least one symbol window"):
        _certify(windows=[])


def test_a_window_outside_the_pinned_calendar_refuses_rather_than_scoring() -> None:
    report = _certify(windows=[SymbolWindow("SPY", date(2026, 12, 1), date(2027, 1, 10))])
    assert [f.kind for f in report.findings] == [FindingKind.CALENDAR_NOT_COVERED]
    assert report.verdict is Verdict.NOT_CERTIFIED


def test_an_inverted_window_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="must not follow end"):
        SymbolWindow("SPY", date(2024, 3, 1), date(2024, 1, 1))


def test_multiple_symbols_are_reported_in_a_stable_order() -> None:
    report = certify_export(
        dataset_id="chronos-etf-daily-v1",
        windows=[SymbolWindow("QQQ", _START, _END), SymbolWindow("SPY", _START, _END)],
        series_by_symbol={"SPY": _series("SPY"), "QQQ": _series("QQQ")},
        actions_by_symbol={},
        attestation=_attestation(symbols=("SPY", "QQQ")),
        calendar=_CALENDAR,
    )
    assert [entry.symbol for entry in report.coverage] == ["QQQ", "SPY"]
    assert report.verdict is Verdict.CERTIFIED

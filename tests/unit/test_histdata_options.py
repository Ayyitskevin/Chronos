"""Options forward-capture tests (AI Quant plan C0, ADR-0012).

Covers the snapshot schema + round-trip, the bounded selection (horizon / strike
window / empty-with-reason), the capture coordinator's staleness recording and stored
bounds, the append-only fail-closed store, per-underlying failure isolation, and the
owner-gated official client's not-connected guard.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.support.options_fakes import FakeOptionSnapshotClient

from chronos.histdata.official_options_client import OfficialIBKROptionClient
from chronos.histdata.options import (
    OptionChainSnapshot,
    OptionQuoteRow,
    OptionRight,
    SnapshotQuality,
)
from chronos.histdata.options_capture import capture_snapshot, capture_symbols, select_contracts
from chronos.histdata.options_client import ChainParams, OptionSnapshotError
from chronos.histdata.options_store import (
    OptionStoreConflictError,
    read_snapshot,
    write_snapshot,
)

_SESSION = date(2026, 7, 20)
_CAPTURED = "2026-07-20T21:05:00+00:00"


def _chain(spot: float | None, exp_offsets: list[int], strikes: list[float]) -> ChainParams:
    return ChainParams(
        underlying="SPY",
        expirations=tuple(_SESSION + timedelta(days=d) for d in exp_offsets),
        strikes=tuple(strikes),
        spot=spot,
    )


def _connected(chain: ChainParams, **kw: object) -> FakeOptionSnapshotClient:
    client = FakeOptionSnapshotClient({"SPY": chain}, **kw)  # type: ignore[arg-type]
    client.connect()
    return client


# --- schema -----------------------------------------------------------------


def test_quote_row_validates_and_round_trips() -> None:
    row = OptionQuoteRow(
        expiration=date(2026, 8, 21),
        strike=100.0,
        right=OptionRight.PUT,
        data_quality=SnapshotQuality.DELAYED,
        bid=1.2,
        ask=1.3,
        delta=-0.4,
        implied_volatility=0.22,
    )
    assert OptionQuoteRow.from_mapping(row.to_mapping()) == row
    with pytest.raises(ValueError):
        OptionQuoteRow(
            expiration=date(2026, 8, 21),
            strike=0.0,  # must be > 0
            right=OptionRight.CALL,
            data_quality=SnapshotQuality.LIVE,
        )


def test_snapshot_histogram_and_worst_quality() -> None:
    def _row(q: SnapshotQuality) -> OptionQuoteRow:
        return OptionQuoteRow(date(2026, 8, 21), 100.0, OptionRight.CALL, q)

    snap = OptionChainSnapshot(
        underlying="SPY",
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
        spot=100.0,
        rows=(
            _row(SnapshotQuality.LIVE),
            _row(SnapshotQuality.DELAYED),
            _row(SnapshotQuality.DELAYED),
        ),
    )
    assert snap.quality_histogram() == {"DELAYED": 2, "LIVE": 1}
    assert snap.worst_quality() is SnapshotQuality.DELAYED
    assert OptionChainSnapshot.from_mapping(snap.to_mapping()).rows == snap.rows


def test_empty_snapshot_worst_quality_is_unknown() -> None:
    snap = OptionChainSnapshot(
        underlying="SPY",
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    assert snap.worst_quality() is SnapshotQuality.UNKNOWN
    assert snap.quality_histogram() == {}


# --- bounded selection ------------------------------------------------------


def test_selection_keeps_in_horizon_expirations_and_in_window_strikes() -> None:
    chain = _chain(spot=100.0, exp_offsets=[30, 60, 200], strikes=[80, 95, 100, 105, 120])
    contracts, reason = select_contracts(
        chain, _SESSION, expiry_horizon_days=120, strike_window_pct=0.10
    )
    assert reason == ""
    # Expirations +30, +60 (not +200); strikes 95,100,105 (within +/-10% of 100); x2 rights.
    expirations = {c.expiration for c in contracts}
    strikes = {c.strike for c in contracts}
    assert expirations == {_SESSION + timedelta(days=30), _SESSION + timedelta(days=60)}
    assert strikes == {95.0, 100.0, 105.0}
    assert len(contracts) == 2 * 3 * 2


def test_selection_empty_reasons() -> None:
    far = _chain(spot=100.0, exp_offsets=[200], strikes=[100])
    _, reason = select_contracts(far, _SESSION, expiry_horizon_days=120, strike_window_pct=0.2)
    assert "no expiration" in reason

    no_spot = _chain(spot=None, exp_offsets=[30], strikes=[100])
    _, reason = select_contracts(no_spot, _SESSION, expiry_horizon_days=120, strike_window_pct=0.2)
    assert "spot unavailable" in reason

    off = _chain(spot=1000.0, exp_offsets=[30], strikes=[90, 100, 110])
    _, reason = select_contracts(off, _SESSION, expiry_horizon_days=120, strike_window_pct=0.10)
    assert "no strike" in reason


# --- capture coordinator ----------------------------------------------------


def test_capture_records_rows_bounds_and_staleness(tmp_path: Path) -> None:
    client = _connected(_chain(spot=100.0, exp_offsets=[30, 60], strikes=[95, 100, 105]))
    snap = capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.10,
    )
    assert len(snap.rows) == 2 * 3 * 2
    assert snap.expiry_horizon_days == 120
    assert snap.strike_window_pct == 0.10
    assert snap.spot == 100.0
    assert snap.worst_quality() is SnapshotQuality.DELAYED  # $0-tier labeled, not hidden
    # Rows are deterministically ordered.
    keys = [(r.expiration, r.strike, r.right.value) for r in snap.rows]
    assert keys == sorted(keys)
    # And it was persisted + re-readable.
    assert read_snapshot(tmp_path, "SPY", _SESSION) == snap


def test_capture_empty_horizon_writes_snapshot_with_reason(tmp_path: Path) -> None:
    client = _connected(_chain(spot=100.0, exp_offsets=[200], strikes=[100]))
    snap = capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    assert snap.rows == ()
    assert "no expiration" in snap.reason
    assert read_snapshot(tmp_path, "SPY", _SESSION) is not None  # written, not a silent no-op


# --- store ------------------------------------------------------------------


def test_store_conflict_is_fail_closed_and_correctable(tmp_path: Path) -> None:
    base = _connected(_chain(spot=100.0, exp_offsets=[30], strikes=[100]))
    capture_snapshot(
        tmp_path,
        base,
        "SPY",
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    # A different snapshot for the same date fails closed.
    conflicting = OptionChainSnapshot(
        underlying="SPY",
        captured_at="2026-07-20T22:00:00+00:00",
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
        spot=101.0,
    )
    with pytest.raises(OptionStoreConflictError):
        write_snapshot(tmp_path, _SESSION, conflicting)
    # Unless deliberately superseded.
    write_snapshot(tmp_path, _SESSION, conflicting, allow_correction=True)
    assert read_snapshot(tmp_path, "SPY", _SESSION) == conflicting


def test_recapture_identical_data_with_new_clock_is_a_no_op(tmp_path: Path) -> None:
    # A same-day re-run with identical market data but a fresh capture clock must not
    # false-conflict; the stored snapshot (and its original clock) is left untouched.
    client = _connected(_chain(spot=100.0, exp_offsets=[30], strikes=[100]))
    first = capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at="2026-07-20T21:05:00+00:00",
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at="2026-07-20T21:15:00+00:00",
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    stored = read_snapshot(tmp_path, "SPY", _SESSION)
    assert stored is not None
    assert stored.captured_at == first.captured_at  # no-op: original clock preserved


def test_correction_is_logged_in_manifest(tmp_path: Path) -> None:
    import json

    client = _connected(_chain(spot=100.0, exp_offsets=[30], strikes=[100]))
    capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    snapshots = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    prior_sha = snapshots["symbols"]["SPY"]["options"]["snapshots"][_SESSION.isoformat()]["sha256"]

    conflicting = OptionChainSnapshot(
        underlying="SPY",
        captured_at="2026-07-20T22:00:00+00:00",
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
        spot=101.0,
    )
    write_snapshot(tmp_path, _SESSION, conflicting, allow_correction=True)
    entry = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    superseded = entry["symbols"]["SPY"]["options"]["snapshots"][_SESSION.isoformat()]["superseded"]
    assert len(superseded) == 1
    assert superseded[0]["prior_sha256"] == prior_sha
    assert superseded[0]["prior_captured_at"] == _CAPTURED


def test_boundary_strike_is_not_dropped_by_float_error() -> None:
    # spot=50, pct=0.16 → high edge is 50*1.16 == 57.99999999999999; strike 58 must stay.
    chain = _chain(spot=50.0, exp_offsets=[30], strikes=[58.0])
    contracts, reason = select_contracts(
        chain, _SESSION, expiry_horizon_days=120, strike_window_pct=0.16
    )
    assert reason == ""
    assert {c.strike for c in contracts} == {58.0}


def test_options_cli_reports_connect_failure_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No gateway / no ibapi here: connect() fails; the run reports it and exits 1
    # without a traceback (the docstring's contract).
    from chronos.histdata.__main__ import main

    code = main(["options", "--symbols", "SPY", "--history-root", str(tmp_path)])
    assert code == 1
    assert "connect failed" in capsys.readouterr().out


def test_manifest_records_provenance_and_quality(tmp_path: Path) -> None:
    import json

    client = _connected(_chain(spot=100.0, exp_offsets=[30], strikes=[100]))
    capture_snapshot(
        tmp_path,
        client,
        "SPY",
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    manifest = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    entry = manifest["symbols"]["SPY"]["options"]["snapshots"][_SESSION.isoformat()]
    assert entry["worst_quality"] == "DELAYED"
    assert entry["quality_histogram"] == {"DELAYED": 2}
    assert entry["rows"] == 2
    assert len(entry["sha256"]) == 64


# --- failure isolation + owner-gated client ---------------------------------


def test_capture_symbols_isolates_per_underlying_failures(tmp_path: Path) -> None:
    client = FakeOptionSnapshotClient({"SPY": _chain(spot=100.0, exp_offsets=[30], strikes=[100])})
    client.connect()  # QQQ has no canned chain → its capture errors, SPY still succeeds
    outcomes = capture_symbols(
        tmp_path,
        client,
        ["SPY", "QQQ"],
        session=_SESSION,
        captured_at=_CAPTURED,
        source="ibkr",
        expiry_horizon_days=120,
        strike_window_pct=0.2,
    )
    by_symbol = {o.underlying: o for o in outcomes}
    assert by_symbol["SPY"].snapshot is not None and by_symbol["SPY"].error is None
    assert by_symbol["QQQ"].snapshot is None and by_symbol["QQQ"].error is not None


def test_official_option_client_guards_not_connected() -> None:
    client = OfficialIBKROptionClient()
    with pytest.raises(OptionSnapshotError):
        client.fetch_chain("SPY")
    with pytest.raises(OptionSnapshotError):
        client.fetch_quotes("SPY", ())

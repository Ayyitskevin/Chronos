"""Holdout guardian tests (AI Quant plan C2, ADR-0013 §3/§4).

Pins the fail-closed state machine: an owner-typed correct phrase grants a single-use
unlock; the mediated read is the only unmasking path and burns the window first; a
wrong phrase, undeclared/burned window, outstanding grant, zero budget, expired grant,
consumed grant, or symbol-mismatch all fail closed; and the raw phrase never reaches
the ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from chronos.histdata.holdout import HoldoutWindow, read_embargoed_bars, write_holdouts
from chronos.histdata.store import write_bars
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.registry import (
    REQUIRED_HOLDOUT_UNLOCK_PHRASE,
    HoldoutGuardianError,
    RegistryLedger,
    is_burned,
    mediated_holdout_read,
    request_unlock,
)

_T0 = datetime(2026, 7, 20, 21, 0, tzinfo=UTC)
_WINDOW = "holdout_2024_jun"
_CAPTURED = "2026-07-20T21:00:00+00:00"


def _bar(d: date, close: float) -> Bar:
    return Bar(
        symbol="SPY",
        source="ibkr",
        exchange="SMART",
        interval=BarInterval.DAY_1,
        session_date=d,
        timestamp_utc=datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


def _setup(root: Path) -> RegistryLedger:
    """A history root with one June holdout window and bars straddling it."""

    write_holdouts(
        root,
        (HoldoutWindow(_WINDOW, date(2024, 6, 1), date(2024, 6, 30), symbols=("SPY",)),),
    )
    series = BarSeries(
        symbol="SPY",
        interval=BarInterval.DAY_1,
        bars=(
            _bar(date(2024, 5, 15), 10),
            _bar(date(2024, 6, 14), 11),
            _bar(date(2024, 7, 15), 12),
        ),
    )
    write_bars(root, series, captured_at=_CAPTURED)
    return RegistryLedger(root / "registry.jsonl")


def _grant(ledger: RegistryLedger, root: Path, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "typed_phrase": REQUIRED_HOLDOUT_UNLOCK_PHRASE,
        "reason": "validation run",
        "now": _T0,
        "accrued_sessions": 40,
        "ttl_minutes": 15,
        "sessions_per_unlock": 20,
        "max_outstanding_unlocks": 2,
    }
    kwargs.update(overrides)
    return request_unlock(ledger, root, _WINDOW, **kwargs)  # type: ignore[arg-type]


def test_happy_path_unlock_then_read_burns_the_window(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    # Default (masked) read drops the June bar.
    assert len(read_embargoed_bars(tmp_path, "SPY")) == 2

    grant = _grant(ledger, tmp_path)
    series = mediated_holdout_read(
        ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1)
    )
    assert len(series) == 3  # unmasked: the June bar is now visible
    assert is_burned(ledger, _WINDOW)
    ok, detail = ledger.verify()
    assert ok, detail


def test_wrong_phrase_is_refused_and_records_nothing(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    with pytest.raises(HoldoutGuardianError, match="phrase mismatch"):
        _grant(ledger, tmp_path, typed_phrase="not the phrase")
    assert ledger.records() == ()  # nothing appended on a failed unlock


def test_phrase_never_reaches_the_ledger(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    _grant(ledger, tmp_path, reason="a reason")
    blob = ledger.path.read_text(encoding="utf-8")
    assert REQUIRED_HOLDOUT_UNLOCK_PHRASE not in blob
    assert "a reason" in blob  # the reason is logged, the phrase is not


def test_undeclared_window_is_refused(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    with pytest.raises(HoldoutGuardianError, match="not declared"):
        request_unlock(
            ledger,
            tmp_path,
            "no_such_window",
            typed_phrase=REQUIRED_HOLDOUT_UNLOCK_PHRASE,
            reason="x",
            now=_T0,
            accrued_sessions=40,
            ttl_minutes=15,
            sessions_per_unlock=20,
            max_outstanding_unlocks=2,
        )


def test_zero_budget_is_refused(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    with pytest.raises(HoldoutGuardianError, match="budget"):
        _grant(ledger, tmp_path, accrued_sessions=0)


def test_grant_is_single_use(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1))
    with pytest.raises(HoldoutGuardianError, match="already"):
        mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=2))


def test_burned_window_cannot_be_reunlocked(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1))
    with pytest.raises(HoldoutGuardianError, match="burned"):
        _grant(ledger, tmp_path, now=_T0 + timedelta(minutes=3))


def test_expired_grant_is_refused(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path, ttl_minutes=15)
    with pytest.raises(HoldoutGuardianError, match="expired"):
        mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=20))


def test_outstanding_grant_blocks_a_second_unlock(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    _grant(ledger, tmp_path)
    with pytest.raises(HoldoutGuardianError, match="outstanding"):
        _grant(ledger, tmp_path)


def test_symbol_not_covered_by_window_is_refused(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    with pytest.raises(HoldoutGuardianError, match="does not cover"):
        mediated_holdout_read(ledger, tmp_path, "QQQ", grant=grant, now=_T0 + timedelta(minutes=1))


def test_guardian_refuses_on_a_tampered_ledger(tmp_path: Path) -> None:
    # The guardian verifies the chain before trusting it (review F2).
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["payload"]["window"] = "x"  # in-place edit → chain break
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HoldoutGuardianError, match="verification"):
        mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1))


def test_truncating_a_burn_is_caught_not_silently_unburned(tmp_path: Path) -> None:
    # The exact M5 exploit: burn a window, then drop the trailing consume line to
    # un-burn it. The head anchor makes verify() catch the truncation, and the guardian
    # refuses to act on the unverified ledger rather than re-unlocking a spent window.
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1))
    assert is_burned(ledger, _WINDOW)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # drop the consume
    with pytest.raises(HoldoutGuardianError, match="verification"):
        _grant(ledger, tmp_path, now=_T0 + timedelta(minutes=3))

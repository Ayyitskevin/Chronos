"""Holdout guardian tests (AI Quant plan C2, ADR-0013 §3/§4).

Pins the fail-closed state machine: an owner-typed correct phrase grants a single-use
unlock; the mediated read is the only unmasking path and burns the window first; a
wrong phrase, undeclared/burned window, outstanding grant, zero budget, expired grant,
consumed grant, or symbol-mismatch all fail closed; and the raw phrase never reaches
the ledger.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import chronos.registry.holdout_guardian as guardian_module
from chronos.histdata.holdout import HoldoutWindow, read_embargoed_bars, write_holdouts
from chronos.histdata.store import write_bars
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.registry import (
    REQUIRED_HOLDOUT_UNLOCK_PHRASE,
    CanonicalTrialRegistry,
    HoldoutGuardianError,
    RegistryLedger,
    RunStage,
    UnlockGrant,
    burned_windows,
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


def _grant(
    ledger: RegistryLedger,
    root: Path,
    *,
    window_name: str = _WINDOW,
    **overrides: object,
) -> object:
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
    return request_unlock(ledger, root, window_name, **kwargs)  # type: ignore[arg-type]


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
    unlock, consume = ledger.records()
    binding_keys = {
        "window_definition",
        "window_definition_sha256",
        "window_scope_sha256",
        "data_identity_sha256",
        "holdout_set_sha256",
    }
    assert {key: unlock.payload[key] for key in binding_keys} == {
        key: consume.payload[key] for key in binding_keys
    }


def test_guardian_recovers_a_fresh_writer_after_a_canonical_trial_append(
    tmp_path: Path,
) -> None:
    ledger = _setup(tmp_path)  # Its AuditLog head is now deliberately stale.
    registry = CanonicalTrialRegistry._for_tests(ledger.path)
    registry.start_trial(
        trial_id="trial-1",
        campaign_id="campaign-1",
        campaign_manifest_sha256="a" * 64,
        stage=RunStage.DEV,
        strategy_id="five-tool",
        config_hash="b" * 64,
        code_commit="c" * 40,
        data_hashes={"dataset": {"sha256": "d" * 64}},
        criteria_ref="criteria@v1",
    )

    grant = _grant(ledger, tmp_path)
    mediated_holdout_read(ledger, tmp_path, "SPY", grant=grant, now=_T0 + timedelta(minutes=1))

    records = RegistryLedger(ledger.path).records()
    assert [record.sequence for record in records] == [0, 1, 2]
    assert [record.kind for record in records] == [
        "trial_started",
        "holdout_unlock",
        "holdout_consume",
    ]
    assert RegistryLedger(ledger.path).verify()[0] is True


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


@pytest.mark.parametrize("field", ["window", "expires_at"])
def test_grant_must_match_the_durable_unlock_record_exactly(
    tmp_path: Path,
    field: str,
) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    assert isinstance(grant, UnlockGrant)
    forged = replace(
        grant,
        **(
            {"window": "different-window"}
            if field == "window"
            else {"expires_at": "2099-01-01T00:00:00+00:00"}
        ),
    )

    with pytest.raises(HoldoutGuardianError, match="disagrees"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=forged,
            now=_T0 + timedelta(minutes=1),
        )
    assert not is_burned(ledger, _WINDOW)


def test_grant_refuses_same_name_redeclared_to_a_different_window(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    write_holdouts(
        tmp_path,
        (
            HoldoutWindow(
                _WINDOW,
                date(2024, 7, 1),
                date(2024, 7, 31),
                symbols=("SPY",),
            ),
        ),
    )

    with pytest.raises(HoldoutGuardianError, match="definition or stored data changed"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_grant_refuses_removal_of_another_masked_window(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    june = HoldoutWindow(
        _WINDOW,
        date(2024, 6, 1),
        date(2024, 6, 30),
        symbols=("SPY",),
    )
    july = HoldoutWindow(
        "holdout_2024_jul",
        date(2024, 7, 1),
        date(2024, 7, 31),
        symbols=("SPY",),
    )
    write_holdouts(tmp_path, (june, july))
    grant = _grant(ledger, tmp_path)
    write_holdouts(tmp_path, (june,))

    with pytest.raises(HoldoutGuardianError, match="definition or stored data changed"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_lowercase_symbol_reads_the_same_canonical_bytes_that_were_bound(
    tmp_path: Path,
) -> None:
    ledger = _setup(tmp_path)
    canonical = tmp_path / "bars" / "SPY.csv"
    decoy = tmp_path / "bars" / "spy.csv"
    decoy.write_text(
        canonical.read_text(encoding="utf-8").replace("11.0", "999.0"), encoding="utf-8"
    )
    grant = _grant(ledger, tmp_path)

    series = mediated_holdout_read(
        ledger,
        tmp_path,
        "spy",
        grant=grant,
        now=_T0 + timedelta(minutes=1),
    )
    assert series.symbol == "SPY"
    assert [bar.close for bar in series] == [10.0, 11.0, 12.0]


def test_replacing_bar_path_during_held_fd_read_cannot_return_attacker_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    canonical = tmp_path / "bars" / "SPY.csv"
    displaced = tmp_path / "bars" / "SPY.original"
    attacker = tmp_path / "bars" / "attacker.csv"
    attacker.write_text(
        canonical.read_text(encoding="utf-8").replace("11.0", "999.0"),
        encoding="utf-8",
    )
    real_read_all = guardian_module._read_all
    attack_armed = True

    def replace_while_reading(descriptor: int) -> bytes:
        nonlocal attack_armed
        if not attack_armed:
            return real_read_all(descriptor)
        attack_armed = False
        canonical.rename(displaced)
        attacker.rename(canonical)
        try:
            return real_read_all(descriptor)
        finally:
            canonical.rename(attacker)
            displaced.rename(canonical)

    monkeypatch.setattr(guardian_module, "_read_all", replace_while_reading)

    with pytest.raises(HoldoutGuardianError, match="changed during its read"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_in_place_bar_mutation_during_read_is_refused_before_burn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    canonical = tmp_path / "bars" / "SPY.csv"
    original = canonical.read_bytes()
    attacker = original.replace(b"11.0", b"99.0")
    assert len(attacker) == len(original)
    real_read_all = guardian_module._read_all
    attack_armed = True

    def mutate_while_reading(descriptor: int) -> bytes:
        nonlocal attack_armed
        if not attack_armed:
            return real_read_all(descriptor)
        attack_armed = False
        canonical.write_bytes(attacker)
        try:
            return real_read_all(descriptor)
        finally:
            canonical.write_bytes(original)

    monkeypatch.setattr(guardian_module, "_read_all", mutate_while_reading)

    with pytest.raises(HoldoutGuardianError, match="changed during its read"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_fifo_swap_at_bar_open_is_nonblocking_and_refused_before_burn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    canonical = tmp_path / "bars" / "SPY.csv"
    displaced = tmp_path / "bars" / "SPY.original"
    real_open = os.open
    attack_armed = True

    def swap_fifo_before_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attack_armed
        if attack_armed and path == canonical.name and dir_fd is not None:
            attack_armed = False
            assert flags & getattr(os, "O_NONBLOCK", 0)
            canonical.rename(displaced)
            os.mkfifo(canonical, mode=0o600)
            try:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                canonical.unlink()
                displaced.rename(canonical)
            return descriptor
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(guardian_module.os, "open", swap_fifo_before_open)

    with pytest.raises(HoldoutGuardianError, match="not a regular file"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_burned_scope_cannot_be_redeclared_under_a_new_name(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    mediated_holdout_read(
        ledger,
        tmp_path,
        "SPY",
        grant=grant,
        now=_T0 + timedelta(minutes=1),
    )
    write_holdouts(
        tmp_path,
        (
            HoldoutWindow(
                "renamed_june",
                date(2024, 6, 1),
                date(2024, 6, 30),
                symbols=("SPY",),
            ),
        ),
    )

    with pytest.raises(HoldoutGuardianError, match="overlaps already-burned scope"):
        _grant(
            ledger,
            tmp_path,
            window_name="renamed_june",
            now=_T0 + timedelta(minutes=2),
        )
    assert [record.kind for record in ledger.records()] == [
        "holdout_unlock",
        "holdout_consume",
    ]


def test_grant_is_bound_to_stored_bar_bytes(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    grant = _grant(ledger, tmp_path)
    path = tmp_path / "bars" / "SPY.csv"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(HoldoutGuardianError, match="definition or stored data changed"):
        mediated_holdout_read(
            ledger,
            tmp_path,
            "SPY",
            grant=grant,
            now=_T0 + timedelta(minutes=1),
        )
    assert [record.kind for record in ledger.records()] == ["holdout_unlock"]


def test_unlock_reveals_only_its_window_and_keeps_other_same_symbol_holdouts_masked(
    tmp_path: Path,
) -> None:
    ledger = _setup(tmp_path)
    write_holdouts(
        tmp_path,
        (
            HoldoutWindow(
                _WINDOW,
                date(2024, 6, 1),
                date(2024, 6, 30),
                symbols=("SPY",),
            ),
            HoldoutWindow(
                "holdout_2024_jul",
                date(2024, 7, 1),
                date(2024, 7, 31),
                symbols=("SPY",),
            ),
        ),
    )
    grant = _grant(ledger, tmp_path)
    series = mediated_holdout_read(
        ledger,
        tmp_path,
        "SPY",
        grant=grant,
        now=_T0 + timedelta(minutes=1),
    )

    assert [bar.session_date for bar in series] == [date(2024, 5, 15), date(2024, 6, 14)]
    assert is_burned(ledger, _WINDOW)
    assert not is_burned(ledger, "holdout_2024_jul")


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


def test_public_guardian_queries_translate_a_malformed_last_line(tmp_path: Path) -> None:
    ledger = _setup(tmp_path)
    _grant(ledger, tmp_path)
    ledger.path.write_text("{malformed-json\n", encoding="utf-8")

    with pytest.raises(HoldoutGuardianError, match="unreadable"):
        burned_windows(ledger)
    with pytest.raises(HoldoutGuardianError, match="unreadable"):
        is_burned(ledger, _WINDOW)


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
        burned_windows(ledger)
    with pytest.raises(HoldoutGuardianError, match="verification"):
        is_burned(ledger, _WINDOW)
    with pytest.raises(HoldoutGuardianError, match="verification"):
        _grant(ledger, tmp_path, now=_T0 + timedelta(minutes=3))

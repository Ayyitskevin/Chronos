"""Milestone 6 live safety layer: arming, kill switch, drawdown, gate stack.

These assert the fail-closed behavior of each component in isolation, and that
each live gate independently blocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread

import pytest

from chronos.orders.arming import (
    REQUIRED_ARM_PHRASE,
    LiveArmingError,
    LiveArmingService,
)
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.live_gate import (
    LIVE_GATE_NAMES,
    LiveGateInputs,
    evaluate_live_gates,
)
from chronos.orders.session_drawdown import SessionDrawdownBreaker
from chronos.orders.state_generation import (
    KILL_SWITCH,
    SESSION_DRAWDOWN,
    CorruptStateGeneration,
    StateGenerationMarker,
)

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


# --- arming ---------------------------------------------------------------


def test_arm_requires_exact_phrase() -> None:
    service = LiveArmingService(ttl_minutes=15)
    with pytest.raises(LiveArmingError):
        service.arm("i accept live trading risk", now=_NOW)  # wrong case
    assert not service.is_armed(now=_NOW)


def test_arm_non_ascii_phrase_rejected_cleanly() -> None:
    # A non-ASCII phrase must be a clean mismatch, not a TypeError from
    # hmac.compare_digest (which would surface as a 500 in the API).
    service = LiveArmingService(ttl_minutes=15)
    with pytest.raises(LiveArmingError):
        service.arm("I ACCEPT LIVE TRADING RÌSK", now=_NOW)
    assert not service.is_armed(now=_NOW)


def test_arm_requires_timezone_aware_now() -> None:
    service = LiveArmingService(ttl_minutes=15)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.arm(REQUIRED_ARM_PHRASE, now=datetime(2026, 7, 17, 15, 0))  # noqa: DTZ001


def test_arm_grants_then_expires() -> None:
    service = LiveArmingService(ttl_minutes=15)
    service.arm(REQUIRED_ARM_PHRASE, now=_NOW)
    assert service.is_armed(now=_NOW)
    assert service.is_armed(now=_NOW + timedelta(minutes=14))
    assert not service.is_armed(now=_NOW + timedelta(minutes=15))
    assert not service.is_armed(now=_NOW + timedelta(minutes=16))


def test_arm_revoked_before_expiry() -> None:
    service = LiveArmingService(ttl_minutes=15)
    service.arm(REQUIRED_ARM_PHRASE, now=_NOW)
    service.revoke(now=_NOW + timedelta(minutes=1))
    assert not service.is_armed(now=_NOW + timedelta(minutes=2))


def test_arm_audit_never_receives_the_phrase() -> None:
    events: list[dict[str, object]] = []

    class _Sink:
        def record(self, *, event: str, reason: str, occurred_at: datetime) -> None:
            events.append({"event": event, "reason": reason})

    service = LiveArmingService(ttl_minutes=15, audit=_Sink())
    service.arm(REQUIRED_ARM_PHRASE, now=_NOW, reason="operator arm")
    assert events == [{"event": "armed", "reason": "operator arm"}]
    # The raw phrase appears in no audit payload.
    assert all(REQUIRED_ARM_PHRASE not in str(e) for e in events)


# --- kill switch ----------------------------------------------------------


def test_kill_switch_fresh_is_disengaged(tmp_path: Path) -> None:
    assert not LiveKillSwitch(tmp_path / "ks.json").is_engaged()


def test_kill_switch_engage_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "ks.json"
    LiveKillSwitch(path).engage(reason="halt", initiated_by="op", now=_NOW)
    assert LiveKillSwitch(path).is_engaged()  # a fresh instance sees it


def test_kill_switch_disengage_requires_note(tmp_path: Path) -> None:
    switch = LiveKillSwitch(tmp_path / "ks.json")
    switch.engage(reason="halt", initiated_by="op", now=_NOW)
    with pytest.raises(ValueError, match="operator note"):
        switch.disengage(operator_note="  ", initiated_by="op", now=_NOW)
    switch.disengage(operator_note="reviewed and clear", initiated_by="op", now=_NOW)
    assert not switch.is_engaged()


def test_kill_switch_disengage_audit_failure_remains_engaged(tmp_path: Path) -> None:
    class _Audit:
        def record(
            self,
            *,
            action: str,
            initiated_by: str,
            detail: dict[str, object],
            occurred_at: datetime,
        ) -> None:
            del initiated_by, detail, occurred_at
            if action == "disengage":
                raise RuntimeError("audit unavailable")

    switch = LiveKillSwitch(tmp_path / "ks.json", audit=_Audit())
    switch.engage(reason="halt", initiated_by="op", now=_NOW)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        switch.disengage(operator_note="reviewed", initiated_by="op", now=_NOW)
    assert switch.is_engaged()


def test_kill_switch_corrupt_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ks.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert LiveKillSwitch(path).is_engaged()  # fail closed


def test_kill_switch_engage_requires_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty reason"):
        LiveKillSwitch(tmp_path / "ks.json").engage(reason="  ", initiated_by="op", now=_NOW)


def test_kill_switch_engagement_waits_for_synchronous_send(tmp_path: Path) -> None:
    switch = LiveKillSwitch(tmp_path / "ks.json")
    engage_started = Event()
    engage_finished = Event()

    def engage() -> None:
        engage_started.set()
        switch.engage(reason="operator halt", initiated_by="op", now=_NOW)
        engage_finished.set()

    with switch.at_send() as authorized:
        assert authorized is True
        thread = Thread(target=engage)
        thread.start()
        assert engage_started.wait(timeout=1)
        assert not engage_finished.wait(timeout=0.05)

    assert engage_finished.wait(timeout=1)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert switch.is_engaged()


# --- session drawdown -----------------------------------------------------


def _breaker(tmp_path: Path, kill_switch: LiveKillSwitch | None = None) -> SessionDrawdownBreaker:
    return SessionDrawdownBreaker(
        tmp_path / "baseline.json",
        max_drawdown_usd=Decimal("1000"),
        max_drawdown_pct=Decimal("0.02"),
        market_timezone="America/New_York",
        kill_switch=kill_switch,
    )


def test_drawdown_establishes_baseline_and_persists(tmp_path: Path) -> None:
    breaker = _breaker(tmp_path)
    first = breaker.check(Decimal("100000"), now=_NOW)
    assert not first.breached
    assert first.baseline_nlv == Decimal("100000")
    # A fresh breaker on the same file keeps the same session baseline.
    later = _breaker(tmp_path).check(Decimal("99900"), now=_NOW + timedelta(minutes=5))
    assert later.baseline_nlv == Decimal("100000")


def test_drawdown_breach_engages_kill_switch(tmp_path: Path) -> None:
    switch = LiveKillSwitch(tmp_path / "ks.json")
    breaker = _breaker(tmp_path, kill_switch=switch)
    breaker.check(Decimal("100000"), now=_NOW)  # baseline
    decision = breaker.check(Decimal("98500"), now=_NOW)  # -1500 breaches both limits
    assert decision.breached
    assert switch.is_engaged()


# --- R-66: a lost state file is not a fresh install -------------------------
#
# Both readers answered a missing file with the permissive reading, and the two
# cases -- "never written" and "written, then lost" -- are indistinguishable from
# absence alone. The installation marker distinguishes them, so these exercise the
# loss, the fresh install that must stay permissive, and the marker's own failure.


def test_a_kill_switch_file_lost_after_engagement_reads_engaged(tmp_path: Path) -> None:
    """Delete an engaged switch's file and the switch must still read ENGAGED.

    Before the marker this restored trading: absence was the fresh-deploy case,
    so the emergency stop an operator had pulled simply evaporated.
    """

    path = tmp_path / "ks.json"
    switch = LiveKillSwitch(path)
    switch.engage(reason="operator stop", initiated_by="operator", now=_NOW)
    assert switch.is_engaged()

    path.unlink()

    state = LiveKillSwitch(path).read()
    assert state.engaged is True
    assert "missing but this installation wrote one" in state.reason


def test_a_kill_switch_file_lost_after_a_disengagement_also_reads_engaged(
    tmp_path: Path,
) -> None:
    """Materialisation is about the file existing, not about its last value."""

    path = tmp_path / "ks.json"
    switch = LiveKillSwitch(path)
    switch.engage(reason="operator stop", initiated_by="operator", now=_NOW)
    switch.disengage(operator_note="resolved", initiated_by="operator", now=_NOW)
    assert not switch.is_engaged()

    path.unlink()

    assert LiveKillSwitch(path).is_engaged() is True


def test_a_fresh_install_kill_switch_is_still_disengaged(tmp_path: Path) -> None:
    """Guard the guard: nothing written, nothing lost, and the default holds.

    A rule that read ENGAGED for every missing file would pass the two tests
    above and make every fresh deploy boot killed.
    """

    switch = LiveKillSwitch(tmp_path / "ks.json")
    assert switch.is_engaged() is False
    assert switch.is_engaged() is False  # and still, on the second read
    assert not (tmp_path / "state_generation.json").exists()


def test_a_baseline_lost_mid_session_refuses_instead_of_re_baselining(
    tmp_path: Path,
) -> None:
    """The probe from the 2026-09-03 review, as a test.

    Establish a 100,000 baseline, delete the file, then check at 98,000. Before
    the marker the breaker re-established at 98,000 and reported no breach -- the
    drawdown that had already happened became the new normal.
    """

    switch = LiveKillSwitch(tmp_path / "ks.json")
    breaker = _breaker(tmp_path, kill_switch=switch)
    first = breaker.check(Decimal("100000"), now=_NOW)
    assert first.baseline_nlv == Decimal("100000")

    (tmp_path / "baseline.json").unlink()

    decision = _breaker(tmp_path, kill_switch=switch).check(Decimal("98000"), now=_NOW)
    assert decision.breached is True
    assert "missing but this installation established one" in decision.reason
    assert switch.is_engaged() is True


def test_a_fresh_install_baseline_is_still_established(tmp_path: Path) -> None:
    """Guard the guard: the first session of a new installation must baseline."""

    decision = _breaker(tmp_path).check(Decimal("100000"), now=_NOW)
    assert decision.breached is False
    assert decision.baseline_nlv == Decimal("100000")


def test_an_unreadable_marker_is_read_as_materialized(tmp_path: Path) -> None:
    """A marker we cannot read is not evidence that a file was never written."""

    marker = StateGenerationMarker(tmp_path / "state_generation.json")
    marker.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CorruptStateGeneration):
        marker.read()
    assert marker.was_materialized(KILL_SWITCH) is True
    assert LiveKillSwitch(tmp_path / "ks.json").is_engaged() is True


def test_the_marker_records_each_component_without_dropping_the_other(
    tmp_path: Path,
) -> None:
    """Two components share one marker; recording one must not erase the other."""

    switch = LiveKillSwitch(tmp_path / "ks.json")
    switch.engage(reason="operator stop", initiated_by="operator", now=_NOW)
    _breaker(tmp_path).check(Decimal("100000"), now=_NOW)

    generation = StateGenerationMarker(tmp_path / "state_generation.json").read()
    assert generation is not None
    assert generation.materialized == (KILL_SWITCH, SESSION_DRAWDOWN)
    assert generation.installation_id


def test_drawdown_new_session_resets_baseline(tmp_path: Path) -> None:
    breaker = _breaker(tmp_path)
    breaker.check(Decimal("100000"), now=_NOW)
    # Next trading day: baseline resets to the new day's first observation.
    next_day = _NOW + timedelta(days=1)
    decision = breaker.check(Decimal("90000"), now=next_day)
    assert decision.baseline_nlv == Decimal("90000")
    assert not decision.breached


def test_drawdown_corrupt_baseline_fails_closed(tmp_path: Path) -> None:
    # A PRESENT but unreadable baseline must fail CLOSED (breach + engage the
    # kill switch), NOT silently re-baseline at the current (drawn-down) value.
    switch = LiveKillSwitch(tmp_path / "ks.json")
    breaker = _breaker(tmp_path, kill_switch=switch)
    breaker.check(Decimal("100000"), now=_NOW)  # establish
    (tmp_path / "baseline.json").write_text("{ not valid json", encoding="utf-8")
    decision = breaker.check(Decimal("90000"), now=_NOW)
    assert decision.breached
    assert switch.is_engaged()
    assert "failing closed" in decision.reason


def test_drawdown_nan_baseline_fails_closed(tmp_path: Path) -> None:
    # A well-formed file whose baseline for TODAY is NaN/non-numeric fails closed.
    import json

    switch = LiveKillSwitch(tmp_path / "ks.json")
    breaker = _breaker(tmp_path, kill_switch=switch)
    breaker.check(Decimal("100000"), now=_NOW)  # establish (writes today's session_date)
    payload = json.loads((tmp_path / "baseline.json").read_text())
    payload["baseline_nlv"] = "NaN"  # today's baseline is now non-numeric
    (tmp_path / "baseline.json").write_text(json.dumps(payload), encoding="utf-8")
    decision = breaker.check(Decimal("95000"), now=_NOW)
    assert decision.breached
    assert switch.is_engaged()


def test_arm_audit_rejects_raw_account_id() -> None:
    # Defense-in-depth: a raw broker account id in a free-text reason is refused.
    from chronos.orders.live_audit import LiveArmEventRepository

    class _Sessions:
        def begin(self) -> object:  # pragma: no cover - never reached (guard first)
            raise AssertionError("should reject before opening a session")

    repo = LiveArmEventRepository.__new__(LiveArmEventRepository)
    repo._sessions = _Sessions()  # type: ignore[attr-defined]
    repo._fingerprint = "abc123"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="raw broker account"):
        repo.record(event="armed", reason="halted account DU1234567", occurred_at=_NOW)


# --- live gate stack ------------------------------------------------------


def _all_pass() -> LiveGateInputs:
    return LiveGateInputs(
        config_live_enabled=True,
        connection_healthy=True,
        reconciled=True,
        data_quality_ok=True,
        risk_approved=True,
        preview_accepted=True,
        armed=True,
        confirmation_valid=True,
        kill_switch_engaged=False,
        drawdown_breached=False,
    )


def test_live_gate_all_pass() -> None:
    decision = evaluate_live_gates(_all_pass())
    assert decision.all_passed
    assert decision.blocking_reasons == ()
    assert {c.name for c in decision.checks} == set(LIVE_GATE_NAMES)


def test_live_gate_defaults_fail_closed() -> None:
    decision = evaluate_live_gates(LiveGateInputs())
    assert not decision.all_passed
    # Every one of the ten gates blocks from the fail-closed defaults.
    assert len(decision.blocking_reasons) == len(LIVE_GATE_NAMES)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config_live_enabled", False),
        ("connection_healthy", False),
        ("reconciled", False),
        ("data_quality_ok", False),
        ("risk_approved", False),
        ("preview_accepted", False),
        ("armed", False),
        ("confirmation_valid", False),
        ("kill_switch_engaged", True),
        ("drawdown_breached", True),
    ],
)
def test_each_gate_independently_blocks(field: str, value: bool) -> None:
    inputs = _all_pass().model_copy(update={field: value})
    decision = evaluate_live_gates(inputs)
    assert not decision.all_passed
    assert len(decision.blocking_reasons) == 1

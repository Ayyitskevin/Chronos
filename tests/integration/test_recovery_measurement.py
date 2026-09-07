"""Measured recovery observations over disposable Chronos state.

These tests exercise the packaged recovery command against the real WAL-backed
stores.  They prove capture/restore integrity and the observation arithmetic;
they do not establish an operational RPO or RTO.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import chronos.recovery.measurement as recovery_measurement
from chronos.auditlog.log import AuditLog
from chronos.control.halt import HaltReason, HaltStore
from chronos.domain.enums import OrderSide
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.sqlite_ledger import SqliteLedger
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.persistence.database import Database
from chronos.recovery.measurement import (
    RecoveryMeasurementError,
    capture_snapshot,
    restore_snapshot,
)

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class _TickingClock:
    """Deterministic wall and monotonic clocks for exact duration assertions."""

    def __init__(self, wall_start: datetime) -> None:
        self._wall = wall_start
        self._monotonic_ns = 0

    def now_utc(self) -> datetime:
        current = self._wall
        self._wall += timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        current = self._monotonic_ns
        self._monotonic_ns += 1_000_000_000
        return current


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(slots=True)
class _OpenSource:
    data: Path
    platform: SqliteLedger
    application: Database
    intent: OrderIntent

    def close(self) -> None:
        self.platform.close()
        self.application.dispose()


@pytest.fixture
def open_source(tmp_path: Path) -> Iterator[_OpenSource]:
    data = tmp_path / "source" / "data"
    data.mkdir(parents=True)

    platform = SqliteLedger(data / "platform_ledger.db")
    intent = OrderIntent(
        strategy_id="recovery_measurement",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=Decimal("500.10"),
        stop_price=Decimal("485.00"),
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=_NOW,
        source_bar_sequence_id="recovery-measurement:SPY:2026-08-29",
        proposal_reason="prove WAL-backed evidence survives measured capture",
    )
    platform.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
    platform.record_transition(intent.intent_id, IntentStatus.SUBMITTED, _NOW, "sent")
    platform.record_fill(
        intent.intent_id,
        cumulative_quantity=1,
        average_price=Decimal("500.05"),
        commission_usd=Decimal("0.35"),
        at_utc=_NOW,
    )
    application = Database(f"sqlite:///{(data / 'chronos.db').resolve()}")
    application.initialize()
    application.bind_scope(
        broker_mode="demo",
        environment="paper",
        account_id="DU1234567",
    )
    HaltStore(data / "platform_halt.json").halt(
        HaltReason.OPERATOR_REQUEST,
        "recovery measurement fixture",
    )
    LiveKillSwitch(data / "live_kill_switch.json").engage(
        reason="recovery measurement fixture",
        initiated_by="test",
        now=_NOW,
    )
    audit = AuditLog(data / "platform_audit.jsonl")
    audit.append("recovery_measurement_fixture", {"safe": True})

    source = _OpenSource(data=data, platform=platform, application=application, intent=intent)
    try:
        yield source
    finally:
        source.close()


def test_capture_and_restore_emit_bounded_measurements(open_source: _OpenSource) -> None:
    snapshot_root = open_source.data.parent.parent / "snapshot"
    capture_clock = _TickingClock(_NOW)
    manifest = capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="disposable-test-source",
        clock=capture_clock,
    )

    assert manifest.schema_version == "chronos-recovery-snapshot-v1"
    assert manifest.source_id == "disposable-test-source"
    assert manifest.artifact_capture_elapsed_seconds == 1.0
    assert {artifact.name for artifact in manifest.artifacts} == {
        "chronos.db",
        "live_kill_switch.json",
        "platform_audit.jsonl",
        "platform_halt.json",
        "platform_ledger.db",
    }
    assert all(artifact.size_bytes > 0 for artifact in manifest.artifacts)
    assert all(len(artifact.sha256) == 64 for artifact in manifest.artifacts)
    assert stat.S_IMODE(snapshot_root.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((snapshot_root / artifact.name).stat().st_mode) == 0o600
        for artifact in manifest.artifacts
    )
    assert {path.name for path in snapshot_root.iterdir()} == {
        "chronos.db",
        "live_kill_switch.json",
        "platform_audit.jsonl",
        "platform_halt.json",
        "platform_ledger.db",
        "snapshot-manifest.json",
    }

    restore_root = snapshot_root.parent / "restored"
    restore_clock = _TickingClock(_NOW + timedelta(seconds=100))
    observation = restore_snapshot(
        snapshot_root=snapshot_root,
        restore_root=restore_root,
        clock=restore_clock,
    )

    assert observation.schema_version == "chronos-recovery-observation-v1"
    assert observation.result == "PASS"
    assert observation.source_id == "disposable-test-source"
    assert observation.oldest_snapshot_age_seconds == 99.0
    assert observation.snapshot_capture_window_seconds == 9.0
    assert observation.local_restore_copy_seconds == 1.0
    assert observation.local_verification_seconds == 1.0
    assert observation.local_recovery_elapsed_seconds == 2.0
    assert observation.clock_basis == "wall-clock UTC for age; monotonic clock for durations"

    persisted = json.loads((restore_root / "recovery-observation.json").read_text())
    assert persisted == observation.to_dict()
    rendered = json.dumps(persisted, sort_keys=True)
    assert str(open_source.data.resolve()) not in rendered
    assert "DU1234567" not in rendered
    assert stat.S_IMODE(restore_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((restore_root / "data").stat().st_mode) == 0o700
    assert stat.S_IMODE((restore_root / "recovery-observation.json").stat().st_mode) == 0o600
    assert {path.name for path in (restore_root / "data").iterdir()} == {
        "chronos.db",
        "live_kill_switch.json",
        "platform_audit.jsonl",
        "platform_halt.json",
        "platform_ledger.db",
        # ADR-0054: the witness that makes a backend started from this directory
        # boot read-only and unreconciled. A wholesale restore carries both of the
        # backend's own witnesses from one snapshot, so nothing inside the
        # directory can tell it apart from a clean restart -- this file can.
        "recovery_pending.json",
    }

    restored_platform = SqliteLedger(restore_root / "data/platform_ledger.db")
    try:
        assert restored_platform.has_intent(open_source.intent.intent_id)
        assert restored_platform.working_order_snapshots() == {
            open_source.intent.intent_id: (IntentStatus.SUBMITTED, 1)
        }
    finally:
        restored_platform.close()

    restored_application = Database(f"sqlite:///{(restore_root / 'data/chronos.db').resolve()}")
    try:
        restored_application.initialize()
        restored_application.bind_scope(
            broker_mode="demo",
            environment="paper",
            account_id="DU1234567",
        )
        with pytest.raises(RuntimeError, match="already bound to a different broker scope"):
            restored_application.bind_scope(
                broker_mode="demo",
                environment="paper",
                account_id="DU7654321",
            )
    finally:
        restored_application.dispose()


def test_capture_does_not_change_source_artifact_bytes(open_source: _OpenSource) -> None:
    before = {
        name: _sha256(open_source.data / name)
        for name in (
            "chronos.db",
            "live_kill_switch.json",
            "platform_audit.jsonl",
            "platform_halt.json",
            "platform_ledger.db",
        )
    }

    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=open_source.data.parent.parent / "snapshot",
        source_id="disposable-test-source",
    )

    assert {name: _sha256(open_source.data / name) for name in before} == before


def test_capture_fsyncs_the_snapshot_root_and_parent(
    open_source: _OpenSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_root = open_source.data.parent.parent / "snapshot"
    fsynced: list[Path] = []
    real_fsync_directory = recovery_measurement._fsync_directory

    def record_fsync(path: Path) -> None:
        fsynced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(recovery_measurement, "_fsync_directory", record_fsync)

    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="disposable-test-source",
    )

    assert fsynced == [snapshot_root, snapshot_root.parent]


def test_capture_refuses_whitespace_only_audit_evidence_before_writing(
    open_source: _OpenSource,
) -> None:
    (open_source.data / "platform_audit.jsonl").write_text("\n\n", encoding="utf-8")
    snapshot_root = open_source.data.parent.parent / "snapshot"

    with pytest.raises(RecoveryMeasurementError, match="audit evidence contains no records"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert not snapshot_root.exists()


def test_capture_refuses_a_missing_audit_file_and_never_calls_it_verified(
    open_source: _OpenSource,
) -> None:
    """#179 caller-level pin for recovery/measurement.

    This site never actually conflated absent with valid — `_contains_non_whitespace`
    calls `_require_regular_file`, which raises for a missing file two lines before
    `verify_chain` is reached. That guard is what this pins, because the refusal depends
    on statement ORDER: if it were ever reordered below the chain check, the old
    True-for-missing answer would have passed a recovery snapshot with no audit evidence
    at all. The chain check itself now also refuses on anything that is not VALID.
    """

    (open_source.data / "platform_audit.jsonl").unlink()
    snapshot_root = open_source.data.parent.parent / "snapshot"

    with pytest.raises(RecoveryMeasurementError, match=r"is missing: platform_audit\.jsonl"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert not snapshot_root.exists()


def test_restore_fsyncs_the_restored_data_directory(
    open_source: _OpenSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_root = open_source.data.parent.parent / "snapshot"
    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="disposable-test-source",
    )
    restore_root = snapshot_root.parent / "restored"
    fsynced: list[Path] = []
    real_fsync_directory = recovery_measurement._fsync_directory

    def record_fsync(path: Path) -> None:
        fsynced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(recovery_measurement, "_fsync_directory", record_fsync)

    restore_snapshot(snapshot_root=snapshot_root, restore_root=restore_root)

    # `data` twice: once when the copied artifacts are durable, and again after
    # the ADR-0054 restore witness is written. The witness is written AFTER
    # verification on purpose -- a failed restore must leave no witness claiming
    # success -- which puts it outside the measured verification window, so it
    # costs its own fsync rather than contaminating `local_verification_seconds`.
    assert fsynced == [
        restore_root / "data",
        restore_root / "data",
        restore_root,
        restore_root.parent,
    ]


def test_capture_refuses_an_existing_destination_without_changing_it(
    open_source: _OpenSource,
) -> None:
    snapshot_root = open_source.data.parent.parent / "existing"
    snapshot_root.mkdir()
    marker = snapshot_root / "owner-file"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(RecoveryMeasurementError, match="already exists"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(snapshot_root.iterdir()) == [marker]


def test_capture_refuses_a_disengaged_live_kill_switch_before_writing(
    open_source: _OpenSource,
) -> None:
    LiveKillSwitch(open_source.data / "live_kill_switch.json").disengage(
        operator_note="exercise unsafe recovery posture",
        initiated_by="test",
        now=_NOW,
    )
    snapshot_root = open_source.data.parent.parent / "snapshot"

    with pytest.raises(RecoveryMeasurementError, match="kill switch is disengaged"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert not snapshot_root.exists()


def test_capture_refuses_a_corrupt_kill_file_even_though_runtime_fails_closed(
    open_source: _OpenSource,
) -> None:
    (open_source.data / "live_kill_switch.json").write_text("not json", encoding="utf-8")
    snapshot_root = open_source.data.parent.parent / "snapshot"

    with pytest.raises(RecoveryMeasurementError, match="kill-switch evidence is corrupt"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert not snapshot_root.exists()


def test_capture_refuses_corrupt_halt_evidence_even_though_runtime_fails_closed(
    open_source: _OpenSource,
) -> None:
    (open_source.data / "platform_halt.json").write_text("not json", encoding="utf-8")
    snapshot_root = open_source.data.parent.parent / "snapshot"

    with pytest.raises(RecoveryMeasurementError, match="halt evidence is corrupt"):
        capture_snapshot(
            source_data=open_source.data,
            snapshot_root=snapshot_root,
            source_id="disposable-test-source",
        )

    assert not snapshot_root.exists()


def test_restore_refuses_tampered_snapshot_bytes_before_writing(
    open_source: _OpenSource,
) -> None:
    snapshot_root = open_source.data.parent.parent / "snapshot"
    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="disposable-test-source",
    )
    with (snapshot_root / "platform_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    restore_root = snapshot_root.parent / "restored"

    with pytest.raises(RecoveryMeasurementError, match=r"snapshot (size|digest) does not match"):
        restore_snapshot(snapshot_root=snapshot_root, restore_root=restore_root)

    assert not restore_root.exists()


def test_restore_refuses_a_wall_clock_older_than_the_snapshot(
    open_source: _OpenSource,
) -> None:
    snapshot_root = open_source.data.parent.parent / "snapshot"
    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="disposable-test-source",
        clock=_TickingClock(_NOW),
    )
    restore_root = snapshot_root.parent / "restored"

    with pytest.raises(RecoveryMeasurementError, match="wall clock precedes"):
        restore_snapshot(
            snapshot_root=snapshot_root,
            restore_root=restore_root,
            clock=_TickingClock(_NOW - timedelta(seconds=1)),
        )

    assert not restore_root.exists()


def test_packaged_command_runs_capture_then_restore(open_source: _OpenSource) -> None:
    snapshot_root = open_source.data.parent.parent / "cli-snapshot"
    restore_root = snapshot_root.parent / "cli-restored"

    captured = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.recovery",
            "capture",
            "--source-data",
            str(open_source.data),
            "--snapshot-root",
            str(snapshot_root),
            "--source-id",
            "cli-disposable-source",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert captured.returncode == 0, captured.stderr
    assert json.loads(captured.stdout)["schema_version"] == "chronos-recovery-snapshot-v1"

    restored = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.recovery",
            "restore",
            "--snapshot-root",
            str(snapshot_root),
            "--restore-root",
            str(restore_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["result"] == "PASS"


def test_a_restore_leaves_a_witness_the_backend_will_refuse_to_ignore(
    open_source: _OpenSource,
) -> None:
    """The restored data directory carries a token unique to this restore (ADR-0054).

    Two properties, and the second is the one worth a test: the file is a real
    JSON object with a non-empty token, and two restores of the same snapshot do
    not share it — otherwise the operator note acknowledging the first restore
    would silently cover the second.
    """

    root = open_source.data.parent.parent
    snapshot_root = root / "snapshot"
    capture_snapshot(
        source_data=open_source.data,
        snapshot_root=snapshot_root,
        source_id="paper-drill",
    )

    tokens: list[str] = []
    for attempt in ("first", "second"):
        restore_root = root / f"restore-{attempt}"
        restore_snapshot(snapshot_root=snapshot_root, restore_root=restore_root)
        witness = restore_root / "data" / "recovery_pending.json"
        assert stat.S_IMODE(witness.stat().st_mode) == 0o600
        payload = json.loads(witness.read_text(encoding="utf-8"))
        assert isinstance(payload["token"], str) and payload["token"]
        tokens.append(payload["token"])

    assert tokens[0] != tokens[1]

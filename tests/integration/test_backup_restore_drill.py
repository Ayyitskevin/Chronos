"""Isolated backup/restore drill over Chronos' real durable stores.

The drill exercises SQLite's online backup API while both WAL-backed source
databases are open, restores only into ``tmp_path``, and then reopens the real
Chronos stores.  It proves artifact integrity and fail-closed recovery posture;
it does not prove broker reconciliation, RPO/RTO, or permission to rearm.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.auditlog.log import AuditLog, ChainState, verify_chain
from chronos.control.halt import HaltReason, HaltStore
from chronos.domain.enums import OrderSide
from chronos.execution.intents import IntentStatus, OrderIntent, TimeInForce
from chronos.execution.sqlite_ledger import SqliteLedger
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.persistence.database import Database

_NOW = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
_ACCOUNT_ID = "DU1234567"


@dataclass(frozen=True, slots=True)
class _RestoredCopy:
    root: Path
    intent: OrderIntent

    @property
    def data(self) -> Path:
        return self.root / "data"


def _intent() -> OrderIntent:
    return OrderIntent(
        strategy_id="restore_drill",
        strategy_version="1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=Decimal("500.10"),
        stop_price=Decimal("485.00"),
        time_in_force=TimeInForce.DAY,
        decision_timestamp_utc=_NOW,
        source_bar_sequence_id="restore-drill:SPY:2026-08-28",
        proposal_reason="prove committed order evidence survives online backup",
    )


def _online_backup(source: Path, destination: Path) -> None:
    """Copy one live SQLite database through the SQLite backup API."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _checkpoint_wal(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert result is not None and result[0] == 0, f"could not checkpoint test database: {result}"


def _row_count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _sqlite_integrity(path: Path) -> None:
    assert path.is_file(), f"required restored database is missing: {path}"
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        detail = f"restored database integrity check failed: {path}: {error}"
        raise AssertionError(detail) from error
    assert result == ("ok",), f"restored database integrity check failed: {path}: {result}"


def _assert_recovery_evidence(restored: _RestoredCopy) -> None:
    """Fail unless the isolated copy has the evidence required before reconciliation."""

    platform_database = restored.data / "platform_ledger.db"
    application_database = restored.data / "chronos.db"
    _sqlite_integrity(platform_database)
    _sqlite_integrity(application_database)

    kill_path = restored.data / "live_kill_switch.json"
    assert kill_path.is_file(), "restored live kill-switch evidence is missing"
    assert LiveKillSwitch(kill_path).read().engaged, "restored live kill switch is disengaged"
    assert HaltStore(restored.data / "platform_halt.json").read().halted, (
        "restored deterministic platform is rearmed"
    )

    audit_path = restored.data / "platform_audit.jsonl"
    assert audit_path.is_file() and audit_path.stat().st_size > 0, (
        "restored audit evidence is missing or empty"
    )
    verification = verify_chain(audit_path)
    assert verification.state is ChainState.VALID, verification.detail


def _build_isolated_restore(tmp_path: Path) -> _RestoredCopy:
    source_data = tmp_path / "source" / "data"
    restored_data = tmp_path / "restored" / "data"
    source_data.mkdir(parents=True)
    restored_data.mkdir(parents=True)

    intent = _intent()
    platform_path = source_data / "platform_ledger.db"
    application_path = source_data / "chronos.db"
    platform = SqliteLedger(platform_path)
    application = Database(f"sqlite:///{application_path.resolve()}")
    try:
        application.initialize()
        _checkpoint_wal(platform_path)
        _checkpoint_wal(application_path)

        platform.record_intent(intent, IntentStatus.PENDING_SUBMISSION)
        platform.record_transition(intent.intent_id, IntentStatus.SUBMITTED, _NOW, "sent")
        platform.record_fill(
            intent.intent_id,
            cumulative_quantity=1,
            average_price=Decimal("500.05"),
            commission_usd=Decimal("0.35"),
            at_utc=_NOW,
        )
        application.bind_scope(
            broker_mode="demo",
            environment="paper",
            account_id=_ACCOUNT_ID,
        )

        assert platform_path.with_name(f"{platform_path.name}-wal").stat().st_size > 0
        assert application_path.with_name(f"{application_path.name}-wal").stat().st_size > 0

        unsafe_data = tmp_path / "unsafe-main-file-only"
        unsafe_data.mkdir()
        shutil.copy2(platform_path, unsafe_data / platform_path.name)
        shutil.copy2(application_path, unsafe_data / application_path.name)
        assert _row_count(unsafe_data / platform_path.name, "intents") == 0
        assert _row_count(unsafe_data / application_path.name, "database_scope") == 0

        _online_backup(platform_path, restored_data / platform_path.name)
        _online_backup(application_path, restored_data / application_path.name)
    finally:
        platform.close()
        application.dispose()

    HaltStore(source_data / "platform_halt.json").halt(
        HaltReason.OPERATOR_REQUEST,
        "isolated restore drill",
    )
    LiveKillSwitch(source_data / "live_kill_switch.json").engage(
        reason="isolated restore drill",
        initiated_by="test",
        now=_NOW,
    )
    audit = AuditLog(source_data / "platform_audit.jsonl")
    audit.append("backup_started", {"kind": "isolated_restore_drill"})
    audit.append("backup_completed", {"databases": 2})
    for name in ("platform_halt.json", "live_kill_switch.json", "platform_audit.jsonl"):
        shutil.copy2(source_data / name, restored_data / name)

    return _RestoredCopy(root=restored_data.parent, intent=intent)


@pytest.fixture
def restored(tmp_path: Path) -> _RestoredCopy:
    return _build_isolated_restore(tmp_path)


def test_online_backup_restores_committed_wal_evidence(restored: _RestoredCopy) -> None:
    platform = SqliteLedger(restored.data / "platform_ledger.db")
    try:
        assert platform.has_intent(restored.intent.intent_id)
        assert platform.working_order_snapshots() == {
            restored.intent.intent_id: (IntentStatus.SUBMITTED, 1)
        }
    finally:
        platform.close()

    application = Database(f"sqlite:///{(restored.data / 'chronos.db').resolve()}")
    try:
        application.initialize()
        application.bind_scope(
            broker_mode="demo",
            environment="paper",
            account_id=_ACCOUNT_ID,
        )
        with pytest.raises(RuntimeError, match="already bound to a different broker scope"):
            application.bind_scope(
                broker_mode="demo",
                environment="paper",
                account_id="DU7654321",
            )
    finally:
        application.dispose()


def test_isolated_restore_preserves_required_recovery_evidence(restored: _RestoredCopy) -> None:
    _assert_recovery_evidence(restored)


def test_drill_rejects_an_omitted_live_kill_file(restored: _RestoredCopy) -> None:
    (restored.data / "live_kill_switch.json").unlink()

    with pytest.raises(AssertionError, match="kill-switch evidence is missing"):
        _assert_recovery_evidence(restored)


def test_drill_rejects_a_disengaged_live_kill_switch(restored: _RestoredCopy) -> None:
    LiveKillSwitch(restored.data / "live_kill_switch.json").disengage(
        operator_note="simulate an unsafe recovered copy",
        initiated_by="test",
        now=_NOW,
    )

    with pytest.raises(AssertionError, match="kill switch is disengaged"):
        _assert_recovery_evidence(restored)


def test_drill_rejects_a_rearmed_deterministic_platform(restored: _RestoredCopy) -> None:
    HaltStore(restored.data / "platform_halt.json").rearm("simulate an unsafe recovered copy")

    with pytest.raises(AssertionError, match="platform is rearmed"):
        _assert_recovery_evidence(restored)


def test_drill_rejects_a_tampered_audit_chain(restored: _RestoredCopy) -> None:
    audit_path = restored.data / "platform_audit.jsonl"
    content = audit_path.read_text(encoding="utf-8")
    assert "backup_started" in content
    audit_path.write_text(content.replace("backup_started", "backup_started_x"), encoding="utf-8")

    with pytest.raises(AssertionError, match="hash mismatch"):
        _assert_recovery_evidence(restored)


def test_drill_rejects_a_corrupt_database(restored: _RestoredCopy) -> None:
    application_path = restored.data / "chronos.db"
    application_path.write_bytes(b"not a sqlite database")

    with pytest.raises(AssertionError, match="database integrity check failed"):
        _assert_recovery_evidence(restored)

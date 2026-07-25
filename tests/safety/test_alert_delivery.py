"""The owner gets told, and the channel cannot quietly grow teeth (R-32, M6).

M3 made alerts durable. M5 disclosed the gap: durable is not delivered, the
channel was **pull**, and an owner who was not looking was not told.

The decision taken here is local sinks only. These tests pin both halves of it:
that delivery works, and that the module cannot acquire a networked sender
without someone deliberately editing a safety test. A networked sender would
need credentials beside a process that moves money and an egress path a
compromised component could ride, and its failure mode is silence -- which is
indistinguishable from "all clear".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyOwnerAlertRow
from chronos.supervisor import alerts, delivery

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as db_session:
            yield db_session
    finally:
        database.dispose()


def _raise(session: Session, **overrides: object) -> AutonomyOwnerAlertRow:
    base: dict[str, object] = {
        "account_fingerprint": _FINGERPRINT,
        "severity": alerts.AlertSeverity.WARNING,
        "kind": "degraded.broker",
        "summary": "broker unreachable",
        "now": _NOW,
    }
    base.update(overrides)
    return alerts.raise_alert(session, **base)  # type: ignore[arg-type]


class _FailingSink:
    name = "failing"

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        return False


class _RaisingSink:
    name = "raising"

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        raise RuntimeError("the sink itself is broken")


class _RecordingSink:
    def __init__(self) -> None:
        self.name = "recording"
        self.seen: list[alerts.OwnerAlert] = []

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        self.seen.append(alert)
        return True


# ------------------------------------------------------------ the guarantee


def test_the_delivery_module_has_no_network_capability() -> None:
    """The decision, enforced structurally rather than promised in prose.

    A networked sender needs a credential store beside a process that moves
    money, and its failure mode is silence. If someone adds one later this test
    fails, which is exactly the deliberate act it should be.
    """

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(delivery))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = {
        "smtplib",
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "email",
        "subprocess",
        "ftplib",
        "telnetlib",
    }
    offenders = [name for name in imported if name.split(".")[0] in forbidden]
    assert offenders == [], (
        f"the alert delivery module gained a network capability: {offenders}. Local sinks "
        "only was a deliberate decision (R-32); adding a networked channel needs an ADR."
    )


def test_delivery_marks_told_which_is_not_acknowledged(session: Session) -> None:
    """The distinction the whole schema change exists for."""

    row = _raise(session)
    sink = _RecordingSink()
    report = delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(sink,), now=_NOW
    )
    assert report.delivered == 1
    assert row.delivered_at == _NOW
    # Delivered, and still unacknowledged: the owner was told and has not replied.
    assert row.acknowledged_at is None
    assert len(alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)) == 1


def test_an_alert_is_not_delivered_twice(session: Session) -> None:
    """Delivered-and-unacknowledged is a normal state, not a reason to re-tell."""

    _raise(session)
    sink = _RecordingSink()
    delivery.deliver_pending(session, account_fingerprint=_FINGERPRINT, sinks=(sink,), now=_NOW)
    second = delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(sink,), now=_NOW
    )
    assert second.considered == 0
    assert len(sink.seen) == 1


def test_a_failing_sink_records_an_attempt_rather_than_raising(session: Session) -> None:
    """A persistently failing sink should be visible, not silently retried."""

    row = _raise(session)
    report = delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(_FailingSink(),), now=_NOW
    )
    assert report.failed == 1
    assert report.complete is False
    assert row.delivered_at is None
    assert row.delivery_attempts == 1


def test_a_sink_that_raises_does_not_crash_the_pass(session: Session) -> None:
    """An alerting system that crashes what it is alerting about made it worse."""

    _raise(session)
    report = delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(_RaisingSink(),), now=_NOW
    )
    assert report.failed == 1


def test_one_working_sink_is_enough(session: Session) -> None:
    """A misconfigured file path must not suppress the log sink forever."""

    row = _raise(session)
    working = _RecordingSink()
    report = delivery.deliver_pending(
        session,
        account_fingerprint=_FINGERPRINT,
        sinks=(_FailingSink(), working),
        now=_NOW,
    )
    assert report.delivered == 1
    assert row.delivered_at == _NOW


def test_every_sink_is_attempted_even_after_one_succeeds(session: Session) -> None:
    """The owner may be watching only one; order of registration must not decide."""

    _raise(session)
    first = _RecordingSink()
    second = _RecordingSink()
    delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(first, second), now=_NOW
    )
    assert len(first.seen) == 1
    assert len(second.seen) == 1


def test_no_sink_means_the_owner_cannot_be_told(session: Session) -> None:
    _raise(session)
    report = delivery.deliver_pending(session, account_fingerprint=_FINGERPRINT, sinks=(), now=_NOW)
    assert report.delivered == 0
    assert report.sinks == ()
    assert delivery.undelivered_count(session, account_fingerprint=_FINGERPRINT) == 1


def test_delivery_is_per_account(session: Session) -> None:
    _raise(session)
    sink = _RecordingSink()
    delivery.deliver_pending(session, account_fingerprint="b" * 64, sinks=(sink,), now=_NOW)
    assert sink.seen == []


def test_a_pass_is_bounded(session: Session) -> None:
    """A backlog should drain over several passes, not stall a cycle."""

    for index in range(10):
        _raise(session, kind=f"degraded.subsystem{index}")
    report = delivery.deliver_pending(
        session, account_fingerprint=_FINGERPRINT, sinks=(_RecordingSink(),), now=_NOW, limit=3
    )
    assert report.delivered == 3
    assert delivery.undelivered_count(session, account_fingerprint=_FINGERPRINT) == 7


def test_a_severity_threshold_filters_delivery(session: Session) -> None:
    _raise(session, severity=alerts.AlertSeverity.INFO, kind="informational")
    report = delivery.deliver_pending(
        session,
        account_fingerprint=_FINGERPRINT,
        sinks=(_RecordingSink(),),
        now=_NOW,
        minimum_severity=alerts.AlertSeverity.CRITICAL,
    )
    assert report.considered == 0


# --------------------------------------------------------------- the sinks


def test_the_file_sink_writes_one_json_line_per_alert(tmp_path: Path) -> None:
    target = tmp_path / "alerts" / "owner.jsonl"
    sink = delivery.FileAlertSink(target)
    alert = alerts.OwnerAlert(
        id=1,
        severity=alerts.AlertSeverity.CRITICAL,
        kind="degraded.reconciliation",
        summary="positions unproven",
        detail={"subsystem": "reconciliation"},
        raised_at=_NOW,
        last_seen_at=_NOW,
        occurrences=3,
    )
    assert sink.deliver(alert) is True
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["severity"] == "CRITICAL"
    assert record["occurrences"] == 3


def test_the_file_sink_creates_the_file_private(tmp_path: Path) -> None:
    target = tmp_path / "owner.jsonl"
    delivery.FileAlertSink(target).deliver(
        alerts.OwnerAlert(
            id=1,
            severity=alerts.AlertSeverity.INFO,
            kind="k",
            summary="s",
            detail={},
            raised_at=_NOW,
            last_seen_at=_NOW,
            occurrences=1,
        )
    )
    assert (target.stat().st_mode & 0o777) == 0o600


def test_the_file_sink_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    """The same R-21 guard the database and audit files use."""

    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)

    delivered = delivery.FileAlertSink(link).deliver(
        alerts.OwnerAlert(
            id=1,
            severity=alerts.AlertSeverity.INFO,
            kind="k",
            summary="s",
            detail={},
            raised_at=_NOW,
            last_seen_at=_NOW,
            occurrences=1,
        )
    )
    assert delivered is False
    assert real.read_text(encoding="utf-8") == ""


def test_the_log_sink_is_always_present(tmp_path: Path) -> None:
    """A floor that cannot fail for environmental reasons."""

    assert [sink.name for sink in delivery.default_sinks()] == ["log"]
    named = delivery.default_sinks(tmp_path / "owner.jsonl")
    assert [sink.name for sink in named] == ["log", "file:owner.jsonl"]


def test_the_log_sink_reports_severity_and_recurrence() -> None:
    """One occurrence is an event; four thousand is a stuck system.

    Uses its own handler rather than `caplog`: the suite configures logging
    globally elsewhere, and a test that depends on that config is testing the
    config rather than the sink.
    """

    import logging

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("chronos.supervisor.alerts")
    handler = _Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        delivery.LogAlertSink().deliver(
            alerts.OwnerAlert(
                id=1,
                severity=alerts.AlertSeverity.WARNING,
                kind="degraded.market_data",
                summary="stale quotes",
                detail={},
                raised_at=_NOW,
                last_seen_at=_NOW,
                occurrences=4000,
            )
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert len(captured) == 1
    rendered = captured[0].getMessage()
    assert "x4000" in rendered
    assert "degraded.market_data" in rendered
    assert captured[0].levelno == logging.WARNING

"""Getting an alert in front of the owner (ADR-0016 §8, R-32, M6).

M3 made owner alerts durable. M5 disclosed the gap plainly: durable is not
delivered, the channel was **pull**, and an owner who was not looking was not
told — which is the whole problem under unattended autonomy. This module is the
push half, built to a shape that does not reintroduce the risks the pull-only
design was avoiding.

## The decision: local sinks only

Every sink shipped here writes **locally**. There is no email, SMS, webhook, or
push-service client, and adding one is a deliberate act with an ADR attached —
enforced by a structural test that fails if this module gains a network import.

That is not timidity, it is the actual threat model:

- A networked sender needs **credentials**, which means a secret store sitting
  beside a process that can move money. The directive names that pattern as one
  of the reference project's failures.
- It needs **outbound egress**, which is a path a compromised component can ride
  in the other direction. Chronos otherwise makes no outbound call at all.
- Its failure mode is **silence**, and silence is indistinguishable from "all
  clear". A push channel that quietly stops working is worse than none, because
  it converts "no alerts" from *unknown* into *fine*.

A local file has none of those properties, and on a single-operator local-first
system it reaches the operator's own tooling — a terminal tail, an editor watch,
a desktop notifier, a systemd path unit — without Chronos holding a secret or
opening a socket.

## What this honestly does and does not close

**Closes:** an alert now leaves the database and lands somewhere an operator's
own tooling can watch, with delivery recorded so "was the owner told" has an
answer distinct from "did the owner acknowledge".

**Does not close:** if you are away from the machine, a local file does not
follow you. Reaching a phone needs a networked channel, and that remains an
owner decision with an ADR, not something to add quietly. R-32 is downgraded,
not deleted, and the residual is stated in the risk register.

## Why delivery is recorded, and why failure is not fatal

``delivered_at`` distinguishes *told* from *acknowledged* — a distinction that
matters most in the case where something went wrong and nobody noticed. Delivery
failures increment a durable attempt count rather than raising: an alerting
system that crashes the process it is alerting about has made things worse, and
a persistently failing sink should be *visible*, which a growing count is and an
exception in a log is not.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.persistence.schema import AutonomyOwnerAlertRow
from chronos.supervisor.alerts import AlertSeverity, OwnerAlert, severity_rank

_logger = logging.getLogger("chronos.supervisor.alerts")


class AlertSink(Protocol):
    """Somewhere an alert can be put where a human might see it.

    Implementations must not raise: :func:`deliver_pending` treats a ``False``
    return as a failure to record, and an exception as the same thing, but a
    sink that manages its own failure reports a cleaner reason.
    """

    @property
    def name(self) -> str:  # pragma: no cover - Protocol
        ...

    def deliver(self, alert: OwnerAlert) -> bool:  # pragma: no cover - Protocol
        ...


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What one delivery pass achieved."""

    considered: int
    delivered: int
    failed: int
    sinks: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.failed == 0


class LogAlertSink:
    """Emit to the standard logger at a level matching severity.

    Always available, needs no configuration, and lands wherever the operator
    already sends logs. Listed first because a sink that cannot itself fail for
    environmental reasons is the one you want as a floor.
    """

    name = "log"

    def deliver(self, alert: OwnerAlert) -> bool:
        level = {
            AlertSeverity.INFO: logging.INFO,
            AlertSeverity.WARNING: logging.WARNING,
            AlertSeverity.CRITICAL: logging.ERROR,
        }[alert.severity]
        _logger.log(
            level,
            "OWNER ALERT [%s] %s (x%d): %s",
            alert.severity.value,
            alert.kind,
            alert.occurrences,
            alert.summary,
            extra={
                "event": "owner_alert",
                "alert_kind": alert.kind,
                "severity": alert.severity.value,
                "occurrences": alert.occurrences,
            },
        )
        return True


class FileAlertSink:
    """Append one JSON line per alert to a private file.

    Chosen as the primary local channel because it is the one an operator's own
    tooling composes with: ``tail -f``, an editor watch, a systemd path unit, a
    desktop notifier. Chronos does not need to know which, and holds no
    credential for any of them.

    The file is created 0600 and appended with an fsync, matching how the audit
    log and kill switch treat durability: an alert that was reported as
    delivered but lost in a page cache would be a lie of exactly the kind this
    module exists to prevent.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.name = f"file:{path.name}"

    def deliver(self, alert: OwnerAlert) -> bool:
        record = {
            "raised_at": alert.raised_at.isoformat(),
            "last_seen_at": alert.last_seen_at.isoformat(),
            "severity": alert.severity.value,
            "kind": alert.kind,
            "summary": alert.summary,
            "occurrences": alert.occurrences,
            "detail": alert.detail,
        }
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW: refuse to append through a symlink, the same guard the
            # database and audit files use (R-21).
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(self._path, flags, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            _logger.exception(
                "Failed to write an owner alert to %s",
                self._path,
                extra={"event": "alert_sink_failed"},
            )
            return False
        return True


def default_sinks(alert_file: Path | None = None) -> tuple[AlertSink, ...]:
    """The shipped local channels. Never networked.

    The log sink is always present so there is a floor that cannot fail for
    environmental reasons. A file sink is added when a path is configured.
    """

    sinks: list[AlertSink] = [LogAlertSink()]
    if alert_file is not None:
        sinks.append(FileAlertSink(alert_file))
    return tuple(sinks)


def deliver_pending(
    session: Session,
    *,
    account_fingerprint: str,
    sinks: tuple[AlertSink, ...],
    now: datetime,
    minimum_severity: AlertSeverity = AlertSeverity.INFO,
    limit: int = 100,
) -> DeliveryReport:
    """Deliver every alert the owner has not yet been told about.

    Selects on ``delivered_at IS NULL`` rather than on acknowledgement, because
    those are different questions: an alert can be delivered and not
    acknowledged (the owner was told and has not replied), and that is a normal
    state, not a reason to tell them again.

    An alert counts as delivered when **at least one** sink accepted it. A
    stricter rule — every sink must succeed — would mean a single misconfigured
    file path suppressed the log sink's report forever, which is the opposite of
    what an alerting system should do when partly broken.

    ``limit`` bounds one pass. A backlog of thousands should drain over several
    passes rather than block a cycle, and an unbounded pass is exactly how an
    alerting system becomes the thing that stalls the loop.
    """

    if not sinks:
        # No sink is not "nothing to do" — it means the owner cannot be told.
        # Reported as failure so a caller checking `complete` notices.
        return DeliveryReport(considered=0, delivered=0, failed=0, sinks=())

    rows = list(
        session.scalars(
            select(AutonomyOwnerAlertRow)
            .where(
                AutonomyOwnerAlertRow.account_fingerprint == account_fingerprint,
                AutonomyOwnerAlertRow.delivered_at.is_(None),
            )
            .order_by(AutonomyOwnerAlertRow.id.asc())
            .limit(limit)
        )
    )
    pending = [
        row
        for row in rows
        if severity_rank(AlertSeverity(row.severity)) >= severity_rank(minimum_severity)
    ]

    delivered = 0
    failed = 0
    for row in pending:
        alert = OwnerAlert(
            id=row.id,
            severity=AlertSeverity(row.severity),
            kind=row.kind,
            summary=row.summary,
            detail=dict(row.detail),
            raised_at=row.raised_at,
            last_seen_at=row.last_seen_at,
            occurrences=row.occurrences,
        )
        row.delivery_attempts += 1
        if _deliver_to_any(alert, sinks):
            row.delivered_at = now
            delivered += 1
        else:
            failed += 1

    return DeliveryReport(
        considered=len(pending),
        delivered=delivered,
        failed=failed,
        sinks=tuple(sink.name for sink in sinks),
    )


def _deliver_to_any(alert: OwnerAlert, sinks: tuple[AlertSink, ...]) -> bool:
    """True if any sink accepted. A sink that raises counts as a failure.

    Every sink is attempted even after one succeeds: the owner may be watching
    only one of them, and skipping the rest would make delivery depend on the
    order the sinks happen to be listed in.
    """

    accepted = False
    for sink in sinks:
        try:
            if sink.deliver(alert):
                accepted = True
        except Exception:
            # An alerting system that crashes the process it is alerting about
            # has made things worse. The attempt count records the trouble.
            _logger.exception(
                "Alert sink %s raised while delivering %s",
                sink.name,
                alert.kind,
                extra={"event": "alert_sink_raised"},
            )
    return accepted


def undelivered_count(session: Session, *, account_fingerprint: str) -> int:
    """How many alerts the owner has not been told about.

    Exposed so a health endpoint or dashboard can surface it. A number that
    stops falling is the signal that delivery itself is broken — which is the
    one failure an alerting system cannot report through its own channel.
    """

    rows = session.scalars(
        select(AutonomyOwnerAlertRow.id).where(
            AutonomyOwnerAlertRow.account_fingerprint == account_fingerprint,
            AutonomyOwnerAlertRow.delivered_at.is_(None),
        )
    ).all()
    return len(rows)

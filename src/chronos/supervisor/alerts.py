"""Telling the owner something went wrong (ADR-0016 §8, M3).

The degraded-state rule has four clauses: create no new exposure, permit
deterministic risk-reducing behavior, **record the denial**, and **alert the
owner**. M2 delivered the first three. This is the fourth, and it is the one
that matters most under unattended autonomy, because the other three are all
things the system does quietly to itself.

## The honest boundary, stated first

**This module does not deliver alerts out of band.** It durably records them and
makes them impossible to lose. It does not send email, SMS, push notifications,
or webhooks, and that is a deliberate limit rather than an oversight:

- Chronos is local-first and makes no outbound network calls. Adding one would
  introduce a credential store, an egress path, and a dependency whose failure
  mode is *silence* — an alerting channel that quietly stops working is worse
  than none, because it converts "no alerts" from "unknown" into "all clear".
- The directive forbids reproducing the reference project's unsafe patterns, and
  a background sender holding SMTP or webhook secrets next to a trading system
  is squarely in that category.

So what this gives you is a **pull** channel: alerts are durable, deduplicated,
survive restarts, and stay visible until acknowledged. What it does not give you
is a **push** channel, and an owner who is not looking will not be told.

That gap is a promotion criterion, not a footnote. Out-of-band delivery must
exist before ``LIVE_AUTONOMOUS`` runs unattended, and it is recorded as such in
``docs/limitations.md`` and the risk register. A system that can only alert
someone who is already watching is not yet an unattended system.

## Why alerts are acknowledged rather than deleted

An alert that can vanish cannot prove the owner was told, and being able to
prove that is the only thing an alert is for. Acknowledgement is a separate,
recorded act with a note, so "nobody saw it" and "someone saw it and decided it
was fine" stay distinguishable forever.

## Why repeats fold into one row

A degraded loop can raise the same condition thousands of times a minute. Left
alone that buries every *other* alert under identical entries, and an alert list
nobody can read is an alert nobody receives. Recurrences increment a counter on
the existing unacknowledged row instead. The count is itself information: one
occurrence is an event, four thousand is a stuck system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.persistence import hash_chain
from chronos.persistence.schema import AutonomyOwnerAlertRow
from chronos.supervisor.admission import AdmissionOutcome, DegradedReason

#: Hash-chain stream for owner alerts. Chained like the decision journal so an
#: alert cannot be quietly removed from the record after the fact.
ALERT_STREAM = "autonomy.alerts"


class AlertSeverity(StrEnum):
    """How much the owner needs to care. Ordered, so a caller can threshold."""

    #: Something happened that the owner should know but need not act on.
    INFO = "INFO"
    #: The system narrowed its own authority; trading continues, reduced.
    WARNING = "WARNING"
    #: The system stopped, or cannot prove its own state. Needs a human.
    CRITICAL = "CRITICAL"


_SEVERITY_RANK: dict[AlertSeverity, int] = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
}


def severity_rank(severity: AlertSeverity) -> int:
    return _SEVERITY_RANK[severity]


@dataclass(frozen=True, slots=True)
class OwnerAlert:
    """One condition the owner has not yet acknowledged."""

    id: int
    severity: AlertSeverity
    kind: str
    summary: str
    detail: dict[str, Any]
    raised_at: datetime
    last_seen_at: datetime
    occurrences: int

    @property
    def is_recurring(self) -> bool:
        """More than once means a condition, not an event."""

        return self.occurrences > 1


def raise_alert(
    session: Session,
    *,
    account_fingerprint: str,
    severity: AlertSeverity,
    kind: str,
    summary: str,
    detail: dict[str, Any] | None = None,
    now: datetime,
) -> AutonomyOwnerAlertRow:
    """Record something the owner must see, folding repeats into one row.

    Takes the caller's session so the alert commits with whatever it describes.
    An alert that commits separately can survive a rolled-back refusal, or be
    lost while the refusal survives — and an alert about something that did not
    happen erodes trust in the ones about things that did.
    """

    if not kind:
        raise ValueError("an alert must be named so recurrences can be recognized")

    existing = session.scalar(
        select(AutonomyOwnerAlertRow)
        .where(
            AutonomyOwnerAlertRow.account_fingerprint == account_fingerprint,
            AutonomyOwnerAlertRow.kind == kind,
            AutonomyOwnerAlertRow.acknowledged_at.is_(None),
        )
        .order_by(AutonomyOwnerAlertRow.id.desc())
        .limit(1)
    )
    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = now
        # Escalation is one-way while unacknowledged: a later INFO must not
        # downgrade a CRITICAL the owner has not yet seen.
        if severity_rank(severity) > severity_rank(AlertSeverity(existing.severity)):
            existing.severity = severity.value
            existing.summary = summary
        return existing

    row = AutonomyOwnerAlertRow(
        account_fingerprint=account_fingerprint,
        severity=severity.value,
        kind=kind,
        summary=summary,
        detail=detail or {},
        raised_at=now,
        acknowledged_at=None,
        acknowledged_note="",
        occurrences=1,
        last_seen_at=now,
    )
    session.add(row)
    session.flush()
    hash_chain.append(
        session,
        stream=f"{ALERT_STREAM}:{account_fingerprint}",
        kind="alert_raised",
        payload={"severity": severity.value, "kind": kind, "summary": summary},
        recorded_at=now,
    )
    return row


def unacknowledged(
    session: Session,
    *,
    account_fingerprint: str,
    minimum_severity: AlertSeverity = AlertSeverity.INFO,
) -> tuple[OwnerAlert, ...]:
    """Every alert still awaiting the owner, most severe and most recent first."""

    rows = session.scalars(
        select(AutonomyOwnerAlertRow).where(
            AutonomyOwnerAlertRow.account_fingerprint == account_fingerprint,
            AutonomyOwnerAlertRow.acknowledged_at.is_(None),
        )
    ).all()
    alerts = [
        OwnerAlert(
            id=row.id,
            severity=AlertSeverity(row.severity),
            kind=row.kind,
            summary=row.summary,
            detail=dict(row.detail),
            raised_at=row.raised_at,
            last_seen_at=row.last_seen_at,
            occurrences=row.occurrences,
        )
        for row in rows
        if severity_rank(AlertSeverity(row.severity)) >= severity_rank(minimum_severity)
    ]
    alerts.sort(key=lambda alert: (-severity_rank(alert.severity), -alert.last_seen_at.timestamp()))
    return tuple(alerts)


def acknowledge(
    session: Session,
    *,
    account_fingerprint: str,
    alert_id: int,
    note: str,
    now: datetime,
) -> bool:
    """Record that the owner saw an alert. Returns False if there was none.

    A note is required. "Acknowledged" with no explanation is indistinguishable
    from a stray click, and the point of the record is to make the two
    different.
    """

    if not note.strip():
        raise ValueError("acknowledging an alert requires a note saying what was decided")
    row = session.scalar(
        select(AutonomyOwnerAlertRow).where(
            AutonomyOwnerAlertRow.id == alert_id,
            AutonomyOwnerAlertRow.account_fingerprint == account_fingerprint,
            AutonomyOwnerAlertRow.acknowledged_at.is_(None),
        )
    )
    if row is None:
        return False
    row.acknowledged_at = now
    row.acknowledged_note = note
    hash_chain.append(
        session,
        stream=f"{ALERT_STREAM}:{account_fingerprint}",
        kind="alert_acknowledged",
        payload={"alert_id": alert_id, "kind": row.kind, "note": note},
        recorded_at=now,
    )
    return True


def alert_for_refusal(
    session: Session,
    *,
    account_fingerprint: str,
    decision_id: str,
    outcome: AdmissionOutcome,
    now: datetime,
) -> AutonomyOwnerAlertRow | None:
    """Alert on the refusals that mean the *system* is wrong, not the model.

    Not every refusal deserves the owner's attention, and treating them alike
    would be its own failure: a gateway that alerts on each out-of-scope symbol
    trains the owner to ignore alerts, and then the one that matters is ignored
    too. So this is deliberately narrow.

    Alerted:

    - **Degraded state.** The directive's clause, verbatim: the system is
      unavailable, ambiguous, stale, or inconsistent somewhere.
    - **Exhausted re-submission.** A model that hit the R-31 bound is looping
      against a refusal, which is a symptom the owner should see even though
      the kernel handled it correctly.
    - **Revoked or unactivated authority** while decisions are still arriving,
      which usually means something is running that the owner thinks is stopped.

    Not alerted: ordinary scope, promotion, version-pin and market-data
    refusals. Those are the gate doing its job on a well-formed disagreement,
    and they are all in the decision journal for anyone who looks.
    """

    from chronos.supervisor.admission import AdmissionRefusal

    refusal = outcome.refusal
    if refusal is None:
        return None

    severity: AlertSeverity
    if refusal in {AdmissionRefusal.DEGRADED_STATE, AdmissionRefusal.DEGRADED_RISK_REDUCTION_ONLY}:
        severity = (
            AlertSeverity.CRITICAL
            if refusal is AdmissionRefusal.DEGRADED_STATE
            else AlertSeverity.WARNING
        )
    elif refusal is AdmissionRefusal.RESUBMISSION_EXHAUSTED or refusal in {
        AdmissionRefusal.MANDATE_REVOKED,
        AdmissionRefusal.MANDATE_NOT_ACTIVATED,
    }:
        severity = AlertSeverity.WARNING
    else:
        return None

    return raise_alert(
        session,
        account_fingerprint=account_fingerprint,
        severity=severity,
        kind=f"admission.{refusal.value.lower()}",
        summary=outcome.detail or refusal.value,
        detail={"decision_id": decision_id, "refusal": refusal.value},
        now=now,
    )


def alert_for_degradation(
    session: Session,
    *,
    account_fingerprint: str,
    reasons: tuple[DegradedReason, ...],
    now: datetime,
) -> tuple[AutonomyOwnerAlertRow, ...]:
    """Alert on each degraded subsystem, whether or not a decision arrived.

    Degradation that nobody decided against is still degradation. A system that
    only reported problems when a model happened to ask would stay silent
    exactly when the model plane is the thing that failed.
    """

    raised: list[AutonomyOwnerAlertRow] = []
    for reason in reasons:
        raised.append(
            raise_alert(
                session,
                account_fingerprint=account_fingerprint,
                severity=(
                    AlertSeverity.CRITICAL
                    if reason.blocks_risk_reduction
                    else AlertSeverity.WARNING
                ),
                kind=f"degraded.{reason.subsystem}",
                summary=str(reason),
                detail={
                    "subsystem": reason.subsystem,
                    "blocks_risk_reduction": reason.blocks_risk_reduction,
                },
                now=now,
            )
        )
    return tuple(raised)

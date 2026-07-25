"""The supervisor's memory: durable state behind the M2 admission gate (M3).

M2 shipped :func:`chronos.supervisor.admission.admit` as a pure function over a
``SupervisorState`` the caller assembled. That was the right shape — it makes
the kernel's veto testable without a broker, a database, or a model — but it
left the state itself nowhere. Three consequences, all disclosed at the time and
all closed here:

- ``LossLimits`` and ``ActivityLimits`` were **contract-only**. Nothing counted
  session losses or orders, so a mandate could set them and the kernel would
  never look. This module counts both.
- ``MandateActivation`` was a value the caller passed in, so *"is this mandate
  in force"* had no durable answer — and ``RestartBehavior.REQUIRE_REACTIVATION``
  is specifically about surviving a restart, which an in-memory value cannot do.
- R-31's re-submission counters lived in memory, so a restart reset the attempt
  budget. A model that wanted to route around a refusal only had to wait for
  one.

## Deny-by-default, restated for state

The rule that governs every read here: **absent state is never permissive.**

- An account with no counter row has *not* proven it is under its loss limit; it
  has proven nothing. The read returns zeroed counters, and the caller must
  still supply the equity figures the limits are measured against — a missing
  figure refuses, exactly as a missing ceiling evidence does in sizing.
- An account with no activation row has no authority, and
  :func:`load_activation` returns ``None``, which admission already treats as a
  refusal.
- A decision id with no attempt row is unseen, not approved.

The one place this is subtle is a *fresh upgrade*: migration 0004 creates these
tables empty, so a pre-M3 database wakes up with no activation and no counters.
That reads as "no authority established", which is correct and deliberately
inconvenient — the owner re-activates.

## Why the writes are transactional with the thing they describe

Every function here takes a ``Session`` rather than opening its own. An audit
record or counter increment that commits separately from the decision it
describes can outlive a rolled-back decision, or be lost while the decision
survives. Both are worse than an atomic write, and both are the kind of drift
that is invisible until someone tries to reconstruct what happened.

## What this module does NOT do

It does not decide anything. It reads and writes state and hands it to
``admit``, which remains the only place a decision is judged. Keeping the
judgement pure is what lets the M2 test suite exercise every refusal path
without a database, and that property is worth preserving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.autonomy import AutonomyMandate
from chronos.persistence import hash_chain
from chronos.persistence.schema import (
    AutonomyDecisionAttemptRow,
    AutonomyMandateActivationRow,
    AutonomySessionCounterRow,
)
from chronos.supervisor.admission import (
    AdmissionOutcome,
    DegradedReason,
    MandateActivation,
    MarketDataEvidence,
    SupervisorState,
)

#: Hash-chain stream carrying every decision the supervisor judged, admitted or
#: refused. Named per account so one account's history cannot be invalidated by
#: another's, and so a stream can be verified in isolation.
DECISION_STREAM = "autonomy.decisions"

#: Hash-chain stream carrying owner authority events: activation and revocation.
AUTHORITY_STREAM = "autonomy.authority"


def stream_for(base: str, account_fingerprint: str) -> str:
    """Per-account stream name. Fingerprint only — never a raw account id."""

    return f"{base}:{account_fingerprint}"


@dataclass(frozen=True, slots=True)
class SessionCounters:
    """What this account has already done in this session.

    Every field is a *fact the supervisor observed*, never a model assertion.
    Zeroed counters mean "nothing recorded", which is not the same as "within
    limits" — the caller still needs the equity figures the loss limits are
    measured against.
    """

    session_date: str
    realized_loss_usd: Decimal = Decimal(0)
    peak_equity_usd: Decimal = Decimal(0)
    trough_equity_usd: Decimal = Decimal(0)
    orders_submitted: int = 0
    cancellations: int = 0
    replacements: int = 0
    turnover_usd: Decimal = Decimal(0)

    @property
    def drawdown_usd(self) -> Decimal:
        """Peak-to-trough drawdown, or zero when no peak has been recorded.

        Returns zero rather than a negative number when the trough exceeds the
        peak, which can only happen if a caller recorded them out of order: a
        negative drawdown would *widen* the headroom under a drawdown ceiling,
        turning bad bookkeeping into extra authority.
        """

        if self.peak_equity_usd <= 0:
            return Decimal(0)
        return max(Decimal(0), self.peak_equity_usd - self.trough_equity_usd)

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity_usd <= 0:
            return Decimal(0)
        return self.drawdown_usd / self.peak_equity_usd


def session_key(moment: datetime, *, market_timezone: str | None = None) -> str:
    """The trading session a moment belongs to, as an ISO date.

    M3 shipped this as the UTC calendar date and disclosed the gap as R-34: a
    US equity session in UTC rolls mid-afternoon local, so "session" limits were
    really "UTC day" limits and a single afternoon could straddle two counters.

    Passing ``market_timezone`` converts first, so the boundary falls where the
    market's own day does. It stays **optional and explicit** rather than
    defaulting to a guess: a module that silently assumed New York would be
    inventing the boundary that every loss and activity limit is measured
    across, and inventing it wrongly for anyone trading elsewhere.

    An unknown zone raises rather than falling back to UTC. A silent fallback
    would produce counters that are subtly wrong in exactly the way nobody
    notices — the session would still roll, just not where the operator thinks.

    **Still bounded (R-34).** This aligns the boundary to a market's *calendar
    day*, not to its session calendar: it does not know holidays, half-days, or
    that an overnight futures session spans two dates. For equities and equity
    options, which is the whole of the current capability matrix, a market-local
    day and a session coincide. Futures would need a real calendar, and futures
    are not tradable here.
    """

    if market_timezone is None:
        at: date = moment.date()
        return at.isoformat()
    try:
        zone = ZoneInfo(market_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(
            f"unknown market timezone {market_timezone!r}; refusing to fall back to UTC, "
            "which would roll the session counter somewhere the operator does not expect"
        ) from error
    return moment.astimezone(zone).date().isoformat()


# ----------------------------------------------------------------- activation


def load_activation(
    session: Session, *, account_fingerprint: str, mandate: AutonomyMandate
) -> MandateActivation | None:
    """The owner event that put this mandate in force, or ``None``.

    ``None`` is a refusal in :func:`~chronos.supervisor.admission.admit`, so an
    unknown mandate, a mandate for another account, or a database with no
    activation row all deny rather than default.
    """

    row = session.scalar(
        select(AutonomyMandateActivationRow)
        .where(
            AutonomyMandateActivationRow.mandate_id == mandate.mandate_id,
            AutonomyMandateActivationRow.mandate_version == mandate.mandate_version,
            AutonomyMandateActivationRow.account_fingerprint == account_fingerprint,
        )
        .order_by(AutonomyMandateActivationRow.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return MandateActivation(
        owner_event_id=row.owner_event_id,
        activated_at=row.activated_at,
        revoked=row.revoked,
        process_generation=row.process_generation,
    )


def activate(
    session: Session,
    *,
    account_fingerprint: str,
    mandate: AutonomyMandate,
    owner_event_id: str,
    now: datetime,
    process_generation: int,
) -> AutonomyMandateActivationRow:
    """Record an owner putting a mandate in force.

    This is an *owner* action. Nothing in the model plane may call it, which the
    autonomy isolation tests enforce structurally: ``chronos.autonomy`` cannot
    import ``chronos.supervisor`` at all.
    """

    if not owner_event_id.strip():
        raise ValueError("an activation must cite the owner event that authorized it")
    row = AutonomyMandateActivationRow(
        mandate_id=mandate.mandate_id,
        mandate_version=mandate.mandate_version,
        account_fingerprint=account_fingerprint,
        owner_event_id=owner_event_id,
        activated_at=now,
        revoked=False,
        process_generation=process_generation,
    )
    session.add(row)
    hash_chain.append(
        session,
        stream=stream_for(AUTHORITY_STREAM, account_fingerprint),
        kind="mandate_activated",
        payload={
            "mandate_id": mandate.mandate_id,
            "mandate_version": mandate.mandate_version,
            "mode": mandate.mode.value,
            "owner_event_id": owner_event_id,
            "process_generation": process_generation,
            "expires_at": mandate.expires_at.isoformat(),
        },
        recorded_at=now,
    )
    return row


def revoke(
    session: Session,
    *,
    account_fingerprint: str,
    mandate_id: str,
    reason: str,
    now: datetime,
) -> bool:
    """Revoke authority. Returns False when there was nothing in force.

    Revocation marks the activation in place rather than deleting it: an audit
    trail that forgets a revocation cannot show when authority ended, which is
    the one question an incident review will ask first.
    """

    row = session.scalar(
        select(AutonomyMandateActivationRow)
        .where(
            AutonomyMandateActivationRow.mandate_id == mandate_id,
            AutonomyMandateActivationRow.account_fingerprint == account_fingerprint,
            AutonomyMandateActivationRow.revoked.is_(False),
        )
        .order_by(AutonomyMandateActivationRow.id.desc())
        .limit(1)
    )
    if row is None:
        return False
    row.revoked = True
    row.revoked_at = now
    row.revoked_reason = reason
    hash_chain.append(
        session,
        stream=stream_for(AUTHORITY_STREAM, account_fingerprint),
        kind="mandate_revoked",
        payload={"mandate_id": mandate_id, "reason": reason},
        recorded_at=now,
    )
    return True


# ------------------------------------------------------------------- counters


def load_counters(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    market_timezone: str | None = None,
) -> SessionCounters:
    """This session's counters, zeroed when nothing has been recorded.

    Zeroed is not "within limits" — see the module docstring. It is the honest
    statement that this session has no recorded activity yet.
    """

    key = session_key(now, market_timezone=market_timezone)
    row = _counter_row(session, account_fingerprint=account_fingerprint, session_date=key)
    if row is None:
        return SessionCounters(session_date=key)
    return SessionCounters(
        session_date=key,
        realized_loss_usd=row.realized_loss_usd,
        peak_equity_usd=row.peak_equity_usd,
        trough_equity_usd=row.trough_equity_usd,
        orders_submitted=row.orders_submitted,
        cancellations=row.cancellations,
        replacements=row.replacements,
        turnover_usd=row.turnover_usd,
    )


def _counter_row(
    session: Session, *, account_fingerprint: str, session_date: str
) -> AutonomySessionCounterRow | None:
    return session.scalar(
        select(AutonomySessionCounterRow).where(
            AutonomySessionCounterRow.account_fingerprint == account_fingerprint,
            AutonomySessionCounterRow.session_date == session_date,
        )
    )


def record_equity(
    session: Session,
    *,
    account_fingerprint: str,
    equity_usd: Decimal,
    now: datetime,
    market_timezone: str | None = None,
) -> SessionCounters:
    """Track the session's peak and trough equity for the drawdown ceiling.

    The first observation seeds both, so a session that only ever falls still
    has a peak to measure the fall against.
    """

    key = session_key(now, market_timezone=market_timezone)
    row = _ensure_counter_row(session, account_fingerprint=account_fingerprint, session_date=key)
    if row.peak_equity_usd <= 0 and row.trough_equity_usd <= 0:
        row.peak_equity_usd = equity_usd
        row.trough_equity_usd = equity_usd
    else:
        row.peak_equity_usd = max(row.peak_equity_usd, equity_usd)
        row.trough_equity_usd = min(row.trough_equity_usd, equity_usd)
    row.updated_at = now
    return load_counters(
        session, account_fingerprint=account_fingerprint, now=now, market_timezone=market_timezone
    )


def record_activity(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    orders_submitted: int = 0,
    cancellations: int = 0,
    replacements: int = 0,
    turnover_usd: Decimal = Decimal(0),
    realized_loss_usd: Decimal = Decimal(0),
    market_timezone: str | None = None,
) -> SessionCounters:
    """Increment this session's activity counters.

    Increments are additive and never negative: a caller that could *decrease*
    a count could also restore headroom under an activity ceiling, which would
    make the ceiling advisory. A negative argument is a programming error and
    raises rather than being clamped silently.
    """

    for label, value in (
        ("orders_submitted", orders_submitted),
        ("cancellations", cancellations),
        ("replacements", replacements),
    ):
        if value < 0:
            raise ValueError(f"{label} increment must not be negative, got {value}")
    for label, amount in (
        ("turnover_usd", turnover_usd),
        ("realized_loss_usd", realized_loss_usd),
    ):
        if amount < 0:
            raise ValueError(f"{label} increment must not be negative, got {amount}")

    key = session_key(now, market_timezone=market_timezone)
    row = _ensure_counter_row(session, account_fingerprint=account_fingerprint, session_date=key)
    row.orders_submitted += orders_submitted
    row.cancellations += cancellations
    row.replacements += replacements
    row.turnover_usd += turnover_usd
    row.realized_loss_usd += realized_loss_usd
    row.updated_at = now
    return load_counters(
        session, account_fingerprint=account_fingerprint, now=now, market_timezone=market_timezone
    )


def _ensure_counter_row(
    session: Session, *, account_fingerprint: str, session_date: str
) -> AutonomySessionCounterRow:
    row = _counter_row(session, account_fingerprint=account_fingerprint, session_date=session_date)
    if row is None:
        row = AutonomySessionCounterRow(
            account_fingerprint=account_fingerprint,
            session_date=session_date,
            realized_loss_usd=Decimal(0),
            peak_equity_usd=Decimal(0),
            trough_equity_usd=Decimal(0),
            orders_submitted=0,
            cancellations=0,
            replacements=0,
            turnover_usd=Decimal(0),
        )
        session.add(row)
        session.flush()
    return row


# --------------------------------------------------------- decision attempts


def load_attempts(
    session: Session, *, account_fingerprint: str
) -> tuple[frozenset[str], dict[str, int]]:
    """Admitted decision ids and refusal counts, for replay protection (R-31).

    Returned in the shape ``admit`` already consumes, so durability is a change
    of *source*, not of the admission contract.
    """

    rows = list(
        session.scalars(
            select(AutonomyDecisionAttemptRow).where(
                AutonomyDecisionAttemptRow.account_fingerprint == account_fingerprint
            )
        )
    )
    admitted = frozenset(row.decision_id for row in rows if row.admitted)
    refusals = {row.decision_id: row.refusals for row in rows if row.refusals}
    return admitted, refusals


def record_outcome(
    session: Session,
    *,
    account_fingerprint: str,
    decision_id: str,
    outcome: AdmissionOutcome,
    now: datetime,
    decision_payload: dict[str, Any] | None = None,
) -> None:
    """Persist what the supervisor decided, and chain it.

    Both halves happen in the caller's transaction: the attempt counter that
    bounds re-submission and the journal entry that explains it commit together,
    so a counter can never claim a refusal the journal cannot account for.
    """

    row = session.scalar(
        select(AutonomyDecisionAttemptRow).where(
            AutonomyDecisionAttemptRow.account_fingerprint == account_fingerprint,
            AutonomyDecisionAttemptRow.decision_id == decision_id,
        )
    )
    if row is None:
        row = AutonomyDecisionAttemptRow(
            account_fingerprint=account_fingerprint,
            decision_id=decision_id,
            admitted=False,
            refusals=0,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(row)
        session.flush()
    row.last_seen_at = now
    if outcome.admitted:
        row.admitted = True
    else:
        row.refusals += 1

    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "admitted": outcome.admitted,
        "refusal": outcome.refusal.value if outcome.refusal else None,
        "detail": outcome.detail,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "evaluated": check.evaluated,
                "detail": check.detail,
            }
            for check in outcome.checks
        ],
    }
    if decision_payload:
        payload["decision"] = decision_payload
    hash_chain.append(
        session,
        stream=stream_for(DECISION_STREAM, account_fingerprint),
        kind="admitted" if outcome.admitted else "refused",
        payload=payload,
        recorded_at=now,
    )


# --------------------------------------------------------------- limit checks


def limit_breaches(
    mandate: AutonomyMandate, counters: SessionCounters
) -> tuple[DegradedReason, ...]:
    """Which of the mandate's loss and activity ceilings this session has hit.

    Returned as ``DegradedReason``s because that is the lever admission already
    understands, and because it produces the behavior the directive asks for:
    a breached limit stops *new exposure* while leaving risk reduction available.
    Every reason here is therefore ``blocks_risk_reduction=False`` — being at a
    loss limit is the moment closing must remain possible, and refusing a close
    would trap the position the limit exists to cap.

    A ceiling of zero does not bind, matching ``CapitalLimits`` semantics only in
    the sense that these are *breach* comparisons rather than headroom ones: a
    zero loss ceiling would mean "no loss is tolerable", which would halt on the
    first adverse tick, so the mandate validator requires submitting mandates to
    set the ones it wants enforced and zero is read as unset here. This asymmetry
    is deliberate and is disclosed in ``docs/limitations.md``.
    """

    breaches: list[DegradedReason] = []
    loss = mandate.loss
    activity = mandate.activity

    if loss.max_session_loss_usd > 0 and counters.realized_loss_usd >= loss.max_session_loss_usd:
        breaches.append(
            DegradedReason(
                subsystem="loss_limit",
                detail=(
                    f"session realized loss {counters.realized_loss_usd} reached the mandate's "
                    f"{loss.max_session_loss_usd} ceiling"
                ),
                blocks_risk_reduction=False,
            )
        )
    if loss.max_daily_loss_usd > 0 and counters.realized_loss_usd >= loss.max_daily_loss_usd:
        breaches.append(
            DegradedReason(
                subsystem="loss_limit",
                detail=(
                    f"daily realized loss {counters.realized_loss_usd} reached the mandate's "
                    f"{loss.max_daily_loss_usd} ceiling"
                ),
                blocks_risk_reduction=False,
            )
        )
    if (
        loss.max_peak_to_trough_drawdown_usd > 0
        and counters.drawdown_usd >= loss.max_peak_to_trough_drawdown_usd
    ):
        breaches.append(
            DegradedReason(
                subsystem="drawdown_limit",
                detail=(
                    f"peak-to-trough drawdown {counters.drawdown_usd} reached the mandate's "
                    f"{loss.max_peak_to_trough_drawdown_usd} ceiling"
                ),
                blocks_risk_reduction=False,
            )
        )
    if (
        loss.max_peak_to_trough_drawdown_pct > 0
        and counters.drawdown_pct >= loss.max_peak_to_trough_drawdown_pct
    ):
        breaches.append(
            DegradedReason(
                subsystem="drawdown_limit",
                detail=(
                    f"peak-to-trough drawdown {counters.drawdown_pct:.4f} reached the mandate's "
                    f"{loss.max_peak_to_trough_drawdown_pct} fraction"
                ),
                blocks_risk_reduction=False,
            )
        )

    for value, ceiling, label in (
        (counters.orders_submitted, activity.max_orders_per_session, "orders"),
        (counters.cancellations, activity.max_cancellations_per_session, "cancellations"),
        (counters.replacements, activity.max_replacements_per_session, "replacements"),
    ):
        if ceiling > 0 and value >= ceiling:
            breaches.append(
                DegradedReason(
                    subsystem="activity_limit",
                    detail=f"{label} this session ({value}) reached the mandate's {ceiling}",
                    blocks_risk_reduction=False,
                )
            )
    if (
        activity.max_turnover_usd_per_session > 0
        and counters.turnover_usd >= activity.max_turnover_usd_per_session
    ):
        breaches.append(
            DegradedReason(
                subsystem="activity_limit",
                detail=(
                    f"session turnover {counters.turnover_usd} reached the mandate's "
                    f"{activity.max_turnover_usd_per_session}"
                ),
                blocks_risk_reduction=False,
            )
        )
    return tuple(breaches)


# ------------------------------------------------------- assembling the state


def build_state(
    session: Session,
    *,
    account_fingerprint: str,
    mandate: AutonomyMandate,
    now: datetime,
    process_generation: int,
    expected_evidence_bundle_id: str | None = None,
    expected_evidence_bundle_digest: str | None = None,
    market_data: MarketDataEvidence | None = None,
    extra_degraded_reasons: tuple[DegradedReason, ...] = (),
    market_timezone: str | None = None,
) -> SupervisorState:
    """Assemble the state `admit` judges against, from durable facts.

    This is the join that makes M2's contract-only limits real. `admit` did not
    change: it still refuses on a degraded reason. What changed is that a
    breached loss or activity ceiling now *produces* one, read from counters
    that survive a restart, instead of relying on a caller to notice and pass
    one by hand.

    Routing the breaches through `DegradedReason` rather than adding new refusal
    codes is deliberate. It gets the directive's behavior for free: a breached
    limit stops new exposure while leaving risk reduction available, which is
    exactly what a loss limit should do — the position it capped must still be
    closable. New codes would have needed that rule re-implemented, and a second
    implementation of a safety rule is a second chance to get it wrong.

    `extra_degraded_reasons` is for degradations this module cannot observe —
    an unreachable broker, a stale resolver, a lost lease. They are appended,
    never used to override: a caller cannot pass a reason that *clears* a
    breach this function found.
    """

    counters = load_counters(
        session, account_fingerprint=account_fingerprint, now=now, market_timezone=market_timezone
    )
    admitted, refusals = load_attempts(session, account_fingerprint=account_fingerprint)
    return SupervisorState(
        account_fingerprint=account_fingerprint,
        now=now,
        activation=load_activation(
            session, account_fingerprint=account_fingerprint, mandate=mandate
        ),
        process_generation=process_generation,
        degraded_reasons=limit_breaches(mandate, counters) + tuple(extra_degraded_reasons),
        admitted_decision_ids=admitted,
        refused_decision_attempts=dict(refusals),
        expected_evidence_bundle_id=expected_evidence_bundle_id,
        expected_evidence_bundle_digest=expected_evidence_bundle_digest,
        market_data=market_data,
    )

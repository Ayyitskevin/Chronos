"""The tick that actually drives autonomy (R-36, M7).

M5 built ``run_cycle`` and M6 made it reachable, and both disclosed that nothing
called it on a schedule. This is that caller: a supervised loop that drains the
proposal queue, delivers pending alerts, and observes its own health.

## Time-driven, with events as hints — and why not the other way round

A tick is the **only** thing that runs cycles. Events do not call
:func:`run_tick`; they set a flag that lets the *next* tick happen sooner, never
sooner than :attr:`RuntimeConfig.minimum_interval_seconds`.

That asymmetry is the safety property. Under a genuinely event-driven design an
event storm is an order storm: a volatile open, a burst of fills, or a
misbehaving worker would each drive cycles at whatever rate the *market* or the
*caller* chose. The mandate's activity limits would eventually refuse, but a
limit catching a problem the design created is not the same as a design that
does not create it. Under a pure timer, a fill waits for the next tick, which is
its own kind of wrong.

Coalescing gives the responsiveness without the unboundedness: any number of
events between two ticks collapse into "wake early once", and the floor bounds
the worst case regardless of how many arrive. Cycles per hour is therefore a
property of the configuration, not of the day's volatility.

## Non-live unless everything says otherwise

``submit`` is optional here exactly as it is in ``run_cycle``, and the default
is ``None``. A runtime wired without it walks every gate and places no order.
Combined with admission's independent mode check, an order requires the mandate
to permit submission **and** the operator to have supplied the handoff — two
separate deliberate acts, neither of which is a default.

## Supervision: a tick that fails must not become a tick that stops silently

Every tick is wrapped. A failing tick records the failure, raises an owner
alert, and **returns** — the loop keeps its cadence rather than dying. The one
thing it must never do is fail quietly, because a runtime that stopped without
saying so looks exactly like a runtime with nothing to do.

Consecutive failures escalate: past :attr:`RuntimeConfig.max_consecutive_failures`
the runtime stops itself and raises a CRITICAL alert. A loop that keeps ticking
into a broken dependency generates noise, not progress, and stopping is the
honest response to "I cannot do my job".

## What this module still does not own

It does not gather facts. :class:`FactGatherer` is a callable the operator
supplies, because assembling ``CycleFacts`` needs the broker, the clock and the
market-data feed — and a module that reached those would be one the model plane
could reach through. Keeping it injected is also what lets every path here be
tested without a broker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from chronos.autonomy import AutonomyMandate
from chronos.supervisor import alerts, delivery, evidence_bundles, proposals, queue
from chronos.supervisor.loop import (
    CycleFacts,
    CycleOutcome,
    CycleStage,
    Handoff,
    InstrumentGatherer,
    run_cycle,
)

_logger = logging.getLogger("chronos.supervisor.runtime")


class FactGatherer(Protocol):
    """Assembles the supervisor's view for one cycle.

    Supplied by the operator because it needs a broker, a clock, and market
    data. Returning ``None`` means the facts could not be gathered — which is a
    *refusal to run*, not an error: a cycle without facts would have to invent
    the numbers it judges against.
    """

    def __call__(self, now: datetime) -> CycleFacts | None:  # pragma: no cover - Protocol
        ...


#: Supplies the mandate in force, or ``None``. Separate from the fact gatherer
#: because authority and market state come from different places and fail
#: independently — and a runtime that could not tell those apart would report
#: "no mandate" when the broker was merely unreachable.
MandateSource = Callable[[], AutonomyMandate | None]


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """Who a queued proposal is stamped as, or why it cannot be (ADR-0023, A3).

    A bare ``HarnessIdentity | None`` was enough while there was one way to fail
    — the registration was not current. A revoked credential is a different
    event with a different response (the owner killed this proposer mid-session,
    on purpose), and a journal that cannot tell the two apart cannot answer the
    first question of an incident review: was this a credential we killed, or
    one that simply aged out?

    So the resolver returns its refusal alongside. ``refusal`` and ``detail``
    are ignored when ``identity`` is present, and default to today's values, so
    every existing caller keeps its exact behavior.
    """

    identity: queue.HarnessIdentity | None = None
    refusal: str = "PROPOSER_UNRESOLVED"
    detail: str = ""


#: Resolves the identity a queued proposal is stamped with (ADR-0023): the
#: verified proposer_id recorded at enqueue (or ``None`` for pre-registry
#: rows), judged against the tick's clock and the durable revocation ledger
#: (A3), to the registration's HarnessIdentity — or to a refusal the cycle
#: records at the STAMP stage rather than guessing an author.
#:
#: The session is passed in because revocation is durable state read at the
#: moment authority is exercised, and the tick already holds the transaction it
#: must be read in: a resolver that opened its own would be reading a different
#: instant than the one it is judging.
IdentityResolver = Callable[[Session, str | None, datetime], ResolvedIdentity]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """How the tick behaves. Every default is the conservative one."""

    account_fingerprint: str
    #: The floor between cycles. An event can pull the next tick forward to this
    #: boundary and no further, which is what bounds the worst case.
    minimum_interval_seconds: float = 5.0
    #: The unhurried cadence when nothing has happened.
    idle_interval_seconds: float = 60.0
    #: Proposals judged per tick. Bounded so a flood cannot starve alert
    #: delivery — which is the work that would *report* the flood.
    proposals_per_tick: int = 10
    #: Alerts delivered per tick.
    alerts_per_tick: int = 50
    #: Consecutive failed ticks before the runtime stops itself.
    max_consecutive_failures: int = 5

    def __post_init__(self) -> None:
        if self.minimum_interval_seconds <= 0:
            raise ValueError(
                "minimum_interval_seconds must be positive; a zero floor makes the tick "
                "event-driven, which is the unbounded shape this design rejects"
            )
        if self.idle_interval_seconds < self.minimum_interval_seconds:
            raise ValueError(
                "idle_interval_seconds must not be below the minimum interval; an idle "
                "cadence faster than the floor would make the floor meaningless"
            )


@dataclass
class TickReport:
    """What one tick did. Mutable so the tick can fill it in as it goes.

    The handoff breakdown is four separate counters (A1) because
    ``orders_handed_off`` used to be incremented for every cycle whose handoff
    did not raise — a read-only refusal counted as an order handed off, and an
    operator reading the number was being told the system had done something it
    had not. Each counter now names exactly one wire truth, and the total that
    matters to an activity ceiling is the one the counting rule defines.
    """

    at: datetime
    proposals_judged: int = 0
    #: Attempts that consumed activity budget: confirmed, unconfirmed, and
    #: rejected-after-send. Equivalently: every cycle that could not prove
    #: nothing reached the wire. Refusals before the wire are NOT here.
    orders_handed_off: int = 0
    #: Confirmed working, partially filled, or filled orders. Nothing weaker.
    orders_confirmed: int = 0
    #: Sends whose outcome is unknown. Each raised a CRITICAL owner alert.
    orders_unconfirmed: int = 0
    #: Sends the venue answered with a non-active lifecycle.
    orders_rejected_after_send: int = 0
    #: Order-plane refusals that provably sent nothing, and spend no budget.
    #: Counts only *classified* refusals: shadow mode (no handoff configured) and
    #: an exception out of the callable stop at ``HANDOFF`` with no disposition,
    #: and are not folded in here as if the order plane had answered.
    handoff_refusals: int = 0
    alerts_delivered: int = 0
    #: Expired evidence-bundle rows reclaimed this tick (ADR-0028's retention
    #: rule). Always zero under the unset posture, which writes no bundles.
    evidence_bundles_pruned: int = 0
    queue_depth: int = 0
    failure: str = ""
    outcomes: list[CycleOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failure


class AutonomyRuntime:
    """Drives cycles on a tick, and coalesces events into "wake early".

    Deliberately synchronous and single-threaded. Two ticks overlapping would
    mean two cycles reading the same counters and each deciding it had room —
    a classic double-spend of an activity limit. Under the single-writer lease
    a second runtime is already a bug; this makes a second *tick* impossible
    within one runtime as well.
    """

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        config: RuntimeConfig,
        identity: queue.HarnessIdentity,
        mandate_source: MandateSource,
        gather_facts: FactGatherer,
        sinks: tuple[delivery.AlertSink, ...] = (),
        submit: Handoff | None = None,
        gather_instrument: InstrumentGatherer | None = None,
        resolve_identity: IdentityResolver | None = None,
        bind_evidence: bool = False,
    ) -> None:
        self._sessions = sessions
        self._config = config
        self._identity = identity
        # ADR-0023: when a proposer registry is configured, identity is
        # per-proposal, resolved from the credential the route verified. When
        # no resolver is wired, every proposal is stamped with the static
        # `identity` above — the pre-registry posture, unchanged.
        self._resolve_identity = resolve_identity
        # ADR-0028: with evidence binding in force, every proposal must cite a
        # bundle this backend issued to that proposer and that has not expired
        # against the drain's clock. False is the pre-ADR-0028 posture verbatim.
        self._bind_evidence = bind_evidence
        self._mandate_source = mandate_source
        self._gather_facts = gather_facts
        self._gather_instrument = gather_instrument
        self._sinks = sinks or delivery.default_sinks()
        self._submit = submit
        self._wake_early = False
        self._consecutive_failures = 0
        self._stopped = False
        self._last_tick: datetime | None = None

    # --- event coalescing ---------------------------------------------------

    def note_event(self, reason: str = "") -> None:
        """Ask for the next tick sooner. Idempotent between ticks.

        Any number of events collapse into one flag, so a burst cannot produce
        a burst of cycles. This is the whole of the event-driven surface: there
        is deliberately no way for a caller to *run* a cycle.
        """

        self._wake_early = True
        if reason:
            _logger.debug("Autonomy wake requested: %s", reason, extra={"event": "autonomy_wake"})

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def mandate(self) -> AutonomyMandate | None:
        """The grant this runtime is judging against, as its own source reports it.

        Added for the operator terminal (ADR-0018 §6), which must show the
        authority the tick is *actually* applying. A route that re-read the
        mandate file instead would show what is on disk, so an owner who edited
        the file after boot would be shown a grant this runtime is not using —
        a panel that looks safer than the process it describes.

        Read-only by construction: it returns what ``mandate_source`` returns and
        cannot set, replace, or activate anything. ``None`` means the source has
        no mandate to give, which every caller must read as *no authority
        established* rather than as an error.
        """

        return self._mandate_source()

    def seconds_until_next_tick(self, now: datetime) -> float:
        """How long to wait. The floor applies whether or not an event arrived."""

        if self._stopped:
            return float("inf")
        target = (
            self._config.minimum_interval_seconds
            if self._wake_early
            else self._config.idle_interval_seconds
        )
        if self._last_tick is None:
            return 0.0
        elapsed = (now - self._last_tick).total_seconds()
        return max(0.0, target - elapsed)

    # --- the tick -----------------------------------------------------------

    def run_tick(self, now: datetime) -> TickReport:
        """One pass: drain proposals, deliver alerts, report.

        Never raises. A tick that fails records why, alerts, and returns, so the
        loop keeps its cadence. The failure that must never happen is the silent
        one: a runtime that stopped without saying so is indistinguishable from
        a runtime with nothing to do.
        """

        report = TickReport(at=now)
        if self._stopped:
            report.failure = "runtime stopped"
            return report

        self._last_tick = now
        self._wake_early = False
        try:
            self._tick(now, report)
        except Exception as error:
            # The exception type, never its message: a hostile proposal must not
            # be able to write chosen text into an operator's logs through the
            # one path guaranteed to be read.
            report.failure = f"tick raised {type(error).__name__}"
            _logger.exception("Autonomy tick failed", extra={"event": "autonomy_tick_failed"})

        if report.ok:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._record_failure(report, now)
        return report

    def _tick(self, now: datetime, report: TickReport) -> None:
        mandate = self._mandate_source()
        facts = self._gather_facts(now)

        with self._sessions.begin() as session:
            report.queue_depth = proposals.pending_depth(
                session, account_fingerprint=self._config.account_fingerprint
            )
            if facts is None:
                # No facts is a refusal to run, not an error: a cycle without
                # them would have to invent the numbers it judges against. The
                # queue is left intact so the work happens once facts return.
                alerts.raise_alert(
                    session,
                    account_fingerprint=self._config.account_fingerprint,
                    severity=alerts.AlertSeverity.WARNING,
                    kind="runtime.no_facts",
                    summary="the supervisor could not gather the facts a cycle needs",
                    now=now,
                )
            else:
                batch = proposals.claim_batch(
                    session,
                    account_fingerprint=self._config.account_fingerprint,
                    limit=self._config.proposals_per_tick,
                )

        if facts is not None:
            self._drain(batch, mandate, facts, now, report)

        with self._sessions.begin() as session:
            delivered = delivery.deliver_pending(
                session,
                account_fingerprint=self._config.account_fingerprint,
                sinks=self._sinks,
                now=now,
                limit=self._config.alerts_per_tick,
            )
            report.alerts_delivered = delivered.delivered

            if self._bind_evidence:
                # ADR-0028's retention rule, run as tick housekeeping rather than
                # at issuance: a proposer that stops asking for bundles must not
                # be what stops the table being reclaimed. Only the lookup rows
                # go, and only long after their authority lapsed — the
                # hash-chained record of what was issued is never pruned.
                report.evidence_bundles_pruned = evidence_bundles.prune_expired(
                    session,
                    account_fingerprint=self._config.account_fingerprint,
                    now=now,
                )

    def _drain(
        self,
        batch: tuple[proposals.QueuedProposal, ...],
        mandate: AutonomyMandate | None,
        facts: CycleFacts,
        now: datetime,
        report: TickReport,
    ) -> None:
        for item in batch:
            with self._sessions() as session:
                try:
                    resolved = ResolvedIdentity(identity=self._identity)
                    if self._resolve_identity is not None:
                        # Resolved per proposal AND per tick against the drain's own
                        # clock: a registration that expired between enqueue and drain
                        # refuses at the moment authority is exercised, not the moment
                        # bytes were received. Since A3 the same call consults the
                        # durable revocation ledger, so a credential the owner killed
                        # mid-session refuses here too — without a restart, which is
                        # the whole point of that act. (The resolver's registry is
                        # still a boot-time snapshot for everything else: file edits
                        # such as disabling an entry are honored at the next restart;
                        # see build_identity_resolver.)
                        resolved = self._resolve_identity(session, item.proposer_id, facts.now)
                    outcome = run_cycle(
                        item.payload,
                        session=session,
                        mandate=mandate,
                        identity=resolved.identity,
                        identity_refusal=resolved.refusal,
                        identity_detail=resolved.detail,
                        facts=facts,
                        submit=self._submit,
                        gather_instrument=self._gather_instrument,
                        bind_evidence=self._bind_evidence,
                    )
                    proposals.mark_processed(
                        session,
                        queue_id=item.id,
                        stage=outcome.stage.value,
                        refusal=outcome.refusal,
                        now=now,
                    )
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
            report.outcomes.append(outcome)
            report.proposals_judged += 1
            self._count_handoff(outcome, report)

    @staticmethod
    def _count_handoff(outcome: CycleOutcome, report: TickReport) -> None:
        """Tally one cycle's handoff by what the order plane actually did (A1).

        Reads ``counted_activity_attempt`` rather than re-deriving the rule from
        the stage: the rule has exactly one home (``supervisor.handoff``), and a
        second copy here would be free to drift from the counter the activity
        ceiling actually reads.
        """

        if outcome.counted_activity_attempt:
            report.orders_handed_off += 1
        if outcome.stage is CycleStage.COMPLETE:
            report.orders_confirmed += 1
        elif outcome.stage is CycleStage.SENT_UNCONFIRMED:
            report.orders_unconfirmed += 1
        elif outcome.stage is CycleStage.REJECTED_AFTER_SEND:
            report.orders_rejected_after_send += 1
        elif outcome.handoff_result is not None:
            report.handoff_refusals += 1

    def _record_failure(self, report: TickReport, now: datetime) -> None:
        """Alert on a failed tick, and stop if failures are consecutive.

        Uses its own session: the tick's transaction may be the thing that
        failed, and an alert written inside a doomed transaction is an alert
        that never existed.
        """

        exhausted = self._consecutive_failures >= self._config.max_consecutive_failures
        if exhausted:
            self._stopped = True
        try:
            with self._sessions.begin() as session:
                alerts.raise_alert(
                    session,
                    account_fingerprint=self._config.account_fingerprint,
                    severity=(
                        alerts.AlertSeverity.CRITICAL if exhausted else alerts.AlertSeverity.WARNING
                    ),
                    kind="runtime.tick_failed",
                    summary=(
                        f"the autonomy runtime stopped after {self._consecutive_failures} "
                        "consecutive failed ticks"
                        if exhausted
                        else f"an autonomy tick failed: {report.failure}"
                    ),
                    detail={"consecutive_failures": self._consecutive_failures},
                    now=now,
                )
                delivery.deliver_pending(
                    session,
                    account_fingerprint=self._config.account_fingerprint,
                    sinks=self._sinks,
                    now=now,
                    limit=self._config.alerts_per_tick,
                )
        except Exception:
            # The database is the thing that failed. The log sink is the floor
            # that remains, and it is why one exists.
            _logger.critical(
                "Autonomy runtime could not record a tick failure; %s",
                "the runtime has stopped" if exhausted else "it continues",
                extra={"event": "autonomy_alert_unrecordable"},
            )

    def stop(self, reason: str = "") -> None:
        """Stop ticking. Deliberate, and not reversible on this instance.

        Restarting is an operator act: a runtime that could restart itself would
        obscure why it stopped, and "why did it stop" is the first question.
        """

        self._stopped = True
        _logger.warning(
            "Autonomy runtime stopped: %s",
            reason or "no reason given",
            extra={"event": "autonomy_runtime_stopped"},
        )

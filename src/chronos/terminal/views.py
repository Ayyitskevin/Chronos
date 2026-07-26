"""The panel read-models: everything the terminal is allowed to say (M8a).

ADR-0018 §6 puts the terminal's contract in Python. These are the models that
contract is made of — one per panel, assembled from durable state by a function
that takes a session and explicit facts and returns a value. The browser draws
what these describe and nothing else.

That makes this module the terminal's **disclosure boundary**, which is why its
tests live in ``tests/safety/``. Two rules govern every field below, and both
are structural rather than procedural:

1. **Nothing here may carry a secret or an identifier.** No credential, no 2FA
   code, no raw arm or confirmation phrase, and no full broker account id. The
   ``account_fingerprint`` is a 64-character pseudonym (``chronos.utils.
   identifiers``) and is safe to return; it is the *only* account-shaped value
   any of these models carries.
2. **Nothing here may be invented.** A value the assembler could not read is
   ``None``, and ``None`` means *unknown*, never *zero*. This is the one rule
   that most changes how the models look, so it is worth stating what it costs:
   almost every numeric field is ``Optional``, and the client has to handle
   that. The alternative — a plausible default — is worse in exactly the case
   that matters. An account with no counter row has not proven it is under its
   loss limit; it has proven nothing, and a panel reading ``realized loss: 0``
   would be asserting the opposite of the truth at the moment the operator most
   needs it. ``chronos.supervisor.durable`` makes the same argument about the
   same rows, and ``counters_recorded`` is how this module keeps the
   distinction its ``SessionCounters`` cannot (that type zeroes an absent row,
   which is right for admission and wrong for a display).

## What "unknown" actually looks like here

- No account fingerprint (a backend not bound to an account) means every
  per-account count is ``None``. Counting rows for the empty fingerprint would
  return zero, and zero would read as "nothing pending" rather than "nothing to
  ask".
- No autonomy runtime means ``autonomy_stopped`` and ``seconds_until_next_tick``
  are ``None``, not ``True``/``0.0``. A configured-and-stopped runtime is a
  different fact from an unconfigured one, and the panel must not merge them.
- A stopped runtime has no next tick. ``seconds_until_next_tick`` is ``None``
  there too — ``AutonomyRuntime`` returns infinity, which is not JSON, and
  clamping it to a large float would invent a schedule. ``autonomy_stopped``
  carries the distinction.

## Why the assemblers take plain values rather than handles

Nothing in this module imports ``chronos.orders``, ``chronos.broker`` or
``chronos.execution``, and it never will. The kill switch and the arm state
arrive as ``bool | None`` that the route reads from the objects it already
holds, and the tick schedule arrives as a :class:`TickSchedule` — a structural
protocol satisfied by ``AutonomyRuntime`` without this module naming it. So the
terminal's read-models cannot acquire a broker handle, an arming service, or a
submission surface even by accident: there is no import through which one could
arrive. That is ADR-0018 §4 expressed as an import graph rather than a promise.

The assemblers are also free of FastAPI. They take a ``Session`` and return a
model, which is what lets every property below be tested against an in-memory
database with no app, no lease, and no broker.

## Narrative is text (ADR-0018 §residual 6, R-30)

``JournalEntryView.detail`` and ``QueueEntryView.refusal`` can contain text
derived from an external worker's payload. It is displayed so the owner can
*see* what a model said, and it is never markup and never an order parameter.
The client renders these through ``textContent``; nothing here escapes, encodes,
or interprets them, because a model that tried to would be the second place the
question gets answered.

## Honest residuals

- **The journal is displayed, not verified.** Reading the ``autonomy.cycles``
  stream does not recompute its hash chain; ``chronos.persistence.hash_chain.
  verify`` is an operator tool and running it on every poll would rehash the
  whole stream each time. A tampered record would render as a normal one here.
- **:data:`CYCLE_STREAM` duplicates a literal** that ``chronos.supervisor.loop``
  writes inline. ``tests/safety/test_terminal_views.py`` pins the two together
  by writing a real cycle record and reading it back through
  :func:`journal_view`, so the duplication cannot drift silently.
- **The alerts panel is the pending set.** It reports ``acknowledged`` from the
  row rather than assuming it, but it selects unacknowledged rows, so today the
  field is always ``False``. Acknowledged history is not a panel yet.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from chronos.autonomy import AutonomyMandate
from chronos.domain.models import ChronosModel
from chronos.persistence import hash_chain
from chronos.persistence.schema import (
    AutonomyOwnerAlertRow,
    AutonomyProposalQueueRow,
    AutonomySessionCounterRow,
    HashChainRow,
)
from chronos.supervisor import alerts as alert_module
from chronos.supervisor import durable, proposals

#: The hash-chain stream every cycle appends to, whatever the outcome. Written
#: inline by ``chronos.supervisor.loop._record``; named here because the journal
#: panel is the only reader and a magic string in a read path is a magic string
#: that goes stale unnoticed. The safety tests pin the two together.
CYCLE_STREAM = "autonomy.cycles"

DEFAULT_JOURNAL_LIMIT = 50
MAX_JOURNAL_LIMIT = 500
DEFAULT_QUEUE_LIMIT = 25
MAX_QUEUE_LIMIT = 200
DEFAULT_ALERT_LIMIT = 50
MAX_ALERT_LIMIT = 200

#: How much of one cycle's detail string a panel will carry. The string is
#: Chronos-authored but can interpolate worker-supplied text (a symbol, an
#: instrument that would not resolve), so it is bounded — and when it is cut,
#: ``JournalEntryView.detail_truncated`` says so rather than letting the owner
#: read a sentence that silently ends early.
MAX_DETAIL_CHARS = 2000


class TickSchedule(Protocol):
    """The slice of the autonomy runtime a status panel needs.

    Structural on purpose: ``chronos.supervisor.runtime.AutonomyRuntime``
    satisfies it without this module importing it, so the read-models stay
    unable to reach the tick, the queue drain, or the handoff — they can ask
    when the next tick is and whether the runtime stopped, and that is all.
    """

    @property
    def stopped(self) -> bool: ...  # pragma: no cover - Protocol

    def seconds_until_next_tick(self, now: datetime) -> float: ...  # pragma: no cover - Protocol


#: What the absence of a limit means. A number would have to pick a value for
#: "unset", and every available value is a lie: zero reads as "nothing
#: permitted" where ADR-0017 discretion means "no ceiling", and as "no loss
#: tolerated" where ``durable.limit_breaches`` treats zero as unenforced.
LimitEffect = Literal["BINDS", "NO_CEILING", "AUTHORIZES_NOTHING", "NOT_ENFORCED", "NO_FLOOR"]


class MandateLimitView(ChronosModel):
    """One limit the owner set, or did not — and what its absence means.

    ``value`` is ``None`` when the mandate leaves the limit unset, and ``effect``
    says how the supervisor will read that silence. The three unset readings are
    genuinely different and a panel that showed ``0`` for all of them would
    misrepresent the grant in the direction of looking safer than it is:

    - ``AUTHORIZES_NOTHING`` — a sizing ceiling left at zero without model
      discretion. ADR-0016 deny-by-default: every order sizes to nothing.
    - ``NO_CEILING`` — the same field under ADR-0017 ``model_discretion``, where
      an unset ceiling is *no bound at all* and affordability is the limit.
    - ``NOT_ENFORCED`` — a loss or activity ceiling left at zero, which
      ``durable.limit_breaches`` reads as unset rather than as "zero tolerated",
      so nothing will ever breach it.
    """

    name: str
    value: str | None = None
    effect: LimitEffect


class MandatePromotionView(ChronosModel):
    """The rung one asset family has earned. A family absent here earned nothing."""

    asset_class: str
    level: str


class SystemView(ChronosModel):
    """The supervision status bar: is anything running, and does it have authority.

    Every field that can be unknown is ``None`` when it is. In particular
    ``autonomy_configured`` is the only autonomy field that is always
    answerable — the rest describe a runtime that may not exist.
    """

    read_only: bool
    autonomy_configured: bool
    autonomy_stopped: bool | None = None
    seconds_until_next_tick: float | None = None
    queue_depth: int | None = None
    alerts_unacknowledged: int | None = None
    alerts_critical: int | None = None
    kill_switch_engaged: bool | None = None
    live_armed: bool | None = None
    mandate_active: bool | None = None
    mandate_id: str | None = None
    account_fingerprint: str | None = None
    generated_at: str


class MandateView(ChronosModel):
    """What the owner granted, as the owner granted it.

    This panel exists so the owner can *check* their own authorization, which
    makes misrepresenting it the worst thing it could do. Hence the limit
    modelling above, and hence ``mandate_known``: when no mandate is loaded,
    every other field is ``None`` or an empty-by-unknown ``None`` collection
    rather than a shape that reads as "granted nothing".

    ``active`` is **not an authorization decision.** It reports that an
    activation row exists, is unrevoked, and that ``now`` falls in the mandate's
    window. Admission independently re-derives account match, mode, promotion,
    restart behavior, degraded state and every limit before anything trades
    (``chronos.supervisor.admission``); a ``True`` here is a display, never a
    permission.
    """

    mandate_known: bool
    mandate_id: str | None = None
    mandate_version: int | None = None
    mode: str | None = None
    active: bool | None = None
    revoked: bool | None = None
    expires_at: str | None = None
    effective_from: str | None = None
    model_discretion: bool | None = None
    symbols: tuple[str, ...] | None = None
    asset_classes: tuple[str, ...] | None = None
    strategies: tuple[str, ...] | None = None
    order_forms: tuple[str, ...] | None = None
    floors: tuple[MandateLimitView, ...] | None = None
    ceilings: tuple[MandateLimitView, ...] | None = None
    promotions: tuple[MandatePromotionView, ...] | None = None
    owner_event_id: str | None = None
    activated_at: str | None = None
    account_fingerprint: str | None = None
    generated_at: str


class JournalEntryView(ChronosModel):
    """One cycle, as the hash chain recorded it.

    ``refusal``, ``detail`` and ``decision_id`` are empty strings when the record
    carried none — that is what the writer stored, and substituting ``None``
    would claim the field was unreadable. ``quantity`` and ``limit_price`` are
    genuinely ``None`` in the payload when the cycle stopped before sizing or
    compilation, and stay ``None`` here.
    """

    sequence: int
    recorded_at: str
    stage: str
    refusal: str = ""
    detail: str = ""
    detail_truncated: bool = False
    decision_id: str = ""
    quantity: str | None = None
    limit_price: str | None = None
    #: False when the stored payload would not decode, which means the chain was
    #: corrupted. The record is still listed — dropping it would hide the
    #: corruption — but everything derived from the payload is absent.
    decoded: bool = True


class JournalView(ChronosModel):
    """Why the system did or did not trade, newest first.

    ``stream_present`` distinguishes "this account has never run a cycle" from
    "the last N cycles were uneventful". An empty journal is not evidence of
    calm; on a fresh database it is evidence of nothing at all.
    """

    entries: tuple[JournalEntryView, ...] = ()
    stream_present: bool
    limit: int
    account_fingerprint: str | None = None
    generated_at: str


class CountersView(ChronosModel):
    """This session's observed activity, or an explicit statement that none was.

    ``counters_recorded`` is the whole point of this model. ``SessionCounters``
    returns zeros for a missing row because admission needs a value to compare
    against; a display that copied that would turn "no authority established"
    into "no losses yet", which is the exact inversion doctrine forbids. When
    the row is absent every figure below is ``None``.
    """

    session_date: str
    counters_recorded: bool
    realized_loss_usd: str | None = None
    drawdown_usd: str | None = None
    drawdown_pct: str | None = None
    peak_equity_usd: str | None = None
    trough_equity_usd: str | None = None
    orders_submitted: int | None = None
    cancellations: int | None = None
    replacements: int | None = None
    turnover_usd: str | None = None
    account_fingerprint: str | None = None
    generated_at: str


class QueueEntryView(ChronosModel):
    """One received proposal, described — never reproduced.

    The stored payload is deliberately absent. It is raw external-worker input
    held verbatim (``chronos.supervisor.proposals``), bounded only by the
    ingress size cap, and echoing it into an operator's browser would put
    untrusted text of arbitrary size in the one surface a human reads to decide
    whether to intervene. What the panel needs is what the *cycle* concluded,
    and that is what these fields carry.
    """

    status: str
    cycle_stage: str = ""
    refusal: str = ""
    queued_at: str


class QueueView(ChronosModel):
    """How much work is waiting, and what happened to the recent arrivals.

    A ``pending_depth`` that only grows means the tick is stuck — which is the
    question this panel exists to answer, and the reason the depth comes from
    ``proposals.pending_depth`` rather than from counting the rows listed below.
    """

    pending_depth: int | None = None
    recent: tuple[QueueEntryView, ...] = ()
    limit: int
    account_fingerprint: str | None = None
    generated_at: str


class AlertEntryView(ChronosModel):
    """One condition awaiting the owner.

    ``delivered`` is read from the row's ``delivered_at``. ``GET
    /autonomy/alerts`` hardcodes it to ``False``, which makes a delivered alert
    indistinguishable from an undelivered one — precisely the distinction R-32
    exists to preserve, since an alert nobody was told about is not an alert.
    That bug is not propagated here.
    """

    id: int
    severity: str
    kind: str
    summary: str
    occurrences: int
    raised_at: str
    delivered: bool
    acknowledged: bool


class AlertsView(ChronosModel):
    """Everything the owner has not acknowledged, most severe first."""

    alerts: tuple[AlertEntryView, ...] = ()
    limit: int
    account_fingerprint: str | None = None
    generated_at: str


# --------------------------------------------------------------- assemblers


def system_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    read_only: bool,
    autonomy: TickSchedule | None = None,
    mandate: AutonomyMandate | None = None,
    kill_switch_engaged: bool | None = None,
    live_armed: bool | None = None,
) -> SystemView:
    """Assemble the status bar.

    ``kill_switch_engaged`` and ``live_armed`` are passed in rather than read
    here, and default to ``None``: a caller that cannot observe them says so,
    and the panel reports "unavailable" instead of the reassuring answer. This
    is also what keeps the module free of ``chronos.orders`` — see the module
    docstring.
    """

    scoped = _scope(account_fingerprint)
    activation = None
    if mandate is not None and scoped is not None:
        activation = durable.load_activation(session, account_fingerprint=scoped, mandate=mandate)
    mandate_active: bool | None = None
    if mandate is not None:
        mandate_active = (
            activation is not None and not activation.revoked and mandate.covers_instant(now)
        )
    unacknowledged, critical = _alert_counts(session, scoped)
    return SystemView(
        read_only=read_only,
        autonomy_configured=autonomy is not None,
        autonomy_stopped=None if autonomy is None else autonomy.stopped,
        seconds_until_next_tick=_next_tick_seconds(autonomy, now),
        queue_depth=(
            None if scoped is None else proposals.pending_depth(session, account_fingerprint=scoped)
        ),
        alerts_unacknowledged=unacknowledged,
        alerts_critical=critical,
        kill_switch_engaged=kill_switch_engaged,
        live_armed=live_armed,
        mandate_active=mandate_active,
        mandate_id=None if mandate is None else mandate.mandate_id,
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def mandate_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    mandate: AutonomyMandate | None = None,
) -> MandateView:
    """Assemble the grant panel from the mandate in force and its activation."""

    scoped = _scope(account_fingerprint)
    if mandate is None:
        return MandateView(mandate_known=False, account_fingerprint=scoped, generated_at=_iso(now))

    activation = None
    if scoped is not None:
        activation = durable.load_activation(session, account_fingerprint=scoped, mandate=mandate)
    revoked = None if activation is None else activation.revoked
    active = (
        None
        if scoped is None
        else activation is not None and not activation.revoked and mandate.covers_instant(now)
    )
    discretion = mandate.capital.model_discretion
    return MandateView(
        mandate_known=True,
        mandate_id=mandate.mandate_id,
        mandate_version=mandate.mandate_version,
        mode=mandate.mode.value,
        active=active,
        revoked=revoked,
        expires_at=_iso(mandate.expires_at),
        effective_from=_iso(mandate.effective_from),
        model_discretion=discretion,
        symbols=tuple(mandate.scope.symbols),
        asset_classes=tuple(item.value for item in mandate.scope.asset_classes),
        strategies=tuple(item.value for item in mandate.scope.strategies),
        order_forms=tuple(item.value for item in mandate.scope.order_forms),
        floors=_mandate_floors(mandate),
        ceilings=_mandate_ceilings(mandate),
        promotions=tuple(
            MandatePromotionView(asset_class=item.asset_class.value, level=item.level.value)
            for item in mandate.promotions
        ),
        owner_event_id=None if activation is None else activation.owner_event_id,
        activated_at=None if activation is None else _iso(activation.activated_at),
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def journal_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    limit: int = DEFAULT_JOURNAL_LIMIT,
) -> JournalView:
    """Assemble the cycle journal, newest first.

    Bounded by ``limit`` because the stream is append-only and unbounded: a
    panel that fetched it whole would slow down exactly as the system got
    busier, which is when someone is most likely to be watching it.
    """

    bounded = _bounded(limit, maximum=MAX_JOURNAL_LIMIT)
    scoped = _scope(account_fingerprint)
    if scoped is None:
        return JournalView(
            stream_present=False,
            limit=bounded,
            account_fingerprint=None,
            generated_at=_iso(now),
        )
    stream = durable.stream_for(CYCLE_STREAM, scoped)
    rows = list(
        session.scalars(
            select(HashChainRow)
            .where(HashChainRow.stream == stream)
            .order_by(HashChainRow.sequence.desc())
            .limit(bounded)
        )
    )
    return JournalView(
        entries=tuple(_journal_entry(row) for row in rows),
        stream_present=bool(rows),
        limit=bounded,
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def counters_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    market_timezone: str | None = None,
) -> CountersView:
    """Assemble this session's counters, or say plainly that none were recorded.

    The existence check is a separate query from ``durable.load_counters``, and
    that redundancy is deliberate: ``SessionCounters`` cannot express "no row",
    so asking it whether one exists is impossible, and re-deriving the field
    mapping here would create a second place that has to learn about a new
    counter. One authority for the values, one narrow query for existence.
    """

    key = durable.session_key(now, market_timezone=market_timezone)
    scoped = _scope(account_fingerprint)
    if scoped is None or not _counters_recorded(session, scoped, key):
        return CountersView(
            session_date=key,
            counters_recorded=False,
            account_fingerprint=scoped,
            generated_at=_iso(now),
        )
    counters = durable.load_counters(
        session,
        account_fingerprint=scoped,
        now=now,
        market_timezone=market_timezone,
    )
    return CountersView(
        session_date=counters.session_date,
        counters_recorded=True,
        realized_loss_usd=_decimal(counters.realized_loss_usd),
        drawdown_usd=_decimal(counters.drawdown_usd),
        drawdown_pct=_decimal(counters.drawdown_pct),
        peak_equity_usd=_decimal(counters.peak_equity_usd),
        trough_equity_usd=_decimal(counters.trough_equity_usd),
        orders_submitted=counters.orders_submitted,
        cancellations=counters.cancellations,
        replacements=counters.replacements,
        turnover_usd=_decimal(counters.turnover_usd),
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def queue_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    limit: int = DEFAULT_QUEUE_LIMIT,
) -> QueueView:
    """Assemble the proposal queue: depth, plus what happened to recent arrivals."""

    bounded = _bounded(limit, maximum=MAX_QUEUE_LIMIT)
    scoped = _scope(account_fingerprint)
    if scoped is None:
        return QueueView(limit=bounded, account_fingerprint=None, generated_at=_iso(now))
    rows = session.scalars(
        select(AutonomyProposalQueueRow)
        .where(AutonomyProposalQueueRow.account_fingerprint == scoped)
        .order_by(AutonomyProposalQueueRow.id.desc())
        .limit(bounded)
    )
    return QueueView(
        pending_depth=proposals.pending_depth(session, account_fingerprint=scoped),
        recent=tuple(
            QueueEntryView(
                status=row.status,
                cycle_stage=row.cycle_stage,
                refusal=row.refusal,
                queued_at=_iso(row.received_at),
            )
            for row in rows
        ),
        limit=bounded,
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def alerts_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    limit: int = DEFAULT_ALERT_LIMIT,
) -> AlertsView:
    """Assemble the pending owner alerts, most severe and most recent first.

    Ordering is ``alert_module.severity_rank``'s rather than the database's:
    severities are stored as strings, so SQL would sort them alphabetically and
    put CRITICAL before INFO before WARNING — an order that looks right until
    the day it matters. Sorting in Python is safe here because recurrences fold
    into one row per kind, so the unacknowledged set is bounded by the number of
    distinct conditions rather than by how long one has been failing.
    """

    bounded = _bounded(limit, maximum=MAX_ALERT_LIMIT)
    scoped = _scope(account_fingerprint)
    if scoped is None:
        return AlertsView(limit=bounded, account_fingerprint=None, generated_at=_iso(now))
    rows = list(
        session.scalars(
            select(AutonomyOwnerAlertRow).where(
                AutonomyOwnerAlertRow.account_fingerprint == scoped,
                AutonomyOwnerAlertRow.acknowledged_at.is_(None),
            )
        )
    )
    rows.sort(
        key=lambda row: (
            -alert_module.severity_rank(alert_module.AlertSeverity(row.severity)),
            -row.last_seen_at.timestamp(),
        )
    )
    return AlertsView(
        alerts=tuple(
            AlertEntryView(
                id=row.id,
                severity=row.severity,
                kind=row.kind,
                summary=row.summary,
                occurrences=row.occurrences,
                raised_at=_iso(row.raised_at),
                delivered=row.delivered_at is not None,
                acknowledged=row.acknowledged_at is not None,
            )
            for row in rows[:bounded]
        ),
        limit=bounded,
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


# ------------------------------------------------------------------ internals


def _scope(account_fingerprint: str) -> str | None:
    """The account this backend is bound to, or ``None`` when it is bound to none.

    Empty is not a fingerprint that matches nothing — it is the absence of a
    scope, and every per-account figure derived from it is unknown rather than
    zero.
    """

    scoped = account_fingerprint.strip()
    return scoped or None


def _bounded(limit: int, *, maximum: int) -> int:
    return max(1, min(limit, maximum))


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _decimal(value: Decimal) -> str:
    """Decimals travel as strings so a JSON float cannot round money.

    ``str()`` alone is not enough. The counter columns are fixed-scale numerics,
    so an untouched counter comes back as ``Decimal("0E-8")`` and a round figure
    as ``Decimal("1E+2")`` — both of which render in the browser as literal
    ``0E-8`` and ``1E+2``, and the first of those is a zero an operator would
    read as a code. Normalizing drops the trailing zeros the scale added and
    ``format(..., "f")`` forbids exponent notation, so the string is always the
    shortest *exact* spelling of the same value: ``Decimal(rendered) == value``
    for every input.
    """

    return format(value.normalize(), "f")


def _next_tick_seconds(autonomy: TickSchedule | None, now: datetime) -> float | None:
    """Seconds to the next tick, or ``None`` when there will not be one.

    ``AutonomyRuntime`` returns infinity once it has stopped itself. Infinity is
    not representable in JSON and any finite substitute would describe a
    schedule that does not exist, so the honest answer is "unknown" with
    ``autonomy_stopped`` carrying the reason.
    """

    if autonomy is None:
        return None
    seconds = autonomy.seconds_until_next_tick(now)
    return seconds if math.isfinite(seconds) else None


def _alert_counts(session: Session, scoped: str | None) -> tuple[int | None, int | None]:
    if scoped is None:
        return None, None
    total = session.scalar(
        select(func.count())
        .select_from(AutonomyOwnerAlertRow)
        .where(
            AutonomyOwnerAlertRow.account_fingerprint == scoped,
            AutonomyOwnerAlertRow.acknowledged_at.is_(None),
        )
    )
    critical = session.scalar(
        select(func.count())
        .select_from(AutonomyOwnerAlertRow)
        .where(
            AutonomyOwnerAlertRow.account_fingerprint == scoped,
            AutonomyOwnerAlertRow.acknowledged_at.is_(None),
            AutonomyOwnerAlertRow.severity == alert_module.AlertSeverity.CRITICAL.value,
        )
    )
    return int(total or 0), int(critical or 0)


def _counters_recorded(session: Session, scoped: str, session_date: str) -> bool:
    total = session.scalar(
        select(func.count())
        .select_from(AutonomySessionCounterRow)
        .where(
            AutonomySessionCounterRow.account_fingerprint == scoped,
            AutonomySessionCounterRow.session_date == session_date,
        )
    )
    return bool(total)


def _journal_entry(row: HashChainRow) -> JournalEntryView:
    try:
        payload = hash_chain.payload_of(row)
    except (ValueError, TypeError):
        # The stored text would not decode, which means the chain was damaged.
        # The record is still listed: hiding it would hide the damage, and the
        # kind column still says which stage the cycle reached.
        return JournalEntryView(
            sequence=row.sequence,
            recorded_at=_iso(row.recorded_at),
            stage=row.kind,
            decoded=False,
        )
    detail = _text(payload.get("detail"))
    return JournalEntryView(
        sequence=row.sequence,
        recorded_at=_iso(row.recorded_at),
        stage=_text(payload.get("stage")) or row.kind,
        refusal=_text(payload.get("refusal")),
        detail=detail[:MAX_DETAIL_CHARS],
        detail_truncated=len(detail) > MAX_DETAIL_CHARS,
        decision_id=_text(payload.get("decision_id")),
        quantity=_optional_text(payload.get("quantity")),
        limit_price=_optional_text(payload.get("limit_price")),
    )


def _text(value: object) -> str:
    """A payload field as display text. Never markup, never an order parameter."""

    return "" if value is None else str(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _mandate_floors(mandate: AutonomyMandate) -> tuple[MandateLimitView, ...]:
    """The reserves the model may not spend into.

    Floors invert deny-by-default: zero is the *most* permissive value, so an
    unset floor is ``NO_FLOOR`` and not a restriction. A submitting mandate is
    required to set both (``AutonomyMandate._validate_submitting_floors``), and
    model discretion does not waive them — discretion over size is not
    discretion over the reserve.
    """

    capital = mandate.capital
    return (
        _floor("capital.min_cash_floor_usd", capital.min_cash_floor_usd),
        _floor("capital.min_buying_power_usd", capital.min_buying_power_usd),
    )


def _mandate_ceilings(mandate: AutonomyMandate) -> tuple[MandateLimitView, ...]:
    """Every ceiling the mandate carries, set or not, with its unset reading.

    Sizing ceilings and breach ceilings are built by different helpers because
    the supervisor reads their silence differently — ADR-0017 discretion turns
    an unset sizing ceiling into no ceiling, while ``durable.limit_breaches``
    treats an unset loss or activity ceiling as unenforced in every mode.
    Merging them would have required one of those two readings to be wrong.
    """

    capital = mandate.capital
    concentration = mandate.concentration
    loss = mandate.loss
    activity = mandate.activity
    discretion = capital.model_discretion
    sizing: tuple[tuple[str, Decimal | int], ...] = (
        ("capital.allocated_capital_usd", capital.allocated_capital_usd),
        ("capital.max_order_notional_usd", capital.max_order_notional_usd),
        ("capital.max_position_notional_usd", capital.max_position_notional_usd),
        ("capital.max_gross_exposure_usd", capital.max_gross_exposure_usd),
        ("capital.max_net_exposure_usd", capital.max_net_exposure_usd),
        ("capital.max_contracts_per_order", capital.max_contracts_per_order),
        ("capital.max_shares_per_order", capital.max_shares_per_order),
        ("capital.max_leverage", capital.max_leverage),
        ("capital.max_margin_utilization_pct", capital.max_margin_utilization_pct),
        ("concentration.max_symbol_exposure_pct", concentration.max_symbol_exposure_pct),
        ("concentration.max_sector_exposure_pct", concentration.max_sector_exposure_pct),
        ("concentration.max_family_exposure_pct", concentration.max_family_exposure_pct),
        ("concentration.max_correlated_exposure_pct", concentration.max_correlated_exposure_pct),
    )
    breach: tuple[tuple[str, Decimal | int], ...] = (
        ("loss.max_session_loss_usd", loss.max_session_loss_usd),
        ("loss.max_daily_loss_usd", loss.max_daily_loss_usd),
        ("loss.max_peak_to_trough_drawdown_usd", loss.max_peak_to_trough_drawdown_usd),
        ("loss.max_peak_to_trough_drawdown_pct", loss.max_peak_to_trough_drawdown_pct),
        ("activity.max_orders_per_session", activity.max_orders_per_session),
        ("activity.max_cancellations_per_session", activity.max_cancellations_per_session),
        ("activity.max_replacements_per_session", activity.max_replacements_per_session),
        ("activity.max_turnover_usd_per_session", activity.max_turnover_usd_per_session),
    )
    return tuple(
        _sizing_ceiling(name, value, model_discretion=discretion) for name, value in sizing
    ) + tuple(_breach_ceiling(name, value) for name, value in breach)


def _sizing_ceiling(name: str, value: Decimal | int, *, model_discretion: bool) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=str(value), effect="BINDS")
    effect: LimitEffect = "NO_CEILING" if model_discretion else "AUTHORIZES_NOTHING"
    return MandateLimitView(name=name, effect=effect)


def _breach_ceiling(name: str, value: Decimal | int) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=str(value), effect="BINDS")
    return MandateLimitView(name=name, effect="NOT_ENFORCED")


def _floor(name: str, value: Decimal) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=str(value), effect="BINDS")
    return MandateLimitView(name=name, effect="NO_FLOOR")

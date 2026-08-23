"""The panel read-models: everything the terminal is allowed to say (M8a).

ADR-0018 §6 puts the terminal's contract in Python. These are the models that
contract is made of — one per panel, assembled from durable state by a function
that takes a session and explicit facts and returns a value. The browser draws
what these describe and nothing else.

That makes this module the terminal's **disclosure boundary**, which is why its
tests live in ``tests/safety/``. Two rules govern every field below, and both
are structural rather than procedural:

1. **Nothing here may carry a secret or an account identifier.** No credential,
   no 2FA code, no raw arm or confirmation phrase, and no full broker account
   id. The ``account_fingerprint`` is a 64-character pseudonym (``chronos.utils.
   identifiers``) and is safe to return; it is the *only* account-shaped value
   any of these models carries. Market instrument identifiers do appear inside
   the canonical option-selection receipt: they are the exact public-market
   facts the operator is inspecting, not an account or submission handle.
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
- A counter row is not an equity observation. Peak equity is seeded at zero and
  only a snapshot moves it, so on a row written by activity alone the peak, the
  trough and both drawdowns are ``None`` and ``equity_observed`` is ``False``.
  A zero drawdown is the most reassuring figure the counters panel can show,
  and it is only shown when something actually measured one.

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

import json
import math
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import Text, case, cast, func, literal, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from chronos.autonomy import AutonomyMandate
from chronos.config.limits import (
    MAX_DURABLE_HASH_TEXT_CHARS,
    MAX_DURABLE_KIND_TEXT_CHARS,
    MAX_DURABLE_SEQUENCE_TEXT_CHARS,
    MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
    MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
)
from chronos.domain.models import ChronosModel
from chronos.marketdata.bars import BarSeries
from chronos.persistence import hash_chain
from chronos.persistence.schema import (
    AutonomyOwnerAlertRow,
    AutonomyProposalQueueRow,
    AutonomySessionCounterRow,
    HashChainRow,
)
from chronos.supervisor import alerts as alert_module
from chronos.supervisor import durable, proposals
from chronos.supervisor.option_selection import (
    OPTION_SELECTION_STREAM,
    OptionSelectionReceipt,
    SelectionStatus,
)

#: The hash-chain stream every cycle appends to, whatever the outcome. Written
#: inline by ``chronos.supervisor.loop._record``; named here because the journal
#: panel is the only reader and a magic string in a read path is a magic string
#: that goes stale unnoticed. The safety tests pin the two together.
CYCLE_STREAM = "autonomy.cycles"

DEFAULT_JOURNAL_LIMIT = 50
MAX_JOURNAL_LIMIT = 500
DEFAULT_OPTION_SELECTION_LIMIT = 25
MAX_OPTION_SELECTION_LIMIT = 25
MAX_OPTION_SELECTION_DECISION_ID_CHARS = 128
MAX_OPTION_SELECTION_DIGEST_CHARS = 64
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

_OPTION_SELECTION_STATUSES = frozenset(status.value for status in SelectionStatus)
_MAX_OPTION_SELECTION_STATUS_CHARS = max(len(status) for status in _OPTION_SELECTION_STATUSES)
_OPTION_SELECTION_STREAM_BATCH_SIZE = 1


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

    sequence: int | None = None
    recorded_at: str | None = None
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


class OptionSelectionEntryView(ChronosModel):
    """One stored option-selection receipt and the evidence that it is intact.

    ``canonical_receipt`` is the exact string embedded in the append-only
    envelope.  ``receipt`` is populated only when that string is canonical,
    validates as :class:`OptionSelectionReceipt`, deterministically replays,
    and agrees with every duplicated envelope field.  Keeping both lets an
    operator inspect the bytes that were actually committed without presenting
    a convenient parsed value when those bytes cannot be trusted.

    The three validity flags answer different questions. ``receipt_valid``
    covers the canonical receipt and envelope, ``record_hash_valid`` covers the
    row's own stored digest, and ``chain_valid`` says the verified prefix reaches
    this row. ``record_valid`` is the conjunction of the first two; a later row
    can therefore be internally intact while its lineage is invalid because an
    earlier record was changed or removed.
    """

    sequence: int | None = None
    recorded_at: str | None = None
    kind: str | None = None
    decision_id: str | None = None
    status: str | None = None
    output_digest: str | None = None
    canonical_receipt: str | None = None
    receipt: OptionSelectionReceipt | None = None
    receipt_valid: bool
    record_hash_valid: bool
    record_valid: bool
    chain_valid: bool
    invalid_detail: str = ""


class OptionSelectionsView(ChronosModel):
    """Account-scoped option-selection receipts, newest first and bounded.

    Whole-stream verification is deliberately performed on every inspection.
    This endpoint is an operator audit surface rather than a high-frequency
    dashboard poll: returning a reassuring page from a broken prefix would be
    worse than paying the cost of hashing the full append-only stream.
    """

    entries: tuple[OptionSelectionEntryView, ...] = ()
    stream_present: bool
    chain_valid: bool | None = None
    chain_records: int | None = None
    chain_broken_at: int | None = None
    chain_detail: str = ""
    semantic_valid: bool | None = None
    semantic_detail: str = ""
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

    ``equity_observed`` draws the same line *inside* a row that does exist.
    ``durable._ensure_counter_row`` seeds ``peak_equity_usd`` at zero and only
    ``record_equity`` ever moves it, so a row written by ``record_activity``
    alone describes a session whose equity was never read — and
    ``SessionCounters.drawdown_usd``/``drawdown_pct`` return zero for precisely
    that state, again because admission needs a number. Copying the four
    equity-derived figures through would print "drawdown 0 · peak equity 0" for
    a session in which the supervisor observed no equity at all, under a caption
    saying these are the supervisor's own observations. So they are ``None``
    unless a peak was actually recorded, and this flag says which case it is.

    An account whose equity genuinely *is* zero also reads as unobserved. The
    sentinel cannot separate the two, and ``SessionCounters`` already treats a
    non-positive peak as nothing to measure a drawdown against — inventing a
    distinction the stored row does not carry would be the worse answer.
    """

    session_date: str
    counters_recorded: bool
    equity_observed: bool
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

    ``mandate_active`` needs both a mandate *and* an account to have activated
    it against. A backend bound to no account never performs the activation
    lookup, so ``False`` there would be a positive claim derived from a query
    nobody ran — and since both panels are assembled in the same request cycle,
    it would be a claim contradicting the ``unknown`` :func:`mandate_view` draws
    from the identical condition.
    """

    scoped = _scope(account_fingerprint)
    mandate_active: bool | None = None
    if mandate is not None and scoped is not None:
        activation = durable.load_activation(session, account_fingerprint=scoped, mandate=mandate)
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


def option_selections_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    limit: int = DEFAULT_OPTION_SELECTION_LIMIT,
) -> OptionSelectionsView:
    """Inspect canonical option-selection receipts without acquiring or acting.

    The returned page is bounded and newest first, but ``chain_valid`` covers
    the complete account-scoped stream. A truncated page must not turn an
    earlier break into an apparently sound tail.
    """

    bounded = _bounded(limit, maximum=MAX_OPTION_SELECTION_LIMIT)
    scoped = _scope(account_fingerprint)
    if scoped is None:
        return OptionSelectionsView(
            stream_present=False,
            chain_detail="account scope is unavailable; no receipt stream can be named",
            limit=bounded,
            account_fingerprint=None,
            generated_at=_iso(now),
        )

    stream = durable.stream_for(OPTION_SELECTION_STREAM, scoped)
    # Cast every dynamically typed SQLite value to text *inside SQL*, then
    # project only its length and a MAX + 1 prefix. No result processor or DB
    # driver receives an unbounded corrupt value before this disclosure boundary
    # can reject it. PostgreSQL uses the same portable length/substr projection;
    # only SQLite needs ``typeof`` to distinguish a true INTEGER sequence from
    # dynamically stored text.
    sequence_storage_type: ColumnElement[Any]
    kind_storage_type: ColumnElement[Any]
    payload_storage_type: ColumnElement[Any]
    recorded_at_storage_type: ColumnElement[Any]
    previous_hash_storage_type: ColumnElement[Any]
    record_hash_storage_type: ColumnElement[Any]
    sequence_text: ColumnElement[Any]
    kind_text: ColumnElement[Any]
    payload_text: ColumnElement[Any]
    recorded_at_text: ColumnElement[Any]
    previous_hash_text: ColumnElement[Any]
    record_hash_text: ColumnElement[Any]
    if session.get_bind().dialect.name == "sqlite":
        sequence_storage_type = func.typeof(HashChainRow.sequence)
        kind_storage_type = func.typeof(HashChainRow.kind)
        payload_storage_type = func.typeof(HashChainRow.payload_json)
        recorded_at_storage_type = func.typeof(HashChainRow.recorded_at)
        previous_hash_storage_type = func.typeof(HashChainRow.previous_hash)
        record_hash_storage_type = func.typeof(HashChainRow.record_hash)
        sequence_text = case(
            (sequence_storage_type == "integer", cast(HashChainRow.sequence, Text)),
            else_=None,
        )
        kind_text = case(
            (kind_storage_type == "text", cast(HashChainRow.kind, Text)),
            else_=None,
        )
        payload_text = case(
            (payload_storage_type == "text", cast(HashChainRow.payload_json, Text)),
            else_=None,
        )
        recorded_at_text = case(
            (recorded_at_storage_type == "text", cast(HashChainRow.recorded_at, Text)),
            else_=None,
        )
        previous_hash_text = case(
            (previous_hash_storage_type == "text", cast(HashChainRow.previous_hash, Text)),
            else_=None,
        )
        record_hash_text = case(
            (record_hash_storage_type == "text", cast(HashChainRow.record_hash, Text)),
            else_=None,
        )
    else:
        sequence_storage_type = literal("integer")
        kind_storage_type = literal("text")
        payload_storage_type = literal("text")
        recorded_at_storage_type = literal("text")
        previous_hash_storage_type = literal("text")
        record_hash_storage_type = literal("text")
        sequence_text = cast(HashChainRow.sequence, Text)
        kind_text = cast(HashChainRow.kind, Text)
        payload_text = cast(HashChainRow.payload_json, Text)
        recorded_at_text = cast(HashChainRow.recorded_at, Text)
        previous_hash_text = cast(HashChainRow.previous_hash, Text)
        record_hash_text = cast(HashChainRow.record_hash, Text)
    statement = (
        select(
            sequence_storage_type.label("sequence_storage_type"),
            func.length(sequence_text).label("sequence_length"),
            func.substr(
                sequence_text,
                1,
                MAX_DURABLE_SEQUENCE_TEXT_CHARS + 1,
            ).label("sequence_text"),
            kind_storage_type.label("kind_storage_type"),
            func.length(kind_text).label("kind_length"),
            func.substr(
                kind_text,
                1,
                MAX_DURABLE_KIND_TEXT_CHARS + 1,
            ).label("kind"),
            payload_storage_type.label("payload_storage_type"),
            func.length(payload_text).label("payload_length"),
            func.substr(
                payload_text,
                1,
                MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS + 1,
            ).label("payload_json"),
            recorded_at_storage_type.label("recorded_at_storage_type"),
            func.length(recorded_at_text).label("recorded_at_length"),
            func.substr(
                recorded_at_text,
                1,
                MAX_DURABLE_TIMESTAMP_TEXT_CHARS + 1,
            ).label("recorded_at_text"),
            previous_hash_storage_type.label("previous_hash_storage_type"),
            func.length(previous_hash_text).label("previous_hash_length"),
            func.substr(
                previous_hash_text,
                1,
                MAX_DURABLE_HASH_TEXT_CHARS + 1,
            ).label("previous_hash"),
            record_hash_storage_type.label("record_hash_storage_type"),
            func.length(record_hash_text).label("record_hash_length"),
            func.substr(
                record_hash_text,
                1,
                MAX_DURABLE_HASH_TEXT_CHARS + 1,
            ).label("record_hash"),
        )
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.asc())
        .execution_options(yield_per=_OPTION_SELECTION_STREAM_BATCH_SIZE)
    )
    page: deque[OptionSelectionEntryView] = deque(maxlen=bounded)
    semantic_detail = ""
    seen_decisions: dict[str, int] = {}
    expected_previous = hash_chain.GENESIS_HASH
    chain_broken_at: int | None = None
    chain_detail = ""
    record_count = 0
    for row in session.execute(statement):
        record_count += 1
        sequence, sequence_invalid = _stored_sequence(
            storage_type=row.sequence_storage_type,
            reported_length=row.sequence_length,
            value=row.sequence_text,
        )
        kind, kind_invalid = _bounded_stored_text(
            storage_type=row.kind_storage_type,
            value=row.kind,
            reported_length=row.kind_length,
            field="record kind",
            maximum=MAX_DURABLE_KIND_TEXT_CHARS,
        )
        payload_json, payload_invalid = _bounded_stored_text(
            storage_type=row.payload_storage_type,
            value=row.payload_json,
            reported_length=row.payload_length,
            field="record payload",
            maximum=MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
        )
        recorded_at, recorded_at_invalid = _stored_recorded_at(
            storage_type=row.recorded_at_storage_type,
            value=row.recorded_at_text,
            reported_length=row.recorded_at_length,
        )
        previous_hash, previous_hash_invalid = _stored_hash_text(
            storage_type=row.previous_hash_storage_type,
            value=row.previous_hash,
            reported_length=row.previous_hash_length,
            field="previous_hash",
        )
        record_hash, record_hash_invalid = _stored_hash_text(
            storage_type=row.record_hash_storage_type,
            value=row.record_hash,
            reported_length=row.record_hash_length,
            field="record_hash",
        )
        record_hash_checked = False
        record_hash_valid = False
        if (
            sequence is not None
            and payload_json is not None
            and recorded_at is not None
            and previous_hash is not None
            and record_hash is not None
        ):
            record_hash_checked = True
            recomputed_hash = hash_chain.compute_hash(
                stream=stream,
                sequence=sequence,
                recorded_at=recorded_at,
                payload_json=payload_json,
                previous_hash=previous_hash,
            )
            record_hash_valid = recomputed_hash == record_hash
        # Match hash_chain.verify's first-failure contract while continuing to
        # drain the cursor for an honest whole-stream record count and page.
        if chain_broken_at is None:
            if sequence_invalid:
                chain_broken_at = record_count
                chain_detail = f"{sequence_invalid}; its digest was not verified."
            elif sequence != record_count:
                chain_broken_at = record_count
                chain_detail = (
                    f"sequence gap: expected {record_count}, found {sequence}. A record was "
                    "deleted or renumbered."
                )
            elif previous_hash_invalid:
                chain_broken_at = sequence
                chain_detail = (
                    f"record {sequence} {previous_hash_invalid}; the link was not verified."
                )
            elif previous_hash != expected_previous:
                chain_broken_at = sequence
                chain_detail = (
                    f"record {sequence} does not link to its predecessor; the chain was "
                    "reordered or a record was replaced."
                )
            elif recorded_at_invalid:
                chain_broken_at = sequence
                chain_detail = (
                    f"record {sequence} {recorded_at_invalid}; its digest was not verified."
                )
            elif record_hash_invalid:
                chain_broken_at = sequence
                chain_detail = f"record {sequence} {record_hash_invalid}; its digest is invalid."
            elif payload_invalid:
                chain_broken_at = sequence
                chain_detail = f"record {sequence} {payload_invalid}; its digest was not verified."
            elif not record_hash_valid:
                chain_broken_at = sequence
                chain_detail = (
                    f"record {sequence} was modified after it was written; its contents no "
                    "longer match its digest."
                )
            else:
                assert record_hash is not None
                expected_previous = record_hash
        entry = _option_selection_entry(
            sequence=sequence,
            kind=kind,
            kind_invalid=kind_invalid,
            payload_json=payload_json,
            recorded_at=recorded_at,
            record_hash_valid=record_hash_valid,
            record_hash_checked=record_hash_checked,
            storage_invalid=tuple(
                detail
                for detail in (
                    sequence_invalid,
                    recorded_at_invalid,
                    previous_hash_invalid,
                    record_hash_invalid,
                    payload_invalid,
                )
                if detail
            ),
            chain_valid=chain_broken_at is None,
            chain_detail=chain_detail,
        )
        page.append(entry)
        sequence_label = record_count if sequence is None else sequence
        if semantic_detail:
            continue
        if not entry.receipt_valid:
            semantic_detail = (
                f"receipt envelope at sequence {sequence_label} fails semantic validation"
            )
            continue
        assert entry.decision_id is not None
        previous = seen_decisions.get(entry.decision_id)
        if previous is not None:
            semantic_detail = (
                f"duplicate option decision receipt at sequences {previous} and {sequence_label}"
            )
            continue
        seen_decisions[entry.decision_id] = sequence_label
    if record_count == 0:
        chain_detail = "chain is empty"
    elif chain_broken_at is None:
        chain_detail = f"{record_count} record(s) verified to head {expected_previous[:12]}…"
    return OptionSelectionsView(
        entries=tuple(reversed(page)),
        stream_present=record_count > 0,
        chain_valid=chain_broken_at is None,
        chain_records=record_count,
        chain_broken_at=chain_broken_at,
        chain_detail=chain_detail,
        semantic_valid=not semantic_detail,
        semantic_detail=semantic_detail,
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

    There are two gates, not one, because there are two ways for a figure to be
    unmeasured. ``counters_recorded`` says a row exists; ``equity_observed``
    says something ever wrote an equity snapshot into it. See
    :class:`CountersView` for why the second matters as much as the first.
    """

    key = durable.session_key(now, market_timezone=market_timezone)
    scoped = _scope(account_fingerprint)
    if scoped is None or not _counters_recorded(session, scoped, key):
        return CountersView(
            session_date=key,
            counters_recorded=False,
            equity_observed=False,
            account_fingerprint=scoped,
            generated_at=_iso(now),
        )
    counters = durable.load_counters(
        session,
        account_fingerprint=scoped,
        now=now,
        market_timezone=market_timezone,
    )
    # The row exists, so this peak is the row's own — the seeded zero that
    # ``record_equity`` has not yet displaced, or a real observation. Every
    # equity figure the drawdown ceiling is measured against hangs off it, so
    # they stand or fall together rather than being copied through one by one.
    equity_observed = counters.peak_equity_usd > 0
    return CountersView(
        session_date=counters.session_date,
        counters_recorded=True,
        equity_observed=equity_observed,
        realized_loss_usd=_decimal(counters.realized_loss_usd),
        drawdown_usd=_decimal(counters.drawdown_usd) if equity_observed else None,
        drawdown_pct=_decimal(counters.drawdown_pct) if equity_observed else None,
        peak_equity_usd=_decimal(counters.peak_equity_usd) if equity_observed else None,
        trough_equity_usd=_decimal(counters.trough_equity_usd) if equity_observed else None,
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


def _bounded_stored_text(
    *,
    storage_type: object | None = None,
    value: object,
    reported_length: object,
    field: str,
    maximum: int,
) -> tuple[str | None, str]:
    """Validate one SQL-bounded text projection without echoing corrupt data."""

    if storage_type is not None and storage_type != "text":
        return None, f"{field} has an invalid storage type"
    if not isinstance(reported_length, int) or reported_length < 0:
        return None, f"{field} has an invalid stored length"
    if reported_length > maximum:
        return None, f"{field} exceeds the {maximum}-character inspection limit"
    if not isinstance(value, str) or len(value) != reported_length:
        return None, f"{field} could not be read as bounded text"
    return value, ""


def _stored_sequence(
    *,
    storage_type: object,
    reported_length: object,
    value: object,
) -> tuple[int | None, str]:
    if storage_type != "integer":
        return None, "record sequence has an invalid storage type"
    text, invalid = _bounded_stored_text(
        value=value,
        reported_length=reported_length,
        field="record sequence",
        maximum=MAX_DURABLE_SEQUENCE_TEXT_CHARS,
    )
    if text is None:
        return None, invalid
    if not text.isascii() or not text.isdecimal():
        return None, "record sequence is not a canonical positive integer"
    sequence = int(text)
    if sequence <= 0 or str(sequence) != text:
        return None, "record sequence is not a canonical positive integer"
    return sequence, ""


def _stored_recorded_at(
    *,
    storage_type: object,
    value: object,
    reported_length: object,
) -> tuple[datetime | None, str]:
    text, invalid = _bounded_stored_text(
        storage_type=storage_type,
        value=value,
        reported_length=reported_length,
        field="record timestamp",
        maximum=MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
    )
    if text is None:
        return None, invalid
    if not text.isascii() or len(text) < 19 or text[10] not in {" ", "T"}:
        return None, "record timestamp is not a valid ISO datetime"
    try:
        recorded_at = datetime.fromisoformat(text)
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        else:
            recorded_at = recorded_at.astimezone(UTC)
    except (OverflowError, ValueError):
        return None, "record timestamp is not a valid ISO datetime"
    return recorded_at, ""


def _stored_hash_text(
    *,
    storage_type: object,
    value: object,
    reported_length: object,
    field: str,
) -> tuple[str | None, str]:
    text, invalid = _bounded_stored_text(
        storage_type=storage_type,
        value=value,
        reported_length=reported_length,
        field=field,
        maximum=MAX_DURABLE_HASH_TEXT_CHARS,
    )
    if text is None:
        return None, invalid
    if len(text) != MAX_DURABLE_HASH_TEXT_CHARS:
        return None, f"{field} is not exactly {MAX_DURABLE_HASH_TEXT_CHARS} characters"
    return text, ""


def _option_selection_entry(
    *,
    sequence: int | None,
    kind: str | None,
    kind_invalid: str,
    payload_json: str | None,
    recorded_at: datetime | None,
    record_hash_valid: bool,
    record_hash_checked: bool,
    storage_invalid: tuple[str, ...],
    chain_valid: bool,
    chain_detail: str,
) -> OptionSelectionEntryView:
    """Decode and independently verify one selection receipt envelope."""

    invalid = list(storage_invalid)
    if record_hash_checked and not record_hash_valid:
        invalid.append("stored record contents do not match record_hash")
    if not chain_valid:
        invalid.append(f"chain lineage is invalid: {chain_detail}")
    receipt_invalid: list[str] = []
    if kind_invalid:
        receipt_invalid.append(kind_invalid)
        kind_for_view = None
    else:
        kind_for_view = _selection_status_text(kind, "record kind", receipt_invalid)
    recorded_at_for_view = None if recorded_at is None else _iso(recorded_at)

    if payload_json is None:
        return OptionSelectionEntryView(
            sequence=sequence,
            recorded_at=recorded_at_for_view,
            kind=kind_for_view,
            receipt_valid=False,
            record_hash_valid=record_hash_valid,
            record_valid=False,
            chain_valid=chain_valid,
            invalid_detail="; ".join((*invalid, *receipt_invalid)),
        )
    try:
        decoded: object = json.loads(payload_json)
    except (RecursionError, TypeError, ValueError):
        receipt_invalid.append("record payload is not valid JSON")
        return OptionSelectionEntryView(
            sequence=sequence,
            recorded_at=recorded_at_for_view,
            kind=kind_for_view,
            receipt_valid=False,
            record_hash_valid=record_hash_valid,
            record_valid=False,
            chain_valid=chain_valid,
            invalid_detail="; ".join((*invalid, *receipt_invalid)),
        )
    if not isinstance(decoded, dict):
        receipt_invalid.append("record payload is not a JSON object")
        return OptionSelectionEntryView(
            sequence=sequence,
            recorded_at=recorded_at_for_view,
            kind=kind_for_view,
            receipt_valid=False,
            record_hash_valid=record_hash_valid,
            record_valid=False,
            chain_valid=chain_valid,
            invalid_detail="; ".join((*invalid, *receipt_invalid)),
        )
    payload: dict[str, object] = decoded
    try:
        canonical_payload = hash_chain.canonical_payload(payload)
    except (RecursionError, TypeError, ValueError):
        receipt_invalid.append("record payload cannot be represented as canonical JSON")
    else:
        if payload_json != canonical_payload:
            receipt_invalid.append("record payload is not its canonical JSON representation")
    expected_fields = {
        "decision_id",
        "status",
        "output_digest",
        "canonical_receipt",
    }
    if set(payload) != expected_fields:
        receipt_invalid.append("record payload has unexpected or missing envelope fields")

    decision_id = _required_payload_text(
        payload,
        "decision_id",
        receipt_invalid,
        maximum=MAX_OPTION_SELECTION_DECISION_ID_CHARS,
    )
    status = _selection_status_text(
        _required_payload_text(payload, "status", receipt_invalid),
        "payload status",
        receipt_invalid,
    )
    output_digest = _output_digest_text(payload, receipt_invalid)
    canonical_receipt = _required_payload_text(payload, "canonical_receipt", receipt_invalid)

    candidate: OptionSelectionReceipt | None = None
    canonical_receipt_for_view = canonical_receipt
    if canonical_receipt is not None:
        # UTF-8 is at least one byte per code point, so the character count is
        # a cheap early refusal that avoids allocating another giant byte
        # string. Invalid oversized bytes are never echoed under a field whose
        # name promises the exact canonical receipt.
        oversized = len(canonical_receipt) > MAX_OPTION_SELECTION_RECEIPT_BYTES
        if not oversized:
            oversized = len(canonical_receipt.encode("utf-8")) > MAX_OPTION_SELECTION_RECEIPT_BYTES
        if oversized:
            receipt_invalid.append("canonical_receipt exceeds the inspection size limit")
            canonical_receipt_for_view = None
        else:
            try:
                candidate = OptionSelectionReceipt.model_validate_json(canonical_receipt)
            except (TypeError, ValueError):
                receipt_invalid.append(
                    "canonical_receipt does not validate as OptionSelectionReceipt"
                )

    if candidate is not None:
        try:
            is_exact_canonical = candidate.canonical_bytes().decode("utf-8") == canonical_receipt
            replay_valid = candidate.verifies()
        except (ArithmeticError, AssertionError, TypeError, ValueError):
            is_exact_canonical = False
            replay_valid = False
        if not is_exact_canonical:
            receipt_invalid.append(
                "canonical_receipt is not the receipt's canonical byte representation"
            )
        if not replay_valid:
            receipt_invalid.append(
                "receipt output digest or deterministic replay verification failed"
            )
        if output_digest != candidate.output_digest:
            receipt_invalid.append("payload output_digest does not match the canonical receipt")
        if status != candidate.status.value:
            receipt_invalid.append("payload status does not match the canonical receipt")
        if kind_for_view != candidate.status.value:
            receipt_invalid.append("record kind does not match the canonical receipt status")
        if decision_id != candidate.request.decision_id:
            receipt_invalid.append("payload decision_id does not match the canonical receipt")
        if recorded_at != candidate.evaluated_at:
            receipt_invalid.append("record timestamp does not match the receipt evaluation time")

    receipt_valid = candidate is not None and not receipt_invalid
    invalid.extend(receipt_invalid)
    # Chain lineage is reported separately: a sound receipt after an earlier
    # break remains inspectable, but it is not represented as part of a sound
    # append-only history.
    receipt = candidate if receipt_valid else None
    return OptionSelectionEntryView(
        sequence=sequence,
        recorded_at=recorded_at_for_view,
        kind=kind_for_view,
        decision_id=decision_id,
        status=status,
        output_digest=output_digest,
        canonical_receipt=canonical_receipt_for_view,
        receipt=receipt,
        receipt_valid=receipt_valid,
        record_hash_valid=record_hash_valid,
        record_valid=receipt_valid and record_hash_valid,
        chain_valid=chain_valid,
        invalid_detail="; ".join(invalid),
    )


def _required_payload_text(
    payload: dict[str, object],
    field: str,
    invalid: list[str],
    *,
    maximum: int | None = None,
) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        invalid.append(f"payload {field} is missing or is not non-empty text")
        return None
    if maximum is not None and len(value) > maximum:
        invalid.append(f"payload {field} exceeds the {maximum}-character inspection limit")
        return None
    return value


def _selection_status_text(value: object, label: str, invalid: list[str]) -> str | None:
    if not isinstance(value, str) or not value:
        if label == "record kind":
            invalid.append("record kind is missing or is not non-empty text")
        return None
    if len(value) > _MAX_OPTION_SELECTION_STATUS_CHARS:
        invalid.append(
            f"{label} exceeds the {_MAX_OPTION_SELECTION_STATUS_CHARS}-character inspection limit"
        )
        return None
    if value not in _OPTION_SELECTION_STATUSES:
        invalid.append(f"{label} is not a recognized option-selection status")
        return None
    return value


def _output_digest_text(payload: dict[str, object], invalid: list[str]) -> str | None:
    value = _required_payload_text(
        payload,
        "output_digest",
        invalid,
        maximum=MAX_OPTION_SELECTION_DIGEST_CHARS,
    )
    if value is None:
        return None
    if len(value) != MAX_OPTION_SELECTION_DIGEST_CHARS or any(
        character not in "0123456789abcdef" for character in value
    ):
        invalid.append("payload output_digest is not exactly 64 lowercase hexadecimal characters")
        return None
    return value


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


def _limit_text(value: Decimal | int) -> str:
    """A mandate limit as text: money through :func:`_decimal`, counts as counts.

    A limit is not read from a fixed-scale column, so it reaches here in
    whatever form the mandate was written in — a mandate authored as JSON keeps
    ``"1E+5"`` as ``Decimal("1E+5")``, which ``str()`` renders back as literal
    ``1E+5``. This is the panel whose whole purpose is letting the owner check
    their own authorization, so it is the last place a figure should need
    decoding. Counts are ``int``, have no exponent form, and would only be
    obscured by a Decimal path.
    """

    return _decimal(value) if isinstance(value, Decimal) else str(value)


def _sizing_ceiling(name: str, value: Decimal | int, *, model_discretion: bool) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=_limit_text(value), effect="BINDS")
    effect: LimitEffect = "NO_CEILING" if model_discretion else "AUTHORIZES_NOTHING"
    return MandateLimitView(name=name, effect=effect)


def _breach_ceiling(name: str, value: Decimal | int) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=_limit_text(value), effect="BINDS")
    return MandateLimitView(name=name, effect="NOT_ENFORCED")


def _floor(name: str, value: Decimal) -> MandateLimitView:
    if value > 0:
        return MandateLimitView(name=name, value=_limit_text(value), effect="BINDS")
    return MandateLimitView(name=name, effect="NO_FLOOR")


# ----------------------------------------------------------------------- bars


class BarView(ChronosModel):
    """One OHLCV bar, rendered for a chart.

    Prices are floats here, not ``Decimal``. That is a deliberate exception to
    this module's usual rule and it is safe for exactly one reason: nothing
    downstream of a chart is an order parameter. ``chronos.marketdata.bars``
    already holds bars as float for Pine parity, and re-quantizing them here
    would imply a precision the source never had. Every value that *does* reach
    an order still goes through ``Decimal`` at the execution boundary.
    """

    session_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarsView(ChronosModel):
    """A chart's whole answer, including the reasons it might be a poor one.

    ``refusal`` non-empty means there is nothing to draw and the panel says why.
    ``stale`` means the bars are real but were not re-fetched — the request was
    paced out — and the chart must be labelled rather than silently trusted.

    ``source`` matters more here than on other panels: the demo adapter emits a
    deterministic synthetic series, and a synthetic chart drawn in the same
    register as a live one would be the most convincing lie this terminal could
    tell. The client renders anything that is not ``ibkr`` as an explicit banner.
    """

    symbol: str
    interval: str
    lookback_days: int
    bars: tuple[BarView, ...]
    source: str
    fetched_at: str | None
    stale: bool
    refusal: str
    generated_at: str


def bars_view(
    series: BarSeries,
    *,
    lookback_days: int,
    source: str,
    fetched_at: datetime | None,
    stale: bool,
    refusal: str,
    now: datetime,
) -> BarsView:
    """Assemble the chart payload. Takes primitives, so it imports no app plane."""

    return BarsView(
        symbol=series.symbol,
        interval=series.interval.value,
        lookback_days=lookback_days,
        bars=tuple(
            BarView(
                session_date=bar.session_date.isoformat(),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in series.bars
        ),
        source=source,
        fetched_at=fetched_at.isoformat() if fetched_at is not None else None,
        stale=stale,
        refusal=refusal,
        generated_at=now.isoformat(),
    )


# --------------------------------------------------------------------- theses

#: How much narrative one panel row shows before it is cut. The journal keeps
#: the full text (an audit record that paraphrases is a record of someone's
#: reading), so truncation belongs here, where it can be labelled.
MAX_NARRATIVE_CHARS = 1200

#: How far back the theses scan reads. The stream is append-only and a thesis is
#: only interesting while it is the latest one for its symbol, so a bounded scan
#: answers the question at a bounded cost.
DEFAULT_THESES_SCAN = 400
MAX_THESES_SCAN = 2000


class ThesisView(ChronosModel):
    """What the system last said about one symbol, and whether it acted on it."""

    symbol: str
    kind: str
    asset_class: str
    confidence: str | None
    thesis: str
    thesis_truncated: bool
    rationale: str
    rationale_truncated: bool
    key_uncertainties: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    recorded_at: str
    stage: str
    refusal: str
    #: ``None`` means positions could not be read at all — never "not held".
    held: bool | None
    held_quantity: str | None


class ThesesView(ChronosModel):
    """Per-symbol beliefs, and the holdings the system has never spoken about.

    ``silent_holdings`` is the field this panel exists for. Listing only the
    symbols with a thesis would let a position the model has never mentioned
    disappear from the one view whose whole job is "what does the system believe
    about what it is holding". A holding with nothing on record is the single
    most interesting row here, so it is carried separately rather than omitted.

    ``positions_observed`` is ``False`` when the broker could not be read. Every
    ``held`` then reads ``None``, because "we could not ask" and "not held" are
    different facts and only one of them is reassuring.
    """

    theses: tuple[ThesisView, ...]
    silent_holdings: tuple[str, ...]
    positions_observed: bool
    scanned: int
    stream_present: bool
    account_fingerprint: str | None
    generated_at: str


def theses_view(
    session: Session,
    *,
    account_fingerprint: str,
    now: datetime,
    positions: dict[str, Decimal] | None,
    scan: int = DEFAULT_THESES_SCAN,
) -> ThesesView:
    """The latest belief per symbol, joined to what is actually held.

    ``positions`` is passed in rather than fetched: this module reads the
    database and nothing else, and a read-model that could reach a broker would
    be a read-model that can be slow in ways its caller cannot see.
    """

    bounded = _bounded(scan, maximum=MAX_THESES_SCAN)
    scoped = _scope(account_fingerprint)
    if scoped is None:
        return ThesesView(
            theses=(),
            silent_holdings=(),
            positions_observed=positions is not None,
            scanned=0,
            stream_present=False,
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

    seen: dict[str, ThesisView] = {}
    for row in rows:
        entry = _thesis_of(row, positions=positions)
        # Newest first, so the first sighting of a symbol is its latest belief.
        if entry is not None and entry.symbol not in seen:
            seen[entry.symbol] = entry

    spoken = set(seen)
    silent = tuple(sorted(symbol for symbol in (positions or {}) if symbol not in spoken))
    return ThesesView(
        theses=tuple(seen[symbol] for symbol in sorted(seen)),
        silent_holdings=silent,
        positions_observed=positions is not None,
        scanned=len(rows),
        stream_present=bool(rows),
        account_fingerprint=scoped,
        generated_at=_iso(now),
    )


def _thesis_of(row: HashChainRow, *, positions: dict[str, Decimal] | None) -> ThesisView | None:
    """One journal record as a belief, or ``None`` when it carries no narrative.

    Records written before M8d have no symbol and no thesis, and cycles that
    failed at the ingress never had a decision to describe. Both are skipped
    rather than rendered as a symbol-less blank row.
    """

    try:
        payload = hash_chain.payload_of(row)
    except (ValueError, TypeError):
        return None
    symbol = _text(payload.get("symbol"))
    if not symbol:
        return None

    thesis = _text(payload.get("thesis"))
    rationale = _text(payload.get("rationale"))
    held: bool | None = None
    quantity: str | None = None
    if positions is not None:
        size = positions.get(symbol)
        held = size is not None and size != 0
        quantity = _decimal(size) if size is not None else None

    return ThesisView(
        symbol=symbol,
        kind=_text(payload.get("kind")),
        asset_class=_text(payload.get("asset_class")),
        confidence=_optional_text(payload.get("confidence")),
        thesis=thesis[:MAX_NARRATIVE_CHARS],
        thesis_truncated=len(thesis) > MAX_NARRATIVE_CHARS,
        rationale=rationale[:MAX_NARRATIVE_CHARS],
        rationale_truncated=len(rationale) > MAX_NARRATIVE_CHARS,
        key_uncertainties=_string_tuple(payload.get("key_uncertainties")),
        invalidation_conditions=_string_tuple(payload.get("invalidation_conditions")),
        recorded_at=_iso(row.recorded_at),
        stage=_text(payload.get("stage")) or row.kind,
        refusal=_text(payload.get("refusal")),
        held=held,
        held_quantity=quantity,
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    """A journal list as strings. Anything else reads as empty, never as garbage."""

    if not isinstance(value, list):
        return ()
    return tuple(_text(item) for item in value if _text(item))

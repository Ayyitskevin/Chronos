"""CB-1: one supervised consumer journals the broker's unsolicited callbacks (D-16 items 1-3, 5).

The official adapter's reader thread (and the Demo seam) hands every unsolicited execution,
orderStatus and commissionReport to :meth:`CallbackConsumer.offer` as a raw
:class:`~chronos.broker.callbacks.StreamEvent`. ``offer`` only enqueues: no database, no
asyncio, no conversion. One consumer thread then, per event, in three committed steps:

1. journals it (``application_events`` row ``callback_journal``, keyed by a broker-derived
   identity; a replay is a no-op, a divergent replay is a typed ``callback_conflict``);
2. applies at most ONE effect: :meth:`~chronos.orders.tracker.OrderTracker.ingest` for an
   orderStatus, or the injected ``record_execution`` (the fills writer, wired by the runtime)
   for an execution, never both;
3. journals the typed outcome (``callback_outcome``).

A crash between the steps leaves the event PENDING (journaled, no outcome). A sweep re-drives
PENDING rows from their STORED facts on every start, after a failure streak clears, and every
``_SWEEP_SECONDS`` (never on an idle second: the sweep is a full anti-join over an append-only
journal). The effects are idempotent (the tracker's event key, the execution id), so a re-drive
never applies twice and no lifecycle, fill or commission advance can exist without its journal
row before it.

Correlation is exact or nothing: an execution must be own at capture (the RC-1 classifier), in
the bound account and proven by the persisted ownership ledger; an orderStatus must be own at
capture and match exactly one intent's persisted broker order id, client id and permId. Anything
else is a typed outcome that mutates nothing.

Readiness: the consumer holds ONE latch callable, ``invalidate``. It demotes readiness on a
divergent replay, malformed or unproven input, a lifecycle contradiction, a dropped event, a
failure, a lost writer lease or a stop with loss. It never publishes, reconciles or reads
readiness: ``AppRuntime.reconcile_submission_readiness`` stays the only publisher, and no
callback re-arms anything (D-16 item 4 is not admitted).

Ingress is at most once: IBKR offers no acknowledgement or cursor, so an event still queued in
memory when the process dies is lost; the startup reconciliation must pass before readiness is
RECONCILED again. Exactly once is claimed for JOURNALING per identity only.

What IBKR actually streams to which client id is UNVERIFIED on live (K4): demo/synthetic only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.broker.base import OrderOwnershipLedger, VenueOrderIdentity
from chronos.broker.callbacks import (
    _BROKER_ID_MAX,
    _MIN_CLIENT_ID,
    _MIN_ORDER_ID,
    _UNSET_THRESHOLD,
    StreamEvent,
    StreamKind,
    _broker_id,
)
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import BrokerExecution
from chronos.orders.state_machine import OrderLifecycleError
from chronos.orders.tracker import (
    OrderIdentityConflict,
    OrderStatusUpdate,
    OrderTracker,
    broker_status_to_lifecycle,
)
from chronos.persistence.order_repositories import OrderIntentRepository
from chronos.persistence.repositories import (
    _reject_raw_account_event_data,
    _require_matching_account_scope,
    _require_scope,
)
from chronos.persistence.schema import ApplicationEventRow, OrderEventRow
from chronos.utils.identifiers import PersistenceKey, account_fingerprint
from chronos.utils.time import utc_now

_LOGGER = logging.getLogger("chronos.orders.callback_consumer")

#: The journal's ``application_events.event_type`` values (no new table: no schema change).
CALLBACK_JOURNAL = "callback_journal"
CALLBACK_OUTCOME = "callback_outcome"
CALLBACK_CONFLICT = "callback_conflict"
CALLBACK_CONSUMER_HEALTH = "callback_consumer_health"

#: ``order_events.source`` of a lifecycle row the consumer applied.
CALLBACK_SOURCE = "CALLBACK"

#: The typed reasons the consumer gives readiness when it demotes it (tightening only).
DROPPED_EVENT_REASON = "callback consumer dropped an event; reconciliation required"
CONSUMER_FAILED_REASON = "callback consumer failed; reconciliation required"
DIVERGENT_REPLAY_REASON = "divergent broker callback replay; reconciliation required"
UNPROVEN_CALLBACK_REASON = (
    "broker callback could not be proven against the order book; reconciliation required"
)

QUEUE_CAPACITY = 1024
COMMISSION_GRACE = timedelta(seconds=30)
DEFER_GRACE = timedelta(seconds=60)
_IDLE_SECONDS = 1.0
#: CB1-FIX1 F5: the PENDING sweep is a full anti-join over the append-only journal, so it runs on
#: start, after a failure streak clears and on this cadence -- never on every idle second.
_SWEEP_SECONDS = 30.0
#: Retry backoff per failed attempt: base * 2**(attempt-1), capped at 10 * base. At the default a
#: persistently failing event holds the one consumer thread 0.5+1+2+4+5 = 12.5 s before its drop.
_RETRY_BASE_SECONDS = 0.5
_STOP_DRAIN_SECONDS = 2.0
_RETRY_LIMIT = 5
_SWEEP_LIMIT = 200

#: Intents whose lifecycle a callback may advance. ``USER_CONFIRMED`` and ``SUBMISSION_UNKNOWN``
#: belong to the submission boundary's CAS and to the restart reconciler: a callback for one of
#: them waits (PENDING) and, past ``DEFER_GRACE``, is left to the reconciler.
_INGESTIBLE = frozenset(
    {
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
        OrderLifecycle.FILLED,
        OrderLifecycle.CANCELLED,
        OrderLifecycle.REJECTED,
    }
)


class CallbackOutcome(StrEnum):
    """What the consumer did with one journaled event (the ``callback_outcome`` row)."""

    APPLIED = "applied"
    IDENTITY_REFINED = "identity_refined"
    NO_CHANGE = "no_change"
    ALREADY_RECORDED = "already_recorded"
    PAIRED = "paired"
    NOT_OWN_AT_CAPTURE = "not_own_at_capture"
    CROSS_ACCOUNT = "cross_account"
    UNKNOWN_INTENT = "unknown_intent"
    AMBIGUOUS_INTENT = "ambiguous_intent"
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_CONFLICT = "identity_conflict"
    OWNERSHIP_UNPROVEN = "ownership_unproven"
    LIFECYCLE_CONTRADICTION = "lifecycle_contradiction"
    UNRECOGNIZED_STATUS = "unrecognized_status"
    DEFERRED_TO_RECONCILER = "deferred_to_reconciler"
    UNKNOWN_EXECUTION = "unknown_execution"
    EXECUTION_CONFLICT = "execution_conflict"
    EXECUTION_REFUSED = "execution_refused"
    LATE_COMMISSION_REFUSED = "late_commission_refused"
    MALFORMED = "malformed"
    REFUSED_UNSAFE_PAYLOAD = "refused_unsafe_payload"


class ExecutionWriteOutcome(StrEnum):
    """The fills writer's answer, mapped by the runtime's write-only adapter."""

    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    CONFLICT = "conflict"
    REFUSED = "refused"


class JournalVerdict(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


#: Outcomes that also demote readiness (the evidence contradicts or cannot be proven). The rest do
#: not: the successes; NOT_OWN_AT_CAPTURE (RC-1 already demoted when it arrived);
#: DEFERRED_TO_RECONCILER (a SUBMISSION_UNKNOWN intent already keeps the composite locked as
#: unresolved; a pre-submit status carries no persisted broker order id, so it is never the one
#: candidate); LATE_COMMISSION_REFUSED (cost-basis evidence, no order state; D-15: leave).
_INVALIDATING = frozenset(
    {
        CallbackOutcome.UNKNOWN_EXECUTION,  # CB1-FIX1 F6: as unproven as an unknown intent
        CallbackOutcome.CROSS_ACCOUNT,
        CallbackOutcome.UNKNOWN_INTENT,
        CallbackOutcome.AMBIGUOUS_INTENT,
        CallbackOutcome.IDENTITY_MISMATCH,
        CallbackOutcome.IDENTITY_CONFLICT,
        CallbackOutcome.OWNERSHIP_UNPROVEN,
        CallbackOutcome.LIFECYCLE_CONTRADICTION,
        CallbackOutcome.UNRECOGNIZED_STATUS,
        CallbackOutcome.EXECUTION_CONFLICT,
        CallbackOutcome.EXECUTION_REFUSED,
        CallbackOutcome.MALFORMED,
        CallbackOutcome.REFUSED_UNSAFE_PAYLOAD,
    }
)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One ``callback_journal`` row read back."""

    row_id: int
    key: str
    kind: StreamKind
    facts: dict[str, Any]
    received_at: datetime
    own_at_capture: bool


@dataclass(frozen=True, slots=True)
class JournalAppend:
    verdict: JournalVerdict
    differing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsumerHealth:
    state: str
    queued: int
    dropped: int
    failures: int
    last_error_type: str | None


class _WriteAuthorityLost(BaseException):
    """Internal: this worker may write no more (CB1-FIX2). Raised immediately before a write.

    A ``BaseException`` so no step's ``except Exception`` retries or swallows it: the event's
    remaining writes are skipped (a journaled row stays PENDING) and ``_run`` ends the worker.
    """

    def __init__(self, *, abandoned: bool) -> None:
        super().__init__("abandoned by stop" if abandoned else "writer lease lost")
        self.abandoned = abandoned


class _AlreadyJournaled(Exception):
    """Internal: an earlier row holds this key; the savepoint rolls the new one back."""

    def __init__(self, stored: dict[str, Any]) -> None:
        super().__init__("already journaled")
        self.stored = stored


@dataclass(frozen=True, slots=True)
class _Normalized:
    kind: StreamKind
    identity: dict[str, Any]
    facts: dict[str, Any]
    malformed: bool


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_key(identity: dict[str, Any]) -> str:
    """The journal key: sha256 of the canonical identity (64 hex, fits ``correlation_id``)."""

    return _digest(identity)


class CallbackJournal:
    """Append-only journal rows in ``application_events``; exactly once per (type, key).

    No uniqueness constraint exists on ``correlation_id`` (adding one is a migration), so the
    write is insert-first, the ``OrderTrackerRepository.record_transition`` idiom: the INSERT
    takes SQLite's write lock inside the savepoint, then the earliest row with the same type and
    key is read; if one exists the savepoint rolls back and nothing is written.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def _append(
        self,
        event_type: str,
        key: str,
        data: dict[str, Any],
        *,
        message: str,
        severity: str = "INFO",
    ) -> dict[str, Any] | None:
        """Write the row once; return None when written, else the earlier row's data."""

        _reject_raw_account_event_data(persisted_values=(event_type, key, message, data))
        with self._sessions.begin() as session:
            _require_scope(session)
            row = ApplicationEventRow(
                event_type=event_type,
                severity=severity,
                correlation_id=key,
                message=message,
                event_data=data,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
                    earlier = session.scalar(
                        select(ApplicationEventRow)
                        .where(
                            ApplicationEventRow.event_type == event_type,
                            ApplicationEventRow.correlation_id == key,
                            ApplicationEventRow.id < row.id,
                        )
                        .order_by(ApplicationEventRow.id)
                        .limit(1)
                    )
                    if earlier is not None:
                        raise _AlreadyJournaled(dict(earlier.event_data))
            except _AlreadyJournaled as already:
                return already.stored
        return None

    def append_pending(
        self,
        key: str,
        kind: StreamKind,
        facts: dict[str, Any],
        *,
        received_at: datetime,
        own_at_capture: bool,
    ) -> JournalAppend:
        stored = self._append(
            CALLBACK_JOURNAL,
            key,
            {
                "kind": kind.value,
                "facts": facts,
                "received_at": received_at.isoformat(),
                "own_at_capture": own_at_capture,
            },
            message=f"broker {kind.value} callback journaled",
        )
        if stored is None:
            return JournalAppend(JournalVerdict.NEW)
        stored_facts = stored.get("facts")
        if stored.get("kind") == kind.value and stored_facts == facts:
            return JournalAppend(JournalVerdict.REPLAY)
        previous = stored_facts if isinstance(stored_facts, dict) else {}
        names = set(previous) | set(facts)
        differing = tuple(sorted(n for n in names if previous.get(n) != facts.get(n)))
        return JournalAppend(JournalVerdict.CONFLICT, differing or ("kind",))

    def append_outcome(
        self, key: str, kind: StreamKind, outcome: CallbackOutcome, detail: dict[str, Any]
    ) -> bool:
        severity = "WARNING" if outcome in _INVALIDATING else "INFO"
        return (
            self._append(
                CALLBACK_OUTCOME,
                key,
                {"kind": kind.value, "outcome": outcome.value, **detail},
                message=f"broker {kind.value} callback: {outcome.value}",
                severity=severity,
            )
            is None
        )

    def append_conflict(
        self, key: str, kind: StreamKind, differing: tuple[str, ...], facts: dict[str, Any]
    ) -> bool:
        digest = _digest(facts)
        return (
            self._append(
                CALLBACK_CONFLICT,
                _digest({"key": key, "facts": digest}),
                {"kind": kind.value, "key": key, "fields": list(differing), "digest": digest},
                message=f"divergent broker {kind.value} callback replay",
                severity="WARNING",
            )
            is None
        )

    def append_health(self, data: dict[str, Any], *, message: str) -> None:
        _reject_raw_account_event_data(persisted_values=(message, data))
        with self._sessions.begin() as session:
            _require_scope(session)
            session.add(
                ApplicationEventRow(
                    event_type=CALLBACK_CONSUMER_HEALTH,
                    severity="WARNING",
                    message=message,
                    event_data=data,
                )
            )

    def entry(self, key: str) -> JournalEntry | None:
        with self._sessions() as session:
            _require_scope(session)
            row = session.scalar(
                select(ApplicationEventRow)
                .where(
                    ApplicationEventRow.event_type == CALLBACK_JOURNAL,
                    ApplicationEventRow.correlation_id == key,
                )
                .order_by(ApplicationEventRow.id)
                .limit(1)
            )
            return _entry(row) if row is not None else None

    def outcome(self, key: str) -> str | None:
        with self._sessions() as session:
            _require_scope(session)
            row = session.scalar(
                select(ApplicationEventRow)
                .where(
                    ApplicationEventRow.event_type == CALLBACK_OUTCOME,
                    ApplicationEventRow.correlation_id == key,
                )
                .order_by(ApplicationEventRow.id)
                .limit(1)
            )
            return str(row.event_data.get("outcome")) if row is not None else None

    def pending(self, *, limit: int = _SWEEP_LIMIT) -> tuple[JournalEntry, ...]:
        """Journal rows with no outcome row, oldest first."""

        with self._sessions() as session:
            _require_scope(session)
            closed = select(ApplicationEventRow.correlation_id).where(
                ApplicationEventRow.event_type == CALLBACK_OUTCOME
            )
            rows = session.scalars(
                select(ApplicationEventRow)
                .where(
                    ApplicationEventRow.event_type == CALLBACK_JOURNAL,
                    ApplicationEventRow.correlation_id.not_in(closed),
                )
                .order_by(ApplicationEventRow.id)
                .limit(limit)
            ).all()
            return tuple(_entry(row) for row in rows)


def _entry(row: ApplicationEventRow) -> JournalEntry:
    data = dict(row.event_data)
    return JournalEntry(
        row_id=int(row.id),
        key=str(row.correlation_id),
        kind=StreamKind(str(data["kind"])),
        facts=dict(data["facts"]),
        received_at=datetime.fromisoformat(str(data["received_at"])),
        own_at_capture=data.get("own_at_capture") is True,
    )


# --------------------------------------------------------------------------------------------- #
# Normalization (consumer thread): raw callback values to JSON-ready facts, or a typed malformed
# --------------------------------------------------------------------------------------------- #


def _quantity_text(value: object) -> str | None:
    """A finite, non-negative broker number as a canonical string, else None (malformed)."""

    if type(value) not in (int, float, Decimal):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number < 0 or number >= Decimal(_UNSET_THRESHOLD):
        return None
    return format((number + 0).normalize(), "f")


def _id_field(value: object) -> int | None:
    """An identifier exactly as sent (an int), else None. Domain checks are correlation's job."""

    return value if type(value) is int else None


def _safe_text(value: object) -> str:
    try:
        return repr(value)[:200]
    except Exception:
        return f"<{type(value).__name__}>"


def _malformed(kind: StreamKind, fields: list[str], raw: object) -> _Normalized:
    digest = _digest({"kind": kind.value, "raw": _safe_text(raw), "fields": sorted(fields)})
    identity = {"kind": kind.value, "malformed_digest": digest}
    return _Normalized(
        kind, identity, {"malformed": sorted(set(fields)), "digest": digest}, malformed=True
    )


def _normalize_order_status(raw: dict[str, Any], scope_fingerprint: str) -> _Normalized:
    kind = StreamKind.ORDER_STATUS
    order_id = _id_field(raw.get("order_id"))
    client_id = _id_field(raw.get("client_id"))
    perm_id = _id_field(raw.get("perm_id"))
    status = raw.get("status")
    filled = _quantity_text(raw.get("filled"))
    remaining = _quantity_text(raw.get("remaining"))
    average = _quantity_text(raw.get("avg_fill_price"))
    malformed = [
        name
        for name, value in (
            ("order_id", order_id),
            ("client_id", client_id),
            ("perm_id", perm_id),
            ("status", status if type(status) is str and status.strip() else None),
            ("filled", filled),
            ("remaining", remaining),
            ("avg_fill_price", average),
        )
        if value is None
    ]
    if malformed:
        return _malformed(kind, malformed, raw)
    assert isinstance(status, str)
    identity = {
        "kind": kind.value,
        "scope_fingerprint": scope_fingerprint,
        "client_id": client_id,
        "order_id": order_id,
        "perm_id": perm_id,
        "status": status.strip().lower(),
        "filled": filled,
    }
    facts = {**identity, "remaining": remaining, "avg_fill_price": average}
    return _Normalized(kind, identity, facts, malformed=False)


def _execution_facts(execution: BrokerExecution, fingerprint: str) -> dict[str, Any]:
    dumped = execution.model_dump(mode="json")
    for name in ("account_id", "commission", "commission_currency"):
        dumped.pop(name, None)
    return {"kind": StreamKind.EXECUTION.value, "scope_fingerprint": fingerprint, **dumped}


def _normalize_commission(raw: dict[str, Any]) -> _Normalized:
    kind = StreamKind.COMMISSION
    exec_id = raw.get("execId")
    amount = _quantity_text(raw.get("commission"))
    currency = raw.get("currency")
    malformed = [
        name
        for name, ok in (
            ("execId", type(exec_id) is str and bool(exec_id.strip())),
            ("commission", amount is not None),
            ("currency", type(currency) is str and bool(currency.strip())),
        )
        if not ok
    ]
    if malformed:
        return _malformed(kind, malformed, raw)
    assert isinstance(exec_id, str) and isinstance(currency, str)
    identity = {"kind": kind.value, "execution_id": exec_id.strip()}
    facts = {**identity, "amount": amount, "currency": currency.strip().upper()}
    return _Normalized(kind, identity, facts, malformed=False)


# --------------------------------------------------------------------------------------------- #
# The consumer
# --------------------------------------------------------------------------------------------- #

_STOP = object()


class CallbackConsumer:
    """Bounded, supervised, restart-safe: see the module docstring."""

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        intents: OrderIntentRepository,
        tracker: OrderTracker,
        ownership: OrderOwnershipLedger,
        record_execution: Callable[[BrokerExecution], ExecutionWriteOutcome],
        account_id: str,
        persistence_key: PersistenceKey,
        invalidate: Callable[[str], object],
        convert_execution: Callable[..., BrokerExecution] | None = None,
        clock: Callable[[], datetime] = utc_now,
        capacity: int = QUEUE_CAPACITY,
        commission_grace: timedelta = COMMISSION_GRACE,
        defer_grace: timedelta = DEFER_GRACE,
    ) -> None:
        self._sessions = sessions
        self.journal = CallbackJournal(sessions)
        self._intents = intents
        self._tracker = tracker
        self._ownership = ownership
        self._record_execution = record_execution
        self._account_id = account_id
        self._fingerprint = account_fingerprint(account_id, key=persistence_key)
        self._persistence_key = persistence_key
        self._invalidate_callable = invalidate
        self._convert_execution = convert_execution
        self._clock = clock
        self._commission_grace = commission_grace
        self._defer_grace = defer_grace
        self._queue: queue.Queue[object] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._accepting = False
        self._state = "stopped"
        self._dropped = 0
        self._dropped_recorded = 0
        self._failures = 0
        self._last_error_type: str | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        # CB1-FIX2 P1-1: the running worker's token. stop() revokes it, so a worker it could not
        # join writes nothing further; the worker keeps its own copy in a thread-local.
        self._token: object | None = None
        self._worker = threading.local()
        self._inflight: StreamEvent | None = None  # the event the worker holds right now
        # CB1-FIX4 P2-1: the held event has ONE accounting owner. Whoever counts it lost first (the
        # worker's terminal drop or exceptional exit, or the stop that abandons the worker) sets
        # this under _lock, and nobody counts it again (DESIGN §11, I16/I19).
        self._inflight_counted = False
        # CB1-FIX4 P2-2: every database write the consumer makes runs under this lock with its
        # authority check (the worker's handle/sweep, and every health row, stop's included), so
        # at most one checked write is ever in flight (DESIGN §4, R10). Never taken under _lock.
        self._write_lock = threading.Lock()
        # CB1-FIX4 P2-1: stop() runs one at a time; a second stop sees the first one's result
        self._stop_lock = threading.Lock()
        # time.monotonic() of the last sweep; the cadence starts at construction, so the start
        # sweep in _run is the only immediate one
        self._last_sweep = time.monotonic()
        self._sweep_requested = False  # a failure streak cleared: sweep at the next loop
        self._is_writer: Callable[[], bool] = lambda: True

    # ---------------------------------------------------------------- reader-thread side

    def offer(self, event: StreamEvent) -> None:
        """Enqueue one streamed callback. Never blocks, never raises, never touches the DB."""

        try:
            # CB1-FIX3 P1-1: the check and the put are ONE critical section under the lock every
            # _accepting flip takes, so no put can land after stop() flips and drains (DESIGN §11).
            # put_nowait never blocks, and no database call ever runs under this lock.
            with self._lock:
                accepting = self._accepting
                if accepting:
                    self._queue.put_nowait(event)
            if not accepting:
                self._drop(event, "callback consumer is not running")
        except queue.Full:
            self._drop(event, "callback consumer queue is full")
        except Exception:  # pragma: no cover - defensive: the reader thread keeps its result
            self._drop(event, "callback consumer could not enqueue")

    def _drop(self, event: object, why: str) -> None:
        with self._lock:
            held = getattr(self._worker, "token", None) is not None and event is self._inflight
            if held and not self._claim_held():
                return  # CB1-FIX4 P2-1: an abandoning stop already counted, logged and demoted it
            self._dropped += 1
        kind = getattr(getattr(event, "kind", None), "value", "unknown")
        _LOGGER.warning(
            "%s; an unsolicited broker callback was not journaled",
            why,
            extra={"event": "callback_consumer_dropped", "stream": kind},
        )
        self._invalidate(DROPPED_EVENT_REASON)

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> None:
        """Accept events (the synchronous mode tests drive with :meth:`drain`)."""

        with self._lock:
            self._accepting = True
            self._state = "running"

    def start(self, *, is_writer: Callable[[], bool]) -> None:
        """Recover PENDING rows, then run the consumer thread (writer only)."""

        if self._thread is not None:
            return
        self._is_writer = is_writer
        self._stopping.clear()
        self.open()
        token = object()
        self._token = token
        thread = threading.Thread(
            target=self._run, args=(token,), name="chronos-callback-consumer", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = _STOP_DRAIN_SECONDS) -> int:
        """Stop accepting, drain for up to ``timeout``, count what is left as lost.

        CB1-FIX2 P1-1: a worker still alive after the joins is abandoned. Its token is revoked, so
        it writes nothing further; the event it holds counts as lost; and readiness is demoted and
        the stop recorded exactly as for queued losses, before the caller disposes the database.
        """

        with self._stop_lock:  # CB1-FIX4 P2-1: a concurrent second stop finds _thread None
            with self._lock:
                self._accepting = False
            thread = self._thread
            lost = 0
            abandoned = False
            if thread is not None:
                try:
                    self._queue.put(_STOP, timeout=timeout)
                except queue.Full:
                    self._stopping.set()
                thread.join(timeout)
                self._stopping.set()
                thread.join(0.1)
                with self._lock:
                    self._token = None
                    abandoned = thread.is_alive()
                    if abandoned and self._claim_held():
                        lost += 1
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP:
                    lost += 1
            with self._lock:
                self._state = "stopped"
                self._dropped += lost
            self._thread = None
            if lost or abandoned:
                _LOGGER.warning(
                    "Callback consumer stopped with %d unjournaled events%s",
                    lost,
                    "; its worker did not exit and was abandoned" if abandoned else "",
                    extra={
                        "event": "callback_consumer_stopped",
                        "lost": lost,
                        "abandoned": abandoned,
                    },
                )
                self._invalidate(DROPPED_EVENT_REASON if lost else CONSUMER_FAILED_REASON)
                reason = "stopped with unjournaled events" if lost else "stopped"
                if abandoned:
                    reason += "; worker abandoned"
                # CB1-FIX4 P2-2: waits (up to timeout) for a write the abandoned worker has in
                # flight, then re-checks the lease, so it is never a second write after a loss
                self._record_health("stopped", reason=reason, wait=timeout)
            return lost

    def health(self) -> ConsumerHealth:
        with self._lock:
            return ConsumerHealth(
                state=self._state,
                queued=self._queue.qsize(),
                dropped=self._dropped,
                failures=self._failures,
                last_error_type=self._last_error_type,
            )

    def _run(self, token: object) -> None:
        self._worker.token = token
        try:
            if self._writer():
                self._sweep_safely()
            while not self._stopping.is_set():
                if not self._writer():
                    self._halt("writer lease lost")
                    return
                self._note_drops()
                if self._sweep_is_due():
                    self._sweep_safely()
                try:
                    item = self._queue.get(timeout=_IDLE_SECONDS)
                except queue.Empty:
                    continue
                if item is _STOP:
                    return
                assert isinstance(item, StreamEvent)
                with self._lock:
                    if token is not self._token:
                        # stop() abandoned this worker after the get, so it could not count this
                        # event; it is lost, and that stop already demoted readiness
                        self._dropped += 1
                        return
                    self._inflight = item  # _inflight_counted is False: reset in finally
                try:
                    self._handle_with_retry(item)
                except BaseException:
                    # CB1-FIX3 P1-1b: a lost lease or a dying thread leaves the held event
                    # unfinished; count it (an abandoned worker's event was counted by its stop)
                    self._count_held_lost()
                    raise
                finally:
                    with self._lock:
                        self._inflight = None
                        self._inflight_counted = False
        except _WriteAuthorityLost as lost:
            if not lost.abandoned:
                self._halt("writer lease lost")
            # abandoned: the stop that revoked the token already demoted and recorded it
        except BaseException as error:  # the thread dies loudly, never silently
            self._halt(f"consumer thread died ({type(error).__name__})")
        finally:
            with self._lock:
                self._accepting = False

    def _claim_held(self) -> bool:
        """Under ``_lock``: take the held event's accounting, once (CB1-FIX4 P2-1)."""

        if self._inflight is None or self._inflight_counted:
            return False
        self._inflight_counted = True
        return True

    def _count_held_lost(self) -> None:
        """The worker's exceptional exit counts its held event, unless a stop already did."""

        with self._lock:
            if self._claim_held():
                self._dropped += 1

    def _require_write_authority(self) -> None:
        """CB1-FIX2: called immediately before every database write the consumer makes."""

        token = getattr(self._worker, "token", None)  # None: synchronous mode (drain)
        if token is not None and token is not self._token:
            raise _WriteAuthorityLost(abandoned=True)
        if not self._writer():  # P2-1: the lease bounds every write, not only the next loop
            raise _WriteAuthorityLost(abandoned=False)

    def _writer(self) -> bool:
        try:
            return bool(self._is_writer())
        except Exception:
            return False

    def _halt(self, reason: str) -> None:
        with self._lock:
            self._accepting = False
            self._state = "halted"
        _LOGGER.error(
            "Callback consumer halted: %s; every further callback demotes readiness",
            reason,
            extra={"event": "callback_consumer_halted"},
        )
        self._invalidate(CONSUMER_FAILED_REASON)
        if reason != "writer lease lost":
            self._record_health("halted", reason=reason)

    def _note_drops(self) -> None:
        with self._lock:
            dropped, recorded = self._dropped, self._dropped_recorded
        if dropped > recorded:
            self._record_health("dropped", reason=f"{dropped - recorded} events not journaled")
            with self._lock:
                self._dropped_recorded = dropped

    def _record_health(self, state: str, *, reason: str, wait: float = -1.0) -> None:
        # CB1-FIX4 P2-2: the health row is a consumer write like any other: it takes the write
        # lock (stop() bounds the wait) and checks its authority under it
        if not self._write_lock.acquire(timeout=wait):
            _LOGGER.warning(
                "callback consumer health row was not persisted: a consumer write is in flight",
                extra={"event": "callback_consumer_health_skipped"},
            )
            return
        try:
            try:
                self._require_write_authority()
            except _WriteAuthorityLost:
                return  # CB1-FIX2 P2-1: a non-writer records nothing; its log line still fires
            current = self.health()
            try:
                self.journal.append_health(
                    {
                        "state": state,
                        "reason": reason,
                        "dropped": current.dropped,
                        "failures": current.failures,
                        "last_error_type": current.last_error_type,
                    },
                    message=f"callback consumer {state}: {reason}",
                )
            except Exception:
                _LOGGER.warning("callback consumer health row was not persisted")
        finally:
            self._write_lock.release()

    def _invalidate(self, reason: str) -> None:
        try:
            self._invalidate_callable(reason)
        except Exception:
            _LOGGER.exception(
                "Callback consumer readiness invalidation failed",
                extra={"event": "callback_consumer_invalidation_failed"},
            )

    def _handle_with_retry(self, event: StreamEvent) -> None:
        for attempt in range(1, _RETRY_LIMIT + 1):
            try:
                self.handle(event)
            except Exception as error:
                self._note_failure(error)
                backoff = min(_RETRY_BASE_SECONDS * 2 ** (attempt - 1), 10 * _RETRY_BASE_SECONDS)
                if self._stopping.wait(backoff):
                    break
                continue
            with self._lock:
                streak_cleared = self._failures > 0
                self._failures = 0
                if self._state == "degraded":
                    self._state = "running"
                if streak_cleared:
                    self._sweep_requested = True
            return
        # still unhandled: a journaled event stays PENDING for the sweep; an unjournaled one is lost
        self._drop(event, "callback consumer gave up on an event after retries")

    def _note_failure(self, error: Exception) -> None:
        with self._lock:
            first = self._failures == 0
            self._failures += 1
            self._last_error_type = type(error).__name__
            self._state = "degraded"
        _LOGGER.warning(
            "Callback consumer step failed (%s); the event is retried",
            type(error).__name__,
            extra={"event": "callback_consumer_failed", "error_type": type(error).__name__},
        )
        if first:
            self._invalidate(CONSUMER_FAILED_REASON)

    def _sweep_is_due(self) -> bool:
        with self._lock:
            requested = self._sweep_requested
        return requested or time.monotonic() - self._last_sweep >= _SWEEP_SECONDS

    def _sweep_safely(self) -> None:
        with self._lock:
            self._sweep_requested = False
        self._last_sweep = time.monotonic()
        try:
            self.sweep()
        except Exception as error:
            self._note_failure(error)

    # ---------------------------------------------------------------- consumer-thread side

    def drain(self) -> int:
        """Handle every queued event in the caller's thread (synchronous mode)."""

        handled = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return handled
            if item is _STOP:
                continue
            assert isinstance(item, StreamEvent)
            self.handle(item)
            handled += 1

    def sweep(self) -> int:
        """Re-drive PENDING journal rows (no outcome yet), oldest first, from stored facts."""

        with self._write_lock:  # CB1-FIX4 P2-2: one write bound across the worker and stop
            return self._sweep_locked()

    def _sweep_locked(self) -> int:
        closed = 0
        for entry in self.journal.pending():
            if self._apply_and_close(entry, recovered=True):
                closed += 1
        return closed

    def handle(self, event: StreamEvent) -> CallbackOutcome | None:
        """Journal one event, apply its one effect, journal its outcome (in that order)."""

        with self._write_lock:  # CB1-FIX4 P2-2: one write bound across the worker and stop
            return self._handle_locked(event)

    def _handle_locked(self, event: StreamEvent) -> CallbackOutcome | None:
        normalized = self._normalize(event)
        key = event_key(normalized.identity)
        self._require_write_authority()
        try:
            appended = self.journal.append_pending(
                key,
                normalized.kind,
                normalized.facts,
                received_at=event.received_at,
                own_at_capture=event.own_at_capture,
            )
        except ValueError:
            # the raw-account guard refused the payload: journal a digest only, typed
            return self._refuse_unsafe(normalized, event)
        if appended.verdict is JournalVerdict.CONFLICT:
            self._require_write_authority()
            self.journal.append_conflict(key, normalized.kind, appended.differing, normalized.facts)
            _LOGGER.warning(
                "Divergent broker callback replay; the first journal row stands",
                extra={"event": "callback_divergent_replay", "stream": normalized.kind.value},
            )
            self._invalidate(DIVERGENT_REPLAY_REASON)
            return None
        if self.journal.outcome(key) is not None:
            return None  # a replay of a closed event: no-op
        entry = self.journal.entry(key)
        assert entry is not None
        outcome = self._apply_and_close(entry, recovered=appended.verdict is JournalVerdict.REPLAY)
        return outcome

    def _refuse_unsafe(self, normalized: _Normalized, event: StreamEvent) -> CallbackOutcome:
        digest = _digest(normalized.facts)
        identity = {"kind": normalized.kind.value, "refused_digest": digest}
        key = event_key(identity)
        facts = {"refused": "raw-account-like value", "digest": digest}
        self._require_write_authority()
        self.journal.append_pending(
            key,
            normalized.kind,
            facts,
            received_at=event.received_at,
            own_at_capture=event.own_at_capture,
        )
        self._require_write_authority()
        self.journal.append_outcome(
            key, normalized.kind, CallbackOutcome.REFUSED_UNSAFE_PAYLOAD, {}
        )
        self._invalidate(UNPROVEN_CALLBACK_REASON)
        return CallbackOutcome.REFUSED_UNSAFE_PAYLOAD

    def _apply_and_close(self, entry: JournalEntry, *, recovered: bool) -> CallbackOutcome | None:
        result = self._apply(entry)
        if result is None:
            return None  # stays PENDING (deferred or awaiting its commission)
        outcome, detail = result
        self._require_write_authority()
        if (
            self.journal.append_outcome(
                entry.key, entry.kind, outcome, {**detail, "recovered": recovered}
            )
            and outcome in _INVALIDATING
        ):
            self._invalidate(UNPROVEN_CALLBACK_REASON)
        return outcome

    def _normalize(self, event: StreamEvent) -> _Normalized:
        if event.kind is StreamKind.ORDER_STATUS:
            return _normalize_order_status(event.raw, self._fingerprint)
        if event.kind is StreamKind.COMMISSION:
            return _normalize_commission(event.raw)
        return self._normalize_execution(event)

    def _normalize_execution(self, event: StreamEvent) -> _Normalized:
        kind = StreamKind.EXECUTION
        execution = event.execution
        raw_fields = event.execution_fields
        if execution is None:
            if raw_fields is None or event.contract is None or self._convert_execution is None:
                return _malformed(kind, ["execution"], vars(raw_fields) if raw_fields else None)
            ids = {
                name: _id_field(getattr(raw_fields, name, None))
                for name in ("clientId", "orderId", "permId")
            }
            bad = [name for name, value in ids.items() if value is None]
            if bad:
                return _malformed(kind, bad, vars(raw_fields))
            try:
                # the commission is paired later from the journal, never from a cache
                execution = self._convert_execution(
                    event.contract, raw_fields, commission=None, commission_currency=None
                )
            except Exception:
                return _malformed(kind, ["execution"], vars(raw_fields))
            # the converter coerces with int(); the identity must be exactly what was sent
            if (
                execution.client_id != ids["clientId"]
                or execution.broker_order_id != ids["orderId"]
                or (execution.permanent_id or 0) != ids["permId"]
            ):
                return _malformed(kind, ["identity"], vars(raw_fields))
        if not isinstance(execution, BrokerExecution) or not execution.execution_id.strip():
            return _malformed(kind, ["execution"], None)
        fingerprint = account_fingerprint(execution.account_id, key=self._persistence_key)
        facts = _execution_facts(execution, fingerprint)
        identity = {
            "kind": kind.value,
            "scope_fingerprint": fingerprint,
            "execution_id": execution.execution_id,
        }
        return _Normalized(kind, identity, facts, malformed=False)

    # ---------------------------------------------------------------- apply (one effect)

    def _apply(self, entry: JournalEntry) -> tuple[CallbackOutcome, dict[str, Any]] | None:
        if "malformed" in entry.facts:
            return CallbackOutcome.MALFORMED, {"fields": entry.facts.get("malformed", [])}
        if entry.kind is StreamKind.COMMISSION:
            return self._apply_commission(entry)
        if not entry.own_at_capture:
            return CallbackOutcome.NOT_OWN_AT_CAPTURE, {}
        if entry.kind is StreamKind.ORDER_STATUS:
            return self._apply_order_status(entry)
        return self._apply_execution(entry)

    def _waited(self, entry: JournalEntry, grace: timedelta) -> bool:
        return self._clock() - entry.received_at >= grace

    def _apply_order_status(
        self, entry: JournalEntry
    ) -> tuple[CallbackOutcome, dict[str, Any]] | None:
        facts = entry.facts
        if facts.get("scope_fingerprint") != self._fingerprint:
            return CallbackOutcome.CROSS_ACCOUNT, {}
        order_id = int(facts["order_id"])
        client_id = int(facts["client_id"])
        perm_id = int(facts["perm_id"])
        if _broker_id(order_id, minimum=_MIN_ORDER_ID) is None or not (
            _MIN_CLIENT_ID <= client_id <= _BROKER_ID_MAX and 1 <= perm_id <= _BROKER_ID_MAX
        ):
            return CallbackOutcome.IDENTITY_MISMATCH, {"reason": "identifier outside its domain"}
        candidates = self._intents_for_broker_order(order_id)
        if not candidates:
            return CallbackOutcome.UNKNOWN_INTENT, {}
        if len(candidates) > 1:
            return CallbackOutcome.AMBIGUOUS_INTENT, {"candidates": len(candidates)}
        intent_id = candidates[0]
        try:
            persisted = (
                self._tracker.single_broker_order_id(
                    intent_id, current_account_id=self._account_id
                ),
                self._tracker.client_id(intent_id, current_account_id=self._account_id),
                self._tracker.permanent_id(intent_id, current_account_id=self._account_id),
            )
        except OrderIdentityConflict as conflict:
            return CallbackOutcome.IDENTITY_CONFLICT, {"field": conflict.field}
        if persisted != (order_id, client_id, perm_id):
            fields = [
                name
                for name, stored, seen in zip(
                    ("broker_order_id", "client_id", "permanent_id"),
                    persisted,
                    (order_id, client_id, perm_id),
                    strict=True,
                )
                if stored != seen
            ]
            return CallbackOutcome.IDENTITY_MISMATCH, {"fields": fields}
        intent = self._tracker_intent_status(intent_id)
        if intent not in _INGESTIBLE:
            if not self._waited(entry, self._defer_grace):
                return None
            return CallbackOutcome.DEFERRED_TO_RECONCILER, {"intent_status": str(intent)}
        filled = Decimal(str(facts["filled"]))
        remaining = Decimal(str(facts["remaining"]))
        lifecycle = broker_status_to_lifecycle(
            str(facts["status"]), filled_quantity=filled, remaining_quantity=remaining
        )
        if lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN:
            # CB1-FIX1 F1 (D-43's rule: refuse to a typed unknown that names the cause): a status
            # the mapper does not know never moves a working order; the reconciler owns it
            return CallbackOutcome.UNRECOGNIZED_STATUS, {"status": str(facts["status"])}
        update = OrderStatusUpdate(
            intent_id=intent_id,
            broker_order_id=order_id,
            permanent_id=perm_id,
            client_id=client_id,
            lifecycle=lifecycle,
            filled_quantity=filled,
            remaining_quantity=remaining,
            source=CALLBACK_SOURCE,
            occurred_at=entry.received_at,
        )
        self._require_write_authority()
        try:
            result = self._tracker.ingest(update, current_account_id=self._account_id)
        except OrderLifecycleError:
            return CallbackOutcome.LIFECYCLE_CONTRADICTION, {"lifecycle": lifecycle.value}
        if result.lifecycle_changed:
            return CallbackOutcome.APPLIED, {"lifecycle": lifecycle.value}
        if result.identity_refined:
            return CallbackOutcome.IDENTITY_REFINED, {"lifecycle": lifecycle.value}
        if self._tracker_intent_status(intent_id) is not intent:
            # CB1-FIX1 F3: another writer moved the intent inside our CAS, so "no change" is not
            # what happened. Stay PENDING; the next sweep re-drives against the new status, and the
            # state machine decides. It re-pends only while other writers keep moving the intent.
            return None
        return CallbackOutcome.NO_CHANGE, {"lifecycle": lifecycle.value}

    def _tracker_intent_status(self, intent_id: str) -> OrderLifecycle | None:
        intent = self._intents.get(intent_id, current_account_id=self._account_id)
        return intent.status if intent is not None else None

    def _intents_for_broker_order(self, order_id: int) -> tuple[str, ...]:
        with self._sessions() as session:
            _require_matching_account_scope(session, self._account_id)
            return tuple(
                session.scalars(
                    select(OrderEventRow.intent_id)
                    .where(OrderEventRow.broker_order_id == order_id)
                    .distinct()
                    .order_by(OrderEventRow.intent_id)
                )
            )

    def _execution_of(self, entry: JournalEntry) -> BrokerExecution:
        facts = {
            name: value
            for name, value in entry.facts.items()
            if name not in ("kind", "scope_fingerprint")
        }
        return BrokerExecution.model_validate({**facts, "account_id": self._account_id})

    def _apply_execution(
        self, entry: JournalEntry
    ) -> tuple[CallbackOutcome, dict[str, Any]] | None:
        facts = entry.facts
        if facts.get("scope_fingerprint") != self._fingerprint:
            return CallbackOutcome.CROSS_ACCOUNT, {}
        execution = self._execution_of(entry)
        if execution.order_ref is None or execution.permanent_id is None:
            return CallbackOutcome.OWNERSHIP_UNPROVEN, {"reason": "no order ref or permId"}
        identity = VenueOrderIdentity(
            broker_order_id=execution.broker_order_id,
            order_ref=execution.order_ref,
            account_id=self._account_id,
            client_id=execution.client_id,
            permanent_id=execution.permanent_id,
        )
        if self._ownership.proves_owned(identity) is not True:
            return CallbackOutcome.OWNERSHIP_UNPROVEN, {}
        commission = self._commission_entry(execution.execution_id)
        if commission is None and not self._waited(entry, self._commission_grace):
            return None  # awaiting its commission report (journal-driven, restart-safe)
        if commission is not None:
            execution = execution.model_copy(
                update={
                    "commission": Decimal(str(commission.facts["amount"])),
                    "commission_currency": str(commission.facts["currency"]),
                }
            )
            execution = BrokerExecution.model_validate(execution.model_dump())
        self._require_write_authority()
        written = self._record_execution(execution)
        detail = {"commission": commission is not None}
        if written is ExecutionWriteOutcome.CONFLICT:
            return CallbackOutcome.EXECUTION_CONFLICT, detail
        if written is ExecutionWriteOutcome.REFUSED:
            return CallbackOutcome.EXECUTION_REFUSED, detail
        if commission is not None and self.journal.outcome(commission.key) is None:
            self._require_write_authority()
            self.journal.append_outcome(
                commission.key, StreamKind.COMMISSION, CallbackOutcome.PAIRED, {}
            )
        if written is ExecutionWriteOutcome.ALREADY_RECORDED:
            return CallbackOutcome.ALREADY_RECORDED, detail
        return CallbackOutcome.APPLIED, detail

    def _commission_entry(self, execution_id: str) -> JournalEntry | None:
        entry = self.journal.entry(
            event_key({"kind": StreamKind.COMMISSION.value, "execution_id": execution_id})
        )
        if entry is None or "malformed" in entry.facts:
            return None
        return entry

    def _apply_commission(
        self, entry: JournalEntry
    ) -> tuple[CallbackOutcome, dict[str, Any]] | None:
        execution_id = str(entry.facts["execution_id"])
        execution_key = event_key(
            {
                "kind": StreamKind.EXECUTION.value,
                "scope_fingerprint": self._fingerprint,
                "execution_id": execution_id,
            }
        )
        execution_entry = self.journal.entry(execution_key)
        if execution_entry is None:
            if not self._waited(entry, self._commission_grace):
                return None
            return CallbackOutcome.UNKNOWN_EXECUTION, {}
        execution_outcome = self.journal.outcome(execution_key)
        if execution_outcome is None:
            # its execution is PENDING: applying it pairs this commission and closes both
            self._apply_and_close(execution_entry, recovered=False)
            return None
        if execution_outcome not in (
            CallbackOutcome.APPLIED.value,
            CallbackOutcome.ALREADY_RECORDED.value,
        ):
            return CallbackOutcome.UNKNOWN_EXECUTION, {"execution_outcome": execution_outcome}
        # the execution was recorded before this commission arrived: the writer decides
        late = self._execution_of(execution_entry).model_copy(
            update={
                "commission": Decimal(str(entry.facts["amount"])),
                "commission_currency": str(entry.facts["currency"]),
            }
        )
        self._require_write_authority()
        written = self._record_execution(BrokerExecution.model_validate(late.model_dump()))
        if written is ExecutionWriteOutcome.ALREADY_RECORDED:
            return CallbackOutcome.PAIRED, {}
        _LOGGER.warning(
            "A late commission was refused by the fills writer; it stays in the journal",
            extra={"event": "callback_late_commission_refused", "outcome": written.value},
        )
        return CallbackOutcome.LATE_COMMISSION_REFUSED, {"writer": written.value}


__all__ = [
    "CALLBACK_CONFLICT",
    "CALLBACK_CONSUMER_HEALTH",
    "CALLBACK_JOURNAL",
    "CALLBACK_OUTCOME",
    "CALLBACK_SOURCE",
    "CONSUMER_FAILED_REASON",
    "DIVERGENT_REPLAY_REASON",
    "DROPPED_EVENT_REASON",
    "UNPROVEN_CALLBACK_REASON",
    "CallbackConsumer",
    "CallbackJournal",
    "CallbackOutcome",
    "ConsumerHealth",
    "ExecutionWriteOutcome",
    "JournalVerdict",
    "event_key",
]

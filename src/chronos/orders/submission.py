"""The single centralized order-submission boundary (M5 paper; M7 live branch).

This module contains the single ``transmit=True`` assignment in the live-Wheel
order path (``chronos.orders``); it happens exactly once, inside
:meth:`OrderSubmissionBoundary._cas_and_transmit`, and only after every
fail-closed gate of the selected branch has passed. (The dormant autonomous
``chronos.execution`` adapter has its own separate transmit site, but
``chronos.orders`` imports nothing from it and no production entrypoint wires
it, so it is unreachable from this path.)

PAPER branch gates (M5 plus generation-bound reconciliation readiness):

1. the single-writer lease is held (no read-only mode);
2. ``settings.transmission_possible`` — False in every demo/test/CI config;
3. the multi-condition mode lock grants PAPER_SUBMISSION (live is hard-denied
   there; the LIVE branch below never consults that lock);
4. the order's account matches the connected paper account;
5. a fresh broker status confirms the same healthy paper account and the shared
   reconciliation latch is RECONCILED for this connection generation;
6. the structured risk decision is APPROVED and unexpired;
7. a typed operator confirmation exists, is unexpired, and its hash matches the
   server-re-derived order+risk summary;
8. the intent is in exactly the USER_CONFIRMED state, re-verified by a true
   status CAS at the pre-submit transition.

LIVE branch (M7, ADR-0009): selected purely by frozen configuration
(``settings.ib_environment is LIVE``). All broker/DB evidence is gathered
FRESH inside :meth:`submit` (never trusted from startup bindings), the
TTL-sensitive gates evaluate against a fresh clock reading taken after the
slow I/O, the ten-gate live stack (``evaluate_live_gates``) must pass with the
kill-switch file read as the LAST piece of evidence, and the kill switch is
re-read once more between the CAS and the transmit line. A local, provably
not-sent refusal (``BrokerRefusedBeforeSend``, or that final kill-switch
re-read) resolves the intent to REJECTED synchronously; only a failure after
bytes may have left remains SUBMISSION_UNKNOWN for reconciliation — which is
never auto-retried.

Before the broker call the intent is marked SUBMISSION_UNKNOWN via a true CAS
(``enforce_from_status=True``): the event-key uniqueness refuses duplicate
submits, and the in-transaction status predicate refuses a submit whose intent
left USER_CONFIRMED while evidence was being gathered.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from chronos.broker.base import BrokerError, BrokerRefusedBeforeSend
from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.control.modes import ExecutionCapability, TradingMode, resolve_mode_lock
from chronos.domain.accounts import classify_observed_environment
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    IBEnvironment,
    OrderLifecycle,
    ProductFamily,
)
from chronos.domain.models import (
    ChronosModel,
    CryptoContract,
    MarketQuote,
    OptionContract,
    OrderSubmission,
)
from chronos.orders.arming import LiveArmingService
from chronos.orders.intent import WheelOrderIntent, order_summary_hash
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.live_gate import LiveGateInputs, evaluate_live_gates
from chronos.orders.live_mode import resolve_live_submission
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskDecision, OrderRiskEngine
from chronos.orders.session_drawdown import SessionDrawdownBreaker
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.time import utc_now

# Acceptable data-quality classes for the LIVE data gate. Frozen/stale/unknown
# always fail; DEMO never occurs on a live connection but is excluded anyway.
_LIVE_ACCEPTABLE_QUALITY = frozenset({DataQuality.LIVE, DataQuality.DELAYED})

_STOCK_MIN_TICK = Decimal("0.01")


class SubmissionRefusalCode(StrEnum):
    NOT_REFUSED = "NOT_REFUSED"
    READ_ONLY_LEASE = "READ_ONLY_LEASE"
    TRANSMISSION_NOT_POSSIBLE = "TRANSMISSION_NOT_POSSIBLE"
    MODE_FORBIDS = "MODE_FORBIDS"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    RISK_NOT_APPROVED = "RISK_NOT_APPROVED"
    RISK_EXPIRED = "RISK_EXPIRED"
    CONFIRMATION_MISSING = "CONFIRMATION_MISSING"
    RISK_EVIDENCE_STALE = "RISK_EVIDENCE_STALE"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    INTENT_NOT_CONFIRMED = "INTENT_NOT_CONFIRMED"
    BROKER_SUBMIT_FAILED = "BROKER_SUBMIT_FAILED"
    RECONCILIATION_NOT_READY = "RECONCILIATION_NOT_READY"
    # LIVE branch (ADR-0009).
    LIVE_DEPENDENCIES_MISSING = "LIVE_DEPENDENCIES_MISSING"
    LIVE_GRANT_DENIED = "LIVE_GRANT_DENIED"
    LIVE_GATE_BLOCKED = "LIVE_GATE_BLOCKED"
    BROKER_REFUSED_BEFORE_SEND = "BROKER_REFUSED_BEFORE_SEND"


class SubmissionOutcome(ChronosModel):
    submitted: bool
    refusal: SubmissionRefusalCode
    submission: OrderSubmission | None = None
    detail: str = ""


def _refuse(code: SubmissionRefusalCode, detail: str) -> SubmissionOutcome:
    return SubmissionOutcome(submitted=False, refusal=code, detail=detail)


def _not_sent_resolution(applied: bool) -> str:
    if applied:
        return "the intent was resolved to REJECTED"
    return "a newer persisted lifecycle was retained"


def _tick_conforms(limit_price: Decimal, min_tick: Decimal) -> bool:
    if min_tick <= 0:
        return False
    return limit_price % min_tick == 0


class OrderSubmissionBoundary:
    """Fail-closed gate chains culminating in the sole ``transmit=True`` site.

    The live dependencies default to ``None``; a boundary constructed without
    them refuses the LIVE branch entirely (missing machinery = fail closed).
    ``quote_reader`` returns a fresh :class:`MarketQuote` for an intent's
    contract (production wiring adapts :class:`MarketDataManager`); ``clock``
    supplies the fresh post-I/O timestamp for TTL gates (tests inject a
    controllable clock; production defaults to :func:`utc_now`).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        confirmations: OrderConfirmationRepository,
        tracker: OrderTrackerRepository,
        live_arming: LiveArmingService | None = None,
        live_kill_switch: LiveKillSwitch | None = None,
        session_drawdown: SessionDrawdownBreaker | None = None,
        quote_reader: Callable[[WheelOrderIntent], MarketQuote | None] | None = None,
        reconciliation_readiness: ReconciliationReadiness | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if settings.ib_environment is IBEnvironment.LIVE and (
            live_arming is None
            or live_kill_switch is None
            or session_drawdown is None
            or quote_reader is None
        ):
            # Fail LOUD at construction (M7 review finding F6): a live-environment
            # boundary missing its safety machinery must never exist — a silent
            # per-submit refusal would surface only at the first real order.
            raise RuntimeError(
                "a LIVE-environment submission boundary requires arming, kill "
                "switch, drawdown breaker, and quote reader at construction"
            )
        self._settings = settings
        self._connection = connection
        self._intents = intents
        self._confirmations = confirmations
        self._tracker = tracker
        self._live_arming = live_arming
        self._live_kill_switch = live_kill_switch
        self._session_drawdown = session_drawdown
        self._quote_reader = quote_reader
        self._reconciliation_readiness = reconciliation_readiness or ReconciliationReadiness()
        self._clock = clock or utc_now

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def submit(
        self,
        *,
        intent: WheelOrderIntent,
        risk_decision: OrderRiskDecision,
        connected_account_id: str,
        broker_environment_is_paper: bool,
        writer_lease_held: bool,
        now: datetime,
    ) -> SubmissionOutcome:
        # 1. Single-writer lease (both branches).
        if not writer_lease_held:
            return _refuse(
                SubmissionRefusalCode.READ_ONLY_LEASE,
                "backend is read-only; the single-writer lease is not held",
            )
        if self._settings.ib_environment is IBEnvironment.LIVE:
            return self._submit_live(intent=intent, risk_decision=risk_decision, now=now)
        return self._submit_paper(
            intent=intent,
            risk_decision=risk_decision,
            connected_account_id=connected_account_id,
            broker_environment_is_paper=broker_environment_is_paper,
            now=now,
        )

    # ------------------------------------------------------------------ #
    # PAPER branch (M5 semantics, plus the true CAS)
    # ------------------------------------------------------------------ #

    def _submit_paper(
        self,
        *,
        intent: WheelOrderIntent,
        risk_decision: OrderRiskDecision,
        connected_account_id: str,
        broker_environment_is_paper: bool,
        now: datetime,
    ) -> SubmissionOutcome:
        account_id = connected_account_id

        # 2. Configuration can even enter the paper transmission path.
        if not self._settings.transmission_possible:
            return _refuse(
                SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE,
                "settings.transmission_possible is False (not IBKR+PAPER+allow_order_transmit)",
            )

        # 3. Mode lock (hard-denies live; PAPER requires all conditions).
        lock = resolve_mode_lock(
            requested_mode=TradingMode.PAPER,
            paper_account_allowlist=self._settings.ib_account_allowlist,
            broker_reported_account_id=account_id,
            broker_reported_environment_is_paper=broker_environment_is_paper,
            order_transmission_enabled=self._settings.allow_order_transmit,
        )
        if not lock.may_submit_paper or lock.capability is not ExecutionCapability.PAPER_SUBMISSION:
            return _refuse(
                SubmissionRefusalCode.MODE_FORBIDS,
                "mode lock does not grant PAPER_SUBMISSION: " + "; ".join(lock.denial_reasons),
            )

        # 4. The order's account matches the connected paper account.
        if intent.account_id != account_id:
            return _refuse(
                SubmissionRefusalCode.ACCOUNT_MISMATCH,
                "order account does not match the connected paper account",
            )

        try:
            observed_status = self._connection.run(self._connection.broker.connection_status())
        except BrokerError as error:
            return _refuse(
                SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                f"paper broker status is unavailable; reconciliation invalidated: {error}",
            )
        if (
            not observed_status.connected
            or observed_status.state is not ConnectionState.CONNECTED
            or observed_status.account_id != account_id
        ):
            self._reconciliation_readiness.invalidate(
                "paper broker connection or account scope changed"
            )
            return _refuse(
                SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                "paper broker connection/account changed; run full reconciliation",
            )

        readiness = self._reconciliation_readiness.snapshot()
        if not readiness.ready:
            return _refuse(
                SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                f"reconciliation is {readiness.status.value}: {readiness.reason}",
            )
        reconciliation_generation = readiness.generation
        if (
            risk_decision.reconciliation_generation != reconciliation_generation
            or risk_decision.reconciliation_session_id != readiness.session_id
        ):
            return _refuse(
                SubmissionRefusalCode.RISK_EVIDENCE_STALE,
                "risk evidence does not belong to the current reconciliation "
                "session/generation; re-propose the order",
            )

        shared = self._shared_confirmation_gates(
            intent=intent, risk_decision=risk_decision, account_id=account_id, now=now
        )
        if shared is not None:
            return shared

        return self._cas_and_transmit(
            intent=intent,
            account_id=account_id,
            now=now,
            final_kill_check=False,
            branch_detail="paper order transmitted",
            expected_reconciliation_generation=reconciliation_generation,
        )

    # ------------------------------------------------------------------ #
    # LIVE branch (M7, ADR-0009)
    # ------------------------------------------------------------------ #

    def _submit_live(
        self,
        *,
        intent: WheelOrderIntent,
        risk_decision: OrderRiskDecision,
        now: datetime,
    ) -> SubmissionOutcome:
        del now  # LIVE TTL gates use a fresh post-I/O clock reading (ADR-0009 §4).

        # 0. Live machinery must exist: missing dependencies fail closed.
        if (
            self._live_arming is None
            or self._live_kill_switch is None
            or self._session_drawdown is None
            or self._quote_reader is None
        ):
            return _refuse(
                SubmissionRefusalCode.LIVE_DEPENDENCIES_MISSING,
                "live submission dependencies are not wired (arming/kill switch/"
                "drawdown/quote reader); refusing the LIVE branch entirely",
            )

        # 2. Configuration gate: the full ADR-0009 conjunction, re-derived.
        if not self._settings.live_transmission_possible:
            return _refuse(
                SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE,
                "settings.live_transmission_possible is False (the ADR-0009 live "
                "conjunction is not satisfied)",
            )

        # 3. Fresh broker evidence -> the orders-plane live grant.
        try:
            status = self._connection.run(self._connection.broker.connection_status())
        except BrokerError as error:
            return _refuse(
                SubmissionRefusalCode.LIVE_GRANT_DENIED,
                f"broker connection status unavailable; refusing live submission: {error}",
            )
        if not status.connected or status.state is not ConnectionState.CONNECTED:
            self._reconciliation_readiness.invalidate("live broker connection became unhealthy")
        observed_environment = classify_observed_environment(status.managed_accounts)
        grant = resolve_live_submission(
            order_transmission_enabled=self._settings.allow_order_transmit,
            live_trading_enabled=self._settings.allow_live_trading,
            live_account_allowlist=self._settings.ib_account_allowlist,
            broker_reported_account_id=status.account_id,
            broker_observed_environment=observed_environment,
        )
        if not grant.may_submit_live or grant.live_account_id is None:
            self._reconciliation_readiness.invalidate("live broker account scope changed")
            return _refuse(
                SubmissionRefusalCode.LIVE_GRANT_DENIED,
                "live submission grant denied: " + "; ".join(grant.denial_reasons),
            )
        account_id = grant.live_account_id

        # 4. Account match: intent == freshly observed connected account.
        if intent.account_id != account_id:
            return _refuse(
                SubmissionRefusalCode.ACCOUNT_MISMATCH,
                "order account does not match the broker-observed live account",
            )

        readiness = self._reconciliation_readiness.snapshot()
        if not readiness.ready:
            return _refuse(
                SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                f"reconciliation is {readiness.status.value}: {readiness.reason}",
            )
        reconciliation_generation = readiness.generation
        if (
            risk_decision.reconciliation_generation != reconciliation_generation
            or risk_decision.reconciliation_session_id != readiness.session_id
        ):
            return _refuse(
                SubmissionRefusalCode.RISK_EVIDENCE_STALE,
                "risk evidence does not belong to the current reconciliation "
                "session/generation; re-propose the order",
            )

        # 5. Remaining I/O evidence, gathered up front (ADR-0009 §4 step 5).
        connection_healthy = bool(status.connected)
        stuck_ids = self._intents.ids_in_status(
            OrderLifecycle.SUBMISSION_UNKNOWN, current_account_id=account_id
        )
        quote = self._read_quote(intent)
        nlv, nlv_error = self._read_net_liquidation()

        # 6. Fresh clock AFTER the slow I/O: TTL gates must not pass on stale time.
        fresh_now = self._clock()

        drawdown_breached, drawdown_reason = self._drawdown_evidence(
            nlv=nlv, nlv_error=nlv_error, now=fresh_now
        )

        # Data gate: fresh, acceptable-quality quote AND tick-valid limit price.
        data_ok, data_reason = self._data_evidence(intent, quote, fresh_now)

        # Risk / confirmation / arming evidence at the fresh clock.
        risk_ok = risk_decision.approved and not OrderRiskEngine.is_expired(
            risk_decision, now=fresh_now
        )
        confirmation_ok, confirmation_reason = self._confirmation_evidence(
            intent=intent, risk_decision=risk_decision, account_id=account_id, now=fresh_now
        )
        armed = self._live_arming.is_armed(now=fresh_now)

        # 7. Intent state + persisted preview evidence (re-fetched, not trusted).
        stored = self._intents.get(intent.intent_id, current_account_id=account_id)
        if stored is None or stored.status is not OrderLifecycle.USER_CONFIRMED:
            return _refuse(
                SubmissionRefusalCode.INTENT_NOT_CONFIRMED,
                "intent is not in the USER_CONFIRMED state; refusing to (re)submit",
            )
        preview_ok = stored.preview_id is not None

        # 8. The ten-gate walk — kill-switch file read LAST (ADR-0009 §4 step 8).
        kill_switch_engaged = self._live_kill_switch.is_engaged()
        reconciliation_current = self._reconciliation_readiness.is_reconciled(
            expected_generation=reconciliation_generation
        )
        if stuck_ids:
            reconciliation_reason = (
                "unresolved SUBMISSION_UNKNOWN intents block live submission: "
                + ", ".join(stuck_ids)
            )
        elif not reconciliation_current:
            reconciliation_reason = "reconciliation evidence changed during the live gate walk"
        else:
            reconciliation_reason = ""
        inputs = LiveGateInputs(
            config_live_enabled=True,
            connection_healthy=connection_healthy,
            reconciled=reconciliation_current and not stuck_ids,
            data_quality_ok=data_ok,
            risk_approved=risk_ok,
            preview_accepted=preview_ok,
            armed=armed,
            confirmation_valid=confirmation_ok,
            kill_switch_engaged=kill_switch_engaged,
            drawdown_breached=drawdown_breached,
            reconciliation_reason=reconciliation_reason,
            data_reason=data_reason,
            confirmation_reason=confirmation_reason,
            drawdown_reason=drawdown_reason,
        )
        decision = evaluate_live_gates(inputs)
        if not decision.all_passed:
            return _refuse(
                SubmissionRefusalCode.LIVE_GATE_BLOCKED,
                "live gate stack blocked submission: " + "; ".join(decision.blocking_reasons),
            )

        return self._cas_and_transmit(
            intent=intent,
            account_id=account_id,
            now=fresh_now,
            final_kill_check=True,
            branch_detail="live order transmitted",
            expected_reconciliation_generation=reconciliation_generation,
        )

    # ------------------------------------------------------------------ #
    # Shared gates and the single transmit site
    # ------------------------------------------------------------------ #

    def _shared_confirmation_gates(
        self,
        *,
        intent: WheelOrderIntent,
        risk_decision: OrderRiskDecision,
        account_id: str,
        now: datetime,
    ) -> SubmissionOutcome | None:
        """Gates 6-8 of the paper chain; returns a refusal or ``None`` to pass."""

        if not risk_decision.approved:
            return _refuse(
                SubmissionRefusalCode.RISK_NOT_APPROVED,
                "risk decision is not APPROVED",
            )
        if OrderRiskEngine.is_expired(risk_decision, now=now):
            return _refuse(SubmissionRefusalCode.RISK_EXPIRED, "risk decision has expired")

        confirmation_ok, reason = self._confirmation_evidence(
            intent=intent, risk_decision=risk_decision, account_id=account_id, now=now
        )
        if not confirmation_ok:
            code = {
                "missing": SubmissionRefusalCode.CONFIRMATION_MISSING,
                "expired": SubmissionRefusalCode.CONFIRMATION_EXPIRED,
            }.get(reason.split(":")[0], SubmissionRefusalCode.CONFIRMATION_MISMATCH)
            return _refuse(code, reason)

        stored = self._intents.get(intent.intent_id, current_account_id=account_id)
        if stored is None or stored.status is not OrderLifecycle.USER_CONFIRMED:
            return _refuse(
                SubmissionRefusalCode.INTENT_NOT_CONFIRMED,
                "intent is not in the USER_CONFIRMED state; refusing to (re)submit",
            )
        return None

    def _confirmation_evidence(
        self,
        *,
        intent: WheelOrderIntent,
        risk_decision: OrderRiskDecision,
        account_id: str,
        now: datetime,
    ) -> tuple[bool, str]:
        confirmation = self._confirmations.latest(intent.intent_id, current_account_id=account_id)
        if confirmation is None:
            return False, "missing: no typed confirmation recorded"
        if now >= confirmation.expires_at:
            return False, "expired: typed confirmation has expired"
        expected_hash = order_summary_hash(intent, risk_decision_id=risk_decision.decision_id)
        if confirmation.summary_hash != expected_hash:
            return False, (
                "mismatch: confirmation hash does not match the re-derived order/risk summary"
            )
        return True, ""

    def _read_quote(self, intent: WheelOrderIntent) -> MarketQuote | None:
        assert self._quote_reader is not None  # guarded by the dependency gate
        try:
            return self._quote_reader(intent)
        except Exception:
            return None

    def _read_net_liquidation(self) -> tuple[Decimal | None, str]:
        try:
            account = self._connection.run(self._connection.broker.account_summary())
        except BrokerError as error:
            return None, f"net liquidation unavailable: {error}"
        return account.net_liquidation, ""

    def _drawdown_evidence(
        self, *, nlv: Decimal | None, nlv_error: str, now: datetime
    ) -> tuple[bool, str]:
        """ADR-0009 §4: an unreadable NLV refuses WITHOUT the breaker side effects."""

        assert self._session_drawdown is not None  # guarded by the dependency gate
        if nlv is None:
            return True, (
                "session drawdown cannot be evaluated; refusing this submission "
                f"without engaging the kill switch ({nlv_error})"
            )
        decision = self._session_drawdown.check(nlv, now=now)
        return decision.breached, decision.reason

    def _data_evidence(
        self, intent: WheelOrderIntent, quote: MarketQuote | None, now: datetime
    ) -> tuple[bool, str]:
        if quote is None:
            return False, "no fresh quote available for the order's contract"
        age = now - quote.timestamp
        max_age = timedelta(seconds=self._settings.max_quote_age_seconds)
        if age > max_age:
            return False, f"quote is stale ({age.total_seconds():.1f}s old)"
        if quote.data_quality not in _LIVE_ACCEPTABLE_QUALITY:
            return False, f"quote data quality is not acceptable ({quote.data_quality.value})"
        if intent.product_family is ProductFamily.OPTION and isinstance(
            intent.contract, OptionContract
        ):
            min_tick = intent.contract.min_tick
        elif intent.product_family is ProductFamily.CRYPTO and isinstance(
            intent.contract, CryptoContract
        ):
            # Crypto tick comes ONLY from the qualified contract (ADR-0010 §6):
            # coins have venue-specific ticks, so the stock 0.01 default is wrong.
            # Absent metadata is UNKNOWN ⇒ fail closed, exactly like size.
            if intent.contract.min_tick is None:
                return False, (
                    "crypto contract is missing venue min-tick metadata; "
                    "cannot verify limit-price conformance"
                )
            min_tick = intent.contract.min_tick
        else:
            min_tick = _STOCK_MIN_TICK
        if not _tick_conforms(intent.limit_price, min_tick):
            return False, (
                f"limit price {intent.limit_price} does not conform to min tick {min_tick}"
            )
        return True, ""

    def _cas_and_transmit(
        self,
        *,
        intent: WheelOrderIntent,
        account_id: str,
        now: datetime,
        final_kill_check: bool,
        branch_detail: str,
        expected_reconciliation_generation: int,
    ) -> SubmissionOutcome:
        with self._reconciliation_readiness.submission_guard(
            expected_generation=expected_reconciliation_generation
        ) as acquired:
            if not acquired:
                return _refuse(
                    SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                    "reconciliation evidence changed before pre-submit persistence",
                )
            return self._cas_and_transmit_claimed(
                intent=intent,
                account_id=account_id,
                now=now,
                final_kill_check=final_kill_check,
                branch_detail=branch_detail,
                expected_reconciliation_generation=expected_reconciliation_generation,
            )

    def _cas_and_transmit_claimed(
        self,
        *,
        intent: WheelOrderIntent,
        account_id: str,
        now: datetime,
        final_kill_check: bool,
        branch_detail: str,
        expected_reconciliation_generation: int,
    ) -> SubmissionOutcome:
        # Pre-persist SUBMISSION_UNKNOWN so a crash mid-submit is recoverable.
        # TRUE CAS (ADR-0009 §4 step 9): the unique pre-submit event_key refuses
        # a duplicate/concurrent submit, and enforce_from_status refuses a
        # submit whose intent left USER_CONFIRMED while evidence was gathered.
        won_presubmit = self._tracker.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:presubmit:SUBMISSION_UNKNOWN",
            source="SUBMIT",
            from_status=OrderLifecycle.USER_CONFIRMED,
            to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            current_account_id=account_id,
            evidence={"phase": "pre_submit"},
            occurred_at=now,
            enforce_from_status=True,
        )
        if not won_presubmit:
            return _refuse(
                SubmissionRefusalCode.INTENT_NOT_CONFIRMED,
                "intent already left USER_CONFIRMED (a concurrent/duplicate submit "
                "or a status change won the pre-submit guard); refusing to transmit",
            )

        if not self._reconciliation_readiness.submission_claim_is_current(
            expected_generation=expected_reconciliation_generation
        ):
            resolved = self._resolve_not_sent(
                intent=intent,
                account_id=account_id,
                reason="reconciliation evidence changed between pre-submit and transmit",
                now=now,
            )
            return _refuse(
                SubmissionRefusalCode.RECONCILIATION_NOT_READY,
                "reconciliation evidence changed between pre-submit and transmit; "
                f"nothing was sent and {_not_sent_resolution(resolved)}",
            )

        # Final kill-switch re-read (LIVE only): a local file read between the
        # CAS and the transmit line. A refusal here is provably not-sent, so the
        # intent is resolved to REJECTED synchronously (ADR-0009 §4 step 10).
        if final_kill_check:
            assert self._live_kill_switch is not None  # guarded by the dependency gate
            if self._live_kill_switch.is_engaged():
                resolved = self._resolve_not_sent(
                    intent=intent,
                    account_id=account_id,
                    reason="kill switch engaged between pre-submit and transmit",
                    now=now,
                )
                return _refuse(
                    SubmissionRefusalCode.LIVE_GATE_BLOCKED,
                    "kill switch engaged between pre-submit and transmit; "
                    f"nothing was sent and {_not_sent_resolution(resolved)}",
                )

        # --- THE SINGLE transmit=True ASSIGNMENT IN chronos.orders -----------
        request = intent.to_order_request(transmit=True)
        try:
            submission = self._connection.run(
                self._connection.broker.submit_order(
                    request, send_guard=self._reconciliation_readiness
                )
            )
        except BrokerRefusedBeforeSend as error:
            # The adapter refused locally before any network send: the venue
            # provably never saw the order (ADR-0009 §6).
            resolved = self._resolve_not_sent(
                intent=intent,
                account_id=account_id,
                reason=f"broker adapter refused before send: {error}",
                now=now,
            )
            return _refuse(
                SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND,
                f"broker adapter refused before any network send: {error}; "
                f"{_not_sent_resolution(resolved)} (nothing reached the venue)",
            )
        except BrokerError as error:
            # Bytes may have left: leave SUBMISSION_UNKNOWN for reconciliation;
            # never auto-retry from here.
            return _refuse(
                SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
                f"broker refused or failed the submission: {error}",
            )

        transition_applied = self._tracker.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:{submission.broker_order_id}:{submission.lifecycle.value}",
            source="SUBMIT",
            from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            to_status=submission.lifecycle,
            current_account_id=account_id,
            broker_order_id=submission.broker_order_id,
            evidence={
                "permanent_id": submission.permanent_id,
                "client_id": submission.client_id,
                "broker_lifecycle": submission.lifecycle.value,
            },
            occurred_at=now,
            enforce_from_status=True,
        )
        persistence_detail = branch_detail
        if not transition_applied:
            durable_intent = self._intents.get(
                intent.intent_id,
                current_account_id=account_id,
            )
            if durable_intent is None:
                return SubmissionOutcome(
                    submitted=False,
                    refusal=SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
                    submission=submission,
                    detail=(
                        "broker send completed but its acknowledgement could not be "
                        "persisted because the order intent disappeared"
                    ),
                )
            submission = submission.model_copy(update={"lifecycle": durable_intent.status})
            persistence_detail = (
                "broker send completed; a newer persisted lifecycle "
                f"{durable_intent.status.value} won the acknowledgement CAS"
            )
        if submission.lifecycle not in {
            OrderLifecycle.SUBMITTED,
            OrderLifecycle.PARTIALLY_FILLED,
            OrderLifecycle.FILLED,
        }:
            failure_detail = persistence_detail
            if transition_applied:
                failure_detail = (
                    f"broker acknowledged the send as {submission.lifecycle.value}; "
                    "the known broker identity was persisted without pretending it is active"
                )
            return SubmissionOutcome(
                submitted=False,
                refusal=SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
                submission=submission,
                detail=failure_detail,
            )

        return SubmissionOutcome(
            submitted=True,
            refusal=SubmissionRefusalCode.NOT_REFUSED,
            submission=submission,
            detail=persistence_detail,
        )

    def _resolve_not_sent(
        self, *, intent: WheelOrderIntent, account_id: str, reason: str, now: datetime
    ) -> bool:
        return self._tracker.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:presend_refusal:REJECTED",
            source="SUBMIT",
            from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            to_status=OrderLifecycle.REJECTED,
            current_account_id=account_id,
            evidence={"phase": "refused_before_send", "reason": reason},
            occurred_at=now,
            enforce_from_status=True,
        )

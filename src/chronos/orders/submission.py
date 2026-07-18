"""The single centralized paper order-submission boundary (Milestone 5).

This module contains the ONLY assignment of ``transmit=True`` in the entire
codebase, and it happens exactly once, inside
:meth:`PaperOrderSubmissionBoundary.submit`, and only after every fail-closed
gate below has passed:

1. the single-writer lease is held (no read-only mode);
2. ``settings.transmission_possible`` (IBKR + PAPER + allow_order_transmit +
   not allow_live_trading + account set) — False in every demo/test/CI config;
3. the multi-condition mode lock grants PAPER_SUBMISSION (live is hard-denied);
4. the order's account matches the connected paper account;
5. the structured risk decision is APPROVED and unexpired;
6. a typed operator confirmation exists, is unexpired, and its hash matches the
   server-re-derived order+risk summary;
7. the intent is in exactly the USER_CONFIRMED state (idempotency / no replay).

M5 is paper-only: there is no live branch here. The mode lock hard-denies live
and ``transmission_possible`` requires PAPER, so a live account can never reach
the ``transmit=True`` assignment. The live gate stack (arming, the kill switch,
the eight formal gates) is layered on in Milestone 6.

Before the broker call the intent is marked SUBMISSION_UNKNOWN, so a crash or
timeout mid-submit leaves a recoverable state that reconciliation resolves by
``order_ref`` — never an auto-retry.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from chronos.broker.base import BrokerError
from chronos.broker.connection import BrokerConnectionManager
from chronos.config.settings import Settings
from chronos.control.modes import ExecutionCapability, TradingMode, resolve_mode_lock
from chronos.domain.enums import OrderLifecycle
from chronos.domain.models import ChronosModel, OrderSubmission
from chronos.orders.intent import WheelOrderIntent, order_summary_hash
from chronos.orders.risk import OrderRiskDecision, OrderRiskEngine
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
)


class SubmissionRefusalCode(StrEnum):
    NOT_REFUSED = "NOT_REFUSED"
    READ_ONLY_LEASE = "READ_ONLY_LEASE"
    TRANSMISSION_NOT_POSSIBLE = "TRANSMISSION_NOT_POSSIBLE"
    MODE_FORBIDS = "MODE_FORBIDS"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    RISK_NOT_APPROVED = "RISK_NOT_APPROVED"
    RISK_EXPIRED = "RISK_EXPIRED"
    CONFIRMATION_MISSING = "CONFIRMATION_MISSING"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    INTENT_NOT_CONFIRMED = "INTENT_NOT_CONFIRMED"
    BROKER_SUBMIT_FAILED = "BROKER_SUBMIT_FAILED"


class SubmissionOutcome(ChronosModel):
    submitted: bool
    refusal: SubmissionRefusalCode
    submission: OrderSubmission | None = None
    detail: str = ""


def _refuse(code: SubmissionRefusalCode, detail: str) -> SubmissionOutcome:
    return SubmissionOutcome(submitted=False, refusal=code, detail=detail)


class PaperOrderSubmissionBoundary:
    """Fail-closed gate chain culminating in the sole ``transmit=True`` site."""

    def __init__(
        self,
        *,
        settings: Settings,
        connection: BrokerConnectionManager,
        intents: OrderIntentRepository,
        confirmations: OrderConfirmationRepository,
        tracker: OrderTrackerRepository,
    ) -> None:
        self._settings = settings
        self._connection = connection
        self._intents = intents
        self._confirmations = confirmations
        self._tracker = tracker

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
        account_id = connected_account_id

        # 1. Single-writer lease.
        if not writer_lease_held:
            return _refuse(
                SubmissionRefusalCode.READ_ONLY_LEASE,
                "backend is read-only; the single-writer lease is not held",
            )

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

        # 5. Structured risk decision approved and fresh.
        if not risk_decision.approved:
            return _refuse(
                SubmissionRefusalCode.RISK_NOT_APPROVED,
                "risk decision is not APPROVED",
            )
        if OrderRiskEngine.is_expired(risk_decision, now=now):
            return _refuse(SubmissionRefusalCode.RISK_EXPIRED, "risk decision has expired")

        # 6. Typed operator confirmation: present, fresh, and hash-matched.
        confirmation = self._confirmations.latest(intent.intent_id, current_account_id=account_id)
        if confirmation is None:
            return _refuse(
                SubmissionRefusalCode.CONFIRMATION_MISSING, "no typed confirmation recorded"
            )
        if now >= confirmation.expires_at:
            return _refuse(
                SubmissionRefusalCode.CONFIRMATION_EXPIRED, "typed confirmation has expired"
            )
        expected_hash = order_summary_hash(intent, risk_decision_id=risk_decision.decision_id)
        if confirmation.summary_hash != expected_hash:
            return _refuse(
                SubmissionRefusalCode.CONFIRMATION_MISMATCH,
                "confirmation hash does not match the re-derived order/risk summary",
            )

        # 7. Idempotency: the intent must be exactly USER_CONFIRMED (not already
        #    submitting/submitted, not reverted).
        stored = self._intents.get(intent.intent_id, current_account_id=account_id)
        if stored is None or stored.status is not OrderLifecycle.USER_CONFIRMED:
            return _refuse(
                SubmissionRefusalCode.INTENT_NOT_CONFIRMED,
                "intent is not in the USER_CONFIRMED state; refusing to (re)submit",
            )

        # Pre-persist SUBMISSION_UNKNOWN so a crash mid-submit is recoverable.
        self._tracker.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:presubmit:SUBMISSION_UNKNOWN",
            source="SUBMIT",
            from_status=OrderLifecycle.USER_CONFIRMED,
            to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            current_account_id=account_id,
            evidence={"phase": "pre_submit"},
            occurred_at=now,
        )

        # --- THE SINGLE transmit=True ASSIGNMENT IN THE ENTIRE CODEBASE ------
        request = intent.to_order_request(transmit=True)
        try:
            submission = self._connection.run(self._connection.broker.submit_order(request))
        except BrokerError as error:
            # Leave the intent at SUBMISSION_UNKNOWN for reconciliation; never
            # auto-retry from here.
            return _refuse(
                SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
                f"broker refused or failed the submission: {error}",
            )

        self._tracker.record_transition(
            intent_id=intent.intent_id,
            event_key=f"{intent.intent_id}:{submission.broker_order_id}:SUBMITTED",
            source="SUBMIT",
            from_status=OrderLifecycle.SUBMISSION_UNKNOWN,
            to_status=OrderLifecycle.SUBMITTED,
            current_account_id=account_id,
            broker_order_id=submission.broker_order_id,
            evidence={"permanent_id": submission.permanent_id},
            occurred_at=now,
        )
        return SubmissionOutcome(
            submitted=True,
            refusal=SubmissionRefusalCode.NOT_REFUSED,
            submission=submission,
            detail="paper order transmitted",
        )

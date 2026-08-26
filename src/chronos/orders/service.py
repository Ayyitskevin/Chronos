"""OrderManagementService: the human-in-the-loop order pipeline facade.

Orchestrates the full lineage — propose (build intent + structured risk) ->
preview (broker what-if) -> confirm (typed operator confirmation) -> submit (the
single transmit boundary) -> track — plus modify/cancel/list/get and restart
reconciliation. Every stage re-derives from fresh, server-side evidence and
persists its result; the client never carries an authorization token across the
stateless HTTP round-trips (the confirmation hash is recomputed at submit time).

Broker-integration evidence gathering is injected as a
:class:`RiskEvidenceProvider` so the orchestration is fully testable with a
canned provider while the concrete broker-reading provider is owner-verified in
paper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from chronos.config.settings import Settings
from chronos.domain.enums import IBEnvironment, OrderLifecycle
from chronos.domain.models import CancellationResult, OrderSubmission
from chronos.orders.intent import WheelOrderIntent, order_summary_hash
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewResult, OrderPreviewService
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    OrderRestartReconciliationReport,
)
from chronos.orders.risk import (
    OrderRiskDecision,
    OrderRiskEngine,
    RiskEvidence,
    order_risk_decision_from_record,
)
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionOutcome
from chronos.orders.tracker import OrderTracker
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskCheckRecord,
    RiskDecisionRecord,
    RiskDecisionRepository,
)
from chronos.utils.time import utc_now


class RiskEvidenceProvider(Protocol):
    """Gathers fresh risk evidence for one intent (reads the broker + repos)."""

    def gather(self, intent: WheelOrderIntent, *, now: datetime) -> RiskEvidence: ...


class OrderPipelineError(RuntimeError):
    """A pipeline stage was invoked on an order in the wrong state."""


@dataclass(frozen=True, slots=True)
class ProposeResult:
    intent: OrderIntentRecord
    risk: OrderRiskDecision


class OrderManagementService:
    def __init__(
        self,
        *,
        settings: Settings,
        environment: IBEnvironment,
        account_id: str,
        evidence_provider: RiskEvidenceProvider,
        risk_engine: OrderRiskEngine,
        preview_service: OrderPreviewService,
        submission_boundary: OrderSubmissionBoundary,
        modification: OrderModificationService,
        cancellation: OrderCancellationService,
        tracker: OrderTracker,
        tracker_repo: OrderTrackerRepository,
        reconciler: OrderRestartReconciler,
        intents: OrderIntentRepository,
        confirmations: OrderConfirmationRepository,
        risk_decisions: RiskDecisionRepository,
        broker_environment_is_paper: bool,
    ) -> None:
        self._settings = settings
        self._environment = environment
        self._account_id = account_id
        self._evidence = evidence_provider
        self._risk_engine = risk_engine
        self._preview = preview_service
        self._boundary = submission_boundary
        #: The single submission boundary this service drives. Exposed so the
        #: backend lifespan can bind the live single-writer verifier (R-24)
        #: after it acquires the lease, which happens later than runtime build.
        self.submission_boundary = submission_boundary
        self._modification = modification
        self._cancellation = cancellation
        self._tracker = tracker
        self._tracker_repo = tracker_repo
        self._reconciler = reconciler
        self._intents = intents
        self._confirmations = confirmations
        self._risk_decisions = risk_decisions
        self._broker_environment_is_paper = broker_environment_is_paper
        # Proposed intents held in-process so the stateless HTTP stages
        # (preview/confirm/submit) can re-address them by id without trusting a
        # client-supplied contract. Short-lived; a restart simply requires the
        # operator to re-propose (proposals expire anyway).
        self._proposed: dict[str, WheelOrderIntent] = {}

    @property
    def account_id(self) -> str:
        """The connected account this service is bound to (raw; not persisted)."""

        return self._account_id

    # --- stage 1: propose --------------------------------------------------

    def propose(self, intent: WheelOrderIntent, *, now: datetime | None = None) -> ProposeResult:
        moment = now or utc_now()
        day_bucket = self._day_bucket(moment)
        record = intent.to_intent_record(
            environment=self._environment,
            status=OrderLifecycle.DRAFT,
            created_at=moment,
            expires_at=moment + timedelta(seconds=self._settings.order_confirmation_ttl_seconds),
            day_bucket=day_bucket,
        )
        self._intents.create(record, current_account_id=self._account_id)

        evidence = self._evidence.gather(intent, now=moment)
        decision = self._risk_engine.evaluate(intent, evidence, now=moment)
        self._risk_decisions.store(
            _decision_to_record(decision, intent=intent, evidence=evidence),
            current_account_id=self._account_id,
        )

        next_status = OrderLifecycle.VALIDATED if decision.approved else OrderLifecycle.REJECTED
        self._intents.set_status(
            intent.intent_id,
            next_status,
            current_account_id=self._account_id,
            at=moment,
            risk_snapshot_id=decision.decision_id,
        )
        stored = self._require_intent(intent.intent_id)
        self._proposed[intent.intent_id] = intent
        return ProposeResult(intent=stored, risk=decision)

    def _proposed_intent(self, intent_id: str) -> WheelOrderIntent:
        intent = self._proposed.get(intent_id)
        if intent is None:
            raise OrderPipelineError(
                "no in-flight proposal for this order; re-propose before previewing/submitting"
            )
        return intent

    def preview_by_id(self, intent_id: str, *, now: datetime | None = None) -> OrderPreviewResult:
        return self.preview(self._proposed_intent(intent_id), now=now)

    def confirm_by_id(
        self,
        intent_id: str,
        *,
        risk_decision_id: str,
        ui_session_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        return self.confirm(
            self._proposed_intent(intent_id),
            risk_decision_id=risk_decision_id,
            ui_session_id=ui_session_id,
            now=now,
        )

    def submit_by_id(
        self, intent_id: str, *, writer_lease_held: bool, now: datetime | None = None
    ) -> SubmissionOutcome:
        return self.submit(
            self._proposed_intent(intent_id), writer_lease_held=writer_lease_held, now=now
        )

    # --- stage 2: preview --------------------------------------------------

    def preview(
        self, intent: WheelOrderIntent, *, now: datetime | None = None
    ) -> OrderPreviewResult:
        moment = now or utc_now()
        stored = self._require_intent(intent.intent_id)
        if stored.status is not OrderLifecycle.VALIDATED:
            raise OrderPipelineError("order must be VALIDATED before preview")
        result = self._preview.preview(intent)
        if not result.accepted:
            # ADR-0009 §5: a declined what-if must NOT advance the lifecycle —
            # the intent stays VALIDATED and the refusal is durably recorded,
            # so no downstream gate can mistake "previewed" for "accepted".
            self._tracker_repo.record_transition(
                intent_id=intent.intent_id,
                event_key=f"{intent.intent_id}:preview_declined:{moment.isoformat()}",
                source="PREVIEW",
                from_status=OrderLifecycle.VALIDATED,
                to_status=OrderLifecycle.VALIDATED,
                current_account_id=self._account_id,
                evidence={"accepted": False, "warnings": list(result.warnings)},
                occurred_at=moment,
            )
            return result
        preview_id = f"PRV-{intent.intent_id}-{result.previewed_at.isoformat()}"
        self._intents.set_status(
            intent.intent_id,
            OrderLifecycle.WHAT_IF_PREVIEWED,
            current_account_id=self._account_id,
            at=moment,
            preview_id=preview_id,
        )
        return result

    # --- stage 3: confirm --------------------------------------------------

    def confirm(
        self,
        intent: WheelOrderIntent,
        *,
        risk_decision_id: str,
        ui_session_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Record a typed confirmation and advance to USER_CONFIRMED.

        Returns the server-derived summary hash that the submit gate will
        recompute and require to match.
        """

        moment = now or utc_now()
        stored = self._require_intent(intent.intent_id)
        if stored.status is not OrderLifecycle.WHAT_IF_PREVIEWED:
            raise OrderPipelineError("order must be previewed before confirmation")
        if stored.risk_snapshot_id != risk_decision_id:
            raise OrderPipelineError("confirmation references a stale or unknown risk decision")
        summary_hash = order_summary_hash(intent, risk_decision_id=risk_decision_id)
        self._confirmations.record(
            intent_id=intent.intent_id,
            summary_hash=summary_hash,
            current_account_id=self._account_id,
            ui_session_id=ui_session_id,
            quote_snapshot_id=None,
            risk_snapshot_id=risk_decision_id,
            confirmed_at=moment,
            expires_at=moment + timedelta(seconds=self._settings.order_confirmation_ttl_seconds),
        )
        self._intents.set_status(
            intent.intent_id,
            OrderLifecycle.USER_CONFIRMED,
            current_account_id=self._account_id,
            at=moment,
            confirmation_hash=summary_hash,
        )
        return summary_hash

    # --- stage 4: submit ---------------------------------------------------

    def submit(
        self,
        intent: WheelOrderIntent,
        *,
        writer_lease_held: bool,
        now: datetime | None = None,
    ) -> SubmissionOutcome:
        moment = now or utc_now()
        stored = self._require_intent(intent.intent_id)
        if stored.risk_snapshot_id is None:
            raise OrderPipelineError("order has no risk decision to submit against")
        decision = self._load_decision(stored.risk_snapshot_id)
        return self._boundary.submit(
            intent=intent,
            risk_decision=decision,
            connected_account_id=self._account_id,
            broker_environment_is_paper=self._broker_environment_is_paper,
            writer_lease_held=writer_lease_held,
            now=moment,
        )

    # --- mutations & queries ----------------------------------------------

    def modify(
        self, intent_id: str, new_limit_price: Decimal, *, now: datetime | None = None
    ) -> OrderSubmission:
        moment = now or utc_now()
        return self._modification.modify(
            intent_id,
            new_limit_price,
            current_account_id=self._account_id,
            now=moment,
        )

    def cancel(self, intent_id: str, *, now: datetime | None = None) -> CancellationResult:
        moment = now or utc_now()
        return self._cancellation.cancel(intent_id, current_account_id=self._account_id, now=moment)

    def resolve_submission_unknown(
        self, intent_id: str, *, operator_note: str, now: datetime | None = None
    ) -> bool:
        """Run an audited evidence refresh for a SUBMISSION_UNKNOWN intent.

        Positive broker evidence is applied and returns ``False``. Broker
        absence raises without writing a terminal lifecycle; it cannot safely
        prove rejection (ADR-0009 §6).
        """

        moment = now or utc_now()
        return self._reconciler.operator_resolve(
            intent_id,
            operator_note=operator_note,
            current_account_id=self._account_id,
            now=moment,
        )

    def reconcile_on_restart(self, *, now: datetime | None = None) -> int:
        moment = now or utc_now()
        return len(self._reconciler.reconcile(current_account_id=self._account_id, now=moment))

    def reconcile_on_restart_report(
        self, *, now: datetime | None = None
    ) -> OrderRestartReconciliationReport:
        moment = now or utc_now()
        return self._reconciler.reconcile_report(current_account_id=self._account_id, now=moment)

    def get(self, intent_id: str) -> OrderIntentRecord | None:
        return self._intents.get(intent_id, current_account_id=self._account_id)

    def list_orders(self) -> tuple[OrderIntentRecord, ...]:
        return self._intents.list_all(current_account_id=self._account_id)

    # --- helpers -----------------------------------------------------------

    def _require_intent(self, intent_id: str) -> OrderIntentRecord:
        stored = self._intents.get(intent_id, current_account_id=self._account_id)
        if stored is None:
            raise OrderPipelineError(f"unknown order intent {intent_id!r}")
        return stored

    def _load_decision(self, decision_id: str) -> OrderRiskDecision:
        record = self._risk_decisions.get(decision_id, current_account_id=self._account_id)
        if record is None:
            raise OrderPipelineError("risk decision could not be reloaded")
        stored_generation = record.evidence.get("reconciliation_generation")
        stored_session_id = record.evidence.get("reconciliation_session_id")
        reconciliation_generation = (
            stored_generation if type(stored_generation) is int and stored_generation >= 0 else None
        )
        reconciliation_session_id = (
            stored_session_id
            if (
                type(stored_session_id) is str
                and bool(stored_session_id)
                and stored_session_id.strip() == stored_session_id
            )
            else None
        )
        return order_risk_decision_from_record(
            tuple((c.check_name, c.status, c.detail) for c in record.checks),
            decision_id=record.decision_id,
            overall=record.overall_result,
            decided_at=record.decided_at,
            expires_at=record.expires_at,
            reconciliation_generation=reconciliation_generation,
            reconciliation_session_id=reconciliation_session_id,
        )

    def _day_bucket(self, moment: datetime) -> str:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(self._settings.market_timezone)).date().isoformat()


def _decision_to_record(
    decision: OrderRiskDecision,
    *,
    intent: WheelOrderIntent,
    evidence: RiskEvidence,
) -> RiskDecisionRecord:
    from chronos.utils.identifiers import account_fingerprint

    decision_evidence: dict[str, object] = {
        "reconciliation_generation": decision.reconciliation_generation,
        "reconciliation_session_id": decision.reconciliation_session_id,
    }
    if evidence.qqq_position_management is not None:
        decision_evidence["qqq_position_management"] = evidence.qqq_position_management.model_dump(
            mode="json"
        )
    checks = tuple(
        RiskCheckRecord(
            sequence=index,
            check_name=check.name,
            status=check.status,
            detail=check.detail,
            evidence={},
        )
        for index, check in enumerate(decision.checks)
    )
    return RiskDecisionRecord(
        decision_id=decision.decision_id,
        correlation_id=intent.correlation_id,
        account_fingerprint=account_fingerprint(intent.account_id),
        symbol=intent.symbol,
        product_family=intent.product_family,
        overall_result=decision.overall,
        decided_at=decision.decided_at,
        expires_at=decision.expires_at,
        evidence=decision_evidence,
        checks=checks,
    )

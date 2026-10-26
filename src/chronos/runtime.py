"""Process-lifetime application runtime, shared by the backend and the UI.

Extracted from ``chronos.ui.session`` (Milestone 1 of the live Wheel plan) so
the FastAPI backend can own the identical wiring without importing Streamlit.
``chronos.ui.session`` now wraps :func:`build_runtime` with Streamlit's
resource cache; the backend calls it directly, exactly once, at startup.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.engine import make_url

from chronos.broker.base import Broker
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker, demo_now
from chronos.broker.market_data import MarketDataManager
from chronos.config.persistence_key import resolve_persistence_key
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    ConnectionState,
    IBEnvironment,
    ProductFamily,
    ReconciliationStatus,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    ConnectionStatus,
    CryptoContract,
    MarketQuote,
    OptionContract,
    UnderlyingContract,
)
from chronos.orders.arming import LiveArmingService
from chronos.orders.callback_consumer import (
    CallbackConsumer,
    ConsumerHealth,
    ExecutionWriteOutcome,
)
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.live_audit import KillSwitchEventRepository, LiveArmEventRepository
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.ownership import PersistedOrderOwnershipLedger
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_readiness import (
    ReconciliationReadiness,
    ReconciliationReadinessSnapshot,
)
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    OrderRestartReconciliationReport,
)
from chronos.orders.risk import OrderRiskEngine
from chronos.orders.service import OrderManagementService
from chronos.orders.session_drawdown import SessionDrawdownBreaker
from chronos.orders.submission import OrderSubmissionBoundary
from chronos.orders.tracker import OrderTracker
from chronos.persistence import execution_repository as fills_writer
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import ExecutionRepository
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.persistence.reconciliation_repository import (
    ReconciliationRunRecorder,
    ReconciliationSnapshot,
    ReconciliationTrigger,
)
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.services.reconciliation import (
    ReconciliationCoordinator,
    ReconciliationResult,
)
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.services.short_put_demo_approval import ShortPutDemoApprovalService
from chronos.services.short_put_demo_what_if import ShortPutDemoWhatIfService
from chronos.services.short_put_risk_preview import ShortPutRiskPreviewService
from chronos.ui.runtime_scope import RuntimeScopeView, build_bound_runtime_scope
from chronos.utils.identifiers import PersistenceKey, account_fingerprint
from chronos.utils.logging import configure_logging
from chronos.utils.time import utc_now


class _CallbackSource(Protocol):
    """CB-1: where unsolicited callbacks come from (the official bridge, the Demo seam)."""

    def set_stream_sink(self, sink: Callable[[Any], object] | None) -> None: ...


def _file_backed(url: str) -> bool:
    parsed = make_url(url)
    return parsed.get_backend_name() != "sqlite" or parsed.database not in (None, "", ":memory:")


def _callback_execution_writer(
    repository: ExecutionRepository,
) -> Callable[[BrokerExecution], ExecutionWriteOutcome]:
    """CB-1: the consumer's write-only seam to the fills writer (BP-1's authority line).

    The consumer lives in ``chronos.orders``, which never imports the writer; it gets this
    callable instead. It records and never reads a row back. The writer's typed refusals map to
    the consumer's outcome enum the way the coordinator's ``_record_execution`` maps them.
    """

    def record(execution: BrokerExecution) -> ExecutionWriteOutcome:
        try:
            correlation_id = repository.linked_correlation_id(execution.order_ref)
            written = repository.record(execution, correlation_id=correlation_id)
        except fills_writer.ExecutionConflict:
            return ExecutionWriteOutcome.CONFLICT
        except (fills_writer.UnknownCorrelation, ValueError):
            return ExecutionWriteOutcome.REFUSED
        return ExecutionWriteOutcome.RECORDED if written else ExecutionWriteOutcome.ALREADY_RECORDED

    return record


@dataclass(frozen=True, slots=True)
class SubmissionReconciliationReport:
    """Composite evidence used to publish order-submission readiness."""

    restart: OrderRestartReconciliationReport
    portfolio: ReconciliationResult
    readiness: ReconciliationReadinessSnapshot


@dataclass(slots=True)
class AppRuntime:
    settings: Settings
    runtime_scope: RuntimeScopeView
    broker: Broker
    connection: BrokerConnectionManager
    market_data: MarketDataManager
    database: Database
    reconciliation: ReconciliationCoordinator
    short_put_candidates: ShortPutCandidateService
    reconciliation_readiness: ReconciliationReadiness
    short_put_risk_preview: ShortPutRiskPreviewService
    short_put_demo_what_if: ShortPutDemoWhatIfService
    short_put_demo_approval: ShortPutDemoApprovalService
    order_management: OrderManagementService
    live_arming: LiveArmingService
    live_kill_switch: LiveKillSwitch
    session_drawdown: SessionDrawdownBreaker
    # N3: the install's persistence key, resolved once by build_runtime before anything was
    # created; every fingerprint this runtime computes is keyed by it.
    persistence_key: PersistenceKey
    # BP-3: one reconciliation_runs row per decided pass, written AFTER the readiness latch
    # published; evidence only (K1 (a) append-only, K2 (a) masked id + fingerprint). Optional
    # so a runtime built without a database seam records nothing.
    reconciliation_runs: ReconciliationRunRecorder | None = None
    # CB-1: the supervised callback consumer and the stream it drains (the official adapter's
    # bridge or the Demo seam). Built for every runtime; started only by a writer's lifespan.
    callback_consumer: CallbackConsumer | None = None
    callback_source: _CallbackSource | None = None

    def start_callback_consumer(self, *, is_writer: Callable[[], bool]) -> bool:
        """Start draining unsolicited callbacks (writer only, after the startup reconciliation).

        Refused on an in-memory database: its one ``StaticPool`` connection is shared across
        threads, so a second thread's commit would adopt another session's pending writes.
        """

        consumer = getattr(self, "callback_consumer", None)
        source = getattr(self, "callback_source", None)
        if consumer is None or source is None:
            return False
        if not _file_backed(self.database.url):
            logging.getLogger("chronos.runtime").warning(
                "Callback consumer not started on an in-memory database",
                extra={"event": "callback_consumer_not_started"},
            )
            return False
        consumer.start(is_writer=is_writer)
        source.set_stream_sink(consumer.offer)
        return True

    def callback_consumer_health(self) -> ConsumerHealth | None:
        consumer = getattr(self, "callback_consumer", None)
        return consumer.health() if consumer is not None else None

    def _stop_callback_consumer(self) -> None:
        source = getattr(self, "callback_source", None)
        consumer = getattr(self, "callback_consumer", None)
        try:
            if source is not None:
                source.set_stream_sink(None)
            if consumer is not None:
                consumer.stop()
        except Exception:
            logging.getLogger("chronos.runtime").exception(
                "Callback consumer shutdown failed",
                extra={"event": "callback_consumer_shutdown_failed"},
            )

    def reconcile_submission_readiness(
        self,
        *,
        now: datetime | None = None,
        trigger: ReconciliationTrigger = "unattributed",
    ) -> SubmissionReconciliationReport:
        """Publish readiness only from one complete, unchanged evidence generation.

        ``trigger`` names what started this pass for the persisted run row (BP-3); a caller
        that does not say is recorded as ``unattributed`` — never a guessed label.
        """

        moment = now or utc_now()
        with self.reconciliation_readiness.reconciliation_session(
            "reconciliation observation in progress"
        ) as generation:
            return self._reconcile_submission_readiness_generation(
                moment, generation, trigger=trigger
            )

    def _reconcile_submission_readiness_generation(
        self,
        moment: datetime,
        generation: int,
        *,
        trigger: ReconciliationTrigger = "unattributed",
    ) -> SubmissionReconciliationReport:
        try:
            restart = self.order_management.reconcile_on_restart_report(now=moment)
            portfolio = self.reconciliation.reconcile()
            scope_status = self.connection.connection_status()
        except BaseException:
            self.reconciliation_readiness.complete(
                expected_generation=generation,
                status=ReconciliationStatus.PENDING,
                reason="reconciliation failed; order submission remains locked",
            )
            raise

        # ReconciliationResult.opening_actions_locked deliberately remains True:
        # its status is one predicate in this composite, never authorization by itself.
        scope_is_current = (
            scope_status.connected
            and scope_status.state is ConnectionState.CONNECTED
            and scope_status.account_id == self.order_management.account_id
        )
        if not scope_is_current:
            status = ReconciliationStatus.PENDING
            reason = "broker connection/account scope changed during reconciliation"
        elif portfolio.status is ReconciliationStatus.MANUAL_REVIEW:
            status = ReconciliationStatus.MANUAL_REVIEW
            reason = "portfolio reconciliation requires manual review"
        elif portfolio.status is not ReconciliationStatus.RECONCILED or portfolio.snapshot is None:
            status = ReconciliationStatus.PENDING
            reason = "portfolio reconciliation did not produce complete evidence"
        elif portfolio.snapshot.open_orders:
            status = ReconciliationStatus.PENDING
            reason = "broker open orders remain; another opening action is locked"
        elif not restart.permits_new_opening_order:
            status = ReconciliationStatus.PENDING
            reason = (
                "sent-active order intents remain after restart reconciliation; "
                "another opening action is locked"
            )
        else:
            status = ReconciliationStatus.RECONCILED
            reason = "full broker/local parity proven with no sent-active order intents"

        self.reconciliation_readiness.complete(
            expected_generation=generation,
            status=status,
            reason=reason,
            reconciled_at=moment if status is ReconciliationStatus.RECONCILED else None,
        )
        report = SubmissionReconciliationReport(
            restart=restart,
            portfolio=portfolio,
            readiness=self.reconciliation_readiness.snapshot(),
        )
        # BP-3: the run row is written only now — after the decided result and after the
        # latch published (the latch never waits on the database); the recorder never raises.
        # getattr: the readiness tests build AppRuntime.__new__ (slots) without the seam.
        recorder = getattr(self, "reconciliation_runs", None)
        if recorder is not None:
            recorder.record(
                _reconciliation_run_snapshot(
                    trigger=trigger,
                    report=report,
                    generation=generation,
                    started_at=moment,
                    account_fingerprint=account_fingerprint(
                        self.order_management.account_id, key=self.persistence_key
                    ),
                )
            )
        return report

    def close(self) -> None:
        try:
            self._stop_callback_consumer()
            self.connection.close()
        finally:
            self.market_data.clear_cache()
            self.database.dispose()


def _reconciliation_run_snapshot(
    *,
    trigger: ReconciliationTrigger,
    report: SubmissionReconciliationReport,
    generation: int,
    started_at: datetime,
    account_fingerprint: str,
) -> ReconciliationSnapshot:
    """The row's content from the decided report — masked, typed, run-keyed."""

    completed_at = utc_now()
    if completed_at < started_at:
        completed_at = started_at
    restart_decisions = tuple(
        {
            "kind": "restart_observation",
            "intent_id": item.intent_id,
            "local_status": item.local_status.value,
            "broker_status": item.broker_status.value if item.broker_status else None,
            "transition_applied": item.transition_applied,
            "reason": item.reason,
        }
        for item in (*report.restart.proven, *report.restart.unresolved)
    )
    return ReconciliationSnapshot.from_result(
        run_id=f"{trigger}-{generation:08d}-{started_at.strftime('%Y%m%dT%H%M%S%fZ')}",
        trigger=trigger,
        portfolio=report.portfolio,
        readiness_status=report.readiness.status,
        readiness_reason=report.readiness.reason,
        generation=generation,
        started_at=started_at,
        completed_at=completed_at,
        account_fingerprint=account_fingerprint,
        restart_decisions=restart_decisions,
    )


def _validate_scope_observations(
    account: AccountSummary,
    status: ConnectionStatus,
) -> None:
    """Fail closed unless both broker reads identify one connected account."""

    if not status.connected or status.state is not ConnectionState.CONNECTED:
        raise RuntimeError("Broker scope cannot bind while the connection is not healthy")
    if (
        not account.account_id
        or account.account_id != account.account_id.strip()
        or status.account_id != account.account_id
    ):
        raise RuntimeError("Broker scope observations do not identify the same account")


def build_runtime(*, register_atexit: bool = True) -> AppRuntime:
    """Construct the full application runtime (broker, DB, services).

    Both entry points share this wiring; the backend owns the process-level
    instance and the Streamlit path caches one per UI process (during the
    migration window in which the UI still runs in-process demo mode).
    """

    settings = get_settings()
    # N3: first, before the log file, the database file or any sidecar exists. An absent or
    # malformed key refuses here in every broker mode, demo included.
    persistence_key = resolve_persistence_key(settings)
    configure_logging(settings.log_level, settings.log_file)
    database = Database(settings.database_url)
    connection: BrokerConnectionManager | None = None
    try:
        database.initialize()
        # ADR-0020: the owner-frozen maximum evidence age. Wired here, in the same
        # change that starts the refresher — expiry without a refresher would
        # tighten with nothing to re-arm it and block the submission path harder
        # than leaving the defect in place. Expiry is evaluated inside
        # `snapshot()`, so a proof stops being trusted whether or not the
        # refresher is alive.
        reconciliation_readiness = ReconciliationReadiness(
            max_evidence_age=timedelta(seconds=settings.reconciliation_max_evidence_age_seconds),
            clock=utc_now,
        )
        # ADR-0009: ONE durable kill-switch instance shared by the broker
        # adapter's last-line check, the submission boundary, and the /live API.
        live_kill_switch = LiveKillSwitch(
            settings.live_kill_switch_file,
            audit=KillSwitchEventRepository(database.sessions),
        )
        broker: Broker
        # CB-1: the adapter's unsolicited-callback stream; the read-only ib_async adapter has none
        callback_source: _CallbackSource | None = None
        convert_execution: Callable[..., BrokerExecution] | None = None
        if settings.broker_mode is BrokerMode.DEMO:
            broker = DemoBroker(profile=settings.demo_profile)
            callback_source = broker
        elif settings.broker_adapter is BrokerAdapter.IB_ASYNC:
            from chronos.broker.ibkr import IBKRBroker

            broker = IBKRBroker(
                settings, on_connection_uncertain=reconciliation_readiness.invalidate
            )
        else:
            # Production default: the official TWS API adapter (lazy import;
            # raises with install guidance when the package is absent). The
            # kill switch is constructed first and shared so the adapter's
            # last-line breaker check (ADR-0009 §8) sees the same durable state
            # as the /live API and the submission boundary.
            from chronos.broker.official_ibkr import OfficialIBKRBroker, execution_from_pair

            broker = OfficialIBKRBroker(
                settings,
                live_kill_switch=live_kill_switch,
                on_connection_uncertain=reconciliation_readiness.invalidate,
                on_managed_account_scope_change=(reconciliation_readiness.invalidating_transition),
                order_ownership=PersistedOrderOwnershipLedger(
                    OrderIntentRepository(database.sessions),
                    OrderTrackerRepository(database.sessions),
                    persistence_key=persistence_key,
                ),
            )
            # CB-1: an adapter double without the callback bridge simply has no stream
            callback_source = getattr(broker, "bridge", None)
            convert_execution = execution_from_pair
        market_data = MarketDataManager(
            broker,
            max_quote_age=timedelta(seconds=settings.max_quote_age_seconds),
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        connection = BrokerConnectionManager(broker, readiness=reconciliation_readiness)
        connection.connect()
        account = connection.run(broker.account_summary())
        status = connection.connection_status()
        _validate_scope_observations(account, status)
        runtime_scope = build_bound_runtime_scope(
            database, settings, account, status, key=persistence_key
        )
        reconciliation = ReconciliationCoordinator(
            connection,
            LocalReconciliationRepository(database.sessions),
            # ADR-0010: a held crypto position must not surface as a permanent
            # MANUAL_REVIEW 'outside the configured allowlist' symbol.
            tuple(dict.fromkeys(settings.symbol_allowlist + settings.crypto_allowlist)),
            # BP-1b: executions the pass observes persist once into fills + commissions;
            # evidence only — no verdict, admission or risk path reads them back.
            execution_repository=ExecutionRepository(database.sessions),
        )
        # BP-3: one reconciliation_runs row per decided pass — evidence only, written after
        # the readiness latch published; failures become application events, never raises.
        reconciliation_runs = ReconciliationRunRecorder(database.sessions)
        short_put_candidates = ShortPutCandidateService(
            connection=connection,
            market_data=market_data,
            reconciliation=reconciliation,
            settings=settings,
            expected_account_fingerprint=account_fingerprint(
                account.account_id, key=persistence_key
            ),
            persistence_key=persistence_key,
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        short_put_risk_preview = ShortPutRiskPreviewService(
            short_put_candidates,
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        short_put_demo_what_if = ShortPutDemoWhatIfService(
            short_put_risk_preview,
            connection,
            settings,
            account_fingerprint(account.account_id, key=persistence_key),
            persistence_key=persistence_key,
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        short_put_demo_approval = ShortPutDemoApprovalService(
            short_put_demo_what_if,
            connection,
            settings,
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        # ADR-0009 §4: the live safety services are constructed BEFORE the order
        # pipeline and the SAME instances are injected into the submission
        # boundary — LiveArmingService state is process-memory, so a second
        # instance would make /live/arm invisible to the boundary. (The kill
        # switch instance was built above, before broker construction, so the
        # adapter's last-line check shares it too.)
        live_arming = LiveArmingService(
            ttl_minutes=settings.live_arm_ttl_minutes,
            audit=LiveArmEventRepository(
                database.sessions,
                account_fingerprint=account_fingerprint(account.account_id, key=persistence_key),
            ),
        )
        session_drawdown = SessionDrawdownBreaker(
            settings.session_baseline_file,
            max_drawdown_usd=settings.max_session_drawdown_usd,
            max_drawdown_pct=settings.max_session_drawdown_pct,
            market_timezone=settings.market_timezone,
            kill_switch=live_kill_switch,
        )
        order_management = _build_order_management(
            settings=settings,
            connection=connection,
            database=database,
            account=account,
            market_data=market_data,
            live_arming=live_arming,
            live_kill_switch=live_kill_switch,
            session_drawdown=session_drawdown,
            reconciliation_readiness=reconciliation_readiness,
            persistence_key=persistence_key,
        )
        callback_consumer = (
            _build_callback_consumer(
                database=database,
                account_id=account.account_id,
                persistence_key=persistence_key,
                invalidate=reconciliation_readiness.invalidate,
                convert_execution=convert_execution,
            )
            if callback_source is not None
            else None
        )
    except BaseException:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logging.getLogger("chronos.runtime").exception(
                    "Broker cleanup failed after runtime startup error",
                    extra={"event": "startup_broker_cleanup_failed"},
                )
        try:
            database.dispose()
        except Exception:
            logging.getLogger("chronos.runtime").exception(
                "Database cleanup failed after runtime startup error",
                extra={"event": "startup_database_cleanup_failed"},
            )
        raise
    runtime = AppRuntime(
        settings=settings,
        runtime_scope=runtime_scope,
        broker=broker,
        connection=connection,
        market_data=market_data,
        database=database,
        reconciliation=reconciliation,
        reconciliation_readiness=reconciliation_readiness,
        short_put_candidates=short_put_candidates,
        short_put_risk_preview=short_put_risk_preview,
        short_put_demo_what_if=short_put_demo_what_if,
        short_put_demo_approval=short_put_demo_approval,
        order_management=order_management,
        live_arming=live_arming,
        live_kill_switch=live_kill_switch,
        session_drawdown=session_drawdown,
        persistence_key=persistence_key,
        reconciliation_runs=reconciliation_runs,
        callback_consumer=callback_consumer,
        callback_source=callback_source,
    )
    if register_atexit:
        atexit.register(runtime.close)
    return runtime


def _build_callback_consumer(
    *,
    database: Database,
    account_id: str,
    persistence_key: PersistenceKey,
    invalidate: Callable[[str], object],
    convert_execution: Callable[..., BrokerExecution] | None,
) -> CallbackConsumer:
    """CB-1: built here, started only by a writer (``AppRuntime.start_callback_consumer``)."""

    intents = OrderIntentRepository(database.sessions)
    tracker_repo = OrderTrackerRepository(database.sessions)
    return CallbackConsumer(
        sessions=database.sessions,
        intents=intents,
        tracker=OrderTracker(intents, tracker_repo),
        ownership=PersistedOrderOwnershipLedger(
            intents, tracker_repo, persistence_key=persistence_key
        ),
        record_execution=_callback_execution_writer(ExecutionRepository(database.sessions)),
        account_id=account_id,
        persistence_key=persistence_key,
        # the ONE latch method the consumer may reach: it demotes, never publishes
        invalidate=invalidate,
        convert_execution=convert_execution,
    )


def _build_order_management(
    *,
    settings: Settings,
    connection: BrokerConnectionManager,
    database: Database,
    account: AccountSummary,
    market_data: MarketDataManager,
    live_arming: LiveArmingService,
    live_kill_switch: LiveKillSwitch,
    session_drawdown: SessionDrawdownBreaker,
    reconciliation_readiness: ReconciliationReadiness,
    persistence_key: PersistenceKey,
) -> OrderManagementService:
    """Wire the human-in-the-loop order pipeline (M5 paper + M7 live branch)."""

    def _read_fresh_quote(intent: WheelOrderIntent) -> MarketQuote | None:
        """Fresh-quote adapter for the boundary's LIVE data gate."""

        if intent.product_family is ProductFamily.OPTION and isinstance(
            intent.contract, OptionContract
        ):
            quotes = connection.run(
                market_data.option_quotes((intent.contract,), force_refresh=True)
            )
            managed = quotes[0] if quotes else None
        elif intent.product_family is ProductFamily.CRYPTO and isinstance(
            intent.contract, CryptoContract
        ):
            managed = connection.run(market_data.crypto_quote(intent.contract, force_refresh=True))
        elif isinstance(intent.contract, UnderlyingContract):
            managed = connection.run(
                market_data.underlying_quote(intent.contract, force_refresh=True)
            )
        else:
            return None
        return managed.quote if managed is not None else None

    intents = OrderIntentRepository(database.sessions)
    tracker_repo = OrderTrackerRepository(database.sessions)
    confirmations = OrderConfirmationRepository(database.sessions)
    risk_decisions = RiskDecisionRepository(database.sessions)
    tracker = OrderTracker(intents, tracker_repo)
    return OrderManagementService(
        settings=settings,
        environment=settings.ib_environment,
        account_id=account.account_id,
        evidence_provider=BrokerRiskEvidenceProvider(
            connection=connection,
            settings=settings,
            reconciliation_readiness=reconciliation_readiness,
            # The repository that makes `max_opening_orders_per_day` real (R-25).
            # Same instance the submission boundary writes intents through, so
            # the cap counts the rows this process is creating.
            intents=intents,
        ),
        risk_engine=OrderRiskEngine(settings, persistence_key=persistence_key),
        preview_service=OrderPreviewService(connection),
        submission_boundary=OrderSubmissionBoundary(
            settings=settings,
            connection=connection,
            intents=intents,
            confirmations=confirmations,
            tracker=tracker_repo,
            live_arming=live_arming,
            live_kill_switch=live_kill_switch,
            session_drawdown=session_drawdown,
            quote_reader=_read_fresh_quote,
            reconciliation_readiness=reconciliation_readiness,
        ),
        modification=OrderModificationService(
            connection=connection,
            intents=intents,
            tracker=tracker,
            tracker_repo=tracker_repo,
            live_environment=settings.ib_environment is IBEnvironment.LIVE,
        ),
        cancellation=OrderCancellationService(
            connection=connection,
            intents=intents,
            tracker=tracker,
            tracker_repo=tracker_repo,
        ),
        tracker=tracker,
        tracker_repo=tracker_repo,
        reconciler=OrderRestartReconciler(
            connection=connection,
            intents=intents,
            tracker=tracker,
            reconciliation_readiness=reconciliation_readiness,
            expected_broker_client_id=settings.ib_client_id,
            persistence_key=persistence_key,
            market_timezone=settings.market_timezone,
        ),
        intents=intents,
        confirmations=confirmations,
        risk_decisions=risk_decisions,
        broker_environment_is_paper=settings.ib_environment is IBEnvironment.PAPER,
        persistence_key=persistence_key,
    )

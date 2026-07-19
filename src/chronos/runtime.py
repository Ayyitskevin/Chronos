"""Process-lifetime application runtime, shared by the backend and the UI.

Extracted from ``chronos.ui.session`` (Milestone 1 of the live Wheel plan) so
the FastAPI backend can own the identical wiring without importing Streamlit.
``chronos.ui.session`` now wraps :func:`build_runtime` with Streamlit's
resource cache; the backend calls it directly, exactly once, at startup.
"""

from __future__ import annotations

import atexit
import logging
from dataclasses import dataclass
from datetime import timedelta

from chronos.broker.base import Broker
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker, demo_now
from chronos.broker.market_data import MarketDataManager
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    ConnectionState,
    IBEnvironment,
    ProductFamily,
)
from chronos.domain.models import (
    AccountSummary,
    ConnectionStatus,
    MarketQuote,
    OptionContract,
    UnderlyingContract,
)
from chronos.orders.arming import LiveArmingService
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.live_audit import KillSwitchEventRepository, LiveArmEventRepository
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine
from chronos.orders.service import OrderManagementService
from chronos.orders.session_drawdown import SessionDrawdownBreaker
from chronos.orders.submission import OrderSubmissionBoundary
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.services.reconciliation import ReconciliationCoordinator
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.services.short_put_demo_approval import ShortPutDemoApprovalService
from chronos.services.short_put_demo_what_if import ShortPutDemoWhatIfService
from chronos.services.short_put_risk_preview import ShortPutRiskPreviewService
from chronos.ui.runtime_scope import RuntimeScopeView, build_bound_runtime_scope
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.logging import configure_logging


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
    short_put_risk_preview: ShortPutRiskPreviewService
    short_put_demo_what_if: ShortPutDemoWhatIfService
    short_put_demo_approval: ShortPutDemoApprovalService
    order_management: OrderManagementService
    live_arming: LiveArmingService
    live_kill_switch: LiveKillSwitch
    session_drawdown: SessionDrawdownBreaker

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self.market_data.clear_cache()
            self.database.dispose()


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
    configure_logging(settings.log_level, settings.log_file)
    database = Database(settings.database_url)
    connection: BrokerConnectionManager | None = None
    try:
        database.initialize()
        # ADR-0009: ONE durable kill-switch instance shared by the broker
        # adapter's last-line check, the submission boundary, and the /live API.
        live_kill_switch = LiveKillSwitch(
            settings.live_kill_switch_file,
            audit=KillSwitchEventRepository(database.sessions),
        )
        broker: Broker
        if settings.broker_mode is BrokerMode.DEMO:
            broker = DemoBroker(profile=settings.demo_profile)
        elif settings.broker_adapter is BrokerAdapter.IB_ASYNC:
            from chronos.broker.ibkr import IBKRBroker

            broker = IBKRBroker(settings)
        else:
            # Production default: the official TWS API adapter (lazy import;
            # raises with install guidance when the package is absent). The
            # kill switch is constructed first and shared so the adapter's
            # last-line breaker check (ADR-0009 §8) sees the same durable state
            # as the /live API and the submission boundary.
            from chronos.broker.official_ibkr import OfficialIBKRBroker

            broker = OfficialIBKRBroker(settings, live_kill_switch=live_kill_switch)
        market_data = MarketDataManager(
            broker,
            max_quote_age=timedelta(seconds=settings.max_quote_age_seconds),
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        connection = BrokerConnectionManager(broker)
        connection.connect()
        account = connection.run(broker.account_summary())
        status = connection.run(broker.connection_status())
        _validate_scope_observations(account, status)
        runtime_scope = build_bound_runtime_scope(database, settings, account, status)
        reconciliation = ReconciliationCoordinator(
            connection,
            LocalReconciliationRepository(database.sessions),
            # ADR-0010: a held crypto position must not surface as a permanent
            # MANUAL_REVIEW 'outside the configured allowlist' symbol.
            tuple(dict.fromkeys(settings.symbol_allowlist + settings.crypto_allowlist)),
        )
        short_put_candidates = ShortPutCandidateService(
            connection=connection,
            market_data=market_data,
            reconciliation=reconciliation,
            settings=settings,
            expected_account_fingerprint=account_fingerprint(account.account_id),
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
            account_fingerprint(account.account_id),
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
                account_fingerprint=account_fingerprint(account.account_id),
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
        short_put_candidates=short_put_candidates,
        short_put_risk_preview=short_put_risk_preview,
        short_put_demo_what_if=short_put_demo_what_if,
        short_put_demo_approval=short_put_demo_approval,
        order_management=order_management,
        live_arming=live_arming,
        live_kill_switch=live_kill_switch,
        session_drawdown=session_drawdown,
    )
    if register_atexit:
        atexit.register(runtime.close)
    return runtime


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
        evidence_provider=BrokerRiskEvidenceProvider(connection=connection, settings=settings),
        risk_engine=OrderRiskEngine(settings),
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
            market_timezone=settings.market_timezone,
        ),
        intents=intents,
        confirmations=confirmations,
        risk_decisions=risk_decisions,
        broker_environment_is_paper=settings.ib_environment is IBEnvironment.PAPER,
    )

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
from chronos.domain.enums import BrokerAdapter, BrokerMode, ConnectionState
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.persistence.database import Database
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
        broker: Broker
        if settings.broker_mode is BrokerMode.DEMO:
            broker = DemoBroker(profile=settings.demo_profile)
        elif settings.broker_adapter is BrokerAdapter.IB_ASYNC:
            from chronos.broker.ibkr import IBKRBroker

            broker = IBKRBroker(settings)
        else:
            # Production default: the official TWS API adapter (lazy import;
            # raises with install guidance when the package is absent).
            from chronos.broker.official_ibkr import OfficialIBKRBroker

            broker = OfficialIBKRBroker(settings)
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
            settings.symbol_allowlist,
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
    )
    if register_atexit:
        atexit.register(runtime.close)
    return runtime

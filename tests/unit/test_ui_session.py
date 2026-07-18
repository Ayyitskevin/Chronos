from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from chronos import runtime as session
from chronos.broker.demo import DEMO_NOW, DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    EligibilityStatus,
)
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.persistence.database import Database
from chronos.persistence.schema import DatabaseScopeRow
from chronos.services.short_put_risk_preview import ShortPutRiskPreviewRequest
from chronos.ui.session import _validate_scope_observations

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)


def _account(account_id: str = "DU1234567") -> AccountSummary:
    return AccountSummary(
        account_id=account_id,
        net_liquidation=Decimal("100000"),
        total_cash=Decimal("50000"),
        buying_power=Decimal("50000"),
        as_of=NOW,
    )


def _status(
    *,
    account_id: str | None = "DU1234567",
    connected: bool = True,
    state: ConnectionState = ConnectionState.CONNECTED,
) -> ConnectionStatus:
    return ConnectionStatus(
        state=state,
        environment=DisplayEnvironment.DEMO,
        connected=connected,
        account_id=account_id,
        data_quality=DataQuality.DEMO,
        last_successful_sync=NOW,
    )


def test_scope_observations_accept_one_healthy_matching_account() -> None:
    _validate_scope_observations(_account(), _status())


@pytest.mark.parametrize(
    "status",
    [
        _status(connected=False, state=ConnectionState.DISCONNECTED),
        _status(connected=True, state=ConnectionState.DEGRADED),
    ],
)
def test_scope_observations_reject_unhealthy_connection(status: ConnectionStatus) -> None:
    with pytest.raises(RuntimeError, match="connection is not healthy"):
        _validate_scope_observations(_account(), status)


@pytest.mark.parametrize("status_account", [None, "DU7654321", " DU1234567 "])
def test_scope_observations_reject_missing_or_mismatched_account(
    status_account: str | None,
) -> None:
    with pytest.raises(RuntimeError, match="same account"):
        _validate_scope_observations(_account(), _status(account_id=status_account))


def test_scope_observations_reject_noncanonical_summary_account() -> None:
    with pytest.raises(RuntimeError, match="same account"):
        _validate_scope_observations(
            _account(" DU1234567 "),
            _status(account_id=" DU1234567 "),
        )


def test_runtime_wires_demo_reads_through_one_market_data_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    startup_calls = {"account_summary": 0, "connection_status": 0}
    original_account_summary = DemoBroker.account_summary
    original_connection_status = DemoBroker.connection_status

    async def track_account_summary(broker: DemoBroker) -> AccountSummary:
        startup_calls["account_summary"] += 1
        return await original_account_summary(broker)

    async def track_connection_status(broker: DemoBroker) -> ConnectionStatus:
        startup_calls["connection_status"] += 1
        return await original_connection_status(broker)

    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chronos.db'}",
            "log_file": tmp_path / "chronos.log",
        }
    )
    monkeypatch.setattr(session, "get_settings", lambda: settings)
    monkeypatch.setattr(DemoBroker, "account_summary", track_account_summary)
    monkeypatch.setattr(DemoBroker, "connection_status", track_connection_status)

    runtime = session.build_runtime(register_atexit=False)
    try:
        assert startup_calls == {"account_summary": 1, "connection_status": 1}
        assert isinstance(runtime.broker, DemoBroker)
        assert runtime.runtime_scope.broker_mode is BrokerMode.DEMO
        assert runtime.runtime_scope.masked_account_id == "DU•••4567"
        assert runtime.runtime_scope.account_observed_at == DEMO_NOW
        assert runtime.runtime_scope.runtime_view_persisted is False
        assert runtime.runtime_scope.submission_locked is True
        assert "DU1234567" not in runtime.runtime_scope.model_dump_json()
        underlying = runtime.connection.run(runtime.broker.qualify_underlying("AAPL"))
        first = runtime.connection.run(runtime.market_data.underlying_quote(underlying))
        second = runtime.connection.run(runtime.market_data.underlying_quote(underlying))

        assert first.quote.data_quality is DataQuality.DEMO
        assert first.observed_at == DEMO_NOW
        assert first.from_cache is False
        assert second.from_cache is True
        assert runtime.market_data.active_subscription_count == 0

        reconciliation = runtime.reconciliation.reconcile()
        assert reconciliation.opening_actions_locked is True
        assert reconciliation.snapshot is not None
        assert reconciliation.snapshot.account.masked_account_id == "DU•••4567"
        assert "DU1234567" not in reconciliation.model_dump_json()

        candidate_evaluation = runtime.short_put_candidates.evaluate("AAPL")
        assert candidate_evaluation.status is EligibilityStatus.NO_TRADE
        assert candidate_evaluation.opening_actions_locked is True
        assert candidate_evaluation.reconciliation is not None
        assert "DU1234567" not in candidate_evaluation.model_dump_json()

        risk_result = runtime.short_put_risk_preview.preview(
            ShortPutRiskPreviewRequest(
                symbol="AAPL",
                selected_contract_id=2001,
                total_commission_estimate=Decimal("0.65"),
            )
        )
        assert risk_result.opening_actions_locked is True
        assert risk_result.preview is None
        assert risk_result.candidate_refresh is not None
        assert "DU1234567" not in risk_result.model_dump_json()
    finally:
        runtime.close()

    assert runtime.connection.running is False


def test_runtime_scope_binding_failure_closes_broker_and_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chronos.db'}",
            "log_file": tmp_path / "chronos.log",
        }
    )
    events: list[str] = []
    original_disconnect = DemoBroker.disconnect
    original_dispose = Database.dispose

    def reject_scope_binding(_database: Database, **_kwargs: object) -> None:
        events.append("scope_binding_attempted")
        raise ValueError("fixed test rejection")

    async def track_disconnect(broker: DemoBroker) -> None:
        events.append("broker_disconnected")
        await original_disconnect(broker)

    def track_dispose(database: Database) -> None:
        events.append("database_disposed")
        original_dispose(database)

    monkeypatch.setattr(session, "get_settings", lambda: settings)
    monkeypatch.setattr(Database, "bind_scope", reject_scope_binding)
    monkeypatch.setattr(DemoBroker, "disconnect", track_disconnect)
    monkeypatch.setattr(Database, "dispose", track_dispose)

    with pytest.raises(ValueError, match="fixed test rejection"):
        session.build_runtime(register_atexit=False)

    assert events == [
        "scope_binding_attempted",
        "broker_disconnected",
        "database_disposed",
    ]


def test_runtime_scope_preflight_rejects_before_database_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chronos.db'}",
            "log_file": tmp_path / "chronos.log",
        }
    )
    original_status = DemoBroker.connection_status

    async def mismatched_status(broker: DemoBroker) -> ConnectionStatus:
        status = await original_status(broker)
        return status.model_copy(
            update={
                "environment": DisplayEnvironment.PAPER,
                "data_quality": DataQuality.UNKNOWN,
            }
        )

    monkeypatch.setattr(session, "get_settings", lambda: settings)
    monkeypatch.setattr(DemoBroker, "connection_status", mismatched_status)

    with pytest.raises(ValueError, match="environment does not match"):
        session.build_runtime(register_atexit=False)

    verification = Database(settings.database_url)
    try:
        verification.initialize()
        with verification.sessions() as database_session:
            assert database_session.get(DatabaseScopeRow, 1) is None
    finally:
        verification.dispose()

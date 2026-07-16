from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.broker.demo import DEMO_NOW, DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
)
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.ui import session
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
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chronos.db'}",
            "log_file": tmp_path / "chronos.log",
        }
    )
    monkeypatch.setattr(session, "get_settings", lambda: settings)

    runtime = session._build_runtime()
    try:
        assert isinstance(runtime.broker, DemoBroker)
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
    finally:
        runtime.close()

    assert runtime.connection.running is False

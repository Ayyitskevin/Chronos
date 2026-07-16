from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
)
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.ui import dashboard
from chronos.ui.runtime_scope import RuntimeScopeView, build_bound_runtime_scope
from chronos.ui.session import AppRuntime


class _ExplodingRuntime:
    def __init__(self, runtime_scope: object) -> None:
        self.runtime_scope = runtime_scope

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"interactive runtime attribute was accessed: {name}")


class _ScopeBinder:
    def bind_scope(
        self,
        *,
        broker_mode: str,
        environment: str,
        account_id: str,
    ) -> None:
        pass


def _valid_runtime_scope() -> RuntimeScopeView:
    observed_at = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
    return build_bound_runtime_scope(
        _ScopeBinder(),
        Settings.model_validate({}),
        AccountSummary(
            account_id="DU1234567",
            net_liquidation=Decimal("100000"),
            total_cash=Decimal("50000"),
            buying_power=Decimal("50000"),
            as_of=observed_at,
        ),
        ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.DEMO,
            connected=True,
            account_id="DU1234567",
            data_quality=DataQuality.DEMO,
            last_successful_sync=observed_at,
        ),
    )


@pytest.mark.parametrize(
    "runtime_scope",
    [
        None,
        _valid_runtime_scope().model_copy(update={"masked_account_id": "DU1234567"}),
        _valid_runtime_scope().model_copy(update={"submission_locked": False}),
        _valid_runtime_scope().model_copy(
            update={"account_observed_at": datetime.min.replace(tzinfo=UTC)}
        ),
    ],
)
def test_dashboard_withholds_interactive_workspace_for_invalid_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
    runtime_scope: RuntimeScopeView | None,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(dashboard.st, "title", lambda _value: None)
    monkeypatch.setattr(dashboard.st, "caption", lambda _value: None)
    monkeypatch.setattr(
        dashboard,
        "render_runtime_scope_unavailable",
        lambda: calls.append("unavailable"),
    )
    monkeypatch.setattr(
        dashboard,
        "_stored_demo_approval_record",
        lambda: (_ for _ in ()).throw(AssertionError("session state was accessed")),
    )

    dashboard.render_dashboard(cast(AppRuntime, _ExplodingRuntime(runtime_scope)))

    assert calls == ["unavailable"]

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from streamlit.testing.v1 import AppTest
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

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
from chronos.utils.identifiers import PersistenceKey


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
        key: PersistenceKey,
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
        key=TEST_PERSISTENCE_KEY,
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


def _run_reconciliation_account(provenance: str) -> None:
    # AppTest executes this body in a standalone module; no broker is constructed.
    from datetime import UTC, datetime
    from decimal import Decimal

    from chronos.domain.enums import (
        AccountSummaryProvenance,
        DataQuality,
        DisplayEnvironment,
        ReconciliationStatus,
    )
    from chronos.services.reconciliation import (
        ReconciliationAccountView,
        ReconciliationResult,
        ReconciliationSnapshotView,
    )
    from chronos.ui.dashboard import _render_portfolio

    observed_at = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    result = ReconciliationResult(
        status=ReconciliationStatus.RECONCILED,
        reasons=(),
        symbols=(),
        snapshot=ReconciliationSnapshotView(
            environment=DisplayEnvironment.PAPER,
            data_quality=DataQuality.UNKNOWN,
            account=ReconciliationAccountView(
                masked_account_id="DU•••4567",
                net_liquidation=Decimal("250000"),
                total_cash=Decimal("100000"),
                buying_power=Decimal("480000"),
                currency="USD",
                as_of=observed_at,
                provenance=AccountSummaryProvenance(provenance),
            ),
            positions=(),
            open_orders=(),
            execution_count=0,
            server_time_start=observed_at,
            server_time_end=observed_at,
            captured_at=observed_at,
            window_seconds=0,
            server_window_seconds=0,
        ),
    )
    _render_portfolio(result)


@pytest.mark.parametrize("provenance", ["UNKNOWN", "VERIFIED_RESPONSE"])
def test_reconciliation_dashboard_marks_account_provenance(provenance: str) -> None:
    app = AppTest.from_function(_run_reconciliation_account, args=(provenance,)).run(timeout=60)
    assert not app.exception
    warnings = " ".join(w.value for w in app.warning)
    captions = " ".join(c.value for c in app.caption)
    metrics = {m.label: m.value for m in app.metric}
    if provenance == "VERIFIED_RESPONSE":
        assert "VERIFIED_RESPONSE" in captions
        assert "unverified cache" not in warnings.lower()
        suffix = ""
    else:
        assert "UNKNOWN" in warnings
        assert "unverified cache" in warnings.lower()
        assert "read-only" in warnings.lower()
        suffix = " (unverified)"
    assert metrics[f"Net liquidation{suffix}"] == "250,000.00 USD"
    assert metrics[f"Cash{suffix}"] == "100,000.00 USD"
    assert metrics[f"Buying power{suffix}"] == "480,000.00 USD"
    assert "local read completion time" in captions.lower()
    assert "not broker valuation time" in captions.lower()
    assert metrics["Open account orders"] == "0"
    assert metrics["Observed executions"] == "0"

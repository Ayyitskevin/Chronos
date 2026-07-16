from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

from chronos.broker.base import BrokerDataError
from chronos.broker.demo import DemoBroker
from chronos.config.settings import get_settings
from chronos.domain.enums import BrokerMode, DataQuality
from chronos.domain.models import OrderModification, OrderPreview, OrderRequest
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.persistence.schema import (
    ApplicationEventRow,
    GuardrailDecisionRow,
    OrderDraftRow,
    OrderPreviewRow,
    ReconciliationRunRow,
    SubmittedOrderRow,
    WheelCycleRow,
)
from chronos.services.reconciliation import ReconciliationCoordinator
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.services.short_put_demo_approval import (
    ShortPutDemoApprovalRequest,
    ShortPutDemoApprovalService,
)
from chronos.services.short_put_risk_preview import (
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewService,
)
from chronos.strategy.strike_resolver import (
    CandidateRejection,
    CandidateRejectionCode,
    RejectedCandidate,
)
from chronos.ui.dashboard import (
    _CANDIDATE_EVALUATION_STATE_KEY,
    _DEMO_APPROVAL_FEEDBACK_STATE_KEY,
    _DEMO_APPROVAL_STATE_KEY,
    _DEMO_WHAT_IF_STATE_KEY,
    _PORTFOLIO_OBSERVATION_STATE_KEY,
    _RISK_PREVIEW_STATE_KEY,
)
from chronos.ui.portfolio_state import PortfolioObservationSessionRecord
from chronos.ui.rehearsal_state import (
    DemoApprovalDisposition,
    DemoApprovalSessionRecord,
)
from chronos.ui.session import get_runtime


def test_demo_portfolio_and_symbol_pages_render_without_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    broker_calls = {
        "account_summary": 0,
        "connection_status": 0,
        "server_time": 0,
        "positions": 0,
        "open_orders": 0,
        "executions": 0,
        "preview_order": 0,
        "submit_order": 0,
        "modify_order": 0,
        "cancel_order": 0,
    }
    local_read_calls: list[str] = []
    original_evaluate = ShortPutCandidateService.evaluate
    original_account_summary = DemoBroker.account_summary
    original_connection_status = DemoBroker.connection_status
    original_server_time = DemoBroker.server_time
    original_positions = DemoBroker.positions
    original_open_orders = DemoBroker.open_orders
    original_executions = DemoBroker.executions
    original_preview_order = DemoBroker.preview_order
    original_submit_order = DemoBroker.submit_order
    original_modify_order = DemoBroker.modify_order
    original_cancel_order = DemoBroker.cancel_order
    original_local_read = LocalReconciliationRepository.read

    def track_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    async def track_account_summary(broker: DemoBroker):
        broker_calls["account_summary"] += 1
        return await original_account_summary(broker)

    async def track_connection_status(broker: DemoBroker):
        broker_calls["connection_status"] += 1
        return await original_connection_status(broker)

    async def track_server_time(broker: DemoBroker):
        broker_calls["server_time"] += 1
        return await original_server_time(broker)

    async def track_positions(broker: DemoBroker):
        broker_calls["positions"] += 1
        return await original_positions(broker)

    async def track_open_orders(broker: DemoBroker):
        broker_calls["open_orders"] += 1
        return await original_open_orders(broker)

    async def track_executions(broker: DemoBroker, since=None):
        broker_calls["executions"] += 1
        return await original_executions(broker, since)

    async def track_preview_order(broker: DemoBroker, request: OrderRequest):
        broker_calls["preview_order"] += 1
        return await original_preview_order(broker, request)

    async def track_submit_order(broker: DemoBroker, request: OrderRequest):
        broker_calls["submit_order"] += 1
        return await original_submit_order(broker, request)

    async def track_modify_order(broker: DemoBroker, request: OrderModification):
        broker_calls["modify_order"] += 1
        return await original_modify_order(broker, request)

    async def track_cancel_order(broker: DemoBroker, broker_order_id: int):
        broker_calls["cancel_order"] += 1
        return await original_cancel_order(broker, broker_order_id)

    def track_local_read(
        repository: LocalReconciliationRepository,
        current_account_id: str,
    ):
        local_read_calls.append(current_account_id)
        return original_local_read(repository, current_account_id)

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", track_evaluation)
    monkeypatch.setattr(DemoBroker, "account_summary", track_account_summary)
    monkeypatch.setattr(DemoBroker, "connection_status", track_connection_status)
    monkeypatch.setattr(DemoBroker, "server_time", track_server_time)
    monkeypatch.setattr(DemoBroker, "positions", track_positions)
    monkeypatch.setattr(DemoBroker, "open_orders", track_open_orders)
    monkeypatch.setattr(DemoBroker, "executions", track_executions)
    monkeypatch.setattr(DemoBroker, "preview_order", track_preview_order)
    monkeypatch.setattr(DemoBroker, "submit_order", track_submit_order)
    monkeypatch.setattr(DemoBroker, "modify_order", track_modify_order)
    monkeypatch.setattr(DemoBroker, "cancel_order", track_cancel_order)
    monkeypatch.setattr(LocalReconciliationRepository, "read", track_local_read)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)

        assert not app.exception
        assert [title.value for title in app.title] == ["Chronos"]
        assert app.radio[0].value == "Portfolio Dashboard"
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Broker source"] == "DEMO"
        assert metrics["Startup environment"] == "DEMO"
        assert metrics["Startup data"] == "DEMO"
        assert metrics["Startup broker"] == "CONNECTED"
        assert metrics["Startup masked account"] == "DU•••4567"
        assert metrics["Order path"] == "CODE LOCKED"
        assert metrics["Portfolio observation"] == "NOT_OBSERVED"
        assert metrics["Opening actions"] == "LOCKED"
        assert "Reconciliation" not in metrics
        assert not app.dataframe
        assert app.button[0].label == "Run read-only portfolio observation"
        assert broker_calls == {
            "account_summary": 1,
            "connection_status": 1,
            "server_time": 0,
            "positions": 0,
            "open_orders": 0,
            "executions": 0,
            "preview_order": 0,
            "submit_order": 0,
            "modify_order": 0,
            "cancel_order": 0,
        }
        assert local_read_calls == []
        assert any("Passive reruns do not refresh it" in item.value for item in app.caption)

        app.run(timeout=10)

        assert not app.exception
        assert broker_calls["account_summary"] == 1
        assert broker_calls["connection_status"] == 1
        assert all(
            broker_calls[name] == 0
            for name in (
                "server_time",
                "positions",
                "open_orders",
                "executions",
                "preview_order",
                "submit_order",
                "modify_order",
                "cancel_order",
            )
        )
        assert local_read_calls == []

        app.button[0].click().run(timeout=10)

        assert not app.exception
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Reconciliation"] == "MANUAL_REVIEW"
        assert metrics["Opening actions"] == "LOCKED"
        assert metrics["Account"] == "DU•••4567"
        assert metrics["Open account orders"] == "1"
        assert metrics["Observed executions"] == "1"
        assert metrics["Net liquidation"] == "250,000.00 USD"
        assert all("$" not in str(metric.value) for metric in app.metric)
        assert all("$" not in dataframe.value.to_string() for dataframe in app.dataframe)

        reconciliation_table = next(
            dataframe.value
            for dataframe in app.dataframe
            if "Wheel stage" in dataframe.value.columns
        )
        assert set(reconciliation_table["Opening actions"]) == {"LOCKED"}
        assert {"RECONCILED", "MANUAL_REVIEW"} <= set(reconciliation_table["Status"])
        assert app.expander
        assert "DU1234567" not in str(app)
        observation = app.session_state[_PORTFOLIO_OBSERVATION_STATE_KEY]
        assert isinstance(observation, PortfolioObservationSessionRecord)
        assert observation.historical_display_only is True
        assert observation.authority_created is False
        assert observation.persistence_recorded is False
        assert observation.opening_actions_locked is True
        assert "DU1234567" not in str(app.session_state.filtered_state)
        assert broker_calls == {
            "account_summary": 3,
            "connection_status": 3,
            "server_time": 2,
            "positions": 2,
            "open_orders": 2,
            "executions": 2,
            "preview_order": 0,
            "submit_order": 0,
            "modify_order": 0,
            "cancel_order": 0,
        }
        assert local_read_calls == ["DU1234567"]
        assert any("Historical startup identity only" in caption.value for caption in app.caption)
        assert any("historical display only" in caption.value for caption in app.caption)

        app.run(timeout=10)

        assert broker_calls["account_summary"] == 3
        assert broker_calls["connection_status"] == 3
        assert broker_calls["server_time"] == 2
        assert broker_calls["positions"] == 2
        assert broker_calls["open_orders"] == 2
        assert broker_calls["executions"] == 2
        assert local_read_calls == ["DU1234567"]

        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == []
        detail_metrics = {metric.label: metric.value for metric in app.metric}
        assert detail_metrics["Broker source"] == "DEMO"
        assert detail_metrics["Startup masked account"] == "DU•••4567"
        assert detail_metrics["Order path"] == "CODE LOCKED"
        assert "Reconciliation" not in detail_metrics
        assert detail_metrics["Candidate result"] == "NOT_EVALUATED"
        assert detail_metrics["Candidate actions"] == "LOCKED"
        assert "Last" not in detail_metrics
        assert app.selectbox[0].options == ["AAPL", "MSFT", "SPY"]
        assert app.button[0].label == "Run read-only evaluation"
        app.run(timeout=10)
        assert evaluate_calls == []
        assert broker_calls["account_summary"] == 3
        assert broker_calls["connection_status"] == 3
        assert broker_calls["server_time"] == 2
        assert broker_calls["positions"] == 2
        assert broker_calls["open_orders"] == 2
        assert broker_calls["executions"] == 2
        assert local_read_calls == ["DU1234567"]

        app.radio[0].set_value("Portfolio Dashboard").run(timeout=10)

        assert not app.exception
        assert broker_calls["account_summary"] == 3
        assert broker_calls["connection_status"] == 3
        assert broker_calls["server_time"] == 2
        assert broker_calls["positions"] == 2
        assert broker_calls["open_orders"] == 2
        assert broker_calls["executions"] == 2
        assert local_read_calls == ["DU1234567"]

        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert broker_calls == {
            "account_summary": 5,
            "connection_status": 5,
            "server_time": 4,
            "positions": 4,
            "open_orders": 4,
            "executions": 4,
            "preview_order": 0,
            "submit_order": 0,
            "modify_order": 0,
            "cancel_order": 0,
        }
        assert local_read_calls == ["DU1234567", "DU1234567"]

        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        detail_metrics = {metric.label: metric.value for metric in app.metric}
        assert detail_metrics["Reconciliation"] == "MANUAL_REVIEW"
        assert detail_metrics["Candidate result"] == "NO_TRADE"
        assert any(
            "Whole-account allocation is not proven empty" in warning.value
            for warning in app.warning
        )
        assert all("$" not in str(metric.value) for metric in app.metric)
        assert app.selectbox[0].value == "AAPL"
        assert "DU1234567" not in str(app)
        assert any("historical display only" in caption.value for caption in app.caption)
        assert any("EDT" in caption.value for caption in app.caption)
        app.run(timeout=10)
        assert evaluate_calls == ["AAPL"]
        assert all(
            broker_calls[name] == 0
            for name in ("preview_order", "submit_order", "modify_order", "cancel_order")
        )
        with get_runtime().database.sessions() as session:
            for row_type in (
                ReconciliationRunRow,
                OrderDraftRow,
                OrderPreviewRow,
                SubmittedOrderRow,
                GuardrailDecisionRow,
                ApplicationEventRow,
            ):
                assert session.scalar(select(func.count()).select_from(row_type)) == 0
        assert "DU1234567" not in (tmp_path / "chronos.log").read_text()
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_portfolio_observation_failure_and_invalid_state_clear_prior_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    reconcile_calls = 0
    fail_next = False
    invalid_next = False
    original_reconcile = ReconciliationCoordinator.reconcile

    def controlled_reconcile(coordinator: ReconciliationCoordinator):
        nonlocal reconcile_calls
        reconcile_calls += 1
        if fail_next:
            raise BrokerDataError("DU1234567 sensitive broker failure")
        result = original_reconcile(coordinator)
        if not invalid_next:
            return result
        assert result.snapshot is not None
        snapshot = result.snapshot.model_copy(
            update={"account": result.snapshot.account.model_copy(update={"currency": " "})}
        )
        return result.model_copy(update={"snapshot": snapshot})

    monkeypatch.setattr(ReconciliationCoordinator, "reconcile", controlled_reconcile)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 0
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 1
        valid_record = app.session_state[_PORTFOLIO_OBSERVATION_STATE_KEY]
        assert isinstance(valid_record, PortfolioObservationSessionRecord)
        assert any("historical display only" in item.value for item in app.caption)

        fail_next = True
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 2
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Portfolio observation"] == "NOT_OBSERVED"
        assert metrics["Opening actions"] == "LOCKED"
        assert "Reconciliation" not in metrics
        assert not app.dataframe
        assert _PORTFOLIO_OBSERVATION_STATE_KEY not in app.session_state.filtered_state
        assert any(
            "read-only portfolio observation could not complete safely" in item.value
            for item in app.error
        )
        assert "DU1234567" not in str(app)

        forged_result = valid_record.result.model_copy(
            update={
                "reasons": (
                    *valid_record.result.reasons,
                    "Account DU1234567 escaped into presentation state.",
                )
            }
        )
        app.session_state[_PORTFOLIO_OBSERVATION_STATE_KEY] = valid_record.model_copy(
            update={"result": forged_result}
        )
        app.run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 2
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Portfolio observation"] == "NOT_OBSERVED"
        assert "Reconciliation" not in metrics
        assert not app.dataframe
        assert _PORTFOLIO_OBSERVATION_STATE_KEY not in app.session_state.filtered_state
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)

        fail_next = False
        invalid_next = True
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 3
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Portfolio observation"] == "NOT_OBSERVED"
        assert metrics["Opening actions"] == "LOCKED"
        assert "Reconciliation" not in metrics
        assert not app.dataframe
        assert _PORTFOLIO_OBSERVATION_STATE_KEY not in app.session_state.filtered_state
        assert any(
            "read-only portfolio observation could not be retained safely" in item.value
            for item in app.error
        )

        app.run(timeout=10)

        assert not app.exception
        assert reconcile_calls == 3
        assert _PORTFOLIO_OBSERVATION_STATE_KEY not in app.session_state.filtered_state
        assert "DU1234567" not in (tmp_path / "chronos.log").read_text()
        with get_runtime().database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ReconciliationRunRow)) == 0
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_explicit_candidate_evaluation_renders_locked_eligible_and_clears_stale_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    fail_next = False
    original_evaluate = ShortPutCandidateService.evaluate

    def controlled_evaluation(service: ShortPutCandidateService, symbol: str):
        nonlocal fail_next
        evaluate_calls.append(symbol)
        if fail_next:
            raise BrokerDataError("DU1234567 sensitive broker detail")
        broker = service._broker
        assert isinstance(broker, DemoBroker)
        broker._positions = ()
        broker._orders = ()
        broker._executions = ()
        service._settings = service._settings.model_copy(
            update={"max_expirations": 1, "max_strikes_per_expiration": 1}
        )
        result = original_evaluate(service, symbol)
        assert result.resolution is not None
        rejected = RejectedCandidate(
            contract=broker._option_contracts[2003],
            data_quality=DataQuality.DEMO,
            data_age_seconds=Decimal("0"),
            rejection_reasons=(
                CandidateRejection(
                    code=CandidateRejectionCode.INSUFFICIENT_LIQUIDITY,
                    explanation="Crafted presentation-only rejection.",
                ),
            ),
        )
        resolution = result.resolution.model_copy(update={"rejected": (rejected,)})
        return result.model_copy(update={"resolution": resolution})

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == []
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Candidate result"] == "ELIGIBLE"
        assert metrics["Candidate actions"] == "LOCKED"
        assert metrics["Underlying data quality"] == "DEMO"
        candidate_table = next(
            dataframe.value for dataframe in app.dataframe if "Rank" in dataframe.value.columns
        )
        rejected_table = next(
            dataframe.value
            for dataframe in app.dataframe
            if "Reason codes" in dataframe.value.columns
        )
        assert set(candidate_table["Opening actions"]) == {"LOCKED"}
        assert set(candidate_table["Data quality"]) == {"DEMO"}
        assert "Opening actions" not in rejected_table.columns
        assert set(rejected_table["Data quality"]) == {"DEMO"}
        assert any(expander.label == "Candidate 1 rationale" for expander in app.expander)
        assert all("$" not in str(metric.value) for metric in app.metric)
        assert all("$" not in dataframe.value.to_string() for dataframe in app.dataframe)
        assert _CANDIDATE_EVALUATION_STATE_KEY in app.session_state
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL"]
        app.selectbox[0].set_value("MSFT").run(timeout=10)
        assert evaluate_calls == ["AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert {metric.label: metric.value for metric in app.metric}["Candidate result"] == (
            "NOT_EVALUATED"
        )

        app.selectbox[0].set_value("AAPL").run(timeout=10)
        app.button[0].click().run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY in app.session_state

        fail_next = True
        app.button[0].click().run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert any(
            error.value == "The broker request could not complete safely. See the local log."
            for error in app.error
        )
        assert "sensitive broker detail" not in str(app)
        assert "DU1234567" not in str(app)
        log_text = (tmp_path / "chronos.log").read_text(encoding="utf-8")
        assert '"error_type":"BrokerDataError"' in log_text
        assert "sensitive broker detail" not in log_text
        assert "DU1234567" not in log_text
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_explicit_risk_preview_refreshes_evidence_and_invalidates_stale_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    unexpected_order_calls: list[str] = []
    fail_next = False
    original_evaluate = ShortPutCandidateService.evaluate

    def controlled_evaluation(service: ShortPutCandidateService, symbol: str):
        nonlocal fail_next
        evaluate_calls.append(symbol)
        if fail_next:
            raise BrokerDataError("DU1234567 sensitive risk-refresh detail")
        broker = service._broker
        assert isinstance(broker, DemoBroker)
        broker._positions = ()
        broker._orders = ()
        broker._executions = ()
        service._settings = service._settings.model_copy(
            update={"max_expirations": 1, "max_strikes_per_expiration": 1}
        )
        return original_evaluate(service, symbol)

    async def reject_preview(_broker: DemoBroker, _request: OrderRequest) -> OrderPreview:
        unexpected_order_calls.append("preview")
        raise AssertionError("Risk refresh unexpectedly called preview_order")

    async def reject_submit(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("submit")
        raise AssertionError("Risk refresh unexpectedly called submit_order")

    async def reject_modify(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("modify")
        raise AssertionError("Risk refresh unexpectedly called modify_order")

    async def reject_cancel(_broker: DemoBroker, _broker_order_id: int) -> None:
        unexpected_order_calls.append("cancel")
        raise AssertionError("Risk refresh unexpectedly called cancel_order")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)
    monkeypatch.setattr(DemoBroker, "preview_order", reject_preview)
    monkeypatch.setattr(DemoBroker, "submit_order", reject_submit)
    monkeypatch.setattr(DemoBroker, "modify_order", reject_modify)
    monkeypatch.setattr(DemoBroker, "cancel_order", reject_cancel)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == []
        assert len(app.text_input) == 0
        assert len(app.button) == 1

        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        assert len(app.selectbox) == 2
        assert len(app.text_input) == 1
        assert len(app.button) == 2
        assert app.button[1].label == "Refresh evidence & calculate locked risk"
        assert app.button[1].disabled is True
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state

        app.text_input[0].set_value("0.65").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        assert app.button[1].disabled is False
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state

        app.button[1].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY in app.session_state
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Risk preview"] == "READY"
        assert metrics["Opening actions"] == "LOCKED"
        assert metrics["Fresh-bid credit / share"].endswith(" USD")
        assert metrics["Effective entry / share"].endswith(" USD")
        assert metrics["Total commission estimate"] == "0.65 USD"
        assert metrics["Total gross premium"].endswith(" USD")
        assert metrics["Total net premium"].endswith(" USD")
        assert metrics["Total assignment obligation"].endswith(" USD")
        assert metrics["Broker margin"] == "Unavailable — not requested"
        scenario_table = next(
            dataframe.value
            for dataframe in app.dataframe
            if "Reference point" in dataframe.value.columns
        )
        assert set(scenario_table["Opening actions"]) == {"LOCKED"}
        assert {
            "Observed Spot",
            "Strike",
            "Effective Entry",
            "Underlying Zero",
        } == set(scenario_table["Reference point"])
        assert all("USD" in value for value in scenario_table["Modeled expiration P&L"])
        assert all("$" not in dataframe.value.to_string() for dataframe in app.dataframe)
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)
        assert any("historical display only" in caption.value for caption in app.caption)
        assert any("EDT" in caption.value for caption in app.caption)

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL"]

        app.text_input[0].set_value("0.66").run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert "Risk preview" not in {metric.label for metric in app.metric}

        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert _RISK_PREVIEW_STATE_KEY in app.session_state

        fail_next = True
        app.button[1].click().run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Candidate result"] == "NOT_EVALUATED"
        assert metrics["Risk preview"] == "WITHHELD"
        assert "Fresh-bid credit / share" not in metrics
        assert "DEMO what-if" not in metrics
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state
        assert _DEMO_APPROVAL_STATE_KEY not in app.session_state
        assert not any(
            button.label
            in {
                "Refresh evidence & calculate locked risk",
                "Refresh evidence & run locked DEMO what-if",
                "Refresh evidence & rehearse locked DEMO approval",
            }
            for button in app.button
        )
        assert unexpected_order_calls == []
        assert any(
            "Fresh candidate evidence could not be obtained safely" in warning.value
            for warning in app.warning
        )
        assert "sensitive risk-refresh detail" not in str(app)
        assert "DU1234567" not in str(app)
        log_text = (tmp_path / "chronos.log").read_text(encoding="utf-8")
        assert '"error_type":"BrokerDataError"' in log_text
        assert "sensitive risk-refresh detail" not in log_text
        assert "DU1234567" not in log_text

        app.run(timeout=10)
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        assert {metric.label: metric.value for metric in app.metric}["Risk preview"] == "WITHHELD"
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert unexpected_order_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_demo_what_if_rehearsal_refreshes_terms_without_persistence_or_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    preview_calls: list[OrderRequest] = []
    unexpected_order_calls: list[str] = []
    original_evaluate = ShortPutCandidateService.evaluate
    original_preview = DemoBroker.preview_order

    def controlled_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    async def track_preview(broker: DemoBroker, request: OrderRequest) -> OrderPreview:
        preview_calls.append(request)
        return await original_preview(broker, request)

    async def reject_submit(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("submit")
        raise AssertionError("DEMO what-if unexpectedly called submit_order")

    async def reject_modify(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("modify")
        raise AssertionError("DEMO what-if unexpectedly called modify_order")

    async def reject_cancel(_broker: DemoBroker, _broker_order_id: int) -> None:
        unexpected_order_calls.append("cancel")
        raise AssertionError("DEMO what-if unexpectedly called cancel_order")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)
    monkeypatch.setattr(DemoBroker, "preview_order", track_preview)
    monkeypatch.setattr(DemoBroker, "submit_order", reject_submit)
    monkeypatch.setattr(DemoBroker, "modify_order", reject_modify)
    monkeypatch.setattr(DemoBroker, "cancel_order", reject_cancel)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert preview_calls == []
        assert len(app.text_input) == 2
        assert app.button[2].label == "Refresh evidence & run locked DEMO what-if"
        assert app.button[2].disabled is True
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state

        app.text_input[1].set_value("1e400").run(timeout=10)
        assert app.button[2].disabled is True
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert preview_calls == []

        app.text_input[1].set_value("3.20").run(timeout=10)
        assert app.button[2].disabled is False
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert preview_calls == []
        app.button[2].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        broker_request = preview_calls[0]
        assert broker_request.quantity == 1
        assert broker_request.limit_price == Decimal("3.20")
        assert broker_request.transmit is False
        assert broker_request.outside_rth is False
        assert _CANDIDATE_EVALUATION_STATE_KEY in app.session_state
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        assert _DEMO_WHAT_IF_STATE_KEY in app.session_state
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["DEMO what-if"] == "WHAT_IF_PREVIEWED"
        assert metrics["Progression"] == "STOPPED"
        assert metrics["Submission"] == "LOCKED"
        assert metrics["Exact limit / share"] == "3.20 USD"
        assert metrics["Broker commission estimate"] == "0.65 USD"
        assert metrics["Initial margin change"] == "0.00 USD"
        assert metrics["Exact-limit gross premium"] == "320.00 USD"
        assert metrics["Exact-limit net premium"] == "319.35 USD"
        what_if_table = next(
            dataframe.value
            for dataframe in app.dataframe
            if "What-if reference point" in dataframe.value.columns
        )
        assert set(what_if_table["Submission"]) == {"LOCKED"}
        assert all("$" not in dataframe.value.to_string() for dataframe in app.dataframe)
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)
        assert "DU1234567" not in (tmp_path / "chronos.log").read_text(encoding="utf-8")
        assert not any(
            "confirm" in button.label.lower() or "submit" in button.label.lower()
            for button in app.button
        )
        assert unexpected_order_calls == []

        runtime = get_runtime()
        with runtime.database.sessions() as session:
            for row_type in (
                WheelCycleRow,
                OrderDraftRow,
                OrderPreviewRow,
                SubmittedOrderRow,
                GuardrailDecisionRow,
            ):
                assert session.scalar(select(func.count()).select_from(row_type)) == 0

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert _DEMO_WHAT_IF_STATE_KEY in app.session_state

        app.text_input[1].set_value("3.21").run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state
        assert "DEMO what-if" not in {metric.label for metric in app.metric}

        app.text_input[0].set_value("0.66").run(timeout=10)
        app.button[1].click().run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert len(app.text_input) == 2
        assert app.text_input[1].value == ""
        assert app.button[2].disabled is True
        assert unexpected_order_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_withheld_demo_what_if_rerun_clears_parent_panels_and_retains_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    risk_preview_calls: list[ShortPutRiskPreviewRequest] = []
    broker_preview_calls: list[OrderRequest] = []
    unexpected_order_calls: list[str] = []
    fail_risk_refresh = False
    original_evaluate = ShortPutCandidateService.evaluate
    original_risk_preview = ShortPutRiskPreviewService.preview
    original_broker_preview = DemoBroker.preview_order

    def track_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    def controlled_risk_preview(
        service: ShortPutRiskPreviewService,
        request: ShortPutRiskPreviewRequest,
    ):
        risk_preview_calls.append(request)
        if fail_risk_refresh:
            raise BrokerDataError("DU1234567 sensitive what-if risk-refresh detail")
        return original_risk_preview(service, request)

    async def track_broker_preview(
        broker: DemoBroker,
        request: OrderRequest,
    ) -> OrderPreview:
        broker_preview_calls.append(request)
        return await original_broker_preview(broker, request)

    async def reject_submit(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("submit")
        raise AssertionError("Withheld what-if unexpectedly called submit_order")

    async def reject_modify(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("modify")
        raise AssertionError("Withheld what-if unexpectedly called modify_order")

    async def reject_cancel(_broker: DemoBroker, _broker_order_id: int) -> None:
        unexpected_order_calls.append("cancel")
        raise AssertionError("Withheld what-if unexpectedly called cancel_order")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", track_evaluation)
    monkeypatch.setattr(ShortPutRiskPreviewService, "preview", controlled_risk_preview)
    monkeypatch.setattr(DemoBroker, "preview_order", track_broker_preview)
    monkeypatch.setattr(DemoBroker, "submit_order", reject_submit)
    monkeypatch.setattr(DemoBroker, "modify_order", reject_modify)
    monkeypatch.setattr(DemoBroker, "cancel_order", reject_cancel)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)
        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)
        app.text_input[1].set_value("3.20").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert len(risk_preview_calls) == 1
        assert broker_preview_calls == []
        before_metrics = {metric.label: metric.value for metric in app.metric}
        assert before_metrics["Risk preview"] == "READY"
        assert app.button[2].label == "Refresh evidence & run locked DEMO what-if"

        fail_risk_refresh = True
        app.button[2].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert len(risk_preview_calls) == 2
        assert broker_preview_calls == []
        assert unexpected_order_calls == []
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert _DEMO_WHAT_IF_STATE_KEY in app.session_state
        assert _DEMO_APPROVAL_STATE_KEY not in app.session_state
        result = app.session_state[_DEMO_WHAT_IF_STATE_KEY]
        assert result.risk_refresh is None
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Candidate result"] == "NOT_EVALUATED"
        assert "Risk preview" not in metrics
        assert metrics["DEMO what-if"] == "WITHHELD"
        assert "Exact limit / share" not in metrics
        assert not any(
            button.label
            in {
                "Refresh evidence & calculate locked risk",
                "Refresh evidence & run locked DEMO what-if",
                "Refresh evidence & rehearse locked DEMO approval",
            }
            for button in app.button
        )
        assert not app.text_input
        assert not app.checkbox
        assert any(
            "Fresh risk evidence could not be obtained safely" in warning.value
            for warning in app.warning
        )
        assert "sensitive what-if risk-refresh detail" not in str(app)
        assert "DU1234567" not in str(app)
        log_text = (tmp_path / "chronos.log").read_text(encoding="utf-8")
        assert '"error_type":"BrokerDataError"' in log_text
        assert "sensitive what-if risk-refresh detail" not in log_text
        assert "DU1234567" not in log_text

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert len(risk_preview_calls) == 2
        assert broker_preview_calls == []
        assert _DEMO_WHAT_IF_STATE_KEY in app.session_state
        assert {metric.label: metric.value for metric in app.metric}["DEMO what-if"] == "WITHHELD"
        assert unexpected_order_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_demo_approval_rehearsal_is_memoryless_lineage_bound_and_nontransmitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    preview_calls: list[OrderRequest] = []
    approval_calls: list[ShortPutDemoApprovalRequest] = []
    unexpected_order_calls: list[str] = []
    original_evaluate = ShortPutCandidateService.evaluate
    original_preview = DemoBroker.preview_order
    original_rehearse = ShortPutDemoApprovalService.rehearse

    def track_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    async def track_preview(broker: DemoBroker, request: OrderRequest) -> OrderPreview:
        preview_calls.append(request)
        return await original_preview(broker, request)

    def track_approval(
        service: ShortPutDemoApprovalService,
        request: ShortPutDemoApprovalRequest,
    ):
        approval_calls.append(request)
        return original_rehearse(service, request)

    async def reject_submit(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("submit")
        raise AssertionError("DEMO approval rehearsal unexpectedly called submit_order")

    async def reject_modify(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("modify")
        raise AssertionError("DEMO approval rehearsal unexpectedly called modify_order")

    async def reject_cancel(_broker: DemoBroker, _broker_order_id: int) -> None:
        unexpected_order_calls.append("cancel")
        raise AssertionError("DEMO approval rehearsal unexpectedly called cancel_order")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", track_evaluation)
    monkeypatch.setattr(DemoBroker, "preview_order", track_preview)
    monkeypatch.setattr(ShortPutDemoApprovalService, "rehearse", track_approval)
    monkeypatch.setattr(DemoBroker, "submit_order", reject_submit)
    monkeypatch.setattr(DemoBroker, "modify_order", reject_modify)
    monkeypatch.setattr(DemoBroker, "cancel_order", reject_cancel)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)
        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)
        app.text_input[1].set_value("3.20").run(timeout=10)
        app.button[2].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert approval_calls == []
        assert len(app.text_input) == 4
        assert app.text_input[2].value == ""
        assert app.text_input[3].value == ""
        assert len(app.checkbox) == 1
        assert app.checkbox[0].value is False
        assert "conId 2002" in app.checkbox[0].label
        assert "3.20 USD" in app.checkbox[0].label
        assert "18,500.00 USD" in app.checkbox[0].label
        assert app.button[3].label == "Refresh evidence & rehearse locked DEMO approval"
        assert app.button[3].disabled is True
        assert _DEMO_APPROVAL_STATE_KEY not in app.session_state

        app.text_input[2].set_value("aapl").run(timeout=10)
        app.text_input[3].set_value("999999999999999999999999999999999").run(timeout=10)
        assert app.button[3].disabled is True
        assert approval_calls == []
        assert len(preview_calls) == 1

        app.text_input[2].set_value("AAPL").run(timeout=10)
        app.text_input[3].set_value("01").run(timeout=10)
        assert app.button[3].disabled is True
        assert approval_calls == []
        app.text_input[3].set_value("1").run(timeout=10)
        app.checkbox[0].set_value(True).run(timeout=10)
        assert app.button[3].disabled is False
        assert approval_calls == []
        app.button[3].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 2
        assert len(approval_calls) == 1
        approval_request = approval_calls[0]
        assert approval_request.symbol == "AAPL"
        assert approval_request.typed_symbol == "AAPL"
        assert approval_request.selected_contract_id == 2002
        assert approval_request.acknowledged_contract_id == 2002
        assert approval_request.acknowledged_quantity == 1
        assert approval_request.limit_price == Decimal("3.20")
        assert approval_request.acknowledged_limit_price == Decimal("3.20")
        assert approval_request.gross_assignment_obligation == Decimal("18500")
        assert approval_request.acknowledged_gross_assignment_obligation == Decimal("18500")
        assert approval_request.risk_terms_acknowledged is True
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state
        assert _DEMO_APPROVAL_STATE_KEY in app.session_state
        approval_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(approval_record, DemoApprovalSessionRecord)
        assert approval_record.disposition is DemoApprovalDisposition.RETAINED
        assert approval_record.receipt is not None
        serialized_approval = approval_record.model_dump_json()
        assert '"what_if_refresh"' not in serialized_approval
        assert '"risk_refresh"' not in serialized_approval
        assert '"broker_warning"' not in serialized_approval
        assert '"assumptions"' not in serialized_approval
        assert '"local_symbol"' not in serialized_approval
        assert '"trading_class"' not in serialized_approval
        assert "DU1234567" not in serialized_approval
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["Candidate result"] == "NOT_EVALUATED"
        assert "Risk preview" not in metrics
        assert "DEMO what-if" not in metrics
        assert metrics["DEMO approval rehearsal"] == "APPROVAL_REHEARSED"
        assert metrics["Receipt disposition"] == "RETAINED"
        assert metrics["Progression"] == "STOPPED"
        assert metrics["Submission"] == "LOCKED"
        assert metrics["Rehearsed contract"] == "2002"
        assert metrics["Rehearsed quantity"] == "1"
        assert metrics["Rehearsed exact limit / share"] == "3.20 USD"
        assert metrics["Rehearsed gross assignment obligation"] == "18,500.00 USD"
        assert unexpected_order_calls == []
        assert not app.text_input
        assert not app.checkbox
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)
        assert "DU1234567" not in (tmp_path / "chronos.log").read_text(encoding="utf-8")
        assert not any(
            "confirm" in button.label.lower() or "submit" in button.label.lower()
            for button in app.button
        )
        assert any(
            button.label == "Abandon historical DEMO rehearsal receipt" for button in app.button
        )

        runtime = get_runtime()
        assert isinstance(runtime.short_put_demo_approval, ShortPutDemoApprovalService)
        with runtime.database.sessions() as session:
            for row_type in (
                WheelCycleRow,
                OrderDraftRow,
                OrderPreviewRow,
                SubmittedOrderRow,
                GuardrailDecisionRow,
            ):
                assert session.scalar(select(func.count()).select_from(row_type)) == 0

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 2
        assert len(approval_calls) == 1
        assert _DEMO_APPROVAL_STATE_KEY in app.session_state
        rerun_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(rerun_record, DemoApprovalSessionRecord)
        assert rerun_record.approval_reference == approval_record.approval_reference
        assert rerun_record.receipt == approval_record.receipt
        assert (
            rerun_record.display_expires_at_monotonic
            == approval_record.display_expires_at_monotonic
        )
        assert rerun_record.last_observed_at_monotonic >= approval_record.last_observed_at_monotonic
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state
        assert {metric.label: metric.value for metric in app.metric}[
            "DEMO approval rehearsal"
        ] == "APPROVAL_REHEARSED"

        forged_session_record = approval_record.model_copy(update={"authority_created": True})
        app.session_state[_DEMO_APPROVAL_STATE_KEY] = forged_session_record
        app.run(timeout=10)
        assert not app.exception
        assert _DEMO_APPROVAL_STATE_KEY not in app.session_state
        assert "Rehearsed contract" not in {metric.label: metric.value for metric in app.metric}
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []

        portfolio_expiring_record = DemoApprovalSessionRecord.model_validate(
            {
                **approval_record.model_dump(),
                "retained_at_monotonic": 0.0,
                "last_observed_at_monotonic": 0.0,
                "display_expires_at_monotonic": 1.0,
            }
        )
        app.session_state[_DEMO_APPROVAL_STATE_KEY] = portfolio_expiring_record
        app.radio[0].set_value("Portfolio Dashboard").run(timeout=10)
        portfolio_expired_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(portfolio_expired_record, DemoApprovalSessionRecord)
        assert portfolio_expired_record.disposition is DemoApprovalDisposition.EXPIRED
        assert portfolio_expired_record.receipt is None
        assert "selected_contract" not in portfolio_expired_record.model_dump_json()
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []

        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.session_state[_DEMO_APPROVAL_STATE_KEY] = approval_record
        app.run(timeout=10)
        app.selectbox[0].set_value("MSFT").run(timeout=10)
        symbol_superseded_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(symbol_superseded_record, DemoApprovalSessionRecord)
        assert symbol_superseded_record.disposition is DemoApprovalDisposition.SUPERSEDED
        assert symbol_superseded_record.receipt is None
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []

        app.selectbox[0].set_value("AAPL").run(timeout=10)
        app.session_state[_DEMO_APPROVAL_STATE_KEY] = approval_record
        app.run(timeout=10)
        boundary_record = DemoApprovalSessionRecord.model_validate(
            {
                **approval_record.model_dump(),
                "retained_at_monotonic": 100.0,
                "last_observed_at_monotonic": 100.0,
                "display_expires_at_monotonic": 101.0,
            }
        )
        boundary_values = iter((100.999, 100.999, 100.999, 100.999, 101.0))
        boundary_calls: list[float] = []

        def boundary_clock() -> float:
            value = next(boundary_values, 101.0)
            boundary_calls.append(value)
            return value

        app.session_state[_DEMO_APPROVAL_STATE_KEY] = boundary_record
        with monkeypatch.context() as boundary_patch:
            boundary_patch.setattr("chronos.ui.dashboard.monotonic", boundary_clock)
            app.run(timeout=10)
        exact_deadline_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(exact_deadline_record, DemoApprovalSessionRecord)
        assert exact_deadline_record.disposition is DemoApprovalDisposition.EXPIRED
        assert exact_deadline_record.receipt is None
        assert len(boundary_calls) >= 5
        assert boundary_calls[:5] == [100.999, 100.999, 100.999, 100.999, 101.0]
        exact_deadline_metrics = {metric.label: metric.value for metric in app.metric}
        assert exact_deadline_metrics["DEMO approval rehearsal"] == "EXPIRED"
        assert exact_deadline_metrics["Receipt disposition"] == "EXPIRED"
        assert "Rehearsed contract" not in exact_deadline_metrics
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []

        app.session_state[_DEMO_APPROVAL_STATE_KEY] = approval_record
        app.run(timeout=10)
        abandon_button = next(
            button
            for button in app.button
            if button.label == "Abandon historical DEMO rehearsal receipt"
        )
        abandon_button.click().run(timeout=10)
        abandoned_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(abandoned_record, DemoApprovalSessionRecord)
        assert abandoned_record.disposition is DemoApprovalDisposition.ABANDONED
        assert abandoned_record.receipt is None
        assert "selected_contract" not in abandoned_record.model_dump_json()
        abandoned_metrics = {metric.label: metric.value for metric in app.metric}
        assert abandoned_metrics["DEMO approval rehearsal"] == "ABANDONED"
        assert abandoned_metrics["Receipt disposition"] == "ABANDONED"
        assert "Rehearsed contract" not in abandoned_metrics
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []

        app.session_state[_DEMO_APPROVAL_STATE_KEY] = approval_record
        app.button[0].click().run(timeout=10)
        superseded_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(superseded_record, DemoApprovalSessionRecord)
        assert superseded_record.disposition is DemoApprovalDisposition.SUPERSEDED
        assert superseded_record.receipt is None
        assert "selected_contract" not in superseded_record.model_dump_json()
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []
        superseded_metrics = {metric.label: metric.value for metric in app.metric}
        assert superseded_metrics["DEMO approval rehearsal"] == "SUPERSEDED"
        assert superseded_metrics["Receipt disposition"] == "SUPERSEDED"
        assert "Rehearsed contract" not in superseded_metrics

        expiring_record = DemoApprovalSessionRecord.model_validate(
            {
                **approval_record.model_dump(),
                "retained_at_monotonic": 0.0,
                "last_observed_at_monotonic": 0.0,
                "display_expires_at_monotonic": 1.0,
            }
        )
        app.session_state[_DEMO_APPROVAL_STATE_KEY] = expiring_record
        app.run(timeout=10)
        expired_record = app.session_state[_DEMO_APPROVAL_STATE_KEY]
        assert isinstance(expired_record, DemoApprovalSessionRecord)
        assert expired_record.disposition is DemoApprovalDisposition.EXPIRED
        assert expired_record.receipt is None
        assert "selected_contract" not in expired_record.model_dump_json()
        expired_metrics = {metric.label: metric.value for metric in app.metric}
        assert expired_metrics["DEMO approval rehearsal"] == "EXPIRED"
        assert expired_metrics["Receipt disposition"] == "EXPIRED"
        assert "Rehearsed contract" not in expired_metrics
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL", "AAPL", "AAPL"]
        assert len(approval_calls) == 1
        assert len(preview_calls) == 2
        assert unexpected_order_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_withheld_demo_approval_rerun_clears_parent_panels_and_retains_safe_feedback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    preview_calls: list[OrderRequest] = []
    approval_calls: list[ShortPutDemoApprovalRequest] = []
    unexpected_order_calls: list[str] = []
    original_evaluate = ShortPutCandidateService.evaluate
    original_preview = DemoBroker.preview_order
    original_rehearse = ShortPutDemoApprovalService.rehearse

    def track_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    async def track_preview(broker: DemoBroker, request: OrderRequest) -> OrderPreview:
        preview_calls.append(request)
        return await original_preview(broker, request)

    def track_approval(
        service: ShortPutDemoApprovalService,
        request: ShortPutDemoApprovalRequest,
    ):
        approval_calls.append(request)
        return original_rehearse(service, request)

    async def reject_submit(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("submit")
        raise AssertionError("Withheld DEMO approval unexpectedly called submit_order")

    async def reject_modify(_broker: DemoBroker, _request: object) -> None:
        unexpected_order_calls.append("modify")
        raise AssertionError("Withheld DEMO approval unexpectedly called modify_order")

    async def reject_cancel(_broker: DemoBroker, _broker_order_id: int) -> None:
        unexpected_order_calls.append("cancel")
        raise AssertionError("Withheld DEMO approval unexpectedly called cancel_order")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", track_evaluation)
    monkeypatch.setattr(DemoBroker, "preview_order", track_preview)
    monkeypatch.setattr(ShortPutDemoApprovalService, "rehearse", track_approval)
    monkeypatch.setattr(DemoBroker, "submit_order", reject_submit)
    monkeypatch.setattr(DemoBroker, "modify_order", reject_modify)
    monkeypatch.setattr(DemoBroker, "cancel_order", reject_cancel)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)
        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)
        app.text_input[1].set_value("3.20").run(timeout=10)
        app.button[2].click().run(timeout=10)
        app.text_input[2].set_value("AAPL").run(timeout=10)
        app.text_input[3].set_value("1").run(timeout=10)
        app.checkbox[0].set_value(True).run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert approval_calls == []
        pre_attempt_metrics = {metric.label: metric.value for metric in app.metric}
        assert pre_attempt_metrics["Risk preview"] == "READY"
        assert pre_attempt_metrics["DEMO what-if"] == "WHAT_IF_PREVIEWED"
        assert app.button[3].label == "Refresh evidence & rehearse locked DEMO approval"
        assert app.button[3].disabled is False

        runtime = get_runtime()
        runtime.short_put_demo_approval._settings = runtime.settings.model_copy(
            update={"broker_mode": BrokerMode.IBKR}
        )
        app.button[3].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert len(approval_calls) == 1
        assert unexpected_order_calls == []
        assert _CANDIDATE_EVALUATION_STATE_KEY not in app.session_state
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert _DEMO_WHAT_IF_STATE_KEY not in app.session_state
        assert _DEMO_APPROVAL_STATE_KEY not in app.session_state
        assert _DEMO_APPROVAL_FEEDBACK_STATE_KEY in app.session_state
        post_attempt_metrics = {metric.label: metric.value for metric in app.metric}
        assert "Risk preview" not in post_attempt_metrics
        assert "DEMO what-if" not in post_attempt_metrics
        assert post_attempt_metrics["DEMO approval rehearsal"] == "WITHHELD"
        assert not any(
            button.label == "Refresh evidence & rehearse locked DEMO approval"
            for button in app.button
        )
        assert not app.checkbox
        assert any(
            "bound DEMO adapter" in warning.value or "withheld safely" in warning.value
            for warning in app.warning
        )
        assert any("failed refresh was cleared" in caption.value for caption in app.caption)
        assert "DU1234567" not in str(app)
        assert "DU1234567" not in str(app.session_state.filtered_state)

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL", "AAPL"]
        assert len(preview_calls) == 1
        assert len(approval_calls) == 1
        assert _DEMO_APPROVAL_FEEDBACK_STATE_KEY in app.session_state
        assert unexpected_order_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_invalid_demo_boundaries_expose_no_what_if_or_approval_action_control() -> None:
    app = AppTest.from_string(
        """
from types import SimpleNamespace

from chronos.broker.demo import DemoBroker
from chronos.domain.enums import BrokerMode
from chronos.services.short_put_demo_what_if import DemoWhatIfStatus
from chronos.ui.dashboard import _render_short_put_demo_approval, _render_short_put_demo_what_if

nominal_success = SimpleNamespace(
    status=DemoWhatIfStatus.WHAT_IF_PREVIEWED,
    preview=object(),
    risk_refresh=object(),
)

class DemoBrokerSubclass(DemoBroker):
    pass

demo_broker = DemoBroker()
subclass_broker = DemoBrokerSubclass()
runtimes = (
    SimpleNamespace(
        settings=SimpleNamespace(broker_mode=BrokerMode.IBKR),
        broker=demo_broker,
        connection=SimpleNamespace(broker=demo_broker),
    ),
    SimpleNamespace(
        settings=SimpleNamespace(broker_mode=BrokerMode.DEMO),
        broker=demo_broker,
        connection=SimpleNamespace(broker=DemoBroker()),
    ),
    SimpleNamespace(
        settings=SimpleNamespace(broker_mode=BrokerMode.DEMO),
        broker=subclass_broker,
        connection=SimpleNamespace(broker=subclass_broker),
    ),
)
for runtime in runtimes:
    _render_short_put_demo_what_if(
        runtime,
        "AAPL",
        None,
        clear_rendered_evidence=lambda: None,
    )
    _render_short_put_demo_approval(
        runtime,
        "AAPL",
        nominal_success,
        clear_rendered_evidence=lambda: None,
    )
"""
    ).run(timeout=10)

    assert not app.exception
    assert any("real IBKR what-if" in message.value for message in app.error)
    assert not app.button


def test_risk_preview_rejects_unbounded_or_overprecise_commission_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    preview_calls: list[object] = []
    original_evaluate = ShortPutCandidateService.evaluate

    def controlled_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        broker = service._broker
        assert isinstance(broker, DemoBroker)
        broker._positions = ()
        broker._orders = ()
        broker._executions = ()
        service._settings = service._settings.model_copy(
            update={"max_expirations": 1, "max_strikes_per_expiration": 1}
        )
        return original_evaluate(service, symbol)

    def track_preview(_service: ShortPutRiskPreviewService, request: object):
        preview_calls.append(request)
        raise AssertionError("Invalid commission unexpectedly reached the preview service")

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)
    monkeypatch.setattr(ShortPutRiskPreviewService, "preview", track_preview)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL"]
        assert preview_calls == []

        for invalid_value in (
            "1e400",
            "10000.01",
            "0.00001",
            "1e-999999999999999999",
            "1.23000000000000000000",
            "000000000000000000000000000000001",
        ):
            app.text_input[0].set_value(invalid_value).run(timeout=10)

            assert not app.exception
            assert app.button[1].disabled is True
            assert evaluate_calls == ["AAPL"]
            assert preview_calls == []
            assert _RISK_PREVIEW_STATE_KEY not in app.session_state

        app.text_input[0].set_value("1.23000").run(timeout=10)
        assert not app.exception
        assert app.button[1].disabled is False
        assert evaluate_calls == ["AAPL"]
        assert preview_calls == []
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()


def test_disappeared_risk_contract_remains_visible_until_operator_changes_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    evaluate_calls: list[str] = []
    selected_contract_id: int | None = None
    original_evaluate = ShortPutCandidateService.evaluate

    def controlled_evaluation(service: ShortPutCandidateService, symbol: str):
        nonlocal selected_contract_id
        evaluate_calls.append(symbol)
        broker = service._broker
        assert isinstance(broker, DemoBroker)
        broker._positions = ()
        broker._orders = ()
        broker._executions = ()
        service._settings = service._settings.model_copy(
            update={"max_expirations": 1, "max_strikes_per_expiration": 1}
        )
        result = original_evaluate(service, symbol)
        assert result.resolution is not None
        assert len(result.resolution.candidates) == 1
        candidate = result.resolution.candidates[0]
        replacements = tuple(
            candidate.model_copy(
                update={
                    "contract": candidate.contract.model_copy(
                        update={
                            "con_id": candidate.contract.con_id + offset,
                            "local_symbol": f"{candidate.contract.local_symbol} R{offset}",
                        }
                    )
                }
            )
            for offset in (100, 200)
        )
        if selected_contract_id is None:
            selected_contract_id = candidate.contract.con_id
            initial_candidates = (candidate, *replacements)
            return result.model_copy(
                update={
                    "resolution": result.resolution.model_copy(
                        update={"candidates": initial_candidates}
                    )
                }
            )

        return result.model_copy(
            update={"resolution": result.resolution.model_copy(update={"candidates": replacements})}
        )

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)
        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)
        app.button[0].click().run(timeout=10)

        assert not app.exception
        assert selected_contract_id is not None
        assert app.selectbox[1].value == selected_contract_id
        app.text_input[0].set_value("0.65").run(timeout=10)
        app.button[1].click().run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _CANDIDATE_EVALUATION_STATE_KEY in app.session_state
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        assert {metric.label: metric.value for metric in app.metric}["Risk preview"] == ("WITHHELD")
        assert any(
            "selected contract is no longer uniquely eligible" in warning.value.lower()
            for warning in app.warning
        )
        assert app.selectbox[1].value != selected_contract_id

        app.run(timeout=10)
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _RISK_PREVIEW_STATE_KEY in app.session_state
        assert {metric.label: metric.value for metric in app.metric}["Risk preview"] == ("WITHHELD")

        assert len(app.selectbox[1].options) >= 2
        current_replacement = app.selectbox[1].value
        new_replacement = selected_contract_id + 200
        assert new_replacement != current_replacement
        app.selectbox[1].set_value(new_replacement).run(timeout=10)

        assert not app.exception
        assert evaluate_calls == ["AAPL", "AAPL"]
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
        assert "Risk preview" not in {metric.label for metric in app.metric}
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()

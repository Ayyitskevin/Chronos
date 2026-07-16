from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

from chronos.broker.base import BrokerDataError
from chronos.broker.demo import DemoBroker
from chronos.config.settings import get_settings
from chronos.domain.enums import DataQuality
from chronos.domain.models import OrderPreview, OrderRequest
from chronos.persistence.schema import (
    GuardrailDecisionRow,
    OrderDraftRow,
    OrderPreviewRow,
    SubmittedOrderRow,
    WheelCycleRow,
)
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.services.short_put_risk_preview import ShortPutRiskPreviewService
from chronos.strategy.strike_resolver import (
    CandidateRejection,
    CandidateRejectionCode,
    RejectedCandidate,
)
from chronos.ui.dashboard import (
    _CANDIDATE_EVALUATION_STATE_KEY,
    _DEMO_WHAT_IF_STATE_KEY,
    _RISK_PREVIEW_STATE_KEY,
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
    original_evaluate = ShortPutCandidateService.evaluate

    def track_evaluation(service: ShortPutCandidateService, symbol: str):
        evaluate_calls.append(symbol)
        return original_evaluate(service, symbol)

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", track_evaluation)

    try:
        app = AppTest.from_file("src/chronos/app.py").run(timeout=10)

        assert not app.exception
        assert [title.value for title in app.title] == ["Chronos"]
        assert app.radio[0].value == "Portfolio Dashboard"
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

        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        assert not app.exception
        assert evaluate_calls == []
        detail_metrics = {metric.label: metric.value for metric in app.metric}
        assert "Reconciliation" not in detail_metrics
        assert detail_metrics["Candidate result"] == "NOT_EVALUATED"
        assert detail_metrics["Candidate actions"] == "LOCKED"
        assert "Last" not in detail_metrics
        assert app.selectbox[0].options == ["AAPL", "MSFT", "SPY"]
        assert app.button[0].label == "Run read-only evaluation"
        app.run(timeout=10)
        assert evaluate_calls == []

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

    monkeypatch.setattr(ShortPutCandidateService, "evaluate", controlled_evaluation)

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
        assert {metric.label: metric.value for metric in app.metric}["Risk preview"] == "WITHHELD"
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
        assert _RISK_PREVIEW_STATE_KEY not in app.session_state
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


def test_non_demo_what_if_panel_exposes_no_action_control() -> None:
    app = AppTest.from_string(
        """
from types import SimpleNamespace

from chronos.ui.dashboard import _render_short_put_demo_what_if

_render_short_put_demo_what_if(SimpleNamespace(broker=object()), "AAPL", None)
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

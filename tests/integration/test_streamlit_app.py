from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from chronos.broker.base import BrokerDataError
from chronos.broker.demo import DemoBroker
from chronos.config.settings import get_settings
from chronos.domain.enums import DataQuality
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.strategy.strike_resolver import (
    CandidateRejection,
    CandidateRejectionCode,
    RejectedCandidate,
)
from chronos.ui.dashboard import _CANDIDATE_EVALUATION_STATE_KEY
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

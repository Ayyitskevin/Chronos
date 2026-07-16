from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from chronos.config.settings import get_settings
from chronos.ui.session import get_runtime


def test_demo_portfolio_and_symbol_pages_render_without_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))

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
        metric_labels = {metric.label for metric in app.metric}
        assert {"Last", "Bid", "Ask", "Data quality"} <= metric_labels
        assert all("$" not in str(metric.value) for metric in app.metric)
        assert app.selectbox[0].value == "AAPL"
    finally:
        try:
            get_runtime().close()
        finally:
            get_runtime.clear()
            get_settings.cache_clear()

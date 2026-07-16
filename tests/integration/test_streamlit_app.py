from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

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

        app.radio[0].set_value("Symbol Detail & Order Workspace").run(timeout=10)

        assert not app.exception
        metric_labels = {metric.label for metric in app.metric}
        assert {"Last", "Bid", "Ask", "Data quality"} <= metric_labels
        assert app.selectbox[0].value == "AAPL"
    finally:
        get_runtime().close()

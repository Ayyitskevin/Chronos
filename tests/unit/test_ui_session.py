from pathlib import Path

import pytest

from chronos.broker.demo import DEMO_NOW, DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import DataQuality
from chronos.ui import session


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
    finally:
        runtime.close()

    assert runtime.connection.running is False

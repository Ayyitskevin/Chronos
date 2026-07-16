"""Process-lifetime application resources reused across Streamlit reruns."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import streamlit as st

from chronos.broker.base import Broker
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker, demo_now
from chronos.broker.market_data import MarketDataManager
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import BrokerMode
from chronos.persistence.database import Database
from chronos.utils.logging import configure_logging


@dataclass(slots=True)
class AppRuntime:
    settings: Settings
    broker: Broker
    connection: BrokerConnectionManager
    market_data: MarketDataManager
    database: Database

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self.market_data.clear_cache()
            self.database.dispose()


def _build_runtime() -> AppRuntime:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file)
    database = Database(settings.database_url)
    connection: BrokerConnectionManager | None = None
    try:
        database.initialize()
        broker: Broker
        if settings.broker_mode is BrokerMode.DEMO:
            broker = DemoBroker()
        else:
            from chronos.broker.ibkr import IBKRBroker

            broker = IBKRBroker(settings)
        market_data = MarketDataManager(
            broker,
            max_quote_age=timedelta(seconds=settings.max_quote_age_seconds),
            clock=demo_now if isinstance(broker, DemoBroker) else None,
        )
        connection = BrokerConnectionManager(broker)
        connection.connect()
    except BaseException:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logging.getLogger("chronos.ui.session").exception(
                    "Broker cleanup failed after runtime startup error",
                    extra={"event": "startup_broker_cleanup_failed"},
                )
        try:
            database.dispose()
        except Exception:
            logging.getLogger("chronos.ui.session").exception(
                "Database cleanup failed after runtime startup error",
                extra={"event": "startup_database_cleanup_failed"},
            )
        raise
    runtime = AppRuntime(
        settings=settings,
        broker=broker,
        connection=connection,
        market_data=market_data,
        database=database,
    )
    atexit.register(runtime.close)
    return runtime


get_runtime = cast(
    Callable[[], AppRuntime],
    st.cache_resource(show_spinner="Starting the local Chronos runtime…")(_build_runtime),
)

"""Process-lifetime application resources reused across Streamlit reruns."""

from __future__ import annotations

import atexit
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import streamlit as st

from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DemoBroker
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import BrokerMode
from chronos.persistence.database import Database
from chronos.utils.logging import configure_logging


@dataclass(slots=True)
class AppRuntime:
    settings: Settings
    broker: DemoBroker
    connection: BrokerConnectionManager
    database: Database

    def close(self) -> None:
        self.connection.close()
        self.database.dispose()


def _build_runtime() -> AppRuntime:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file)
    if settings.broker_mode is not BrokerMode.DEMO:
        raise RuntimeError(
            "The IBKR adapter is not enabled in this milestone. Set BROKER_MODE=demo."
        )
    database = Database(settings.database_url)
    database.initialize()
    broker = DemoBroker()
    connection = BrokerConnectionManager(broker)
    connection.connect()
    runtime = AppRuntime(
        settings=settings,
        broker=broker,
        connection=connection,
        database=database,
    )
    atexit.register(runtime.close)
    return runtime


get_runtime = cast(
    Callable[[], AppRuntime],
    st.cache_resource(show_spinner="Starting the local Chronos runtime…")(_build_runtime),
)

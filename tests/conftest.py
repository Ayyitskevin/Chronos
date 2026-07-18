"""Shared deterministic test constants and process-wide safety tripwires."""

from datetime import UTC, datetime

import pytest

from chronos.broker.demo import DemoBroker

FIXED_NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)


@pytest.fixture
def demo_broker() -> DemoBroker:
    return DemoBroker(clock=lambda: FIXED_NOW)


@pytest.fixture(autouse=True, scope="session")
def _ambient_settings_never_live_capable() -> None:
    """ADR-0009 tripwire (session scope): a live `.env` must not leak into tests.

    Constructs a throwaway ``Settings()`` from the ambient environment — it
    deliberately does NOT call ``get_settings()``, which would populate the
    process-level lru_cache before tests monkeypatch their environments.
    """

    from chronos.config.settings import Settings

    try:
        ambient = Settings()
    except Exception:
        return  # an invalid ambient config cannot be live-capable
    if ambient.live_transmission_possible:  # pragma: no cover - tripwire
        pytest.fail(
            "SAFETY TRIPWIRE: ambient settings are live-capable inside pytest; "
            "a live .env leaked into the test environment"
        )


@pytest.fixture(autouse=True)
def _cached_settings_never_live_capable() -> object:
    """ADR-0009 tripwire (per test): cached process settings must never be
    live-capable. Runs AFTER the test and inspects the cache only when the
    test itself populated it — never populating it here (that would poison
    per-test environment monkeypatching)."""

    yield None
    from chronos.config.settings import get_settings

    if (
        get_settings.cache_info().currsize and get_settings().live_transmission_possible
    ):  # pragma: no cover - tripwire
        pytest.fail("SAFETY TRIPWIRE: process-cached settings became live-capable during this test")

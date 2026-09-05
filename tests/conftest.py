"""Shared deterministic test constants and process-wide safety tripwires."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from chronos.broker.demo import DemoBroker

FIXED_NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)


@pytest.fixture(autouse=True, scope="session")
def _deterministic_umask() -> Iterator[None]:
    """Pin the umask, because file modes are now load-bearing (ADR-0053).

    The mandate and proposer-registry loaders read through
    ``AuthorityMode.GRANT``, which refuses a group- or other-writable grant.
    A fixture that writes a grant with plain ``write_text`` inherits the
    developer's umask: 0644 under the common 022, but **0664 under 002** — the
    Debian/Ubuntu per-user-group default — which the loader then correctly
    refuses. Without this pin the suite passes in CI and fails on a developer's
    machine for reasons that have nothing to do with the change under test.

    This does not hide the rule it makes room for: every mode case has its own
    test in ``tests/safety/test_authority_file_contract.py``, each chmodding
    explicitly rather than relying on whatever the umask happened to be.
    """

    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


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

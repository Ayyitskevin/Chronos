"""Shared deterministic test constants."""

from datetime import UTC, datetime

import pytest

from chronos.broker.demo import DemoBroker

FIXED_NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)


@pytest.fixture
def demo_broker() -> DemoBroker:
    return DemoBroker(clock=lambda: FIXED_NOW)

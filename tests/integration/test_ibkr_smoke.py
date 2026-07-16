"""Read-only IBKR smoke contract; implemented in Milestone 2."""

import pytest

pytestmark = [
    pytest.mark.ibkr,
    pytest.mark.skip(reason="IBKR read-only adapter arrives in Milestone 2"),
]


def test_ibkr_read_only_smoke_placeholder() -> None:
    """Never place an order from this test."""

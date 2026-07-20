"""Paper-path market data quality gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chronos.paperops.data_quality import QuoteSnapshot, evaluate_paper_quote
from chronos.paperops.reasons import PaperReasonCode

NOW = datetime(2024, 6, 3, 15, 0, 0, tzinfo=UTC)


def _quote(**overrides: object) -> QuoteSnapshot:
    base: dict[str, object] = {
        "symbol": "SPY",
        "bid": 100.0,
        "ask": 100.1,
        "last": 100.05,
        "quote_utc": NOW - timedelta(seconds=1),
        "source": "ibkr",
        "quality_label": "LIVE",
    }
    base.update(overrides)
    return QuoteSnapshot(**base)  # type: ignore[arg-type]


def test_live_fresh_quote_may_authorize() -> None:
    health = evaluate_paper_quote(_quote(), now_utc=NOW, max_quote_age_seconds=5)
    assert health.may_authorize_open is True
    assert health.reason_code is PaperReasonCode.DATA_OK


def test_stale_quote_blocks() -> None:
    health = evaluate_paper_quote(
        _quote(quote_utc=NOW - timedelta(seconds=60)),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_STALE


def test_missing_quote_blocks() -> None:
    health = evaluate_paper_quote(
        _quote(quote_utc=None, source="missing"),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_MISSING


def test_crossed_market_blocks() -> None:
    health = evaluate_paper_quote(
        _quote(bid=101.0, ask=100.0),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_CROSSED


def test_nonsensical_price_blocks() -> None:
    health = evaluate_paper_quote(
        _quote(last=float("nan")),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_NONSENSICAL


def test_invalid_greeks_block_when_required() -> None:
    health = evaluate_paper_quote(
        _quote(require_greeks=True, iv=-0.1, delta=0.3, gamma=0.01, theta=-0.01, vega=0.1),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_INVALID_GREEKS


def test_missing_greeks_block_when_required() -> None:
    health = evaluate_paper_quote(
        _quote(require_greeks=True, iv=None, delta=None),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_INVALID_GREEKS


def test_future_quote_is_clock_anomaly() -> None:
    health = evaluate_paper_quote(
        _quote(quote_utc=NOW + timedelta(seconds=30)),
        now_utc=NOW,
        max_quote_age_seconds=60,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_CLOCK_ANOMALY


def test_naive_timestamp_is_clock_anomaly() -> None:
    # Intentionally naive (no tzinfo) — ruff DTZ001 is the point under test.
    naive = datetime(2024, 6, 3, 15, 0, 0)  # noqa: DTZ001
    health = evaluate_paper_quote(
        _quote(quote_utc=naive),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.reason_code is PaperReasonCode.DATA_CLOCK_ANOMALY


@pytest.mark.parametrize("label", ["DEMO", "SYNTHETIC", "DELAYED", "STALE", "UNKNOWN", "FROZEN"])
def test_degraded_labels_never_authorize_open(label: str) -> None:
    health = evaluate_paper_quote(
        _quote(quality_label=label),
        now_utc=NOW,
        max_quote_age_seconds=5,
    )
    assert health.may_authorize_open is False
    assert health.label == label

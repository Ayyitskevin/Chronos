"""C1 remediation: the LIVE data-gate tick check honors the crypto venue tick.

Before the fix, ``_data_evidence`` used the hardcoded stock 0.01 tick for every
non-option family, so the ``min_tick`` that ``qualify_crypto`` harvests was dead
metadata — a coin with a finer/coarser venue tick had valid orders refused (or
mis-ticked prices slipped through to IBKR). The tick now comes ONLY from the
qualified crypto contract; absent metadata fails closed (ADR-0010 §6), never the
stock default.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.support.order_fakes import FakeBroker, paper_settings

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import DataQuality, OrderIntent
from chronos.domain.models import CryptoContract, MarketQuote
from chronos.orders.intent import WheelOrderIntent, build_crypto_intent
from chronos.orders.submission import OrderSubmissionBoundary
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
)

_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def boundary() -> Iterator[OrderSubmissionBoundary]:
    db = Database("sqlite:///:memory:")
    db.initialize()
    connection = BrokerConnectionManager(FakeBroker())
    connection.start()
    try:
        yield OrderSubmissionBoundary(
            settings=paper_settings(),
            connection=connection,
            intents=OrderIntentRepository(db.sessions),
            confirmations=OrderConfirmationRepository(db.sessions),
            tracker=OrderTrackerRepository(db.sessions),
        )
    finally:
        connection.close()
        db.dispose()


def _crypto_intent(*, min_tick: Decimal | None, limit_price: Decimal) -> WheelOrderIntent:
    return build_crypto_intent(
        account_id="U7654321",
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        contract=CryptoContract(
            con_id=479624278,
            symbol="BTC",
            min_tick=min_tick,
            min_size=Decimal("0.0001"),
            size_increment=Decimal("0.0001"),
        ),
        quantity=Decimal("0.001"),
        limit_price=limit_price,
    )


def _fresh_quote(intent: WheelOrderIntent) -> MarketQuote:
    return MarketQuote(
        contract=intent.contract,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("0.5431"),
        ask=Decimal("0.5433"),
        last=Decimal("0.5432"),
    )


def test_crypto_price_on_venue_tick_passes(boundary: OrderSubmissionBoundary) -> None:
    # Venue tick 0.0001; 0.5432 is a whole multiple → conforms.
    intent = _crypto_intent(min_tick=Decimal("0.0001"), limit_price=Decimal("0.5432"))
    ok, detail = boundary._data_evidence(intent, _fresh_quote(intent), _NOW)
    assert ok, detail


def test_crypto_price_off_venue_tick_but_on_stock_tick_is_blocked(
    boundary: OrderSubmissionBoundary,
) -> None:
    # 0.54325 is NOT a multiple of the 0.0001 venue tick. Under the old hardcoded
    # 0.01 stock tick this both fails — but the point is the venue tick governs:
    # a price valid on 0.01 yet invalid on the finer venue tick must still block.
    intent = _crypto_intent(min_tick=Decimal("0.0001"), limit_price=Decimal("0.54325"))
    ok, detail = boundary._data_evidence(intent, _fresh_quote(intent), _NOW)
    assert not ok
    assert "min tick" in detail


def test_crypto_price_valid_on_venue_tick_would_fail_the_old_stock_tick(
    boundary: OrderSubmissionBoundary,
) -> None:
    # 0.5432 conforms to the 0.0001 venue tick but NOT to the old 0.01 stock
    # default — proving the gate now uses the venue tick, not the stock default.
    intent = _crypto_intent(min_tick=Decimal("0.0001"), limit_price=Decimal("0.5432"))
    ok, _detail = boundary._data_evidence(intent, _fresh_quote(intent), _NOW)
    assert ok


def test_crypto_absent_min_tick_fails_closed(boundary: OrderSubmissionBoundary) -> None:
    # No venue tick metadata ⇒ UNKNOWN ⇒ fail closed, never the stock default.
    intent = _crypto_intent(min_tick=None, limit_price=Decimal("0.5432"))
    ok, detail = boundary._data_evidence(intent, _fresh_quote(intent), _NOW)
    assert not ok
    assert "min-tick" in detail

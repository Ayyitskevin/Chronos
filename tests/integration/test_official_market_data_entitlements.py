"""B1 U-b: the smoke's market-data read path over the real OfficialIBKRBroker and FakeGateway.

``tests/integration/test_ibkr_smoke.py`` is the only end-to-end read of market data, and it has
never run (it needs a gateway). This suite runs the same path (connect, account, qualify the
first symbol, the option-chain parameters, one underlying snapshot quote) through the unmodified
adapter and the real ``MarketDataManager``, then drives every entitlement, pacing and capacity
answer the bridge classifies (``callbacks.py:47-51``) and asserts that each one becomes an
EXPLICIT missing-data state: a typed error carrying the broker code, or a quote whose quality
says what it is, never a silent success. Scope is the smoke's path only (DESIGN r1 U-b); the
capture harness's wider read set is U-f.

The gateway answers come from knobs on ``tests/support/fake_ibapi.py``. The codes' meanings
(354 not subscribed, 10197 competing live session, 10167 delayed notice, 200 no definition)
and the field semantics in the two I12 characterizations are ASSUMED-IB: the author's reading
of the TWS API, to be checked at K4. Real-adapter gaps are strict xfails with a ``B1R-F`` id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from tests.support import fake_ibapi
from tests.support.order_fakes import option_contract

from chronos.broker.base import BrokerDataError
from chronos.broker.market_data import (
    MarketDataManager,
    MarketDataPacingExhaustedError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import DataQuality
from chronos.domain.models import UnderlyingContract

PERMISSION_CODES = (354, 10089, 10090, 10091, 10186, 10189, 10197)
AAPL_CON_ID = 999


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    try:
        yield seeded
    finally:
        seeded.release_readers()


class _Sleeps:
    """The manager's injected sleeper: records the backoff, never really waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _requests(gateway: fake_ibapi.FakeGateway, method: str) -> int:
    return sum(1 for _, name in gateway.transcript if name == method)


async def _connected() -> OfficialIBKRBroker:
    broker = OfficialIBKRBroker(fake_ibapi.official_settings())
    await broker.connect()
    return broker


async def _aapl(broker: OfficialIBKRBroker) -> UnderlyingContract:
    return await broker.qualify_underlying("AAPL")


# --- 1. the smoke's path, end to end --------------------------------------------------------


async def test_the_smoke_read_path_runs_end_to_end_over_the_real_adapter(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """The smoke's own sequence and assertions (test_ibkr_smoke.py:56-107), over the fake."""

    broker = await _connected()
    market_data = MarketDataManager(broker)
    try:
        status = await broker.connection_status()
        server_time = await broker.server_time()
        account = await broker.account_summary()
        underlying = await broker.qualify_underlying("AAPL")
        chains = await market_data.option_chain_parameters(underlying, force_refresh=True)
        managed = await market_data.underlying_quote(underlying, force_refresh=True)
    finally:
        await broker.disconnect()
    assert status.connected is True
    assert server_time.tzinfo is not None
    assert account.account_id == fake_ibapi.ACCOUNT
    assert (underlying.symbol, underlying.con_id) == ("AAPL", AAPL_CON_ID)
    assert underlying.liquid_hours  # the session evidence ContractDetails carries
    populated = [
        chain
        for chain in chains.parameters
        if chain.expirations and chain.strikes and chain.underlying_con_id == AAPL_CON_ID
    ]
    assert populated
    assert date(2026, 8, 21) in populated[0].expirations
    assert Decimal("150") in populated[0].strikes
    quote = managed.quote
    assert quote.contract.con_id == AAPL_CON_ID
    assert quote.data_quality is DataQuality.LIVE
    assert any(value is not None for value in (quote.bid, quote.ask, quote.last, quote.close))
    # the whole path is reads: no order call ever reached the gateway
    assert {name for _, name in gateway.transcript}.isdisjoint({"placeOrder", "cancelOrder"})


async def test_an_unknown_symbol_is_an_explicit_no_definition_error(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = await _connected()
    try:
        with pytest.raises(BrokerDataError) as raised:
            await broker.qualify_underlying("ZZZZ")
    finally:
        await broker.disconnect()
    assert raised.value.broker_error_code == fake_ibapi.CODE_NO_SECURITY  # type: ignore[attr-defined]
    assert str(fake_ibapi.CODE_NO_SECURITY) in str(raised.value)


# --- 2. entitlements: explicit, typed, and never retried --------------------------------------


@pytest.mark.parametrize("code", PERMISSION_CODES)
async def test_a_missing_entitlement_is_a_typed_error_with_its_code_and_is_not_retried(
    gateway: fake_ibapi.FakeGateway, code: int
) -> None:
    gateway.entitlement[AAPL_CON_ID] = code
    sleeps = _Sleeps()
    broker = await _connected()
    market_data = MarketDataManager(broker, sleep=sleeps)
    try:
        underlying = await _aapl(broker)
        with pytest.raises(MarketDataPermissionError) as raised:
            await market_data.underlying_quote(underlying, force_refresh=True)
    finally:
        await broker.disconnect()
    assert raised.value.broker_error_code == code  # type: ignore[attr-defined]
    assert str(code) in str(raised.value)
    assert _requests(gateway, "reqMktData") == 1
    assert sleeps.delays == []


async def test_a_refused_quote_is_never_served_from_cache(gateway: fake_ibapi.FakeGateway) -> None:
    gateway.entitlement[AAPL_CON_ID] = 354
    broker = await _connected()
    market_data = MarketDataManager(broker, sleep=_Sleeps())
    try:
        underlying = await _aapl(broker)
        for _ in range(2):
            with pytest.raises(MarketDataPermissionError):
                await market_data.underlying_quote(underlying)
    finally:
        await broker.disconnect()
    assert _requests(gateway, "reqMktData") == 2  # asked again, not a cached "answer"


# --- 3. pacing and capacity ---------------------------------------------------------------


@pytest.mark.parametrize("code", [100, 420])
async def test_pacing_is_retried_with_bounded_backoff_then_succeeds(
    gateway: fake_ibapi.FakeGateway, code: int
) -> None:
    gateway.entitlement[AAPL_CON_ID] = [code, code, 1]
    sleeps = _Sleeps()
    broker = await _connected()
    market_data = MarketDataManager(broker, sleep=sleeps)
    try:
        managed = await market_data.underlying_quote(await _aapl(broker), force_refresh=True)
    finally:
        await broker.disconnect()
    assert managed.quote.data_quality is DataQuality.LIVE
    assert sleeps.delays == [0.25, 0.5]
    assert _requests(gateway, "reqMktData") == 3


async def test_persistent_pacing_is_an_explicit_exhausted_error(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.entitlement[AAPL_CON_ID] = [420]
    sleeps = _Sleeps()
    broker = await _connected()
    market_data = MarketDataManager(broker, sleep=sleeps)
    try:
        underlying = await _aapl(broker)
        with pytest.raises(MarketDataPacingExhaustedError):
            await market_data.underlying_quote(underlying, force_refresh=True)
    finally:
        await broker.disconnect()
    assert _requests(gateway, "reqMktData") == 3  # max_attempts, not more
    assert sleeps.delays == [0.25, 0.5]


async def test_a_capacity_refusal_is_an_explicit_unavailable_error(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.entitlement[AAPL_CON_ID] = 101
    sleeps = _Sleeps()
    broker = await _connected()
    market_data = MarketDataManager(broker, sleep=sleeps)
    try:
        underlying = await _aapl(broker)
        with pytest.raises(MarketDataUnavailableError) as raised:
            await market_data.underlying_quote(underlying, force_refresh=True)
    finally:
        await broker.disconnect()
    assert raised.value.broker_error_code == 101  # type: ignore[attr-defined]
    assert _requests(gateway, "reqMktData") == 1 and sleeps.delays == []


# --- 4. the quality a quote carries ---------------------------------------------------------


@pytest.mark.parametrize(
    ("market_data_type", "quality"),
    [
        (1, DataQuality.LIVE),
        (2, DataQuality.FROZEN),
        (3, DataQuality.DELAYED),
        (4, DataQuality.DELAYED_FROZEN),
        (9, DataQuality.UNKNOWN),
    ],
)
async def test_the_quote_carries_the_quality_the_gateway_reported(
    gateway: fake_ibapi.FakeGateway, market_data_type: int, quality: DataQuality
) -> None:
    gateway.entitlement[AAPL_CON_ID] = market_data_type
    broker = await _connected()
    try:
        quote = await broker.request_underlying_quote(await _aapl(broker))
    finally:
        await broker.disconnect()
    assert quote.data_quality is quality


async def test_a_snapshot_with_no_quality_report_reads_unknown(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.entitlement[AAPL_CON_ID] = "no_type"
    broker = await _connected()
    try:
        quote = await broker.request_underlying_quote(await _aapl(broker))
    finally:
        await broker.disconnect()
    assert quote.data_quality is DataQuality.UNKNOWN
    assert quote.bid is not None  # the prices came; only their quality is unknown


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "B1R-F5 (B1R-F4 in build-ub; renumbered in build-ua-r1, which gives F4 to the "
        "two-step cancel): a snapshot that ends (tickSnapshotEnd) with a LIVE quality report "
        "and NO price ticks comes back as a LIVE quote whose bid/ask/last/close are all None "
        "(official_ibkr.py:2003-2063 builds it from whatever state arrived). The quality says "
        "LIVE; the data is absent. Fail-closed would be MarketDataUnavailableError (or a "
        "non-LIVE quality). The smoke's own any-value check would catch it there; other callers "
        "must each re-check. ASSUMED-IB that a gateway ever sends this; owner call."
    ),
)
async def test_a_live_snapshot_with_no_prices_is_not_a_live_quote(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.entitlement[AAPL_CON_ID] = "empty"
    broker = await _connected()
    try:
        try:
            quote = await broker.request_underlying_quote(await _aapl(broker))
        except MarketDataUnavailableError:
            return  # explicit: what fail-closed looks like
    finally:
        await broker.disconnect()
    assert quote.data_quality is not DataQuality.LIVE


# --- 5. id-less refusals and notices --------------------------------------------------------


async def test_an_idless_permission_error_fails_the_request_in_flight(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """A classified code with reqId -1 fails every in-flight request (callbacks.py:378-396)."""

    gateway.entitlement[AAPL_CON_ID] = "hang"
    broker = await _connected()
    try:
        underlying = await _aapl(broker)
        pending = asyncio.ensure_future(broker.request_underlying_quote(underlying))
        while _requests(gateway, "reqMktData") == 0:
            await asyncio.sleep(0.01)
        await asyncio.get_running_loop().run_in_executor(
            None, gateway.emit_error, -1, 10197, "No market data during competing live session"
        )
        with pytest.raises(MarketDataPermissionError) as raised:
            await asyncio.wait_for(pending, timeout=5)
    finally:
        await broker.disconnect()
    assert raised.value.broker_error_code == 10197  # type: ignore[attr-defined]
    assert any(note.startswith("[10197]") for note in broker.bridge.notices)


async def test_a_delayed_data_notice_fails_the_request_today(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Characterization, not a finding (K4 #6): 10167 'displaying delayed market data' is in no
    classified set, so with its reqId it fails the request as a PLAIN BrokerDataError and the
    delayed ticks that follow are dropped. Under today's reqMarketDataType(1) a gateway is
    believed (ASSUMED-IB) to answer 354 instead; this matters only if a delayed fallback is
    ever chosen."""

    gateway.entitlement[AAPL_CON_ID] = ("notice_then", 10167, 3)
    broker = await _connected()
    try:
        with pytest.raises(BrokerDataError) as raised:
            await broker.request_underlying_quote(await _aapl(broker))
    finally:
        await broker.disconnect()
    assert type(raised.value) is BrokerDataError
    assert raised.value.broker_error_code == 10167  # type: ignore[attr-defined]


# --- 6. two normalizer assumptions a real account exercises (I12; characterizations) --------


async def test_an_account_summary_in_two_currencies_keeps_the_last_row(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Characterization (K4 #8, ASSUMED-IB that IB ever sends this for these tags):
    account_summary_from_rows overwrites a tag per row and keeps the LAST row's currency
    (official_ibkr.py:654-690). Pinned so a change is seen; a finding only if K4 shows it."""

    gateway.extra_summary_rows = [("NetLiquidation", "92000.00", "EUR")]
    broker = await _connected()
    try:
        summary = await broker.account_summary()
    finally:
        await broker.disconnect()
    assert summary.net_liquidation == Decimal("92000.00")
    assert summary.currency == "EUR"


async def test_an_option_position_average_cost_is_stored_as_reported(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Characterization (K4 #9, ASSUMED-IB): IB is believed to report an option's avgCost per
    CONTRACT (price x multiplier). position_from_row stores it unscaled
    (official_ibkr.py:752-766), so a 1.50 fill reads as average_cost 150."""

    gateway.extra_positions = [(option_contract(), Decimal("-1"), 150.0)]
    broker = await _connected()
    try:
        positions = await broker.positions()
    finally:
        await broker.disconnect()
    (option,) = [p for p in positions if p.contract.con_id == option_contract().con_id]
    assert option.average_cost == Decimal("150.0")

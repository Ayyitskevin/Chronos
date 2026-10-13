"""B1 U-f1: the FakeGateway additions the capture workload needs, each with its own pin.

U-f2 runs ``capture_readonly.py`` and ``replay_check.py`` over the real adapter and this fake; it
can only be believed if the fake answers the capture's reads deterministically and in the shapes
the adapter reads. Each block below pins one addition, red first: ``reqMarketRule``; option
contract details (liquid hours, time-zone id, min tick, deliverable evidence, market-rule ids);
option ticks with ``tickOptionComputation``; ``reqHistoricalData`` daily bars; and a capture-day
book (positions, executions and open orders).

Every value the fake emits is the author's reading of the TWS API: a green run proves the real
adapter against that model, not against IB. The ASSUMED-IB list (the K4 / B1-day verification
items) is the module docstring of ``tests/support/fake_ibapi.py`` and RESULT.md. Nothing here
transmits an order and nothing opens a socket.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from tests.support import fake_ibapi

from chronos.broker.base import BrokerError
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import OptionContract, OptionContractSpec
from chronos.marketdata.bars import BarInterval, BarStatus

NOV = date(2026, 11, 20)
DEC = date(2026, 12, 18)
PUT_100_NOV = 211  # the capture day's con ids (fake_ibapi.CAPTURE_OPTION_CON_IDS)
CALL_100_NOV = 214


# --- harness -------------------------------------------------------------------------------------


@pytest.fixture
def day(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    yield gateway


def _broker() -> OfficialIBKRBroker:
    return OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )


def _plain(value: Any) -> Any:
    """A comparable rendering of callback arguments (the stub objects have no ``__eq__``)."""

    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if hasattr(value, "__dict__"):
        return {name: _plain(item) for name, item in sorted(vars(value).items())}
    return value


def _drive(gateway: fake_ibapi.FakeGateway, script: Any) -> list[tuple[str, Any]]:
    """Run ``script(client)`` on a raw client and return every dispatched callback, plainly."""

    recorder = fake_ibapi.RecordingWrapper()
    client = fake_ibapi.FakeEClient.bound_to(gateway)(recorder)
    client.connect("127.0.0.1", 7497, fake_ibapi.CLIENT_ID)
    reader = threading.Thread(target=client.run, daemon=True)
    reader.start()
    try:
        script(client)
        client.wait_dispatched()
    finally:
        client.disconnect()
        reader.join(timeout=5)
    session = {"nextValidId", "managedAccounts", "connectionClosed"}  # handshake and teardown
    return [(name, _plain(args)) for name, args in recorder.calls if name not in session]


def _names(calls: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in calls]


def _spec(
    strike: str,
    right: OptionRight = OptionRight.PUT,
    expiration: date = NOV,
) -> OptionContractSpec:
    return OptionContractSpec(
        symbol="AAPL",
        expiration=expiration,
        strike=Decimal(strike),
        right=right,
        multiplier=Decimal("100"),
        trading_class="AAPL",
    )


async def _qualified(adapter: OfficialIBKRBroker, *specs: OptionContractSpec) -> tuple[Any, ...]:
    return await adapter.qualify_option_contracts(list(specs))


# --- reqMarketRule -------------------------------------------------------------------------------


def test_req_market_rule_answers_with_the_rules_increment_bands() -> None:
    gateway = fake_ibapi.FakeGateway.capture_day()
    calls = _drive(gateway, lambda client: client.reqMarketRule(fake_ibapi.OPTION_RULE_ID))
    assert _names(calls) == ["marketRule"]
    rule_id, increments = calls[0][1]
    assert rule_id == fake_ibapi.OPTION_RULE_ID
    assert [(row["lowEdge"], row["increment"]) for row in increments] == [(0.0, 0.01), (3.0, 0.05)]


def test_req_market_rule_is_deterministic_across_gateways() -> None:
    def ask(client: Any) -> None:
        client.reqMarketRule(fake_ibapi.OPTION_RULE_ID)
        client.reqMarketRule(fake_ibapi.OPTION_RULE_ID)

    first = _drive(fake_ibapi.FakeGateway.capture_day(), ask)
    second = _drive(fake_ibapi.FakeGateway.capture_day(), ask)
    assert first == second
    assert _names(first) == ["marketRule", "marketRule"]


def test_an_unknown_market_rule_id_is_refused_loudly_not_invented() -> None:
    """ASSUMED-IB (K4): what the real gateway does for an unknown rule id is not known."""

    gateway = fake_ibapi.FakeGateway.capture_day()
    client = fake_ibapi.FakeEClient.bound_to(gateway)(fake_ibapi.RecordingWrapper())
    client.connect("127.0.0.1", 7497, fake_ibapi.CLIENT_ID)
    try:
        with pytest.raises(AssertionError, match="market rule"):
            client.reqMarketRule(424242)
    finally:
        client.disconnect()


async def test_the_adapter_reads_a_complete_schedule_through_the_fake(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
        (rule,) = await adapter.option_market_rules([option])
    finally:
        await adapter.disconnect()
    assert rule.con_id == option.con_id == PUT_100_NOV
    assert (rule.exchange, rule.market_rule_id) == ("SMART", fake_ibapi.OPTION_RULE_ID)
    assert [(p.low_edge, p.increment) for p in rule.price_increments] == [
        (Decimal("0"), Decimal("0.01")),
        (Decimal("3.0"), Decimal("0.05")),
    ]
    assert "reqMarketRule" in [m for _c, m in day.transcript]


# --- option contract details ---------------------------------------------------------------------


async def test_option_qualification_returns_the_option_and_its_evidence(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
    finally:
        await adapter.disconnect()
    assert isinstance(option, OptionContract)  # not the underlying (the old OPT-by-symbol bug)
    assert (option.con_id, option.right, option.strike, option.expiration) == (
        PUT_100_NOV,
        OptionRight.PUT,
        Decimal("100"),
        NOV,
    )
    assert option.min_tick == Decimal("0.01")
    assert option.time_zone_id == "US/Eastern"
    assert option.liquid_hours and "CLOSED" not in option.liquid_hours
    # deliverable evidence: the fields the adapter reads off the DETAILS, not the contract
    assert option.deliverable_verified is True
    assert (option.underlying_con_id, option.deliverable_shares) == (999, Decimal("100"))


async def test_a_call_and_a_put_qualify_to_their_own_con_ids(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        put, call = await _qualified(adapter, _spec("100"), _spec("100", OptionRight.CALL))
    finally:
        await adapter.disconnect()
    assert (put.con_id, call.con_id) == (PUT_100_NOV, CALL_100_NOV)
    assert (put.right, call.right) == (OptionRight.PUT, OptionRight.CALL)
    assert put.local_symbol != call.local_symbol


async def test_the_conid_query_the_market_rule_read_makes_returns_exactly_one_detail(
    day: fake_ibapi.FakeGateway,
) -> None:
    def ask(client: Any) -> None:
        query = fake_ibapi.FakeContract()
        query.conId, query.exchange = PUT_100_NOV, "SMART"
        client.reqContractDetails(7, query)

    calls = _drive(day, ask)
    assert _names(calls) == ["contractDetails", "contractDetailsEnd"]
    details = calls[0][1][1]
    assert details["contract"]["conId"] == PUT_100_NOV
    exchanges = details["validExchanges"].split(",")
    rule_ids = details["marketRuleIds"].split(",")
    assert len(exchanges) == len(rule_ids) and "SMART" in exchanges  # aligned, as the adapter needs
    assert details["underSecType"] == "STK" and details["underConId"] == 999


@pytest.mark.parametrize(
    ("override", "expect"),
    [
        ({"liquidHours": ""}, "no_session"),
        ({"timeZoneId": ""}, "no_session"),
        ({"underSecType": "FUT"}, "not_deliverable"),
        ({"underSymbol": "MSFT"}, "not_deliverable"),
    ],
)
async def test_missing_evidence_leaves_the_contract_unproven_not_proven(
    day: fake_ibapi.FakeGateway, override: dict[str, Any], expect: str
) -> None:
    """Positive control for the evidence pin above: the same path can fail."""

    day.details_override[PUT_100_NOV] = override
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
    finally:
        await adapter.disconnect()
    if expect == "no_session":
        assert option.liquid_hours == "" or option.time_zone_id == ""
    else:
        assert option.deliverable_verified is False


async def test_an_option_that_does_not_exist_is_a_no_security_definition_error(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        with pytest.raises(BrokerError):
            await _qualified(adapter, _spec("333"))
    finally:
        await adapter.disconnect()
    assert (fake_ibapi.CODE_NO_SECURITY, "No security definition") in [
        (code, message[:22]) for _req, code, message in day.errors_sent
    ]


async def test_the_chain_the_capture_narrows_from_has_two_new_expirations(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        underlying = await adapter.qualify_underlying("AAPL")
        chain = await adapter.option_chain_parameters(underlying)
    finally:
        await adapter.disconnect()
    (row,) = [c for c in chain.parameters if c.underlying_con_id == underlying.con_id]
    assert {NOV, DEC} <= set(row.expirations)
    assert {Decimal("90"), Decimal("95"), Decimal("100")} <= set(row.strikes)


# --- option ticks with tickOptionComputation -----------------------------------------------------


def _stream(gateway: fake_ibapi.FakeGateway, con_id: int, *, snapshot: bool) -> list[Any]:
    def ask(client: Any) -> None:
        contract = fake_ibapi.FakeContract()
        contract.conId, contract.exchange = con_id, "SMART"
        client.reqMktData(9, contract, "100,101", snapshot, False, [])

    return _drive(gateway, ask)


def test_a_streaming_option_request_sends_the_model_computation_and_no_snapshot_end() -> None:
    calls = _stream(fake_ibapi.FakeGateway.capture_day(), PUT_100_NOV, snapshot=False)
    names = _names(calls)
    assert names[0] == "marketDataType"
    assert "tickOptionComputation" in names
    assert "tickSnapshotEnd" not in names  # a stream has no terminator (the adapter knows)
    computations = [args for name, args in calls if name == "tickOptionComputation"]
    # the non-model computations (bid 10, ask 11, last 12) arrive first and differ from the
    # model (13): the adapter must read only 13, which the adapter pin below checks
    assert [args[1] for args in computations] == [10, 11, 12, 13]
    assert len({tuple(args[3:]) for args in computations}) == 4


def test_a_snapshot_option_request_ends_with_tick_snapshot_end() -> None:
    calls = _stream(fake_ibapi.FakeGateway.capture_day(), PUT_100_NOV, snapshot=True)
    assert _names(calls)[-1] == "tickSnapshotEnd"


def test_option_ticks_are_deterministic_and_right_specific() -> None:
    first = _stream(fake_ibapi.FakeGateway.capture_day(), PUT_100_NOV, snapshot=False)
    second = _stream(fake_ibapi.FakeGateway.capture_day(), PUT_100_NOV, snapshot=False)
    assert first == second
    sized = {args[1] for name, args in first if name == "tickSize"}
    call_sized = {
        args[1]
        for name, args in _stream(
            fake_ibapi.FakeGateway.capture_day(), CALL_100_NOV, snapshot=False
        )
        if name == "tickSize"
    }
    assert {28, 30} <= sized and not {27, 29} & sized  # put open interest, put volume
    assert {27, 29} <= call_sized and not {28, 30} & call_sized


async def test_the_adapter_assembles_a_complete_put_quote_from_the_stream(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
        (quote,) = await adapter.request_option_quotes([option])
    finally:
        await adapter.disconnect()
    assert quote.data_quality is DataQuality.LIVE
    assert (quote.bid, quote.ask) == (Decimal("3.1"), Decimal("3.2"))
    assert (quote.volume, quote.open_interest) == (120, 3400)
    assert quote.greeks is not None
    assert quote.greeks.delta == Decimal("-0.47")  # the model computation, not bid's (type 10)
    assert quote.greeks.implied_volatility == Decimal("0.3")
    assert (quote.greeks.gamma, quote.greeks.theta) == (Decimal("0.05"), Decimal("-0.06"))
    assert day.callback_errors == []


async def test_the_adapter_assembles_a_call_quote_from_the_call_ticks(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100", OptionRight.CALL))
        (quote,) = await adapter.request_option_quotes([option])
    finally:
        await adapter.disconnect()
    assert (quote.bid, quote.ask) == (Decimal("3.4"), Decimal("3.5"))
    assert (quote.volume, quote.open_interest) == (240, 5100)
    assert quote.greeks is not None and quote.greeks.delta == Decimal("0.53")


async def test_unset_greeks_stay_none_and_do_not_stall_the_stream(
    day: fake_ibapi.FakeGateway,
) -> None:
    day.option_ticks[PUT_100_NOV] = fake_ibapi.OptionTicks.unset_greeks()
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
        (quote,) = await adapter.request_option_quotes([option])
    finally:
        await adapter.disconnect()
    assert quote.greeks is None  # sentinels are honest None evidence
    assert quote.bid == Decimal("3.1")


async def test_an_option_subscription_error_fails_the_request_with_its_code(
    day: fake_ibapi.FakeGateway,
) -> None:
    day.entitlement[PUT_100_NOV] = 10089
    adapter = _broker()
    await adapter.connect()
    try:
        (option,) = await _qualified(adapter, _spec("100"))
        with pytest.raises(BrokerError):
            await adapter.request_option_quotes([option])
    finally:
        await adapter.disconnect()
    assert 10089 in [code for _req, code, _message in day.errors_sent]


# --- reqHistoricalData daily bars ----------------------------------------------------------------


def _bars(gateway: fake_ibapi.FakeGateway, duration: str = "30 D") -> list[Any]:
    def ask(client: Any) -> None:
        contract = fake_ibapi.FakeContract()
        contract.conId, contract.exchange = 999, "SMART"
        client.reqHistoricalData(5, contract, "", duration, "1 day", "TRADES", 1, 1, False, [])

    return _drive(gateway, ask)


def test_daily_bars_are_ordered_weekday_sessions_ending_at_the_sim_date() -> None:
    calls = _bars(fake_ibapi.FakeGateway.capture_day())
    assert _names(calls)[-1] == "historicalDataEnd"
    stamps = [args[1]["date"] for name, args in calls if name == "historicalData"]
    assert stamps == sorted(stamps) and len(stamps) == len(set(stamps))
    assert all(len(stamp) == 8 and stamp.isdigit() for stamp in stamps)  # formatDate=1, daily
    sessions = [date(int(s[:4]), int(s[4:6]), int(s[6:])) for s in stamps]
    assert all(day_.weekday() < 5 for day_ in sessions)
    assert sessions[-1] == date(2026, 10, 12) and sessions[0] >= date(2026, 9, 12)
    assert len(sessions) == 21  # the weekdays in the 30 calendar days ending 2026-10-12


def test_historical_bars_are_deterministic_and_internally_coherent() -> None:
    first = _bars(fake_ibapi.FakeGateway.capture_day())
    second = _bars(fake_ibapi.FakeGateway.capture_day())
    assert first == second
    for name, args in first:
        if name == "historicalData":
            bar = args[1]
            assert 0 < bar["low"] <= min(bar["open"], bar["close"])
            assert max(bar["open"], bar["close"]) <= bar["high"] and bar["volume"] > 0


def test_a_bar_size_the_fake_does_not_model_is_refused_loudly() -> None:
    gateway = fake_ibapi.FakeGateway.capture_day()
    client = fake_ibapi.FakeEClient.bound_to(gateway)(fake_ibapi.RecordingWrapper())
    client.connect("127.0.0.1", 7497, fake_ibapi.CLIENT_ID)
    try:
        contract = fake_ibapi.FakeContract()
        contract.conId = 999
        with pytest.raises(AssertionError, match="not modelled"):
            client.reqHistoricalData(5, contract, "", "1 D", "5 mins", "TRADES", 1, 1, False, [])
    finally:
        client.disconnect()


async def test_the_adapter_returns_the_series_the_capture_asks_for(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        underlying = await adapter.qualify_underlying("AAPL")
        series = await adapter.historical_bars(
            underlying, interval=BarInterval.DAY_1, lookback_days=30
        )
    finally:
        await adapter.disconnect()
    assert len(series.bars) == 21
    assert [bar.timestamp_utc for bar in series.bars] == sorted(
        b.timestamp_utc for b in series.bars
    )
    assert series.bars[-1].session_date == date(2026, 10, 12)
    assert "reqHistoricalData" in [m for _c, m in day.transcript]


class _EventCollector(logging.Handler):
    """Collects ``extra={"event": ...}`` names straight off a named logger.

    ``caplog`` listens on the root logger, which goes blind once any test has run
    ``configure_logging`` (the ``chronos`` logger stops propagating); a handler on the adapter's
    own logger does not depend on propagation.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append(str(getattr(record, "event", "")))


async def test_a_malformed_row_is_dropped_and_the_rest_kept(
    day: fake_ibapi.FakeGateway,
) -> None:
    bad = fake_ibapi.FakeBarData("20261009", open_=100.0, high=99.0, low=101.0, close=100.0)
    day.bars_override[999] = [
        fake_ibapi.FakeBarData("20261008", open_=100.0, high=101.0, low=99.0, close=100.5),
        bad,  # high below low
        fake_ibapi.FakeBarData("20261012", open_=100.5, high=102.0, low=100.0, close=101.0),
    ]
    collector = _EventCollector()
    logger = logging.getLogger("chronos.broker.official_ibkr")
    previous_level = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    adapter = _broker()
    await adapter.connect()
    try:
        underlying = await adapter.qualify_underlying("AAPL")
        series = await adapter.historical_bars(underlying, lookback_days=30)
    finally:
        await adapter.disconnect()
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
    assert [bar.session_date for bar in series.bars] == [date(2026, 10, 8), date(2026, 10, 12)]
    assert collector.events == ["historical_bar_unparseable"]


async def test_a_historical_data_error_fails_the_request(
    day: fake_ibapi.FakeGateway,
) -> None:
    day.historical[999] = 162
    adapter = _broker()
    await adapter.connect()
    try:
        underlying = await adapter.qualify_underlying("AAPL")
        with pytest.raises(BrokerError):
            await adapter.historical_bars(underlying, lookback_days=30)
    finally:
        await adapter.disconnect()
    assert 162 in [code for _req, code, _message in day.errors_sent]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "U-F1-F1 (candidate, ASSUMED-IB, K4): during the regular session a daily request with an "
        "empty endDateTime returns today's still-forming bar; the adapter stamps every parsed "
        "bar BarStatus.CLOSED (official_ibkr.py `_parse_ibkr_bar`). Verify against one capture."
    ),
)
async def test_the_session_in_progress_is_not_reported_as_a_closed_bar(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        underlying = await adapter.qualify_underlying("AAPL")
        series = await adapter.historical_bars(underlying, lookback_days=30)
    finally:
        await adapter.disconnect()
    assert series.bars[-1].session_date == date(2026, 10, 12)  # the sim clock is mid-session
    assert series.bars[-1].status is BarStatus.FORMING


# --- the capture-day book: positions, executions, open orders ------------------------------------


async def test_a_capture_day_has_stock_and_short_put_positions(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        positions = await adapter.positions()
    finally:
        await adapter.disconnect()
    held = {(p.contract.symbol, p.contract.security_type.value): p.quantity for p in positions}
    assert held == {
        ("AAPL", "STK"): Decimal("5"),
        ("MSFT", "STK"): Decimal("8"),
        ("AAPL", "OPT"): Decimal("-1"),
    }
    (short_put,) = [p for p in positions if p.contract.security_type.value == "OPT"]
    assert short_put.contract.con_id == PUT_100_NOV
    assert short_put.average_cost == Decimal("310.0")  # per contract, multiplier included


async def test_a_capture_day_has_two_executions_with_commissions(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        executions = await adapter.executions()
    finally:
        await adapter.disconnect()
    assert sorted(e.contract.symbol for e in executions) == ["AAPL", "MSFT"]
    assert all(e.commission is not None for e in executions)
    assert len({e.execution_id for e in executions}) == 2


async def test_a_capture_day_has_an_owned_a_manual_and_a_new_working_order(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        orders = await adapter.open_orders()
    finally:
        await adapter.disconnect()
    assert {o.order_ref for o in orders} == {"CHR-CONTRACT-SEED", "MANUAL-1", "CHR-CAPTURE-1"}
    capture = next(o for o in orders if o.order_ref == "CHR-CAPTURE-1")
    assert (capture.quantity, capture.filled_quantity) == (Decimal("20"), Decimal("8"))


async def test_two_capture_days_read_identically_through_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read() -> tuple[Any, ...]:
        fake_ibapi.install(monkeypatch, fake_ibapi.FakeGateway.capture_day())
        adapter = _broker()
        await adapter.connect()
        try:
            return (
                tuple(p.model_dump(mode="json") for p in await adapter.positions()),
                tuple(e.model_dump(mode="json") for e in await adapter.executions()),
                tuple(o.model_dump(mode="json") for o in await adapter.open_orders()),
            )
        finally:
            await adapter.disconnect()

    assert await read() == await read()


async def test_the_capture_workload_reads_never_reach_an_order_method(
    day: fake_ibapi.FakeGateway,
) -> None:
    adapter = _broker()
    await adapter.connect()
    try:
        await adapter.positions()
        await adapter.executions()
        await adapter.open_orders()
        (option,) = await _qualified(adapter, _spec("100"))
        await adapter.option_market_rules([option])
        await adapter.request_option_quotes([option])
        underlying = await adapter.qualify_underlying("AAPL")
        await adapter.historical_bars(underlying, lookback_days=30)
    finally:
        await adapter.disconnect()
    sent = {method for _client, method in day.transcript}
    assert not sent & {"placeOrder", "cancelOrder"}
    assert {"reqMarketRule", "reqHistoricalData", "reqMktData", "reqContractDetails"} <= sent

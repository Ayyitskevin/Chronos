"""SimBroker (tests/support/sim_broker.py): a deterministic seam-A ``Broker`` for the sim harness.

Every expected value below is written out by hand from the documented rule, never
recomputed the way the simulator computes it:

* limit-cross on a bar (seam B's model, ``execution/brokers/simulated.py:143-146``):
  BUY fills when ``low <= limit`` at ``min(open, limit)``; SELL fills when
  ``high >= limit`` at ``max(open, limit)``;
* limit-cross on a quote: BUY fills when ``ask <= limit`` at the ask; SELL when
  ``bid >= limit`` at the bid;
* commission ``max(0.005 * shares, 1.00)`` USD (seam B's schedule);
* positions and cash derive from the simulator's own executions only.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tests.support.order_fakes import option_contract
from tests.support.sim_broker import FillPlan, SimBroker, SimClock

from chronos.broker.base import (
    Broker,
    BrokerConnectionError,
    BrokerDataError,
    BrokerRefusedBeforeSend,
)
from chronos.domain.enums import (
    ConnectionState,
    DisplayEnvironment,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    ConnectionStatus,
    OptionContract,
    OrderModification,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.marketdata.bars import Bar, BarInterval

ACCOUNT = "DU7654321"
T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


class _Guard:
    """A stand-in for the submission boundary's readiness guard."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.entered = 0

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        self.entered += 1
        yield self.allowed


def _broker(**kwargs: object) -> SimBroker:
    broker = SimBroker(
        account_id=ACCOUNT,
        clock=SimClock(T0),
        symbols={"AAPL": 265598, "MSFT": 272093},
        starting_cash=Decimal("10000"),
        **kwargs,  # type: ignore[arg-type]
    )
    return broker


async def _connected(**kwargs: object) -> SimBroker:
    broker = _broker(**kwargs)
    await broker.connect()
    return broker


async def _request(
    broker: SimBroker,
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    limit: str = "100.00",
    ref: str = "CHR-1",
    symbol: str = "AAPL",
    transmit: bool = True,
    account: str = ACCOUNT,
) -> OrderRequest:
    contract = await broker.qualify_underlying(symbol)
    return OrderRequest(
        correlation_id=f"corr-{ref}",
        account_id=account,
        contract=contract,
        intent=OrderIntent.OPEN_LONG_STOCK
        if side is OrderSide.BUY
        else OrderIntent.CLOSE_LONG_STOCK,
        side=side,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit),
        order_ref=ref,
        transmit=transmit,
    )


def _bar(
    symbol: str, *, open_: float, high: float, low: float, close: float, minutes: int = 1
) -> Bar:
    stamp = T0 + timedelta(minutes=minutes)
    return Bar(
        symbol=symbol,
        source="sim",
        exchange="SMART",
        interval=BarInterval.MIN_1,
        session_date=stamp.date(),
        timestamp_utc=stamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


# --- the seam ---------------------------------------------------------------------------------


async def test_the_simulator_satisfies_the_seam_a_protocol() -> None:
    assert isinstance(_broker(), Broker)


async def test_status_and_server_time_follow_the_injected_clock() -> None:
    clock = SimClock(T0)
    broker = SimBroker(account_id=ACCOUNT, clock=clock, symbols={"AAPL": 1})
    status = await broker.connection_status()
    assert status.connected is False
    assert status.state is ConnectionState.DISCONNECTED
    await broker.connect()
    clock.advance(timedelta(seconds=90))
    status = await broker.connection_status()
    assert status.connected is True
    assert status.environment is DisplayEnvironment.PAPER
    assert status.account_id == ACCOUNT
    assert status.managed_accounts == (ACCOUNT,)
    assert await broker.server_time() == datetime(2026, 10, 12, 14, 1, 30, tzinfo=UTC)


async def test_unknown_symbols_and_unsupported_families_refuse_as_data_errors() -> None:
    broker = await _connected()
    assert (await broker.qualify_underlying("aapl")).con_id == 265598
    with pytest.raises(BrokerDataError, match="unknown symbol"):
        await broker.qualify_underlying("TSLA")
    with pytest.raises(BrokerDataError, match="not crypto"):
        await broker.qualify_crypto("BTC")


async def test_scripted_quotes_are_returned_as_set() -> None:
    broker = await _connected()
    broker.set_quote("AAPL", bid="99.90", ask="100.10", last="100.00")
    quote = await broker.request_underlying_quote(await broker.qualify_underlying("AAPL"))
    assert (quote.bid, quote.ask, quote.last) == (
        Decimal("99.90"),
        Decimal("100.10"),
        Decimal("100.00"),
    )
    assert quote.timestamp == T0


# --- refusals before send leave no trace ------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("untransmitted", "transmit"),
        ("no_guard", "authorization"),
        ("guard_closed", "authorization changed"),
        ("wrong_account", "account"),
        ("disconnected", "not connected"),
    ],
)
async def test_every_pre_send_refusal_records_nothing(change: str, match: str) -> None:
    broker = await _connected()
    request = await _request(
        broker,
        transmit=change != "untransmitted",
        account="DU0000001" if change == "wrong_account" else ACCOUNT,
    )
    guard: _Guard | None = (
        None if change == "no_guard" else _Guard(allowed=change != "guard_closed")
    )
    if change == "disconnected":
        await broker.disconnect()
    with pytest.raises(BrokerRefusedBeforeSend, match=match):
        await broker.submit_order(request, send_guard=guard)
    assert broker.sent_order_count == 0
    if change != "disconnected":
        assert await broker.open_orders() == ()


async def test_a_planned_refusal_before_send_records_nothing() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(refuse_before_send=True)})
    with pytest.raises(BrokerRefusedBeforeSend):
        await broker.submit_order(await _request(broker), send_guard=_Guard())
    assert broker.sent_order_count == 0
    assert await broker.open_orders() == ()


async def test_an_engaged_kill_switch_refuses_inside_the_send_guard() -> None:
    class _Kill:
        @contextmanager
        def at_send(self) -> Iterator[bool]:
            yield False

    broker = await _connected(kill_switch=_Kill())
    guard = _Guard()
    with pytest.raises(BrokerRefusedBeforeSend, match="kill switch"):
        await broker.submit_order(await _request(broker), send_guard=guard)
    assert guard.entered == 1
    assert broker.sent_order_count == 0


# --- submit, fills, partials ------------------------------------------------------------------


async def test_a_submitted_order_works_until_a_bar_crosses_its_limit() -> None:
    broker = await _connected()
    submission = await broker.submit_order(
        await _request(broker, limit="100.00"), send_guard=_Guard()
    )
    assert submission.lifecycle is OrderLifecycle.SUBMITTED
    assert submission.submitted_at == T0
    (working,) = await broker.open_orders()
    assert (working.filled_quantity, working.remaining_quantity) == (Decimal("0"), Decimal("10"))
    # low 100.50 > limit 100.00: no fill.
    broker.advance_bar(_bar("AAPL", open_=101.0, high=101.5, low=100.5, close=101.0, minutes=1))
    assert await broker.executions() == ()
    # low 99.80 <= limit: fills at min(open 100.40, limit 100.00) = 100.00.
    broker.advance_bar(_bar("AAPL", open_=100.4, high=100.6, low=99.8, close=100.2, minutes=2))
    (execution,) = await broker.executions()
    assert (execution.quantity, execution.price) == (Decimal("10"), Decimal("100.00"))
    assert execution.commission == Decimal("1.00")
    assert execution.broker_order_id == submission.broker_order_id
    assert await broker.open_orders() == ()
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.FILLED


async def test_a_buy_gapping_below_its_limit_fills_at_the_open() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=98.5, high=99.0, low=98.0, close=98.7))
    (execution,) = await broker.executions()
    assert execution.price == Decimal("98.50")


async def test_a_sell_fills_at_max_of_open_and_limit() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.0, close=99.0, minutes=1))
    sell = await _request(broker, side=OrderSide.SELL, quantity="10", limit="101.00", ref="CHR-2")
    await broker.submit_order(sell, send_guard=_Guard())
    # high 101.20 >= 101.00, open 100.50: fills at max(100.50, 101.00) = 101.00.
    broker.advance_bar(_bar("AAPL", open_=100.5, high=101.2, low=100.4, close=101.0, minutes=2))
    sells = [e for e in await broker.executions() if e.side is OrderSide.SELL]
    assert [(e.quantity, e.price) for e in sells] == [(Decimal("10"), Decimal("101.00"))]


async def test_a_quote_through_the_limit_fills_at_the_quote() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_quote("AAPL", bid="100.05", ask="100.15")  # ask above limit: nothing
    assert await broker.executions() == ()
    broker.advance_quote("AAPL", bid="99.85", ask="99.95")  # ask 99.95 <= 100.00
    (execution,) = await broker.executions()
    assert execution.price == Decimal("99.95")


async def test_bars_for_another_symbol_do_not_fill() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_bar(_bar("MSFT", open_=50.0, high=51.0, low=49.0, close=50.0))
    assert await broker.executions() == ()


async def test_a_planned_partial_fills_once_then_completes() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(partial_fraction=(3, 10))})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.9, high=100.1, low=99.5, close=99.9, minutes=1))
    (working,) = await broker.open_orders()
    assert working.lifecycle is OrderLifecycle.PARTIALLY_FILLED
    assert (working.filled_quantity, working.remaining_quantity) == (Decimal("3"), Decimal("7"))
    broker.advance_bar(_bar("AAPL", open_=99.7, high=99.9, low=99.2, close=99.4, minutes=2))
    quantities = [(e.quantity, e.price) for e in await broker.executions()]
    assert quantities == [(Decimal("3"), Decimal("99.90")), (Decimal("7"), Decimal("99.70"))]
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.FILLED
    assert await broker.open_orders() == ()


async def test_a_planned_reject_is_rejected_and_never_works() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(reject=True)})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    assert submission.lifecycle is OrderLifecycle.REJECTED
    assert broker.sent_order_count == 1  # it was sent; the venue refused it
    assert await broker.open_orders() == ()
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.0, low=98.0, close=98.5))
    assert await broker.executions() == ()


async def test_a_planned_ambiguous_send_reports_unknown_but_the_venue_holds_the_order() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(ambiguous=True)})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    assert submission.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN
    assert "unknown" in submission.message
    (held,) = await broker.open_orders()
    assert held.broker_order_id == submission.broker_order_id
    assert held.lifecycle is OrderLifecycle.SUBMITTED


# --- cancel and modify ------------------------------------------------------------------------


async def test_cancel_removes_a_working_order_and_keeps_partial_fills() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(partial_fraction=(1, 2))})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0))
    result = await broker.cancel_order(submission.broker_order_id)
    assert (result.requested, result.lifecycle) == (True, OrderLifecycle.CANCELLED)
    assert await broker.open_orders() == ()
    (position,) = await broker.positions()
    assert position.quantity == Decimal("5")
    broker.advance_bar(_bar("AAPL", open_=98.0, high=98.5, low=97.0, close=98.0, minutes=2))
    assert len(await broker.executions()) == 1  # a cancelled order never fills again


async def test_cancel_of_an_unknown_or_finished_order_is_not_requested() -> None:
    broker = await _connected()
    unknown = await broker.cancel_order(424242)
    assert (unknown.requested, unknown.lifecycle) == (False, OrderLifecycle.REJECTED)
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0))
    finished = await broker.cancel_order(submission.broker_order_id)
    assert (finished.requested, finished.lifecycle) == (False, OrderLifecycle.FILLED)


async def test_modify_refuses_before_send_like_the_official_adapter() -> None:
    broker = await _connected()
    submission = await broker.submit_order(
        await _request(broker, limit="100.00"), send_guard=_Guard()
    )
    with pytest.raises(BrokerRefusedBeforeSend, match="cancel and re-submit"):
        await broker.modify_order(
            OrderModification(
                correlation_id="m-1",
                broker_order_id=submission.broker_order_id,
                new_limit_price=Decimal("101.00"),
            )
        )
    (working,) = await broker.open_orders()
    assert working.limit_price == Decimal("100.00")


async def test_modify_when_enabled_changes_the_limit_the_fill_rule_uses() -> None:
    broker = await _connected(allow_modify=True)
    submission = await broker.submit_order(
        await _request(broker, limit="100.00"), send_guard=_Guard()
    )
    modified = await broker.modify_order(
        OrderModification(
            correlation_id="m-1",
            broker_order_id=submission.broker_order_id,
            new_limit_price=Decimal("99.00"),
        )
    )
    assert modified.broker_order_id == submission.broker_order_id
    # low 99.50 would have crossed 100.00 but not the new 99.00.
    broker.advance_bar(_bar("AAPL", open_=100.0, high=100.2, low=99.5, close=99.8))
    assert await broker.executions() == ()
    (working,) = await broker.open_orders()
    assert working.limit_price == Decimal("99.00")


# --- positions and account from fills ---------------------------------------------------------


async def test_positions_and_cash_derive_from_the_simulators_own_fills() -> None:
    broker = await _connected()
    await broker.submit_order(
        await _request(broker, quantity="10", limit="100.00"), send_guard=_Guard()
    )
    broker.advance_bar(_bar("AAPL", open_=100.0, high=100.2, low=99.8, close=100.0, minutes=1))
    (position,) = await broker.positions()
    # 10 @ 100.00 + 1.00 commission: average cost 100.10 per share.
    assert (position.quantity, position.average_cost) == (Decimal("10"), Decimal("100.10"))
    account = await broker.account_summary()
    assert account.total_cash == Decimal("8999.00")  # 10000 - 1000 - 1
    sell = await _request(broker, side=OrderSide.SELL, quantity="4", limit="110.00", ref="CHR-2")
    await broker.submit_order(sell, send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=110.0, high=110.5, low=109.5, close=110.0, minutes=2))
    (position,) = await broker.positions()
    assert (position.quantity, position.average_cost) == (Decimal("6"), Decimal("100.10"))
    account = await broker.account_summary()
    assert account.total_cash == Decimal("9438.00")  # 8999 + 440 - 1
    # mark = the last bar close for AAPL (110.00): 9438 + 6 * 110 = 10098.
    assert account.net_liquidation == Decimal("10098.00")
    sell_rest = await _request(
        broker, side=OrderSide.SELL, quantity="6", limit="110.00", ref="CHR-3"
    )
    await broker.submit_order(sell_rest, send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=110.0, high=110.5, low=109.5, close=110.0, minutes=3))
    assert await broker.positions() == ()


async def test_executions_since_filters_by_fill_time() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, ref="CHR-1"), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0, minutes=1))
    await broker.submit_order(await _request(broker, ref="CHR-2"), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0, minutes=5))
    later = await broker.executions(since=T0 + timedelta(minutes=3))
    assert [e.order_ref for e in later] == ["CHR-2"]


async def test_two_runs_of_the_same_script_are_identical() -> None:
    async def run() -> tuple[object, ...]:
        broker = await _connected(plans={"CHR-1": FillPlan(partial_fraction=(1, 2))})
        await broker.submit_order(await _request(broker), send_guard=_Guard())
        broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0, minutes=1))
        broker.advance_bar(_bar("AAPL", open_=98.0, high=98.5, low=97.5, close=98.0, minutes=2))
        return (
            await broker.executions(),
            await broker.positions(),
            await broker.account_summary(),
            await broker.connection_status(),
        )

    first, second = await run(), await run()
    assert first == second
    status = first[3]
    assert isinstance(status, ConnectionStatus)
    assert status.last_successful_sync == T0  # the injected clock, never the wall clock


async def test_preview_refuses_a_transmitting_request_and_estimates_commission() -> None:
    broker = await _connected()
    with pytest.raises(BrokerRefusedBeforeSend, match="transmit"):
        await broker.preview_order(await _request(broker, transmit=True))
    preview = await broker.preview_order(await _request(broker, transmit=False, quantity="300"))
    assert preview.accepted is True
    assert preview.estimated_commission == Decimal("1.50")  # 300 * 0.005
    assert preview.previewed_at == T0


async def test_historical_bars_return_the_scripted_closed_bars() -> None:
    broker = await _connected()
    day = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    broker.add_history(
        Bar(
            symbol="AAPL",
            source="sim",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=date(2026, 10, 9),
            timestamp_utc=day,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1.0,
        )
    )
    series = await broker.historical_bars(await broker.qualify_underlying("AAPL"))
    assert [b.close for b in series.bars] == [100.5]


# --- r1: the seam-A conformance, typed and by signature (review P3-4) -------------------------


def test_simbroker_type_checks_as_a_broker() -> None:
    conforming: Broker = _broker()  # strict mypy rejects this if a signature drifts
    assert conforming is not None


def test_every_protocol_method_has_the_protocol_signature() -> None:
    names = [name for name, value in vars(Broker).items() if inspect.iscoroutinefunction(value)]
    assert len(names) == 23
    for name in names:
        expected = inspect.signature(getattr(Broker, name))
        actual = inspect.signature(getattr(SimBroker, name))
        assert list(actual.parameters) == list(expected.parameters), name
        for parameter in expected.parameters.values():
            mine = actual.parameters[parameter.name]
            assert (mine.kind, mine.default) == (parameter.kind, parameter.default), name


# --- r1: reads fail closed while disconnected (review P2-2) -----------------------------------


async def test_every_read_refuses_while_disconnected() -> None:
    broker = _broker()
    contract = UnderlyingContract(con_id=265598, symbol="AAPL")
    for read in (
        broker.server_time(),
        broker.account_summary(),
        broker.positions(),
        broker.executions(),
        broker.open_orders(),
        broker.qualify_underlying("AAPL"),
        broker.request_underlying_quote(contract),
        broker.historical_bars(contract),
    ):
        with pytest.raises(BrokerConnectionError):
            await read


# --- r1: the rules the surviving mutants showed were unprotected (review P2-3) ----------------


async def _held_aapl(broker: SimBroker, quantity: str = "10") -> None:
    """Buy ``quantity`` AAPL at 99.00 (bar open 99.00, low 98.00, limit 100.00)."""

    await broker.submit_order(
        await _request(broker, quantity=quantity, ref="CHR-HOLD"), send_guard=_Guard()
    )
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.0, close=99.0, minutes=1))


async def test_a_sell_gapping_above_its_limit_fills_at_the_open() -> None:
    broker = await _connected()
    await _held_aapl(broker)
    sell = await _request(broker, side=OrderSide.SELL, limit="100.00", ref="CHR-S")
    await broker.submit_order(sell, send_guard=_Guard())
    # high 102.00 >= 100.00, open 101.50: fills at max(101.50, 100.00) = 101.50.
    broker.advance_bar(_bar("AAPL", open_=101.5, high=102.0, low=101.0, close=101.8, minutes=2))
    sells = [e for e in await broker.executions() if e.side is OrderSide.SELL]
    assert [e.price for e in sells] == [Decimal("101.50")]


async def test_a_sell_fills_at_a_bid_above_its_limit() -> None:
    broker = await _connected()
    await _held_aapl(broker)
    await broker.submit_order(
        await _request(broker, side=OrderSide.SELL, limit="100.00", ref="CHR-S"),
        send_guard=_Guard(),
    )
    broker.advance_quote("AAPL", bid="99.95", ask="100.05")  # bid below limit: nothing
    broker.advance_quote("AAPL", bid="100.20", ask="100.30")
    sells = [e for e in await broker.executions() if e.side is OrderSide.SELL]
    assert [e.price for e in sells] == [Decimal("100.20")]


async def test_a_quote_exactly_at_the_limit_fills_both_sides() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_quote("AAPL", bid="99.90", ask="100.00")  # ask == limit
    assert [e.price for e in await broker.executions()] == [Decimal("100.00")]
    await broker.submit_order(
        await _request(broker, side=OrderSide.SELL, limit="101.00", ref="CHR-S"),
        send_guard=_Guard(),
    )
    broker.advance_quote("AAPL", bid="101.00", ask="101.10")  # bid == limit
    sells = [e for e in await broker.executions() if e.side is OrderSide.SELL]
    assert [e.price for e in sells] == [Decimal("101.00")]


async def test_a_bar_from_before_the_order_existed_never_fills_it() -> None:
    clock = SimClock(T0)
    broker = SimBroker(account_id=ACCOUNT, clock=clock, symbols={"AAPL": 265598})
    await broker.connect()
    clock.advance(timedelta(minutes=5))  # submit at T0+5m
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    # A T0+1m bar that crosses the limit arrives late: lookahead, so no fill.
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.0, close=99.0, minutes=1))
    assert await broker.executions() == ()
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.0, close=99.0, minutes=6))
    assert len(await broker.executions()) == 1


async def test_selling_through_zero_opens_a_short_at_the_trade_price() -> None:
    broker = await _connected()
    await _held_aapl(broker)  # long 10 @ 99.00 + 1.00 commission
    sell = await _request(broker, side=OrderSide.SELL, quantity="15", limit="110.00", ref="CHR-S")
    await broker.submit_order(sell, send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=110.0, high=110.5, low=109.5, close=110.0, minutes=2))
    (position,) = await broker.positions()
    # 10 close the long; 5 open a short at 110.00, so its cost basis is 5 x 110.00.
    assert (position.quantity, position.average_cost) == (Decimal("-5"), Decimal("110.00"))


async def test_unrealized_pnl_and_buying_power_follow_the_fills() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker, limit="100.00"), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=100.0, high=100.2, low=99.8, close=100.5, minutes=1))
    (position,) = await broker.positions()
    # average 100.10 (incl. commission), mark = close 100.50: (100.50 - 100.10) x 10 = 4.00.
    assert position.unrealized_pnl == Decimal("4.00")
    account = await broker.account_summary()
    assert account.buying_power == Decimal("8999.00")  # cash 10000 - 1000 - 1


async def test_executions_since_includes_an_execution_at_the_bound() -> None:
    broker = await _connected()
    await broker.submit_order(await _request(broker), send_guard=_Guard())
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.5, close=99.0, minutes=2))
    stamp = T0 + timedelta(minutes=2)
    assert [e.timestamp for e in await broker.executions(since=stamp)] == [stamp]


async def test_historical_bars_keep_only_the_lookback_window_up_to_now() -> None:
    broker = await _connected()

    def day(when: datetime) -> Bar:
        return Bar(
            symbol="AAPL",
            source="sim",
            exchange="SMART",
            interval=BarInterval.DAY_1,
            session_date=when.date(),
            timestamp_utc=when,
            open=1.0,
            high=1.0,
            low=1.0,
            close=float(when.day),
            volume=1.0,
        )

    broker.add_history(day(T0 - timedelta(days=200)))  # older than the 180-day lookback
    broker.add_history(day(T0 - timedelta(days=3)))  # inside
    broker.add_history(day(T0 + timedelta(days=1)))  # after now
    series = await broker.historical_bars(await broker.qualify_underlying("AAPL"))
    assert [b.timestamp_utc for b in series.bars] == [T0 - timedelta(days=3)]


def test_fill_plans_refuse_contradictory_or_degenerate_settings() -> None:
    with pytest.raises(ValueError, match="at most one"):
        FillPlan(reject=True, ambiguous=True)
    with pytest.raises(ValueError, match="0 < n < d"):
        FillPlan(partial_fraction=(2, 2))


# --- r1: options (contract finding U12-F2) ----------------------------------------------------


def _option_request(*, side: OrderSide, limit: str, ref: str, quantity: str = "1") -> OrderRequest:
    return OrderRequest(
        correlation_id=f"corr-{ref}",
        account_id=ACCOUNT,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT
        if side is OrderSide.SELL
        else OrderIntent.CLOSE_SHORT_OPTION,
        side=side,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit),
        order_ref=ref,
        transmit=True,
    )


async def test_a_short_put_moves_cash_by_the_multiplier_and_closes_flat() -> None:
    broker = await _connected()
    put: OptionContract = option_contract()
    sold = await broker.submit_order(
        _option_request(side=OrderSide.SELL, limit="1.25", ref="CHR-P1"), send_guard=_Guard()
    )
    broker.fill_order(sold.broker_order_id, Decimal("1"))
    (position,) = await broker.positions()
    # -1 contract; basis 1.25 x 100 = 125.00 per contract; credit 125 - 1.00 commission.
    assert (position.quantity, position.average_cost) == (Decimal("-1"), Decimal("125.00"))
    assert position.contract == put
    assert (await broker.account_summary()).total_cash == Decimal("10124.00")
    broker.advance_option_quote(put, bid="1.00", ask="1.10")  # mark 1.05, no working order
    account = await broker.account_summary()
    assert account.net_liquidation == Decimal("10019.00")  # 10124 - 1 x 1.05 x 100
    await broker.submit_order(
        _option_request(side=OrderSide.BUY, limit="1.20", ref="CHR-P2"), send_guard=_Guard()
    )
    broker.advance_option_quote(put, bid="1.00", ask="1.10")  # ask 1.10 <= 1.20
    assert await broker.positions() == ()
    assert (await broker.account_summary()).total_cash == Decimal("10013.00")  # -110 - 1


async def test_option_commission_is_per_contract_with_the_minimum() -> None:
    broker = await _connected()
    preview_one = await broker.preview_order(
        _option_request(side=OrderSide.SELL, limit="1.25", ref="CHR-P").model_copy(
            update={"transmit": False}
        )
    )
    preview_two = await broker.preview_order(
        _option_request(side=OrderSide.SELL, limit="1.25", ref="CHR-P", quantity="2").model_copy(
            update={"transmit": False}
        )
    )
    assert preview_one.estimated_commission == Decimal("1.00")  # 0.65 -> the 1.00 minimum
    assert preview_two.estimated_commission == Decimal("1.30")  # 2 x 0.65


async def test_a_stock_bar_never_fills_an_option_on_the_same_symbol() -> None:
    broker = await _connected()
    await broker.submit_order(
        _option_request(side=OrderSide.SELL, limit="1.25", ref="CHR-P1"), send_guard=_Guard()
    )
    broker.advance_bar(_bar("AAPL", open_=150.0, high=160.0, low=140.0, close=150.0))
    assert await broker.executions() == ()


# --- r1: venue rejections, ownership, pending cancel, ambiguity (review P3-2/P3-3) ------------


async def test_a_buy_beyond_the_cash_is_sent_and_rejected_by_the_venue() -> None:
    broker = await _connected()  # cash 10000
    submission = await broker.submit_order(
        await _request(broker, quantity="200", limit="100.00"), send_guard=_Guard()
    )
    assert submission.lifecycle is OrderLifecycle.REJECTED
    assert "insufficient funds" in submission.message
    assert broker.sent_order_count == 1
    assert await broker.open_orders() == ()


async def test_a_stock_with_an_unqualified_contract_id_is_rejected_by_the_venue() -> None:
    broker = await _connected()
    request = (await _request(broker)).model_copy(
        update={"contract": UnderlyingContract(con_id=1, symbol="AAPL")}
    )
    submission = await broker.submit_order(request, send_guard=_Guard())
    assert submission.lifecycle is OrderLifecycle.REJECTED
    assert "no security definition" in submission.message


async def test_cancel_refuses_an_order_chronos_does_not_own() -> None:
    broker = await _connected()
    submission = await broker.submit_order(
        await _request(broker, ref="MANUAL-1"), send_guard=_Guard()
    )
    result = await broker.cancel_order(submission.broker_order_id)
    assert result.requested is False
    assert [o.broker_order_id for o in await broker.open_orders()] == [submission.broker_order_id]


async def test_a_pending_cancel_keeps_working_until_the_venue_confirms() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(cancel_pending=True)})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    result = await broker.cancel_order(submission.broker_order_id)
    assert (result.requested, result.lifecycle) == (True, OrderLifecycle.CANCEL_PENDING)
    (pending,) = await broker.open_orders()
    assert pending.lifecycle is OrderLifecycle.CANCEL_PENDING
    again = await broker.cancel_order(submission.broker_order_id)
    assert again.requested is False
    broker.confirm_cancel(submission.broker_order_id)
    assert await broker.open_orders() == ()
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.CANCELLED


async def test_a_pending_cancel_can_still_fill() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(cancel_pending=True)})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    await broker.cancel_order(submission.broker_order_id)
    broker.advance_bar(_bar("AAPL", open_=99.0, high=99.5, low=98.0, close=99.0))
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.FILLED


async def test_an_ambiguous_send_reports_no_permanent_id() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(ambiguous=True)})
    submission = await broker.submit_order(await _request(broker), send_guard=_Guard())
    assert submission.permanent_id is None
    (held,) = await broker.open_orders()
    assert held.permanent_id is not None  # the venue knows it; the caller never learnt it


# --- r2: sonnet's independent mutants of the r1 behaviours (review P2-1) --------------------


def _opt(
    *, side: OrderSide, limit: str, ref: str, quantity: str = "1", contract: Any = None
) -> OrderRequest:
    return _option_request(side=side, limit=limit, ref=ref, quantity=quantity).model_copy(
        update={"contract": contract or option_contract()}
    )


async def _submit(broker: SimBroker, request: OrderRequest) -> OrderSubmission:
    return await broker.submit_order(request, send_guard=_Guard())


# funds: the exact boundary, the multiplier, and BUY-only (no margin model)


async def test_a_buy_costing_exactly_the_cash_is_accepted_and_one_cent_more_is_rejected() -> None:
    broker = await _connected()  # cash 10000.00
    exact = await _submit(
        broker, await _request(broker, quantity="1", limit="9999.00", ref="CHR-A")
    )
    assert exact.lifecycle is OrderLifecycle.SUBMITTED  # 9999.00 + 1.00 commission == 10000.00
    over = await _submit(broker, await _request(broker, quantity="1", limit="9999.01", ref="CHR-B"))
    assert over.lifecycle is OrderLifecycle.REJECTED  # 10000.01 > 10000.00


async def test_the_funds_check_prices_an_option_with_its_multiplier() -> None:
    broker = await _connected()
    affordable = await _submit(broker, _opt(side=OrderSide.BUY, limit="99.00", ref="CHR-A"))
    assert affordable.lifecycle is OrderLifecycle.SUBMITTED  # 99.00 x 100 + 1.00 = 9901.00
    too_dear = await _submit(broker, _opt(side=OrderSide.BUY, limit="100.00", ref="CHR-B"))
    assert too_dear.lifecycle is OrderLifecycle.REJECTED  # 100.00 x 100 + 1.00 = 10001.00


async def test_a_sell_is_not_funds_checked() -> None:
    """No margin model: a short sale is accepted with almost no cash (the docstring's rule)."""

    broker = SimBroker(
        account_id=ACCOUNT,
        clock=SimClock(T0),
        symbols={"AAPL": 265598},
        starting_cash=Decimal("10"),
    )
    await broker.connect()
    submission = await _submit(
        broker, await _request(broker, side=OrderSide.SELL, quantity="10", limit="100.00")
    )
    assert submission.lifecycle is OrderLifecycle.SUBMITTED


async def test_a_stock_with_an_unknown_symbol_is_rejected_by_the_venue() -> None:
    broker = await _connected()
    request = (await _request(broker)).model_copy(
        update={"contract": UnderlyingContract(con_id=5, symbol="ZZZZ")}
    )
    submission = await _submit(broker, request)
    assert submission.lifecycle is OrderLifecycle.REJECTED
    assert "no security definition" in submission.message


# option fills by quote: boundaries, the price, and the exact contract


async def test_an_option_sell_fills_at_a_bid_exactly_at_its_limit() -> None:
    broker = await _connected()
    await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-S"))
    broker.advance_option_quote(option_contract(), bid="1.25", ask="1.35")
    assert [e.price for e in await broker.executions()] == [Decimal("1.25")]


async def test_an_option_buy_fills_at_an_ask_exactly_at_its_limit() -> None:
    broker = await _connected()
    await _submit(broker, _opt(side=OrderSide.BUY, limit="1.20", ref="CHR-B"))
    broker.advance_option_quote(option_contract(), bid="1.10", ask="1.20")
    assert [e.price for e in await broker.executions()] == [Decimal("1.20")]


async def test_an_option_sell_fills_at_the_bid_not_at_its_limit() -> None:
    broker = await _connected()
    await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-S"))
    broker.advance_option_quote(option_contract(), bid="1.40", ask="1.50")
    assert [e.price for e in await broker.executions()] == [Decimal("1.40")]


async def test_a_quote_fills_only_the_option_it_quotes() -> None:
    broker = await _connected()
    quoted = option_contract(con_id=111)
    sibling = option_contract(con_id=112, strike=Decimal("145"))  # same symbol, another series
    await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-A", contract=quoted))
    await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-B", contract=sibling))
    broker.advance_option_quote(quoted, bid="1.40", ask="1.50")
    assert [e.contract.con_id for e in await broker.executions()] == [111]


# pending cancel


async def test_confirm_cancel_refuses_an_order_with_no_pending_cancel() -> None:
    broker = await _connected()
    submission = await _submit(broker, await _request(broker))
    with pytest.raises(ValueError, match="no pending cancel"):
        broker.confirm_cancel(submission.broker_order_id)
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.SUBMITTED


async def test_a_partial_fill_while_a_cancel_is_pending_keeps_the_cancel_pending() -> None:
    broker = await _connected(plans={"CHR-1": FillPlan(cancel_pending=True)})
    submission = await _submit(broker, await _request(broker))
    await broker.cancel_order(submission.broker_order_id)
    broker.fill_order(submission.broker_order_id, Decimal("4"))  # 4 of 10 fill meanwhile
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.CANCEL_PENDING
    broker.confirm_cancel(submission.broker_order_id)
    assert broker.order_lifecycle(submission.broker_order_id) is OrderLifecycle.CANCELLED
    (position,) = await broker.positions()
    assert position.quantity == Decimal("4")  # the partial fill stays


# the option multiplier in the books


async def test_an_option_held_without_a_mark_is_valued_at_its_cost_per_unit() -> None:
    """The normal state after fill_order: no quote yet, so the mark falls back to cost."""

    broker = await _connected()
    sold = await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-P"))
    broker.fill_order(sold.broker_order_id, Decimal("1"))
    (position,) = await broker.positions()
    assert position.market_price == Decimal("1.25")  # per unit, not per contract
    account = await broker.account_summary()
    assert account.total_cash == Decimal("10124.00")  # 125.00 - 1.00
    assert account.net_liquidation == Decimal("9999.00")  # 10124.00 - 1 x 1.25 x 100


async def test_option_unrealized_pnl_uses_the_multiplier() -> None:
    broker = await _connected()
    sold = await _submit(broker, _opt(side=OrderSide.SELL, limit="1.25", ref="CHR-P"))
    broker.fill_order(sold.broker_order_id, Decimal("1"))
    broker.advance_option_quote(option_contract(), bid="1.00", ask="1.10")  # mark 1.05
    (position,) = await broker.positions()
    # (1.05 x 100 - 125.00) x -1 = 20.00: the short gained as the option cheapened.
    assert position.unrealized_pnl == Decimal("20.00")


async def test_an_option_sold_through_zero_opens_the_short_at_the_contract_value() -> None:
    broker = await _connected()
    bought = await _submit(broker, _opt(side=OrderSide.BUY, limit="1.20", ref="CHR-B"))
    broker.fill_order(bought.broker_order_id, Decimal("1"))
    sold = await _submit(broker, _opt(side=OrderSide.SELL, limit="1.30", ref="CHR-S", quantity="3"))
    broker.fill_order(sold.broker_order_id, Decimal("3"))
    (position,) = await broker.positions()
    # 1 closes the long; 2 open a short at 1.30 x 100 = 130.00 per contract.
    assert (position.quantity, position.average_cost) == (Decimal("-2"), Decimal("130.00"))


# ownership and the fill_order control


async def test_cancel_refuses_a_reference_that_only_starts_with_chr() -> None:
    broker = await _connected()
    submission = await _submit(broker, await _request(broker, ref="CHRX-1"))
    result = await broker.cancel_order(submission.broker_order_id)
    assert result.requested is False


async def test_fill_order_fills_at_the_price_given() -> None:
    broker = await _connected()
    submission = await _submit(broker, await _request(broker, limit="100.00"))
    broker.fill_order(submission.broker_order_id, Decimal("10"), price=Decimal("99.50"))
    assert [e.price for e in await broker.executions()] == [Decimal("99.50")]


async def test_fill_order_refuses_more_than_the_remaining_quantity() -> None:
    broker = await _connected()
    submission = await _submit(broker, await _request(broker, quantity="10"))
    with pytest.raises(ValueError, match="at most the remaining"):
        broker.fill_order(submission.broker_order_id, Decimal("11"))
    assert await broker.executions() == ()

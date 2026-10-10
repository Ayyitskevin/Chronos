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

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tests.support.sim_broker import FillPlan, SimBroker, SimClock

from chronos.broker.base import Broker, BrokerDataError, BrokerRefusedBeforeSend
from chronos.domain.enums import (
    ConnectionState,
    DisplayEnvironment,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import OrderModification, OrderRequest
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
    with pytest.raises(BrokerDataError, match="stocks only"):
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
        return (await broker.executions(), await broker.positions(), await broker.account_summary())

    assert await run() == await run()


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

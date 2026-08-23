"""Callback bridge: routing, sentinel hygiene, quote accumulation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from chronos.broker.base import BrokerDataError
from chronos.broker.callbacks import CallbackBridge, clean_price, clean_ratio, clean_size
from chronos.broker.market_data import (
    MarketDataCancellationError,
    MarketDataManager,
    MarketDataPacingError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.broker.request_registry import RequestRegistry
from chronos.config.limits import (
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
)
from chronos.domain.enums import DataQuality, OptionRight, ReconciliationStatus
from chronos.domain.models import (
    MarketQuote,
    OptionContract,
    OptionMarketRule,
    UnderlyingContract,
)
from chronos.orders.reconciliation_readiness import ReconciliationReadiness


@pytest.fixture()
def bridge() -> CallbackBridge:
    return CallbackBridge(RequestRegistry())


class TestSentinelHygiene:
    def test_clean_price_rejects_sentinels(self) -> None:
        assert clean_price(-1.0) is None  # TWS "no data"
        assert clean_price(0.0) is None
        assert clean_price(1.7976931348623157e308) is None  # UNSET_DOUBLE
        assert clean_price(float("nan")) is None
        assert clean_price(float("inf")) is None
        assert clean_price(123.45) == Decimal("123.45")

    def test_clean_size_rejects_negative_and_unset(self) -> None:
        assert clean_size(-1.0) is None
        assert clean_size(1e308) is None
        assert clean_size(250.0) == 250

    def test_clean_ratio_bounds(self) -> None:
        assert clean_ratio(0.31, low=-1.0, high=1.0) == Decimal("0.31")
        assert clean_ratio(2.0, low=-1.0, high=1.0) is None
        assert clean_ratio(float("nan"), low=-1.0, high=1.0) is None


class TestConnectionCallbacks:
    def test_next_valid_id_sets_connected(self, bridge: CallbackBridge) -> None:
        assert not bridge.connected_event.is_set()
        bridge.on_next_valid_id(42)
        assert bridge.connected_event.is_set()
        assert bridge.next_valid_id == 42

    def test_managed_accounts_parse(self, bridge: CallbackBridge) -> None:
        bridge.on_managed_accounts("DU111, DU222 ,")
        assert bridge.managed_accounts == ("DU111", "DU222")
        assert bridge.managed_accounts_event.is_set()

    def test_benign_error_becomes_notice(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.on_error(request_id, 2104, "Market data farm connection is OK")
        # The request must NOT be failed by a benign notice.
        bridge.registry.add(request_id, "row")
        bridge.registry.finish(request_id)
        assert bridge.registry.wait_sync(request_id, timeout=1.0) == ["row"]
        assert any("2104" in notice for notice in bridge.notices)

    def test_idless_benign_notice_does_not_fail_pending_read(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.registry.add(request_id, "row")

        bridge.on_error(-1, 2104, "Market data farm connection is OK")

        bridge.registry.finish(request_id)
        assert bridge.registry.wait_sync(request_id, timeout=1.0) == ["row"]

    def test_request_scoped_error_fails_request(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.on_error(request_id, 200, "No security definition")
        with pytest.raises(BrokerDataError, match="broker error 200"):
            bridge.registry.wait_sync(request_id, timeout=1.0)

    @pytest.mark.parametrize(
        ("code", "error_type"),
        [
            (420, MarketDataPacingError),
            (10089, MarketDataPermissionError),
        ],
    )
    def test_market_data_callback_errors_keep_typed_failure_and_prefix(
        self,
        bridge: CallbackBridge,
        code: int,
        error_type: type[BrokerDataError],
    ) -> None:
        request_id = bridge.registry.open()
        bridge.registry.add(request_id, "partial")

        bridge.on_error(request_id, code, "simulated market-data error")

        with pytest.raises(error_type) as raised:
            bridge.registry.wait_sync(request_id, timeout=1.0)
        assert raised.value.observed_values == ("partial",)
        assert raised.value.broker_error_code == code

    @pytest.mark.parametrize(
        ("code", "error_type"),
        [
            (100, MarketDataPacingError),
            (101, MarketDataUnavailableError),
        ],
    )
    def test_idless_classified_error_fails_all_read_flights_with_typed_prefixes(
        self,
        bridge: CallbackBridge,
        code: int,
        error_type: type[BrokerDataError],
    ) -> None:
        first_id = bridge.registry.open(max_items=2)
        second_id = bridge.registry.open(max_items=2)
        bridge.registry.add(first_id, "first-prefix")
        bridge.registry.add(second_id, "second-prefix")
        rule_flight = bridge.begin_market_rule(26)

        bridge.on_error(-1, code, "simulated global market-data error")

        with pytest.raises(error_type) as first:
            bridge.registry.wait_sync(first_id, timeout=1.0)
        with pytest.raises(error_type) as second:
            bridge.registry.wait_sync(second_id, timeout=1.0)
        with pytest.raises(error_type) as rule:
            bridge.take_market_rule(26, rule_flight)
        assert first.value is not second.value
        assert first.value.observed_values == ("first-prefix",)
        assert second.value.observed_values == ("second-prefix",)
        assert first.value.broker_error_code == second.value.broker_error_code == code
        assert rule.value.broker_error_code == code
        assert rule.value.observed_values == ()
        assert rule.value.truncated is False

    def test_idless_error_is_notice(self, bridge: CallbackBridge) -> None:
        bridge.on_error(-1, 502, "Couldn't connect to TWS")
        assert any("502" in notice for notice in bridge.notices)

    def test_connection_closed_invalidates_reconciliation(self) -> None:
        reasons: list[str] = []
        guarded = CallbackBridge(RequestRegistry(), on_connection_uncertain=reasons.append)
        guarded.on_connection_closed()
        assert reasons == ["official IBKR connection closed"]

    @pytest.mark.parametrize("code", [1100, 1101, 1102, 1300, 2110])
    def test_connectivity_loss_or_restoration_invalidates(self, code: int) -> None:
        reasons: list[str] = []
        guarded = CallbackBridge(RequestRegistry(), on_connection_uncertain=reasons.append)
        guarded.on_error(-1, code, "connectivity changed")
        assert reasons == [f"official IBKR connectivity event {code}"]

    def test_managed_account_reordering_is_stable_but_scope_change_invalidates(self) -> None:
        reasons: list[str] = []
        guarded = CallbackBridge(RequestRegistry(), on_connection_uncertain=reasons.append)
        guarded.on_managed_accounts("DU111,DU222")

        guarded.on_managed_accounts("DU222,DU111")
        assert reasons == []

        guarded.on_managed_accounts("DU111,DU222,DU333")
        assert reasons == ["official IBKR managed-account scope changed"]

    def test_scope_invalidation_precedes_new_managed_account_publication(self) -> None:
        observed: list[tuple[str, tuple[str, ...]]] = []

        def observe(reason: str) -> None:
            observed.append((reason, guarded.managed_accounts))

        guarded = CallbackBridge(
            RequestRegistry(),
            on_connection_uncertain=observe,
        )
        guarded.on_managed_accounts("DU111")
        guarded.on_managed_accounts("DU222")

        assert observed == [
            (
                "official IBKR managed-account scope changed",
                ("DU111",),
            )
        ]
        assert guarded.managed_accounts == ("DU222",)

    def test_scope_change_blocks_rearm_until_new_accounts_are_published(self) -> None:
        readiness = ReconciliationReadiness()
        initial_generation = readiness.begin_reconciliation("initial broker scope")
        assert readiness.complete(
            expected_generation=initial_generation,
            status=ReconciliationStatus.RECONCILED,
            reason="initial scope reconciled",
            reconciled_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        transition_entered = Event()
        allow_publication = Event()
        reconciliation_started = Event()
        reconciliation_finished = Event()
        observed_scopes: list[tuple[str, ...]] = []
        published: list[bool] = []

        @contextmanager
        def paused_transition(reason: str) -> Iterator[None]:
            with readiness.invalidating_transition(reason):
                transition_entered.set()
                allow_publication.wait(timeout=2)
                yield

        guarded = CallbackBridge(
            RequestRegistry(),
            on_connection_uncertain=readiness.invalidate,
            on_managed_account_scope_change=paused_transition,
        )
        guarded.on_managed_accounts("DU111")
        callback_thread = Thread(
            target=guarded.on_managed_accounts,
            args=("DU111,U222",),
        )
        callback_thread.start()
        assert transition_entered.wait(timeout=1)

        def reconcile() -> None:
            reconciliation_started.set()
            with readiness.reconciliation_session("reconcile changed scope") as generation:
                observed_scopes.append(guarded.managed_accounts)
                published.append(
                    readiness.complete(
                        expected_generation=generation,
                        status=ReconciliationStatus.RECONCILED,
                        reason="changed scope reconciled",
                        reconciled_at=datetime(2026, 7, 21, tzinfo=UTC),
                    )
                )
            reconciliation_finished.set()

        reconciliation_thread = Thread(target=reconcile)
        reconciliation_thread.start()
        assert reconciliation_started.wait(timeout=1)
        assert reconciliation_finished.wait(timeout=0.05) is False
        assert guarded.managed_accounts == ("DU111",)

        allow_publication.set()
        callback_thread.join(timeout=1)
        reconciliation_thread.join(timeout=1)

        assert callback_thread.is_alive() is False
        assert reconciliation_thread.is_alive() is False
        assert guarded.managed_accounts == ("DU111", "U222")
        assert observed_scopes == [("DU111", "U222")]
        assert published == [True]
        assert readiness.snapshot().status is ReconciliationStatus.RECONCILED


class TestSingleFlights:
    def test_positions_flow(self, bridge: CallbackBridge) -> None:
        flight = bridge.start_positions()
        contract = SimpleNamespace(secType="STK", symbol="AAPL", conId=1)
        bridge.on_position("DU111", contract, 100.0, 150.0)
        bridge.on_position_end()
        assert flight.done.is_set()
        assert flight.items == [("DU111", contract, 100.0, 150.0)]

    def test_double_positions_request_refused(self, bridge: CallbackBridge) -> None:
        bridge.start_positions()
        with pytest.raises(RuntimeError, match="already in flight"):
            bridge.start_positions()

    def test_current_time_flow(self, bridge: CallbackBridge) -> None:
        flight = bridge.start_current_time()
        bridge.on_current_time(1_800_000_000)
        assert flight.done.is_set()
        assert flight.items == [1_800_000_000]

    def test_connection_closed_releases_waiters(self, bridge: CallbackBridge) -> None:
        flight = bridge.start_positions()
        bridge.on_connection_closed()
        assert flight.done.is_set()
        assert bridge.connection_closed.is_set()


class TestQuoteAccumulation:
    def test_snapshot_quote_lifecycle(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.open_quote(request_id)
        bridge.on_market_data_type(request_id, 1)
        bridge.on_tick_price(request_id, 1, 187.11)  # bid
        bridge.on_tick_price(request_id, 2, 187.31)  # ask
        bridge.on_tick_price(request_id, 4, 187.20)  # last
        bridge.on_tick_price(request_id, 9, 186.50)  # close
        bridge.on_tick_size(request_id, 8, 1_000_000)  # volume
        bridge.on_tick_option(request_id, 13, 0.29, -0.31, 0.02, -0.05)
        bridge.on_tick_snapshot_end(request_id)

        assert bridge.registry.wait_sync(request_id, timeout=1.0) == []
        state = bridge.close_quote(request_id)
        assert state is not None
        assert state.bid == Decimal("187.11")
        assert state.ask == Decimal("187.31")
        assert state.last == Decimal("187.2")
        assert state.close == Decimal("186.5")
        assert state.volume == 1_000_000
        assert state.delta == Decimal("-0.31")
        assert state.implied_volatility == Decimal("0.29")
        assert state.data_quality is DataQuality.LIVE

    def test_delayed_frozen_classification(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.open_quote(request_id)
        bridge.on_market_data_type(request_id, 4)
        state = bridge.quote_state(request_id)
        assert state is not None
        assert state.data_quality is DataQuality.DELAYED_FROZEN

    def test_non_model_option_tick_ignored(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.open_quote(request_id)
        bridge.on_tick_option(request_id, 10, 0.5, 0.5, 0.1, -0.1)  # BID computation
        state = bridge.quote_state(request_id)
        assert state is not None
        assert state.delta is None

    def test_sentinel_bid_stays_missing(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.open_quote(request_id)
        bridge.on_tick_price(request_id, 1, -1.0)  # "no bid" sentinel
        state = bridge.quote_state(request_id)
        assert state is not None
        assert state.bid is None

    @pytest.mark.parametrize(
        "callback_order",
        [
            ("quality", "bid", "ask", "volume", "oi", "model"),
            ("model", "oi", "volume", "ask", "quality", "bid"),
        ],
    )
    def test_streaming_option_completion_is_callback_order_independent(
        self,
        bridge: CallbackBridge,
        callback_order: tuple[str, ...],
    ) -> None:
        request_id = bridge.registry.open()
        bridge.open_option_quote(request_id, OptionRight.PUT)
        callbacks = {
            "quality": lambda: bridge.on_market_data_type(request_id, 1),
            "bid": lambda: bridge.on_tick_price(request_id, 1, 3.10),
            "ask": lambda: bridge.on_tick_price(request_id, 2, 3.25),
            "volume": lambda: bridge.on_tick_size(request_id, 30, 44),
            "oi": lambda: bridge.on_tick_size(request_id, 28, 555),
            "model": lambda: bridge.on_tick_option(request_id, 13, 0.25, -0.30, 0.02, -0.10),
        }

        for name in callback_order[:-1]:
            callbacks[name]()
            state = bridge.quote_state(request_id)
            assert state is not None
            assert state.completed_at is None
        callbacks[callback_order[-1]]()

        assert bridge.registry.wait_sync(request_id, timeout=0.01) == []
        state = bridge.close_quote(request_id)
        assert state is not None
        assert state.option_stream_complete is True
        assert state.volume == 44
        assert state.open_interest == 555

    def test_option_volume_and_open_interest_are_bound_to_exact_right(
        self, bridge: CallbackBridge
    ) -> None:
        request_id = bridge.registry.open()
        bridge.open_option_quote(request_id, OptionRight.PUT)

        bridge.on_tick_size(request_id, 29, 999)  # call volume
        bridge.on_tick_size(request_id, 27, 888)  # call open interest
        state = bridge.quote_state(request_id)

        assert state is not None
        assert state.volume is None
        assert state.open_interest is None
        assert state.volume_seen is False
        assert state.open_interest_seen is False

        bridge.on_tick_size(request_id, 30, -1)  # put volume sentinel
        bridge.on_tick_size(request_id, 28, 0)  # exact put OI callback
        updated = bridge.quote_state(request_id)
        assert updated is not None
        assert updated.volume is None
        assert updated.open_interest == 0
        assert updated.volume_seen is True
        assert updated.open_interest_seen is True


class TestMarketRules:
    def test_market_rule_parses_increments(self, bridge: CallbackBridge) -> None:
        flight = bridge.begin_market_rule(26)
        increments = [
            SimpleNamespace(lowEdge=0.0, increment=0.01),
            SimpleNamespace(lowEdge=3.0, increment=0.05),
        ]
        bridge.on_market_rule(26, increments)
        assert flight.done.is_set()
        assert bridge.take_market_rule(26, flight) == (
            (Decimal("0"), Decimal("0.01")),
            (Decimal("3"), Decimal("0.05")),
        )

        replacement = bridge.begin_market_rule(26)
        assert replacement.done.is_set() is False

    @pytest.mark.parametrize(
        "increments",
        [
            [SimpleNamespace(lowEdge=0.0)],
            [SimpleNamespace(lowEdge=1.0, increment=0.01)],
            [
                SimpleNamespace(lowEdge=0.0, increment=0.01),
                SimpleNamespace(lowEdge=0.0, increment=0.05),
            ],
            [SimpleNamespace(lowEdge=0.0, increment=-0.01)],
        ],
    )
    def test_malformed_market_rule_refuses_the_entire_schedule(
        self,
        bridge: CallbackBridge,
        increments: list[SimpleNamespace],
    ) -> None:
        flight = bridge.begin_market_rule(26)
        bridge.on_market_rule(26, increments)

        with pytest.raises(BrokerDataError, match="malformed"):
            bridge.take_market_rule(26, flight)

    def test_market_rule_overflow_retains_bounded_prefix(self, bridge: CallbackBridge) -> None:
        flight = bridge.begin_market_rule(26)
        increments = [
            SimpleNamespace(lowEdge=float(index), increment=0.01)
            for index in range(MAX_OPTION_MARKET_RULE_INCREMENTS + 1)
        ]

        bridge.on_market_rule(26, increments)

        with pytest.raises(BrokerDataError, match="hard evidence bound") as raised:
            bridge.take_market_rule(26, flight)
        assert raised.value.truncated is True
        assert len(raised.value.observed_values) == MAX_OPTION_MARKET_RULE_INCREMENTS
        assert raised.value.observed_values[0] == (Decimal("0"), Decimal("0.01"))

    def test_abandoned_rule_is_quarantined_until_reconnect(self, bridge: CallbackBridge) -> None:
        stale = bridge.begin_market_rule(26)
        bridge.abandon_market_rule(26, stale)
        bridge.on_market_rule(
            26,
            [SimpleNamespace(lowEdge=0.0, increment=0.01)],
        )

        with pytest.raises(BrokerDataError, match="quarantined"):
            bridge.begin_market_rule(26)

        bridge.reset_for_reconnect()
        fresh = bridge.begin_market_rule(26)
        assert fresh.done.is_set() is False


def _official_option(con_id: int, right: OptionRight) -> OptionContract:
    return OptionContract(
        con_id=con_id,
        symbol="AAPL",
        underlying_con_id=101,
        expiration=date(2026, 8, 21),
        strike=Decimal("185" if right is OptionRight.PUT else "200"),
        right=right,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol=f"AAPL-{con_id}",
    )


class _FakeOfficialApp:
    def __init__(
        self,
        bridge: CallbackBridge,
        *,
        expected_requests: int,
        fail_on_request: int | None = None,
        fail_cancellation: bool = False,
        callback_errors: dict[int, int] | None = None,
        chain_rows: tuple[tuple[Any, ...], ...] = (),
        finish_chain: bool = True,
        market_rule_ids: dict[int, int] | None = None,
        market_rule_responses: dict[int, tuple[Any, ...] | None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.expected_requests = expected_requests
        self.fail_on_request = fail_on_request
        self.fail_cancellation = fail_cancellation
        self.callback_errors = callback_errors or {}
        self.chain_rows = chain_rows
        self.finish_chain = finish_chain
        self.market_rule_ids = market_rule_ids or {}
        self.market_rule_responses = market_rule_responses or {}
        self.requests: list[tuple[int, int, str, bool, bool]] = []
        self.cancelled: list[int] = []
        self.request_count_at_first_callback: int | None = None

    def isConnected(self) -> bool:
        return True

    def reqMktData(
        self,
        request_id: int,
        contract: Any,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[Any],
    ) -> None:
        assert options == []
        self.requests.append(
            (
                request_id,
                int(contract.conId),
                generic_ticks,
                snapshot,
                regulatory_snapshot,
            )
        )
        if self.fail_on_request == len(self.requests):
            raise RuntimeError("simulated request send failure")
        if len(self.requests) != self.expected_requests:
            return

        self.request_count_at_first_callback = len(self.requests)
        for index, (req_id, con_id, _ticks, _snapshot, _regulatory) in enumerate(self.requests):
            if con_id in self.callback_errors:
                self.bridge.on_error(
                    req_id,
                    self.callback_errors[con_id],
                    "simulated callback error",
                )
                continue
            right = OptionRight.PUT if con_id == 202 else OptionRight.CALL
            self.bridge.on_tick_option(req_id, 13, 0.25, -0.30, 0.02, -0.10)
            self.bridge.on_tick_size(
                req_id,
                28 if right is OptionRight.PUT else 27,
                500 + index,
            )
            self.bridge.on_tick_size(
                req_id,
                30 if right is OptionRight.PUT else 29,
                40 + index,
            )
            self.bridge.on_tick_price(req_id, 2, 3.25 + index)
            self.bridge.on_market_data_type(req_id, 1)
            self.bridge.on_tick_price(req_id, 1, 3.10 + index)

    def reqSecDefOptParams(
        self,
        request_id: int,
        symbol: str,
        exchange: str,
        security_type: str,
        con_id: int,
    ) -> None:
        assert symbol == "AAPL"
        assert exchange == ""
        assert security_type == "STK"
        assert con_id == 101
        for row in self.chain_rows:
            self.bridge.on_sec_def_opt_params(request_id, *row)
        if self.finish_chain:
            self.bridge.on_sec_def_opt_params_end(request_id)

    def reqContractDetails(self, request_id: int, contract: Any) -> None:
        con_id = int(contract.conId)
        rule_id = self.market_rule_ids[con_id]
        self.bridge.on_contract_details(
            request_id,
            SimpleNamespace(
                contract=SimpleNamespace(conId=con_id),
                validExchanges="SMART",
                marketRuleIds=str(rule_id),
            ),
        )
        self.bridge.on_contract_details_end(request_id)

    def reqMarketRule(self, rule_id: int) -> None:
        response = self.market_rule_responses[rule_id]
        if response is not None:
            self.bridge.on_market_rule(rule_id, response)

    def cancelMktData(self, request_id: int) -> None:
        self.cancelled.append(request_id)
        if self.fail_cancellation:
            raise RuntimeError("simulated cancellation failure")


class _FakeOfficialContract:
    pass


def _official_broker_with_fake(app: _FakeOfficialApp) -> OfficialIBKRBroker:
    broker = object.__new__(OfficialIBKRBroker)
    broker.registry = app.bridge.registry
    broker.bridge = app.bridge
    broker._app = app
    broker._thread = None
    broker._active_market_data = {}
    return broker


def _official_chain_row(
    *,
    exchange: str = "SMART",
    expirations: tuple[str, ...] = ("20260821",),
    strikes: tuple[object, ...] = (Decimal("185"),),
) -> tuple[Any, ...]:
    return (exchange, 101, "AAPL", "100", expirations, strikes)


@pytest.mark.asyncio
async def test_official_chain_timeout_retains_partial_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=0,
        chain_rows=(_official_chain_row(),),
        finish_chain=False,
    )
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr("chronos.broker.official_ibkr._REQUEST_TIMEOUT_S", 0.01)

    with pytest.raises(BrokerDataError, match="timed out") as raised:
        await broker.option_chain_parameters(UnderlyingContract(con_id=101, symbol="AAPL"))

    response = raised.value.chain_response
    assert response is not None
    assert len(response.parameters) == 1
    assert response.parameters[0].strikes == (Decimal("185"),)
    assert response.complete is False
    assert response.truncated is False
    assert response.completion_marker == "request-registry-timeout"
    assert response.source == "ibapi-v1"
    assert response.observed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_manager_deadline_retains_partial_official_chain_and_consumes_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=0,
        chain_rows=(_official_chain_row(),),
        finish_chain=False,
    )
    broker = _official_broker_with_fake(app)
    manager = MarketDataManager(
        broker,
        request_timeout_seconds=0.01,
        max_attempts=1,
    )
    monkeypatch.setattr("chronos.broker.official_ibkr._REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(MarketDataUnavailableError, match="interrupted") as raised:
        await manager.option_chain_parameters(
            UnderlyingContract(con_id=101, symbol="AAPL"),
            force_refresh=True,
        )

    response = raised.value.chain_response
    assert response is not None
    assert len(response.parameters) == 1
    assert response.complete is False
    assert response.completion_marker == "request-registry-interrupted"
    assert raised.value.request_completed is False
    assert bridge.registry.pending_count == 0


@pytest.mark.asyncio
async def test_manager_deadline_retains_completed_official_market_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=0,
        market_rule_ids={202: 26, 203: 27},
        market_rule_responses={
            26: (
                SimpleNamespace(lowEdge=0.0, increment=0.01),
                SimpleNamespace(lowEdge=3.0, increment=0.05),
            ),
            27: None,
        },
    )
    broker = _official_broker_with_fake(app)
    manager = MarketDataManager(
        broker,
        request_timeout_seconds=0.01,
        max_attempts=1,
    )
    monkeypatch.setattr("chronos.broker.official_ibkr._REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(MarketDataUnavailableError, match="interrupted") as raised:
        await manager.option_market_rules(
            (_official_option(202, OptionRight.PUT), _official_option(203, OptionRight.CALL))
        )

    observed_rules = tuple(
        value for value in raised.value.observed_values if isinstance(value, OptionMarketRule)
    )
    assert tuple(rule.con_id for rule in observed_rules) == (202,)
    assert raised.value.contract_ids == (203,)
    assert raised.value.request_completed is False
    assert bridge._market_rule_flights == {}


@pytest.mark.asyncio
async def test_official_market_rule_overflow_retains_adapter_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=0,
        market_rule_ids={202: 26},
        market_rule_responses={
            26: tuple(
                SimpleNamespace(lowEdge=float(index), increment=0.01)
                for index in range(MAX_OPTION_MARKET_RULE_INCREMENTS + 1)
            )
        },
    )
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(BrokerDataError, match="hard evidence bound") as raised:
        await broker.option_market_rules((_official_option(202, OptionRight.PUT),))

    increments = tuple(
        value
        for value in raised.value.observed_values
        if isinstance(value, tuple) and len(value) == 2
    )
    assert len(increments) == MAX_OPTION_MARKET_RULE_INCREMENTS
    assert raised.value.truncated is True
    assert raised.value.contract_ids == (202,)


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["rows", "expirations", "strikes"])
async def test_official_chain_overflow_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    dimension: str,
) -> None:
    if dimension == "rows":
        rows = tuple(
            _official_chain_row(exchange=f"EX{index:02d}")
            for index in range(MAX_OPTION_CHAIN_ROWS + 1)
        )
    elif dimension == "expirations":
        start = date(2026, 1, 1)
        expirations = tuple(
            (start + timedelta(days=index)).strftime("%Y%m%d")
            for index in range(MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW + 1)
        )
        rows = (_official_chain_row(expirations=expirations),)
    else:
        strikes = tuple(Decimal(index) for index in range(1, MAX_OPTION_CHAIN_STRIKES_PER_ROW + 2))
        rows = (_official_chain_row(strikes=strikes),)

    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(bridge, expected_requests=0, chain_rows=rows)
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr("chronos.broker.official_ibkr._REQUEST_TIMEOUT_S", 0.1)

    with pytest.raises(BrokerDataError, match="bounded callback evidence limit") as raised:
        await broker.option_chain_parameters(UnderlyingContract(con_id=101, symbol="AAPL"))

    response = raised.value.chain_response
    assert response is not None
    assert response.complete is True
    assert response.truncated is True
    assert response.completion_marker == "securityDefinitionOptionParameterEnd"
    assert raised.value.failure_kind == "overflow"
    assert len(response.parameters) <= MAX_OPTION_CHAIN_ROWS
    assert all(
        len(parameter.expirations) <= MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW
        and len(parameter.strikes) <= MAX_OPTION_CHAIN_STRIKES_PER_ROW
        for parameter in response.parameters
    )


@pytest.mark.asyncio
async def test_official_option_batch_starts_streams_concurrently_and_cancels_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(bridge, expected_requests=2)
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    quotes = await broker.request_option_quotes(
        (_official_option(202, OptionRight.PUT), _official_option(203, OptionRight.CALL))
    )

    assert app.request_count_at_first_callback == 2
    assert [request[2:] for request in app.requests] == [
        ("100,101", False, False),
        ("100,101", False, False),
    ]
    assert tuple(quote.contract.con_id for quote in quotes) == (202, 203)
    assert tuple(quote.volume for quote in quotes) == (40, 41)
    assert tuple(quote.open_interest for quote in quotes) == (500, 501)
    assert app.cancelled == [app.requests[0][0], app.requests[1][0]]
    assert broker._active_market_data == {}


@pytest.mark.asyncio
async def test_official_option_batch_retains_completed_quote_when_peer_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=2,
        callback_errors={203: 10089},
    )
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(MarketDataPermissionError) as raised:
        await broker.request_option_quotes(
            (_official_option(202, OptionRight.PUT), _official_option(203, OptionRight.CALL))
        )

    observed = tuple(
        value for value in raised.value.observed_values if isinstance(value, MarketQuote)
    )
    assert tuple(quote.contract.con_id for quote in observed) == (202,)
    assert raised.value.contract_ids == (203,)
    assert app.cancelled == [app.requests[0][0], app.requests[1][0]]
    assert broker._active_market_data == {}


@pytest.mark.asyncio
async def test_official_cleanup_failure_wins_without_discarding_completed_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=2,
        callback_errors={203: 10089},
        fail_cancellation=True,
    )
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(MarketDataCancellationError, match="could not cancel") as raised:
        await broker.request_option_quotes(
            (_official_option(202, OptionRight.PUT), _official_option(203, OptionRight.CALL))
        )

    assert isinstance(raised.value.__cause__, MarketDataPermissionError)
    assert tuple(
        quote.contract.con_id
        for quote in raised.value.observed_values
        if isinstance(quote, MarketQuote)
    ) == (202,)
    assert app.cancelled == [app.requests[0][0], app.requests[1][0]]
    assert set(broker._active_market_data) == {202, 203}


@pytest.mark.asyncio
async def test_official_option_batch_cancels_every_started_stream_after_send_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(bridge, expected_requests=2, fail_on_request=2)
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(BrokerDataError, match="could not be sent"):
        await broker.request_option_quotes(
            (_official_option(202, OptionRight.PUT), _official_option(203, OptionRight.CALL))
        )

    assert app.cancelled == [app.requests[0][0], app.requests[1][0]]
    assert broker._active_market_data == {}


@pytest.mark.asyncio
async def test_official_option_cancellation_failure_is_explicit_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = CallbackBridge(RequestRegistry())
    app = _FakeOfficialApp(
        bridge,
        expected_requests=1,
        fail_cancellation=True,
    )
    broker = _official_broker_with_fake(app)
    monkeypatch.setattr(
        "chronos.broker.official_ibkr._load_ibapi",
        lambda: (object, object, _FakeOfficialContract, object),
    )

    with pytest.raises(BrokerDataError, match="could not cancel"):
        await broker.request_option_quotes((_official_option(202, OptionRight.PUT),))

    request_id = app.requests[0][0]
    assert app.cancelled == [request_id]
    assert broker._active_market_data == {202: request_id}

    app.fail_cancellation = False
    await broker.cancel_market_data((202,))
    assert app.cancelled == [request_id, request_id]
    assert broker._active_market_data == {}


class TestCommissionReports:
    def test_commission_stored_and_fetched(self, bridge: CallbackBridge) -> None:
        report = SimpleNamespace(execId="abc-1", commission=1.05, currency="USD")
        bridge.on_commission_report(report)
        assert bridge.commission_for("abc-1") == (Decimal("1.05"), "USD")
        assert bridge.commission_for("missing") == (None, None)

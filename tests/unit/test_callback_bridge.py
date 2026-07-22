"""Callback bridge: routing, sentinel hygiene, quote accumulation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from chronos.broker.base import BrokerDataError
from chronos.broker.callbacks import CallbackBridge, clean_price, clean_ratio, clean_size
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import DataQuality, ReconciliationStatus
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

    def test_request_scoped_error_fails_request(self, bridge: CallbackBridge) -> None:
        request_id = bridge.registry.open()
        bridge.on_error(request_id, 200, "No security definition")
        with pytest.raises(BrokerDataError, match="broker error 200"):
            bridge.registry.wait_sync(request_id, timeout=1.0)

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


class TestMarketRules:
    def test_market_rule_parses_increments(self, bridge: CallbackBridge) -> None:
        event = bridge.expect_market_rule(26)
        increments = [
            SimpleNamespace(lowEdge=0.0, increment=0.01),
            SimpleNamespace(lowEdge=3.0, increment=0.05),
        ]
        bridge.on_market_rule(26, increments)
        assert event.is_set()
        assert bridge.market_rule(26) == [
            (Decimal("0"), Decimal("0.01")),
            (Decimal("3"), Decimal("0.05")),
        ]


class TestCommissionReports:
    def test_commission_stored_and_fetched(self, bridge: CallbackBridge) -> None:
        report = SimpleNamespace(execId="abc-1", commission=1.05, currency="USD")
        bridge.on_commission_report(report)
        assert bridge.commission_for("abc-1") == (Decimal("1.05"), "USD")
        assert bridge.commission_for("missing") == (None, None)

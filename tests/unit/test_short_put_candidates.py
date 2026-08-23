from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar, cast

import pytest

from chronos.broker.base import Broker, BrokerDataError, OptionChainResponse
from chronos.broker.market_data import MarketDataManager
from chronos.config.limits import MAX_CANDIDATE_EXPIRATIONS
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerPosition,
    ConnectionStatus,
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    UnderlyingContract,
)
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationResult,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.strategy.strike_resolver import CandidateRejectionCode
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.logging import mask_account_id

_T = TypeVar("_T")

ACCOUNT_ID = "DU1234567"
OTHER_ACCOUNT_ID = "DU7654321"
NOW = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)


class _Runner:
    def __init__(self, broker: _CandidateBroker) -> None:
        self.broker = cast(Broker, broker)
        self.run_calls = 0
        self.inside_run = False
        self.timeouts: list[float | None] = []
        broker.runner = self

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T:
        self.run_calls += 1
        self.timeouts.append(timeout)
        self.inside_run = True
        try:
            return asyncio.run(coroutine)
        finally:
            self.inside_run = False


class _ReconciliationReader:
    def __init__(
        self,
        result: ReconciliationResult,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls = 0

    def reconcile(self) -> ReconciliationResult:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class _CandidateBroker:
    def __init__(
        self,
        *,
        account_first: AccountSummary | None = None,
        account_second: AccountSummary | None = None,
        positions: tuple[
            tuple[BrokerPosition, ...],
            tuple[BrokerPosition, ...],
        ] = ((), ()),
        deliverable_verified: bool = True,
        ambiguous_chain: bool = False,
        option_quote_failure: Exception | None = None,
        cancellation_failure: Exception | None = None,
        status_qualities: tuple[DataQuality, DataQuality] = (
            DataQuality.DEMO,
            DataQuality.DEMO,
        ),
        quote_quality: DataQuality = DataQuality.DEMO,
        mutate_quoted_contract: bool = False,
        unverified_strikes: frozenset[Decimal] = frozenset(),
    ) -> None:
        account = account_first or _account()
        self.accounts = (account, account_second or account)
        self.positions_values = positions
        self.deliverable_verified = deliverable_verified
        self.ambiguous_chain = ambiguous_chain
        self.option_quote_failure = option_quote_failure
        self.cancellation_failure = cancellation_failure
        self.status_qualities = status_qualities
        self.quote_quality = quote_quality
        self.mutate_quoted_contract = mutate_quoted_contract
        self.unverified_strikes = unverified_strikes
        self.runner: _Runner | None = None
        self.calls: list[str] = []
        self.cancellations: list[tuple[int, ...]] = []
        self.option_quote_contract_ids: list[tuple[int, ...]] = []
        self._counts = {
            "status": 0,
            "account": 0,
            "positions": 0,
            "orders": 0,
            "executions": 0,
        }
        self.underlying = UnderlyingContract(
            con_id=1001,
            symbol="AAPL",
            exchange="SMART",
            primary_exchange="NASDAQ",
            currency="USD",
        )

    def _record(self, name: str) -> None:
        assert self.runner is not None and self.runner.inside_run
        self.calls.append(name)

    async def connection_status(self) -> ConnectionStatus:
        self._record("status")
        index = self._counts["status"]
        self._counts["status"] += 1
        return ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.DEMO,
            connected=True,
            account_id=ACCOUNT_ID,
            data_quality=self.status_qualities[index],
            last_successful_sync=NOW,
        )

    async def account_summary(self) -> AccountSummary:
        self._record("account")
        index = self._counts["account"]
        self._counts["account"] += 1
        return self.accounts[index]

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self._record("positions")
        index = self._counts["positions"]
        self._counts["positions"] += 1
        return self.positions_values[index]

    async def open_orders(self) -> tuple[Any, ...]:
        self._record("orders")
        self._counts["orders"] += 1
        return ()

    async def executions(self, since: datetime | None = None) -> tuple[Any, ...]:
        self._record("executions")
        assert since is None
        self._counts["executions"] += 1
        return ()

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self._record("qualify_underlying")
        assert symbol == "AAPL"
        return self.underlying

    async def request_underlying_quote(
        self,
        contract: UnderlyingContract,
    ) -> MarketQuote:
        self._record("underlying_quote")
        assert contract == self.underlying
        return MarketQuote(
            contract=contract,
            timestamp=NOW,
            data_quality=self.quote_quality,
            bid=Decimal("190.20"),
            ask=Decimal("190.30"),
            last=Decimal("190.25"),
            volume=1_000_000,
        )

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> OptionChainResponse:
        self._record("option_chain")
        chain = OptionChainParameters(
            exchange="SMART",
            underlying_con_id=underlying.con_id,
            trading_class=underlying.symbol,
            multiplier=Decimal("100"),
            expirations=(
                NOW.date() + timedelta(days=14),
                NOW.date() + timedelta(days=21),
            ),
            strikes=(Decimal("175"), Decimal("180"), Decimal("185")),
        )
        return OptionChainResponse(
            parameters=(chain, chain) if self.ambiguous_chain else (chain,),
            complete=True,
            truncated=False,
            completion_marker="synthetic-security-definition-option-parameter-end",
            observed_at=NOW,
            source="synthetic-short-put-candidate-broker",
        )

    async def qualify_option_contracts(
        self,
        specs: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        self._record("qualify_options")
        return tuple(
            OptionContract(
                **spec.model_dump(),
                con_id=2001 + index,
                local_symbol=(
                    f"{spec.symbol} {spec.expiration:%y%m%d}"
                    f"{spec.right.value}{int(spec.strike * 1000):08d}"
                ),
                deliverable_shares=(
                    spec.multiplier
                    if self.deliverable_verified and spec.strike not in self.unverified_strikes
                    else None
                ),
                deliverable_verified=(
                    self.deliverable_verified and spec.strike not in self.unverified_strikes
                ),
            )
            for index, spec in enumerate(specs)
        )

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        self._record("option_quotes")
        self.option_quote_contract_ids.append(tuple(contract.con_id for contract in contracts))
        if self.option_quote_failure is not None:
            raise self.option_quote_failure
        deltas = {
            Decimal("175"): Decimal("-0.22"),
            Decimal("180"): Decimal("-0.28"),
            Decimal("185"): Decimal("-0.34"),
        }
        quotes = tuple(
            MarketQuote(
                contract=(
                    contract.model_copy(
                        update={
                            "strike": contract.strike - Decimal("5"),
                            "local_symbol": f"{contract.local_symbol}-MUTATED",
                        }
                    )
                    if self.mutate_quoted_contract and index == 0
                    else contract
                ),
                timestamp=NOW,
                data_quality=self.quote_quality,
                bid=Decimal("1.90"),
                ask=Decimal("2.00"),
                last=Decimal("1.95"),
                volume=500,
                open_interest=2_000,
                greeks=ModelGreeks(
                    delta=deltas[contract.strike],
                    gamma=Decimal("0.02"),
                    theta=Decimal("-0.08"),
                    implied_volatility=Decimal("0.24"),
                ),
            )
            for index, contract in enumerate(contracts)
        )
        return quotes

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        self._record("cancel_market_data")
        values = tuple(contract_ids)
        self.cancellations.append(values)
        if self.cancellation_failure is not None:
            raise self.cancellation_failure

    async def preview_order(self, _request: object) -> None:
        raise AssertionError("candidate evaluation must not preview an order")

    async def submit_order(self, _request: object) -> None:
        raise AssertionError("candidate evaluation must not submit an order")

    async def modify_order(self, _request: object) -> None:
        raise AssertionError("candidate evaluation must not modify an order")

    async def cancel_order(self, _broker_order_id: int) -> None:
        raise AssertionError("candidate evaluation must not cancel an order")


def _account(*, cash: str = "125000", as_of: datetime = NOW) -> AccountSummary:
    return AccountSummary(
        account_id=ACCOUNT_ID,
        net_liquidation=Decimal("250000"),
        total_cash=Decimal(cash),
        buying_power=Decimal("240000"),
        currency="USD",
        as_of=as_of,
    )


def _symbol(
    *,
    status: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    stage: WheelStage = WheelStage.FLAT,
    stock_shares: str = "0",
    manual_review_required: bool = False,
) -> SymbolReconciliation:
    return SymbolReconciliation(
        symbol="AAPL",
        status=status,
        stage=stage,
        stock_shares=Decimal(stock_shares),
        unencumbered_shares=Decimal("0"),
        short_put_contracts=Decimal("0"),
        short_call_contracts=Decimal("0"),
        pending_put_contracts=Decimal("0"),
        pending_call_contracts=Decimal("0"),
        manual_review_required=manual_review_required,
        reasons=(),
    )


def _reconciliation(
    *,
    captured_at: datetime = NOW,
    status: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    symbol: SymbolReconciliation | None = None,
    account: AccountSummary | None = None,
    execution_count: int = 0,
    data_quality: DataQuality = DataQuality.DEMO,
) -> ReconciliationResult:
    account_value = account or _account()
    return ReconciliationResult(
        status=status,
        snapshot=ReconciliationSnapshotView(
            environment=DisplayEnvironment.DEMO,
            data_quality=data_quality,
            account=ReconciliationAccountView(
                masked_account_id=mask_account_id(account_value.account_id),
                net_liquidation=account_value.net_liquidation,
                total_cash=account_value.total_cash,
                buying_power=account_value.buying_power,
                currency=account_value.currency,
                as_of=account_value.as_of,
            ),
            positions=(),
            open_orders=(),
            execution_count=execution_count,
            server_time_start=captured_at,
            server_time_end=captured_at,
            captured_at=captured_at,
            window_seconds=0,
            server_window_seconds=0,
        ),
        symbols=(symbol or _symbol(),),
        reasons=(),
    )


def _settings() -> Settings:
    return Settings(
        symbol_allowlist=("AAPL",),
        max_expirations=2,
        max_strikes_per_expiration=3,
    )


def _service(
    broker: _CandidateBroker,
    reconciliation: ReconciliationResult,
    *,
    expected_account_id: str = ACCOUNT_ID,
    service_clock: Callable[[], datetime] = lambda: NOW,
) -> tuple[ShortPutCandidateService, _Runner, _ReconciliationReader, MarketDataManager]:
    runner = _Runner(broker)
    reader = _ReconciliationReader(reconciliation)
    market_data = MarketDataManager(
        cast(Broker, broker),
        clock=service_clock,
        max_quote_age=timedelta(seconds=5),
    )
    service = ShortPutCandidateService(
        runner,
        market_data,
        reader,
        _settings(),
        account_fingerprint(expected_account_id),
        clock=lambda: NOW,
    )
    return service, runner, reader, market_data


def _changed_position() -> BrokerPosition:
    return BrokerPosition(
        account_id=ACCOUNT_ID,
        contract=UnderlyingContract(
            con_id=1001,
            symbol="AAPL",
            exchange="SMART",
            primary_exchange="NASDAQ",
        ),
        quantity=Decimal("100"),
        average_cost=Decimal("180"),
        market_price=Decimal("190"),
    )


def _rejection_codes(result: Any) -> set[CandidateRejectionCode]:
    assert result.resolution is not None
    return {
        reason.code
        for rejected in result.resolution.rejected
        for reason in rejected.rejection_reasons
    }


def test_fresh_empty_account_ranks_puts_in_one_serialized_read_only_run() -> None:
    broker = _CandidateBroker()
    service, runner, reader, market_data = _service(broker, _reconciliation())

    result = service.evaluate("aapl")

    assert result.status is EligibilityStatus.ELIGIBLE
    assert result.opening_actions_locked is True
    assert result.resolution is not None
    assert len(result.resolution.candidates) == 6
    assert all(
        candidate.right is OptionRight.PUT
        and candidate.capital_check.passed
        and candidate.capital_check.resulting_total_wheel_allocation
        == candidate.capital_check.proposal_amount
        for candidate in result.resolution.candidates
    )
    assert result.resolution.selected is not None
    assert result.resolution.selected.delta == Decimal("-0.28")
    serialized = result.model_dump_json()
    assert ACCOUNT_ID not in serialized
    assert account_fingerprint(ACCOUNT_ID) not in serialized
    assert reader.calls == 1
    assert runner.run_calls == 1
    assert runner.timeouts == [30.0]
    assert broker.calls[:6] == [
        "status",
        "account",
        "positions",
        "orders",
        "executions",
        "qualify_underlying",
    ]
    assert broker.calls[-5:] == [
        "positions",
        "orders",
        "executions",
        "account",
        "status",
    ]
    assert broker.cancellations[0] == (1001,)
    assert set(broker.cancellations[1]) == {
        candidate.contract.con_id for candidate in result.resolution.candidates
    }
    assert market_data.active_subscription_count == 0


def test_snapshot_captured_during_reconciliation_uses_post_reconcile_clock() -> None:
    clock_calls = 0

    def progressing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW - timedelta(seconds=2) if clock_calls == 1 else NOW

    broker = _CandidateBroker()
    service, runner, _reader, _market_data = _service(
        broker,
        _reconciliation(captured_at=NOW - timedelta(seconds=1)),
        service_clock=progressing_clock,
    )

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.ELIGIBLE
    assert runner.run_calls == 1


def test_service_rechecks_hard_candidate_bounds_before_broker_access() -> None:
    broker = _CandidateBroker()
    runner = _Runner(broker)
    reader = _ReconciliationReader(_reconciliation())
    market_data = MarketDataManager(cast(Broker, broker), clock=lambda: NOW)
    unsafe_settings = _settings().model_copy(
        update={"max_expirations": MAX_CANDIDATE_EXPIRATIONS + 1}
    )

    with pytest.raises(ValueError, match="hard safety limits"):
        ShortPutCandidateService(
            runner,
            market_data,
            reader,
            unsafe_settings,
            account_fingerprint(ACCOUNT_ID),
            clock=lambda: NOW,
        )

    assert reader.calls == 0
    assert runner.run_calls == 0
    assert broker.calls == []


def test_unknown_connection_quality_can_bootstrap_to_live_quotes() -> None:
    broker = _CandidateBroker(
        status_qualities=(DataQuality.UNKNOWN, DataQuality.LIVE),
        quote_quality=DataQuality.LIVE,
    )
    service, runner, _reader, _market_data = _service(
        broker,
        _reconciliation(data_quality=DataQuality.UNKNOWN),
    )

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.ELIGIBLE
    assert result.resolution is not None
    assert runner.run_calls == 1


@pytest.mark.parametrize("quality", [DataQuality.DELAYED, DataQuality.STALE])
def test_nonrankable_quote_quality_remains_no_trade(quality: DataQuality) -> None:
    broker = _CandidateBroker(
        status_qualities=(DataQuality.UNKNOWN, DataQuality.LIVE),
        quote_quality=quality,
    )
    service, _runner, _reader, _market_data = _service(
        broker,
        _reconciliation(data_quality=DataQuality.UNKNOWN),
    )

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is not None
    assert CandidateRejectionCode.DATA_QUALITY in _rejection_codes(result)


@pytest.mark.parametrize(
    ("reconciliation", "reason_fragment"),
    [
        (
            _reconciliation(captured_at=NOW - timedelta(seconds=6)),
            "too old",
        ),
        (
            _reconciliation(captured_at=NOW + timedelta(seconds=1)),
            "future",
        ),
        (
            _reconciliation(status=ReconciliationStatus.MANUAL_REVIEW),
            "does not have fresh reconciled",
        ),
        (
            _reconciliation(
                symbol=_symbol(
                    status=ReconciliationStatus.MANUAL_REVIEW,
                    stage=WheelStage.MANUAL_REVIEW,
                    manual_review_required=True,
                )
            ),
            "not freshly reconciled and flat",
        ),
        (
            _reconciliation(symbol=_symbol(stage=WheelStage.LONG_STOCK, stock_shares="100")),
            "still has reconciled exposure",
        ),
        (
            _reconciliation(execution_count=1),
            "Whole-account allocation is not proven empty",
        ),
    ],
)
def test_preflight_failures_never_touch_market_data(
    reconciliation: ReconciliationResult,
    reason_fragment: str,
) -> None:
    broker = _CandidateBroker()
    service, runner, reader, _market_data = _service(broker, reconciliation)

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.opening_actions_locked is True
    assert any(reason_fragment in reason for reason in result.reasons)
    assert reader.calls == 1
    assert runner.run_calls == 0
    assert broker.calls == []


def test_invalid_symbol_does_not_even_reconcile() -> None:
    broker = _CandidateBroker()
    service, runner, reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("MSFT")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.reconciliation is None
    assert reader.calls == 0
    assert runner.run_calls == 0


def test_explicit_zero_reconciliation_age_is_rejected() -> None:
    broker = _CandidateBroker()
    runner = _Runner(broker)
    reader = _ReconciliationReader(_reconciliation())
    market_data = MarketDataManager(cast(Broker, broker), clock=lambda: NOW)

    with pytest.raises(ValueError, match="max_reconciliation_age must be positive"):
        ShortPutCandidateService(
            runner,
            market_data,
            reader,
            _settings(),
            account_fingerprint(ACCOUNT_ID),
            clock=lambda: NOW,
            max_reconciliation_age=timedelta(0),
        )


def test_changed_exposure_after_reconciliation_locks_without_resolution() -> None:
    broker = _CandidateBroker(positions=((), (_changed_position(),)))
    service, runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert any("exposure changed" in reason for reason in result.reasons)
    assert runner.run_calls == 1
    assert result.underlying_quote is not None


def test_changed_account_capital_locks_before_resolver() -> None:
    broker = _CandidateBroker(
        account_first=_account(),
        account_second=_account(cash="124999"),
    )
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert any("capital values changed" in reason for reason in result.reasons)


def test_low_cash_is_transparently_rejected_by_resolver_capital_check() -> None:
    account = _account(cash="100")
    broker = _CandidateBroker(account_first=account)
    service, _runner, _reader, _market_data = _service(
        broker,
        _reconciliation(account=account),
    )

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is not None
    assert CandidateRejectionCode.CAPITAL_LIMIT in _rejection_codes(result)


def test_unverified_deliverables_fail_before_option_quote_fanout() -> None:
    broker = _CandidateBroker(deliverable_verified=False)
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("No qualified option contract has a verified standard deliverable.",)
    assert "option_quotes" not in broker.calls


def test_mixed_deliverables_quote_only_verified_standard_contracts() -> None:
    broker = _CandidateBroker(unverified_strikes=frozenset({Decimal("175")}))
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.ELIGIBLE
    assert result.resolution is not None
    assert all(candidate.strike != Decimal("175") for candidate in result.resolution.candidates)
    assert len(broker.option_quote_contract_ids) == 1
    assert len(broker.option_quote_contract_ids[0]) == 4


def test_account_observation_time_must_not_move_backward() -> None:
    broker = _CandidateBroker(
        account_first=_account(as_of=NOW),
        account_second=_account(as_of=NOW - timedelta(seconds=1)),
    )
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("Account observation timestamps were invalid or moved backward.",)


def test_account_observation_time_must_remain_timezone_aware() -> None:
    valid = _account()
    naive = AccountSummary.model_construct(
        account_id=valid.account_id,
        net_liquidation=valid.net_liquidation,
        total_cash=valid.total_cash,
        buying_power=valid.buying_power,
        currency=valid.currency,
        as_of=NOW.replace(tzinfo=None),
    )
    broker = _CandidateBroker(account_first=valid, account_second=naive)
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("Account observation timestamps were invalid or moved backward.",)


def test_ambiguous_chain_is_locked_before_contract_or_option_quote_requests() -> None:
    broker = _CandidateBroker(ambiguous_chain=True)
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("Option-chain routing identity was missing or ambiguous.",)
    assert "qualify_options" not in broker.calls
    assert "option_quotes" not in broker.calls
    assert broker.cancellations == [(1001,)]


def test_mutated_quote_contract_identity_is_locked_before_resolution() -> None:
    broker = _CandidateBroker(mutate_quoted_contract=True)
    service, _runner, _reader, _market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("Option quote identity does not match its qualified contract.",)


def test_option_quote_failure_cancels_subscriptions_and_returns_sanitized_lock() -> None:
    broker = _CandidateBroker(option_quote_failure=BrokerDataError("sensitive broker detail"))
    service, _runner, _reader, market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("Candidate market evidence could not be captured safely.",)
    assert len(broker.cancellations) == 2
    assert market_data.active_subscription_count == 0
    assert "sensitive broker detail" not in result.model_dump_json()


def test_cancellation_failure_quarantines_market_data_and_keeps_output_sanitized() -> None:
    broker = _CandidateBroker(cancellation_failure=BrokerDataError("subscription cleanup detail"))
    service, _runner, _reader, market_data = _service(broker, _reconciliation())

    result = service.evaluate("AAPL")

    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.opening_actions_locked is True
    assert market_data.active_subscription_count == 1
    assert "subscription cleanup detail" not in result.model_dump_json()


def test_wrong_bound_account_never_leaks_raw_identifier() -> None:
    broker = _CandidateBroker()
    service, runner, _reader, _market_data = _service(
        broker,
        _reconciliation(),
        expected_account_id=OTHER_ACCOUNT_ID,
    )

    result = service.evaluate("AAPL")

    rendered = result.model_dump_json()
    assert result.status is EligibilityStatus.NO_TRADE
    assert result.resolution is None
    assert result.reasons == ("The broker account does not match the bound candidate scope.",)
    assert ACCOUNT_ID not in rendered
    assert OTHER_ACCOUNT_ID not in rendered
    assert runner.run_calls == 1
    assert "qualify_underlying" not in broker.calls

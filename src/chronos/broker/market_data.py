"""Broker-neutral, bounded market-data orchestration.

The manager owns batching, pacing retries, subscription cleanup, and short-lived
caches. Broker adapters remain responsible for translating vendor errors into the
explicit exception types below (or supplying a pacing classifier).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TypeVar

from chronos.broker.base import Broker, BrokerDataError, OptionChainResponse
from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
)
from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import (
    CryptoContract,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    UnderlyingContract,
)

T = TypeVar("T")
AsyncSleeper = Callable[[float], Awaitable[None]]
ErrorClassifier = Callable[[BaseException], bool]


class MarketDataPermissionError(BrokerDataError):
    """The connected account lacks permission for the requested market data."""


class MarketDataPacingError(BrokerDataError):
    """The broker explicitly classified a failure as a pacing violation."""


class MarketDataUnavailableError(BrokerDataError):
    """The broker omitted or mismatched requested market data.

    ``observed_values`` retains the bounded prefix the broker did return.  A
    fail-closed caller must not turn a partial response into success, but the
    evidence receipt must still say what was actually observed.
    """

    def __init__(
        self,
        message: str,
        *,
        contract_ids: Sequence[int] = (),
        observed_values: Sequence[object] = (),
        observed_at: datetime | None = None,
        truncated: bool = False,
        chain_response: OptionChainResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.contract_ids = tuple(contract_ids)
        self.observed_values = tuple(observed_values)
        self.observed_at = observed_at
        self.truncated = truncated
        self.chain_response = chain_response


class MarketDataPacingExhaustedError(BrokerDataError):
    """All bounded attempts failed with classified pacing violations."""

    def __init__(
        self,
        operation: str,
        attempts: int,
        *,
        contract_ids: Sequence[int] = (),
        observed_values: Sequence[object] = (),
        observed_at: datetime | None = None,
        truncated: bool = False,
        chain_response: OptionChainResponse | None = None,
    ) -> None:
        super().__init__(f"{operation} exhausted {attempts} pacing-limited attempts")
        self.operation = operation
        self.attempts = attempts
        self.contract_ids = tuple(contract_ids)[:MAX_CANDIDATE_REQUEST_CONTRACTS]
        self.observed_values = tuple(observed_values)[:MAX_CANDIDATE_REQUEST_CONTRACTS]
        self.observed_at = observed_at
        self.truncated = truncated
        self.chain_response = chain_response


class MarketDataCancellationError(BrokerDataError):
    """A subscription could not be cancelled; any observed values stay attached."""

    def __init__(
        self,
        message: str,
        *,
        contract_ids: Sequence[int] = (),
        observed_values: Sequence[object] = (),
        observed_at: datetime | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.contract_ids = tuple(contract_ids)
        self.observed_values = tuple(observed_values)
        self.observed_at = observed_at
        self.truncated = truncated


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """Option-chain metadata plus observable cache freshness."""

    parameters: tuple[OptionChainParameters, ...]
    fetched_at: datetime
    observed_at: datetime
    from_cache: bool
    fresh: bool
    complete: bool
    truncated: bool
    completion_marker: str
    completion_observed_at: datetime
    completion_source: str

    @property
    def cache_age_seconds(self) -> float:
        return (self.observed_at - self.fetched_at).total_seconds()


@dataclass(frozen=True, slots=True)
class ManagedQuote:
    """A quote annotated with temporal freshness and transmission eligibility.

    ``fresh`` is deliberately temporal: it says only that the source timestamp is
    recent enough. ``transmission_eligible`` is the fail-closed market-quality gate
    and additionally requires live/frozen, positive, two-sided, non-crossed prices.
    """

    quote: MarketQuote
    fetched_at: datetime
    observed_at: datetime
    from_cache: bool
    source_age_seconds: float
    cache_age_seconds: float
    fresh: bool
    transmission_eligible: bool

    @property
    def effective_data_quality(self) -> DataQuality:
        """Return STALE when temporal freshness is stricter than source labeling."""

        return self.quote.data_quality if self.fresh else DataQuality.STALE


@dataclass(frozen=True, slots=True)
class _CacheEntry[T]:
    value: T
    fetched_at: datetime


class _SubscriptionLimiter:
    """Atomic weighted semaphore used to cap subscribed contracts."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._available = capacity
        self._condition = asyncio.Condition()

    @property
    def active(self) -> int:
        return self._capacity - self._available

    async def acquire(self, count: int) -> None:
        if count <= 0 or count > self._capacity:
            raise ValueError("Subscription count must be within limiter capacity")
        async with self._condition:
            await self._condition.wait_for(lambda: self._available >= count)
            self._available -= count

    async def release(self, count: int) -> None:
        async with self._condition:
            self._available += count
            if self._available > self._capacity:
                raise RuntimeError("Market-data subscription limiter released too many slots")
            self._condition.notify_all()


class MarketDataManager:
    """Bounded async access to broker contract metadata and quote endpoints."""

    def __init__(
        self,
        broker: Broker,
        *,
        batch_size: int = 20,
        max_concurrent_requests: int = 2,
        max_active_subscriptions: int = 40,
        metadata_cache_ttl: timedelta = timedelta(hours=8),
        quote_cache_ttl: timedelta = timedelta(seconds=2),
        max_quote_age: timedelta = timedelta(seconds=5),
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 2.0,
        request_timeout_seconds: float = 15.0,
        cancellation_timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        sleep: AsyncSleeper = asyncio.sleep,
        is_pacing_error: ErrorClassifier | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
        if max_active_subscriptions <= 0:
            raise ValueError("max_active_subscriptions must be positive")
        if batch_size > max_active_subscriptions:
            raise ValueError("batch_size cannot exceed max_active_subscriptions")
        if metadata_cache_ttl <= timedelta(0) or quote_cache_ttl <= timedelta(0):
            raise ValueError("cache TTLs must be positive")
        if max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if initial_backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("backoff delays cannot be negative")
        if request_timeout_seconds <= 0 or cancellation_timeout_seconds <= 0:
            raise ValueError("broker timeouts must be positive")

        self._broker = broker
        self._batch_size = batch_size
        self._metadata_cache_ttl = metadata_cache_ttl
        self._quote_cache_ttl = quote_cache_ttl
        self._max_quote_age = max_quote_age
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._cancellation_timeout_seconds = cancellation_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._is_pacing_error = is_pacing_error or (
            lambda error: isinstance(error, MarketDataPacingError)
        )
        self._logger = logger or logging.getLogger("chronos.broker.market_data")
        self._request_limiter = asyncio.Semaphore(max_concurrent_requests)
        self._subscription_limiter = _SubscriptionLimiter(max_active_subscriptions)
        self._quote_operation_lock = asyncio.Lock()
        self._cancellation_failure: MarketDataCancellationError | None = None
        self._chain_cache: dict[int, _CacheEntry[OptionChainResponse]] = {}
        self._contract_cache: dict[tuple[object, ...], _CacheEntry[OptionContract]] = {}
        self._quote_cache: dict[int, _CacheEntry[MarketQuote]] = {}

    @property
    def active_subscription_count(self) -> int:
        """Return the number of contract slots currently reserved by the manager."""

        return self._subscription_limiter.active

    def clear_cache(self) -> None:
        """Drop local metadata and quote cache entries without touching the broker."""

        self._chain_cache.clear()
        self._contract_cache.clear()
        self._quote_cache.clear()

    @property
    def cache_entry_count(self) -> int:
        """Return the number of live cache entries for local diagnostics."""

        return len(self._chain_cache) + len(self._contract_cache) + len(self._quote_cache)

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        """Qualify one stock through the manager's bounded read discipline."""

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("underlying symbol must not be blank")
        self._raise_if_quarantined()
        try:
            async with self._request_limiter:
                contract = await self._with_pacing_retry(
                    lambda: self._broker.qualify_underlying(normalized_symbol),
                    operation=f"underlying qualification for {normalized_symbol}",
                )
        except TimeoutError as error:
            raise MarketDataUnavailableError(
                f"Broker underlying qualification timed out for {normalized_symbol}"
            ) from error
        if contract.symbol != normalized_symbol:
            raise MarketDataUnavailableError(
                f"Broker qualified a mismatched underlying for {normalized_symbol}",
                contract_ids=(contract.con_id,),
                observed_values=(contract,),
                observed_at=self._now(),
            )
        return contract

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
        *,
        force_refresh: bool = False,
    ) -> OptionChainSnapshot:
        """Fetch or reuse option-chain metadata with explicit cache age."""

        observed_at = self._now()
        self._raise_if_quarantined()
        self._prune_expired_caches(observed_at)
        cached = self._chain_cache.get(underlying.con_id)
        if (
            not force_refresh
            and cached is not None
            and self._cache_is_fresh(
                cached,
                self._metadata_cache_ttl,
                observed_at,
            )
        ):
            return OptionChainSnapshot(
                parameters=cached.value.parameters,
                fetched_at=cached.fetched_at,
                observed_at=observed_at,
                from_cache=True,
                fresh=True,
                complete=cached.value.complete,
                truncated=cached.value.truncated,
                completion_marker=cached.value.completion_marker,
                completion_observed_at=cached.value.observed_at,
                completion_source=cached.value.source,
            )

        async with self._request_limiter:
            response = await self._with_pacing_retry(
                lambda: self._broker.option_chain_parameters(underlying),
                operation=f"option chain for {underlying.symbol}",
            )
        if not isinstance(response, OptionChainResponse):
            raise MarketDataUnavailableError(
                f"Broker returned option-chain metadata without completion evidence for "
                f"{underlying.symbol}",
                contract_ids=(underlying.con_id,),
            )
        bounded_response = self._bounded_chain_response(response)
        if not response.complete or response.truncated:
            raise MarketDataUnavailableError(
                f"Broker returned a partial or truncated option chain for {underlying.symbol}",
                contract_ids=(underlying.con_id,),
                observed_at=self._now(),
                truncated=bounded_response.truncated,
                chain_response=bounded_response,
            )
        if len(response.parameters) > MAX_OPTION_CHAIN_ROWS or any(
            len(value.expirations) > MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW
            or len(value.strikes) > MAX_OPTION_CHAIN_STRIKES_PER_ROW
            for value in response.parameters
        ):
            raise MarketDataUnavailableError(
                f"Broker returned option-chain metadata beyond the hard evidence bound for "
                f"{underlying.symbol}",
                contract_ids=(underlying.con_id,),
                observed_at=self._now(),
                truncated=True,
                chain_response=bounded_response,
            )
        if not response.parameters:
            raise MarketDataUnavailableError(
                f"Broker returned no option-chain metadata for {underlying.symbol}",
                contract_ids=(underlying.con_id,),
                observed_at=self._now(),
                chain_response=bounded_response,
            )
        if any(value.underlying_con_id != underlying.con_id for value in response.parameters):
            raise MarketDataUnavailableError(
                f"Broker returned mismatched option-chain metadata for {underlying.symbol}",
                contract_ids=(underlying.con_id,),
                observed_at=self._now(),
                truncated=bounded_response.truncated,
                chain_response=bounded_response,
            )
        fetched_at = self._now()
        self._chain_cache[underlying.con_id] = _CacheEntry(response, fetched_at)
        return OptionChainSnapshot(
            parameters=response.parameters,
            fetched_at=fetched_at,
            observed_at=fetched_at,
            from_cache=False,
            fresh=True,
            complete=response.complete,
            truncated=response.truncated,
            completion_marker=response.completion_marker,
            completion_observed_at=response.observed_at,
            completion_source=response.source,
        )

    @staticmethod
    def narrow_option_specs(
        parameters: OptionChainParameters,
        *,
        symbol: str,
        spot: Decimal,
        right: OptionRight,
        as_of: date,
        min_dte: int,
        target_dte: int,
        max_dte: int,
        max_expirations: int,
        strikes_per_expiration: int,
        currency: str = "USD",
    ) -> tuple[OptionContractSpec, ...]:
        """Deterministically narrow metadata before qualification or quote requests."""

        if spot <= 0:
            raise ValueError("spot must be positive")
        if not 0 <= min_dte <= target_dte <= max_dte:
            raise ValueError("DTE bounds must satisfy 0 <= min <= target <= max")
        if max_expirations <= 0 or strikes_per_expiration <= 0:
            raise ValueError("narrowing limits must be positive")
        if (
            max_expirations > MAX_CANDIDATE_EXPIRATIONS
            or strikes_per_expiration > MAX_CANDIDATE_STRIKES_PER_EXPIRATION
            or max_expirations * strikes_per_expiration > MAX_CANDIDATE_REQUEST_CONTRACTS
        ):
            raise ValueError("narrowing limits exceed the hard option request bound")

        eligible_expirations = tuple(
            expiration
            for expiration in parameters.expirations
            if min_dte <= (expiration - as_of).days <= max_dte
        )
        selected_expirations = tuple(
            sorted(
                sorted(
                    eligible_expirations,
                    key=lambda expiration: (
                        abs((expiration - as_of).days - target_dte),
                        expiration,
                    ),
                )[:max_expirations]
            )
        )
        selected_strikes = tuple(
            sorted(
                sorted(parameters.strikes, key=lambda strike: (abs(strike - spot), strike))[
                    :strikes_per_expiration
                ]
            )
        )
        return tuple(
            OptionContractSpec(
                symbol=symbol.upper(),
                underlying_con_id=parameters.underlying_con_id,
                expiration=expiration,
                strike=strike,
                right=right,
                exchange=parameters.exchange,
                currency=currency,
                multiplier=parameters.multiplier,
                trading_class=parameters.trading_class,
            )
            for expiration in selected_expirations
            for strike in selected_strikes
        )

    async def qualify_option_contracts(
        self,
        specs: Sequence[OptionContractSpec],
        *,
        force_refresh: bool = False,
    ) -> tuple[OptionContract, ...]:
        """Qualify option specifications, caching broker contract metadata."""

        if len(specs) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("option qualification exceeds the hard request bound")
        observed_at = self._now()
        self._raise_if_quarantined()
        self._prune_expired_caches(observed_at)
        resolved: dict[tuple[object, ...], OptionContract] = {}
        freshly_resolved: dict[tuple[object, ...], OptionContract] = {}
        missing: list[OptionContractSpec] = []
        for spec in specs:
            key = self._spec_key(spec)
            cached = self._contract_cache.get(key)
            if (
                not force_refresh
                and cached is not None
                and self._cache_is_fresh(
                    cached,
                    self._metadata_cache_ttl,
                    observed_at,
                )
            ):
                resolved[key] = cached.value
            else:
                missing.append(spec)

        for batch in self._batches(tuple(missing)):
            try:
                async with self._request_limiter:
                    qualified = await self._with_pacing_retry(
                        partial(self._broker.qualify_option_contracts, batch),
                        operation="option contract qualification",
                    )
            except BaseException as error:
                observed = tuple(
                    sorted(
                        (
                            *freshly_resolved.values(),
                            *(
                                item
                                for item in getattr(error, "observed_values", ())
                                if isinstance(item, OptionContract)
                            ),
                        ),
                        key=lambda contract: (
                            self._spec_key(contract),
                            contract.con_id,
                            contract.local_symbol,
                            contract.model_dump_json(),
                        ),
                    )
                )
                vars(error).update(
                    observed_values=observed[:MAX_CANDIDATE_REQUEST_CONTRACTS],
                    observed_at=(
                        getattr(error, "observed_at", None)
                        if isinstance(getattr(error, "observed_at", None), datetime)
                        else self._now()
                    ),
                    truncated=(
                        bool(getattr(error, "truncated", False))
                        or len(observed) > MAX_CANDIDATE_REQUEST_CONTRACTS
                    ),
                )
                raise
            returned = {self._spec_key(contract): contract for contract in qualified}
            expected = {self._spec_key(spec) for spec in batch}
            missing_keys = expected - returned.keys()
            unexpected_keys = returned.keys() - expected
            if missing_keys or unexpected_keys or len(returned) != len(qualified):
                observed = tuple(
                    sorted(
                        (*freshly_resolved.values(), *qualified),
                        key=lambda contract: (
                            self._spec_key(contract),
                            contract.con_id,
                            contract.local_symbol,
                            contract.model_dump_json(),
                        ),
                    )
                )
                raise MarketDataUnavailableError(
                    "Broker returned incomplete or mismatched qualified option contracts",
                    observed_values=observed[:MAX_CANDIDATE_REQUEST_CONTRACTS],
                    observed_at=self._now(),
                    truncated=len(observed) > MAX_CANDIDATE_REQUEST_CONTRACTS,
                )
            fetched_at = self._now()
            for key, contract in returned.items():
                resolved[key] = contract
                freshly_resolved[key] = contract
                self._contract_cache[key] = _CacheEntry(contract, fetched_at)

        return tuple(resolved[self._spec_key(spec)] for spec in specs)

    async def option_market_rules(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionMarketRule, ...]:
        """Fetch a complete, exactly aligned set of read-only price rules."""

        if len(contracts) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("option market-rule request exceeds the hard request bound")
        if not contracts:
            return ()
        self._require_unique_contract_ids(contracts, operation="option market-rule request")
        self._raise_if_quarantined()
        async with self._request_limiter:
            rules = await self._with_pacing_retry(
                lambda: self._broker.option_market_rules(contracts),
                operation="option market-rule evidence",
            )
        requested = {contract.con_id: contract for contract in contracts}
        returned = {rule.con_id: rule for rule in rules}
        if any(len(rule.price_increments) > MAX_OPTION_MARKET_RULE_INCREMENTS for rule in rules):
            bounded_rules = tuple(
                rule.model_copy(
                    update={
                        "price_increments": rule.price_increments[
                            :MAX_OPTION_MARKET_RULE_INCREMENTS
                        ]
                    }
                )
                for rule in sorted(rules, key=lambda item: item.con_id)[
                    :MAX_CANDIDATE_REQUEST_CONTRACTS
                ]
            )
            raise MarketDataUnavailableError(
                "Broker returned an option market rule beyond the hard evidence bound",
                observed_values=bounded_rules,
                observed_at=self._now(),
                truncated=True,
            )
        if set(returned) != set(requested) or len(returned) != len(rules):
            raise MarketDataUnavailableError(
                "Broker returned incomplete or mismatched option market rules",
                contract_ids=tuple(sorted(set(requested) - set(returned))),
                observed_values=tuple(sorted(rules, key=lambda rule: rule.con_id))[
                    :MAX_CANDIDATE_REQUEST_CONTRACTS
                ],
                observed_at=self._now(),
                truncated=len(rules) > MAX_CANDIDATE_REQUEST_CONTRACTS,
            )
        for con_id, rule in returned.items():
            if rule.exchange != requested[con_id].exchange:
                raise MarketDataUnavailableError(
                    "Broker returned a market rule for the wrong option route",
                    contract_ids=(con_id,),
                    observed_values=tuple(sorted(rules, key=lambda item: item.con_id))[
                        :MAX_CANDIDATE_REQUEST_CONTRACTS
                    ],
                    observed_at=self._now(),
                    truncated=len(rules) > MAX_CANDIDATE_REQUEST_CONTRACTS,
                )
        return tuple(returned[contract.con_id] for contract in contracts)

    async def option_deliverable_facts(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionDeliverableFacts, ...]:
        """Fetch an exactly aligned deliverable fact for every candidate."""

        if len(contracts) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("option deliverable request exceeds the hard request bound")
        if not contracts:
            return ()
        self._require_unique_contract_ids(contracts, operation="option deliverable request")
        self._raise_if_quarantined()
        async with self._request_limiter:
            facts = await self._with_pacing_retry(
                lambda: self._broker.option_deliverable_facts(contracts),
                operation="option deliverable evidence",
            )
        requested_ids = {contract.con_id for contract in contracts}
        returned = {fact.con_id: fact for fact in facts}
        if set(returned) != requested_ids or len(returned) != len(facts):
            raise MarketDataUnavailableError(
                "Broker returned incomplete or mismatched option deliverable facts",
                contract_ids=tuple(sorted(requested_ids - set(returned))),
                observed_values=tuple(sorted(facts, key=lambda fact: fact.con_id))[
                    :MAX_CANDIDATE_REQUEST_CONTRACTS
                ],
                observed_at=self._now(),
                truncated=len(facts) > MAX_CANDIDATE_REQUEST_CONTRACTS,
            )
        return tuple(returned[contract.con_id] for contract in contracts)

    async def underlying_quote(
        self,
        contract: UnderlyingContract,
        *,
        force_refresh: bool = False,
    ) -> ManagedQuote:
        """Fetch an underlying quote and always cancel its temporary subscription."""

        async with self._quote_operation_lock:
            return await self._underlying_quote_locked(contract, force_refresh=force_refresh)

    async def _underlying_quote_locked(
        self,
        contract: UnderlyingContract,
        *,
        force_refresh: bool,
    ) -> ManagedQuote:
        observed_at = self._now()
        self._raise_if_quarantined()
        self._prune_expired_caches(observed_at)
        cached = self._usable_cached_quote(contract.con_id, observed_at, force_refresh)
        if cached is not None:
            return self._managed_quote(cached, observed_at, from_cache=True)

        quote = await self._request_with_subscription(
            (contract.con_id,),
            lambda: self._broker.request_underlying_quote(contract),
            operation=f"underlying quote for {contract.symbol}",
        )
        if quote.contract.con_id != contract.con_id:
            raise MarketDataUnavailableError(
                f"Broker returned a mismatched quote for {contract.symbol}",
                contract_ids=(contract.con_id,),
                observed_values=(quote,),
                observed_at=self._now(),
            )
        fetched_at = self._now()
        entry = _CacheEntry(quote, fetched_at)
        self._quote_cache[contract.con_id] = entry
        return self._managed_quote(entry, fetched_at, from_cache=False)

    async def crypto_quote(
        self,
        contract: CryptoContract,
        *,
        force_refresh: bool = False,
    ) -> ManagedQuote:
        """Fetch a spot-crypto quote and always cancel its temporary subscription."""

        async with self._quote_operation_lock:
            return await self._crypto_quote_locked(contract, force_refresh=force_refresh)

    async def _crypto_quote_locked(
        self,
        contract: CryptoContract,
        *,
        force_refresh: bool,
    ) -> ManagedQuote:
        observed_at = self._now()
        self._raise_if_quarantined()
        self._prune_expired_caches(observed_at)
        cached = self._usable_cached_quote(contract.con_id, observed_at, force_refresh)
        if cached is not None:
            return self._managed_quote(cached, observed_at, from_cache=True)

        quote = await self._request_with_subscription(
            (contract.con_id,),
            lambda: self._broker.request_crypto_quote(contract),
            operation=f"crypto quote for {contract.symbol}",
        )
        if quote.contract.con_id != contract.con_id:
            raise MarketDataUnavailableError(
                f"Broker returned a mismatched quote for {contract.symbol}",
                contract_ids=(contract.con_id,),
                observed_values=(quote,),
                observed_at=self._now(),
            )
        fetched_at = self._now()
        entry = _CacheEntry(quote, fetched_at)
        self._quote_cache[contract.con_id] = entry
        return self._managed_quote(entry, fetched_at, from_cache=False)

    async def option_quotes(
        self,
        contracts: Sequence[OptionContract],
        *,
        force_refresh: bool = False,
    ) -> tuple[ManagedQuote, ...]:
        """Fetch option quotes in bounded batches, preserving caller order."""

        if len(contracts) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("option quote request exceeds the hard request bound")
        async with self._quote_operation_lock:
            return await self._option_quotes_locked(contracts, force_refresh=force_refresh)

    async def _option_quotes_locked(
        self,
        contracts: Sequence[OptionContract],
        *,
        force_refresh: bool,
    ) -> tuple[ManagedQuote, ...]:
        observed_at = self._now()
        self._raise_if_quarantined()
        self._prune_expired_caches(observed_at)
        entries: dict[int, tuple[_CacheEntry[MarketQuote], bool]] = {}
        missing_by_id: dict[int, OptionContract] = {}
        for contract in contracts:
            cached = self._usable_cached_quote(contract.con_id, observed_at, force_refresh)
            if cached is not None:
                entries[contract.con_id] = (cached, True)
            else:
                missing_by_id[contract.con_id] = contract

        batches = self._batches(tuple(missing_by_id.values()))
        tasks = [asyncio.create_task(self._request_option_batch(batch)) for batch in batches]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            completed_quotes: list[MarketQuote] = []
            failures: list[BaseException] = []
            for result in results:
                if isinstance(result, BaseException):
                    failures.append(result)
                else:
                    completed_quotes.extend(result)
            if failures:
                observed_quotes = [entry.value for entry, _ in entries.values()]
                observed_quotes.extend(completed_quotes)
                for failure in failures:
                    observed_quotes.extend(
                        item
                        for item in getattr(failure, "observed_values", ())
                        if isinstance(item, MarketQuote)
                    )
                observed_quotes.sort(
                    key=lambda quote: (
                        quote.contract.con_id,
                        quote.timestamp,
                        quote.bid if quote.bid is not None else Decimal("-1"),
                        quote.ask if quote.ask is not None else Decimal("-1"),
                    )
                )
                first_failure = failures[0]
                bounded = tuple(observed_quotes[:MAX_CANDIDATE_REQUEST_CONTRACTS])
                vars(first_failure).update(
                    observed_values=bounded,
                    observed_at=self._now(),
                    truncated=(
                        bool(getattr(first_failure, "truncated", False))
                        or len(observed_quotes) > MAX_CANDIDATE_REQUEST_CONTRACTS
                    ),
                )
                raise first_failure
            fetched_at = self._now()
            for quote in completed_quotes:
                entry = _CacheEntry(quote, fetched_at)
                self._quote_cache[quote.contract.con_id] = entry
                entries[quote.contract.con_id] = (entry, False)

        absent = tuple(contract.con_id for contract in contracts if contract.con_id not in entries)
        if absent:
            raise MarketDataUnavailableError(
                "Broker returned no quote for one or more requested contracts",
                contract_ids=absent,
            )
        final_observed_at = self._now()
        return tuple(
            self._managed_quote(
                entries[contract.con_id][0],
                final_observed_at,
                from_cache=entries[contract.con_id][1],
            )
            for contract in contracts
        )

    async def _request_option_batch(
        self,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[MarketQuote, ...]:
        contract_ids = tuple(contract.con_id for contract in contracts)
        quotes = await self._request_with_subscription(
            contract_ids,
            lambda: self._broker.request_option_quotes(contracts),
            operation=f"option quote batch {contract_ids}",
        )
        returned = {quote.contract.con_id: quote for quote in quotes}
        if set(returned) != set(contract_ids) or len(returned) != len(quotes):
            missing = tuple(
                contract_id for contract_id in contract_ids if contract_id not in returned
            )
            raise MarketDataUnavailableError(
                "Broker returned incomplete or mismatched option quotes",
                contract_ids=missing or contract_ids,
                observed_values=tuple(
                    sorted(
                        quotes,
                        key=lambda quote: (
                            quote.contract.con_id,
                            quote.timestamp,
                            quote.bid if quote.bid is not None else Decimal("-1"),
                            quote.ask if quote.ask is not None else Decimal("-1"),
                        ),
                    )
                )[:MAX_CANDIDATE_REQUEST_CONTRACTS],
                observed_at=self._now(),
                truncated=len(quotes) > MAX_CANDIDATE_REQUEST_CONTRACTS,
            )
        return tuple(returned[contract_id] for contract_id in contract_ids)

    async def _request_with_subscription(
        self,
        contract_ids: tuple[int, ...],
        operation_call: Callable[[], Awaitable[T]],
        *,
        operation: str,
    ) -> T:
        self._raise_if_quarantined()
        await self._subscription_limiter.acquire(len(contract_ids))
        result: T | None = None
        primary_error: BaseException | None = None
        cancellation_error: BaseException | None = None
        async with self._request_limiter:
            try:
                result = await self._with_pacing_retry(operation_call, operation=operation)
            except BaseException as error:
                primary_error = error
            finally:
                try:
                    await asyncio.wait_for(
                        self._broker.cancel_market_data(contract_ids),
                        timeout=self._cancellation_timeout_seconds,
                    )
                except BaseException as error:
                    cancellation_error = error
        if cancellation_error is None:
            await self._subscription_limiter.release(len(contract_ids))
        else:
            observed_values: tuple[object, ...]
            evidence_error = primary_error or cancellation_error
            if result is not None and isinstance(result, tuple):
                observed_values = result[:MAX_CANDIDATE_REQUEST_CONTRACTS]
            elif result is not None:
                observed_values = (result,)
            else:
                observed_values = tuple(getattr(evidence_error, "observed_values", ()))[
                    :MAX_CANDIDATE_REQUEST_CONTRACTS
                ]
            evidence_observed_at = getattr(evidence_error, "observed_at", None)
            quarantine = MarketDataCancellationError(
                "Market-data cleanup failed; requests are locked until the runtime reconnects",
                contract_ids=contract_ids,
                observed_values=observed_values,
                observed_at=(
                    evidence_observed_at
                    if isinstance(evidence_observed_at, datetime)
                    else self._now()
                ),
                truncated=bool(getattr(evidence_error, "truncated", False)),
            )
            self._cancellation_failure = quarantine
            self._logger.critical(
                "Market-data manager quarantined after cancellation failure",
                exc_info=cancellation_error,
                extra={"contract_ids": contract_ids, "operation": operation},
            )
            raise quarantine from cancellation_error

        if primary_error is not None:
            raise primary_error
        if result is None:
            raise MarketDataUnavailableError(
                f"{operation} returned no result",
                contract_ids=contract_ids,
            )
        return result

    async def _with_pacing_retry(
        self,
        operation_call: Callable[[], Awaitable[T]],
        *,
        operation: str,
    ) -> T:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await asyncio.wait_for(
                    operation_call(),
                    timeout=self._request_timeout_seconds,
                )
            except MarketDataPermissionError:
                raise
            except Exception as error:
                if not self._is_pacing_error(error):
                    raise
                last_error = error
                if attempt == self._max_attempts:
                    break
                delay = min(
                    self._initial_backoff_seconds * (2 ** (attempt - 1)),
                    self._max_backoff_seconds,
                )
                self._logger.warning(
                    "Broker pacing limit; retrying bounded market-data request",
                    extra={"attempt": attempt, "delay_seconds": delay, "operation": operation},
                )
                await self._sleep(delay)

        # A retry is a new snapshot, not an additive stream. Preserve the
        # bounded evidence from the final attempt only: merging observations
        # across attempts could manufacture a complete-looking set whose
        # members were never simultaneously true.
        exhausted = MarketDataPacingExhaustedError(
            operation,
            self._max_attempts,
            contract_ids=getattr(last_error, "contract_ids", ()),
            observed_values=getattr(last_error, "observed_values", ()),
            observed_at=getattr(last_error, "observed_at", None),
            truncated=bool(getattr(last_error, "truncated", False)),
            chain_response=getattr(last_error, "chain_response", None),
        )
        raise exhausted from last_error

    def _usable_cached_quote(
        self,
        contract_id: int,
        observed_at: datetime,
        force_refresh: bool,
    ) -> _CacheEntry[MarketQuote] | None:
        if force_refresh:
            return None
        cached = self._quote_cache.get(contract_id)
        if cached is None:
            return None
        if not self._cache_is_fresh(cached, self._quote_cache_ttl, observed_at):
            return None
        if not self._quote_is_fresh(cached.value, observed_at):
            return None
        return cached

    def _managed_quote(
        self,
        entry: _CacheEntry[MarketQuote],
        observed_at: datetime,
        *,
        from_cache: bool,
    ) -> ManagedQuote:
        source_age = (observed_at - entry.value.timestamp).total_seconds()
        cache_age = (observed_at - entry.fetched_at).total_seconds()
        return ManagedQuote(
            quote=entry.value,
            fetched_at=entry.fetched_at,
            observed_at=observed_at,
            from_cache=from_cache,
            source_age_seconds=source_age,
            cache_age_seconds=cache_age,
            fresh=self._quote_is_fresh(entry.value, observed_at),
            transmission_eligible=self._quote_is_transmission_eligible(
                entry.value,
                observed_at,
            ),
        )

    def _quote_is_fresh(self, quote: MarketQuote, observed_at: datetime) -> bool:
        source_age = observed_at - quote.timestamp
        return (
            timedelta(0) <= source_age <= self._max_quote_age
            and quote.data_quality is not DataQuality.STALE
        )

    def _quote_is_transmission_eligible(
        self,
        quote: MarketQuote,
        observed_at: datetime,
    ) -> bool:
        return (
            self._quote_is_fresh(quote, observed_at)
            and quote.data_quality in {DataQuality.LIVE, DataQuality.FROZEN}
            and quote.bid is not None
            and quote.ask is not None
            and quote.bid > 0
            and quote.ask >= quote.bid
        )

    def _raise_if_quarantined(self) -> None:
        if self._cancellation_failure is not None:
            raise self._cancellation_failure

    @staticmethod
    def _require_unique_contract_ids(
        contracts: Sequence[OptionContract], *, operation: str
    ) -> None:
        contract_ids = tuple(contract.con_id for contract in contracts)
        if len(set(contract_ids)) != len(contract_ids):
            raise MarketDataUnavailableError(
                f"{operation} contains duplicate contract identifiers",
                contract_ids=contract_ids,
            )

    def _prune_expired_caches(self, observed_at: datetime) -> None:
        self._chain_cache = {
            key: entry
            for key, entry in self._chain_cache.items()
            if self._cache_is_fresh(entry, self._metadata_cache_ttl, observed_at)
        }
        self._contract_cache = {
            key: entry
            for key, entry in self._contract_cache.items()
            if self._cache_is_fresh(entry, self._metadata_cache_ttl, observed_at)
        }
        self._quote_cache = {
            key: entry
            for key, entry in self._quote_cache.items()
            if self._cache_is_fresh(entry, self._quote_cache_ttl, observed_at)
        }

    @staticmethod
    def _cache_is_fresh[T](
        entry: _CacheEntry[T],
        ttl: timedelta,
        observed_at: datetime,
    ) -> bool:
        age = observed_at - entry.fetched_at
        return timedelta(0) <= age <= ttl

    def _batches(self, values: tuple[T, ...]) -> tuple[tuple[T, ...], ...]:
        return tuple(
            values[index : index + self._batch_size]
            for index in range(0, len(values), self._batch_size)
        )

    @staticmethod
    def _bounded_chain_response(response: OptionChainResponse) -> OptionChainResponse:
        """Retain a deterministic bounded prefix even when the broker over-returns."""

        ordered_rows = tuple(
            sorted(
                response.parameters,
                key=lambda row: (
                    row.underlying_con_id,
                    row.exchange,
                    row.trading_class,
                    row.multiplier,
                    tuple(sorted(row.expirations)),
                    tuple(sorted(row.strikes)),
                ),
            )
        )
        parameters = tuple(
            row.model_copy(
                update={
                    "expirations": tuple(
                        sorted(row.expirations)[:MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW]
                    ),
                    "strikes": tuple(sorted(row.strikes)[:MAX_OPTION_CHAIN_STRIKES_PER_ROW]),
                }
            )
            for row in ordered_rows[:MAX_OPTION_CHAIN_ROWS]
        )
        was_bounded = len(parameters) != len(response.parameters) or any(
            len(retained.expirations) != len(original.expirations)
            or len(retained.strikes) != len(original.strikes)
            for retained, original in zip(parameters, ordered_rows, strict=False)
        )
        return OptionChainResponse(
            parameters=parameters,
            complete=response.complete,
            truncated=response.truncated or was_bounded,
            completion_marker=response.completion_marker,
            observed_at=response.observed_at,
            source=response.source,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise ValueError("MarketDataManager clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _spec_key(spec: OptionContractSpec) -> tuple[object, ...]:
        return (
            spec.symbol,
            spec.underlying_con_id,
            spec.expiration,
            spec.strike,
            spec.right,
            spec.exchange,
            spec.currency,
            spec.multiplier,
            spec.trading_class,
        )

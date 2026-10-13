"""Typed brokerage port used by Chronos services."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, overload, runtime_checkable

from chronos.config.limits import MAX_BROKER_EVIDENCE_SOURCE_CHARS
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    CryptoContract,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.marketdata.bars import BarInterval, BarSeries


class BrokerError(RuntimeError):
    """Base class for broker adapter failures safe to surface in the UI."""


class BrokerConnectionError(BrokerError):
    """The adapter cannot perform an operation without a healthy connection."""


class BrokerDataError(BrokerError):
    """Broker data is unavailable, incomplete, or internally inconsistent."""


class BrokerSafetyError(BrokerError):
    """An operation crossed a Chronos safety boundary and was blocked."""


class BrokerRefusedBeforeSend(BrokerSafetyError):
    """The adapter refused locally, before any network send (ADR-0009 §6).

    Raising this is a PROOF CLAIM: the venue never saw the order — no bytes
    were written to the gateway socket. The submission boundary relies on it to
    resolve the intent to REJECTED synchronously instead of stranding it in
    SUBMISSION_UNKNOWN. An adapter must never raise it after starting a send.
    """


class BrokerSendGuard(Protocol):
    """Atomic last-line authorization held only across a broker send call.

    A transmitting adapter must enter this guard exactly once and keep it held
    only across its synchronous socket-send primitive, never while awaiting an
    acknowledgement callback.
    """

    def at_send(self) -> AbstractContextManager[bool]: ...


@dataclass(frozen=True, slots=True)
class VenueOrderIdentity:
    """What the VENUE's open-order row says about one order: nothing inferred, nothing local.

    Handed to an :class:`OrderOwnershipLedger` so it can compare every field with the persisted
    record of an order Chronos submitted (S1-F1, Q-P1 Option A).
    """

    broker_order_id: int
    order_ref: str
    account_id: str
    client_id: int
    permanent_id: int


class OrderOwnershipLedger(Protocol):
    """The durable proof that an order on the venue is one Chronos submitted.

    Implemented over the persisted order book by the trusted order boundary and wired into the
    adapter by the runtime. ``True`` means an EXACT match on the order ref, account, broker order
    id, client id and permanent id; anything else (including an error) means Chronos does not
    cancel the order.
    """

    def proves_owned(self, identity: VenueOrderIdentity) -> bool: ...


@dataclass(frozen=True, slots=True)
class OptionChainResponse(Sequence[OptionChainParameters]):
    """One broker response plus explicit proof that its stream completed.

    A non-empty tuple alone cannot distinguish a complete option-chain response
    from the prefix accumulated before a timeout.  Adapters therefore publish
    the vendor completion signal explicitly; the market-data manager validates
    it before caching or exposing any parameters.

    The sequence methods keep direct, read-only adapter callers ergonomic while
    making it impossible for the manager to manufacture success metadata.
    """

    parameters: tuple[OptionChainParameters, ...]
    complete: bool
    truncated: bool
    completion_marker: str
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("option-chain response timestamp must be timezone-aware")
        if (
            len(self.completion_marker) > MAX_BROKER_EVIDENCE_SOURCE_CHARS
            or len(self.source) > MAX_BROKER_EVIDENCE_SOURCE_CHARS
        ):
            raise ValueError("option-chain completion provenance exceeds the hard evidence bound")
        marker = self.completion_marker.strip()
        source = self.source.strip()
        if not marker:
            raise ValueError("option-chain completion marker must not be blank")
        if not source:
            raise ValueError("option-chain response source must not be blank")
        object.__setattr__(self, "parameters", tuple(self.parameters))
        object.__setattr__(self, "completion_marker", marker)
        object.__setattr__(self, "source", source)

    def __len__(self) -> int:
        return len(self.parameters)

    def __iter__(self) -> Iterator[OptionChainParameters]:
        return iter(self.parameters)

    @overload
    def __getitem__(self, index: int) -> OptionChainParameters: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[OptionChainParameters, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> OptionChainParameters | tuple[OptionChainParameters, ...]:
        return self.parameters[index]


@runtime_checkable
class CompletedOrderEvidence(Protocol):
    """Optional capability: the venue's completed (terminal) orders for this account/session.

    Not part of :class:`Broker`: only an adapter that can read ``reqCompletedOrders`` provides
    it. The CANCEL_PENDING settle planner treats its absence as "no terminal evidence" -- an
    order that is merely missing from the open book never settles on its own.
    """

    async def completed_orders(self) -> tuple[BrokerOrder, ...]: ...


@runtime_checkable
class Broker(Protocol):
    """Async broker boundary; callers serialize it through the connection manager."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def connection_status(self) -> ConnectionStatus: ...

    async def server_time(self) -> datetime: ...

    async def account_summary(self) -> AccountSummary: ...

    async def positions(self) -> tuple[BrokerPosition, ...]: ...

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]: ...

    async def open_orders(self) -> tuple[BrokerOrder, ...]: ...

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract: ...

    async def qualify_crypto(self, symbol: str) -> CryptoContract: ...

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> OptionChainResponse: ...

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]: ...

    async def option_market_rules(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionMarketRule, ...]: ...

    async def option_deliverable_facts(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionDeliverableFacts, ...]: ...

    async def request_underlying_quote(
        self,
        contract: UnderlyingContract,
    ) -> MarketQuote: ...

    async def request_crypto_quote(
        self,
        contract: CryptoContract,
    ) -> MarketQuote: ...

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]: ...

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None: ...

    async def historical_bars(
        self,
        contract: UnderlyingContract,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
    ) -> BarSeries:
        """Closed OHLCV bars for a qualified underlying (M8c, ADR-0019).

        Read-only and idempotent, but **not free**: IBKR paces historical
        requests far more tightly than quotes, and this connection is shared with
        the order pipeline and the autonomy tick. Callers go through
        :mod:`chronos.api.bars`, which caches and paces; nothing should call this
        method directly on a schedule.

        The series contains **closed bars only**. A forming bar is a number that
        changes while it is being read, and everything downstream of this — a
        chart, a moving average, an operator's judgement — is better served by a
        series that is complete as far as it goes than by one whose last element
        is provisional and unlabelled.
        """
        ...

    async def preview_order(self, request: OrderRequest) -> OrderPreview: ...

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission: ...

    async def modify_order(self, request: OrderModification) -> OrderSubmission: ...

    async def cancel_order(self, broker_order_id: int) -> CancellationResult: ...

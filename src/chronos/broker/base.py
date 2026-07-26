"""Typed brokerage port used by Chronos services."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol, runtime_checkable

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
    ) -> tuple[OptionChainParameters, ...]: ...

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]: ...

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

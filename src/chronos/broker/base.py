"""Typed brokerage port used by Chronos services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
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


class BrokerError(RuntimeError):
    """Base class for broker adapter failures safe to surface in the UI."""


class BrokerConnectionError(BrokerError):
    """The adapter cannot perform an operation without a healthy connection."""


class BrokerDataError(BrokerError):
    """Broker data is unavailable, incomplete, or internally inconsistent."""


class BrokerSafetyError(BrokerError):
    """An operation crossed a Chronos safety boundary and was blocked."""


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

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]: ...

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None: ...

    async def preview_order(self, request: OrderRequest) -> OrderPreview: ...

    async def submit_order(self, request: OrderRequest) -> OrderSubmission: ...

    async def modify_order(self, request: OrderModification) -> OrderSubmission: ...

    async def cancel_order(self, broker_order_id: int) -> CancellationResult: ...

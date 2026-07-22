"""A recording, venue-free fake broker plus builders for order-pipeline tests.

The fake implements the :class:`~chronos.broker.base.Broker` protocol surface
the order pipeline uses. It records every submit/modify/cancel call and returns
canned results; it reaches NO venue, so "no order is placed by any test path"
holds structurally. Tests assert ``submit_calls == 0`` on every non-submission
path, and inspect the recorded ``OrderRequest`` (including ``transmit``) on the
single happy-path submission test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from chronos.broker.base import BrokerError, BrokerRefusedBeforeSend, BrokerSendGuard
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DisplayEnvironment,
    OptionRight,
    OrderLifecycle,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    OptionContract,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)

FIXED_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
PAPER_ACCOUNT = "DU1234567"


class FakeBroker:
    """Records order calls; never contacts a venue."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        positions: Sequence[BrokerPosition] = (),
        open_orders: Sequence[BrokerOrder] = (),
        executions: Sequence[BrokerExecution] = (),
        preview_accepted: bool = True,
        next_broker_order_id: int = 9001,
    ) -> None:
        self.account_id = account_id
        self._positions = tuple(positions)
        self._open_orders = tuple(open_orders)
        self._executions = tuple(executions)
        self._preview_accepted = preview_accepted
        self._next_id = next_broker_order_id
        self.submit_calls: list[OrderRequest] = []
        self.modify_calls: list[OrderModification] = []
        self.cancel_calls: list[int] = []
        self.preview_calls: list[OrderRequest] = []
        # Fault injection: when set, submit_order raises this before recording,
        # so submit_calls stays empty (the boundary caught it). Used to drive the
        # transmit-path exception handling under fault.
        self.submit_error: BrokerError | None = None

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.PAPER,
            connected=True,
            account_id=self.account_id,
        )

    async def server_time(self) -> datetime:
        return FIXED_NOW

    async def account_summary(self) -> AccountSummary:
        return AccountSummary(
            account_id=self.account_id,
            net_liquidation=Decimal("100000"),
            total_cash=Decimal("80000"),
            buying_power=Decimal("160000"),
            as_of=FIXED_NOW,
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        return self._positions

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        return self._executions

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        return self._open_orders

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        self.preview_calls.append(request)
        return OrderPreview(
            request=request,
            accepted=self._preview_accepted and not request.transmit,
            estimated_commission=Decimal("0.65"),
            warnings=(),
            broker_message="fake what-if",
            previewed_at=FIXED_NOW,
        )

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        if self.submit_error is not None:
            raise self.submit_error
        if send_guard is None:
            raise BrokerRefusedBeforeSend(
                "fake submission is missing atomic reconciliation authorization"
            )
        with send_guard.at_send() as authorized:
            if not authorized:
                raise BrokerRefusedBeforeSend(
                    "reconciliation authorization changed before fake send"
                )
            self.submit_calls.append(request)
        broker_order_id = self._next_id
        self._next_id += 1
        return OrderSubmission(
            correlation_id=request.correlation_id,
            broker_order_id=broker_order_id,
            permanent_id=broker_order_id + 100000,
            client_id=17,
            lifecycle=OrderLifecycle.SUBMITTED,
            submitted_at=FIXED_NOW,
        )

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        self.modify_calls.append(request)
        return OrderSubmission(
            correlation_id=request.correlation_id,
            broker_order_id=request.broker_order_id,
            client_id=17,
            lifecycle=OrderLifecycle.SUBMITTED,
            submitted_at=FIXED_NOW,
        )

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self.cancel_calls.append(broker_order_id)
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=OrderLifecycle.CANCELLED,
            message="fake cancel accepted",
            timestamp=FIXED_NOW,
        )


def paper_settings(**overrides: object) -> Settings:
    """Settings whose ``transmission_possible`` is True (IBKR + PAPER + allow).

    pydantic-settings only honours lowercase field-name kwargs from the init
    source (uppercase env-style names are ignored as ``extra``), so the fields
    below use their exact names.
    """

    base: dict[str, object] = {
        "_env_file": None,
        "broker_mode": "ibkr",
        "ib_environment": "paper",
        "allow_order_transmit": True,
        "ib_account_id": PAPER_ACCOUNT,
        "ib_account_allowlist": (PAPER_ACCOUNT,),
        "symbol_allowlist": ("AAPL", "MSFT", "SPY"),
        "max_contracts_per_order": 2,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def option_contract(
    *,
    symbol: str = "AAPL",
    strike: Decimal = Decimal("150"),
    right: OptionRight = OptionRight.PUT,
    con_id: int = 111,
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        underlying_con_id=999,
        expiration=date(2026, 8, 21),
        strike=strike,
        right=right,
        multiplier=Decimal("100"),
        trading_class=symbol,
        con_id=con_id,
        local_symbol=f"{symbol} 260821{right.value}00150000",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )


def stock_contract(*, symbol: str = "AAPL", con_id: int = 999) -> UnderlyingContract:
    return UnderlyingContract(con_id=con_id, symbol=symbol)

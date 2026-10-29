"""D-54 harness: the production gatherers over a synthetic broker, a readiness latch and a database.

``BackendGatherers`` is what the backend's autonomy runtime calls each tick (``cycle_facts``) and
per decision (``instrument_facts``). These helpers build exactly what it reads from an
``AppRuntime`` (broker, connection runner, settings, readiness latch, database) with doubles that
never reach a network: every number is synthetic, and nothing has a submit path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from chronos.domain.enums import BrokerAdapter, BrokerMode, DataQuality
from chronos.domain.models import BrokerPosition, MarketQuote, UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.persistence.database import Database
from chronos.persistence.schema import ManagedPositionBindingRow, OrderIntentRow, RiskDecisionRow

NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
ACCOUNT = "DU1234567"
CONTRACTS = {
    "SPY": UnderlyingContract(con_id=111, symbol="SPY"),
    "QQQ": UnderlyingContract(con_id=222, symbol="QQQ"),
}


def held(symbol: str, quantity: str | Decimal) -> BrokerPosition:
    """One broker position row for a known contract (synthetic average cost)."""

    return BrokerPosition(
        account_id=ACCOUNT,
        contract=CONTRACTS[symbol],
        quantity=Decimal(quantity),
        average_cost=Decimal(390),
    )


@dataclass
class Broker:
    """A read-only broker double. ``reads`` is what ``positions()`` returns, call by call.

    ``on_read(n)`` runs before the n-th ``positions()`` call returns (1-based); it is how a test
    moves the readiness latch or closes the connection between the two reads.
    """

    reads: list[tuple[BrokerPosition, ...]] = field(default_factory=list)
    connected: bool = True
    account_in_scope: bool = True
    on_read: Callable[[int], None] | None = None
    position_calls: int = 0

    async def account_summary(self) -> SimpleNamespace:
        return SimpleNamespace(
            account_id=ACCOUNT,
            net_liquidation=Decimal(100_000),
            total_cash=Decimal(60_000),
            buying_power=Decimal(60_000),
        )

    async def connection_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            connected=self.connected,
            account_id=ACCOUNT if self.connected and self.account_in_scope else None,
        )

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        return CONTRACTS[symbol]

    async def request_underlying_quote(self, instrument: UnderlyingContract) -> MarketQuote:
        return MarketQuote(
            contract=instrument,
            timestamp=NOW,
            data_quality=DataQuality.LIVE,
            bid=Decimal("399.98"),
            ask=Decimal("400.02"),
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self.position_calls += 1
        if self.on_read is not None:
            self.on_read(self.position_calls)
        index = min(self.position_calls, len(self.reads)) - 1
        return self.reads[index] if self.reads else ()


def runtime(
    broker: Broker,
    database: Database,
    *,
    readiness: Any = None,
    broker_mode: BrokerMode = BrokerMode.IBKR,
    broker_adapter: BrokerAdapter = BrokerAdapter.OFFICIAL_IBKR,
) -> SimpleNamespace:
    """The slice of ``AppRuntime`` the gatherers read."""

    return SimpleNamespace(
        broker=broker,
        connection=SimpleNamespace(run=asyncio.run),
        settings=SimpleNamespace(
            autonomy_market_timezone="America/New_York",
            broker_mode=broker_mode,
            broker_adapter=broker_adapter,
        ),
        reconciliation_readiness=readiness or ReconciliationReadiness(session_id="sess-d54"),
        database=database,
    )


def bind_managed(
    session: Session,
    *,
    con_id: int,
    symbol: str,
    tag: str,
    status: str = "FILLED",
    fingerprint: str = FINGERPRINT,
    open_close_effect: str = "OPEN",
) -> None:
    """A managed-position binding whose opening intent holds ``con_id`` (AP-1's MANAGED path).

    ``tag`` (one uppercase hex digit) keeps the unique keys apart between bindings.
    """

    order_ref = "CHR-ORD-" + tag * 32
    decision_id = "rd-" + tag * 8
    session.add(
        RiskDecisionRow(
            decision_id=decision_id,
            account_fingerprint=fingerprint,
            symbol=symbol,
            product_family="STOCK",
            overall_result="PASS",
            decided_at=NOW - timedelta(days=1),
            expires_at=NOW - timedelta(days=1) + timedelta(minutes=5),
            evidence={},
        )
    )
    session.add(
        OrderIntentRow(
            intent_id="intent-" + tag * 8,
            idempotency_key="idem-" + tag * 8,
            account_fingerprint=fingerprint,
            environment="paper",
            product_family="STOCK",
            symbol=symbol,
            con_id=con_id,
            action="BUY",
            open_close_effect=open_close_effect,
            quantity=Decimal(10),
            order_type="LMT",
            limit_price=Decimal(400),
            time_in_force="DAY",
            order_ref=order_ref,
            status=status,
            created_at=NOW - timedelta(days=1),
        )
    )
    session.flush()
    session.add(
        ManagedPositionBindingRow(
            account_fingerprint=fingerprint,
            opening_order_ref=order_ref,
            position_id="CHR-POS-" + tag * 32,
            risk_decision_id=decision_id,
            broker_order_id=int(tag, 16) + 1,
            permanent_id=int(tag, 16) + 1,
            opening_fill_evidence_digest="e" * 64,
            entry_risk_evidence_digest="e" * 64,
            candidate_spec_sha256="e" * 64,
            management_policy_sha256="e" * 64,
            reconciliation_session_id="sess-earlier",
            reconciliation_generation=1,
            admitted_at=NOW - timedelta(days=1),
        )
    )
    session.commit()

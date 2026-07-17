"""Read-only queries over the persisted execution ledger, for the monitor.

The ledger is opened in SQLite read-only URI mode (``mode=ro``) so the monitor
can never create, migrate, or mutate it — a monitor that could write to the
ledger would not be a monitor. Everything surfaced here is a direct read of
persisted rows: working (open) orders, recent fills, and fill-derived net
positions.

Deliberately, **no profit-and-loss is reconstructed here.** In this build the
service runs SHADOW with a flat account model and submits nothing, so the
platform ledger holds no fills and any realized/unrealized P&L number would be
fiction. When a future paper build populates the ledger, these direct reads
populate with it; P&L reconstruction remains its own, separately-reviewed job.

This module imports no broker adapter.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from chronos.domain.enums import OrderSide
from chronos.execution.intents import IntentStatus

_WORKING_STATUS_VALUES = frozenset(
    {
        IntentStatus.SUBMITTED.value,
        IntentStatus.PRE_SUBMITTED.value,
        IntentStatus.ACKNOWLEDGED.value,
        IntentStatus.PARTIALLY_FILLED.value,
        IntentStatus.PENDING_CANCEL.value,
    }
)


@dataclass(frozen=True, slots=True)
class OpenOrderView:
    intent_id: str
    symbol: str
    side: str
    quantity: int
    status: str
    filled_quantity: int


@dataclass(frozen=True, slots=True)
class FillView:
    intent_id: str
    symbol: str
    cumulative_quantity: int
    average_price: str | None
    at_utc: str


@dataclass(frozen=True, slots=True)
class PositionView:
    symbol: str
    net_shares: int


@dataclass(frozen=True, slots=True)
class LedgerView:
    open_orders: tuple[OpenOrderView, ...]
    recent_fills: tuple[FillView, ...]
    net_positions: tuple[PositionView, ...]


_CURRENT_STATUS_SQL = """
SELECT
    i.intent_id AS intent_id,
    i.symbol AS symbol,
    i.side AS side,
    i.quantity AS quantity,
    COALESCE(lt.status, i.initial_status) AS status,
    COALESCE(f.filled, 0) AS filled
FROM intents i
LEFT JOIN (
    SELECT t.intent_id AS intent_id, t.status AS status
    FROM transitions t
    INNER JOIN (
        SELECT intent_id, MAX(id) AS max_id FROM transitions GROUP BY intent_id
    ) latest ON latest.max_id = t.id
) lt ON lt.intent_id = i.intent_id
LEFT JOIN (
    SELECT intent_id, MAX(cumulative_quantity) AS filled FROM fills GROUP BY intent_id
) f ON f.intent_id = i.intent_id
"""

_RECENT_FILLS_SQL = """
SELECT f.intent_id, i.symbol, f.cumulative_quantity, f.average_price, f.at_utc
FROM fills f
LEFT JOIN intents i ON i.intent_id = f.intent_id
ORDER BY f.id DESC
LIMIT ?
"""


def read_ledger_view(ledger_path: Path, *, fills_tail: int = 10) -> LedgerView | None:
    """Open the ledger read-only and return a snapshot view, or ``None``.

    Returns ``None`` when the file is absent or cannot be opened as a ledger; a
    bad or missing ledger is a monitor warning upstream, never a crash.
    """

    if not ledger_path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(_CURRENT_STATUS_SQL).fetchall()
        fill_rows = connection.execute(_RECENT_FILLS_SQL, (fills_tail,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    open_orders: list[OpenOrderView] = []
    net_by_symbol: dict[str, int] = {}
    for intent_id, symbol, side, quantity, status, filled in rows:
        filled_int = int(filled)
        if status in _WORKING_STATUS_VALUES:
            open_orders.append(
                OpenOrderView(
                    intent_id=str(intent_id),
                    symbol=str(symbol),
                    side=str(side),
                    quantity=int(quantity),
                    status=str(status),
                    filled_quantity=filled_int,
                )
            )
        if filled_int:
            signed = filled_int if side == OrderSide.BUY.value else -filled_int
            net_by_symbol[str(symbol)] = net_by_symbol.get(str(symbol), 0) + signed

    recent_fills = tuple(
        FillView(
            intent_id=str(intent_id),
            symbol=str(symbol) if symbol is not None else "?",
            cumulative_quantity=int(cumulative_quantity),
            average_price=str(average_price) if average_price is not None else None,
            at_utc=str(at_utc),
        )
        for intent_id, symbol, cumulative_quantity, average_price, at_utc in fill_rows
    )
    net_positions = tuple(
        PositionView(symbol=symbol, net_shares=shares)
        for symbol, shares in sorted(net_by_symbol.items())
        if shares != 0
    )
    return LedgerView(
        open_orders=tuple(open_orders),
        recent_fills=recent_fills,
        net_positions=net_positions,
    )

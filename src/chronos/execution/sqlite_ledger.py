"""Durable SQLite order ledger for shadow/paper runs (ADR-0003).

Uses stdlib ``sqlite3`` with WAL journaling and synchronous=FULL: a ledger
write that returns has reached disk. The ledger is append-oriented: intents
are inserted once (duplicate ids violate the primary key), transitions and
fills are insert-only history. Nothing here updates or deletes rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from chronos.execution.intents import IntentStatus, OrderIntent
from chronos.utils.secure_files import secure_owner_only

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_price TEXT NOT NULL,
    stop_price TEXT,
    time_in_force TEXT NOT NULL,
    decision_timestamp_utc TEXT NOT NULL,
    source_bar_sequence_id TEXT NOT NULL,
    proposal_reason TEXT NOT NULL,
    initial_status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    cumulative_quantity INTEGER NOT NULL,
    average_price TEXT,
    commission_usd TEXT,
    at_utc TEXT NOT NULL
);
"""

_SCHEMA_VERSION = 1


class SqliteLedger:
    """LedgerPort implementation over a local SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        row = self._connection.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_info (version) VALUES (?)", (_SCHEMA_VERSION,)
            )
            self._connection.commit()
        elif row[0] != _SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger schema version {row[0]} unsupported (expected {_SCHEMA_VERSION}); "
                "refusing to run against an unknown schema"
            )
        secure_owner_only(path)
        secure_owner_only(path.with_name(path.name + "-wal"))
        secure_owner_only(path.with_name(path.name + "-shm"))

    def close(self) -> None:
        self._connection.close()

    def record_intent(self, intent: OrderIntent, status: IntentStatus) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO intents (
                    intent_id, strategy_id, strategy_version, symbol, side, quantity,
                    limit_price, stop_price, time_in_force, decision_timestamp_utc,
                    source_bar_sequence_id, proposal_reason, initial_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.strategy_id,
                    intent.strategy_version,
                    intent.symbol,
                    intent.side.value,
                    intent.quantity,
                    str(intent.limit_price),
                    str(intent.stop_price) if intent.stop_price is not None else None,
                    intent.time_in_force.value,
                    intent.decision_timestamp_utc.isoformat(),
                    intent.source_bar_sequence_id,
                    intent.proposal_reason,
                    status.value,
                ),
            )

    def has_intent(self, intent_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return row is not None

    def record_transition(
        self, intent_id: str, status: IntentStatus, at_utc: datetime, evidence: str
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO transitions (intent_id, status, at_utc, evidence) VALUES (?, ?, ?, ?)",
                (intent_id, status.value, at_utc.isoformat(), evidence),
            )

    def record_fill(
        self,
        intent_id: str,
        cumulative_quantity: int,
        average_price: Decimal | None,
        commission_usd: Decimal | None,
        at_utc: datetime,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO fills (intent_id, cumulative_quantity, average_price, "
                "commission_usd, at_utc) VALUES (?, ?, ?, ?, ?)",
                (
                    intent_id,
                    cumulative_quantity,
                    str(average_price) if average_price is not None else None,
                    str(commission_usd) if commission_usd is not None else None,
                    at_utc.isoformat(),
                ),
            )

    def working_intent_ids(self) -> tuple[str, ...]:
        """Intent ids whose latest transition is a working status."""

        rows = self._connection.execute(
            """
            SELECT t.intent_id, t.status FROM transitions t
            INNER JOIN (
                SELECT intent_id, MAX(id) AS max_id FROM transitions GROUP BY intent_id
            ) latest ON latest.max_id = t.id
            """
        ).fetchall()
        working = {
            IntentStatus.SUBMITTED.value,
            IntentStatus.PRE_SUBMITTED.value,
            IntentStatus.ACKNOWLEDGED.value,
            IntentStatus.PARTIALLY_FILLED.value,
            IntentStatus.PENDING_CANCEL.value,
        }
        return tuple(intent_id for intent_id, status in rows if status in working)

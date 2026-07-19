"""Runnable historical-data process (ADR-0011 §1).

    python -m chronos.histdata --symbols SPY,QQQ --end-date 2024-12-31 --duration-days 365

A standalone, **read-only** process: it connects to the gateway with the dedicated
``ib_data_client_id``, paces its historical requests, and writes unadjusted bars to
the file store. It opens no trading database, holds no writer lease, and imports no
order/broker module — enforced structurally by ``tests/safety/test_histdata_isolation.py``.

The real fetch runs only against a live gateway (owner-run; invariant 8/9). In this
environment the official client raises a clear "ibapi not installed" error, which the
per-symbol isolation in the coordinator turns into a reported outcome rather than a crash.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from chronos.histdata.backfill import backfill_symbols
from chronos.histdata.official_client import OfficialIBKRHistoricalClient
from chronos.histdata.pacing import PacingController

HISTORY_ROOT = Path(__file__).resolve().parents[3] / "research/data/history"


def _parse_symbols(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.histdata",
        description="Read-only IBKR historical-bar backfill into the file store.",
    )
    parser.add_argument("--symbols", required=True, help="comma-separated, e.g. SPY,QQQ")
    parser.add_argument(
        "--end-date", default=datetime.now(UTC).date().isoformat(), help="YYYY-MM-DD"
    )
    parser.add_argument("--duration-days", type=int, default=365)
    parser.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    parser.add_argument("--exchange", default="SMART")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    client = OfficialIBKRHistoricalClient(exchange=args.exchange)
    client.connect()
    try:
        outcomes = backfill_symbols(
            client,
            args.history_root,
            symbols,
            end_date=date.fromisoformat(args.end_date),
            duration_days=args.duration_days,
            pacing=PacingController(),
            now_fn=lambda: datetime.now(UTC),
            captured_at=now.isoformat(),
            exchange=args.exchange,
        )
    finally:
        client.disconnect()

    failures = 0
    for outcome in outcomes:
        if outcome.error is not None:
            failures += 1
        print(
            json.dumps(
                {
                    "symbol": outcome.symbol,
                    "rows": outcome.result.rows_written if outcome.result else None,
                    "added": outcome.result.rows_added if outcome.result else None,
                    "error": outcome.error,
                }
            )
        )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

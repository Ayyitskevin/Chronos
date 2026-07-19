"""Runnable historical-data process (ADR-0011 §1, ADR-0012 §5).

Two read-only subcommands::

    python -m chronos.histdata bars --symbols SPY,QQQ --end-date 2024-12-31 --duration-days 365
    python -m chronos.histdata options --symbols SPY,QQQ [--session 2026-07-19]

A standalone, **read-only** process: it connects to the gateway with the dedicated
``ib_data_client_id``, paces its requests, and writes to the file store. It opens no
trading database, holds no writer lease, and imports no order/broker module —
enforced structurally by ``tests/safety/test_histdata_isolation.py``.

The real fetch runs only against a live gateway (owner-run; invariant 8/9). In this
environment the official clients raise a clear "ibapi not installed" error at
``connect()``; the process reports it as a single connection-failure line and exits
non-zero. Once connected, a per-symbol fetch failure is isolated to that symbol's
outcome line by the coordinators.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from chronos.config.settings import get_settings
from chronos.histdata.backfill import backfill_symbols
from chronos.histdata.client import HistoricalDataError
from chronos.histdata.official_client import OfficialIBKRHistoricalClient
from chronos.histdata.official_options_client import OfficialIBKROptionClient
from chronos.histdata.options_capture import capture_symbols
from chronos.histdata.options_client import OptionSnapshotError
from chronos.histdata.pacing import PacingController

HISTORY_ROOT = Path(__file__).resolve().parents[3] / "research/data/history"


def _parse_symbols(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.histdata",
        description="Read-only IBKR data-plane capture into the file store.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bars = sub.add_parser("bars", help="backfill unadjusted historical daily bars")
    bars.add_argument("--symbols", required=True, help="comma-separated, e.g. SPY,QQQ")
    bars.add_argument("--end-date", default=datetime.now(UTC).date().isoformat(), help="YYYY-MM-DD")
    bars.add_argument("--duration-days", type=int, default=365)
    bars.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    bars.add_argument("--exchange", default="SMART")
    bars.set_defaults(func=_run_bars)

    options = sub.add_parser("options", help="capture an EOD option-chain snapshot")
    options.add_argument("--symbols", required=True, help="comma-separated underlyings")
    options.add_argument(
        "--session", default=datetime.now(UTC).date().isoformat(), help="YYYY-MM-DD"
    )
    options.add_argument("--horizon-days", type=int, default=None, help="expiry horizon (setting)")
    options.add_argument(
        "--strike-window-pct", type=float, default=None, help="band of spot (setting)"
    )
    options.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    options.add_argument("--allow-correction", action="store_true")
    options.set_defaults(func=_run_options)

    return parser


def _run_bars(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    client = OfficialIBKRHistoricalClient(exchange=args.exchange)
    try:
        client.connect()
    except HistoricalDataError as error:
        print(json.dumps({"error": f"connect failed: {error}"}))
        return 1
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


def _run_options(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    settings = get_settings()
    horizon = (
        args.horizon_days
        if args.horizon_days is not None
        else settings.option_capture_expiry_horizon_days
    )
    window = (
        args.strike_window_pct
        if args.strike_window_pct is not None
        else settings.option_capture_strike_window_pct
    )
    now = datetime.now(UTC)
    client = OfficialIBKROptionClient()
    try:
        client.connect()
    except OptionSnapshotError as error:
        print(json.dumps({"error": f"connect failed: {error}"}))
        return 1
    try:
        outcomes = capture_symbols(
            args.history_root,
            client,
            symbols,
            session=date.fromisoformat(args.session),
            captured_at=now.isoformat(),
            source="ibkr",
            expiry_horizon_days=horizon,
            strike_window_pct=window,
            allow_correction=args.allow_correction,
        )
    finally:
        client.disconnect()

    failures = 0
    for outcome in outcomes:
        if outcome.error is not None:
            failures += 1
        snapshot = outcome.snapshot
        print(
            json.dumps(
                {
                    "underlying": outcome.underlying,
                    "rows": len(snapshot.rows) if snapshot else None,
                    "worst_quality": snapshot.worst_quality().value if snapshot else None,
                    "reason": snapshot.reason if snapshot else None,
                    "error": outcome.error,
                }
            )
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.func
    result: int = handler(args)
    return result


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

"""``reconciliation-runs`` — list persisted reconciliation runs (BP-3), read-only.

Opens ONLY the database (``Database(settings.database_url)``); it never builds the runtime and
never contacts a broker. Prints one JSON line per run, oldest first within the window, masked as
persisted (the raw broker account id is never in a row — K2 (a)).

Registration in ``chronos.cli.main`` is a two-line change outside BP-3's owned set
(``add_reconciliation_runs_command`` is the hook, the ``add_selection_command`` pattern); until it
is registered the command runs as ``python -m chronos.cli.reconciliation_commands --last N``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from chronos.config.settings import get_settings
from chronos.persistence.database import Database
from chronos.persistence.reconciliation_repository import ReconciliationRepository


def cmd_reconciliation_runs(args: argparse.Namespace) -> int:
    limit = int(args.last)
    if limit < 1:
        print("reconciliation-runs: --last must be >= 1", file=sys.stderr)
        return 64
    database = Database(get_settings().database_url)
    try:
        records = ReconciliationRepository(database.sessions).recent(limit=limit)
    finally:
        database.dispose()
    for record in records:
        line: dict[str, Any] = {
            "run_id": record.run_id,
            "trigger": record.trigger,
            "status": record.status,
            "started_at": record.started_at.isoformat(),
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "broker_snapshot": record.broker_snapshot,
            "decisions": record.decisions,
        }
        print(json.dumps(line, sort_keys=True, separators=(",", ":")))
    return 0


def add_reconciliation_runs_command(sub: Any) -> None:
    runs = sub.add_parser(
        "reconciliation-runs",
        help="list persisted reconciliation runs, oldest first within the window (read-only)",
    )
    runs.add_argument("--last", type=int, default=20, help="how many of the newest runs to list")
    runs.set_defaults(func=cmd_reconciliation_runs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chronos.cli.reconciliation_commands")
    sub = parser.add_subparsers(dest="command", required=True)
    add_reconciliation_runs_command(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["add_reconciliation_runs_command", "cmd_reconciliation_runs", "main"]

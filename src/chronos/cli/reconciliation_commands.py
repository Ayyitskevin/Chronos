"""``reconciliation-runs`` / ``position-provenance`` — persisted reconciliation evidence, read-only.

Opens ONLY the database (``Database(settings.database_url)``); it never builds the runtime and
never contacts a broker. ``reconciliation-runs --last N`` prints one JSON line per run (BP-3),
oldest first within the window; ``position-provenance --run <id>`` prints one JSON line per
provenance row observed at that run (AP-1). Both are masked as persisted (the raw broker
account id is never in a row — K2 (a)). An unknown run id is a typed refusal: one line on
stderr, exit 2.

``chronos.cli.main`` registers both through ``add_reconciliation_runs_command`` (BP-3 r1, the
``add_selection_command`` pattern); the module form
``python -m chronos.cli.reconciliation_commands`` carries the same two subcommands.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from chronos.config.settings import get_settings
from chronos.persistence.acknowledgement_repository import (
    AcknowledgementRepository,
    InvalidAcknowledgement,
    UnknownAcknowledgement,
    operator_fingerprint,
)
from chronos.persistence.database import Database
from chronos.persistence.reconciliation_repository import ReconciliationRepository
from chronos.portfolio.provenance import PositionProvenanceRepository, UnknownReconciliationRun


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


def cmd_position_provenance(args: argparse.Namespace) -> int:
    run_id = str(args.run).strip()
    if not run_id:
        print(
            "position-provenance: --run must name a persisted reconciliation run", file=sys.stderr
        )
        return 64
    database = Database(get_settings().database_url)
    try:
        records = PositionProvenanceRepository(database.sessions).rows_for_run(run_id)
    except UnknownReconciliationRun:
        print(f"position-provenance: no persisted reconciliation run {run_id!r}", file=sys.stderr)
        return 2
    finally:
        database.dispose()
    for record in records:
        line: dict[str, Any] = {
            "id": record.id,
            "account_fingerprint": record.account_fingerprint,
            "position_key": record.position_key,
            "origin_class": record.origin_class,
            "evidence_ref": record.evidence_ref,
            "first_seen_run_id": record.first_seen_run_id,
            "observed_run_id": record.observed_run_id,
            "quantity": str(record.quantity),
            "recorded_at": record.recorded_at.isoformat(),
        }
        print(json.dumps(line, sort_keys=True, separators=(",", ":")))
    return 0


def add_position_provenance_command(sub: Any) -> None:
    provenance = sub.add_parser(
        "position-provenance",
        help="list the origin class recorded for every position at one run (read-only)",
    )
    provenance.add_argument("--run", required=True, help="the persisted reconciliation run id")
    provenance.set_defaults(func=cmd_position_provenance)


def cmd_position_acknowledge(args: argparse.Namespace) -> int:
    """Append ONE acknowledgement row (or ONE superseding row with --withdraw); never the broker."""

    note = str(args.note)
    database = Database(get_settings().database_url)
    line: dict[str, Any]
    try:
        repository = AcknowledgementRepository(database.sessions)
        fingerprint = operator_fingerprint()
        if args.withdraw is not None:
            acknowledgement_id = repository.withdraw(
                acknowledgement_id=int(args.withdraw), note=note, operator_fingerprint=fingerprint
            )
            line = {"acknowledgement_id": acknowledgement_id, "withdraws": int(args.withdraw)}
        else:
            if args.key is None:
                print("position-acknowledge: --key is required unless --withdraw", file=sys.stderr)
                return 64
            acknowledgement_id = repository.acknowledge(
                position_key=str(args.key), note=note, operator_fingerprint=fingerprint
            )
            line = {"acknowledgement_id": acknowledgement_id, "position_key": str(args.key)}
    except InvalidAcknowledgement as error:
        print(f"position-acknowledge: {error}", file=sys.stderr)
        return 64
    except UnknownAcknowledgement as error:
        print(
            f"position-acknowledge: no current acknowledgement {int(str(error))}", file=sys.stderr
        )
        return 2
    finally:
        database.dispose()
    print(json.dumps(line, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_position_acknowledgements(args: argparse.Namespace) -> int:
    limit = int(args.last)
    if limit < 1:
        print("position-acknowledgements: --last must be >= 1", file=sys.stderr)
        return 64
    database = Database(get_settings().database_url)
    try:
        records = AcknowledgementRepository(database.sessions).recent(limit=limit)
    finally:
        database.dispose()
    for record in records:
        line: dict[str, Any] = {
            "id": record.id,
            "position_key": record.position_key,
            "note": record.note,
            "acknowledged_at": record.acknowledged_at.isoformat(),
            "operator_fingerprint": record.operator_fingerprint,
            "superseded_by": record.superseded_by,
        }
        print(json.dumps(line, sort_keys=True, separators=(",", ":")))
    return 0


def add_position_acknowledgement_commands(sub: Any) -> None:
    acknowledge = sub.add_parser(
        "position-acknowledge",
        help="record ONE operator acknowledgement of a position key (append-only, record-only)",
    )
    acknowledge.add_argument(
        "--key", default=None, help="<con_id>:<security_type>:<LONG|SHORT|FLAT>"
    )
    acknowledge.add_argument("--note", required=True, help="why (1-500 chars, one line)")
    acknowledge.add_argument(
        "--withdraw", type=int, default=None, help="supersede this acknowledgement id instead"
    )
    acknowledge.set_defaults(func=cmd_position_acknowledge)
    listing = sub.add_parser(
        "position-acknowledgements",
        help="list acknowledgement rows, oldest first within the window (read-only)",
    )
    listing.add_argument("--last", type=int, default=20, help="how many of the newest rows to list")
    listing.set_defaults(func=cmd_position_acknowledgements)


def add_reconciliation_runs_command(sub: Any) -> None:
    runs = sub.add_parser(
        "reconciliation-runs",
        help="list persisted reconciliation runs, oldest first within the window (read-only)",
    )
    runs.add_argument("--last", type=int, default=20, help="how many of the newest runs to list")
    runs.set_defaults(func=cmd_reconciliation_runs)
    # AP-1: the provenance reader rides the same registration hook (cli/main.py:571) so the
    # real CLI carries it without a second edit outside AP-1's owned set.
    add_position_provenance_command(sub)
    add_position_acknowledgement_commands(sub)  # AP-1b: the MANUAL producer + its listing


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


__all__ = [
    "add_position_acknowledgement_commands",
    "add_position_provenance_command",
    "add_reconciliation_runs_command",
    "cmd_position_acknowledge",
    "cmd_position_acknowledgements",
    "cmd_position_provenance",
    "cmd_reconciliation_runs",
    "main",
]

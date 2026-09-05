"""Read-only operator commands for owner-supplied market-data deliveries."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from chronos.research.data_intake import IntakeUnverified, verify_intake


def cmd_data_verify(args: argparse.Namespace) -> int:
    """Verify one delivery without writing evidence, releases, or corpus state."""

    manifest_path = args.delivery / "INTAKE.json"
    try:
        report = verify_intake(args.delivery)
    except IntakeUnverified as error:
        print(f"UNVERIFIED {error.path}: {error.reason}")
        return 2

    if report.certified:
        print(f"CERTIFIED {manifest_path}: {report.certification_digest}")
        return 0
    finding_kinds = ",".join(finding.kind for finding in report.findings)
    print(
        f"NOT_CERTIFIED {manifest_path}: {len(report.findings)} blocking finding(s): "
        f"{finding_kinds}"
    )
    return 1


def add_data_commands(sub: Any) -> None:
    """Register the read-only market-data intake command group."""

    data = sub.add_parser("data", help="owner-supplied market-data intake tools (read-only)")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    verify = data_sub.add_parser("verify", help="verify an on-disk delivery without writes")
    verify.add_argument("--delivery", type=Path, required=True)
    verify.set_defaults(func=cmd_data_verify)


__all__ = ["add_data_commands", "cmd_data_verify"]

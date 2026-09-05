"""Operator commands for owner-supplied market-data deliveries."""

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


def cmd_data_certify(args: argparse.Namespace) -> int:
    """Certify one delivery before freezing and merging it into existing stores."""

    # Keep the writing module off the repeatedly-run read-only verify import path.
    from chronos.research.data_certification import (
        HISTORY_ROOT,
        DataCertificationWriteError,
        certify_delivery,
    )

    manifest_path = args.delivery / "INTAKE.json"
    try:
        result = certify_delivery(
            args.delivery,
            output_root=args.output,
            history_root=HISTORY_ROOT,
        )
    except IntakeUnverified as error:
        print(f"UNVERIFIED {error.path}: {error.reason}")
        return 2
    except DataCertificationWriteError as error:
        print(f"WRITE_FAILED {error.path}: {error.reason}")
        return 2

    if not result.report.certified:
        finding_kinds = ",".join(finding.kind for finding in result.report.findings)
        print(
            f"NOT_CERTIFIED {manifest_path}: {len(result.report.findings)} "
            f"blocking finding(s): {finding_kinds}"
        )
        return 1

    assert result.release is not None
    print(
        f"CERTIFIED {manifest_path}: {result.report.certification_digest}; "
        f"RELEASE {args.output / 'release.json'}: {result.release.release_digest}; "
        f"STORED {result.history_root}: {result.bars_added} bars, "
        f"{result.actions_stored} actions"
    )
    return 0


def add_data_commands(sub: Any) -> None:
    """Register the market-data intake command group."""

    data = sub.add_parser("data", help="owner-supplied market-data intake tools")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    verify = data_sub.add_parser("verify", help="verify an on-disk delivery without writes")
    verify.add_argument("--delivery", type=Path, required=True)
    verify.set_defaults(func=cmd_data_verify)
    certify = data_sub.add_parser(
        "certify", help="certify, freeze, and merge an on-disk delivery"
    )
    certify.add_argument("--delivery", type=Path, required=True)
    certify.add_argument("--output", type=Path, required=True)
    certify.set_defaults(func=cmd_data_certify)


__all__ = ["add_data_commands", "cmd_data_certify", "cmd_data_verify"]

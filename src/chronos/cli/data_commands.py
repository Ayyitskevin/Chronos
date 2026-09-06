"""Operator commands for owner-supplied market-data deliveries."""

from __future__ import annotations

import argparse
from datetime import date
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
        print(
            f"CERTIFIED {manifest_path}: certification_report_sha256={report.certification_digest}"
        )
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
        f"CERTIFIED {manifest_path}: "
        f"certification_report_sha256={result.report.certification_digest}; "
        f"RELEASE {args.output / 'release.json'}: "
        f"release_digest={result.release.release_digest}; "
        f"STORED {result.history_root}: {result.bars_added} bars, "
        f"{result.actions_stored} actions"
    )
    return 0


def cmd_data_synth_store(args: argparse.Namespace) -> int:
    """Write a deterministic synthetic six-symbol store; no network, no market data."""

    # Import inside the command so the repeatedly-run read-only verify path does not carry
    # a generator it never calls.
    from chronos.research.synth_store import DEFAULT_END, DEFAULT_START, generate_store

    # Defaults are resolved here, not as argparse defaults: importing synth_store at module
    # scope would put chronos.research.session_calendar on the CLI's import graph, which
    # tests/safety/test_session_calendar_isolation.py forbids (R-26 keeps market-open
    # evidence on the venue's own CLOSED token, so the research calendar must not become
    # reachable from the trading plane).
    start = args.start if args.start is not None else DEFAULT_START
    end = args.end if args.end is not None else DEFAULT_END
    try:
        written = generate_store(args.out, seed=args.seed, start=start, end=end)
    except ValueError as error:
        print(f"REFUSED {args.out}: {error}")
        return 2
    total = sum(written.values())
    detail = ", ".join(f"{symbol} {rows}" for symbol, rows in sorted(written.items()))
    print(
        f"SYNTH_STORE {args.out}: {total} synthetic bars across {len(written)} symbols ({detail})"
    )
    print("These are generated prices, not market data; the manifest records source=synthetic.")
    return 0


def _session_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from error


def add_data_commands(sub: Any) -> None:
    """Register the market-data intake command group."""

    data = sub.add_parser("data", help="owner-supplied market-data intake tools")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    verify = data_sub.add_parser("verify", help="verify an on-disk delivery without writes")
    verify.add_argument("--delivery", type=Path, required=True)
    verify.set_defaults(func=cmd_data_verify)
    certify = data_sub.add_parser("certify", help="certify, freeze, and merge an on-disk delivery")
    certify.add_argument("--delivery", type=Path, required=True)
    certify.add_argument("--output", type=Path, required=True)
    certify.set_defaults(func=cmd_data_certify)
    synth = data_sub.add_parser(
        "synth-store",
        help="write a deterministic synthetic six-symbol store (no network, no market data)",
    )
    synth.add_argument("--out", type=Path, required=True)
    synth.add_argument("--seed", type=int, required=True)
    synth.add_argument("--start", type=_session_date, default=None)
    synth.add_argument("--end", type=_session_date, default=None)
    synth.set_defaults(func=cmd_data_synth_store)


__all__ = [
    "add_data_commands",
    "cmd_data_certify",
    "cmd_data_synth_store",
    "cmd_data_verify",
]

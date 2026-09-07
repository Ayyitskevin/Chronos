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


def cmd_data_check(args: argparse.Namespace) -> int:
    """Report the per-symbol gates over a partial capture store. Read-only, no verdict.

    Exit code is a count, not a judgement: 0 when the gates found nothing, 1 when they
    found something, 2 when the store could not be read as a subject at all. There is
    deliberately no word here that a reader could mistake for a certification.
    """

    # Local import for the same reason assemble's and certify's are: keep the research
    # plane off the import path of every command that does not need it.
    from chronos.research.data_check import CheckRefusal, check_store

    symbols = tuple(part.strip() for part in args.symbols.split(",") if part.strip())
    try:
        result = check_store(args.store, symbols or None)
    except CheckRefusal as error:
        print(f"REFUSED {error.path}: {error.reason}")
        return 2

    for item in result.symbols:
        actions = (
            "no action file" if item.action_count is None else f"{item.action_count} action(s)"
        )
        witnesses = (
            "manifest witnesses checked" if item.manifest_checked else "no manifest witnesses"
        )
        print(
            f"CHECKED {item.symbol}: {item.bar_count} bars {item.start.isoformat()}.."
            f"{item.end.isoformat()}, coverage {item.coverage:.4f}, {actions}, {witnesses}, "
            f"{len(item.findings)} finding(s)"
        )
        for finding in item.findings:
            where = f" {finding.session_date.isoformat()}" if finding.session_date else ""
            print(f"  FINDING {finding.kind}{where}: {finding.detail}")

    print(
        f"GATES RUN over {len(result.symbols)} symbol(s) in {args.store}: "
        f"{result.finding_count} finding(s). This is not a certification — "
        "a delivery of all six symbols still has to pass data verify."
    )
    return 1 if result.finding_count else 0


def cmd_data_assemble(args: argparse.Namespace) -> int:
    """Turn a capture store into a delivery directory. Read-only on the store."""

    # Local import for the same reason certify's is local: keep the module that reaches the
    # research plane off the import path of every command that does not need it.
    from chronos.research.data_assemble import AssembleRefusal, assemble_delivery

    provenance = {
        "source_id": args.source_id,
        "source_receipt_sha256": args.source_receipt_sha256,
        "retrieved_at": args.retrieved_at,
        "retrieval_method": args.retrieval_method,
        "license_note": args.license_note,
    }
    try:
        result = assemble_delivery(
            args.store,
            args.out,
            delivery_id=args.delivery_id,
            provenance=provenance,
            provider_price_basis=args.provider_price_basis,
            attestation_path=args.attestation,
            classified_moves_path=args.classified_moves,
            supersedes=args.supersedes,
        )
    except AssembleRefusal as error:
        print(f"REFUSED {error.path}: {error.reason}")
        return 2

    # An owner-declared empty action stream is indistinguishable from a full one downstream,
    # so it is named here rather than passed over: assemble refuses an ABSENT stream, but an
    # explicit `[]` is a statement the owner made and the operator should see it echoed back.
    declared_empty = (
        f"; owner-declared-no-actions: {', '.join(result.owner_declared_no_actions)}"
        if result.owner_declared_no_actions
        else ""
    )
    print(
        f"ASSEMBLED {result.delivery / 'INTAKE.json'}: {len(result.symbols)} symbols, "
        f"{result.bar_rows} bars, {result.action_count} actions, "
        f"{result.holdout_spans} holdout span(s){declared_empty}; run data verify next"
    )
    return 0


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
    check = data_sub.add_parser(
        "check",
        help="run the per-symbol gates over a partial capture store (read-only, no verdict)",
    )
    check.add_argument("--store", type=Path, required=True)
    # One flag for both spellings: --symbol DIA and --symbols DIA,SPY are the same argument,
    # because an owner capturing one symbol should not have to notice the plural.
    check.add_argument("--symbol", "--symbols", dest="symbols", default="")
    check.set_defaults(func=cmd_data_check)
    assemble = data_sub.add_parser(
        "assemble", help="turn a capture store into a delivery directory (read-only on the store)"
    )
    assemble.add_argument("--store", type=Path, required=True)
    assemble.add_argument("--out", type=Path, required=True)
    assemble.add_argument("--delivery-id", required=True)
    # Provenance is an owner assertion; every field is explicit and none has a default.
    assemble.add_argument("--source-id", default="")
    assemble.add_argument("--source-receipt-sha256", default="")
    assemble.add_argument("--retrieved-at", default="")
    assemble.add_argument("--retrieval-method", default="")
    assemble.add_argument("--license-note", default="")
    # Schema 2's vendor fact (ADR-0059). Empty by default rather than argparse-required, so
    # the refusal that names it comes from the assembler with its reasoning attached, the
    # same way a missing provenance field is reported.
    assemble.add_argument("--provider-price-basis", default="")
    assemble.add_argument("--attestation", type=Path, default=None)
    assemble.add_argument("--classified-moves", type=Path, default=None)
    assemble.add_argument("--supersedes", default=None)
    assemble.set_defaults(func=cmd_data_assemble)


__all__ = [
    "add_data_commands",
    "cmd_data_assemble",
    "cmd_data_certify",
    "cmd_data_check",
    "cmd_data_synth_store",
    "cmd_data_verify",
]

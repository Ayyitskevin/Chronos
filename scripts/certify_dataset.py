#!/usr/bin/env python3
"""Certify a historical-data export, and freeze it into an immutable release.

The owner half of D2 — running TWS and pulling the history — cannot be automated. This
is the other half: one command that judges what the pull produced against the Phase 3
gates frozen in ``docs/VISION_COMPLETION_PLAN.md`` §8, and, only if it passes, freezes
it into content-addressed partitions with the catalog manifest and release digest the
research plane reads.

    python scripts/certify_dataset.py certify --declaration research/data/certify.json
    python scripts/certify_dataset.py freeze  --declaration research/data/certify.json \
        --output research/data/releases/etf-daily-001

``certify`` exits non-zero when the export does not certify, so it is usable as a gate.
``freeze`` refuses outright rather than minting a digest over a failed verdict.

The declaration is JSON and deliberately explicit — nothing about a holdout map should
be inferred:

    {
      "dataset_id": "chronos-etf-daily-v1",
      "interval": "1d",
      "catalog_id": "chronos-etf-daily-v1-release-001",
      "source_id": "ibkr-tws-historical",
      "source_receipt_sha256": "<64 hex>",
      "attestation": {
        "source_id": "nasdaq-dividend-history-2026-08-21",
        "sampled_action_count": 12,
        "symbols": ["SPY", "QQQ"],
        "note": "owner reconciled 12 actions against a second source"
      },
      "windows": [{"symbol": "SPY", "start": "2000-01-03", "end": "2024-12-31"}],
      "holdout_map": [
        {"symbol": "SPY", "name": "train", "start": "2000-01-03", "end": "2021-12-31",
         "status": "seen", "reason": "ordinary research window"},
        {"symbol": "SPY", "name": "final-test", "start": "2022-01-03", "end": "2024-12-31",
         "status": "clean", "reason": "untouched final test"}
      ],
      "classified_moves": []
    }

``interval`` is ``"1d"`` (default) or ``"1h"``; hourly reads the ``bars_1h/``
store lane and certifies at bar granularity.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from chronos.histdata.store import read_actions, read_bars, read_hourly_bars
from chronos.marketdata.bars import BarInterval
from chronos.research.certification import (
    CertificationReport,
    ClassifiedMove,
    CorporateActionAttestation,
    SymbolWindow,
    certify_export,
)
from chronos.research.dataset_release import HoldoutSpan, HoldoutStatus, freeze_release

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "research/data/history"


def _load_declaration(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{path}: declaration must be a JSON object")
    return document


def _windows(document: dict[str, Any]) -> list[SymbolWindow]:
    return [
        SymbolWindow(
            symbol=str(entry["symbol"]).upper(),
            start=date.fromisoformat(entry["start"]),
            end=date.fromisoformat(entry["end"]),
        )
        for entry in document.get("windows", [])
    ]


def _attestation(document: dict[str, Any]) -> CorporateActionAttestation | None:
    raw = document.get("attestation")
    if not raw:
        return None
    return CorporateActionAttestation(
        source_id=str(raw["source_id"]),
        sampled_action_count=int(raw["sampled_action_count"]),
        symbols=tuple(str(symbol).upper() for symbol in raw["symbols"]),
        note=str(raw.get("note", "")),
    )


def _classified_moves(document: dict[str, Any]) -> list[ClassifiedMove]:
    return [
        ClassifiedMove(
            symbol=str(entry["symbol"]).upper(),
            session_date=date.fromisoformat(entry["session_date"]),
            reason=str(entry["reason"]),
        )
        for entry in document.get("classified_moves", [])
    ]


def _spans(document: dict[str, Any]) -> list[HoldoutSpan]:
    return [
        HoldoutSpan(
            symbol=str(entry["symbol"]).upper(),
            name=str(entry["name"]),
            start=date.fromisoformat(entry["start"]),
            end=date.fromisoformat(entry["end"]),
            status=HoldoutStatus(str(entry["status"])),
            reason=str(entry.get("reason", "")),
        )
        for entry in document.get("holdout_map", [])
    ]


def _interval(document: dict[str, Any]) -> BarInterval:
    raw = str(document.get("interval", "1d"))
    try:
        return BarInterval(raw)
    except ValueError as error:
        raise SystemExit(f"declaration interval {raw!r} is not a bar interval") from error


def _run_certification(
    document: dict[str, Any], history_root: Path
) -> tuple[CertificationReport, dict[str, Any]]:
    windows = _windows(document)
    if not windows:
        raise SystemExit("declaration has no windows; nothing to certify")
    interval = _interval(document)
    symbols = [window.symbol for window in windows]
    if interval is BarInterval.HOUR_1:
        series_by_symbol = {symbol: read_hourly_bars(history_root, symbol) for symbol in symbols}
    else:
        series_by_symbol = {symbol: read_bars(history_root, symbol) for symbol in symbols}
    actions_by_symbol = {symbol: read_actions(history_root, symbol) for symbol in symbols}
    report = certify_export(
        dataset_id=str(document["dataset_id"]),
        windows=windows,
        series_by_symbol=series_by_symbol,
        actions_by_symbol=actions_by_symbol,
        attestation=_attestation(document),
        classified_moves=_classified_moves(document),
        interval=interval,
    )
    return report, series_by_symbol


def _print_report(report: CertificationReport) -> None:
    print(f"dataset        {report.dataset_id}")
    print(f"interval       {report.interval}")
    print(f"verdict        {report.verdict}")
    print(f"digest         {report.certification_digest}")
    for entry in report.coverage:
        flag = "ok " if entry.meets_floor else "LOW"
        if entry.expected_bar_total is not None:
            print(
                f"  {flag} {entry.symbol:<8} coverage {entry.coverage:.4%} "
                f"({entry.observed_slot_bars}/{entry.expected_bar_total} bars over "
                f"{entry.expected_sessions} sessions, "
                f"{len(entry.missing_bar_timestamps)} bars missing, "
                f"{len(entry.unexpected_bar_timestamps)} off-slot)"
            )
        else:
            print(
                f"  {flag} {entry.symbol:<8} coverage {entry.coverage:.4%} "
                f"({entry.observed_bars}/{entry.expected_sessions} sessions, "
                f"{len(entry.missing_sessions)} missing, {len(entry.unexpected_bars)} unexpected)"
            )
    if report.findings:
        print(f"findings       {len(report.findings)}")
        for finding in report.findings:
            when = finding.session_date.isoformat() if finding.session_date else "-"
            print(f"  {finding.kind:<28} {finding.symbol:<6} {when}  {finding.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="certify_dataset.py",
        description="Judge a historical-data export against the frozen Phase 3 gates.",
    )
    parser.add_argument("command", choices=("certify", "freeze"))
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    parser.add_argument("--output", type=Path, help="release directory (freeze only)")
    args = parser.parse_args(argv)

    document = _load_declaration(args.declaration)
    report, series_by_symbol = _run_certification(document, args.history_root)
    _print_report(report)

    if args.command == "certify":
        return 0 if report.certified else 1

    if args.output is None:
        raise SystemExit("freeze requires --output")
    release = freeze_release(
        dataset_id=str(document["dataset_id"]),
        catalog_id=str(document["catalog_id"]),
        source_id=str(document["source_id"]),
        source_receipt_sha256=str(document["source_receipt_sha256"]),
        certification=report,
        series_by_symbol=series_by_symbol,
        spans=_spans(document),
        output_root=args.output,
    )
    manifest_path = args.output / "catalog.json"
    manifest_path.write_bytes(release.catalog_manifest_bytes())
    (args.output / "release.json").write_bytes(release.release_document_bytes())
    print()
    print(f"partitions     {len(release.partitions)} written under {args.output}")
    print(f"catalog sha256 {release.catalog_manifest_sha256}   <- trusted digest for readers")
    print(f"release digest {release.release_digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

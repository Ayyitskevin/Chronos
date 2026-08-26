#!/usr/bin/env python3
"""Prepare and verify the owner-run QQQ certified-data packet.

This script does not contact IBKR, unlock a holdout, register a trial, or freeze a
release.  The interactive owner wizard calls it after the existing read-only exporter:

* ``ingest-actions`` validates six owner-prepared corporate-action streams and writes
  them through the canonical historical-data store;
* ``finalize-receipt`` authenticates the exact campaign capture and writes an immutable,
  sanitized source receipt binding bars, actions, manifest, and capture output; and
* ``build-declaration`` creates (or revalidates) the conservative owner-approved
  clean/seen/burned map without opening the clean partition through a research reader.

The constants below are the reviewed QQQ campaign identity.  A changed symbol, cutoff,
interval, routing venue, dataset id, catalog id, or holdout boundary needs a new reviewed
artifact rather than an ad-hoc command-line override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from chronos.histdata.corporate_actions import CorporateAction
from chronos.histdata.store import write_actions

SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
SYMBOL_SET = frozenset(SYMBOLS)
END_DATE = "2026-08-21"
DURATION_DAYS = 9_500
BAR_INTERVAL = "1d"
EXCHANGE = "SMART"
SOURCE_ID = "ibkr-tws-historical"
BAR_SOURCE = "ibkr"
DATASET_ID = "chronos-qqq-robustness-daily-v1"
CATALOG_ID = "chronos-qqq-robustness-daily-v1-release-001"
RECEIPT_SCHEMA = "chronos-qqq-source-receipt-v1"
MINIMUM_ATTESTED_ACTIONS = 12

_IBKR_SOURCE_MARKERS = frozenset(
    {
        "ibkr",
        "interactivebroker",
        "interactivebrokers",
        "tws",
        "traderworkstation",
        "ibgateway",
        "ibasync",
    }
)
_LONGEST_IBKR_SOURCE_MARKER = max(len(marker) for marker in _IBKR_SOURCE_MARKERS)

SEEN_SPLIT = date(2022, 1, 1)
BURNED_END = date(2024, 1, 10)
CLEAN_START = date(2024, 1, 11)


class PacketError(RuntimeError):
    """The packet differs from the reviewed identity or lacks required evidence."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PacketError(f"required file is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise PacketError(f"{path}: invalid JSON: {error}") from error


def _mapping(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PacketError(f"{context} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as error:
        raise PacketError(f"required file is missing: {path}") from error
    return digest.hexdigest()


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _uses_ibkr_source_identity(source: str) -> bool:
    normalized = unicodedata.normalize("NFKC", source).casefold()
    tokens = tuple(re.findall(r"[a-z0-9]+", normalized))
    for start in range(len(tokens)):
        candidate = ""
        for token in tokens[start:]:
            candidate += token
            if len(candidate) > _LONGEST_IBKR_SOURCE_MARKER:
                break
            if candidate in _IBKR_SOURCE_MARKERS:
                return True
    return False


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise PacketError(
                f"refusing to overwrite immutable packet artifact {path}; "
                "use a new campaign identity"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _manifest(history_root: Path) -> dict[str, Any]:
    manifest = _mapping(_load_json(history_root / "MANIFEST.json"), context="MANIFEST.json")
    symbols = _mapping(manifest.get("symbols"), context="MANIFEST.json symbols")
    supplied = set(symbols)
    if supplied != SYMBOL_SET:
        raise PacketError(
            "manifest symbol identity differs from the six-symbol QQQ release: "
            f"missing={sorted(SYMBOL_SET - supplied)}, extra={sorted(supplied - SYMBOL_SET)}"
        )
    return manifest


def _validate_bars(history_root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    symbols = _mapping(manifest["symbols"], context="MANIFEST.json symbols")
    validated: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        entry = _mapping(symbols[symbol], context=f"manifest {symbol}")
        bars = _mapping(entry.get("bars"), context=f"manifest {symbol}.bars")
        expected = {
            "source": BAR_SOURCE,
            "exchange": EXCHANGE,
            "adjusted": False,
            "end": END_DATE,
            "corrections": [],
        }
        for key, value in expected.items():
            if bars.get(key) != value:
                raise PacketError(
                    f"{symbol}: manifest bars.{key}={bars.get(key)!r}, expected {value!r}"
                )
        rows = bars.get("rows")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
            raise PacketError(f"{symbol}: manifest row count must be a positive integer")
        start = bars.get("start")
        if not isinstance(start, str):
            raise PacketError(f"{symbol}: manifest start date is missing")
        try:
            if date.fromisoformat(start) > date.fromisoformat(END_DATE):
                raise PacketError(f"{symbol}: manifest starts after the campaign cutoff")
        except ValueError as error:
            raise PacketError(f"{symbol}: invalid manifest date: {error}") from error

        bars_path = history_root / "bars" / f"{symbol}.csv"
        if _sha256(bars_path) != bars.get("sha256"):
            raise PacketError(f"{symbol}: bars bytes do not match the manifest sha256")
        lines = bars_path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != "date,open,high,low,close,volume":
            raise PacketError(f"{symbol}: daily bars header is not canonical")
        if len(lines) - 1 != rows:
            raise PacketError(f"{symbol}: CSV row count does not match the manifest")
        if not lines[-1].startswith(f"{END_DATE},"):
            raise PacketError(f"{symbol}: final CSV row is not the frozen cutoff {END_DATE}")
        validated[symbol] = dict(bars)
    return validated


def _validate_capture_log(path: Path) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    expected_keys = {"symbol", "rows", "added", "empty_chunks", "error"}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise PacketError(f"capture output is missing: {path}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            outcome = _mapping(json.loads(line), context=f"capture line {line_number}")
        except json.JSONDecodeError as error:
            raise PacketError(f"capture line {line_number} is not JSON: {error}") from error
        if set(outcome) != expected_keys:
            raise PacketError(
                f"capture line {line_number} has unexpected fields: "
                f"{sorted(set(outcome) - expected_keys)}"
            )
        symbol = outcome.get("symbol")
        if not isinstance(symbol, str) or symbol not in SYMBOL_SET:
            raise PacketError(f"capture line {line_number} has unexpected symbol {symbol!r}")
        if symbol in outcomes:
            raise PacketError(f"capture output repeats {symbol}")
        if outcome.get("error") is not None:
            raise PacketError(f"{symbol}: capture reported an error")
        rows = outcome.get("rows")
        added = outcome.get("added")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
            raise PacketError(f"{symbol}: capture rows must be a positive integer")
        if isinstance(added, bool) or not isinstance(added, int) or added < 0:
            raise PacketError(f"{symbol}: capture added must be a non-negative integer")
        if outcome.get("empty_chunks") != []:
            raise PacketError(f"{symbol}: daily capture unexpectedly reported hourly empty chunks")
        # Persist only the authenticated exporter schema in the sanitized receipt.
        outcomes[symbol] = {
            "symbol": symbol,
            "rows": rows,
            "added": added,
            "empty_chunks": [],
            "error": None,
        }
    if set(outcomes) != SYMBOL_SET:
        raise PacketError(
            f"capture output is incomplete: missing={sorted(SYMBOL_SET - set(outcomes))}"
        )
    return outcomes


def _parse_action_file(path: Path, *, symbol: str, start: date) -> tuple[CorporateAction, ...]:
    raw = _load_json(path)
    if not isinstance(raw, list):
        raise PacketError(f"{path}: corporate-action input must be a JSON array")
    actions: list[CorporateAction] = []
    seen: set[tuple[str, str, float, str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PacketError(f"{path}: action {index} must be a JSON object")
        try:
            action = CorporateAction.from_mapping(item)
        except (TypeError, ValueError) as error:
            raise PacketError(f"{path}: invalid action {index}: {error}") from error
        if _uses_ibkr_source_identity(action.source):
            raise PacketError(
                f"{path}: {action.ex_date} uses the IBKR/TWS source family; "
                "the action stream must be independent of the IBKR bar export"
            )
        if not start <= action.ex_date <= date.fromisoformat(END_DATE):
            raise PacketError(
                f"{path}: action {action.ex_date} is outside {symbol}'s captured window"
            )
        identity = (
            action.kind.value,
            action.ex_date.isoformat(),
            action.value,
            action.source,
            action.note,
        )
        if identity in seen:
            raise PacketError(f"{path}: duplicate corporate action {identity!r}")
        seen.add(identity)
        actions.append(action)
    return tuple(actions)


def ingest_actions(history_root: Path, input_root: Path) -> None:
    manifest = _manifest(history_root)
    bars = _validate_bars(history_root, manifest)
    supplied = {path.stem for path in input_root.glob("*.json")}
    if supplied != SYMBOL_SET:
        raise PacketError(
            "action-input identity differs from the six-symbol QQQ release: "
            f"missing={sorted(SYMBOL_SET - supplied)}, extra={sorted(supplied - SYMBOL_SET)}"
        )
    captured_at = datetime.now(UTC).isoformat()
    for symbol in SYMBOLS:
        action_path = history_root / "corporate_actions" / f"{symbol}.json"
        actions = _parse_action_file(
            input_root / f"{symbol}.json",
            symbol=symbol,
            start=date.fromisoformat(str(bars[symbol]["start"])),
        )
        expected_payload = (
            json.dumps(
                [
                    action.to_mapping()
                    for action in sorted(
                        actions,
                        key=lambda action: (action.ex_date, action.kind.value, action.value),
                    )
                ],
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if action_path.exists():
            if action_path.read_text(encoding="utf-8") != expected_payload:
                raise PacketError(
                    f"{symbol}: refusing an unlogged corporate-action overwrite; "
                    "a correction needs a separately reviewed supersede"
                )
            manifest_symbols = _mapping(manifest["symbols"], context="MANIFEST.json symbols")
            manifest_entry = _mapping(manifest_symbols[symbol], context=f"manifest {symbol}").get(
                "corporate_actions"
            )
            if (
                isinstance(manifest_entry, dict)
                and manifest_entry.get("sha256") == _sha256(action_path)
                and manifest_entry.get("count") == len(actions)
            ):
                continue
        write_actions(history_root, symbol, actions, captured_at=captured_at)


def _validated_actions(history_root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    symbols = _mapping(manifest["symbols"], context="MANIFEST.json symbols")
    validated: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        entry = _mapping(symbols[symbol], context=f"manifest {symbol}")
        actions = _mapping(
            entry.get("corporate_actions"), context=f"manifest {symbol}.corporate_actions"
        )
        count = actions.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PacketError(f"{symbol}: corporate-action count must be non-negative")
        action_path = history_root / "corporate_actions" / f"{symbol}.json"
        if _sha256(action_path) != actions.get("sha256"):
            raise PacketError(f"{symbol}: action bytes do not match the manifest sha256")
        parsed_actions = _parse_action_file(
            action_path,
            symbol=symbol,
            start=date.fromisoformat(
                str(_mapping(entry["bars"], context=f"manifest {symbol}.bars")["start"])
            ),
        )
        if count != len(parsed_actions):
            raise PacketError(
                f"{symbol}: manifest count {count} does not match "
                f"{len(parsed_actions)} parsed corporate actions"
            )
        validated[symbol] = dict(actions)
    return validated


def finalize_receipt(history_root: Path, capture_log: Path, output: Path) -> None:
    outcomes = _validate_capture_log(capture_log)
    manifest = _manifest(history_root)
    bars = _validate_bars(history_root, manifest)
    actions = _validated_actions(history_root, manifest)
    document = {
        "schema_version": RECEIPT_SCHEMA,
        "dataset_id": DATASET_ID,
        "catalog_id": CATALOG_ID,
        "source_id": SOURCE_ID,
        "capture": {
            "symbols": list(SYMBOLS),
            "end_date": END_DATE,
            "duration_days": DURATION_DAYS,
            "bar_interval": BAR_INTERVAL,
            "exchange": EXCHANGE,
            "outcomes": {symbol: outcomes[symbol] for symbol in SYMBOLS},
            "capture_log_sha256": _sha256(capture_log),
        },
        "manifest_sha256": _sha256(history_root / "MANIFEST.json"),
        "files": {
            symbol: {
                "bars_sha256": bars[symbol]["sha256"],
                "actions_sha256": actions[symbol]["sha256"],
                "bars_rows": bars[symbol]["rows"],
                "actions_count": actions[symbol]["count"],
                "start": bars[symbol]["start"],
                "end": bars[symbol]["end"],
            }
            for symbol in SYMBOLS
        },
        "account_identifiers_included": False,
        "credentials_included": False,
    }
    _write_immutable(output, _canonical_bytes(document))


def validate_capture(history_root: Path, capture_log: Path) -> None:
    """Authenticate a completed bars capture before owner action work begins."""

    _validate_capture_log(capture_log)
    _validate_bars(history_root, _manifest(history_root))


def _expected_holdout_map(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    symbols = _mapping(manifest["symbols"], context="MANIFEST.json symbols")
    spans: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        bars = _mapping(
            _mapping(symbols[symbol], context=f"manifest {symbol}")["bars"],
            context=f"manifest {symbol}.bars",
        )
        start = str(bars["start"])
        if symbol == "QQQ":
            spans.extend(
                [
                    {
                        "symbol": symbol,
                        "name": "seen-pre-2022",
                        "start": start,
                        "end": "2021-12-31",
                        "status": "seen",
                        "reason": "ordinary research and design window",
                    },
                    {
                        "symbol": symbol,
                        "name": "burned-prior-qqq",
                        "start": SEEN_SPLIT.isoformat(),
                        "end": BURNED_END.isoformat(),
                        "status": "burned",
                        "reason": (
                            "QQQ 2022-01-01 through 2024-01-10 was consumed by prior "
                            "Chronos research and can never be represented as clean"
                        ),
                    },
                ]
            )
        else:
            spans.append(
                {
                    "symbol": symbol,
                    "name": "seen-prior",
                    "start": start,
                    "end": BURNED_END.isoformat(),
                    "status": "seen",
                    "reason": "conservatively classified as prior/ordinary research data",
                }
            )
        spans.append(
            {
                "symbol": symbol,
                "name": "final-clean",
                "start": CLEAN_START.isoformat(),
                "end": END_DATE,
                "status": "clean",
                "reason": "owner-reserved untouched final test; ordinary reads are forbidden",
            }
        )
    return spans


def _expected_declaration(
    history_root: Path,
    source_receipt: Path,
    *,
    attestation_source_id: str,
    attestation_count: int,
) -> dict[str, Any]:
    attestation_source_id = attestation_source_id.strip()
    if not attestation_source_id:
        raise PacketError("the independent attestation source id must be non-empty")
    if _uses_ibkr_source_identity(attestation_source_id):
        raise PacketError(
            "the attestation source must be independent of the IBKR/TWS source family"
        )
    if attestation_count < MINIMUM_ATTESTED_ACTIONS:
        raise PacketError(
            f"the reviewed packet requires at least {MINIMUM_ATTESTED_ACTIONS} sampled actions"
        )
    receipt = _mapping(_load_json(source_receipt), context="source receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise PacketError("source receipt schema does not match the QQQ packet")
    for key, expected in (
        ("dataset_id", DATASET_ID),
        ("catalog_id", CATALOG_ID),
        ("source_id", SOURCE_ID),
    ):
        if receipt.get(key) != expected:
            raise PacketError(f"source receipt {key} does not match the reviewed packet")
    manifest = _manifest(history_root)
    _validate_bars(history_root, manifest)
    actions = _validated_actions(history_root, manifest)
    ingested_action_count = sum(int(actions[symbol]["count"]) for symbol in SYMBOLS)
    if ingested_action_count == 0:
        raise PacketError(
            "the all-empty six-symbol corporate-action panel cannot use this frozen "
            "QQQ identity; a legitimately empty panel requires a separately reviewed "
            "identity with an explicit justification"
        )
    if attestation_count > ingested_action_count:
        raise PacketError(
            f"cannot attest to {attestation_count} sampled actions when only "
            f"{ingested_action_count} were ingested"
        )
    if receipt.get("manifest_sha256") != _sha256(history_root / "MANIFEST.json"):
        raise PacketError("source receipt no longer matches MANIFEST.json")
    windows = []
    symbols = _mapping(manifest["symbols"], context="MANIFEST.json symbols")
    for symbol in SYMBOLS:
        bars = _mapping(
            _mapping(symbols[symbol], context=f"manifest {symbol}")["bars"],
            context=f"manifest {symbol}.bars",
        )
        windows.append({"symbol": symbol, "start": bars["start"], "end": END_DATE})
    return {
        "dataset_id": DATASET_ID,
        "interval": BAR_INTERVAL,
        "catalog_id": CATALOG_ID,
        "source_id": SOURCE_ID,
        "source_receipt_sha256": _sha256(source_receipt),
        "attestation": {
            "kind": "sampled_actions",
            "source_id": attestation_source_id,
            "sampled_action_count": attestation_count,
            "symbols": list(SYMBOLS),
            "note": (
                f"owner independently reconciled {attestation_count} actions across the "
                "six-symbol panel against a source separate from both IBKR bars and the "
                "primary corporate-action streams"
            ),
        },
        "windows": windows,
        "holdout_map": _expected_holdout_map(manifest),
        "classified_moves": [],
    }


def build_declaration(
    history_root: Path,
    source_receipt: Path,
    output: Path,
    *,
    attestation_source_id: str,
    attestation_count: int,
) -> None:
    expected = _expected_declaration(
        history_root,
        source_receipt,
        attestation_source_id=attestation_source_id,
        attestation_count=attestation_count,
    )
    if output.exists():
        existing = _mapping(_load_json(output), context="certification declaration")
        classified_moves = existing.get("classified_moves", [])
        if not isinstance(classified_moves, list):
            raise PacketError("classified_moves must remain a JSON array")
        expected["classified_moves"] = classified_moves
        if existing != expected:
            raise PacketError(
                "the existing declaration changed a frozen packet field; only "
                "classified_moves may be added after a failed certification"
            )
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(json.dumps(expected, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def verify_release(
    release_root: Path, source_receipt: Path, declaration_path: Path
) -> tuple[str, str]:
    catalog_path = release_root / "catalog.json"
    release_path = release_root / "release.json"
    catalog = _mapping(_load_json(catalog_path), context="catalog.json")
    release = _mapping(_load_json(release_path), context="release.json")
    declaration = _mapping(_load_json(declaration_path), context="certification declaration")
    receipt_sha = _sha256(source_receipt)
    if catalog.get("catalog_id") != CATALOG_ID:
        raise PacketError("catalog id does not match the reviewed packet")
    for key, expected in (
        ("dataset_id", DATASET_ID),
        ("catalog_id", CATALOG_ID),
        ("source_id", SOURCE_ID),
        ("source_receipt_sha256", receipt_sha),
        ("interval", BAR_INTERVAL),
    ):
        if release.get(key) != expected:
            raise PacketError(f"release {key} does not match the reviewed packet")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PacketError("catalog has no partitions")
    classifications: dict[str, set[str]] = {symbol: set() for symbol in SYMBOLS}
    for index, raw in enumerate(entries):
        entry = _mapping(raw, context=f"catalog entry {index}")
        for key, expected in (
            ("dataset_id", DATASET_ID),
            ("source_id", SOURCE_ID),
        ):
            if entry.get(key) != expected:
                raise PacketError(f"catalog entry {index} {key} does not match the packet")
        partition = entry.get("partition")
        if not isinstance(partition, str) or ":" not in partition:
            raise PacketError(f"catalog entry {index} has an invalid partition identity")
        symbol = partition.split(":", maxsplit=1)[0]
        if symbol not in SYMBOL_SET:
            raise PacketError(f"catalog partition {partition} has an unexpected symbol")
        classification = entry.get("classification")
        if classification not in {"ordinary", "holdout"}:
            raise PacketError(f"catalog partition {partition} has an invalid classification")
        classifications[symbol].add(classification)
        target = release_root / str(entry.get("path", ""))
        if _sha256(target) != entry.get("sha256"):
            raise PacketError(f"catalog partition {partition} does not match its sha256")
        if entry.get("source_receipt_sha256") != receipt_sha:
            raise PacketError(f"catalog partition {partition} lost the source receipt binding")
    if any(values != {"ordinary", "holdout"} for values in classifications.values()):
        raise PacketError("catalog does not contain ordinary and clean identities for all symbols")
    catalog_sha = _sha256(catalog_path)
    if release.get("catalog_manifest_sha256") != catalog_sha:
        raise PacketError("release.json does not authenticate catalog.json")
    declared_map = declaration.get("holdout_map")
    released_map = release.get("holdout_map")
    if not isinstance(declared_map, list) or not isinstance(released_map, list):
        raise PacketError("declaration and release must both carry a holdout map")

    def span_key(span: dict[str, Any]) -> tuple[str, str]:
        return str(span.get("symbol", "")), str(span.get("start", ""))

    if sorted(declared_map, key=span_key) != sorted(released_map, key=span_key):
        raise PacketError("release holdout map differs from the owner-approved declaration")
    return catalog_sha, _sha256(release_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest-actions")
    ingest.add_argument("--history-root", type=Path, required=True)
    ingest.add_argument("--input-root", type=Path, required=True)

    validate = subparsers.add_parser("validate-capture")
    validate.add_argument("--history-root", type=Path, required=True)
    validate.add_argument("--capture-log", type=Path, required=True)

    receipt = subparsers.add_parser("finalize-receipt")
    receipt.add_argument("--history-root", type=Path, required=True)
    receipt.add_argument("--capture-log", type=Path, required=True)
    receipt.add_argument("--output", type=Path, required=True)

    declaration = subparsers.add_parser("build-declaration")
    declaration.add_argument("--history-root", type=Path, required=True)
    declaration.add_argument("--source-receipt", type=Path, required=True)
    declaration.add_argument("--output", type=Path, required=True)
    declaration.add_argument("--attestation-source-id", required=True)
    declaration.add_argument("--attestation-count", type=int, default=MINIMUM_ATTESTED_ACTIONS)

    release = subparsers.add_parser("verify-release")
    release.add_argument("--release-root", type=Path, required=True)
    release.add_argument("--source-receipt", type=Path, required=True)
    release.add_argument("--declaration", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ingest-actions":
            ingest_actions(args.history_root, args.input_root)
        elif args.command == "validate-capture":
            validate_capture(args.history_root, args.capture_log)
        elif args.command == "finalize-receipt":
            finalize_receipt(args.history_root, args.capture_log, args.output)
        elif args.command == "build-declaration":
            build_declaration(
                args.history_root,
                args.source_receipt,
                args.output,
                attestation_source_id=args.attestation_source_id,
                attestation_count=args.attestation_count,
            )
        else:
            catalog_sha, release_digest = verify_release(
                args.release_root, args.source_receipt, args.declaration
            )
            print(f"catalog sha256 {catalog_sha}")
            print(f"release digest {release_digest}")
    except (OSError, PacketError, TypeError, ValueError) as error:
        print(f"QQQ packet refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

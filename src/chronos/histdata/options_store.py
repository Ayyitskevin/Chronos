"""File store for captured option-chain snapshots (ADR-0012 §2).

Extends the C1 history tree with one immutable EOD snapshot per underlying per day::

    research/data/history/options/<SYMBOL>/<YYYY-MM-DD>.json

Writes are **append-only and fail-closed**: a re-capture of an existing date raises
unless the caller passes ``allow_correction=True`` (a deliberate, logged supersede,
mirroring the bar store). Each snapshot is SHA-256-stamped in ``MANIFEST.json`` with
its source, capture time, row count, staleness histogram, and applied bounds. No
trading database is opened — the isolation the C1 store establishes is unchanged.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from chronos.histdata.options import OptionChainSnapshot
from chronos.histdata.store import (
    _load_manifest,
    _sha256_file,
    _store_manifest,
    _symbol_entry,
)


class OptionStoreError(RuntimeError):
    """A snapshot could not be written (I/O, or a conflicting re-capture)."""


class OptionStoreConflictError(OptionStoreError):
    """A snapshot already exists for this date and differs (fail-closed)."""


def options_dir(root: Path, symbol: str) -> Path:
    return root / "options" / symbol


def snapshot_path(root: Path, symbol: str, session: date) -> Path:
    return options_dir(root, symbol) / f"{session.isoformat()}.json"


def _render(snapshot: OptionChainSnapshot) -> str:
    return json.dumps(snapshot.to_mapping(), indent=2, sort_keys=True) + "\n"


def read_snapshot(root: Path, symbol: str, session: date) -> OptionChainSnapshot | None:
    path = snapshot_path(root, symbol, session)
    if not path.exists():
        return None
    return OptionChainSnapshot.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def write_snapshot(
    root: Path,
    session: date,
    snapshot: OptionChainSnapshot,
    *,
    allow_correction: bool = False,
) -> Path:
    """Write one EOD snapshot; fail closed on a conflicting re-capture of the date."""

    path = snapshot_path(root, snapshot.underlying, session)
    rendered = _render(snapshot)
    if path.exists() and path.read_text(encoding="utf-8") != rendered and not allow_correction:
        raise OptionStoreConflictError(
            f"{snapshot.underlying} {session.isoformat()}: a different snapshot already "
            "exists; pass allow_correction to supersede it deliberately"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    _update_options_manifest(root, session, snapshot, path)
    return path


def _update_options_manifest(
    root: Path, session: date, snapshot: OptionChainSnapshot, path: Path
) -> None:
    manifest = _load_manifest(root)
    entry = _symbol_entry(manifest, snapshot.underlying)
    options = entry.get("options")
    snapshots: dict[str, object] = options.get("snapshots", {}) if isinstance(options, dict) else {}
    snapshots[session.isoformat()] = {
        "sha256": _sha256_file(path),
        "source": snapshot.source,
        "captured_at": snapshot.captured_at,
        "rows": len(snapshot.rows),
        "spot": snapshot.spot,
        "expiry_horizon_days": snapshot.expiry_horizon_days,
        "strike_window_pct": snapshot.strike_window_pct,
        "quality_histogram": snapshot.quality_histogram(),
        "worst_quality": snapshot.worst_quality().value,
        "reason": snapshot.reason,
    }
    entry["options"] = {"snapshots": dict(sorted(snapshots.items()))}
    _store_manifest(root, manifest)

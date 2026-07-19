"""File store for captured option-chain snapshots (ADR-0012 §2).

Extends the C1 history tree with one immutable EOD snapshot per underlying per day::

    research/data/history/options/<SYMBOL>/<YYYY-MM-DD>.json

Writes are **idempotent and fail-closed**: a re-capture whose market data matches the
stored snapshot (only the capture clock differs) is a no-op; a re-capture whose data
*differs* raises unless the caller passes ``allow_correction=True``, which records a
deliberate, **logged** supersede (the prior sha256 + capture time appended to an
append-only ``superseded`` list in the manifest). Each snapshot is SHA-256-stamped in
``MANIFEST.json`` with its source, capture time, row count, staleness histogram, and
applied bounds. No trading database is opened — the C1 isolation is unchanged.
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


def _content_key(snapshot: OptionChainSnapshot) -> dict[str, object]:
    """Data identity of a snapshot, ignoring the capture clock (``captured_at``).

    Two snapshots with identical market data but different capture times share a
    content key, so a re-run with a fresh clock is an idempotent no-op, not a conflict.
    """

    mapping = snapshot.to_mapping()
    mapping.pop("captured_at", None)
    return mapping


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
    is_correction = False
    if path.exists():
        stored = OptionChainSnapshot.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        if _content_key(stored) == _content_key(snapshot):
            # Same market data, only the capture clock moved: idempotent no-op. Leave the
            # file and manifest untouched (no churn, and the supersede trail is preserved).
            return path
        if not allow_correction:
            raise OptionStoreConflictError(
                f"{snapshot.underlying} {session.isoformat()}: a different snapshot already "
                "exists; pass allow_correction to supersede it deliberately"
            )
        is_correction = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(snapshot), encoding="utf-8")
    _update_options_manifest(root, session, snapshot, path, is_correction=is_correction)
    return path


def _update_options_manifest(
    root: Path,
    session: date,
    snapshot: OptionChainSnapshot,
    path: Path,
    *,
    is_correction: bool,
) -> None:
    manifest = _load_manifest(root)
    entry = _symbol_entry(manifest, snapshot.underlying)
    options = entry.get("options")
    snapshots: dict[str, object] = options.get("snapshots", {}) if isinstance(options, dict) else {}
    # Supersedes are an append-only audit trail: carry forward any prior ones and, when
    # this write replaces a genuinely different snapshot, record what it replaced.
    prior = snapshots.get(session.isoformat())
    superseded = list(prior.get("superseded", [])) if isinstance(prior, dict) else []
    if is_correction and isinstance(prior, dict):
        superseded.append(
            {
                "prior_sha256": prior.get("sha256"),
                "prior_captured_at": prior.get("captured_at"),
                "corrected_at": snapshot.captured_at,
            }
        )
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
        "superseded": superseded,
    }
    entry["options"] = {"snapshots": dict(sorted(snapshots.items()))}
    _store_manifest(root, manifest)

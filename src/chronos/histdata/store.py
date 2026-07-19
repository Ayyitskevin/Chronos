"""File-based store for the historical-data plane (ADR-0011 §3).

Lays out ``research/data/history/`` as flat files — **no trading database is ever
opened**, so the data process structurally cannot hold the writer lease::

    research/data/history/
      bars/<SYMBOL>.csv                 # UNADJUSTED as-traded OHLCV (never re-adjusted)
      corporate_actions/<SYMBOL>.json   # the split + cash-dividend event stream
      MANIFEST.json                     # per-symbol provenance (sha256, range, capture)

Writes are **idempotent and fail-closed**: a re-fetch that reproduces the stored
rows is a no-op; a *conflicting* row for an already-stored date aborts, unless the
caller passes ``allow_correction=True`` (which records the supersede in the
manifest). Numeric comparison is canonical (parsed + rounded), so a float-spelling
change across API versions does not false-fail while a real one-tick drift still
does (ADR-0011 §11.13). Every series passes ``marketdata.quality.validate_series``
before write; a blocking issue aborts.

The bars stay unadjusted; adjusted views are derived at read time (``adjust.py``).
Both the bars file and the actions file are SHA-256-stamped in the manifest so a
research result can pin *both* hashes (ADR-0011 §11.14).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from chronos.histdata.corporate_actions import CorporateAction
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.marketdata.quality import validate_series

_CANON_DECIMALS = 6
_BARS_HEADER = "date,open,high,low,close,volume"


class StoreError(RuntimeError):
    """A store write could not be completed (quality block, or I/O)."""


class StoreConflictError(StoreError):
    """A re-fetch produced a different row for an already-stored date (fail-closed)."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    symbol: str
    rows_written: int
    rows_added: int
    corrections: tuple[str, ...]


def bars_path(root: Path, symbol: str) -> Path:
    return root / "bars" / f"{symbol}.csv"


def actions_path(root: Path, symbol: str) -> Path:
    return root / "corporate_actions" / f"{symbol}.json"


def manifest_path(root: Path) -> Path:
    return root / "MANIFEST.json"


def _canon(value: float) -> float:
    return round(float(value), _CANON_DECIMALS)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bars(
    root: Path, symbol: str, *, source: str = "ibkr", exchange: str = "SMART"
) -> BarSeries:
    path = bars_path(root, symbol)
    if not path.exists():
        return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=())
    return load_daily_csv(path, symbol, source, exchange).series


def write_bars(
    root: Path,
    series: BarSeries,
    *,
    source: str = "ibkr",
    exchange: str = "SMART",
    captured_at: str,
    allow_correction: bool = False,
) -> WriteResult:
    """Idempotently merge ``series`` into the store; fail closed on conflict."""

    _require_clean(series)
    path = bars_path(root, series.symbol)
    if path.exists():
        existing = load_daily_csv(path, series.symbol, source, exchange).series
        merged, added, corrections = _merge(existing, series, allow_correction=allow_correction)
    else:
        merged, added, corrections = series, len(series), ()

    _require_clean(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_csv(merged), encoding="utf-8")
    _update_manifest(
        root,
        series.symbol,
        source=source,
        exchange=exchange,
        series=merged,
        captured_at=captured_at,
        corrections=corrections,
    )
    return WriteResult(series.symbol, len(merged), added, corrections)


def _require_clean(series: BarSeries) -> None:
    report = validate_series(series)
    if report.blocking:
        blocking = [issue for issue in report.issues if issue.blocking]
        detail = "; ".join(f"{issue.kind.value}: {issue.detail}" for issue in blocking)
        raise StoreError(f"{series.symbol}: blocking data-quality issue(s) — {detail}")


def _merge(
    existing: BarSeries, incoming: BarSeries, *, allow_correction: bool
) -> tuple[BarSeries, int, tuple[str, ...]]:
    by_date: dict[date, Bar] = {bar.session_date: bar for bar in existing.bars}
    added = 0
    corrections: list[str] = []
    for bar in incoming.bars:
        prior = by_date.get(bar.session_date)
        if prior is None:
            by_date[bar.session_date] = bar
            added += 1
        elif _row_changed(prior, bar):
            if not allow_correction:
                raise StoreConflictError(
                    f"{incoming.symbol} {bar.session_date.isoformat()}: re-fetch differs from "
                    "the stored row; pass allow_correction to supersede it deliberately"
                )
            by_date[bar.session_date] = bar
            corrections.append(bar.session_date.isoformat())
    ordered = tuple(by_date[key] for key in sorted(by_date))
    merged = BarSeries(symbol=incoming.symbol, interval=incoming.interval, bars=ordered)
    return merged, added, tuple(corrections)


def _row_changed(a: Bar, b: Bar) -> bool:
    return (
        _canon(a.open) != _canon(b.open)
        or _canon(a.high) != _canon(b.high)
        or _canon(a.low) != _canon(b.low)
        or _canon(a.close) != _canon(b.close)
        or _canon(a.volume) != _canon(b.volume)
    )


def _render_csv(series: BarSeries) -> str:
    lines = [_BARS_HEADER]
    for bar in series.bars:
        lines.append(
            f"{bar.session_date.isoformat()},{bar.open},{bar.high},"
            f"{bar.low},{bar.close},{bar.volume}"
        )
    return "\n".join(lines) + "\n"


def read_actions(root: Path, symbol: str) -> tuple[CorporateAction, ...]:
    path = actions_path(root, symbol)
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(CorporateAction.from_mapping(item) for item in raw)


def write_actions(
    root: Path, symbol: str, actions: tuple[CorporateAction, ...], *, captured_at: str
) -> None:
    """Write the corporate-action stream (sorted by ex-date) and stamp the manifest."""

    ordered = sorted(actions, key=lambda a: (a.ex_date, a.kind.value, a.value))
    path = actions_path(root, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [action.to_mapping() for action in ordered]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _update_manifest_actions(root, symbol, path, captured_at=captured_at, count=len(ordered))


def _load_manifest(root: Path) -> dict[str, object]:
    path = manifest_path(root)
    if path.exists():
        data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return data
    return {"schema_version": 1, "symbols": {}}


def _store_manifest(root: Path, manifest: dict[str, object]) -> None:
    manifest_path(root).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _symbol_entry(manifest: dict[str, object], symbol: str) -> dict[str, object]:
    symbols = manifest.setdefault("symbols", {})
    assert isinstance(symbols, dict)
    entry = symbols.setdefault(symbol, {})
    assert isinstance(entry, dict)
    return entry


def _update_manifest(
    root: Path,
    symbol: str,
    *,
    source: str,
    exchange: str,
    series: BarSeries,
    captured_at: str,
    corrections: tuple[str, ...],
) -> None:
    manifest = _load_manifest(root)
    entry = _symbol_entry(manifest, symbol)
    entry["bars"] = {
        "source": source,
        "exchange": exchange,
        "sha256": _sha256_file(bars_path(root, symbol)),
        "rows": len(series),
        "start": series.bars[0].session_date.isoformat() if series.bars else None,
        "end": series.bars[-1].session_date.isoformat() if series.bars else None,
        "adjusted": False,
        "captured_at": captured_at,
        "corrections": list(corrections),
    }
    _store_manifest(root, manifest)


def _update_manifest_actions(
    root: Path, symbol: str, path: Path, *, captured_at: str, count: int
) -> None:
    manifest = _load_manifest(root)
    entry = _symbol_entry(manifest, symbol)
    entry["corporate_actions"] = {
        "sha256": _sha256_file(path),
        "count": count,
        "captured_at": captured_at,
    }
    _store_manifest(root, manifest)

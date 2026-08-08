"""CSV-backed historical bar provider with provenance.

The research corpus stores raw CSV files under ``research/data/raw`` together
with ``MANIFEST.json`` describing source, retrieval method, and hashes. This
provider is deliberately strict: unknown columns are tolerated, missing
required columns or unparseable rows fail the load rather than being skipped.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from chronos.marketdata.bars import Bar, BarInterval, BarSeries

_REQUIRED = ("date", "open", "high", "low", "close", "volume")

# US equity regular session close in UTC is 21:00 (EDT) or 22:00 (EST); using a
# fixed 21:00 UTC close-of-day convention keeps daily timestamps deterministic
# and strictly increasing. The session date carries the trading-day semantics.
_DAILY_CLOSE_UTC = time(hour=21, minute=0)


@dataclass(frozen=True, slots=True)
class LoadedSeries:
    series: BarSeries
    path: Path
    sha256: str
    has_adjusted_close: bool


def _norm_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def load_daily_csv(path: Path, symbol: str, source: str, exchange: str = "ARCA") -> LoadedSeries:
    """Load one symbol's daily bars from a CSV file, fail-closed."""

    raw = path.read_bytes()
    return load_daily_csv_bytes(
        raw,
        path=path,
        symbol=symbol,
        source=source,
        exchange=exchange,
    )


def load_daily_csv_bytes(
    raw: bytes,
    *,
    path: Path,
    symbol: str,
    source: str,
    exchange: str = "ARCA",
) -> LoadedSeries:
    """Parse one immutable CSV byte snapshot using ``path`` only for diagnostics."""

    if not isinstance(raw, bytes):
        raise TypeError("raw CSV content must be bytes")
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ValueError(f"{path}: CSV has no header row")
    fields = {_norm_header(name): name for name in reader.fieldnames}
    missing = [column for column in _REQUIRED if column not in fields]
    if missing:
        raise ValueError(f"{path}: missing required columns {missing}")
    adj_names = ("adj_close", "adjusted_close", "adjclose", "adj close")
    adj_key = next((fields[k] for k in adj_names if k in fields), None)

    bars: list[Bar] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            session = date.fromisoformat(row[fields["date"]].strip()[:10])
            open_ = float(row[fields["open"]])
            high = float(row[fields["high"]])
            low = float(row[fields["low"]])
            close = float(row[fields["close"]])
            volume = float(row[fields["volume"]])
            adjusted = float(row[adj_key]) if adj_key and row[adj_key].strip() else None
        except (KeyError, ValueError) as error:
            raise ValueError(f"{path}:{line_number}: unparseable row: {error}") from error
        bars.append(
            Bar(
                symbol=symbol,
                source=source,
                exchange=exchange,
                interval=BarInterval.DAY_1,
                session_date=session,
                timestamp_utc=datetime.combine(session, _DAILY_CLOSE_UTC, tzinfo=UTC),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                adjusted_close=adjusted,
            )
        )

    bars.sort(key=lambda bar: bar.timestamp_utc)
    series = BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=tuple(bars))
    return LoadedSeries(
        series=series, path=path, sha256=digest, has_adjusted_close=adj_key is not None
    )

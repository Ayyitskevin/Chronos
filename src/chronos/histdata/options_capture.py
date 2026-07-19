"""Option-snapshot capture coordinator (ADR-0012 §3).

Selects a **bounded** window of the chain — expirations within a horizon, strikes
within a percentage band of spot, both rights — fetches one quote per contract, and
writes an immutable EOD snapshot with the applied bounds and a staleness histogram
recorded. Everything outside the window is *absent by policy*, and an empty capture
(no expiration in the horizon, or spot unavailable) is written **with a recorded
reason**, never a silent no-op.

The capture clock (``captured_at``) is passed in — this module generates no
timestamps, so the selection is a pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from chronos.histdata.options import OptionChainSnapshot, OptionQuoteRow, OptionRight
from chronos.histdata.options_client import ChainParams, ContractKey, OptionSnapshotClient
from chronos.histdata.options_store import write_snapshot


def select_contracts(
    chain: ChainParams, session: date, *, expiry_horizon_days: int, strike_window_pct: float
) -> tuple[tuple[ContractKey, ...], str]:
    """Return ``(contracts, reason)``; ``reason`` non-empty means an empty capture."""

    horizon_end = session + timedelta(days=expiry_horizon_days)
    expirations = tuple(e for e in sorted(chain.expirations) if session <= e <= horizon_end)
    if not expirations:
        return (), f"no expiration within {expiry_horizon_days}d of {session.isoformat()}"
    if chain.spot is None:
        return (), "spot unavailable; cannot center the strike window"
    low = chain.spot * (1.0 - strike_window_pct)
    high = chain.spot * (1.0 + strike_window_pct)
    strikes = tuple(s for s in sorted(chain.strikes) if low <= s <= high)
    if not strikes:
        return (), f"no strike within +/-{strike_window_pct:.0%} of spot {chain.spot}"
    contracts = tuple(
        ContractKey(expiration=exp, strike=strike, right=right)
        for exp in expirations
        for strike in strikes
        for right in (OptionRight.CALL, OptionRight.PUT)
    )
    return contracts, ""


def capture_snapshot(
    root: Path,
    client: OptionSnapshotClient,
    underlying: str,
    *,
    session: date,
    captured_at: str,
    source: str,
    expiry_horizon_days: int,
    strike_window_pct: float,
    allow_correction: bool = False,
) -> OptionChainSnapshot:
    """Capture and persist one EOD option snapshot for ``underlying``."""

    chain = client.fetch_chain(underlying)
    contracts, reason = select_contracts(
        chain,
        session,
        expiry_horizon_days=expiry_horizon_days,
        strike_window_pct=strike_window_pct,
    )
    rows = client.fetch_quotes(underlying, contracts) if contracts else ()
    snapshot = OptionChainSnapshot(
        underlying=underlying,
        captured_at=captured_at,
        source=source,
        expiry_horizon_days=expiry_horizon_days,
        strike_window_pct=strike_window_pct,
        spot=chain.spot,
        reason=reason,
        rows=_sorted_rows(rows),
    )
    write_snapshot(root, session, snapshot, allow_correction=allow_correction)
    return snapshot


def _sorted_rows(rows: tuple[OptionQuoteRow, ...]) -> tuple[OptionQuoteRow, ...]:
    return tuple(sorted(rows, key=lambda r: (r.expiration, r.strike, r.right.value)))


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    underlying: str
    snapshot: OptionChainSnapshot | None
    error: str | None


def capture_symbols(
    root: Path,
    client: OptionSnapshotClient,
    underlyings: Iterable[str],
    *,
    session: date,
    captured_at: str,
    source: str,
    expiry_horizon_days: int,
    strike_window_pct: float,
    allow_correction: bool = False,
) -> tuple[CaptureOutcome, ...]:
    """Capture each underlying; a failure isolates to that underlying's outcome."""

    outcomes: list[CaptureOutcome] = []
    for underlying in underlyings:
        try:
            snapshot = capture_snapshot(
                root,
                client,
                underlying,
                session=session,
                captured_at=captured_at,
                source=source,
                expiry_horizon_days=expiry_horizon_days,
                strike_window_pct=strike_window_pct,
                allow_correction=allow_correction,
            )
            outcomes.append(CaptureOutcome(underlying, snapshot, None))
        except Exception as error:
            outcomes.append(CaptureOutcome(underlying, None, f"{type(error).__name__}: {error}"))
    return tuple(outcomes)

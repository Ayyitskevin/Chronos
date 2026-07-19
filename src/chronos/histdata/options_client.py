"""Option-snapshot client port (ADR-0012 §4).

The boundary the capture coordinator fetches option data through. Two
implementations: ``FakeOptionSnapshotClient`` (tests, deterministic, contacts
nothing) drives every CI/dev path, and ``OfficialIBKROptionClient``
(``official_options_client.py``, lazy ``ibapi``) is the owner-run gateway path —
present but unexercised in this environment (invariant 8).

The port speaks in histdata-local types (``ChainParams``, ``OptionQuoteRow``); no
``chronos.domain`` option model crosses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from chronos.histdata.options import OptionQuoteRow, OptionRight


class OptionSnapshotError(RuntimeError):
    """An option fetch could not be satisfied (no data, gateway error, bad request)."""


@dataclass(frozen=True, slots=True)
class ChainParams:
    """The available expirations and strikes for one underlying, plus its spot."""

    underlying: str
    expirations: tuple[date, ...]
    strikes: tuple[float, ...]
    spot: float | None = None


@dataclass(frozen=True, slots=True)
class ContractKey:
    """One (expiration, strike, right) the coordinator wants a quote for."""

    expiration: date
    strike: float
    right: OptionRight


@runtime_checkable
class OptionSnapshotClient(Protocol):
    """A source of option-chain parameters and per-contract quotes for one underlying."""

    def connect(self) -> None:
        """Open the underlying connection (idempotent)."""

    def disconnect(self) -> None:
        """Close the underlying connection (idempotent)."""

    def fetch_chain(self, underlying: str) -> ChainParams:
        """Return the underlying's available expirations/strikes (+ spot if known)."""

    def fetch_quotes(
        self, underlying: str, contracts: tuple[ContractKey, ...]
    ) -> tuple[OptionQuoteRow, ...]:
        """Return one quote row per requested contract (staleness labeled per row)."""

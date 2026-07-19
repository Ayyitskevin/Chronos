"""Deterministic fake option-snapshot client for tests (ADR-0012 §4).

Records calls and returns canned chain params + per-contract rows; contacts no
gateway. Every CI/dev path uses this — the ``FakeHistoricalDataClient`` pattern
applied to option capture. No real ``ibapi`` and no network, ever.
"""

from __future__ import annotations

from collections.abc import Mapping

from chronos.histdata.options import OptionQuoteRow, SnapshotQuality
from chronos.histdata.options_client import ChainParams, ContractKey, OptionSnapshotError


class FakeOptionSnapshotClient:
    """An ``OptionSnapshotClient`` backed by canned chains and a quote factory.

    ``quote_quality`` labels every canned row; set it to ``DELAYED`` to model the
    $0-tier feed. ``rows_by_underlying`` may override the default quote for exact-row
    assertions; otherwise a deterministic quote is synthesized per contract.
    """

    def __init__(
        self,
        chains: Mapping[str, ChainParams] | None = None,
        *,
        quote_quality: SnapshotQuality = SnapshotQuality.DELAYED,
    ) -> None:
        self._chains: dict[str, ChainParams] = dict(chains or {})
        self._quality = quote_quality
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.chain_calls: list[str] = []
        self.quote_calls: list[tuple[str, int]] = []

    def set_chain(self, underlying: str, chain: ChainParams) -> None:
        self._chains[underlying] = chain

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def fetch_chain(self, underlying: str) -> ChainParams:
        self.chain_calls.append(underlying)
        if not self.connected:
            raise OptionSnapshotError("fake client not connected")
        if underlying not in self._chains:
            raise OptionSnapshotError(f"no canned chain for {underlying!r}")
        return self._chains[underlying]

    def fetch_quotes(
        self, underlying: str, contracts: tuple[ContractKey, ...]
    ) -> tuple[OptionQuoteRow, ...]:
        self.quote_calls.append((underlying, len(contracts)))
        if not self.connected:
            raise OptionSnapshotError("fake client not connected")
        return tuple(self._quote(key) for key in contracts)

    def _quote(self, key: ContractKey) -> OptionQuoteRow:
        return OptionQuoteRow(
            expiration=key.expiration,
            strike=key.strike,
            right=key.right,
            data_quality=self._quality,
            bid=round(key.strike * 0.01, 4),
            ask=round(key.strike * 0.012, 4),
            last=round(key.strike * 0.011, 4),
            delta=0.5,
            implied_volatility=0.25,
        )

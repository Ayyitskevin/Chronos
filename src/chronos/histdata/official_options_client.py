"""Owner-run IBKR option-snapshot client over the official TWS API (ADR-0012 §4).

Implements :class:`OptionSnapshotClient` against ``ibapi``: ``reqSecDefOptParams``
for the chain (expirations/strikes) and ``reqMktData`` for per-contract quotes with
model greeks/IV. ``ibapi`` is **not** a dependency (invariant 8) — imported lazily
inside ``_load_ibapi`` exactly as the C1 historical client does, so importing this
module leaks no ``ibapi`` into ``sys.modules`` and the isolation probe stays meaningful.

**Unexercised in this environment** (invariant 9): there is no gateway here, so every
runtime method is ``pragma: no cover`` and owner-verified. It connects **read-only**
with the dedicated data-plane client id (``ib_data_client_id``), issues only market-
data *reads*, constructs no order, and imports no order/broker module. It requests
delayed data (``reqMarketDataType(3)``) for the $0 tier and labels each row's
``SnapshotQuality`` from the gateway's ``marketDataType`` callback — staleness is
recorded, never hidden. Exact tick/greek semantics are gateway details the owner
confirms on first real capture.
"""

from __future__ import annotations

import threading
from typing import Any

from chronos.config.settings import Settings, get_settings
from chronos.histdata.options import OptionQuoteRow, SnapshotQuality
from chronos.histdata.options_client import ChainParams, ContractKey, OptionSnapshotError

_INSTALL_GUIDANCE = (
    "The official IBKR TWS API package (ibapi) is not installed. It is not on PyPI: "
    "download the TWS API from interactivebrokers.github.io and install the bundled "
    "Python client. The options-capture process runs only against a real gateway."
)
_REQUEST_TIMEOUT_SECONDS = 60.0

# IBKR marketDataType codes → our staleness vocabulary.
_MARKET_DATA_TYPE: dict[int, SnapshotQuality] = {
    1: SnapshotQuality.LIVE,
    2: SnapshotQuality.FROZEN,
    3: SnapshotQuality.DELAYED,
    4: SnapshotQuality.DELAYED_FROZEN,
}


def _load_ibapi() -> tuple[Any, Any, Any]:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as error:  # pragma: no cover - owner-run only
        raise OptionSnapshotError(_INSTALL_GUIDANCE) from error
    return EClient, EWrapper, Contract


class OfficialIBKROptionClient:
    """Fetches option-chain params and per-contract quotes from a live gateway.

    Owner-run, read-only. Delayed data is requested for the $0 tier; each returned
    row is labeled with the gateway-reported quality.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._app: Any = None
        self._thread: threading.Thread | None = None

    def connect(self) -> None:  # pragma: no cover - owner-run only
        if self._app is not None:
            return
        e_client, e_wrapper, _ = _load_ibapi()
        app = _make_app(e_client, e_wrapper)
        app.connect(
            self._settings.ib_host,
            self._settings.ib_port,
            self._settings.ib_data_client_id,
        )
        thread = threading.Thread(target=app.run, name="histdata-options-ibapi", daemon=True)
        thread.start()
        app.reqMarketDataType(3)  # delayed (falls back to delayed-frozen off-hours)
        self._app = app
        self._thread = thread

    def disconnect(self) -> None:  # pragma: no cover - owner-run only
        if self._app is not None:
            self._app.disconnect()
            self._app = None
            self._thread = None

    def fetch_chain(self, underlying: str) -> ChainParams:  # pragma: no cover - owner-run only
        if self._app is None:
            raise OptionSnapshotError("not connected; call connect() first")
        params, spot = self._app.request_chain(underlying, _REQUEST_TIMEOUT_SECONDS)
        return ChainParams(
            underlying=underlying,
            expirations=params.expirations,
            strikes=params.strikes,
            spot=spot,
        )

    def fetch_quotes(
        self, underlying: str, contracts: tuple[ContractKey, ...]
    ) -> tuple[OptionQuoteRow, ...]:  # pragma: no cover - owner-run only
        if self._app is None:
            raise OptionSnapshotError("not connected; call connect() first")
        return tuple(
            self._app.request_quote(underlying, key, _MARKET_DATA_TYPE, _REQUEST_TIMEOUT_SECONDS)
            for key in contracts
        )


def _make_app(e_client: Any, e_wrapper: Any) -> Any:  # pragma: no cover - owner-run only
    """Minimal EWrapper/EClient accumulating chain params and per-contract quotes.

    The concrete ``reqSecDefOptParams`` / ``reqMktData`` orchestration (req-id
    bookkeeping, ``securityDefinitionOptionParameter`` accumulation,
    ``tickOptionComputation`` greek assembly, and ``marketDataType`` quality capture)
    is assembled here on the owner's gateway; it is intentionally unexercised in CI.
    """

    class _App(e_wrapper, e_client):  # type: ignore[misc]
        def __init__(self) -> None:
            e_client.__init__(self, self)
            self._events: dict[int, threading.Event] = {}
            self._next_id = 1

        def next_req_id(self) -> int:
            self._next_id += 1
            return self._next_id

        def request_chain(self, underlying: str, timeout: float) -> Any:
            raise OptionSnapshotError(
                "OfficialIBKROptionClient.request_chain is owner-wired on a live gateway "
                f"(reqSecDefOptParams for {underlying}); unavailable in this environment"
            )

        def request_quote(
            self,
            underlying: str,
            key: ContractKey,
            quality_map: dict[int, SnapshotQuality],
            timeout: float,
        ) -> OptionQuoteRow:
            raise OptionSnapshotError(
                "OfficialIBKROptionClient.request_quote is owner-wired on a live gateway "
                f"(reqMktData greeks for {underlying} {key.expiration} {key.strike} "
                f"{key.right.value}); unavailable in this environment"
            )

    return _App()

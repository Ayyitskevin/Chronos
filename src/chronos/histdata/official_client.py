"""Owner-run IBKR historical client over the official TWS API (ADR-0011 §2).

Implements :class:`HistoricalDataClient` against ``ibapi`` via ``reqHistoricalData``.
``ibapi`` is **not** a dependency (invariant 8) — it is imported lazily inside
``_load_ibapi`` exactly as ``broker/official_ibkr.py`` does, so importing this module
leaks no ``ibapi`` into ``sys.modules`` and the isolation probe stays meaningful.

**Unexercised in this environment.** There is no gateway here, so this path is not
covered by CI and is owner-verified (invariant 9). It connects **read-only** with the
dedicated data-plane client id (``ib_data_client_id``), never the trading id, and
issues only historical *reads* — it constructs no order and imports no order/broker
module. Volume semantics and exact bar-date formatting are gateway details the owner
confirms on first real backfill.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chronos.config.settings import Settings, get_settings
from chronos.histdata.client import HistoricalDataError
from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.session_calendar import CalendarCoverageError, SessionCalendar

_INSTALL_GUIDANCE = (
    "The official IBKR TWS API package (ibapi) is not installed. It is not on PyPI: "
    "download the TWS API from interactivebrokers.github.io and install the bundled "
    "Python client. The historical-data process runs only against a real gateway."
)
_DAILY_CLOSE_UTC = (21, 0)  # matches marketdata.csv_provider's synthetic daily close
_REQUEST_TIMEOUT_SECONDS = 60.0
_EASTERN = ZoneInfo("America/New_York")
_HOURLY_SPAN = timedelta(hours=1)

# The hourly parser needs one fact ingestion cannot derive from the response: the
# session's official close, which caps the final partial bar (a 15:30 start closes
# at 16:00, and at 13:00 on a half-day — adjacency in the response cannot tell you
# that). That fact lives in exactly one place, chronos.research.session_calendar.
# Layering note: research already imports histdata (corporate_actions), so this is
# a package-level cycle but not a module-level one — session_calendar imports
# nothing from chronos, and the R-26 guard is untouched: histdata is the read-only
# data plane, not an authority package, and may never become one.
_calendar = SessionCalendar()


def _load_ibapi() -> tuple[Any, Any, Any]:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as error:  # pragma: no cover - owner-run only
        raise HistoricalDataError(_INSTALL_GUIDANCE) from error
    return EClient, EWrapper, Contract


class OfficialIBKRHistoricalClient:
    """Fetches unadjusted daily bars from a live gateway. Owner-run; read-only."""

    def __init__(self, settings: Settings | None = None, *, exchange: str = "SMART") -> None:
        self._settings = settings or get_settings()
        self._exchange = exchange
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
        thread = threading.Thread(target=app.run, name="histdata-ibapi", daemon=True)
        thread.start()
        self._app = app
        self._thread = thread

    def disconnect(self) -> None:  # pragma: no cover - owner-run only
        if self._app is not None:
            self._app.disconnect()
            self._app = None
            self._thread = None

    def fetch_daily_bars(
        self, symbol: str, *, end_date: date, duration_days: int
    ) -> BarSeries:  # pragma: no cover - owner-run only
        if self._app is None:
            raise HistoricalDataError("not connected; call connect() first")
        _, _, contract_cls = _load_ibapi()
        contract = _stock_contract(contract_cls, symbol, self._exchange)
        req_id = self._app.next_req_id()
        end = end_date.strftime("%Y%m%d 23:59:59 UTC")
        self._app.begin_request(req_id)
        self._app.reqHistoricalData(
            req_id,
            contract,
            end,
            f"{max(1, duration_days)} D",
            "1 day",
            "TRADES",
            1,  # useRTH
            1,  # formatDate (YYYYMMDD)
            False,  # keepUpToDate
            [],
        )
        rows = self._app.await_request(req_id, _REQUEST_TIMEOUT_SECONDS)
        bars = tuple(_bar_from_row(symbol, self._exchange, row) for row in rows)
        return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)

    def fetch_hourly_bars(
        self, symbol: str, *, end_date: date, duration_days: int
    ) -> BarSeries:  # pragma: no cover - owner-run only
        """One chunk of unadjusted HOUR_1 bars. formatDate=2 and RTH-only, on purpose.

        ``formatDate=2`` returns epoch seconds — unambiguous UTC. The in-repo claim
        that intraday rows under ``formatDate=1`` arrive as epoch seconds
        (``broker/official_ibkr.py``) has never run against a gateway and is
        probably wrong (formatDate=1 intraday is a zone-ambiguous datetime string);
        requesting epoch sidesteps the ambiguity instead of resolving it.
        ``useRTH=1`` matches the certification expectation exactly: the session
        calendar counts 09:30-16:00 bars (ADR-0029 records both decisions).
        """

        if self._app is None:
            raise HistoricalDataError("not connected; call connect() first")
        _, _, contract_cls = _load_ibapi()
        contract = _stock_contract(contract_cls, symbol, self._exchange)
        req_id = self._app.next_req_id()
        end = end_date.strftime("%Y%m%d 23:59:59 UTC")
        self._app.begin_request(req_id)
        self._app.reqHistoricalData(
            req_id,
            contract,
            end,
            f"{max(1, duration_days)} D",
            "1 hour",
            "TRADES",
            1,  # useRTH — regular session only; the certification expectation
            2,  # formatDate — epoch seconds, unambiguous UTC
            False,  # keepUpToDate
            [],
        )
        rows = self._app.await_request(req_id, _REQUEST_TIMEOUT_SECONDS)
        bars = tuple(_hourly_bar_from_row(symbol, self._exchange, row) for row in rows)
        return BarSeries(symbol=symbol, interval=BarInterval.HOUR_1, bars=bars)


def _make_app(e_client: Any, e_wrapper: Any) -> Any:  # pragma: no cover - owner-run only
    """A minimal EWrapper/EClient that accumulates one historical request at a time."""

    class _App(e_wrapper, e_client):  # type: ignore[misc]
        def __init__(self) -> None:
            e_client.__init__(self, self)
            self._rows: dict[int, list[Any]] = {}
            self._done: dict[int, threading.Event] = {}
            self._errors: dict[int, str] = {}
            self._next_id = 1

        def next_req_id(self) -> int:
            self._next_id += 1
            return self._next_id

        def begin_request(self, req_id: int) -> None:
            self._rows[req_id] = []
            self._done[req_id] = threading.Event()

        def historicalData(self, reqId: int, bar: Any) -> None:
            self._rows.setdefault(reqId, []).append(bar)

        def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
            event = self._done.get(reqId)
            if event is not None:
                event.set()

        def error(self, reqId: int, *args: Any) -> None:
            # TWS appends parameters across versions; the message is the last string arg.
            message = next((a for a in reversed(args) if isinstance(a, str)), "")
            if reqId in self._done:
                self._errors[reqId] = message
                self._done[reqId].set()

        def await_request(self, req_id: int, timeout: float) -> list[Any]:
            event = self._done[req_id]
            if not event.wait(timeout):
                raise HistoricalDataError(f"historical request {req_id} timed out")
            if req_id in self._errors:
                raise HistoricalDataError(self._errors[req_id])
            return self._rows.get(req_id, [])

    return _App()


def _stock_contract(contract_cls: Any, symbol: str, exchange: str) -> Any:  # pragma: no cover
    contract = contract_cls()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.currency = "USD"
    contract.exchange = exchange
    return contract


def _bar_from_row(symbol: str, exchange: str, row: Any) -> Bar:  # pragma: no cover - owner-run
    text = str(row.date)  # formatDate=1 daily bars are "YYYYMMDD"
    session_date = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    hour, minute = _DAILY_CLOSE_UTC
    return Bar(
        symbol=symbol,
        source="ibkr",
        exchange=exchange,
        interval=BarInterval.DAY_1,
        session_date=session_date,
        timestamp_utc=datetime(
            session_date.year, session_date.month, session_date.day, hour, minute, tzinfo=UTC
        ),
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=float(row.volume),
    )


def _hourly_bar_from_row(symbol: str, exchange: str, row: Any) -> Bar:
    """Build one HOUR_1 bar from a formatDate=2 row. Pure logic, unit-tested.

    IBKR bar timestamps are bar START times; Chronos ``timestamp_utc`` is the bar
    CLOSE. The close is ``start + 1h`` capped at the session's official close, so
    the final partial bar (15:30 start on a regular day, 12:30 on a half-day) is
    stamped 16:00/13:00 rather than a fabricated 16:30/13:30. ``session_date`` is
    the exchange-local (US/Eastern) date of the bar start — RTH bars never cross
    midnight Eastern, and deriving it from UTC would misdate every bar after
    20:00 New York standard time.
    """

    text = str(row.date).strip()
    if not text.isdigit():
        raise HistoricalDataError(
            f"{symbol}: expected epoch-seconds bar date under formatDate=2, got {text!r} — "
            "the gateway answered in a format this parser refuses to guess about"
        )
    start_utc = datetime.fromtimestamp(int(text), tz=UTC)
    start_eastern = start_utc.astimezone(_EASTERN)
    session_date = start_eastern.date()
    try:
        closure = _calendar.closure_reason(session_date)
    except CalendarCoverageError as error:
        raise HistoricalDataError(
            f"{symbol}: bar dated {session_date.isoformat()} is outside the pinned "
            f"session-calendar range — extend the calendar before ingesting ({error})"
        ) from error
    if closure is None:
        session = _calendar.session(session_date)
        session_close_utc = datetime.combine(
            session_date, session.close_time, tzinfo=_EASTERN
        ).astimezone(UTC)
        timestamp_utc = min(start_utc + _HOURLY_SPAN, session_close_utc)
    else:
        # A bar on a non-session day is the venue disagreeing with the calendar.
        # Ingestion stores it honestly (nominal one-hour span) and certification
        # classifies it as UNEXPECTED_BAR — refusing here would hide the evidence.
        timestamp_utc = start_utc + _HOURLY_SPAN
    return Bar(
        symbol=symbol,
        source="ibkr",
        exchange=exchange,
        interval=BarInterval.HOUR_1,
        session_date=session_date,
        timestamp_utc=timestamp_utc,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=float(row.volume),
    )

"""Production IBKR adapter on the official TWS API (read paths + M7 order path).

Design (docs/LIVE_WHEEL_GAME_PLAN.md, Milestone 2):

- The official ``ibapi`` package ships with the TWS API distribution and is
  **never** a pip dependency; it is imported lazily so this module, demo
  mode, tests, and CI all work without it (docs/ibkr_setup.md covers the
  owner install).
- The callback thread never touches domain logic: a thin ``_App`` subclass
  forwards primitive callback payloads into the import-safe
  :class:`~chronos.broker.callbacks.CallbackBridge`; async methods await the
  correlated results through the request registry.
- Read paths plus the M7 order path (ADR-0009 §8): preview/submit/cancel are
  driven ONLY by the chronos.orders submission boundary; every LOCAL refusal
  raises :class:`BrokerRefusedBeforeSend` (provably nothing reached the
  gateway socket).
- Fail-closed normalization: unsupported security types, unparseable
  payloads, or missing account identity raise instead of guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerError,
    BrokerRefusedBeforeSend,
    BrokerSafetyError,
    BrokerSendGuard,
    OptionChainResponse,
)
from chronos.broker.callbacks import CallbackBridge, QuoteState, clean_price
from chronos.broker.market_data import (
    MarketDataCancellationError,
    MarketDataPacingError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.broker.order_ids import OrderIdAllocator
from chronos.broker.request_registry import RequestRegistry
from chronos.config.limits import (
    MAX_BROKER_CODE_CHARS,
    MAX_BROKER_SESSION_CHARS,
    MAX_BROKER_TIME_ZONE_CHARS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
)
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    IBEnvironment,
    OptionRight,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    CryptoContract,
    Instrument,
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    OptionPriceIncrement,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
    UnderlyingContract,
)
from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.orders.tracker import broker_status_to_lifecycle
from chronos.services.option_deliverable import assess_standard_deliverable

_logger = logging.getLogger("chronos.broker.official_ibkr")


def _observation_key(value: object) -> tuple[object, ...]:
    contract = getattr(value, "contract", None)
    con_id = int(getattr(value, "con_id", getattr(contract, "con_id", 0)) or 0)
    payload = value.model_dump_json() if hasattr(value, "model_dump_json") else type(value).__name__
    return (type(value).__name__, con_id, payload)


def _attach_broker_observations(
    error: BaseException,
    observed_values: Sequence[object],
    *,
    contract_ids: Sequence[int] = (),
    observed_at: datetime | None = None,
    truncated: bool = False,
    include_existing: bool = True,
    limit: int = MAX_CANDIDATE_REQUEST_CONTRACTS,
) -> BaseException:
    """Attach one deterministic bounded evidence prefix without changing error type."""

    existing = tuple(getattr(error, "observed_values", ())) if include_existing else ()
    ordered = tuple(sorted((*existing, *observed_values), key=_observation_key))
    bound = ordered[:limit]
    existing_ids = tuple(getattr(error, "contract_ids", ()))
    vars(error).update(
        observed_values=bound,
        observed_at=observed_at or getattr(error, "observed_at", None) or datetime.now(tz=UTC),
        truncated=(truncated or bool(getattr(error, "truncated", False)) or len(ordered) > limit),
        contract_ids=tuple(
            sorted(
                {
                    value
                    for value in (*existing_ids, *contract_ids)
                    if isinstance(value, int) and value > 0
                }
            )
        ),
    )
    return error


def _preferred_broker_error(errors: Sequence[BaseException]) -> BaseException:
    for error_type in (
        MarketDataCancellationError,
        MarketDataPermissionError,
        MarketDataPacingError,
        MarketDataUnavailableError,
        BrokerDataError,
    ):
        matching = next((error for error in errors if isinstance(error, error_type)), None)
        if matching is not None:
            return matching
    return errors[0]


class _KillSwitchLike(Protocol):
    """Structural type for the last-line breaker check (avoids an import cycle)."""

    def is_engaged(self) -> bool: ...

    def at_send(self) -> contextlib.AbstractContextManager[bool]: ...


def _maybe_float(value: Any) -> float | None:
    """ibapi order-state money fields arrive as strings, floats, or sentinels."""

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # ibapi uses very large sentinel values for "unset".
    return result if abs(result) < 1e300 else None


def _parse_ibkr_bar(
    raw: Any,
    *,
    contract: UnderlyingContract,
    interval: BarInterval,
) -> Bar | None:
    """One ``ibapi.common.BarData`` to a Chronos :class:`Bar`, or ``None``.

    Returns ``None`` rather than raising, and the caller drops it with a warning.
    That is the deliberate choice: one malformed row in a 180-bar response should
    cost that row, not the chart. The inverse — inventing a plausible bar to keep
    the series contiguous — would put a price on screen that no venue printed,
    which is the one thing the terminal's data-honesty rule forbids outright.

    Daily bars arrive as ``YYYYMMDD`` under ``formatDate=1``; intraday bars come
    as epoch seconds. Both are handled, and anything else is unparseable rather
    than assumed.
    """

    try:
        stamp = str(raw.date).strip()
        open_price = float(raw.open)
        high = float(raw.high)
        low = float(raw.low)
        close = float(raw.close)
        volume = float(raw.volume)
    except (AttributeError, TypeError, ValueError):
        return None

    session: date
    moment: datetime
    if len(stamp) == 8 and stamp.isdigit():
        try:
            session = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
        except ValueError:
            return None
        # 21:00Z is the conventional US equity close stamp this codebase already
        # uses for daily bars; `session_date` is the field that carries the
        # exchange trading date unambiguously (chronos.marketdata.bars).
        moment = datetime.combine(session, _DAILY_CLOSE_UTC, tzinfo=UTC)
    else:
        try:
            moment = datetime.fromtimestamp(int(stamp), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        session = moment.date()

    if volume < 0 or min(open_price, high, low, close) <= 0 or high < low:
        return None

    try:
        return Bar(
            symbol=contract.symbol,
            source="ibkr",
            exchange=contract.exchange or "SMART",
            interval=interval,
            session_date=session,
            timestamp_utc=moment,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            status=BarStatus.CLOSED,
        )
    except ValueError:
        # `Bar.__post_init__` rejects a non-alphanumeric symbol; a contract that
        # cannot be named is not one this series should silently carry.
        return None


_DAILY_CLOSE_UTC = time(21, 0)

_CONNECT_TIMEOUT_S = 15.0
_REQUEST_TIMEOUT_S = 20.0
_QUOTE_TIMEOUT_S = 15.0
_OPTION_GENERIC_TICKS = "100,101"
#: Historical requests are answered as a burst of callbacks terminated by
#: ``historicalDataEnd``, and a long lookback over a busy gateway is legitimately
#: slower than a quote snapshot. Still bounded: an unanswered request must fail
#: rather than hold a slot on a connection the order pipeline shares.
_HISTORICAL_TIMEOUT_S = 60.0

#: Chronos bar intervals to IBKR's ``barSizeSetting`` vocabulary. A missing entry
#: refuses rather than falling back, because guessing a bar size silently returns
#: a series that is not the one that was asked for.
_IBKR_BAR_SIZE: dict[BarInterval, str] = {
    BarInterval.DAY_1: "1 day",
    BarInterval.HOUR_1: "1 hour",
    BarInterval.MIN_5: "5 mins",
    BarInterval.MIN_1: "1 min",
}

_PAPER_PORTS = frozenset({7497, 4002})
_LIVE_PORTS = frozenset({7496, 4001})

# TWS error codes that are DEFINITIVE order rejections (the order will never
# work at the venue): price violations, rejected/unavailable orders, and
# validation failures. Ambiguous codes deliberately stay out — those leave the
# intent SUBMISSION_UNKNOWN for reconciliation (M7 review finding H2).
_DEFINITIVE_ORDER_REJECT_CODES = frozenset({110, 201, 203, 321, 10268})

_INSTALL_GUIDANCE = (
    "The official IBKR TWS API package (ibapi) is not installed. It is not on "
    "PyPI: download the TWS API from interactivebrokers.github.io, then install "
    "the bundled Python client (see docs/ibkr_setup.md). Demo mode runs without it."
)


def _with_session_evidence[T: (UnderlyingContract, OptionContract)](
    instrument: T, details: Any
) -> T:
    """Carry ``liquidHours``/``timeZoneId`` from ContractDetails onto the contract.

    These live on the *details* object, not on the ``Contract`` inside it, which
    is why ``instrument_from_contract`` never saw them and why R-26 stayed open
    for four milestones: the evidence was arriving on every qualification and
    being dropped one attribute short of where it was needed.

    Absent fields leave the contract unchanged, which reads downstream as
    *unknown* and keeps the session gate blocking. A gateway that answers without
    details must not look like a gateway that answered "no restrictions".
    """

    raw = str(getattr(details, "liquidHours", "") or "")
    zone = str(getattr(details, "timeZoneId", "") or "")
    if len(raw) > MAX_BROKER_SESSION_CHARS or len(zone) > MAX_BROKER_TIME_ZONE_CHARS:
        raise BrokerDataError("option session evidence exceeds the hard evidence bound")
    if not raw or not zone:
        return instrument
    return type(instrument).model_validate(
        {
            **instrument.model_dump(mode="python"),
            "liquid_hours": raw,
            "time_zone_id": zone,
        }
    )


def _with_deliverable_evidence(instrument: OptionContract, details: Any) -> OptionContract:
    """Screen the option's deliverable and, if it passes, record the evidence (M11, R-27).

    Same shape as the session evidence above and for the same reason: the facts
    are on ``ContractDetails`` (``underConId``, ``underSymbol``, ``underSecType``),
    not on the ``Contract`` inside it, so nothing downstream could ever have seen
    them.

    A contract that does not pass is returned **unchanged** — still
    ``deliverable_verified=False``, still refused by the risk engine, exactly as
    every option was before M11. Failing the screen must never be worse than not
    having run it.
    """

    under_con_id = int(getattr(details, "underConId", 0) or 0)
    assessment = assess_standard_deliverable(
        symbol=instrument.symbol,
        trading_class=instrument.trading_class,
        local_symbol=instrument.local_symbol,
        multiplier=instrument.multiplier,
        underlying_con_id=under_con_id or None,
        underlying_symbol=str(getattr(details, "underSymbol", "") or ""),
        underlying_security_type=str(getattr(details, "underSecType", "") or ""),
    )
    if not assessment.standard:
        _logger.warning(
            "Option %s refused a standard deliverable: %s",
            instrument.local_symbol,
            "; ".join(assessment.reasons),
            extra={"event": "option_deliverable_not_standard"},
        )
        return instrument
    return instrument.model_copy(
        update={
            "underlying_con_id": under_con_id,
            "deliverable_shares": instrument.multiplier,
            "deliverable_verified": True,
        }
    )


def _bounded_smallest(
    values: Any,
    *,
    limit: int,
    normalize: Callable[[Any], Any],
) -> tuple[tuple[Any, ...], bool]:
    retained = heapq.nsmallest(limit + 1, (normalize(value) for value in (values or ())))
    return tuple(retained[:limit]), len(retained) > limit


def _bounded_chain_callback_row(row: Any) -> tuple[Any, bool]:
    exchange, underlying_con_id, trading_class, multiplier, expirations, strikes = row

    raw_exchange = str(exchange)
    raw_trading_class = str(trading_class)
    if len(raw_exchange) > MAX_BROKER_CODE_CHARS or len(raw_trading_class) > MAX_BROKER_CODE_CHARS:
        raise BrokerDataError("option-chain routing identity exceeds the hard evidence bound")
    normalized_exchange = raw_exchange.strip().upper()
    normalized_trading_class = raw_trading_class.strip().upper()
    normalized_underlying_con_id = int(underlying_con_id)
    normalized_multiplier = Decimal(str(multiplier))
    if not normalized_exchange or not normalized_trading_class:
        raise BrokerDataError("option-chain routing identity is incomplete")
    if normalized_underlying_con_id <= 0:
        raise BrokerDataError("option-chain underlying conId must be positive")
    if not normalized_multiplier.is_finite() or normalized_multiplier <= 0:
        raise BrokerDataError("option-chain multiplier must be finite and positive")

    def normalize_expiration(raw: Any) -> str:
        return _parse_yyyymmdd(str(raw)).strftime("%Y%m%d")

    def normalize_strike(raw: Any) -> Decimal:
        value = Decimal(str(raw))
        if not value.is_finite() or value <= 0:
            raise BrokerDataError("option-chain strike must be finite and positive")
        return value

    bounded_expirations, expiration_overflow = _bounded_smallest(
        expirations,
        limit=MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
        normalize=normalize_expiration,
    )
    bounded_strikes, strike_overflow = _bounded_smallest(
        strikes,
        limit=MAX_OPTION_CHAIN_STRIKES_PER_ROW,
        normalize=normalize_strike,
    )
    return (
        (
            normalized_exchange,
            normalized_underlying_con_id,
            normalized_trading_class,
            str(normalized_multiplier),
            bounded_expirations,
            bounded_strikes,
        ),
        expiration_overflow or strike_overflow,
    )


def _chain_row_key(row: Any) -> tuple[object, ...]:
    exchange, underlying_con_id, trading_class, multiplier, expirations, strikes = row
    return (
        int(underlying_con_id),
        str(exchange).strip().upper(),
        str(trading_class).strip().upper(),
        str(multiplier),
        tuple(expirations),
        tuple(strikes),
    )


def _contract_detail_key(details: Any) -> tuple[object, ...]:
    contract = getattr(details, "contract", details)
    return (
        int(getattr(contract, "conId", 0) or 0),
        str(getattr(contract, "symbol", "") or "").strip().upper(),
        str(getattr(contract, "localSymbol", "") or "").strip().upper(),
        str(getattr(contract, "exchange", "") or "").strip().upper(),
    )


def _option_from_details(details: Any) -> OptionContract | None:
    raw_contract = getattr(details, "contract", details)
    required = (
        getattr(raw_contract, "multiplier", None),
        getattr(raw_contract, "localSymbol", None),
        getattr(raw_contract, "tradingClass", None),
        getattr(raw_contract, "exchange", None),
        getattr(raw_contract, "currency", None),
    )
    if any(value in (None, "") for value in required):
        raise BrokerDataError(
            "qualified option contract details are missing exact identity metadata"
        )
    instrument = instrument_from_contract(raw_contract)
    if not isinstance(instrument, OptionContract):
        return None
    min_tick_raw = getattr(details, "minTick", None)
    if min_tick_raw:
        instrument = instrument.model_copy(update={"min_tick": Decimal(str(min_tick_raw))})
    instrument = _with_session_evidence(instrument, details)
    return _with_deliverable_evidence(instrument, details)


def _load_ibapi() -> tuple[Any, Any, Any, Any]:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.execution import ExecutionFilter
        from ibapi.wrapper import EWrapper
    except ImportError as error:
        raise BrokerError(_INSTALL_GUIDANCE) from error
    return EClient, EWrapper, Contract, ExecutionFilter


def _load_order_class() -> Any:
    """The real ``ibapi.order.Order`` — a dedicated loader so the order path
    can never silently bind to the wrong ibapi class (M7 review finding C1)."""

    try:
        from ibapi.order import Order
    except ImportError as error:
        raise BrokerRefusedBeforeSend(_INSTALL_GUIDANCE) from error
    return Order


def _make_app(bridge: CallbackBridge) -> Any:
    """Build the EWrapper/EClient subclass that forwards into the bridge.

    Signatures use ``*args`` tolerantly: TWS API minor versions append
    parameters (e.g. ``error`` gained ``advancedOrderRejectJson``), and the
    bridge only needs the stable leading arguments.
    """

    EClient, EWrapper, _, _ = _load_ibapi()

    class _App(EWrapper, EClient):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            EClient.__init__(self, self)

        def error(self, *args: Any) -> None:
            # (reqId, errorCode, errorString[, advancedOrderRejectJson])
            # Older/newer builds may prepend a timestamp; find the int pair.
            values = list(args)
            if values and not isinstance(values[0], int):
                values = values[1:]
            if len(values) >= 3:
                bridge.on_error(int(values[0]), int(values[1]), str(values[2]))

        def connectionClosed(self) -> None:
            bridge.on_connection_closed()

        def nextValidId(self, orderId: int) -> None:
            bridge.on_next_valid_id(int(orderId))

        def managedAccounts(self, accountsList: str) -> None:
            bridge.on_managed_accounts(str(accountsList))

        def currentTime(self, time: int) -> None:
            bridge.on_current_time(int(time))

        def accountSummary(
            self, reqId: int, account: str, tag: str, value: str, currency: str
        ) -> None:
            bridge.on_account_summary(int(reqId), account, tag, value, currency)

        def accountSummaryEnd(self, reqId: int) -> None:
            bridge.on_account_summary_end(int(reqId))

        def position(self, account: str, contract: Any, position: Any, avgCost: float) -> None:
            bridge.on_position(account, contract, float(position), float(avgCost))

        def positionEnd(self) -> None:
            bridge.on_position_end()

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            bridge.on_contract_details(int(reqId), contractDetails)

        def contractDetailsEnd(self, reqId: int) -> None:
            bridge.on_contract_details_end(int(reqId))

        def historicalData(self, reqId: int, bar: Any) -> None:
            # Straight into the registry's generic accumulation rather than into
            # bridge state: a historical response is an append-only sequence that
            # ends with an explicit terminator, which is exactly the shape
            # `RequestRegistry` already models. Quotes need bridge state because
            # their ticks merge into one mutable snapshot; bars do not.
            bridge.registry.add(int(reqId), bar)

        def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
            del start, end
            bridge.registry.finish(int(reqId))

        def securityDefinitionOptionParameter(
            self,
            reqId: int,
            exchange: str,
            underlyingConId: int,
            tradingClass: str,
            multiplier: str,
            expirations: Any,
            strikes: Any,
        ) -> None:
            bridge.on_sec_def_opt_params(
                int(reqId),
                exchange,
                int(underlyingConId),
                tradingClass,
                multiplier,
                expirations,
                strikes,
            )

        def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
            bridge.on_sec_def_opt_params_end(int(reqId))

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
            bridge.on_exec_details(int(reqId), contract, execution)

        def execDetailsEnd(self, reqId: int) -> None:
            bridge.on_exec_details_end(int(reqId))

        def commissionReport(self, commissionReport: Any) -> None:
            bridge.on_commission_report(commissionReport)

        def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
            bridge.on_open_order(int(orderId), contract, order, orderState)

        def openOrderEnd(self) -> None:
            bridge.on_open_order_end()

        def orderStatus(
            self,
            orderId: int,
            status: str,
            filled: Any,
            remaining: Any,
            avgFillPrice: float,
            permId: int,
            parentId: int,
            lastFillPrice: float,
            clientId: int,
            whyHeld: str,
            mktCapPrice: float,
        ) -> None:
            del parentId, lastFillPrice, clientId, whyHeld, mktCapPrice
            bridge.on_order_status(
                int(orderId),
                str(status),
                float(filled),
                float(remaining),
                float(avgFillPrice),
                int(permId),
            )

        def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
            bridge.on_tick_price(int(reqId), int(tickType), float(price))

        def tickSize(self, reqId: int, tickType: int, size: Any) -> None:
            bridge.on_tick_size(int(reqId), int(tickType), float(size))

        def tickOptionComputation(self, reqId: int, tickType: int, *args: Any) -> None:
            # (tickAttrib,) impliedVol, delta, optPrice, pvDividend, gamma,
            # vega, theta, undPrice — tickAttrib present on newer builds.
            values = list(args)
            if values and isinstance(values[0], int):
                values = values[1:]
            implied_vol = float(values[0]) if len(values) > 0 and values[0] is not None else None
            delta = float(values[1]) if len(values) > 1 and values[1] is not None else None
            gamma = float(values[4]) if len(values) > 4 and values[4] is not None else None
            theta = float(values[6]) if len(values) > 6 and values[6] is not None else None
            bridge.on_tick_option(int(reqId), int(tickType), implied_vol, delta, gamma, theta)

        def tickSnapshotEnd(self, reqId: int) -> None:
            bridge.on_tick_snapshot_end(int(reqId))

        def marketDataType(self, reqId: int, marketDataType: int) -> None:
            bridge.on_market_data_type(int(reqId), int(marketDataType))

        def marketRule(self, marketRuleId: int, priceIncrements: Any) -> None:
            bridge.on_market_rule(int(marketRuleId), priceIncrements)

    return _App()


# --------------------------------------------------------------------------- #
# Pure normalizers (unit-tested without ibapi)
# --------------------------------------------------------------------------- #


def account_summary_from_rows(
    rows: Sequence[tuple[str, str, str, str]],
    *,
    expected_account: str,
    as_of: datetime,
) -> AccountSummary:
    values: dict[str, Decimal] = {}
    currency = "USD"
    for account, tag, value, row_currency in rows:
        if account != expected_account:
            raise BrokerDataError(
                f"account summary row for unexpected account (expected {expected_account!r})"
            )
        try:
            values[tag] = Decimal(value)
        except ArithmeticError as error:  # pragma: no cover - Decimal() raises InvalidOperation
            raise BrokerDataError(f"unparseable account summary value for {tag}") from error
        except Exception as error:
            raise BrokerDataError(f"unparseable account summary value for {tag}") from error
        if row_currency:
            currency = row_currency
    required = {"NetLiquidation", "TotalCashValue", "BuyingPower"}
    missing = required - set(values)
    if missing:
        raise BrokerDataError(f"account summary missing tags: {sorted(missing)}")
    return AccountSummary(
        account_id=expected_account,
        net_liquidation=values["NetLiquidation"],
        total_cash=values["TotalCashValue"],
        buying_power=values["BuyingPower"],
        currency=currency,
        as_of=as_of,
    )


def instrument_from_contract(contract: Any) -> Instrument:
    sec_type = str(getattr(contract, "secType", "") or "")
    symbol = str(getattr(contract, "symbol", "") or "")
    con_id = int(getattr(contract, "conId", 0) or 0)
    currency = str(getattr(contract, "currency", "") or "USD")
    if con_id <= 0 or not symbol:
        raise BrokerDataError(f"contract missing identity (conId={con_id}, symbol={symbol!r})")
    if sec_type == "STK":
        exchange = str(getattr(contract, "exchange", "") or "SMART")
        primary = str(getattr(contract, "primaryExchange", "") or "") or None
        return UnderlyingContract(
            con_id=con_id,
            symbol=symbol,
            exchange=exchange,
            primary_exchange=primary,
            currency=currency,
        )
    if sec_type == "OPT":
        raw_expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
        expiration = _parse_yyyymmdd(raw_expiry)
        right_raw = str(getattr(contract, "right", "") or "").upper()
        if right_raw.startswith("C"):
            right = OptionRight.CALL
        elif right_raw.startswith("P"):
            right = OptionRight.PUT
        else:
            raise BrokerDataError(f"unparseable option right {right_raw!r}")
        strike = Decimal(str(getattr(contract, "strike", 0) or 0))
        multiplier_raw = str(getattr(contract, "multiplier", "") or "100")
        local_symbol = str(getattr(contract, "localSymbol", "") or "")
        trading_class = str(getattr(contract, "tradingClass", "") or symbol)
        exchange = str(getattr(contract, "exchange", "") or "SMART")
        return OptionContract(
            con_id=con_id,
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            right=right,
            multiplier=Decimal(multiplier_raw),
            trading_class=trading_class,
            local_symbol=local_symbol or f"{symbol}-{raw_expiry}",
            exchange=exchange,
            currency=currency,
        )
    if sec_type == "CRYPTO":
        # Read-path normalization (ADR-0010 §7 commit 1): venue metadata stays
        # None here — only qualification may populate it. Landing this BEFORE
        # any crypto order path exists also fixes the latent account-wide read
        # wedge: a manually-purchased crypto position no longer breaks
        # positions()/executions()/open_orders() for the whole account.
        exchange = str(getattr(contract, "exchange", "") or "PAXOS")
        return CryptoContract(
            con_id=con_id,
            symbol=symbol,
            exchange=exchange,
            currency=currency,
        )
    raise BrokerDataError(
        f"unsupported security type {sec_type!r} for {symbol!r}; Chronos normalizes "
        "STK, OPT, and CRYPTO contracts only"
    )


def position_from_row(
    row: tuple[str, Any, float, float], *, expected_account: str
) -> BrokerPosition:
    account, contract, quantity, avg_cost = row
    if account != expected_account:
        raise BrokerDataError(
            f"position row for unexpected account (expected {expected_account!r})"
        )
    return BrokerPosition(
        account_id=account,
        contract=instrument_from_contract(contract),
        quantity=Decimal(str(quantity)),
        average_cost=Decimal(str(avg_cost)),
    )


def execution_from_pair(
    contract: Any,
    execution: Any,
    *,
    commission: Decimal | None,
    commission_currency: str | None,
) -> BrokerExecution:
    side_raw = str(getattr(execution, "side", "") or "").upper()
    if side_raw in ("BOT", "BUY"):
        side = OrderSide.BUY
    elif side_raw in ("SLD", "SELL"):
        side = OrderSide.SELL
    else:
        raise BrokerDataError(f"unparseable execution side {side_raw!r}")
    raw_time = str(getattr(execution, "time", "") or "")
    timestamp = _parse_execution_time(raw_time)
    return BrokerExecution(
        execution_id=str(getattr(execution, "execId", "") or ""),
        account_id=str(getattr(execution, "acctNumber", "") or ""),
        broker_order_id=int(getattr(execution, "orderId", 0) or 0),
        permanent_id=int(getattr(execution, "permId", 0) or 0) or None,
        client_id=int(getattr(execution, "clientId", 0) or 0),
        order_ref=str(getattr(execution, "orderRef", "") or "") or None,
        contract=instrument_from_contract(contract),
        side=side,
        quantity=Decimal(str(getattr(execution, "shares", 0) or 0)),
        price=Decimal(str(getattr(execution, "price", 0) or 0)),
        timestamp=timestamp,
        commission=commission,
        commission_currency=commission_currency if commission is not None else None,
    )


def _parse_yyyymmdd(raw: str) -> date:
    text = raw.strip()[:8]
    if len(text) != 8 or not text.isdigit():
        raise BrokerDataError(f"unparseable TWS date {raw!r}")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as error:
        raise BrokerDataError(f"unparseable TWS date {raw!r}") from error


def _parse_execution_time(raw: str) -> datetime:
    # TWS format: "20260717 14:30:00" optionally followed by a timezone name.
    parts = raw.strip().split()
    if len(parts) >= 2:
        day = _parse_yyyymmdd(parts[0])
        clock = parts[1].split(":")
        if len(clock) != 3 or not all(piece.isdigit() for piece in clock):
            raise BrokerDataError(f"unparseable execution time {raw!r}")
        tz: Any = UTC
        if len(parts) >= 3:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(parts[2])
            except Exception as error:
                raise BrokerDataError(f"unknown execution timezone in {raw!r}") from error
        try:
            return datetime(
                day.year,
                day.month,
                day.day,
                int(clock[0]),
                int(clock[1]),
                int(clock[2]),
                tzinfo=tz,
            )
        except ValueError as error:
            raise BrokerDataError(f"unparseable execution time {raw!r}") from error
    raise BrokerDataError(f"unparseable execution time {raw!r}")


def quote_from_state(
    state: QuoteState, *, contract: Instrument, timestamp: datetime
) -> MarketQuote:
    greeks = None
    if any(
        value is not None
        for value in (state.delta, state.gamma, state.theta, state.implied_volatility)
    ):
        greeks = ModelGreeks(
            delta=state.delta,
            gamma=state.gamma,
            theta=state.theta,
            implied_volatility=state.implied_volatility,
        )
    return MarketQuote(
        contract=contract,
        timestamp=timestamp,
        data_quality=state.data_quality,
        bid=state.bid,
        ask=state.ask,
        last=state.last,
        close=state.close,
        volume=state.volume,
        open_interest=state.open_interest,
        greeks=greeks,
    )


def chain_parameters_from_row(
    row: tuple[str, int, str, str, Any, Any],
) -> OptionChainParameters:
    exchange, underlying_con_id, trading_class, multiplier, expirations, strikes = row
    parsed_expirations = [_parse_yyyymmdd(str(raw)) for raw in sorted(set(expirations or ()))]
    parsed_strikes = tuple(
        Decimal(str(strike)) for strike in sorted(set(strikes or ())) if float(strike) > 0
    )
    return OptionChainParameters(
        exchange=exchange,
        underlying_con_id=underlying_con_id,
        trading_class=trading_class,
        multiplier=Decimal(str(multiplier)),
        expirations=tuple(parsed_expirations),
        strikes=parsed_strikes,
    )


def verify_environment_port(environment: IBEnvironment, port: int) -> None:
    """Refuse an environment/port mismatch (paper config on a live port etc.)."""

    if environment is IBEnvironment.PAPER and port not in _PAPER_PORTS:
        raise BrokerSafetyError(
            f"IB_ENVIRONMENT=paper requires a paper port {sorted(_PAPER_PORTS)}, got {port}"
        )
    if environment is IBEnvironment.LIVE and port not in _LIVE_PORTS:
        raise BrokerSafetyError(
            f"IB_ENVIRONMENT=live requires a live port {sorted(_LIVE_PORTS)}, got {port}"
        )


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


class OfficialIBKRBroker:
    """Production adapter over the official TWS API (read paths + M7 order path)."""

    def __init__(
        self,
        settings: Settings,
        *,
        live_kill_switch: _KillSwitchLike | None = None,
        on_connection_uncertain: Callable[[str], object] | None = None,
        on_managed_account_scope_change: (
            Callable[[str], contextlib.AbstractContextManager[None]] | None
        ) = None,
    ) -> None:
        self._live_kill_switch = live_kill_switch
        _load_ibapi()  # fail fast with install guidance if the package is absent
        if not settings.ib_account_id.strip():
            raise BrokerSafetyError(
                "IB_ACCOUNT_ID is required for the official IBKR adapter; refusing to "
                "connect without an expected account to verify against"
            )
        if (
            settings.ib_account_allowlist
            and settings.ib_account_id not in settings.ib_account_allowlist
        ):
            raise BrokerSafetyError(
                "IB_ACCOUNT_ID is not in IB_ACCOUNT_ALLOWLIST; refusing to connect"
            )
        verify_environment_port(settings.ib_environment, settings.ib_port)
        self._settings = settings
        self.registry = RequestRegistry()
        self.bridge = CallbackBridge(
            self.registry,
            on_connection_uncertain=on_connection_uncertain,
            on_managed_account_scope_change=on_managed_account_scope_change,
        )
        self.order_ids = OrderIdAllocator()
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._active_market_data: dict[int, int] = {}

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        if self._app is not None or self._thread is not None:
            if self._app_connected():
                return
            await self.disconnect()
        # Fresh handshake state: stale events must not vouch for a new session
        # (M7 review finding H4).
        self.bridge.reset_for_reconnect()
        app = _make_app(self.bridge)
        app.connect(self._settings.ib_host, self._settings.ib_port, self._settings.ib_client_id)
        thread = threading.Thread(target=app.run, name="chronos-ibapi-reader", daemon=True)
        thread.start()
        self._app = app
        self._thread = thread
        import asyncio

        loop = asyncio.get_running_loop()
        connected = await loop.run_in_executor(
            None, self.bridge.connected_event.wait, _CONNECT_TIMEOUT_S
        )
        accounts_seen = await loop.run_in_executor(
            None, self.bridge.managed_accounts_event.wait, _CONNECT_TIMEOUT_S
        )
        if not connected or not accounts_seen:
            await self.disconnect()
            raise BrokerConnectionError(
                "TWS/Gateway did not complete the connection handshake "
                f"within {_CONNECT_TIMEOUT_S:.0f}s (is the API enabled on "
                f"{self._settings.ib_host}:{self._settings.ib_port}?)"
            )
        if self.bridge.next_valid_id is not None:
            self.order_ids.seed(self.bridge.next_valid_id)
        expected = self._settings.ib_account_id
        if expected not in self.bridge.managed_accounts:
            await self.disconnect()
            raise BrokerSafetyError(
                "connected session does not manage the configured account; refusing "
                f"(managed count={len(self.bridge.managed_accounts)})"
            )
        # Request live-quality data by default; per-request callbacks classify.
        app.reqMarketDataType(1)

    async def disconnect(self) -> None:
        app = self._app
        thread = self._thread
        cancellation_error: BrokerDataError | None = None
        active_market_data = getattr(self, "_active_market_data", {})
        if app is not None and active_market_data:
            try:
                await self.cancel_market_data(tuple(active_market_data))
            except BrokerDataError as error:
                cancellation_error = error
        if app is not None:
            with contextlib.suppress(Exception):
                app.disconnect()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                # Retain the stale session handles and refuse reconnect. The
                # bridge is shared, so resetting it while this reader can still
                # issue callbacks would let old-session events poison a fresh
                # reconciliation generation.
                raise BrokerConnectionError(
                    "official IBKR reader thread did not stop; reconnect is locked"
                )
        self._app = None
        self._thread = None
        active_market_data.clear()
        if cancellation_error is not None:
            raise cancellation_error

    def _app_connected(self) -> bool:
        app = self._app
        if app is None or self.bridge.connection_closed.is_set():
            return False
        try:
            return bool(app.isConnected())
        except Exception:
            return False

    def _require_app(self) -> Any:
        if not self._app_connected():
            raise BrokerConnectionError("official IBKR adapter is not connected")
        return self._app

    async def connection_status(self) -> ConnectionStatus:
        connected = self._app_connected()
        environment = (
            DisplayEnvironment.LIVE
            if self._settings.ib_environment is IBEnvironment.LIVE
            else DisplayEnvironment.PAPER
        )
        return ConnectionStatus(
            state=ConnectionState.CONNECTED if connected else ConnectionState.DISCONNECTED,
            environment=environment,
            connected=connected,
            # Broker-corroborated identity (review finding M2): the configured
            # id is reported ONLY while the gateway currently manages it —
            # otherwise None, which every grant path denies.
            account_id=(
                self._settings.ib_account_id
                if connected and self._settings.ib_account_id in self.bridge.managed_accounts
                else None
            ),
            data_quality=DataQuality.UNKNOWN,
            message="official TWS API adapter",
            # Broker-OBSERVED evidence (ADR-0009 §3): what the gateway itself
            # reported via managedAccounts — never Settings.ib_environment.
            managed_accounts=self.bridge.managed_accounts if connected else (),
        )

    # ------------------------------------------------------------------ #
    # Read paths
    # ------------------------------------------------------------------ #

    async def server_time(self) -> datetime:
        app = self._require_app()
        flight = self.bridge.start_current_time()
        app.reqCurrentTime()
        import asyncio

        loop = asyncio.get_running_loop()
        completed = await loop.run_in_executor(None, flight.done.wait, _REQUEST_TIMEOUT_S)
        if not completed or not flight.items:
            raise BrokerDataError("currentTime request timed out")
        return datetime.fromtimestamp(int(flight.items[0]), tz=UTC)

    async def account_summary(self) -> AccountSummary:
        app = self._require_app()
        request_id = self.registry.open()
        app.reqAccountSummary(request_id, "All", "NetLiquidation,TotalCashValue,BuyingPower")
        try:
            rows = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        finally:
            app.cancelAccountSummary(request_id)
        expected = self._settings.ib_account_id
        relevant = [row for row in rows if row[0] == expected]
        if not relevant:
            raise BrokerDataError("account summary returned no rows for the configured account")
        return account_summary_from_rows(
            [(expected, tag, value, currency) for _, tag, value, currency in relevant],
            expected_account=expected,
            as_of=datetime.now(tz=UTC),
        )

    async def positions(self) -> tuple[BrokerPosition, ...]:
        app = self._require_app()
        flight = self.bridge.start_positions()
        app.reqPositions()
        import asyncio

        loop = asyncio.get_running_loop()
        completed = await loop.run_in_executor(None, flight.done.wait, _REQUEST_TIMEOUT_S)
        app.cancelPositions()
        if not completed:
            raise BrokerDataError("positions request timed out")
        expected = self._settings.ib_account_id
        rows = [row for row in flight.items if row[0] == expected]
        normalized = tuple(
            position_from_row(row, expected_account=expected)
            for row in rows
            if float(row[2]) != 0.0
        )
        return normalized

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        app = self._require_app()
        _, _, _, ExecutionFilter = _load_ibapi()
        request_id = self.registry.open()
        exec_filter = ExecutionFilter()
        exec_filter.acctCode = self._settings.ib_account_id
        if since is not None:
            exec_filter.time = since.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")
        app.reqExecutions(request_id, exec_filter)
        pairs = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        results = []
        for contract, execution in pairs:
            exec_id = str(getattr(execution, "execId", "") or "")
            commission, currency = self.bridge.commission_for(exec_id)
            results.append(
                execution_from_pair(
                    contract,
                    execution,
                    commission=commission,
                    commission_currency=currency,
                )
            )
        return tuple(results)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        """Normalize openOrder/orderStatus pairs with the M5 lifecycle model."""

        app = self._require_app()
        flight = self.bridge.start_open_orders()
        app.reqAllOpenOrders()
        done = await asyncio.get_running_loop().run_in_executor(
            None, flight.done.wait, _REQUEST_TIMEOUT_S
        )
        if not done or self.bridge.connection_closed.is_set():
            raise BrokerConnectionError("open-orders request did not complete")
        orders: list[BrokerOrder] = []
        for order_id, contract, order, order_state in flight.items:
            order_ref = str(getattr(order, "orderRef", "") or "")
            is_chronos = order_ref.startswith("CHR-")
            order_type = str(getattr(order, "orderType", "") or "")
            limit_price = Decimal(str(getattr(order, "lmtPrice", 0) or 0))
            raw_action = str(getattr(order, "action", "") or "")
            conforming = (
                order_type == "LMT"
                and limit_price > 0
                and raw_action in {side.value for side in OrderSide}
            )
            if not conforming:
                if is_chronos:
                    # A Chronos-owned order in a non-Chronos shape is evidence
                    # of corruption: hard fail-closed.
                    raise BrokerSafetyError(
                        f"Chronos open order {order_id} ({order_ref}) is not a "
                        f"positive-limit LMT order ({order_type!r}); refusing"
                    )
                # FOREIGN orders (manual TWS/mobile, other client ids) must not
                # wedge reconciliation or the operator-resolve snapshot (M7
                # review finding H1): quarantine with a notice and move on —
                # Chronos never acts on orders it does not own anyway.
                self.bridge.notices.append(
                    f"[foreign-order] skipped non-LMT open order {order_id} "
                    f"({order_type!r}, ref={order_ref!r}) during normalization"
                )
                continue
            try:
                instrument = instrument_from_contract(contract)
            except BrokerError:
                if is_chronos:
                    raise
                self.bridge.notices.append(
                    f"[foreign-order] skipped unnormalizable open order {order_id}"
                )
                continue
            quantity = Decimal(str(getattr(order, "totalQuantity", 0) or 0))
            status_entry = self.bridge.order_statuses.get(order_id)
            raw_status = (
                status_entry[0] if status_entry else str(getattr(order_state, "status", "") or "")
            )
            filled = Decimal(str(status_entry[1])) if status_entry else Decimal("0")
            remaining = (
                Decimal(str(status_entry[2]))
                if status_entry
                else max(quantity - filled, Decimal("0"))
            )
            orders.append(
                BrokerOrder(
                    broker_order_id=int(order_id),
                    permanent_id=int(getattr(order, "permId", 0) or 0) or None,
                    client_id=int(getattr(order, "clientId", 0) or 0),
                    account_id=str(getattr(order, "account", "") or ""),
                    order_ref=str(getattr(order, "orderRef", "") or "") or None,
                    contract=instrument,
                    side=OrderSide(raw_action),
                    quantity=quantity,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    limit_price=limit_price,
                    lifecycle=broker_status_to_lifecycle(
                        raw_status, filled_quantity=filled, remaining_quantity=remaining
                    ),
                    transmit=bool(getattr(order, "transmit", False)),
                    outside_rth=bool(getattr(order, "outsideRth", False)),
                )
            )
        return tuple(orders)

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        contract = Contract()
        contract.symbol = symbol.strip().upper()
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        request_id = self.registry.open(
            max_items=MAX_CANDIDATE_REQUEST_CONTRACTS,
            sort_key=_contract_detail_key,
        )

        def attach_underlying_observations(error: BrokerDataError) -> BrokerDataError:
            observed: list[UnderlyingContract] = []
            for detail in getattr(error, "observed_values", ()):
                try:
                    instrument = instrument_from_contract(getattr(detail, "contract", detail))
                except BrokerDataError:
                    break
                if isinstance(instrument, UnderlyingContract):
                    observed.append(_with_session_evidence(instrument, detail))
            _attach_broker_observations(error, observed, include_existing=False)
            return error

        try:
            app.reqContractDetails(request_id, contract)
        except Exception as send_error:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"underlying qualification request for {symbol!r} could not be sent"
                ),
            )
            raise attach_underlying_observations(error) from send_error
        try:
            details = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        except asyncio.CancelledError as cancellation:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"underlying qualification request for {symbol!r} was interrupted"
                ),
            )
            raise attach_underlying_observations(error) from cancellation
        except BrokerDataError as error:
            attach_underlying_observations(error)
            raise
        if len(details) != 1:
            ambiguity_error = BrokerDataError(
                f"contract details for underlying {symbol!r} are missing or ambiguous"
            )
            observed = []
            for detail in details:
                try:
                    candidate = instrument_from_contract(getattr(detail, "contract", detail))
                except BrokerDataError:
                    continue
                if isinstance(candidate, UnderlyingContract):
                    observed.append(_with_session_evidence(candidate, detail))
            _attach_broker_observations(ambiguity_error, observed)
            raise ambiguity_error
        first = details[0]
        instrument = instrument_from_contract(getattr(first, "contract", first))
        if not isinstance(instrument, UnderlyingContract):
            raise BrokerDataError(f"underlying qualification returned a non-stock for {symbol!r}")
        if (
            instrument.symbol != contract.symbol
            or instrument.exchange != contract.exchange
            or instrument.currency != contract.currency
        ):
            mismatch_error = BrokerDataError(
                f"underlying qualification returned unexpected identity for {symbol!r}"
            )
            _attach_broker_observations(mismatch_error, (instrument,))
            raise mismatch_error
        return _with_session_evidence(instrument, first)

    async def qualify_crypto(self, symbol: str) -> CryptoContract:
        """Qualify a spot-crypto contract (ADR-0010): secType CRYPTO, PAXOS venue.

        Venue metadata (``min_tick``/``min_size``/``size_increment``) is
        harvested from ContractDetails, mirroring how option qualification
        reads ``minTick``. The exact ContractDetails field names
        (``minSize``/``sizeIncrement``) require TWS API >= 10.10 and are an
        owner gateway-verification item; absent fields stay ``None`` so the
        dependent risk checks fail UNKNOWN rather than assume.
        """

        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        contract = Contract()
        contract.symbol = symbol.strip().upper()
        contract.secType = "CRYPTO"
        contract.exchange = "PAXOS"
        contract.currency = "USD"
        request_id = self.registry.open(
            max_items=MAX_CANDIDATE_REQUEST_CONTRACTS,
            sort_key=_contract_detail_key,
        )

        def attach_crypto_observations(error: BrokerDataError) -> BrokerDataError:
            observed: list[CryptoContract] = []
            for detail in getattr(error, "observed_values", ()):
                try:
                    instrument = instrument_from_contract(getattr(detail, "contract", detail))
                except BrokerDataError:
                    break
                if isinstance(instrument, CryptoContract):
                    observed.append(instrument)
            _attach_broker_observations(error, observed, include_existing=False)
            return error

        try:
            app.reqContractDetails(request_id, contract)
        except Exception as send_error:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"crypto qualification request for {symbol!r} could not be sent"
                ),
            )
            raise attach_crypto_observations(error) from send_error
        try:
            details = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        except asyncio.CancelledError as cancellation:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"crypto qualification request for {symbol!r} was interrupted"
                ),
            )
            raise attach_crypto_observations(error) from cancellation
        except BrokerDataError as error:
            attach_crypto_observations(error)
            raise
        if not details:
            raise BrokerDataError(f"no contract details for crypto {symbol!r}")
        first = details[0]
        instrument = instrument_from_contract(getattr(first, "contract", first))
        if not isinstance(instrument, CryptoContract):
            raise BrokerDataError(f"crypto qualification returned a non-crypto for {symbol!r}")
        updates: dict[str, Decimal] = {}
        for attr, field in (
            ("minTick", "min_tick"),
            ("minSize", "min_size"),
            ("sizeIncrement", "size_increment"),
        ):
            raw = getattr(first, attr, None)
            if raw:
                updates[field] = Decimal(str(raw))
        if updates:
            instrument = instrument.model_copy(update=updates)
        return instrument

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> OptionChainResponse:
        app = self._require_app()
        request_id = self.registry.open(
            max_items=MAX_OPTION_CHAIN_ROWS,
            normalizer=_bounded_chain_callback_row,
            sort_key=_chain_row_key,
        )

        def attach_chain_response(error: BrokerDataError) -> BrokerDataError:
            observed_rows = tuple(getattr(error, "observed_values", ()))
            partial_parameters: list[OptionChainParameters] = []
            for row in observed_rows:
                try:
                    partial_parameters.append(chain_parameters_from_row(row))
                except Exception:
                    break
            completed = bool(getattr(error, "request_completed", False))
            failure_kind = str(getattr(error, "failure_kind", "callback-error"))
            raw_observed_at = getattr(error, "observed_at", None)
            observed_at = (
                raw_observed_at if isinstance(raw_observed_at, datetime) else datetime.now(tz=UTC)
            )
            response = OptionChainResponse(
                parameters=tuple(partial_parameters),
                complete=completed,
                truncated=bool(getattr(error, "truncated", False)),
                completion_marker=(
                    "securityDefinitionOptionParameterEnd"
                    if completed
                    else f"request-registry-{failure_kind}"
                ),
                observed_at=observed_at,
                source="ibapi-v1",
            )
            vars(error).update(
                chain_response=response,
                observed_values=response.parameters,
            )
            return error

        try:
            app.reqSecDefOptParams(request_id, underlying.symbol, "", "STK", underlying.con_id)
        except Exception as send_error:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"option-chain request for {underlying.symbol} could not be sent"
                ),
            )
            raise attach_chain_response(error) from send_error
        try:
            rows = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
        except asyncio.CancelledError as cancellation:
            error = self.registry.interrupt(
                request_id,
                error=MarketDataUnavailableError(
                    f"option-chain request for {underlying.symbol} was interrupted"
                ),
            )
            raise attach_chain_response(error) from cancellation
        except BrokerDataError as error:
            attach_chain_response(error)
            raise
        parameters: list[OptionChainParameters] = []
        try:
            for row in rows:
                parameters.append(chain_parameters_from_row(row))
        except BrokerDataError as parse_error:
            _attach_broker_observations(parse_error, parameters)
            vars(parse_error)["chain_response"] = OptionChainResponse(
                parameters=tuple(parameters),
                complete=True,
                truncated=False,
                completion_marker="securityDefinitionOptionParameterEnd",
                observed_at=datetime.now(tz=UTC),
                source="ibapi-v1",
            )
            raise
        return OptionChainResponse(
            parameters=tuple(parameters),
            complete=True,
            truncated=False,
            completion_marker="securityDefinitionOptionParameterEnd",
            observed_at=datetime.now(tz=UTC),
            source="ibapi-v1",
        )

    async def qualify_option_contracts(
        self,
        contracts: Sequence[OptionContractSpec],
    ) -> tuple[OptionContract, ...]:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        qualified: list[OptionContract] = []
        for spec in contracts:
            contract = Contract()
            contract.symbol = spec.symbol
            contract.secType = "OPT"
            contract.exchange = spec.exchange
            contract.currency = spec.currency
            contract.lastTradeDateOrContractMonth = spec.expiration.strftime("%Y%m%d")
            contract.strike = float(spec.strike)
            contract.right = spec.right.value[0]
            contract.multiplier = str(spec.multiplier)
            contract.tradingClass = spec.trading_class
            request_id = self.registry.open(
                max_items=MAX_CANDIDATE_REQUEST_CONTRACTS,
                sort_key=_contract_detail_key,
            )

            def attach_option_observations(error: BrokerDataError) -> BrokerDataError:
                observed = list(qualified)
                for detail in getattr(error, "observed_values", ()):
                    try:
                        instrument = _option_from_details(detail)
                    except BrokerDataError:
                        break
                    if instrument is not None:
                        observed.append(instrument)
                _attach_broker_observations(
                    error,
                    observed,
                    truncated=bool(getattr(error, "truncated", False)),
                    include_existing=False,
                )
                return error

            try:
                app.reqContractDetails(request_id, contract)
            except Exception as send_error:
                error = self.registry.interrupt(
                    request_id,
                    error=MarketDataUnavailableError(
                        f"option qualification request for {spec.symbol!r} could not be sent"
                    ),
                )
                raise attach_option_observations(error) from send_error
            try:
                details = await self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
            except asyncio.CancelledError as cancellation:
                error = self.registry.interrupt(
                    request_id,
                    error=MarketDataUnavailableError(
                        f"option qualification request for {spec.symbol!r} was interrupted"
                    ),
                )
                raise attach_option_observations(error) from cancellation
            except BrokerDataError as error:
                attach_option_observations(error)
                raise
            for detail in details:
                try:
                    instrument = _option_from_details(detail)
                except BrokerDataError as error:
                    _attach_broker_observations(error, qualified)
                    raise
                if instrument is not None:
                    qualified.append(instrument)
        return tuple(qualified)

    async def option_market_rules(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionMarketRule, ...]:
        """Read exact exchange-to-rule mappings and complete increment schedules."""

        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        if len({option.con_id for option in contracts}) != len(contracts):
            raise BrokerDataError("option market-rule request contains duplicate conIds")

        detail_requests: list[tuple[OptionContract, int]] = []
        try:
            for option in contracts:
                query = Contract()
                query.conId = option.con_id
                query.exchange = option.exchange
                request_id = self.registry.open(
                    max_items=MAX_CANDIDATE_REQUEST_CONTRACTS,
                    sort_key=_contract_detail_key,
                )
                detail_requests.append((option, request_id))
                app.reqContractDetails(request_id, query)
            detail_results = await asyncio.gather(
                *(
                    self.registry.wait(request_id, timeout=_REQUEST_TIMEOUT_S)
                    for _option, request_id in detail_requests
                ),
                return_exceptions=True,
            )
        except asyncio.CancelledError as cancellation:
            for _option, request_id in detail_requests:
                self.registry.abandon(request_id)
            raise MarketDataUnavailableError(
                "official IBKR market-rule contract-detail batch was interrupted",
                contract_ids=tuple(option.con_id for option, _request_id in detail_requests),
            ) from cancellation
        except BaseException:
            for _option, request_id in detail_requests:
                self.registry.abandon(request_id)
            raise

        rule_ids_by_contract: dict[int, int] = {}
        detail_errors: list[BaseException] = []
        for (option, _request_id), detail_result in zip(
            detail_requests, detail_results, strict=True
        ):
            if isinstance(detail_result, BaseException):
                detail_errors.append(detail_result)
                continue
            details = detail_result
            try:
                exact = [
                    detail
                    for detail in details
                    if int(getattr(getattr(detail, "contract", None), "conId", 0) or 0)
                    == option.con_id
                ]
                if len(exact) != 1:
                    raise BrokerDataError(
                        f"market-rule contract details for conId {option.con_id} are "
                        "missing or ambiguous"
                    )
                exchanges = tuple(
                    value.strip().upper()
                    for value in str(getattr(exact[0], "validExchanges", "") or "").split(",")
                    if value.strip()
                )
                raw_ids = tuple(
                    value.strip()
                    for value in str(getattr(exact[0], "marketRuleIds", "") or "").split(",")
                    if value.strip()
                )
                if len(exchanges) != len(raw_ids):
                    raise BrokerDataError(
                        f"market-rule routing metadata for conId {option.con_id} is incomplete"
                    )
                matching_ids = {
                    int(rule_id)
                    for exchange, rule_id in zip(exchanges, raw_ids, strict=True)
                    if exchange == option.exchange and int(rule_id) > 0
                }
                if len(matching_ids) != 1:
                    raise BrokerDataError(
                        f"market rule for conId {option.con_id} and {option.exchange} is "
                        "missing or ambiguous"
                    )
                rule_ids_by_contract[option.con_id] = matching_ids.pop()
            except ValueError:
                detail_errors.append(
                    BrokerDataError(f"market-rule identifier for conId {option.con_id} is invalid")
                )
            except BrokerDataError as error:
                detail_errors.append(error)
        if detail_errors:
            detail_failure = _preferred_broker_error(detail_errors)
            _attach_broker_observations(
                detail_failure,
                (),
                contract_ids=tuple(
                    option.con_id
                    for option, result in zip(contracts, detail_results, strict=True)
                    if isinstance(result, BaseException)
                    or option.con_id not in rule_ids_by_contract
                ),
            )
            raise detail_failure

        flights: dict[int, Any] = {}
        schedules: dict[int, tuple[tuple[Decimal, Decimal], ...]] = {}
        request_errors: dict[int, BaseException] = {}
        try:
            for rule_id in sorted(set(rule_ids_by_contract.values())):
                try:
                    flight = self.bridge.begin_market_rule(rule_id)
                    flights[rule_id] = flight
                    app.reqMarketRule(rule_id)
                except Exception as error:
                    if isinstance(error, BrokerDataError):
                        request_errors[rule_id] = error
                    else:
                        send_error = BrokerDataError(
                            f"market rule {rule_id} request could not be sent"
                        )
                        send_error.__cause__ = error
                        request_errors[rule_id] = send_error

            async def await_rule(rule_id: int, flight: Any) -> tuple[tuple[Decimal, Decimal], ...]:
                completed = await asyncio.to_thread(flight.done.wait, _REQUEST_TIMEOUT_S)
                if not completed or self.bridge.connection_closed.is_set():
                    error = BrokerDataError(f"market rule {rule_id} did not complete")
                    _attach_broker_observations(
                        error,
                        flight.increments or (),
                        truncated=bool(flight.truncated),
                    )
                    raise error
                return self.bridge.take_market_rule(rule_id, flight)

            rule_results = await asyncio.gather(
                *(
                    await_rule(rule_id, flight)
                    for rule_id, flight in flights.items()
                    if rule_id not in request_errors
                ),
                return_exceptions=True,
            )
            awaited_rule_ids = tuple(
                rule_id for rule_id in flights if rule_id not in request_errors
            )
            rule_errors: list[BaseException] = list(request_errors.values())
            failed_rule_ids = set(request_errors)
            for rule_id, rule_result in zip(awaited_rule_ids, rule_results, strict=True):
                if isinstance(rule_result, BaseException):
                    rule_errors.append(rule_result)
                    failed_rule_ids.add(rule_id)
                else:
                    schedules[rule_id] = rule_result
            if rule_errors:
                partial_rules = tuple(
                    OptionMarketRule(
                        con_id=option.con_id,
                        exchange=option.exchange,
                        market_rule_id=rule_ids_by_contract[option.con_id],
                        price_increments=tuple(
                            OptionPriceIncrement(low_edge=low, increment=increment)
                            for low, increment in schedules[rule_ids_by_contract[option.con_id]]
                        ),
                        source="ibkr-tws-market-rule-v1",
                    )
                    for option in contracts
                    if rule_ids_by_contract[option.con_id] in schedules
                )
                retained = tuple(
                    observation
                    for error in rule_errors
                    for observation in getattr(error, "observed_values", ())
                )
                rule_failure = _preferred_broker_error(rule_errors)
                _attach_broker_observations(
                    rule_failure,
                    (*partial_rules, *retained),
                    contract_ids=tuple(
                        option.con_id
                        for option in contracts
                        if rule_ids_by_contract[option.con_id] in failed_rule_ids
                    ),
                    truncated=any(
                        bool(getattr(error, "truncated", False)) for error in rule_errors
                    ),
                    include_existing=False,
                    limit=(MAX_CANDIDATE_REQUEST_CONTRACTS + MAX_OPTION_MARKET_RULE_INCREMENTS),
                )
                raise rule_failure
        except asyncio.CancelledError as cancellation:
            interrupted_schedules = dict(schedules)
            terminal_errors: list[BaseException] = []
            for rule_id, flight in flights.items():
                if not flight.done.is_set():
                    continue
                if flight.error is not None:
                    terminal_errors.append(flight.error)
                elif flight.increments is not None:
                    interrupted_schedules[rule_id] = flight.increments
            partial_rules = tuple(
                OptionMarketRule(
                    con_id=option.con_id,
                    exchange=option.exchange,
                    market_rule_id=rule_ids_by_contract[option.con_id],
                    price_increments=tuple(
                        OptionPriceIncrement(low_edge=low, increment=increment)
                        for low, increment in interrupted_schedules[
                            rule_ids_by_contract[option.con_id]
                        ]
                    ),
                    source="ibkr-tws-market-rule-v1",
                )
                for option in contracts
                if rule_ids_by_contract[option.con_id] in interrupted_schedules
            )
            incomplete_contract_ids = tuple(
                option.con_id
                for option in contracts
                if rule_ids_by_contract[option.con_id] not in interrupted_schedules
            )
            fallback = MarketDataUnavailableError(
                "official IBKR market-rule batch was interrupted",
                contract_ids=incomplete_contract_ids,
            )
            rule_failure = _preferred_broker_error((*terminal_errors, fallback))
            retained = tuple(
                observation
                for error in terminal_errors
                for observation in getattr(error, "observed_values", ())
            )
            _attach_broker_observations(
                rule_failure,
                (*partial_rules, *retained),
                contract_ids=incomplete_contract_ids,
                truncated=any(
                    bool(getattr(error, "truncated", False)) for error in terminal_errors
                ),
                include_existing=False,
                limit=(MAX_CANDIDATE_REQUEST_CONTRACTS + MAX_OPTION_MARKET_RULE_INCREMENTS),
            )
            vars(rule_failure).update(
                request_completed=False,
                failure_kind="interrupted",
            )
            raise rule_failure from cancellation
        finally:
            for rule_id, flight in flights.items():
                self.bridge.abandon_market_rule(rule_id, flight)

        return tuple(
            OptionMarketRule(
                con_id=option.con_id,
                exchange=option.exchange,
                market_rule_id=rule_ids_by_contract[option.con_id],
                price_increments=tuple(
                    OptionPriceIncrement(low_edge=low, increment=increment)
                    for low, increment in schedules[rule_ids_by_contract[option.con_id]]
                ),
                source="ibkr-tws-market-rule-v1",
            )
            for option in contracts
        )

    async def option_deliverable_facts(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[OptionDeliverableFacts, ...]:
        """Report the TWS API's deliverable limitation explicitly.

        ContractDetails exposes identifiers and a naming-based adjustment
        signal, but not the OCC deliverable schedule.  Returning UNKNOWN facts
        is the only honest result until an authoritative read-only source is
        integrated.
        """

        self._require_app()
        return tuple(
            OptionDeliverableFacts(
                con_id=contract.con_id,
                authoritative=False,
                source="ibkr-tws-no-deliverable-schedule-v1",
            )
            for contract in contracts
        )

    async def request_underlying_quote(self, contract: UnderlyingContract) -> MarketQuote:
        return await self._snapshot_quote(contract, con_id=contract.con_id, generic_ticks="")

    async def request_crypto_quote(self, contract: CryptoContract) -> MarketQuote:
        return await self._snapshot_quote(contract, con_id=contract.con_id, generic_ticks="")

    async def request_option_quotes(
        self,
        contracts: Sequence[OptionContract],
    ) -> tuple[MarketQuote, ...]:
        if not contracts:
            return ()
        if len({option.con_id for option in contracts}) != len(contracts):
            raise BrokerDataError("option quote request contains duplicate conIds")

        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        started: list[tuple[OptionContract, int]] = []
        quotes: tuple[MarketQuote, ...] = ()
        request_error: BaseException | None = None
        request_cancellation: asyncio.CancelledError | None = None

        def completed_quotes() -> tuple[MarketQuote, ...]:
            retained: list[MarketQuote] = []
            for option, request_id in started:
                state = self.bridge.quote_state(request_id)
                if state is None or not state.option_stream_complete or state.completed_at is None:
                    continue
                try:
                    retained.append(
                        quote_from_state(
                            state,
                            contract=option,
                            timestamp=state.completed_at,
                        )
                    )
                except Exception:
                    # Evidence collection must not mask the primary request
                    # failure or prevent cancellation cleanup.
                    continue
            return tuple(retained)

        try:
            # Register and send the whole bounded batch before awaiting any one
            # contract.  Streaming requests have no snapshot terminator; the
            # callback bridge completes each request only after every requested
            # right-specific field has produced a callback.
            for option in contracts:
                if option.con_id in self._active_market_data:
                    raise BrokerDataError(
                        f"market data is already active for conId {option.con_id}"
                    )
                query = Contract()
                query.conId = option.con_id
                query.exchange = option.exchange
                request_id = self.registry.open()
                self.bridge.open_option_quote(request_id, option.right)
                self._active_market_data[option.con_id] = request_id
                started.append((option, request_id))
                try:
                    app.reqMktData(
                        request_id,
                        query,
                        _OPTION_GENERIC_TICKS,
                        False,
                        False,
                        [],
                    )
                except Exception as error:
                    raise BrokerDataError(
                        f"option quote request for conId {option.con_id} could not be sent"
                    ) from error

            results = await asyncio.gather(
                *(
                    self.registry.wait(request_id, timeout=_QUOTE_TIMEOUT_S)
                    for _option, request_id in started
                ),
                return_exceptions=True,
            )
            normalized: list[MarketQuote] = []
            failures: list[BaseException] = []
            failed_contract_ids: list[int] = []
            for (option, request_id), result in zip(started, results, strict=True):
                if isinstance(result, BaseException):
                    failures.append(result)
                    failed_contract_ids.append(option.con_id)
                    continue
                state = self.bridge.quote_state(request_id)
                if state is None or not state.option_stream_complete or state.completed_at is None:
                    failures.append(
                        BrokerDataError(
                            f"option quote for conId {option.con_id} completed without "
                            "all required callbacks"
                        )
                    )
                    failed_contract_ids.append(option.con_id)
                    continue
                normalized.append(
                    quote_from_state(
                        state,
                        contract=option,
                        timestamp=state.completed_at,
                    )
                )
            if failures:
                quote_failure = _preferred_broker_error(failures)
                _attach_broker_observations(
                    quote_failure,
                    normalized,
                    contract_ids=failed_contract_ids,
                    include_existing=False,
                )
                raise quote_failure
            quotes = tuple(normalized)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                request_cancellation = error
                request_error = MarketDataUnavailableError(
                    "official IBKR option-quote batch was interrupted before completion"
                )
            else:
                request_error = error
            if isinstance(request_error, BrokerDataError):
                _attach_broker_observations(
                    request_error,
                    completed_quotes(),
                    contract_ids=tuple(
                        option.con_id
                        for option, request_id in started
                        if (
                            (state := self.bridge.quote_state(request_id)) is None
                            or not state.option_stream_complete
                            or state.completed_at is None
                        )
                    ),
                    include_existing=False,
                )

        cleanup_observations = completed_quotes()
        cleanup_error: BaseException | None = None
        try:
            await self.cancel_market_data(tuple(option.con_id for option, _request_id in started))
        except BaseException as error:
            cleanup_error = error
        finally:
            for _option, request_id in started:
                self.registry.abandon(request_id)
                self.bridge.close_quote(request_id)

        if cleanup_error is not None:
            _attach_broker_observations(
                cleanup_error,
                cleanup_observations,
                contract_ids=tuple(option.con_id for option, _request_id in started),
                include_existing=False,
            )
            if request_error is not None:
                raise cleanup_error from request_error
            raise cleanup_error
        if request_error is not None:
            if request_cancellation is not None:
                raise request_error from request_cancellation
            raise request_error
        return quotes

    async def _snapshot_quote(
        self, instrument: Instrument, *, con_id: int, generic_ticks: str
    ) -> MarketQuote:
        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        contract = Contract()
        contract.conId = con_id
        # Route by the instrument's own venue (ADR-0010 §6): STK/OPT use SMART,
        # crypto uses PAXOS/Zero Hash — a SMART-routed crypto quote request
        # fails or returns wrong data at IBKR.
        contract.exchange = getattr(instrument, "exchange", "SMART") or "SMART"
        request_id = self.registry.open()
        self.bridge.open_quote(request_id)
        request_error: BaseException | None = None
        request_cancellation: asyncio.CancelledError | None = None
        try:
            app.reqMktData(request_id, contract, generic_ticks, True, False, [])
            await self.registry.wait(request_id, timeout=_QUOTE_TIMEOUT_S)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                request_cancellation = error
                request_error = self.registry.interrupt(
                    request_id,
                    error=MarketDataUnavailableError(
                        f"official IBKR quote for conId {con_id} was interrupted",
                        contract_ids=(con_id,),
                    ),
                )
            else:
                request_error = error
        finally:
            state = self.bridge.close_quote(request_id)
            self.registry.abandon(request_id)
        if request_error is not None:
            if isinstance(request_error, BrokerDataError) and state is not None:
                observed_at = state.completed_at or datetime.now(tz=UTC)
                observed_quotes: tuple[MarketQuote, ...]
                try:
                    observed_quotes = (
                        quote_from_state(state, contract=instrument, timestamp=observed_at),
                    )
                except Exception:
                    observed_quotes = ()
                _attach_broker_observations(
                    request_error,
                    observed_quotes,
                    contract_ids=(con_id,),
                    observed_at=observed_at,
                    include_existing=False,
                )
            if request_cancellation is not None:
                raise request_error from request_cancellation
            raise request_error
        if state is None:
            raise BrokerDataError(f"no quote state for conId {con_id}")
        return quote_from_state(
            state,
            contract=instrument,
            timestamp=state.completed_at or datetime.now(tz=UTC),
        )

    async def cancel_market_data(self, contract_ids: Sequence[int]) -> None:
        requested = tuple(
            contract_id
            for contract_id in dict.fromkeys(contract_ids)
            if contract_id in self._active_market_data
        )
        if not requested:
            return
        app = self._require_app()
        failures: list[int] = []
        for contract_id in requested:
            request_id = self._active_market_data[contract_id]
            try:
                app.cancelMktData(request_id)
            except Exception:
                failures.append(contract_id)
                continue
            self._active_market_data.pop(contract_id, None)
            self.registry.abandon(request_id)
            self.bridge.close_quote(request_id)
        if failures:
            raise MarketDataCancellationError(
                "official IBKR could not cancel one or more market-data streams",
                contract_ids=tuple(failures),
                observed_at=datetime.now(tz=UTC),
            )

    async def historical_bars(
        self,
        contract: UnderlyingContract,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        lookback_days: int = 180,
    ) -> BarSeries:
        """One ``reqHistoricalData`` call, returned as closed bars.

        ``keepUpToDate=False`` and ``useRTH=1``: a streaming subscription would
        hold a request slot open against a pacing budget this connection shares
        with the order pipeline, and extended-hours prints would put bars on the
        chart that the regular-session close does not reflect.

        ``formatDate=1`` asks IBKR for ``YYYYMMDD`` strings on daily bars, which
        is what :func:`_parse_ibkr_bar` expects. A bar that does not parse is
        **dropped with a warning rather than guessed at** — a chart missing a day
        is visibly wrong, while a chart with a fabricated day is not.
        """

        app = self._require_app()
        _, _, Contract, _ = _load_ibapi()
        if lookback_days < 1:
            raise BrokerDataError("lookback_days must be at least 1")
        bar_size = _IBKR_BAR_SIZE.get(interval)
        if bar_size is None:
            raise BrokerDataError(f"unsupported bar interval {interval.value}")

        ib_contract = Contract()
        ib_contract.conId = contract.con_id
        ib_contract.exchange = contract.exchange or "SMART"

        request_id = self.registry.open()
        app.reqHistoricalData(
            request_id,
            ib_contract,
            "",  # endDateTime empty = up to now
            f"{lookback_days} D",
            bar_size,
            "TRADES",
            1,  # useRTH
            1,  # formatDate
            False,  # keepUpToDate
            [],
        )
        raw = await self.registry.wait(request_id, timeout=_HISTORICAL_TIMEOUT_S)

        bars: list[Bar] = []
        for item in raw:
            parsed = _parse_ibkr_bar(item, contract=contract, interval=interval)
            if parsed is None:
                _logger.warning(
                    "Dropped an unparseable historical bar for %s",
                    contract.symbol,
                    extra={"event": "historical_bar_unparseable"},
                )
                continue
            bars.append(parsed)
        bars.sort(key=lambda bar: bar.timestamp_utc)
        return BarSeries(symbol=contract.symbol, interval=interval, bars=tuple(bars))

    # ------------------------------------------------------------------ #
    # Order surface (M7, ADR-0009 §8). Wiring is validated against a fake
    # ibapi application object plus the boundary-level recording spy; the
    # owner performs gateway verification against a running gateway. All
    # LOCAL refusals raise BrokerRefusedBeforeSend — a proof claim that no
    # bytes reached the gateway socket — so the boundary can resolve the
    # intent to REJECTED synchronously instead of stranding it.
    # ------------------------------------------------------------------ #

    def _reverify_for_order_path(self, *, account_id: str, mutating: bool) -> Any:
        """Local, pre-send re-verification shared by the order methods."""

        app = self._app
        if not self._app_connected():
            # Local, provable pre-send fact: a dead session sends nothing
            # (review finding H3 — refusing here prevents a stranded
            # SUBMISSION_UNKNOWN for an order that could never leave).
            raise BrokerRefusedBeforeSend("not connected; no order call was sent")
        verify_environment_port(self._settings.ib_environment, self._settings.ib_port)
        expected = self._settings.ib_account_id
        if account_id != expected:
            raise BrokerRefusedBeforeSend(
                "order account does not match the configured account; refusing before send"
            )
        if expected not in self.bridge.managed_accounts:
            raise BrokerRefusedBeforeSend(
                "connected session does not manage the configured account; refusing before send"
            )
        if mutating and self._live_kill_switch is not None and self._live_kill_switch.is_engaged():
            # Last-line breaker (ADR-0009 §8): covers engage-after-CAS races and
            # any hypothetical non-boundary caller holding this broker object.
            raise BrokerRefusedBeforeSend(
                "live kill switch is ENGAGED; refusing the order call before send"
            )
        return app

    def _build_order_objects(self, request: OrderRequest, *, what_if: bool) -> tuple[Any, Any]:
        _, _, Contract, _ = _load_ibapi()
        Order = _load_order_class()
        con_id = getattr(request.contract, "con_id", None)
        if not con_id:
            raise BrokerRefusedBeforeSend("contract is not qualified (no con_id); refusing")
        contract = Contract()
        contract.conId = int(con_id)
        # Exchange from the qualified contract: STK/OPT route SMART, CRYPTO
        # carries its venue (PAXOS/Zero Hash) — never hardcoded (ADR-0010 §6).
        contract.exchange = getattr(request.contract, "exchange", "SMART") or "SMART"
        order = Order()
        order.action = request.side.value
        order.orderType = "LMT"  # market orders are impossible by construction
        # Decimal-preserving (ADR-0010 §6): int() would truncate 0.005 BTC to 0.
        # ibapi Order.totalQuantity is Decimal in TWS API >= 10.10 (the crypto
        # precondition); integral option/stock quantities round-trip unchanged.
        order.totalQuantity = request.quantity
        order.lmtPrice = float(request.limit_price)
        # TIF mapped from the request (ADR-0010 §3): DAY for options/stocks,
        # the owner-verified crypto TIF for crypto.
        order.tif = request.time_in_force
        # Defense in depth (adversarial-review F4): outside-RTH requires BOTH the
        # request flag AND the owner setting. The setting is the authority, so no
        # caller — present or future — can transmit outside regular trading hours
        # unless the owner explicitly enabled it. Clamps to the safe RTH default.
        order.outsideRth = bool(request.outside_rth) and self._settings.allow_outside_rth
        order.account = request.account_id
        order.orderRef = request.order_ref
        order.whatIf = bool(what_if)
        # The transmit flag is MAPPED from the request (never assigned a
        # literal True here): only the submission boundary builds transmit=True.
        order.transmit = bool(request.transmit) and not what_if
        return contract, order

    async def _await_order_ack(self, order_id: int) -> tuple[Any, ...]:
        flight = self.bridge.start_order_ack(order_id)
        try:
            done = await asyncio.get_running_loop().run_in_executor(
                None, flight.done.wait, _REQUEST_TIMEOUT_S
            )
            if not done or self.bridge.connection_closed.is_set():
                raise BrokerConnectionError(
                    f"no broker acknowledgement for order {order_id} within {_REQUEST_TIMEOUT_S}s"
                )
            if not flight.items:
                raise BrokerDataError(f"order {order_id} acknowledgement carried no payload")
            return tuple(flight.items[0])
        finally:
            self.bridge.clear_order_ack(order_id)

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        app = self._reverify_for_order_path(account_id=request.account_id, mutating=False)
        if request.transmit:
            raise BrokerRefusedBeforeSend("previews must never carry transmit=True; refusing")
        contract, order = self._build_order_objects(request, what_if=True)
        try:
            order_id = self.order_ids.allocate()
        except RuntimeError as error:  # pre-send local fact (finding L1)
            raise BrokerRefusedBeforeSend(f"order-id allocator not seeded: {error}") from error
        # Register the ack BEFORE placeOrder — the reader thread can answer in
        # the gap otherwise (finding M4; mirrors submit_order).
        flight = self.bridge.start_order_ack(order_id)
        try:
            app.placeOrder(order_id, contract, order)
            done = await asyncio.get_running_loop().run_in_executor(
                None, flight.done.wait, _REQUEST_TIMEOUT_S
            )
            if not done or not flight.items:
                raise BrokerConnectionError(f"no whatIf acknowledgement for order {order_id}")
            ack = tuple(flight.items[0])
        finally:
            self.bridge.clear_order_ack(order_id)
        if ack[0] == "error":
            # The gateway answered the whatIf with an error: a declined preview,
            # not acceptance evidence (finding H2).
            return OrderPreview(
                request=request,
                accepted=False,
                warnings=(f"[{ack[2]}] {ack[3]}",),
                broker_message=str(ack[3]),
                previewed_at=datetime.now(tz=UTC),
            )
        if ack[0] != "openOrder":
            raise BrokerDataError("whatIf preview did not return an order state")
        order_state = ack[4]
        commission = clean_price(_maybe_float(getattr(order_state, "commission", None)))
        # Fail-closed acceptance (review finding M1): only known-good statuses
        # count; an absent/empty/unknown status is NOT acceptance evidence.
        accepted = str(getattr(order_state, "status", "")).lower() in {"presubmitted", "submitted"}
        return OrderPreview(
            request=request,
            accepted=accepted,
            estimated_commission=commission,
            initial_margin_change=clean_price(
                _maybe_float(getattr(order_state, "initMarginChange", None))
            ),
            maintenance_margin_change=clean_price(
                _maybe_float(getattr(order_state, "maintMarginChange", None))
            ),
            equity_with_loan_change=clean_price(
                _maybe_float(getattr(order_state, "equityWithLoanChange", None))
            ),
            warnings=tuple(
                warning
                for warning in (str(getattr(order_state, "warningText", "") or "").strip(),)
                if warning
            ),
            broker_message=str(getattr(order_state, "status", "") or ""),
            previewed_at=datetime.now(tz=UTC),
        )

    async def submit_order(
        self,
        request: OrderRequest,
        *,
        send_guard: BrokerSendGuard | None = None,
    ) -> OrderSubmission:
        app = self._reverify_for_order_path(account_id=request.account_id, mutating=True)
        if not request.transmit:
            raise BrokerRefusedBeforeSend(
                "submit_order requires an explicitly transmit-authorized request "
                "from the submission boundary; refusing before send"
            )
        if send_guard is None:
            raise BrokerRefusedBeforeSend(
                "submission is missing atomic reconciliation authorization; refusing before send"
            )
        contract, order = self._build_order_objects(request, what_if=False)
        try:
            order_id = self.order_ids.allocate()
        except RuntimeError as error:  # pre-send local fact (finding L1)
            raise BrokerRefusedBeforeSend(f"order-id allocator not seeded: {error}") from error
        flight = self.bridge.start_order_ack(order_id)
        post_send_message = ""
        payload: tuple[Any, ...]
        try:
            with send_guard.at_send() as authorized:
                if not authorized:
                    raise BrokerRefusedBeforeSend(
                        "reconciliation authorization changed; refusing before send"
                    )
                kill_guard = (
                    self._live_kill_switch.at_send()
                    if self._live_kill_switch is not None
                    else contextlib.nullcontext(True)
                )
                with kill_guard as kill_authorized:
                    if not kill_authorized:
                        raise BrokerRefusedBeforeSend(
                            "live kill switch engaged; refusing before send"
                        )
                    try:
                        app.placeOrder(order_id, contract, order)
                    except Exception:
                        # Once the send call starts, conservatively retain the
                        # allocated identity even if the client raises.
                        post_send_message = "broker send raised; state unknown"
                        flight.done.set()
            done = await asyncio.get_running_loop().run_in_executor(
                None, flight.done.wait, _REQUEST_TIMEOUT_S
            )
            # A captured ack outranks a subsequent connection close (finding L3).
            if not done or (not flight.items and self.bridge.connection_closed.is_set()):
                # Bytes may have left. Preserve the allocated identity so the
                # boundary can persist it as unknown for reconciliation.
                payload = ()
                post_send_message = "no broker acknowledgement; state unknown"
            else:
                payload = tuple(flight.items[0]) if flight.items else ()
        finally:
            self.bridge.clear_order_ack(order_id)
        raw_status = ""
        perm_id: int | None = None
        filled = Decimal("0")
        remaining = Decimal(request.quantity)
        if payload and payload[0] == "error":
            # The gateway answered placeOrder with an error callback (finding
            # H2). placeOrder WAS called, so this is post-send territory: a
            # definitive venue reject maps to REJECTED; anything ambiguous
            # stays unknown for reconciliation.
            code = int(payload[2])
            if code in _DEFINITIVE_ORDER_REJECT_CODES:
                raw_status = "Inactive"
            else:
                post_send_message = f"ambiguous broker error {code}; state unknown"
        elif payload and payload[0] == "orderStatus":
            raw_status = str(payload[2])
            perm_id = int(payload[5]) or None
            filled = Decimal(str(payload[3]))
            remaining = Decimal(str(payload[4]))
        elif payload and payload[0] == "openOrder":
            raw_status = str(getattr(payload[4], "status", "") or "")
            perm_id = int(getattr(payload[3], "permId", 0) or 0) or None
        lifecycle = broker_status_to_lifecycle(
            raw_status, filled_quantity=filled, remaining_quantity=remaining
        )
        return OrderSubmission(
            correlation_id=request.correlation_id,
            broker_order_id=order_id,
            permanent_id=perm_id,
            client_id=self._settings.ib_client_id,
            lifecycle=lifecycle,
            submitted_at=datetime.now(tz=UTC),
            message=post_send_message,
        )

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        # The LIVE pipeline refuses modifies upstream (ADR-0009 §7). The paper
        # pipeline's modify path currently runs through the ib_async adapter;
        # this adapter defers modify-in-place, and refuses provably-before-send
        # so nothing is ever stranded.
        del request
        self._reverify_for_order_path(account_id=self._settings.ib_account_id, mutating=True)
        raise BrokerRefusedBeforeSend(
            "modify-in-place is not implemented for the official adapter in M7; "
            "cancel and re-submit through the boundary instead (nothing was sent)"
        )

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        # Deliberately NOT kill-switch gated: cancellation is risk-reducing and
        # must work during an emergency stop (ADR-0009 §7).
        app = self._app
        if not self._app_connected():
            raise BrokerRefusedBeforeSend("not connected; no cancel was sent")
        verify_environment_port(self._settings.ib_environment, self._settings.ib_port)
        flight = self.bridge.start_order_ack(broker_order_id)
        try:
            app.cancelOrder(broker_order_id, "")
            done = await asyncio.get_running_loop().run_in_executor(
                None, flight.done.wait, _REQUEST_TIMEOUT_S
            )
            payload = tuple(flight.items[0]) if done and flight.items else ()
        finally:
            self.bridge.clear_order_ack(broker_order_id)
        raw_status = str(payload[2]) if payload and payload[0] == "orderStatus" else ""
        lowered = raw_status.lower()
        if lowered in {"cancelled", "apicancelled"}:
            lifecycle = OrderLifecycle.CANCELLED
        elif not raw_status or lowered == "pendingcancel":
            lifecycle = OrderLifecycle.CANCEL_PENDING
        else:
            lifecycle = broker_status_to_lifecycle(
                raw_status, filled_quantity=Decimal("0"), remaining_quantity=Decimal("0")
            )
        return CancellationResult(
            broker_order_id=broker_order_id,
            requested=True,
            lifecycle=lifecycle,
            message=raw_status or "cancel requested; awaiting broker confirmation",
            timestamp=datetime.now(tz=UTC),
        )

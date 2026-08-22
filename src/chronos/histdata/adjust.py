"""Read-time price/volume adjustment for the historical-data plane (ADR-0011 §4).

The store keeps **unadjusted** as-traded bars plus a corporate-action event stream;
adjusted and total-return views are derived here at read time and **never written
back**, so the hash-pinned unadjusted series is the single source of truth and a
later-discovered action changes only the derived view.

Method — one pass over the bars, applying a *cumulative* back-adjustment factor
computed newest→oldest (the CRSP/vendor convention: the most recent bar keeps its
as-traded price, history is scaled to be continuous with it):

- **SPLIT** (``SPLIT_ADJUSTED`` and ``TOTAL_RETURN``): for bars strictly before the
  ex-date, price ``* 1/r`` and volume ``* r``.
- **CASH_DIVIDEND** (``TOTAL_RETURN`` only): for bars strictly before the ex-date,
  price ``* (1 - d/C_ref)`` where ``C_ref`` is the **unadjusted** close on the last
  trading day strictly before the ex-date; volume unchanged.

Guards from the design panel (ADR-0011 §11): a future-dated action (ex-date after
the last bar) is skipped so the newest bar stays as-traded (D3); a dividend with no
preceding bar is skipped (D1); ``d ≥ C_ref`` is rejected — the view never emits a
non-positive price (D1); ``C_ref``/``d`` are read from the unadjusted series so the
factor is a scale-free ratio and split-plus-dividend compose correctly in one pass.
Because ``d`` and ``C_ref`` share the unadjusted basis, ``TOTAL_RETURN`` is the
adjusted-close *approximation* to a reinvested total return, not an exact TR index.

Pure and deterministic — no clock, no I/O — so it is golden-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from chronos.histdata.corporate_actions import ActionKind, CorporateAction
from chronos.marketdata.bars import Bar, BarInterval, BarSeries

_PRICE_DECIMALS = 6
_SUSPICIOUS_DIVIDEND_YIELD = 0.5


class AdjustmentView(StrEnum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


class AdjustmentError(ValueError):
    """The adjustment would emit an invalid (non-positive) price; fails closed."""


@dataclass(frozen=True, slots=True)
class _Factor:
    ex_date: date
    price_mult: float
    volume_mult: float


@dataclass(frozen=True, slots=True)
class AdjustmentResult:
    """The derived view plus any warnings raised while building it."""

    series: BarSeries
    warnings: tuple[str, ...] = ()


def adjust_series(
    series: BarSeries,
    actions: Iterable[CorporateAction],
    view: AdjustmentView,
) -> AdjustmentResult:
    """Derive ``view`` from an unadjusted ``series`` and its corporate actions.

    Daily series only, for a reason worth keeping in view: the dividend factor's
    reference price is "the unadjusted close on the last trading day strictly
    before the ex-date" — an official daily closing print. Over an hourly series
    ``_close_before`` would silently substitute the last HOURLY close (a
    last-hour trade, not the auction close), and every dividend factor would
    drift from the daily-derived truth. Hourly adjusted views therefore refuse
    until they can reference the daily series for C_ref (ADR-0029 defers this
    deliberately; the hourly research lane consumes unadjusted bars).
    """

    bars = series.bars
    if series.interval is not BarInterval.DAY_1 and view is not AdjustmentView.RAW:
        raise AdjustmentError(
            f"{series.symbol}: adjusted views are defined over daily series only — "
            f"a {series.interval} series has no official closing print to anchor "
            "dividend factors (ADR-0029)"
        )
    if not bars or view is AdjustmentView.RAW:
        return AdjustmentResult(series)

    last_date = bars[-1].session_date
    factors, warnings = _build_factors(bars, actions, view, last_date)
    if not factors:
        return AdjustmentResult(series, tuple(warnings))

    adjusted = tuple(_apply(bar, factors, series.symbol) for bar in bars)
    return AdjustmentResult(
        BarSeries(symbol=series.symbol, interval=series.interval, bars=adjusted),
        tuple(warnings),
    )


def _build_factors(
    bars: tuple[Bar, ...],
    actions: Iterable[CorporateAction],
    view: AdjustmentView,
    last_date: date,
) -> tuple[list[_Factor], list[str]]:
    factors: list[_Factor] = []
    warnings: list[str] = []
    # Deterministic order so warning text and logging are reproducible.
    for action in sorted(actions, key=lambda a: (a.ex_date, a.kind.value, a.value)):
        if action.ex_date > last_date:
            warnings.append(
                f"skipped future-dated {action.kind.value} ex_date={action.ex_date.isoformat()} "
                f"(after last bar {last_date.isoformat()})"
            )
            continue
        if action.kind is ActionKind.SPLIT:
            factors.append(_Factor(action.ex_date, 1.0 / action.value, action.value))
        elif view is AdjustmentView.TOTAL_RETURN:  # CASH_DIVIDEND, total-return only
            factor = _dividend_factor(bars, action, warnings)
            if factor is not None:
                factors.append(factor)
    return factors, warnings


def _dividend_factor(
    bars: tuple[Bar, ...], action: CorporateAction, warnings: list[str]
) -> _Factor | None:
    c_ref = _close_before(bars, action.ex_date)
    if c_ref is None:
        warnings.append(
            f"skipped dividend ex_date={action.ex_date.isoformat()}: no bar before ex-date"
        )
        return None
    ratio = action.value / c_ref
    if ratio >= 1.0:
        warnings.append(
            f"rejected dividend ex_date={action.ex_date.isoformat()}: "
            f"d={action.value} >= C_ref={c_ref} (non-positive factor)"
        )
        return None
    if ratio > _SUSPICIOUS_DIVIDEND_YIELD:
        warnings.append(
            f"suspicious dividend ex_date={action.ex_date.isoformat()}: "
            f"d/C_ref={ratio:.3f} exceeds {_SUSPICIOUS_DIVIDEND_YIELD}"
        )
    return _Factor(action.ex_date, 1.0 - ratio, 1.0)


def _close_before(bars: tuple[Bar, ...], ex_date: date) -> float | None:
    """Unadjusted close of the last bar strictly before ``ex_date`` (bars ascending)."""

    reference: float | None = None
    for bar in bars:
        if bar.session_date < ex_date:
            reference = bar.close
        else:
            break
    return reference


def _apply(bar: Bar, factors: list[_Factor], symbol: str) -> Bar:
    price_mult = 1.0
    volume_mult = 1.0
    for factor in factors:
        if bar.session_date < factor.ex_date:
            price_mult *= factor.price_mult
            volume_mult *= factor.volume_mult

    open_ = round(bar.open * price_mult, _PRICE_DECIMALS)
    high = round(bar.high * price_mult, _PRICE_DECIMALS)
    low = round(bar.low * price_mult, _PRICE_DECIMALS)
    close = round(bar.close * price_mult, _PRICE_DECIMALS)
    if min(open_, high, low, close) <= 0.0:
        raise AdjustmentError(
            f"{symbol} {bar.session_date.isoformat()}: adjustment produced a "
            f"non-positive price (factor={price_mult})"
        )
    return replace(
        bar,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=float(round(bar.volume * volume_mult)),
        adjusted_close=None,
    )

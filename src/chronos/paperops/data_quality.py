"""Paper-path market-data quality gates (fail closed for trade authorization).

Degraded data is always labeled. Trade-permitting paper opens require
``may_authorize_open is True``. Labeled degradation never silently authorizes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chronos.paperops.reasons import PaperReasonCode


class DataDegradation(StrEnum):
    NONE = "none"
    STALE = "stale"
    MISSING = "missing"
    CROSSED = "crossed"
    NONSENSICAL = "nonsensical"
    INVALID_GREEKS = "invalid_greeks_or_iv"
    CLOCK_ANOMALY = "clock_anomaly"
    DELAYED = "delayed"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """Minimal quote evidence for paper decision quality checks."""

    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    quote_utc: datetime | None
    source: str
    quality_label: str  # e.g. LIVE, DELAYED, DEMO, STALE, UNKNOWN
    # Options / Greeks (optional; absence is fine for stocks)
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    require_greeks: bool = False


@dataclass(frozen=True, slots=True)
class PaperDataHealth:
    ok: bool
    may_authorize_open: bool
    reason_code: PaperReasonCode
    degradation: DataDegradation
    label: str
    detail: str
    data_timestamp_utc: str | None
    data_source: str

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "may_authorize_open": self.may_authorize_open,
            "reason_code": self.reason_code.value,
            "degradation": self.degradation.value,
            "label": self.label,
            "detail": self.detail,
            "data_timestamp_utc": self.data_timestamp_utc,
            "data_source": self.data_source,
        }


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def evaluate_paper_quote(
    quote: QuoteSnapshot,
    *,
    now_utc: datetime,
    max_quote_age_seconds: float,
    allow_delayed_as_informational: bool = True,
) -> PaperDataHealth:
    """Assess whether a quote may authorize a paper open.

    Delayed/synthetic/demo quotes are visible and labeled but never authorize
    opens unless quality_label is LIVE (or explicitly modeled — not in this MVP).
    """

    source = (quote.source or "").strip() or "missing"
    ts = quote.quote_utc.isoformat() if quote.quote_utc is not None else None
    label = (quote.quality_label or "UNKNOWN").upper()

    def deny(code: PaperReasonCode, degradation: DataDegradation, detail: str) -> PaperDataHealth:
        return PaperDataHealth(
            ok=False,
            may_authorize_open=False,
            reason_code=code,
            degradation=degradation,
            label=label,
            detail=detail,
            data_timestamp_utc=ts,
            data_source=source,
        )

    if source == "missing" or quote.quote_utc is None:
        return deny(
            PaperReasonCode.DATA_MISSING,
            DataDegradation.MISSING,
            "quote timestamp or source is missing",
        )

    if quote.quote_utc.tzinfo is None:
        return deny(
            PaperReasonCode.DATA_CLOCK_ANOMALY,
            DataDegradation.CLOCK_ANOMALY,
            "quote timestamp is not timezone-aware",
        )
    if now_utc.tzinfo is None:
        return deny(
            PaperReasonCode.DATA_CLOCK_ANOMALY,
            DataDegradation.CLOCK_ANOMALY,
            "evaluation clock is not timezone-aware",
        )
    if quote.quote_utc > now_utc:
        return deny(
            PaperReasonCode.DATA_CLOCK_ANOMALY,
            DataDegradation.CLOCK_ANOMALY,
            "quote timestamp "
            f"{quote.quote_utc.isoformat()} is in the future vs {now_utc.isoformat()}",
        )

    age = (now_utc - quote.quote_utc).total_seconds()
    if max_quote_age_seconds <= 0 or age > max_quote_age_seconds:
        return deny(
            PaperReasonCode.DATA_STALE,
            DataDegradation.STALE,
            f"quote age {age:.0f}s exceeds max {max_quote_age_seconds:.0f}s",
        )

    bid, ask, last = quote.bid, quote.ask, quote.last
    if bid is None and ask is None and last is None:
        return deny(
            PaperReasonCode.DATA_MISSING,
            DataDegradation.MISSING,
            "bid, ask, and last are all missing",
        )

    for name, value in (("bid", bid), ("ask", ask), ("last", last)):
        if value is not None and (not math.isfinite(value) or value < 0):
            return deny(
                PaperReasonCode.DATA_NONSENSICAL,
                DataDegradation.NONSENSICAL,
                f"{name} is non-finite or negative: {value!r}",
            )

    if bid is not None and ask is not None:
        if not _finite_positive(bid) or not _finite_positive(ask):
            return deny(
                PaperReasonCode.DATA_NONSENSICAL,
                DataDegradation.NONSENSICAL,
                f"bid/ask not strictly positive: bid={bid} ask={ask}",
            )
        if bid > ask:
            return deny(
                PaperReasonCode.DATA_CROSSED,
                DataDegradation.CROSSED,
                f"crossed market: bid {bid} > ask {ask}",
            )
        if ask <= 0:
            return deny(
                PaperReasonCode.DATA_NONSENSICAL,
                DataDegradation.NONSENSICAL,
                f"ask is not positive: {ask}",
            )

    if last is not None and not _finite_positive(last):
        return deny(
            PaperReasonCode.DATA_NONSENSICAL,
            DataDegradation.NONSENSICAL,
            f"last price is not strictly positive: {last}",
        )

    if quote.require_greeks:
        greeks = {
            "iv": quote.iv,
            "delta": quote.delta,
            "gamma": quote.gamma,
            "theta": quote.theta,
            "vega": quote.vega,
        }
        missing = [k for k, v in greeks.items() if v is None]
        if missing:
            return deny(
                PaperReasonCode.DATA_INVALID_GREEKS,
                DataDegradation.INVALID_GREEKS,
                f"required option fields missing: {', '.join(missing)}",
            )
        if quote.iv is not None and (not _finite(quote.iv) or quote.iv < 0):
            return deny(
                PaperReasonCode.DATA_INVALID_GREEKS,
                DataDegradation.INVALID_GREEKS,
                f"IV is invalid: {quote.iv!r}",
            )
        if quote.delta is not None and (
            not _finite(quote.delta) or quote.delta < -1.0 or quote.delta > 1.0
        ):
            return deny(
                PaperReasonCode.DATA_INVALID_GREEKS,
                DataDegradation.INVALID_GREEKS,
                f"delta out of range: {quote.delta!r}",
            )
        for name, value in (
            ("gamma", quote.gamma),
            ("vega", quote.vega),
        ):
            if value is not None and not _finite(value):
                return deny(
                    PaperReasonCode.DATA_INVALID_GREEKS,
                    DataDegradation.INVALID_GREEKS,
                    f"{name} is non-finite: {value!r}",
                )

    # Quality label gate: only LIVE authorizes opens. Everything else is labeled
    # and non-authorizing (brutal clarity on synthetic/delayed/demo data).
    if label in {"DEMO", "SYNTHETIC"}:
        return PaperDataHealth(
            ok=True,
            may_authorize_open=False,
            reason_code=PaperReasonCode.DATA_DEGRADED_LABELED,
            degradation=DataDegradation.SYNTHETIC,
            label=label,
            detail="synthetic/demo data is visible but never authorizes paper opens",
            data_timestamp_utc=ts,
            data_source=source,
        )
    if label in {"DELAYED", "DELAYED_FROZEN", "FROZEN", "STALE"}:
        return PaperDataHealth(
            ok=True,
            may_authorize_open=False,
            reason_code=PaperReasonCode.DATA_DEGRADED_LABELED,
            degradation=(DataDegradation.STALE if label == "STALE" else DataDegradation.DELAYED),
            label=label,
            detail=(
                f"quality_label={label} is degraded; "
                + (
                    "informational only"
                    if allow_delayed_as_informational
                    else "blocks authorization"
                )
            ),
            data_timestamp_utc=ts,
            data_source=source,
        )
    if label in {"UNKNOWN", ""}:
        return deny(
            PaperReasonCode.DATA_QUALITY_UNKNOWN,
            DataDegradation.UNKNOWN,
            "data quality is UNKNOWN; refusing trade-permitting decision",
        )
    if label != "LIVE":
        return deny(
            PaperReasonCode.DATA_DEGRADED_LABELED,
            DataDegradation.UNKNOWN,
            f"unrecognized or non-authorizing quality_label={label}",
        )

    return PaperDataHealth(
        ok=True,
        may_authorize_open=True,
        reason_code=PaperReasonCode.DATA_OK,
        degradation=DataDegradation.NONE,
        label=label,
        detail="quote quality acceptable for paper open authorization",
        data_timestamp_utc=ts,
        data_source=source,
    )

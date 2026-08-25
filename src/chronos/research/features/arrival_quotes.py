"""Causal arrival-quote evidence for a blocked Five-Tool intraday plan.

This module is a technical research primitive, not a companion selection or a
trading gate.  It aligns an explicit entry-admission instant to the latest
same-symbol quote that was both source-timestamped and received by that
instant.  It measures quote age and relative quoted spread only when the quote
is valid.  It never applies a threshold, masks an intent, opens a dataset,
registers a trial, or imports runtime authority.

Effective spread, realized spread, fills, and later mark-outs deliberately do
not appear in this vocabulary: those are outcome labels, not causal admission
inputs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Any

from chronos.research.features.models import FeatureInputError, canonical_digest

ARRIVAL_QUOTE_PLAN_SCHEMA = "chronos-five-tool-arrival-quote-evidence-plan-v1"
ARRIVAL_QUOTE_PLAN_ID = "five-tool-intraday-arrival-quote-evidence-001"
ARRIVAL_QUOTE_EXECUTION_STATE = "blocked_before_certified_quote_data"
ARRIVAL_QUOTE_PINE_SHA256 = "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f"
_TRADABLE_SYMBOLS = ("GLD", "IWM", "QQQ")


class QuoteCondition(StrEnum):
    """Owner-normalized quote condition; only ``NORMAL`` is measurable."""

    NORMAL = "NORMAL"
    HALTED = "HALTED"
    LIMIT_STATE = "LIMIT_STATE"
    UNKNOWN = "UNKNOWN"


class ArrivalQuoteStatus(StrEnum):
    """Fail-closed measurement outcome at one admission point."""

    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    EMPTY = "empty"
    LOCKED = "locked"
    CROSSED = "crossed"
    INELIGIBLE_CONDITION = "ineligible_condition"


def _aware_utc(timestamp: datetime, label: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise FeatureInputError(f"{label} must be timezone-aware")
    return timestamp.astimezone(UTC)


def _normalized_symbol(symbol: str, label: str) -> str:
    normalized = symbol.strip().upper()
    if not normalized:
        raise FeatureInputError(f"{label} symbol is required")
    return normalized


def _finite_decimal(value: Decimal, label: str) -> Decimal:
    if not value.is_finite():
        raise FeatureInputError(f"{label} must be finite")
    return value


def _positive_decimal(value: Decimal | int | str, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FeatureInputError(f"{label} must be a finite positive number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise FeatureInputError(f"{label} must be a finite positive number")
    return parsed


@dataclass(frozen=True, slots=True)
class AdmissionPoint:
    """The last explicit instant at which one Five-Tool entry may be withheld."""

    symbol: str
    opportunity_timestamp_utc: datetime
    admission_timestamp_utc: datetime
    primary_sequence_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalized_symbol(self.symbol, "admission"))
        opportunity = _aware_utc(self.opportunity_timestamp_utc, "opportunity_timestamp_utc")
        admission = _aware_utc(self.admission_timestamp_utc, "admission_timestamp_utc")
        if opportunity > admission:
            raise FeatureInputError("opportunity timestamp cannot follow admission timestamp")
        if not self.primary_sequence_id.strip():
            raise FeatureInputError("primary_sequence_id is required")
        object.__setattr__(self, "opportunity_timestamp_utc", opportunity)
        object.__setattr__(self, "admission_timestamp_utc", admission)


@dataclass(frozen=True, slots=True)
class ArrivalQuote:
    """One identity-bound NBBO observation as seen by the research recorder."""

    symbol: str
    bid: Decimal
    ask: Decimal
    source_timestamp_utc: datetime
    received_timestamp_utc: datetime
    source_sequence_id: str
    source: str
    condition: QuoteCondition = QuoteCondition.NORMAL

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalized_symbol(self.symbol, "quote"))
        object.__setattr__(self, "bid", _finite_decimal(self.bid, "bid"))
        object.__setattr__(self, "ask", _finite_decimal(self.ask, "ask"))
        try:
            condition = QuoteCondition(self.condition)
        except ValueError as error:
            raise FeatureInputError("quote condition is not recognized") from error
        source_timestamp = _aware_utc(self.source_timestamp_utc, "source_timestamp_utc")
        received_timestamp = _aware_utc(self.received_timestamp_utc, "received_timestamp_utc")
        if source_timestamp > received_timestamp:
            raise FeatureInputError("source timestamp cannot follow receipt timestamp")
        if not self.source_sequence_id.strip():
            raise FeatureInputError("source_sequence_id is required")
        if not self.source.strip():
            raise FeatureInputError("quote source identity is required")
        object.__setattr__(self, "source_timestamp_utc", source_timestamp)
        object.__setattr__(self, "received_timestamp_utc", received_timestamp)
        object.__setattr__(self, "condition", condition)


@dataclass(frozen=True, slots=True)
class ArrivalQuoteEvidence:
    """Causal measurement at one admission point; never an entry decision."""

    admission: AdmissionPoint
    status: ArrivalQuoteStatus
    quote: ArrivalQuote | None
    quote_age_seconds: Decimal | None
    relative_quoted_spread_bps: Decimal | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status is ArrivalQuoteStatus.MISSING:
            if self.quote is not None or self.quote_age_seconds is not None:
                raise FeatureInputError("missing evidence cannot retain a quote or age")
        elif self.quote is None or self.quote_age_seconds is None:
            raise FeatureInputError("non-missing evidence requires the selected quote and age")
        if self.quote is not None and self.quote.symbol != self.admission.symbol:
            raise FeatureInputError("arrival quote symbol drifted from the admission point")
        if self.quote_age_seconds is not None and self.quote_age_seconds < 0:
            raise FeatureInputError("arrival quote age cannot be negative")
        if self.status is ArrivalQuoteStatus.VALID:
            if self.relative_quoted_spread_bps is None:
                raise FeatureInputError("valid evidence requires relative quoted spread")
            if self.relative_quoted_spread_bps < 0:
                raise FeatureInputError("relative quoted spread cannot be negative")
            if self.reasons:
                raise FeatureInputError("valid evidence cannot carry failure reasons")
        elif self.relative_quoted_spread_bps is not None:
            raise FeatureInputError("invalid evidence cannot expose a decision-grade spread")

    @property
    def measurement_eligible(self) -> bool:
        """Whether the quote may enter a future feature-evaluation table."""

        return self.status is ArrivalQuoteStatus.VALID


@dataclass(frozen=True, slots=True)
class ArrivalQuotePlanReport:
    """Validation result for the blocked, zero-trial preregistration."""

    plan_id: str
    execution_state: str
    selected_companion: None
    executable_trial_count: int
    manifest_digest: str
    blockers: tuple[str, ...]


def _validate_quote_stream(quotes: Sequence[ArrivalQuote]) -> dict[str, tuple[ArrivalQuote, ...]]:
    grouped: dict[str, list[ArrivalQuote]] = defaultdict(list)
    seen_sequences: set[tuple[str, str]] = set()
    source_by_symbol: dict[str, str] = {}
    timestamp_by_symbol: dict[str, set[datetime]] = defaultdict(set)
    for quote in quotes:
        sequence_key = (quote.source, quote.source_sequence_id)
        if sequence_key in seen_sequences:
            raise FeatureInputError("quote source sequence ids must be unique")
        seen_sequences.add(sequence_key)
        expected_source = source_by_symbol.setdefault(quote.symbol, quote.source)
        if quote.source != expected_source:
            raise FeatureInputError(
                f"quote source identity changed within {quote.symbol}: "
                f"{expected_source!r} -> {quote.source!r}"
            )
        if quote.source_timestamp_utc in timestamp_by_symbol[quote.symbol]:
            raise FeatureInputError(
                f"ambiguous duplicate source timestamp for {quote.symbol}; "
                "a reviewed sequence ordering is required"
            )
        timestamp_by_symbol[quote.symbol].add(quote.source_timestamp_utc)
        grouped[quote.symbol].append(quote)
    return {
        symbol: tuple(
            sorted(
                rows,
                key=lambda item: (
                    item.source_timestamp_utc,
                    item.received_timestamp_utc,
                    item.source_sequence_id,
                ),
            )
        )
        for symbol, rows in grouped.items()
    }


def _quote_age(admission: AdmissionPoint, quote: ArrivalQuote) -> Decimal:
    return Decimal(
        str((admission.admission_timestamp_utc - quote.source_timestamp_utc).total_seconds())
    )


def _invalid_status(quote: ArrivalQuote) -> tuple[ArrivalQuoteStatus | None, tuple[str, ...]]:
    if quote.condition is not QuoteCondition.NORMAL:
        return (
            ArrivalQuoteStatus.INELIGIBLE_CONDITION,
            (f"quote:condition:{quote.condition.value}",),
        )
    if quote.bid <= 0 or quote.ask <= 0:
        return ArrivalQuoteStatus.EMPTY, ("quote:non_positive_side",)
    if quote.bid > quote.ask:
        return ArrivalQuoteStatus.CROSSED, ("quote:crossed",)
    if quote.bid == quote.ask:
        return ArrivalQuoteStatus.LOCKED, ("quote:locked",)
    return None, ()


def _relative_spread_bps(quote: ArrivalQuote) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        midpoint = (quote.bid + quote.ask) / Decimal(2)
        return Decimal(10_000) * (quote.ask - quote.bid) / midpoint


def align_arrival_quotes(
    admissions: Sequence[AdmissionPoint],
    quotes: Sequence[ArrivalQuote],
    *,
    max_quote_age_seconds: Decimal | int | str,
) -> tuple[ArrivalQuoteEvidence, ...]:
    """As-of join admission points to quotes known at admission, failing closed.

    A quote is eligible for selection only when both its source timestamp and
    recorder receipt timestamp are no later than the admission instant.  The
    latest source timestamp wins.  Quotes received after admission are never
    backfilled, even when their embedded source timestamp is earlier.
    """

    max_age = _positive_decimal(max_quote_age_seconds, "max_quote_age_seconds")
    grouped = _validate_quote_stream(quotes)
    seen_admissions: set[str] = set()
    evidence: list[ArrivalQuoteEvidence] = []
    for admission in admissions:
        if admission.primary_sequence_id in seen_admissions:
            raise FeatureInputError("admission primary_sequence_id values must be unique")
        seen_admissions.add(admission.primary_sequence_id)
        eligible = tuple(
            quote
            for quote in grouped.get(admission.symbol, ())
            if quote.source_timestamp_utc <= admission.admission_timestamp_utc
            and quote.received_timestamp_utc <= admission.admission_timestamp_utc
        )
        if not eligible:
            evidence.append(
                ArrivalQuoteEvidence(
                    admission=admission,
                    status=ArrivalQuoteStatus.MISSING,
                    quote=None,
                    quote_age_seconds=None,
                    relative_quoted_spread_bps=None,
                    reasons=("quote:missing_at_admission",),
                )
            )
            continue
        quote = eligible[-1]
        age = _quote_age(admission, quote)
        invalid_status, reasons = _invalid_status(quote)
        if invalid_status is not None:
            evidence.append(
                ArrivalQuoteEvidence(
                    admission=admission,
                    status=invalid_status,
                    quote=quote,
                    quote_age_seconds=age,
                    relative_quoted_spread_bps=None,
                    reasons=reasons,
                )
            )
            continue
        if age > max_age:
            evidence.append(
                ArrivalQuoteEvidence(
                    admission=admission,
                    status=ArrivalQuoteStatus.STALE,
                    quote=quote,
                    quote_age_seconds=age,
                    relative_quoted_spread_bps=None,
                    reasons=("quote:stale",),
                )
            )
            continue
        evidence.append(
            ArrivalQuoteEvidence(
                admission=admission,
                status=ArrivalQuoteStatus.VALID,
                quote=quote,
                quote_age_seconds=age,
                relative_quoted_spread_bps=_relative_spread_bps(quote),
                reasons=(),
            )
        )
    return tuple(evidence)


def validate_arrival_quote_plan(manifest: Mapping[str, Any]) -> ArrivalQuotePlanReport:
    """Validate the blocked preregistration without opening data or running trials."""

    required = {
        "blocked_before_first_data_read",
        "candidate",
        "causal_clock",
        "data_contract",
        "executable_trial_count",
        "execution_state",
        "host_strategy",
        "implementation_scope",
        "measurement_contract",
        "performance_claims",
        "plan_id",
        "promotion_authority",
        "schema_version",
        "selected_companion",
        "threshold_policy",
    }
    missing = sorted(required.difference(manifest))
    if missing:
        raise FeatureInputError(f"arrival-quote plan missing keys: {missing}")
    if manifest["schema_version"] != ARRIVAL_QUOTE_PLAN_SCHEMA:
        raise FeatureInputError("unsupported arrival-quote plan schema")
    if manifest["plan_id"] != ARRIVAL_QUOTE_PLAN_ID:
        raise FeatureInputError("unexpected arrival-quote plan id")
    if manifest["execution_state"] != ARRIVAL_QUOTE_EXECUTION_STATE:
        raise FeatureInputError("arrival-quote plan must remain blocked")
    if manifest["blocked_before_first_data_read"] is not True:
        raise FeatureInputError("arrival-quote plan must block before the first data read")
    if manifest["selected_companion"] is not None:
        raise FeatureInputError("arrival-quote plan selects no companion")
    if manifest["performance_claims"] != []:
        raise FeatureInputError("arrival-quote plan cannot carry performance claims")
    if manifest["promotion_authority"] != "none":
        raise FeatureInputError("arrival-quote plan has no promotion authority")
    if manifest["executable_trial_count"] != 0:
        raise FeatureInputError("arrival-quote plan authorizes zero executable trials")
    candidate = manifest["candidate"]
    if (
        not isinstance(candidate, Mapping)
        or candidate.get("status") != "future_priority_not_selected"
    ):
        raise FeatureInputError("arrival-quote candidate must remain unselected")
    host = manifest["host_strategy"]
    if not isinstance(host, Mapping):
        raise FeatureInputError("arrival-quote host_strategy must be an object")
    pine_source = host.get("pine_source")
    if (
        host.get("strategy_id") != "five_tool_confluence_v3_6"
        or host.get("mutates_pine_or_host_identity") is not False
        or not isinstance(pine_source, Mapping)
        or pine_source.get("sha256") != ARRIVAL_QUOTE_PINE_SHA256
    ):
        raise FeatureInputError("arrival-quote plan drifted from the immutable Five-Tool host")
    data_contract = manifest["data_contract"]
    if not isinstance(data_contract, Mapping):
        raise FeatureInputError("arrival-quote data_contract must be an object")
    if data_contract.get("status") != "pending_owner_certified_dataset":
        raise FeatureInputError("arrival-quote data contract must remain pending")
    if data_contract.get("downloads") is not False:
        raise FeatureInputError("arrival-quote plan cannot download market data")
    if tuple(data_contract.get("tradable_symbols", ())) != _TRADABLE_SYMBOLS:
        raise FeatureInputError("arrival-quote plan book is locked to GLD, IWM, and QQQ")
    for identity in ("dataset_id", "catalog_id", "sha256", "owner_holdout"):
        if data_contract.get(identity) is not None:
            raise FeatureInputError(f"arrival-quote {identity} must remain unset")
    threshold_policy = manifest["threshold_policy"]
    if not isinstance(threshold_policy, Mapping):
        raise FeatureInputError("arrival-quote threshold_policy must be an object")
    if threshold_policy.get("status") != "not_selected_or_frozen":
        raise FeatureInputError("arrival-quote thresholds cannot be selected in this plan")
    if (
        threshold_policy.get("max_quote_age_seconds") is not None
        or threshold_policy.get("max_relative_quoted_spread_bps") is not None
    ):
        raise FeatureInputError("arrival-quote thresholds must remain null")
    scope = manifest["implementation_scope"]
    if not isinstance(scope, Mapping):
        raise FeatureInputError("arrival-quote implementation_scope must be an object")
    for forbidden_capability in (
        "may_apply_veto",
        "may_open_dataset",
        "may_register_or_run_trial",
        "may_import_live_authority",
    ):
        if scope.get(forbidden_capability) is not False:
            raise FeatureInputError(f"arrival-quote plan forbids {forbidden_capability}")
    causal = manifest["causal_clock"]
    if not isinstance(causal, Mapping) or causal.get("future_quote") != "forbidden":
        raise FeatureInputError("arrival-quote plan must forbid future quotes")
    measurement = manifest["measurement_contract"]
    if not isinstance(measurement, Mapping):
        raise FeatureInputError("arrival-quote measurement_contract must be an object")
    forbidden_inputs = set(measurement.get("forbidden_decision_inputs", ()))
    if not {"fill_price", "effective_spread", "realized_spread", "mark-out"}.issubset(
        forbidden_inputs
    ):
        raise FeatureInputError("arrival-quote plan must keep execution outcomes out of inputs")
    blockers = (
        "pending_owner_certified_dataset",
        "admission_clock_pending_resolution",
        "thresholds_not_selected_or_frozen",
        "zero_executable_trials",
        "no_promotion_authority",
    )
    return ArrivalQuotePlanReport(
        plan_id=ARRIVAL_QUOTE_PLAN_ID,
        execution_state=ARRIVAL_QUOTE_EXECUTION_STATE,
        selected_companion=None,
        executable_trial_count=0,
        manifest_digest=canonical_digest(dict(manifest)),
        blockers=blockers,
    )

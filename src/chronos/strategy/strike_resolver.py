"""Transparent option-candidate filters and deterministic lower-is-better ranking."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, PositiveInt, field_validator, model_validator

from chronos.config.settings import Settings
from chronos.domain.enums import (
    DataQuality,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
)
from chronos.domain.models import (
    ChronosModel,
    Instrument,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    StockAvailability,
    UnderlyingContract,
)
from chronos.strategy.basis import BasisProjection
from chronos.strategy.capital import (
    CapitalCheck,
    CapitalContext,
    evaluate_covered_call_capital,
    evaluate_short_put_capital,
)

_ALGORITHM_VERSION = "strike-resolver-v1"


def _calculation_context(*values: Decimal | int) -> AbstractContextManager[Context]:
    precision = max(
        64,
        sum(
            len(value.as_tuple().digits) if isinstance(value, Decimal) else len(str(abs(value)))
            for value in values
        )
        + 16,
    )
    return localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN))


class CandidateRejectionCode(StrEnum):
    UNDERLYING_DATA = "UNDERLYING_DATA"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    WRONG_RIGHT = "WRONG_RIGHT"
    EXPIRATION_OUTSIDE_RANGE = "EXPIRATION_OUTSIDE_RANGE"
    EXPIRATION_NOT_SELECTED = "EXPIRATION_NOT_SELECTED"
    NOT_OTM = "NOT_OTM"
    MISSING_STRATEGY_BASIS = "MISSING_STRATEGY_BASIS"
    MISSING_STOCK_AVAILABILITY = "MISSING_STOCK_AVAILABILITY"
    ACCOUNT_SCOPE_MISMATCH = "ACCOUNT_SCOPE_MISMATCH"
    STRATEGY_BASIS_MISMATCH = "STRATEGY_BASIS_MISMATCH"
    STOCK_AVAILABILITY_MISMATCH = "STOCK_AVAILABILITY_MISMATCH"
    BELOW_STRATEGY_BASIS = "BELOW_STRATEGY_BASIS"
    INCOMPLETE_MARKET = "INCOMPLETE_MARKET"
    ZERO_BID = "ZERO_BID"
    CROSSED_MARKET = "CROSSED_MARKET"
    MISSING_DELTA = "MISSING_DELTA"
    INVALID_DELTA = "INVALID_DELTA"
    WRONG_DELTA_SIGN = "WRONG_DELTA_SIGN"
    DELTA_OUTSIDE_RANGE = "DELTA_OUTSIDE_RANGE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    STALE_QUOTE = "STALE_QUOTE"
    FUTURE_QUOTE = "FUTURE_QUOTE"
    DATA_QUALITY = "DATA_QUALITY"
    CONTRACT_METADATA = "CONTRACT_METADATA"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNVERIFIED_DELIVERABLE = "UNVERIFIED_DELIVERABLE"
    DUPLICATE_QUOTE = "DUPLICATE_QUOTE"
    CONFLICTING_EXPOSURE = "CONFLICTING_EXPOSURE"
    RECONCILIATION_NOT_READY = "RECONCILIATION_NOT_READY"
    CONTRACT_LIMIT = "CONTRACT_LIMIT"
    CAPITAL_LIMIT = "CAPITAL_LIMIT"
    UNCOVERED_CALL = "UNCOVERED_CALL"


class CandidateRejection(ChronosModel):
    code: CandidateRejectionCode
    explanation: str


class ResolverPolicy(ChronosModel):
    target_abs_delta: Decimal = Field(gt=0, lt=1)
    min_abs_delta: Decimal = Field(gt=0, lt=1)
    max_abs_delta: Decimal = Field(gt=0, lt=1)
    min_dte: int = Field(ge=0)
    target_dte: int = Field(ge=0)
    max_dte: int = Field(ge=0)
    max_expirations: PositiveInt
    min_option_volume: int = Field(ge=0)
    min_open_interest: int = Field(ge=0)
    max_relative_spread: Decimal = Field(gt=0)
    max_quote_age_seconds: Decimal = Field(gt=0)
    expiration_timezone: str = "America/New_York"
    max_contracts_per_order: PositiveInt
    delta_weight: Decimal = Field(ge=0)
    spread_weight: Decimal = Field(ge=0)
    dte_weight: Decimal = Field(ge=0)
    liquidity_weight: Decimal = Field(ge=0)
    rankable_data_qualities: tuple[DataQuality, ...] = (
        DataQuality.LIVE,
        DataQuality.FROZEN,
        DataQuality.DEMO,
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> ResolverPolicy:
        if not self.min_abs_delta <= self.target_abs_delta <= self.max_abs_delta:
            raise ValueError("target_abs_delta must be within the configured delta range")
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("target_dte must be within the configured DTE range")
        if not self.rankable_data_qualities:
            raise ValueError("At least one data quality must be rankable")
        if self.delta_weight + self.spread_weight + self.dte_weight + self.liquidity_weight <= 0:
            raise ValueError("At least one resolver weight must be positive")
        try:
            ZoneInfo(self.expiration_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("expiration_timezone must name an installed IANA timezone") from error
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> ResolverPolicy:
        return cls(
            target_abs_delta=settings.target_abs_delta,
            min_abs_delta=settings.min_abs_delta,
            max_abs_delta=settings.max_abs_delta,
            min_dte=settings.min_dte,
            target_dte=settings.target_dte,
            max_dte=settings.max_dte,
            max_expirations=settings.max_expirations,
            min_option_volume=settings.min_option_volume,
            min_open_interest=settings.min_open_interest,
            max_relative_spread=settings.max_relative_spread,
            max_quote_age_seconds=Decimal(settings.max_quote_age_seconds),
            expiration_timezone=settings.market_timezone,
            max_contracts_per_order=settings.max_contracts_per_order,
            delta_weight=settings.delta_weight,
            spread_weight=settings.spread_weight,
            dte_weight=settings.dte_weight,
            liquidity_weight=settings.liquidity_weight,
        )


class ResolverContext(ChronosModel):
    wheel_cycle_id: str
    right: OptionRight
    contracts: PositiveInt
    as_of: AwareDatetime
    underlying_quote: MarketQuote
    chain: OptionChainParameters
    option_quotes: tuple[MarketQuote, ...]
    capital: CapitalContext
    strategy_basis: BasisProjection | None = None
    stock_availability: StockAvailability | None = None
    conflicting_position_or_order: bool = False
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING

    @field_validator("wheel_cycle_id")
    @classmethod
    def validate_wheel_cycle_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("wheel_cycle_id must not be blank")
        return normalized


class RankedCandidate(ChronosModel):
    contract: OptionContract
    symbol: str
    expiration: date
    dte: int
    strike: Decimal
    right: OptionRight
    bid: Decimal
    ask: Decimal
    midpoint: Decimal
    last: Decimal | None
    delta: Decimal
    gamma: Decimal | None
    theta: Decimal | None
    implied_volatility: Decimal | None
    volume: int | None
    open_interest: int | None
    relative_spread: Decimal
    score: Decimal
    data_age_seconds: Decimal = Field(ge=0)
    data_quality: DataQuality
    capital_check: CapitalCheck
    eligibility_status: EligibilityStatus = EligibilityStatus.ELIGIBLE
    selection_rationale: str
    rejection_reasons: tuple[CandidateRejection, ...] = ()


class RejectedCandidate(ChronosModel):
    contract: Instrument
    data_quality: DataQuality
    data_age_seconds: Decimal | None = None
    eligibility_status: EligibilityStatus = EligibilityStatus.REJECTED
    rejection_reasons: tuple[CandidateRejection, ...]


class StrikeResolution(ChronosModel):
    algorithm_version: str = _ALGORITHM_VERSION
    policy: ResolverPolicy
    status: EligibilityStatus
    selected_expirations: tuple[date, ...]
    global_rejections: tuple[CandidateRejection, ...] = ()
    candidates: tuple[RankedCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    no_trade_reasons: tuple[str, ...] = ()

    @property
    def selected(self) -> RankedCandidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True, slots=True)
class _PreparedCandidate:
    quote: MarketQuote
    contract: OptionContract
    dte: int
    bid: Decimal
    ask: Decimal
    midpoint: Decimal
    delta: Decimal
    relative_spread: Decimal
    delta_error: Decimal
    dte_error: Decimal
    age_seconds: Decimal
    capital_check: CapitalCheck


class StrikeResolver:
    """Evaluate a bounded quote set; market-data retrieval stays in its manager."""

    def __init__(
        self,
        policy: ResolverPolicy,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._policy = policy
        self._logger = logger or logging.getLogger("chronos.strategy.strike_resolver")

    def resolve(self, context: ResolverContext) -> StrikeResolution:
        selected_expirations = self._selected_expirations(context)
        global_reasons = self._global_rejections(context)
        duplicate_ids = _duplicate_contract_ids(context.option_quotes)
        prepared: list[_PreparedCandidate] = []
        rejected: list[RejectedCandidate] = []

        for quote in context.option_quotes:
            reasons = list(global_reasons)
            contract = quote.contract
            age_seconds = _quote_age_seconds(context.as_of, quote)
            if contract.con_id in duplicate_ids:
                reasons.append(
                    CandidateRejection(
                        code=CandidateRejectionCode.DUPLICATE_QUOTE,
                        explanation="More than one quote was supplied for this contract.",
                    )
                )
            if not isinstance(contract, OptionContract):
                reasons.append(
                    CandidateRejection(
                        code=CandidateRejectionCode.CONTRACT_METADATA,
                        explanation="Candidate quote is not for a qualified option contract.",
                    )
                )
                rejected.append(
                    RejectedCandidate(
                        contract=contract,
                        data_quality=quote.data_quality,
                        data_age_seconds=max(age_seconds, Decimal("0")),
                        rejection_reasons=tuple(_unique_rejections(reasons)),
                    )
                )
                continue

            candidate_reasons, values = self._evaluate_candidate(
                context=context,
                quote=quote,
                contract=contract,
                selected_expirations=selected_expirations,
                age_seconds=age_seconds,
            )
            reasons.extend(candidate_reasons)
            if reasons or values is None:
                rejected.append(
                    RejectedCandidate(
                        contract=contract,
                        data_quality=quote.data_quality,
                        data_age_seconds=max(age_seconds, Decimal("0")),
                        rejection_reasons=tuple(_unique_rejections(reasons)),
                    )
                )
            else:
                prepared.append(values)

        ranked = self._rank(prepared)
        status = EligibilityStatus.ELIGIBLE if ranked else EligibilityStatus.NO_TRADE
        no_trade_reasons: tuple[str, ...] = ()
        if not ranked:
            global_evidence = tuple(
                f"{reason.code.value}: {reason.explanation}" for reason in global_reasons
            )
            if not context.option_quotes:
                no_trade_reasons = (
                    "No qualified option quotes were supplied for evaluation.",
                    *global_evidence,
                )
            else:
                codes = sorted(
                    {
                        reason.code.value
                        for candidate in rejected
                        for reason in candidate.rejection_reasons
                    }
                )
                summary = ", ".join(codes) if codes else "NO_ELIGIBLE_CANDIDATE"
                no_trade_reasons = (f"No candidate passed every resolver filter: {summary}.",)
        resolution = StrikeResolution(
            policy=self._policy,
            status=status,
            selected_expirations=selected_expirations,
            global_rejections=tuple(global_reasons),
            candidates=tuple(ranked),
            rejected=tuple(rejected),
            no_trade_reasons=no_trade_reasons,
        )
        self._logger.info(
            "Option candidates evaluated",
            extra={
                "event": "candidate_evaluation_completed",
                "symbol": context.underlying_quote.contract.symbol,
                "candidate_count": len(ranked),
                "rejected_count": len(rejected),
                "outcome": resolution.status.value,
            },
        )
        return resolution

    def _selected_expirations(self, context: ResolverContext) -> tuple[date, ...]:
        as_of_date = self._evaluation_date(context)
        eligible = (
            expiration
            for expiration in context.chain.expirations
            if self._policy.min_dte <= (expiration - as_of_date).days <= self._policy.max_dte
        )
        return tuple(
            sorted(
                eligible,
                key=lambda expiration: (
                    abs((expiration - as_of_date).days - self._policy.target_dte),
                    expiration,
                ),
            )[: self._policy.max_expirations]
        )

    def _global_rejections(self, context: ResolverContext) -> list[CandidateRejection]:
        reasons: list[CandidateRejection] = []
        underlying = context.underlying_quote
        underlying_age = _quote_age_seconds(context.as_of, underlying)
        if not isinstance(underlying.contract, UnderlyingContract):
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.UNDERLYING_DATA,
                    explanation="The underlying quote is not for a qualified stock contract.",
                )
            )
        if underlying.data_quality not in self._policy.rankable_data_qualities:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.UNDERLYING_DATA,
                    explanation=(
                        f"Underlying data quality {underlying.data_quality.value} is not rankable."
                    ),
                )
            )
        if underlying_age < 0 or underlying_age > self._policy.max_quote_age_seconds:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.UNDERLYING_DATA,
                    explanation="The underlying quote timestamp is not current.",
                )
            )
        if _spot_price(underlying) is None:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.UNDERLYING_DATA,
                    explanation="The underlying has no valid positive last price or midpoint.",
                )
            )
        if (
            underlying.bid is not None
            and underlying.ask is not None
            and underlying.ask < underlying.bid
        ):
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.UNDERLYING_DATA,
                    explanation="The underlying market is crossed.",
                )
            )
        if context.chain.underlying_con_id != underlying.contract.con_id:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.CONTRACT_METADATA,
                    explanation="Option-chain metadata does not match the underlying contract.",
                )
            )
        if (
            not context.chain.exchange.strip()
            or not context.chain.trading_class.strip()
            or not context.chain.expirations
            or not context.chain.strikes
        ):
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.CONTRACT_METADATA,
                    explanation="Option-chain identity or contract universe is incomplete.",
                )
            )
        if (
            underlying.contract.currency.strip().upper() != context.capital.currency
            or not underlying.contract.currency.strip()
        ):
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.CURRENCY_MISMATCH,
                    explanation=(
                        "Underlying and account capital currencies differ; no implicit FX "
                        "conversion is permitted."
                    ),
                )
            )
        if context.contracts > self._policy.max_contracts_per_order:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.CONTRACT_LIMIT,
                    explanation="Proposed quantity exceeds MAX_CONTRACTS_PER_ORDER.",
                )
            )
        if context.conflicting_position_or_order:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.CONFLICTING_EXPOSURE,
                    explanation="A conflicting position or opening order exists for this symbol.",
                )
            )
        if context.reconciliation_status is not ReconciliationStatus.RECONCILED:
            reasons.append(
                CandidateRejection(
                    code=CandidateRejectionCode.RECONCILIATION_NOT_READY,
                    explanation="Strategy reconciliation has not completed successfully.",
                )
            )
        evidence_reasons, _, _ = self._covered_call_evidence(context)
        reasons.extend(evidence_reasons)
        return reasons

    def _covered_call_evidence(
        self,
        context: ResolverContext,
    ) -> tuple[list[CandidateRejection], Decimal | None, Decimal | None]:
        """Return only fully scoped, reconciled covered-call evidence."""

        if context.right is not OptionRight.CALL:
            return [], None, None

        reasons: list[CandidateRejection] = []
        underlying = context.underlying_quote.contract
        expected_symbol = underlying.symbol.strip().upper()
        expected_currency = underlying.currency.strip().upper()
        expected_con_id = underlying.con_id

        trusted_basis: Decimal | None = None
        basis = context.strategy_basis
        if basis is None:
            _reject(
                reasons,
                CandidateRejectionCode.MISSING_STRATEGY_BASIS,
                "A reconciled, cycle-scoped Strategy-Adjusted Basis is required for covered calls.",
            )
        else:
            if basis.account_fingerprint != context.capital.account_fingerprint:
                _reject(
                    reasons,
                    CandidateRejectionCode.ACCOUNT_SCOPE_MISMATCH,
                    "Strategy-basis evidence belongs to a different pseudonymous account.",
                )
            basis_scope_matches = (
                basis.wheel_cycle_id == context.wheel_cycle_id
                and basis.symbol.strip().upper() == expected_symbol
                and basis.underlying_con_id == expected_con_id
                and basis.currency.strip().upper() == expected_currency
                and basis.currency.strip().upper() == context.capital.currency
            )
            if not basis_scope_matches:
                _reject(
                    reasons,
                    CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
                    (
                        "Strategy-basis evidence does not match this cycle, symbol, "
                        "underlying, or currency."
                    ),
                )
            if (
                basis.reconciliation_status is not ReconciliationStatus.RECONCILED
                or basis.strategy_adjusted_basis is None
                or basis.eligible_shares <= 0
            ):
                _reject(
                    reasons,
                    CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
                    "Strategy-basis evidence is not a complete reconciled projection.",
                )
            if (
                basis.account_fingerprint == context.capital.account_fingerprint
                and basis_scope_matches
                and basis.reconciliation_status is ReconciliationStatus.RECONCILED
                and basis.strategy_adjusted_basis is not None
                and basis.eligible_shares > 0
            ):
                trusted_basis = basis.strategy_adjusted_basis

        trusted_shares: Decimal | None = None
        stock = context.stock_availability
        if stock is None:
            _reject(
                reasons,
                CandidateRejectionCode.MISSING_STOCK_AVAILABILITY,
                "Reconciled, instrument-scoped stock availability is required for covered calls.",
            )
        else:
            if stock.account_fingerprint != context.capital.account_fingerprint:
                _reject(
                    reasons,
                    CandidateRejectionCode.ACCOUNT_SCOPE_MISMATCH,
                    "Stock-availability evidence belongs to a different pseudonymous account.",
                )
            stock_scope_matches = (
                stock.wheel_cycle_id == context.wheel_cycle_id
                and stock.symbol == expected_symbol
                and stock.underlying_con_id == expected_con_id
                and stock.currency == expected_currency
                and stock.currency == context.capital.currency
            )
            if not stock_scope_matches:
                _reject(
                    reasons,
                    CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
                    (
                        "Stock-availability evidence does not match this cycle, symbol, "
                        "underlying, or currency."
                    ),
                )
            if (
                trusted_basis is not None
                and basis is not None
                and stock.shares > basis.eligible_shares
            ):
                _reject(
                    reasons,
                    CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
                    "Available shares exceed the cycle's reconciled eligible-share allocation.",
                )
            if (
                stock.account_fingerprint == context.capital.account_fingerprint
                and stock_scope_matches
                and trusted_basis is not None
                and basis is not None
                and stock.shares <= basis.eligible_shares
            ):
                trusted_shares = stock.shares

        return reasons, trusted_basis, trusted_shares

    def _evaluation_date(self, context: ResolverContext) -> date:
        return context.as_of.astimezone(ZoneInfo(self._policy.expiration_timezone)).date()

    def _evaluate_candidate(
        self,
        *,
        context: ResolverContext,
        quote: MarketQuote,
        contract: OptionContract,
        selected_expirations: tuple[date, ...],
        age_seconds: Decimal,
    ) -> tuple[list[CandidateRejection], _PreparedCandidate | None]:
        reasons: list[CandidateRejection] = []
        as_of_date = self._evaluation_date(context)
        dte = (contract.expiration - as_of_date).days
        spot = _spot_price(context.underlying_quote)

        if contract.symbol != context.underlying_quote.contract.symbol:
            _reject(
                reasons,
                CandidateRejectionCode.SYMBOL_MISMATCH,
                "Option symbol does not match the underlying symbol.",
            )
        if contract.right is not context.right:
            _reject(
                reasons,
                CandidateRejectionCode.WRONG_RIGHT,
                "Option right does not match the requested Wheel action.",
            )
        if dte < self._policy.min_dte or dte > self._policy.max_dte:
            _reject(
                reasons,
                CandidateRejectionCode.EXPIRATION_OUTSIDE_RANGE,
                "Contract DTE is outside the configured inclusive range.",
            )
        elif contract.expiration not in selected_expirations:
            _reject(
                reasons,
                CandidateRejectionCode.EXPIRATION_NOT_SELECTED,
                "Expiration is outside the bounded set nearest target DTE.",
            )
        if (
            contract.multiplier != context.chain.multiplier
            or contract.trading_class != context.chain.trading_class
            or contract.underlying_con_id != context.chain.underlying_con_id
            or contract.underlying_con_id != context.underlying_quote.contract.con_id
            or contract.exchange.strip().upper() != context.chain.exchange.strip().upper()
            or contract.expiration not in context.chain.expirations
            or contract.strike not in context.chain.strikes
        ):
            _reject(
                reasons,
                CandidateRejectionCode.CONTRACT_METADATA,
                "Qualified contract does not match the selected chain metadata.",
            )
        if (
            contract.currency.strip().upper() != context.capital.currency
            or contract.currency.strip().upper()
            != context.underlying_quote.contract.currency.strip().upper()
        ):
            _reject(
                reasons,
                CandidateRejectionCode.CURRENCY_MISMATCH,
                "Option, underlying, and account capital currencies must match exactly.",
            )
        if not contract.has_verified_standard_deliverable:
            _reject(
                reasons,
                CandidateRejectionCode.UNVERIFIED_DELIVERABLE,
                "Wheel candidates require a verified standard share-only deliverable.",
            )

        if spot is not None:
            is_otm = (
                contract.strike < spot
                if context.right is OptionRight.PUT
                else contract.strike > spot
            )
            if not is_otm:
                _reject(
                    reasons,
                    CandidateRejectionCode.NOT_OTM,
                    "Put strikes must be below spot and call strikes must be above spot.",
                )

        if context.right is OptionRight.CALL:
            _, trusted_basis, _ = self._covered_call_evidence(context)
            if trusted_basis is not None and contract.strike < trusted_basis:
                _reject(
                    reasons,
                    CandidateRejectionCode.BELOW_STRATEGY_BASIS,
                    "Covered-call strike is below Strategy-Adjusted Basis.",
                )

        bid = quote.bid
        ask = quote.ask
        midpoint: Decimal | None = None
        relative_spread: Decimal | None = None
        if bid is None or ask is None:
            _reject(
                reasons,
                CandidateRejectionCode.INCOMPLETE_MARKET,
                "Both bid and ask are required.",
            )
        else:
            if bid <= 0:
                _reject(reasons, CandidateRejectionCode.ZERO_BID, "Bid must be positive.")
            if ask < bid:
                _reject(
                    reasons,
                    CandidateRejectionCode.CROSSED_MARKET,
                    "Ask is below bid.",
                )
            if bid > 0 and ask >= bid:
                with _calculation_context(
                    bid,
                    ask,
                    self._policy.max_relative_spread,
                ):
                    midpoint = (bid + ask) / Decimal("2")
                    relative_spread = (ask - bid) / midpoint
                    spread_exceeds_limit = Decimal("2") * (
                        ask - bid
                    ) > self._policy.max_relative_spread * (ask + bid)
                if spread_exceeds_limit:
                    _reject(
                        reasons,
                        CandidateRejectionCode.SPREAD_TOO_WIDE,
                        "Relative spread exceeds the configured maximum.",
                    )

        delta = quote.greeks.delta if quote.greeks is not None else None
        if delta is None:
            _reject(
                reasons,
                CandidateRejectionCode.MISSING_DELTA,
                "IBKR model delta is unavailable; no fallback is used.",
            )
        else:
            absolute_delta = delta.copy_abs()
            if not delta.is_finite() or absolute_delta > 1 or delta == 0:
                _reject(
                    reasons,
                    CandidateRejectionCode.INVALID_DELTA,
                    "Model delta must be non-zero and have magnitude no greater than one.",
                )
            expected_sign = delta.is_finite() and (
                delta < 0 if context.right is OptionRight.PUT else delta > 0
            )
            if not expected_sign:
                _reject(
                    reasons,
                    CandidateRejectionCode.WRONG_DELTA_SIGN,
                    "Calls require positive delta and puts require negative delta.",
                )
            if (
                not delta.is_finite()
                or not self._policy.min_abs_delta <= absolute_delta <= self._policy.max_abs_delta
            ):
                _reject(
                    reasons,
                    CandidateRejectionCode.DELTA_OUTSIDE_RANGE,
                    "Absolute delta is outside the configured inclusive range.",
                )

        volume_pass = quote.volume is not None and quote.volume >= self._policy.min_option_volume
        open_interest_pass = (
            quote.open_interest is not None
            and quote.open_interest >= self._policy.min_open_interest
        )
        if not (volume_pass or open_interest_pass):
            _reject(
                reasons,
                CandidateRejectionCode.INSUFFICIENT_LIQUIDITY,
                "Neither reported volume nor open interest meets its threshold.",
            )
        if age_seconds < 0:
            _reject(
                reasons,
                CandidateRejectionCode.FUTURE_QUOTE,
                "Quote timestamp is later than the evaluation clock.",
            )
        elif age_seconds > self._policy.max_quote_age_seconds:
            _reject(
                reasons,
                CandidateRejectionCode.STALE_QUOTE,
                "Quote is older than the configured maximum.",
            )
        if quote.data_quality not in self._policy.rankable_data_qualities:
            _reject(
                reasons,
                CandidateRejectionCode.DATA_QUALITY,
                f"{quote.data_quality.value} option data is not rankable.",
            )

        capital_check = self._capital_check(context=context, contract=contract)
        if capital_check is not None:
            for explanation in capital_check.reasons:
                code = (
                    CandidateRejectionCode.UNCOVERED_CALL
                    if "not fully covered" in explanation
                    else CandidateRejectionCode.CAPITAL_LIMIT
                )
                _reject(reasons, code, explanation)

        if (
            reasons
            or midpoint is None
            or relative_spread is None
            or delta is None
            or capital_check is None
        ):
            return reasons, None
        assert bid is not None and ask is not None
        with _calculation_context(delta, self._policy.target_abs_delta):
            delta_error = abs(abs(delta) - self._policy.target_abs_delta)
        return reasons, _PreparedCandidate(
            quote=quote,
            contract=contract,
            dte=dte,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            delta=delta,
            relative_spread=relative_spread,
            delta_error=delta_error,
            dte_error=Decimal(abs(dte - self._policy.target_dte)),
            age_seconds=age_seconds,
            capital_check=capital_check,
        )

    def _capital_check(
        self,
        *,
        context: ResolverContext,
        contract: OptionContract,
    ) -> CapitalCheck | None:
        if not contract.has_verified_standard_deliverable:
            return None
        assert contract.deliverable_shares is not None
        if context.right is OptionRight.PUT:
            return evaluate_short_put_capital(
                context=context.capital,
                strike=contract.strike,
                multiplier=contract.multiplier,
                verified_deliverable_shares=contract.deliverable_shares,
                contracts=context.contracts,
            )
        _, _, trusted_shares = self._covered_call_evidence(context)
        if trusted_shares is None:
            return None
        return evaluate_covered_call_capital(
            context=context.capital,
            multiplier=contract.multiplier,
            verified_deliverable_shares=contract.deliverable_shares,
            contracts=context.contracts,
            unencumbered_shares=trusted_shares,
        )

    def _rank(self, prepared: list[_PreparedCandidate]) -> list[RankedCandidate]:
        if not prepared:
            return []
        max_volume_log = max(
            (
                _log1p(candidate.quote.volume)
                for candidate in prepared
                if candidate.quote.volume is not None
            ),
            default=Decimal("0"),
        )
        max_oi_log = max(
            (
                _log1p(candidate.quote.open_interest)
                for candidate in prepared
                if candidate.quote.open_interest is not None
            ),
            default=Decimal("0"),
        )
        with _calculation_context(
            self._policy.target_abs_delta,
            self._policy.min_abs_delta,
            self._policy.max_abs_delta,
            self._policy.target_dte,
            self._policy.min_dte,
            self._policy.max_dte,
        ):
            delta_span = max(
                self._policy.target_abs_delta - self._policy.min_abs_delta,
                self._policy.max_abs_delta - self._policy.target_abs_delta,
                Decimal("1e-18"),
            )
            dte_span = max(
                Decimal(self._policy.target_dte - self._policy.min_dte),
                Decimal(self._policy.max_dte - self._policy.target_dte),
                Decimal("1"),
            )
        scored: list[tuple[RankedCandidate, Decimal, Decimal, Decimal]] = []
        for candidate in prepared:
            liquidity_bonus = _liquidity_bonus(
                volume=candidate.quote.volume,
                open_interest=candidate.quote.open_interest,
                max_volume_log=max_volume_log,
                max_oi_log=max_oi_log,
            )
            with _calculation_context(
                candidate.delta_error,
                delta_span,
                candidate.relative_spread,
                self._policy.max_relative_spread,
                candidate.dte_error,
                dte_span,
                liquidity_bonus,
                self._policy.delta_weight,
                self._policy.spread_weight,
                self._policy.dte_weight,
                self._policy.liquidity_weight,
            ):
                normalized_delta = candidate.delta_error / delta_span
                normalized_spread = candidate.relative_spread / self._policy.max_relative_spread
                normalized_dte = candidate.dte_error / dte_span
                score = (
                    self._policy.delta_weight * normalized_delta
                    + self._policy.spread_weight * normalized_spread
                    + self._policy.dte_weight * normalized_dte
                    - self._policy.liquidity_weight * liquidity_bonus
                )
            greeks = candidate.quote.greeks
            ranked = RankedCandidate(
                contract=candidate.contract,
                symbol=candidate.contract.symbol,
                expiration=candidate.contract.expiration,
                dte=candidate.dte,
                strike=candidate.contract.strike,
                right=candidate.contract.right,
                bid=candidate.bid,
                ask=candidate.ask,
                midpoint=candidate.midpoint,
                last=candidate.quote.last,
                delta=candidate.delta,
                gamma=greeks.gamma if greeks is not None else None,
                theta=greeks.theta if greeks is not None else None,
                implied_volatility=(greeks.implied_volatility if greeks is not None else None),
                volume=candidate.quote.volume,
                open_interest=candidate.quote.open_interest,
                relative_spread=candidate.relative_spread,
                score=score,
                data_age_seconds=candidate.age_seconds,
                data_quality=candidate.quote.data_quality,
                capital_check=candidate.capital_check,
                selection_rationale=(
                    "Passed every hard filter; "
                    f"abs-delta distance={candidate.delta_error}, "
                    f"relative spread={candidate.relative_spread}, "
                    f"DTE distance={candidate.dte_error}, "
                    f"liquidity bonus={liquidity_bonus}, score={score}."
                ),
            )
            scored.append((ranked, candidate.delta_error, candidate.dte_error, liquidity_bonus))
        scored.sort(
            key=lambda item: (
                item[0].score,
                item[1],
                item[0].relative_spread,
                item[2],
                -item[3],
                item[0].contract.expiration,
                item[0].strike,
                item[0].contract.con_id,
            )
        )
        return [item[0] for item in scored]


def _spot_price(quote: MarketQuote) -> Decimal | None:
    if quote.last is not None and quote.last > 0:
        return quote.last
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask >= quote.bid:
        with _calculation_context(quote.bid, quote.ask):
            return (quote.bid + quote.ask) / Decimal("2")
    return None


def _quote_age_seconds(as_of: AwareDatetime, quote: MarketQuote) -> Decimal:
    seconds = (as_of.astimezone(UTC) - quote.timestamp.astimezone(UTC)).total_seconds()
    return Decimal(str(seconds))


def _duplicate_contract_ids(quotes: tuple[MarketQuote, ...]) -> set[int]:
    counts: dict[int, int] = {}
    for quote in quotes:
        counts[quote.contract.con_id] = counts.get(quote.contract.con_id, 0) + 1
    return {contract_id for contract_id, count in counts.items() if count > 1}


def _reject(
    reasons: list[CandidateRejection],
    code: CandidateRejectionCode,
    explanation: str,
) -> None:
    reasons.append(CandidateRejection(code=code, explanation=explanation))


def _unique_rejections(
    reasons: list[CandidateRejection],
) -> list[CandidateRejection]:
    unique: dict[tuple[CandidateRejectionCode, str], CandidateRejection] = {}
    for reason in reasons:
        unique[(reason.code, reason.explanation)] = reason
    return list(unique.values())


def _log1p(value: int | None) -> Decimal:
    if value is None or value <= 0:
        return Decimal("0")
    with _calculation_context(value):
        return Decimal(value + 1).ln()


def _liquidity_bonus(
    *,
    volume: int | None,
    open_interest: int | None,
    max_volume_log: Decimal,
    max_oi_log: Decimal,
) -> Decimal:
    with _calculation_context(
        volume or 0,
        open_interest or 0,
        max_volume_log,
        max_oi_log,
    ):
        volume_log = _log1p(volume)
        open_interest_log = _log1p(open_interest)
        volume_component = volume_log / max_volume_log if max_volume_log > 0 else Decimal("0")
        open_interest_component = open_interest_log / max_oi_log if max_oi_log > 0 else Decimal("0")
        # Missing observations are neutral zeroes, never an advantage over known zeroes.
        return (volume_component + open_interest_component) / Decimal("2")

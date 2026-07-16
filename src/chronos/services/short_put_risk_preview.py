"""Fresh-evidence, read-only expiration risk previews for ranked short puts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.domain.enums import (
    DataQuality,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import ChronosModel, OptionContract, UnderlyingContract
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.strategy.scenarios import ShortPutScenario, ShortPutScenarioInput, short_put_scenario
from chronos.strategy.strike_resolver import RankedCandidate, spot_price

_LOGGER = logging.getLogger("chronos.services.short_put_risk_preview")
_RANKABLE_QUALITIES = frozenset({DataQuality.LIVE, DataQuality.FROZEN, DataQuality.DEMO})
MAX_TOTAL_COMMISSION_ESTIMATE = Decimal("10000")
MAX_COMMISSION_DECIMAL_PLACES = 4
MAX_COMMISSION_DECIMAL_DIGITS = 16
MAX_COMMISSION_INPUT_CHARACTERS = 32
MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS = Decimal("30")


def commission_estimate_has_bounded_representation(value: Decimal) -> bool:
    """Reject pathological coefficient and exponent sizes at every request boundary."""

    if not value.is_finite():
        return False
    parts = value.as_tuple()
    exponent = parts.exponent
    return (
        isinstance(exponent, int)
        and len(parts.digits) <= MAX_COMMISSION_DECIMAL_DIGITS
        and abs(exponent) <= MAX_COMMISSION_DECIMAL_DIGITS
    )


def commission_estimate_decimal_places(value: Decimal) -> int:
    """Count normalized fractional places without context-sensitive Decimal arithmetic."""

    if not value.is_finite():
        raise ValueError("commission estimate must be finite")
    if value.is_zero():
        return 0
    parts = value.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError("commission estimate must have a finite decimal exponent")
    trailing_zeros = 0
    for digit in reversed(parts.digits):
        if digit != 0:
            break
        trailing_zeros += 1
    normalized_exponent = exponent + trailing_zeros
    return max(-normalized_exponent, 0)


class ShortPutCandidateEvaluator(Protocol):
    """Fresh candidate surface used by the risk-preview boundary."""

    def evaluate(self, symbol: str) -> ShortPutCandidateEvaluation: ...


class RiskPreviewStatus(StrEnum):
    READY = "READY"
    WITHHELD = "WITHHELD"


class RiskScenarioLabel(StrEnum):
    OBSERVED_SPOT = "OBSERVED_SPOT"
    STRIKE = "STRIKE"
    EFFECTIVE_ENTRY = "EFFECTIVE_ENTRY"
    UNDERLYING_ZERO = "UNDERLYING_ZERO"


class ShortPutRiskPreviewRequest(ChronosModel):
    """Untrusted selection hint plus an explicit operator modeling assumption."""

    symbol: str
    selected_contract_id: Annotated[int, Field(gt=0, strict=True)]
    total_commission_estimate: Decimal = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized or not normalized.isalnum():
            raise ValueError("symbol must be a nonblank alphanumeric code")
        return normalized

    @field_validator("total_commission_estimate")
    @classmethod
    def require_finite_commission(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("total_commission_estimate must be finite")
        if value > MAX_TOTAL_COMMISSION_ESTIMATE:
            raise ValueError("total_commission_estimate exceeds the supported one-contract bound")
        if not commission_estimate_has_bounded_representation(value):
            raise ValueError("total_commission_estimate has an unsupported decimal representation")
        if commission_estimate_decimal_places(value) > MAX_COMMISSION_DECIMAL_PLACES:
            raise ValueError("total_commission_estimate has too many fractional decimal places")
        return value


class ShortPutRiskScenarioPoint(ChronosModel):
    labels: tuple[RiskScenarioLabel, ...] = Field(min_length=1)
    underlying_price: Decimal = Field(ge=0)
    put_intrinsic_value_per_share: Decimal = Field(ge=0)
    expiration_pnl: Decimal


class ShortPutRiskPreview(ChronosModel):
    """Presentation-safe payoff evidence; never an order preview or authorization."""

    symbol: str
    selected_contract: OptionContract
    candidate_rank: Annotated[int, Field(gt=0, strict=True)]
    contracts: Literal[1] = 1
    evidence_evaluated_at: AwareDatetime
    underlying_observed_at: AwareDatetime
    option_data_quality: DataQuality
    option_quote_age_seconds: Decimal = Field(ge=0)
    currency: str
    observed_spot: Decimal = Field(gt=0)
    hypothetical_credit_per_share: Decimal = Field(gt=0)
    hypothetical_credit_source: Literal["FRESH_BID"] = "FRESH_BID"
    total_commission_estimate: Decimal = Field(ge=0)
    commission_estimate_source: Literal["OPERATOR_ESTIMATE"] = "OPERATOR_ESTIMATE"
    fees_excluded: bool
    gross_premium: Decimal = Field(ge=0)
    net_premium: Decimal
    assigned_shares: Decimal = Field(gt=0)
    gross_assignment_obligation: Decimal = Field(gt=0)
    net_cash_deployed_if_assigned: Decimal
    effective_entry_price: Decimal = Field(ge=0)
    cash_secured_notional_pct: Decimal = Field(ge=0)
    broker_margin_impact: None = None
    scenario_points: tuple[ShortPutRiskScenarioPoint, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = Field(min_length=1)
    opening_actions_locked: Literal[True] = True

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("currency must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_identity_and_assumptions(self) -> ShortPutRiskPreview:
        if self.selected_contract.symbol != self.symbol:
            raise ValueError("selected contract symbol must match the preview symbol")
        if self.selected_contract.currency != self.currency:
            raise ValueError("selected contract currency must match the preview currency")
        if self.fees_excluded != (self.total_commission_estimate == 0):
            raise ValueError("fees_excluded must reflect the explicit commission estimate")
        return self


class ShortPutRiskPreviewResult(ChronosModel):
    """One fresh preview attempt, including the candidate refresh used as evidence."""

    request: ShortPutRiskPreviewRequest
    status: RiskPreviewStatus
    evaluated_at: AwareDatetime
    candidate_refresh: ShortPutCandidateEvaluation | None = None
    preview: ShortPutRiskPreview | None = None
    reasons: tuple[str, ...] = ()
    opening_actions_locked: Literal[True] = True

    @model_validator(mode="after")
    def validate_outcome(self) -> ShortPutRiskPreviewResult:
        if self.status is RiskPreviewStatus.READY:
            if self.preview is None or self.candidate_refresh is None or self.reasons:
                raise ValueError("A ready preview requires fresh evidence and no lock reasons")
        elif self.preview is not None:
            raise ValueError("A withheld preview cannot contain payoff evidence")
        return self


class _RiskPreviewEvidenceError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShortPutRiskPreviewService:
    """Refresh candidate evidence and calculate a locked one-contract payoff preview."""

    def __init__(
        self,
        candidate_service: ShortPutCandidateEvaluator,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidate_service = candidate_service
        self._clock = clock or (lambda: datetime.now(UTC))

    def preview(self, request: ShortPutRiskPreviewRequest) -> ShortPutRiskPreviewResult:
        """Use a fresh candidate evaluation; never trust the UI's historical result."""

        request = ShortPutRiskPreviewRequest.model_validate(request.model_dump(mode="python"))
        try:
            candidate_refresh = self._candidate_service.evaluate(request.symbol)
        except Exception as error:
            _LOGGER.warning(
                "Risk-preview candidate refresh failed; preview remains locked",
                extra={
                    "event": "short_put_risk_preview_refresh_failed",
                    "symbol": request.symbol,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=self._now(),
                reason="Fresh candidate evidence could not be obtained safely.",
            )

        attempted_at = self._now()
        try:
            preview = self._build_preview(
                request,
                candidate_refresh,
                attempted_at=attempted_at,
            )
        except _RiskPreviewEvidenceError as error:
            _LOGGER.info(
                "Read-only short-put risk preview withheld by fresh evidence",
                extra={
                    "event": "short_put_risk_preview_withheld",
                    "symbol": request.symbol,
                    "contract_id": request.selected_contract_id,
                    "outcome": RiskPreviewStatus.WITHHELD.value,
                },
            )
            return self._withheld(
                request,
                evaluated_at=attempted_at,
                candidate_refresh=candidate_refresh,
                reasons=tuple(dict.fromkeys((*candidate_refresh.reasons, error.reason))),
            )
        except Exception as error:
            _LOGGER.warning(
                "Risk-preview calculation failed; preview remains locked",
                extra={
                    "event": "short_put_risk_preview_calculation_failed",
                    "symbol": request.symbol,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=attempted_at,
                candidate_refresh=candidate_refresh,
                reason="Expiration risk evidence could not be calculated safely.",
            )

        _LOGGER.info(
            "Read-only short-put risk preview completed",
            extra={
                "event": "short_put_risk_preview_completed",
                "symbol": request.symbol,
                "contract_id": request.selected_contract_id,
                "scenario_count": len(preview.scenario_points),
            },
        )
        return ShortPutRiskPreviewResult(
            request=request,
            status=RiskPreviewStatus.READY,
            evaluated_at=attempted_at,
            candidate_refresh=candidate_refresh,
            preview=preview,
        )

    def _build_preview(
        self,
        request: ShortPutRiskPreviewRequest,
        refresh: ShortPutCandidateEvaluation,
        *,
        attempted_at: datetime,
    ) -> ShortPutRiskPreview:
        if refresh.symbol != request.symbol:
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evidence did not match the requested symbol."
            )
        if refresh.status is not EligibilityStatus.ELIGIBLE or refresh.resolution is None:
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evaluation did not produce an eligible selection."
            )
        if refresh.underlying_quote is None or refresh.reconciliation is None:
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evidence omitted its underlying or reconciliation snapshot."
            )
        snapshot = refresh.reconciliation.snapshot
        if snapshot is None:
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evidence omitted its reconciled account snapshot."
            )
        if (
            refresh.reconciliation.status is not ReconciliationStatus.RECONCILED
            or refresh.resolution.status is not EligibilityStatus.ELIGIBLE
        ):
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evidence is not fully reconciled and eligible."
            )

        matches = tuple(
            (rank, candidate)
            for rank, candidate in enumerate(refresh.resolution.candidates, start=1)
            if candidate.contract.con_id == request.selected_contract_id
        )
        if len(matches) != 1:
            raise _RiskPreviewEvidenceError(
                "The selected contract is no longer uniquely eligible in fresh evidence."
            )
        candidate_rank, candidate = matches[0]
        self._require_candidate_evidence(
            request,
            refresh,
            candidate,
            attempted_at=attempted_at,
        )

        contract = candidate.contract
        deliverable_shares = contract.deliverable_shares
        if deliverable_shares is None:
            raise _RiskPreviewEvidenceError(
                "The selected candidate has no verified share deliverable."
            )
        spot = spot_price(refresh.underlying_quote)
        if spot is None or not spot.is_finite():
            raise _RiskPreviewEvidenceError(
                "Fresh underlying evidence has no valid positive spot reference."
            )
        if (
            not isinstance(refresh.underlying_quote.contract, UnderlyingContract)
            or refresh.underlying_quote.contract.symbol != request.symbol
        ):
            raise _RiskPreviewEvidenceError(
                "Fresh underlying evidence was not the exact requested stock contract."
            )
        if (
            contract.underlying_con_id != refresh.underlying_quote.contract.con_id
            or refresh.chain is None
            or refresh.chain.underlying_con_id != contract.underlying_con_id
            or refresh.chain.multiplier != contract.multiplier
            or refresh.chain.exchange != refresh.underlying_quote.contract.exchange
            or refresh.chain.trading_class != refresh.underlying_quote.contract.symbol
            or contract.exchange != refresh.chain.exchange
            or contract.trading_class != refresh.chain.trading_class
            or candidate.expiration not in refresh.chain.expirations
            or candidate.strike not in refresh.chain.strikes
        ):
            raise _RiskPreviewEvidenceError(
                "Risk-preview contract identity does not match the fresh market evidence."
            )
        if (
            refresh.underlying_quote.contract.currency != contract.currency
            or snapshot.account.currency != contract.currency
        ):
            raise _RiskPreviewEvidenceError(
                "Risk-preview evidence does not share one verified currency."
            )
        self._require_reconciliation_and_capital(request, refresh, candidate)

        def scenario(price: Decimal) -> ShortPutScenario:
            return short_put_scenario(
                ShortPutScenarioInput(
                    strike=candidate.strike,
                    premium_per_share=candidate.bid,
                    multiplier=contract.multiplier,
                    deliverable_shares=deliverable_shares,
                    deliverable_verified=True,
                    contracts=1,
                    commission=request.total_commission_estimate,
                    commission_is_estimate=True,
                    scenario_price=price,
                    net_liquidation=snapshot.account.net_liquidation,
                )
            )

        summary = scenario(spot)
        if summary.effective_entry_price < 0:
            raise _RiskPreviewEvidenceError(
                "The modeled effective entry is outside the supported price range."
            )
        labeled_prices = (
            (RiskScenarioLabel.OBSERVED_SPOT, spot),
            (RiskScenarioLabel.STRIKE, candidate.strike),
            (RiskScenarioLabel.EFFECTIVE_ENTRY, summary.effective_entry_price),
            (RiskScenarioLabel.UNDERLYING_ZERO, Decimal("0")),
        )
        labels_by_price: dict[Decimal, list[RiskScenarioLabel]] = {}
        ordered_prices: list[Decimal] = []
        for label, price in labeled_prices:
            if price not in labels_by_price:
                labels_by_price[price] = []
                ordered_prices.append(price)
            labels_by_price[price].append(label)

        points = tuple(
            ShortPutRiskScenarioPoint(
                labels=tuple(labels_by_price[price]),
                underlying_price=price,
                put_intrinsic_value_per_share=max(
                    candidate.strike - price,
                    Decimal("0"),
                ),
                expiration_pnl=scenario(price).expiration_pnl,
            )
            for price in ordered_prices
        )
        return ShortPutRiskPreview(
            symbol=request.symbol,
            selected_contract=contract,
            candidate_rank=candidate_rank,
            evidence_evaluated_at=refresh.evaluated_at,
            underlying_observed_at=refresh.underlying_quote.timestamp,
            option_data_quality=candidate.data_quality,
            option_quote_age_seconds=candidate.data_age_seconds,
            currency=contract.currency,
            observed_spot=spot,
            hypothetical_credit_per_share=candidate.bid,
            total_commission_estimate=request.total_commission_estimate,
            fees_excluded=request.total_commission_estimate == 0,
            gross_premium=summary.gross_premium,
            net_premium=summary.net_premium,
            assigned_shares=summary.assigned_shares,
            gross_assignment_obligation=summary.gross_assignment_obligation,
            net_cash_deployed_if_assigned=summary.net_cash_deployed_if_assigned,
            effective_entry_price=summary.effective_entry_price,
            cash_secured_notional_pct=summary.cash_secured_notional_pct,
            scenario_points=points,
            assumptions=(
                "One contract is modeled from a fresh ranked candidate.",
                "The hypothetical credit uses the fresh bid and is not a fill guarantee.",
                "The total commission is an explicit operator estimate.",
                "The payoff points describe expiration only, not a forecast or probability.",
                "Broker margin, slippage, taxes, early assignment, and path risk are not modeled.",
            ),
        )

    @staticmethod
    def _require_candidate_evidence(
        request: ShortPutRiskPreviewRequest,
        refresh: ShortPutCandidateEvaluation,
        candidate: RankedCandidate,
        *,
        attempted_at: datetime,
    ) -> None:
        contract = candidate.contract
        resolution = refresh.resolution
        underlying_quote = refresh.underlying_quote
        reconciliation = refresh.reconciliation
        if resolution is None or underlying_quote is None or reconciliation is None:
            raise _RiskPreviewEvidenceError(
                "The selected candidate is missing fresh prerequisite evidence."
            )
        snapshot = reconciliation.snapshot
        if snapshot is None:
            raise _RiskPreviewEvidenceError(
                "The selected candidate is missing a reconciled account snapshot."
            )
        max_age_seconds = resolution.policy.max_quote_age_seconds
        if not max_age_seconds.is_finite() or max_age_seconds <= 0:
            raise _RiskPreviewEvidenceError(
                "Fresh evidence supplied an invalid quote-freshness policy."
            )
        max_age_seconds = min(
            max_age_seconds,
            MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
        )
        if not all(
            ShortPutRiskPreviewService._timestamp_is_fresh(
                timestamp,
                now=attempted_at,
                max_age_seconds=max_age_seconds,
            )
            for timestamp in (
                refresh.evaluated_at,
                underlying_quote.timestamp,
                snapshot.captured_at,
                snapshot.account.as_of,
            )
        ):
            raise _RiskPreviewEvidenceError(
                "Fresh candidate, market, or reconciliation timestamps are outside the "
                "risk-preview window."
            )
        invalid = (
            candidate.symbol != request.symbol,
            contract.symbol != request.symbol,
            candidate.right is not OptionRight.PUT,
            contract.right is not OptionRight.PUT,
            candidate.eligibility_status is not EligibilityStatus.ELIGIBLE,
            not candidate.capital_check.passed,
            not contract.has_verified_standard_deliverable,
            contract.deliverable_shares != contract.multiplier,
            candidate.bid <= 0,
            candidate.ask < candidate.bid,
            candidate.midpoint != (candidate.bid + candidate.ask) / Decimal("2"),
            candidate.data_quality not in _RANKABLE_QUALITIES,
            candidate.data_age_seconds < 0,
            candidate.data_age_seconds > max_age_seconds,
            candidate.strike != contract.strike,
            candidate.expiration != contract.expiration,
            underlying_quote.data_quality not in _RANKABLE_QUALITIES,
            snapshot.data_quality not in _RANKABLE_QUALITIES,
            underlying_quote.timestamp > refresh.evaluated_at,
            snapshot.captured_at > refresh.evaluated_at,
            snapshot.account.as_of > refresh.evaluated_at,
        )
        if any(invalid):
            raise _RiskPreviewEvidenceError(
                "The selected candidate failed the fresh risk-preview evidence checks."
            )
        if not ShortPutRiskPreviewService._all_finite_decimals(
            candidate.bid,
            candidate.ask,
            candidate.midpoint,
            candidate.last,
            candidate.delta,
            candidate.gamma,
            candidate.theta,
            candidate.implied_volatility,
            candidate.relative_spread,
            candidate.score,
            candidate.data_age_seconds,
            contract.strike,
            contract.multiplier,
            contract.min_tick,
            contract.deliverable_shares,
        ):
            raise _RiskPreviewEvidenceError(
                "The selected candidate contains non-finite numeric evidence."
            )
        evaluation_age_seconds = ShortPutRiskPreviewService._timestamp_age_seconds(
            refresh.evaluated_at,
            now=attempted_at,
        )
        if (
            evaluation_age_seconds is None
            or evaluation_age_seconds + candidate.data_age_seconds > max_age_seconds
        ):
            raise _RiskPreviewEvidenceError(
                "The selected option quote is too old at the risk-preview decision point."
            )
        expected_shares = contract.deliverable_shares
        if expected_shares is None:
            raise _RiskPreviewEvidenceError(
                "The selected candidate has no verified share deliverable."
            )
        expected_obligation = candidate.strike * expected_shares
        if (
            candidate.capital_check.shares_required != expected_shares
            or candidate.capital_check.proposal_amount != expected_obligation
        ):
            raise _RiskPreviewEvidenceError(
                "The selected candidate's capital evidence is internally inconsistent."
            )

    @staticmethod
    def _require_reconciliation_and_capital(
        request: ShortPutRiskPreviewRequest,
        refresh: ShortPutCandidateEvaluation,
        candidate: RankedCandidate,
    ) -> None:
        reconciliation = refresh.reconciliation
        if reconciliation is None or reconciliation.snapshot is None:
            raise _RiskPreviewEvidenceError(
                "Fresh candidate evidence omitted its reconciled account snapshot."
            )
        snapshot = reconciliation.snapshot
        matching_symbols = tuple(
            item for item in reconciliation.symbols if item.symbol == request.symbol
        )
        if (
            reconciliation.status is not ReconciliationStatus.RECONCILED
            or reconciliation.reasons
            or snapshot.positions
            or snapshot.open_orders
            or snapshot.execution_count != 0
            or len(matching_symbols) != 1
        ):
            raise _RiskPreviewEvidenceError(
                "Whole-account reconciliation no longer proves an empty, unique symbol scope."
            )
        target = matching_symbols[0]
        quantities = (
            target.stock_shares,
            target.unencumbered_shares,
            target.short_put_contracts,
            target.short_call_contracts,
            target.pending_put_contracts,
            target.pending_call_contracts,
        )
        if (
            target.status is not ReconciliationStatus.RECONCILED
            or target.stage is not WheelStage.FLAT
            or target.manual_review_required
            or any(quantity != 0 for quantity in quantities)
        ):
            raise _RiskPreviewEvidenceError(
                "The selected symbol is not uniquely reconciled, flat, and exposure-free."
            )

        deliverable_shares = candidate.contract.deliverable_shares
        if deliverable_shares is None:
            raise _RiskPreviewEvidenceError(
                "The selected candidate has no verified share deliverable."
            )
        obligation = candidate.strike * deliverable_shares
        account = snapshot.account
        capital = candidate.capital_check
        symbol_pct = capital.resulting_symbol_allocation_pct
        total_pct = capital.resulting_total_wheel_allocation_pct
        if not ShortPutRiskPreviewService._all_finite_decimals(
            obligation,
            account.net_liquidation,
            account.total_cash,
            account.buying_power,
            capital.proposal_amount,
            capital.resulting_symbol_allocation,
            capital.resulting_total_wheel_allocation,
            capital.resulting_symbol_allocation_pct,
            capital.resulting_total_wheel_allocation_pct,
            capital.shares_required,
            capital.remaining_unencumbered_shares,
        ):
            raise _RiskPreviewEvidenceError(
                "Fresh account or capital evidence contains non-finite numeric values."
            )
        if account.net_liquidation <= 0 or account.total_cash < obligation:
            raise _RiskPreviewEvidenceError(
                "Fresh account cash does not secure the gross assignment obligation."
            )
        precision = max(
            50,
            len(obligation.as_tuple().digits) + len(account.net_liquidation.as_tuple().digits) + 16,
        )
        with localcontext(Context(prec=precision)):
            expected_pct = obligation / account.net_liquidation
            percentage_tolerance = Decimal("1e-24")
            percentages_match = (
                symbol_pct is not None
                and total_pct is not None
                and abs(symbol_pct - expected_pct) <= percentage_tolerance
                and abs(total_pct - expected_pct) <= percentage_tolerance
            )
        if (
            not capital.passed
            or capital.reasons
            or capital.proposal_amount != obligation
            or capital.shares_required != deliverable_shares
            or capital.resulting_symbol_allocation != obligation
            or capital.resulting_total_wheel_allocation != obligation
            or capital.remaining_unencumbered_shares is not None
            or not percentages_match
        ):
            raise _RiskPreviewEvidenceError(
                "The selected candidate's capital evidence is internally inconsistent."
            )

    @staticmethod
    def _timestamp_is_fresh(
        value: datetime,
        *,
        now: datetime,
        max_age_seconds: Decimal,
    ) -> bool:
        age_seconds = ShortPutRiskPreviewService._timestamp_age_seconds(value, now=now)
        return age_seconds is not None and age_seconds <= max_age_seconds

    @staticmethod
    def _timestamp_age_seconds(value: datetime, *, now: datetime) -> Decimal | None:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return None
        age = now.astimezone(UTC) - value.astimezone(UTC)
        if age < timedelta(0):
            return None
        return Decimal(age.days * 86_400 + age.seconds) + (
            Decimal(age.microseconds) / Decimal("1000000")
        )

    @staticmethod
    def _all_finite_decimals(*values: Decimal | None) -> bool:
        return all(value is None or value.is_finite() for value in values)

    def _withheld(
        self,
        request: ShortPutRiskPreviewRequest,
        *,
        evaluated_at: datetime,
        reason: str | None = None,
        reasons: tuple[str, ...] = (),
        candidate_refresh: ShortPutCandidateEvaluation | None = None,
    ) -> ShortPutRiskPreviewResult:
        values = tuple(dict.fromkeys((*reasons, *((reason,) if reason is not None else ()))))
        return ShortPutRiskPreviewResult(
            request=request,
            status=RiskPreviewStatus.WITHHELD,
            evaluated_at=evaluated_at,
            candidate_refresh=candidate_refresh,
            reasons=values,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ShortPutRiskPreviewService clock must return an aware datetime")
        return value.astimezone(UTC)

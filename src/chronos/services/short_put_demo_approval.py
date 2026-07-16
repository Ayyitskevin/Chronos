"""Explicit, non-authoritative approval rehearsals for DEMO short puts.

This boundary deliberately stops after a fresh M7 what-if refresh.  It records
neither an order lifecycle transition nor a database decision and never calls a
broker order method.  ``APPROVAL_REHEARSED`` is presentation evidence only; it
is not :class:`~chronos.domain.enums.OrderLifecycle.USER_CONFIRMED`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.broker.base import Broker
from chronos.broker.demo import DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    DataQuality,
    DisplayEnvironment,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import ChronosModel, OptionContract
from chronos.services.short_put_demo_what_if import (
    MAX_LIMIT_DECIMAL_PLACES,
    MAX_LIMIT_INPUT_CHARACTERS,
    MAX_LIMIT_PRICE,
    DemoWhatIfStatus,
    ShortPutDemoWhatIfPreview,
    ShortPutDemoWhatIfRequest,
    ShortPutDemoWhatIfResult,
    ShortPutDemoWhatIfService,
    limit_price_decimal_places,
    limit_price_has_bounded_representation,
)
from chronos.services.short_put_risk_preview import (
    MAX_COMMISSION_DECIMAL_PLACES,
    MAX_COMMISSION_INPUT_CHARACTERS,
    MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
    MAX_TOTAL_COMMISSION_ESTIMATE,
    RiskPreviewStatus,
    RiskScenarioLabel,
    ShortPutRiskPreviewRequest,
    ShortPutRiskScenarioPoint,
    commission_estimate_decimal_places,
    commission_estimate_has_bounded_representation,
)
from chronos.strategy.capital import CapitalContext, evaluate_short_put_capital
from chronos.strategy.scenarios import ShortPutScenario, ShortPutScenarioInput, short_put_scenario
from chronos.strategy.strike_resolver import ResolverPolicy

_LOGGER = logging.getLogger("chronos.services.short_put_demo_approval")
_APPROVAL_REFERENCE_PATTERN = re.compile(r"^CHR-APR-[A-F0-9]{32}$")
_WHAT_IF_REFERENCE_PATTERN = re.compile(r"^CHR-WIF-[A-F0-9]{32}$")
_RECEIPT_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3,8}$")

MAX_TYPED_SYMBOL_CHARACTERS = 32
MAX_CONFIRMATION_INTEGER_INPUT_CHARACTERS = 32
MAX_CONFIRMATION_INTEGER = 9_223_372_036_854_775_807
MAX_ACKNOWLEDGED_ASSIGNMENT_OBLIGATION = Decimal("100000000")
MAX_ASSIGNMENT_OBLIGATION_DECIMAL_PLACES = 4
MAX_ASSIGNMENT_OBLIGATION_INPUT_CHARACTERS = 32
MAX_FRESH_EVIDENCE_STRING_CHARACTERS = 512
MAX_FRESH_EVIDENCE_DECIMAL_DIGITS = 128
_EXPECTED_RISK_ASSUMPTIONS = (
    "One contract is modeled from a fresh ranked candidate.",
    "The hypothetical credit uses the fresh bid and is not a fill guarantee.",
    "The total commission is an explicit operator estimate.",
    "The payoff points describe expiration only, not a forecast or probability.",
    "Broker margin, slippage, taxes, early assignment, and path risk are not modeled.",
)


class DemoApprovalBrokerBoundary(Protocol):
    """Only the current adapter identity is observed; no broker methods are called."""

    broker: Broker


class DemoApprovalStatus(StrEnum):
    """Rehearsal-specific status, intentionally separate from ``OrderLifecycle``."""

    APPROVAL_REHEARSED = "APPROVAL_REHEARSED"
    WITHHELD = "WITHHELD"


def _bound_raw_decimal(value: object, *, max_characters: int, label: str) -> object:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a decimal amount, not a boolean")
    if isinstance(value, str) and len(value) > max_characters:
        raise ValueError(f"{label} input is too long")
    return value


def _validate_assignment_obligation(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("gross assignment obligation must be finite")
    if value > MAX_ACKNOWLEDGED_ASSIGNMENT_OBLIGATION:
        raise ValueError("gross assignment obligation exceeds the supported bound")
    if not limit_price_has_bounded_representation(value):
        raise ValueError("gross assignment obligation has an unsupported representation")
    if limit_price_decimal_places(value) > MAX_ASSIGNMENT_OBLIGATION_DECIMAL_PLACES:
        raise ValueError("gross assignment obligation has too many fractional decimal places")
    return value


def _decimal_has_bounded_representation(value: Decimal, *, max_digits: int) -> bool:
    if not value.is_finite():
        return False
    parts = value.as_tuple()
    exponent = parts.exponent
    return (
        isinstance(exponent, int)
        and len(parts.digits) <= max_digits
        and abs(exponent) <= max_digits
    )


def _limit_is_tick_aligned(limit_price: Decimal, min_tick: Decimal) -> bool:
    if not limit_price.is_finite() or not min_tick.is_finite() or limit_price <= 0 or min_tick <= 0:
        return False
    precision = max(
        50,
        len(limit_price.as_tuple().digits) + len(min_tick.as_tuple().digits) + 16,
    )
    try:
        with localcontext(Context(prec=precision)):
            return limit_price % min_tick == 0
    except InvalidOperation:
        return False


def _scenario_evidence(
    *,
    strike: Decimal,
    premium_per_share: Decimal,
    multiplier: Decimal,
    deliverable_shares: Decimal,
    commission: Decimal,
    observed_spot: Decimal,
    net_liquidation: Decimal,
) -> tuple[ShortPutScenario, tuple[ShortPutRiskScenarioPoint, ...]]:
    def scenario(price: Decimal) -> ShortPutScenario:
        return short_put_scenario(
            ShortPutScenarioInput(
                strike=strike,
                premium_per_share=premium_per_share,
                multiplier=multiplier,
                deliverable_shares=deliverable_shares,
                deliverable_verified=True,
                contracts=1,
                commission=commission,
                commission_is_estimate=True,
                scenario_price=price,
                net_liquidation=net_liquidation,
            )
        )

    summary = scenario(observed_spot)
    effective_entry_price = summary.effective_entry_price
    labeled_prices = (
        (RiskScenarioLabel.OBSERVED_SPOT, observed_spot),
        (RiskScenarioLabel.STRIKE, strike),
        (RiskScenarioLabel.EFFECTIVE_ENTRY, effective_entry_price),
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
            put_intrinsic_value_per_share=max(strike - price, Decimal("0")),
            expiration_pnl=scenario(price).expiration_pnl,
        )
        for price in ordered_prices
    )
    return summary, points


def _what_if_terms_are_coherent(
    request: ShortPutDemoApprovalRequest,
    what_if: ShortPutDemoWhatIfResult,
    *,
    settings: Settings,
) -> bool:
    """Recompute the operator-visible terms from the complete M5-M7 lineage."""

    try:
        preview = what_if.preview
        risk = what_if.risk_refresh
        if preview is None or risk is None:
            return False
        risk_preview = risk.preview
        refresh = risk.candidate_refresh
        if risk_preview is None or refresh is None:
            return False
        resolution = refresh.resolution
        reconciliation = refresh.reconciliation
        underlying_quote = refresh.underlying_quote
        chain = refresh.chain
        if (
            resolution is None
            or reconciliation is None
            or reconciliation.snapshot is None
            or underlying_quote is None
            or chain is None
        ):
            return False
        expected_what_if_request = ShortPutDemoWhatIfRequest(
            symbol=request.symbol,
            selected_contract_id=request.selected_contract_id,
            total_commission_estimate=request.total_commission_estimate,
            limit_price=request.limit_price,
        )
        expected_risk_request = ShortPutRiskPreviewRequest(
            symbol=request.symbol,
            selected_contract_id=request.selected_contract_id,
            total_commission_estimate=request.total_commission_estimate,
        )
        matches = tuple(
            (rank, candidate)
            for rank, candidate in enumerate(resolution.candidates, start=1)
            if candidate.contract.con_id == request.selected_contract_id
        )
        if len(matches) != 1:
            return False
        candidate_rank, candidate = matches[0]
        contract = candidate.contract
        deliverable_shares = contract.deliverable_shares
        snapshot = reconciliation.snapshot
        if deliverable_shares is None:
            return False
        matching_symbols = tuple(
            item for item in reconciliation.symbols if item.symbol == request.symbol
        )
        if len(matching_symbols) != 1:
            return False
        target = matching_symbols[0]
        expected_obligation = contract.strike * deliverable_shares
        expected_capital = evaluate_short_put_capital(
            context=CapitalContext(
                # Capital math does not consume identity; use a valid nonidentifying scope token.
                account_fingerprint="0" * 64,
                net_liquidation=snapshot.account.net_liquidation,
                uncommitted_cash=snapshot.account.total_cash,
                current_symbol_allocation=Decimal("0"),
                current_total_wheel_allocation=Decimal("0"),
                max_symbol_allocation_pct=settings.max_symbol_allocation_pct,
                max_total_wheel_allocation_pct=settings.max_total_wheel_allocation_pct,
                currency=snapshot.account.currency,
            ),
            strike=contract.strike,
            multiplier=contract.multiplier,
            verified_deliverable_shares=deliverable_shares,
            contracts=1,
        )
        expected_what_if, expected_what_if_points = _scenario_evidence(
            strike=contract.strike,
            premium_per_share=request.limit_price,
            multiplier=contract.multiplier,
            deliverable_shares=deliverable_shares,
            commission=preview.broker_estimated_commission,
            observed_spot=risk_preview.observed_spot,
            net_liquidation=snapshot.account.net_liquidation,
        )
        expected_risk, expected_risk_points = _scenario_evidence(
            strike=contract.strike,
            premium_per_share=candidate.bid,
            multiplier=contract.multiplier,
            deliverable_shares=deliverable_shares,
            commission=request.total_commission_estimate,
            observed_spot=risk_preview.observed_spot,
            net_liquidation=snapshot.account.net_liquidation,
        )
        expected_warning_notice = (
            None
            if preview.broker_warning_count == 0
            else (
                f"The deterministic broker returned {preview.broker_warning_count} "
                "rehearsal warning(s); no broker text or authority is carried forward."
            )
        )
        if (
            what_if.request != expected_what_if_request
            or what_if.status is not DemoWhatIfStatus.WHAT_IF_PREVIEWED
            or what_if.reasons
            or not what_if.opening_actions_locked
            or risk.request != expected_risk_request
            or risk.status is not RiskPreviewStatus.READY
            or risk.reasons
            or refresh.symbol != request.symbol
            or refresh.status is not EligibilityStatus.ELIGIBLE
            or resolution.status is not EligibilityStatus.ELIGIBLE
            or reconciliation.status is not ReconciliationStatus.RECONCILED
            or reconciliation.reasons
            or snapshot.environment is not DisplayEnvironment.DEMO
            or snapshot.data_quality is not DataQuality.DEMO
            or snapshot.positions
            or snapshot.open_orders
            or snapshot.execution_count != 0
            or target.status is not ReconciliationStatus.RECONCILED
            or target.stage is not WheelStage.FLAT
            or target.manual_review_required
            or any(
                quantity != 0
                for quantity in (
                    target.stock_shares,
                    target.unencumbered_shares,
                    target.short_put_contracts,
                    target.short_call_contracts,
                    target.pending_put_contracts,
                    target.pending_call_contracts,
                )
            )
            or candidate_rank != preview.candidate_rank
            or candidate_rank != risk_preview.candidate_rank
            or candidate.contract != preview.selected_contract
            or candidate.contract != risk_preview.selected_contract
            or candidate.symbol != request.symbol
            or candidate.right is not OptionRight.PUT
            or candidate.strike != contract.strike
            or candidate.expiration != contract.expiration
            or candidate.eligibility_status is not EligibilityStatus.ELIGIBLE
            or candidate.rejection_reasons
            or candidate.data_quality is not DataQuality.DEMO
            or candidate.capital_check != expected_capital
            or not expected_capital.passed
            or expected_capital.reasons
            or expected_capital.proposal_amount != expected_obligation
            or contract.symbol != request.symbol
            or contract.con_id != request.selected_contract_id
            or contract.right is not OptionRight.PUT
            or not contract.has_verified_standard_deliverable
            or deliverable_shares != contract.multiplier
            or chain.underlying_con_id != contract.underlying_con_id
            or chain.exchange != contract.exchange
            or chain.trading_class != contract.trading_class
            or chain.multiplier != contract.multiplier
            or contract.expiration not in chain.expirations
            or contract.strike not in chain.strikes
            or underlying_quote.contract.con_id != contract.underlying_con_id
            or underlying_quote.contract.currency != contract.currency
            or snapshot.account.currency != contract.currency
            or preview.symbol != request.symbol
            or preview.quantity != 1
            or preview.limit_price != request.limit_price
            or preview.fresh_bid != candidate.bid
            or preview.fresh_ask != candidate.ask
            or not _limit_is_tick_aligned(preview.limit_price, contract.min_tick)
            or preview.limit_price < candidate.bid
            or preview.limit_price > candidate.ask
            or preview.operator_commission_estimate != request.total_commission_estimate
            or preview.commission_variance
            != preview.broker_estimated_commission - request.total_commission_estimate
            or preview.gross_premium != expected_what_if.gross_premium
            or preview.net_premium != expected_what_if.net_premium
            or preview.assigned_shares != expected_what_if.assigned_shares
            or preview.gross_assignment_obligation != expected_obligation
            or preview.gross_assignment_obligation != expected_what_if.gross_assignment_obligation
            or preview.net_cash_deployed_if_assigned
            != expected_what_if.net_cash_deployed_if_assigned
            or preview.effective_entry_price != expected_what_if.effective_entry_price
            or preview.cash_secured_notional_pct != expected_what_if.cash_secured_notional_pct
            or preview.scenario_points != expected_what_if_points
            or preview.broker_warning_notice != expected_warning_notice
            or preview.currency != contract.currency
            or preview.transmit
            or preview.outside_rth
            or not preview.demo_rehearsal_only
            or preview.broker_authorized
            or preview.user_confirmation_available
            or preview.submission_authorized
            or not preview.opening_actions_locked
            or risk_preview.symbol != request.symbol
            or risk_preview.hypothetical_credit_per_share != candidate.bid
            or risk_preview.total_commission_estimate != request.total_commission_estimate
            or risk_preview.gross_premium != expected_risk.gross_premium
            or risk_preview.net_premium != expected_risk.net_premium
            or risk_preview.assigned_shares != expected_risk.assigned_shares
            or risk_preview.gross_assignment_obligation != expected_obligation
            or risk_preview.net_cash_deployed_if_assigned
            != expected_risk.net_cash_deployed_if_assigned
            or risk_preview.effective_entry_price != expected_risk.effective_entry_price
            or risk_preview.cash_secured_notional_pct != expected_risk.cash_secured_notional_pct
            or risk_preview.scenario_points != expected_risk_points
            or risk_preview.assumptions != _EXPECTED_RISK_ASSUMPTIONS
            or underlying_quote.timestamp > refresh.evaluated_at
        ):
            return False
        if resolution.policy != ResolverPolicy.from_settings(settings):
            return False
    except Exception:
        return False
    return True


class ShortPutDemoApprovalRequest(ChronosModel):
    """Scalar-only target and explicit operator affirmations; no parent evidence."""

    symbol: str
    selected_contract_id: Annotated[
        int,
        Field(gt=0, le=MAX_CONFIRMATION_INTEGER, strict=True),
    ]
    total_commission_estimate: Decimal = Field(ge=0)
    limit_price: Decimal = Field(gt=0)
    gross_assignment_obligation: Decimal = Field(gt=0)
    typed_symbol: str
    acknowledged_quantity: Annotated[
        int,
        Field(gt=0, le=MAX_CONFIRMATION_INTEGER, strict=True),
    ]
    acknowledged_contract_id: Annotated[
        int,
        Field(gt=0, le=MAX_CONFIRMATION_INTEGER, strict=True),
    ]
    acknowledged_limit_price: Decimal = Field(gt=0)
    acknowledged_gross_assignment_obligation: Decimal = Field(gt=0)
    risk_terms_acknowledged: Annotated[bool, Field(strict=True)]

    @field_validator("symbol")
    @classmethod
    def normalize_target_symbol(cls, value: str) -> str:
        if len(value) > MAX_TYPED_SYMBOL_CHARACTERS:
            raise ValueError("symbol is too long")
        normalized = value.strip().upper()
        if not normalized or not normalized.isalnum():
            raise ValueError("symbol must be a nonblank alphanumeric code")
        return normalized

    @field_validator("typed_symbol")
    @classmethod
    def bound_exact_typed_symbol(cls, value: str) -> str:
        if not value or len(value) > MAX_TYPED_SYMBOL_CHARACTERS or not value.isprintable():
            raise ValueError("typed_symbol must be a bounded printable value")
        # Deliberately preserve spaces and case so service comparison is exact.
        return value

    @field_validator("total_commission_estimate", mode="before")
    @classmethod
    def bound_raw_commission(cls, value: object) -> object:
        return _bound_raw_decimal(
            value,
            max_characters=MAX_COMMISSION_INPUT_CHARACTERS,
            label="total_commission_estimate",
        )

    @field_validator("limit_price", "acknowledged_limit_price", mode="before")
    @classmethod
    def bound_raw_limit(cls, value: object) -> object:
        return _bound_raw_decimal(
            value,
            max_characters=MAX_LIMIT_INPUT_CHARACTERS,
            label="limit price",
        )

    @field_validator(
        "gross_assignment_obligation",
        "acknowledged_gross_assignment_obligation",
        mode="before",
    )
    @classmethod
    def bound_raw_obligation(cls, value: object) -> object:
        return _bound_raw_decimal(
            value,
            max_characters=MAX_ASSIGNMENT_OBLIGATION_INPUT_CHARACTERS,
            label="gross assignment obligation",
        )

    @field_validator("total_commission_estimate")
    @classmethod
    def require_bounded_commission(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("total_commission_estimate must be finite")
        if value > MAX_TOTAL_COMMISSION_ESTIMATE:
            raise ValueError("total_commission_estimate exceeds the supported bound")
        if not commission_estimate_has_bounded_representation(value):
            raise ValueError("total_commission_estimate has an unsupported representation")
        if commission_estimate_decimal_places(value) > MAX_COMMISSION_DECIMAL_PLACES:
            raise ValueError("total_commission_estimate has too many fractional decimal places")
        return value

    @field_validator("limit_price", "acknowledged_limit_price")
    @classmethod
    def require_bounded_limit(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("limit price must be finite")
        if value > MAX_LIMIT_PRICE:
            raise ValueError("limit price exceeds the supported bound")
        if not limit_price_has_bounded_representation(value):
            raise ValueError("limit price has an unsupported representation")
        if limit_price_decimal_places(value) > MAX_LIMIT_DECIMAL_PLACES:
            raise ValueError("limit price has too many fractional decimal places")
        return value

    @field_validator(
        "gross_assignment_obligation",
        "acknowledged_gross_assignment_obligation",
    )
    @classmethod
    def require_bounded_obligation(cls, value: Decimal) -> Decimal:
        return _validate_assignment_obligation(value)


class ShortPutDemoApprovalContract(ChronosModel):
    """Strict scalar contract summary; broker-origin descriptive text is omitted."""

    con_id: Annotated[int, Field(gt=0, le=MAX_CONFIRMATION_INTEGER, strict=True)]
    symbol: str
    expiration: date
    strike: Decimal = Field(gt=0)
    right: Literal[OptionRight.PUT] = OptionRight.PUT
    currency: str
    multiplier: Decimal = Field(gt=0)
    min_tick: Decimal = Field(gt=0)
    deliverable_shares: Decimal = Field(gt=0)
    deliverable_verified: Literal[True] = True

    @field_validator("symbol")
    @classmethod
    def require_canonical_symbol(cls, value: str) -> str:
        if (
            not value
            or len(value) > MAX_TYPED_SYMBOL_CHARACTERS
            or not value.isascii()
            or not value.isalnum()
            or value != value.upper()
        ):
            raise ValueError("approval receipt symbol must be a canonical code")
        return value

    @field_validator("currency")
    @classmethod
    def require_canonical_currency(cls, value: str) -> str:
        if _RECEIPT_CURRENCY_PATTERN.fullmatch(value) is None:
            raise ValueError("approval receipt currency must be a canonical code")
        return value

    @field_validator("strike", "multiplier", "min_tick", "deliverable_shares")
    @classmethod
    def require_bounded_amount(cls, value: Decimal) -> Decimal:
        if not _decimal_has_bounded_representation(
            value,
            max_digits=MAX_FRESH_EVIDENCE_DECIMAL_DIGITS,
        ):
            raise ValueError("approval receipt contract amount is outside the supported bound")
        return value

    @model_validator(mode="after")
    def require_standard_deliverable(self) -> ShortPutDemoApprovalContract:
        if self.deliverable_shares != self.multiplier:
            raise ValueError("approval receipt requires one verified standard deliverable")
        return self

    @classmethod
    def from_option_contract(
        cls,
        contract: OptionContract,
    ) -> ShortPutDemoApprovalContract:
        """Copy only canonical scalar terms from a fully validated M7 contract."""

        if (
            contract.right is not OptionRight.PUT
            or contract.deliverable_shares is None
            or not contract.deliverable_verified
        ):
            raise ValueError("approval receipt requires a verified standard deliverable")
        return cls(
            con_id=contract.con_id,
            symbol=contract.symbol,
            expiration=contract.expiration,
            strike=contract.strike,
            right=OptionRight.PUT,
            currency=contract.currency,
            multiplier=contract.multiplier,
            min_tick=contract.min_tick,
            deliverable_shares=contract.deliverable_shares,
            deliverable_verified=True,
        )

    def matches_option_contract(self, contract: OptionContract) -> bool:
        """Compare every carried scalar without reading descriptive broker text."""

        return (
            self.con_id == contract.con_id
            and self.symbol == contract.symbol
            and self.expiration == contract.expiration
            and self.strike == contract.strike
            and self.right is contract.right
            and self.currency == contract.currency
            and self.multiplier == contract.multiplier
            and self.min_tick == contract.min_tick
            and self.deliverable_shares == contract.deliverable_shares
            and contract.deliverable_verified
        )


class ShortPutDemoApprovalReceipt(ChronosModel):
    """Sanitized immutable evidence that explicit DEMO terms were rehearsed."""

    approval_reference: str
    what_if_reference: str
    symbol: str
    selected_contract: ShortPutDemoApprovalContract
    quantity: Literal[1] = 1
    limit_price: Decimal = Field(gt=0)
    gross_assignment_obligation: Decimal = Field(gt=0)
    currency: str
    what_if_evaluated_at: AwareDatetime
    approval_rehearsed_at: AwareDatetime
    risk_terms_acknowledged: Literal[True] = True
    demo_rehearsal_only: Literal[True] = True
    lifecycle_transition_recorded: Literal[False] = False
    persistence_recorded: Literal[False] = False
    approval_authority_created: Literal[False] = False
    broker_request_included: Literal[False] = False
    broker_text_included: Literal[False] = False
    submission_authorized: Literal[False] = False
    submission_locked: Literal[True] = True
    opening_actions_locked: Literal[True] = True

    @field_validator("approval_reference")
    @classmethod
    def validate_approval_reference(cls, value: str) -> str:
        if len(value) != 40 or _APPROVAL_REFERENCE_PATTERN.fullmatch(value) is None:
            raise ValueError("approval_reference is not a canonical rehearsal reference")
        return value

    @field_validator("what_if_reference")
    @classmethod
    def validate_what_if_reference(cls, value: str) -> str:
        if len(value) != 40 or _WHAT_IF_REFERENCE_PATTERN.fullmatch(value) is None:
            raise ValueError("what_if_reference is not a canonical rehearsal reference")
        return value

    @field_validator("currency")
    @classmethod
    def require_canonical_currency(cls, value: str) -> str:
        if _RECEIPT_CURRENCY_PATTERN.fullmatch(value) is None:
            raise ValueError("approval receipt currency must be a canonical code")
        return value

    @field_validator("limit_price")
    @classmethod
    def require_bounded_receipt_limit(cls, value: Decimal) -> Decimal:
        if (
            not value.is_finite()
            or value > MAX_LIMIT_PRICE
            or not limit_price_has_bounded_representation(value)
            or limit_price_decimal_places(value) > MAX_LIMIT_DECIMAL_PLACES
        ):
            raise ValueError("approval receipt limit is outside the supported bound")
        return value

    @field_validator("gross_assignment_obligation")
    @classmethod
    def require_bounded_receipt_obligation(cls, value: Decimal) -> Decimal:
        return _validate_assignment_obligation(value)

    @model_validator(mode="after")
    def validate_receipt_coherence(self) -> ShortPutDemoApprovalReceipt:
        contract = self.selected_contract
        if (
            contract.symbol != self.symbol
            or contract.currency != self.currency
            or contract.right is not OptionRight.PUT
            or not contract.deliverable_verified
            or self.gross_assignment_obligation != contract.strike * contract.deliverable_shares
            or not _limit_is_tick_aligned(self.limit_price, contract.min_tick)
        ):
            raise ValueError("approval receipt contract identity is inconsistent")
        if self.approval_rehearsed_at < self.what_if_evaluated_at:
            raise ValueError("approval rehearsal cannot predate its what-if evidence")
        return self


class ShortPutDemoApprovalResult(ChronosModel):
    """One rehearsal attempt; no result can confer order authority."""

    request: ShortPutDemoApprovalRequest
    status: DemoApprovalStatus
    evaluated_at: AwareDatetime
    receipt: ShortPutDemoApprovalReceipt | None = None
    reasons: tuple[str, ...] = ()
    opening_actions_locked: Literal[True] = True

    @model_validator(mode="after")
    def validate_outcome(self) -> ShortPutDemoApprovalResult:
        if self.status is DemoApprovalStatus.APPROVAL_REHEARSED:
            if self.receipt is None or self.reasons:
                raise ValueError("A completed approval rehearsal requires fresh evidence")
            receipt = self.receipt
            if (
                self.request.typed_symbol != self.request.symbol
                or self.request.acknowledged_quantity != 1
                or self.request.acknowledged_contract_id != self.request.selected_contract_id
                or self.request.acknowledged_limit_price != self.request.limit_price
                or self.request.acknowledged_gross_assignment_obligation
                != self.request.gross_assignment_obligation
                or not self.request.risk_terms_acknowledged
                or receipt.symbol != self.request.symbol
                or receipt.selected_contract.con_id != self.request.selected_contract_id
                or receipt.quantity != 1
                or receipt.limit_price != self.request.limit_price
                or receipt.gross_assignment_obligation != self.request.gross_assignment_obligation
                or receipt.gross_assignment_obligation
                != receipt.selected_contract.strike * receipt.selected_contract.deliverable_shares
                or not _limit_is_tick_aligned(
                    receipt.limit_price,
                    receipt.selected_contract.min_tick,
                )
                or receipt.approval_rehearsed_at != self.evaluated_at
            ):
                raise ValueError("Approval rehearsal result evidence is inconsistent")
        elif self.receipt is not None or not self.reasons:
            raise ValueError("A withheld approval rehearsal requires reasons and no receipt")
        return self


class _DemoApprovalEvidenceError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShortPutDemoApprovalService:
    """Rerun M7 and compare every explicit affirmation to fresh DEMO evidence."""

    def __init__(
        self,
        what_if_preview: ShortPutDemoWhatIfService,
        broker_boundary: DemoApprovalBrokerBoundary,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        self._what_if_preview = what_if_preview
        self._broker_boundary = broker_boundary
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reference_factory = reference_factory or (lambda: f"CHR-APR-{uuid4().hex.upper()}")

    def rehearse(self, request: ShortPutDemoApprovalRequest) -> ShortPutDemoApprovalResult:
        """Refresh what-if evidence once; never consume UI-owned parent models."""

        request = ShortPutDemoApprovalRequest.model_validate(request.model_dump(mode="python"))
        attempted_at = self._now()
        try:
            settings = Settings.model_validate(self._settings.model_dump(mode="python"))
            broker = self._require_demo_boundary(settings)
            self._require_preflight_affirmations(request)
        except _DemoApprovalEvidenceError as error:
            return self._withheld(request, evaluated_at=attempted_at, reason=error.reason)
        except Exception:
            return self._withheld(
                request,
                evaluated_at=attempted_at,
                reason="The configured DEMO approval-rehearsal boundary is invalid.",
            )

        what_if_request = ShortPutDemoWhatIfRequest(
            symbol=request.symbol,
            selected_contract_id=request.selected_contract_id,
            total_commission_estimate=request.total_commission_estimate,
            limit_price=request.limit_price,
        )
        try:
            raw_what_if = self._what_if_preview.preview(what_if_request)
        except Exception as error:
            _LOGGER.warning(
                "DEMO approval what-if refresh failed; rehearsal remains locked",
                extra={
                    "event": "short_put_demo_approval_refresh_failed",
                    "symbol": request.symbol,
                    "contract_id": request.selected_contract_id,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=self._now(),
                reason="Fresh DEMO what-if evidence could not be obtained safely.",
            )

        completed_at = self._now()
        safe_what_if: ShortPutDemoWhatIfResult | None = None
        try:
            safe_what_if = ShortPutDemoWhatIfResult.model_validate(
                raw_what_if.model_dump(mode="python")
            )
            self._require_bounded_evidence(safe_what_if.model_dump(mode="python"))
            current_settings = Settings.model_validate(self._settings.model_dump(mode="python"))
            if current_settings != settings:
                raise _DemoApprovalEvidenceError(
                    "The DEMO configuration changed during approval rehearsal."
                )
            self._require_demo_boundary(current_settings, expected_broker=broker)
            preview = self._require_fresh_what_if(
                request,
                safe_what_if,
                expected_request=what_if_request,
                attempted_at=attempted_at,
                completed_at=completed_at,
                settings=current_settings,
            )
            approval_reference = self._reference_factory()
            if (
                not isinstance(approval_reference, str)
                or len(approval_reference) != 40
                or _APPROVAL_REFERENCE_PATTERN.fullmatch(approval_reference) is None
            ):
                raise _DemoApprovalEvidenceError(
                    "A safe internal approval-rehearsal reference could not be generated."
                )
            receipt = ShortPutDemoApprovalReceipt(
                approval_reference=approval_reference,
                what_if_reference=preview.correlation_id,
                symbol=request.symbol,
                selected_contract=ShortPutDemoApprovalContract.from_option_contract(
                    preview.selected_contract
                ),
                limit_price=request.limit_price,
                gross_assignment_obligation=request.gross_assignment_obligation,
                currency=preview.currency,
                what_if_evaluated_at=safe_what_if.evaluated_at,
                approval_rehearsed_at=completed_at,
            )
            success_result = ShortPutDemoApprovalResult(
                request=request,
                status=DemoApprovalStatus.APPROVAL_REHEARSED,
                evaluated_at=completed_at,
                receipt=receipt,
            )
            final_settings = Settings.model_validate(self._settings.model_dump(mode="python"))
            if final_settings != current_settings:
                raise _DemoApprovalEvidenceError(
                    "The DEMO configuration changed during approval rehearsal."
                )
            self._require_demo_boundary(final_settings, expected_broker=broker)
        except _DemoApprovalEvidenceError as error:
            return self._withheld(
                request,
                evaluated_at=completed_at,
                reason=error.reason,
            )
        except Exception as error:
            _LOGGER.warning(
                "DEMO approval evidence validation failed; rehearsal remains locked",
                extra={
                    "event": "short_put_demo_approval_validation_failed",
                    "symbol": request.symbol,
                    "contract_id": request.selected_contract_id,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=completed_at,
                reason="Fresh DEMO what-if evidence could not be validated safely.",
            )

        _LOGGER.info(
            "Short-put DEMO approval rehearsal completed",
            extra={
                "event": "short_put_demo_approval_completed",
                "symbol": request.symbol,
                "contract_id": request.selected_contract_id,
                "outcome": DemoApprovalStatus.APPROVAL_REHEARSED.value,
            },
        )
        return success_result

    def _require_demo_boundary(
        self,
        settings: Settings,
        *,
        expected_broker: DemoBroker | None = None,
    ) -> DemoBroker:
        broker = self._broker_boundary.broker
        if (
            settings.broker_mode is not BrokerMode.DEMO
            or not isinstance(broker, DemoBroker)
            or (expected_broker is not None and broker is not expected_broker)
            or type(self._what_if_preview) is not ShortPutDemoWhatIfService
            or not self._what_if_preview.is_bound_to_demo_boundary(
                self._broker_boundary,
                settings,
            )
        ):
            raise _DemoApprovalEvidenceError(
                "Explicit approval rehearsal is available only on the bound DEMO adapter."
            )
        return broker

    @staticmethod
    def _require_preflight_affirmations(request: ShortPutDemoApprovalRequest) -> None:
        if request.typed_symbol != request.symbol:
            raise _DemoApprovalEvidenceError(
                "The typed symbol does not exactly match the selected symbol."
            )
        if request.acknowledged_quantity != 1:
            raise _DemoApprovalEvidenceError(
                "The acknowledged quantity must exactly match one contract."
            )
        if request.acknowledged_contract_id != request.selected_contract_id:
            raise _DemoApprovalEvidenceError(
                "The acknowledged contract does not match the selected contract."
            )
        if request.acknowledged_limit_price != request.limit_price:
            raise _DemoApprovalEvidenceError(
                "The acknowledged limit does not match the selected limit."
            )
        if request.acknowledged_gross_assignment_obligation != request.gross_assignment_obligation:
            raise _DemoApprovalEvidenceError(
                "The acknowledged assignment obligation does not match the displayed amount."
            )
        if not request.risk_terms_acknowledged:
            raise _DemoApprovalEvidenceError(
                "The risk and rehearsal terms must be explicitly acknowledged."
            )

    @staticmethod
    def _require_fresh_what_if(
        request: ShortPutDemoApprovalRequest,
        what_if: ShortPutDemoWhatIfResult,
        *,
        expected_request: ShortPutDemoWhatIfRequest,
        attempted_at: datetime,
        completed_at: datetime,
        settings: Settings,
    ) -> ShortPutDemoWhatIfPreview:
        preview = what_if.preview
        risk = what_if.risk_refresh
        if (
            what_if.request != expected_request
            or what_if.status is not DemoWhatIfStatus.WHAT_IF_PREVIEWED
            or preview is None
            or risk is None
            or what_if.reasons
            or not what_if.opening_actions_locked
            or not _what_if_terms_are_coherent(request, what_if, settings=settings)
        ):
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence did not produce the exact selected what-if preview."
            )
        expected_risk_request = ShortPutRiskPreviewRequest(
            symbol=request.symbol,
            selected_contract_id=request.selected_contract_id,
            total_commission_estimate=request.total_commission_estimate,
        )
        refresh = risk.candidate_refresh
        risk_preview = risk.preview
        if (
            risk.request != expected_risk_request
            or risk.status is not RiskPreviewStatus.READY
            or risk_preview is None
            or refresh is None
            or risk.reasons
            or preview.symbol != request.symbol
            or preview.selected_contract.con_id != request.selected_contract_id
            or preview.quantity != 1
            or preview.limit_price != request.limit_price
            or preview.operator_commission_estimate != request.total_commission_estimate
            or preview.gross_assignment_obligation != request.gross_assignment_obligation
            or risk_preview.symbol != request.symbol
            or risk_preview.selected_contract != preview.selected_contract
            or risk_preview.total_commission_estimate != request.total_commission_estimate
            or risk_preview.gross_assignment_obligation != request.gross_assignment_obligation
            or preview.currency != preview.selected_contract.currency
            or preview.transmit
            or preview.outside_rth
            or not preview.demo_rehearsal_only
            or preview.broker_authorized
            or preview.user_confirmation_available
            or preview.submission_authorized
            or not preview.opening_actions_locked
        ):
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence does not exactly match the affirmed DEMO terms."
            )
        expected_warning_notice = (
            None
            if preview.broker_warning_count == 0
            else (
                f"The deterministic broker returned {preview.broker_warning_count} "
                "rehearsal warning(s); no broker text or authority is carried forward."
            )
        )
        if preview.broker_warning_notice != expected_warning_notice:
            raise _DemoApprovalEvidenceError("Fresh M7 warning evidence was not presentation-safe.")
        underlying_quote = refresh.underlying_quote
        reconciliation = refresh.reconciliation
        if underlying_quote is None or reconciliation is None or reconciliation.snapshot is None:
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence is missing its observation lineage."
            )
        snapshot = reconciliation.snapshot
        timestamps = (
            risk_preview.evidence_evaluated_at,
            risk_preview.underlying_observed_at,
            refresh.evaluated_at,
            underlying_quote.timestamp,
            snapshot.captured_at,
            snapshot.account.as_of,
            risk.evaluated_at,
            preview.evidence_evaluated_at,
            preview.broker_previewed_at,
            what_if.evaluated_at,
        )
        if (
            completed_at < attempted_at
            or preview.evidence_evaluated_at != risk.evaluated_at
            or risk_preview.evidence_evaluated_at != refresh.evaluated_at
            or risk_preview.underlying_observed_at != underlying_quote.timestamp
            or underlying_quote.timestamp > refresh.evaluated_at
            or refresh.evaluated_at > risk.evaluated_at
            or refresh.evaluated_at < attempted_at
            or risk.evaluated_at < attempted_at
            or snapshot.captured_at > refresh.evaluated_at
            or snapshot.account.as_of > refresh.evaluated_at
            or preview.broker_previewed_at < preview.evidence_evaluated_at
            or what_if.evaluated_at < preview.broker_previewed_at
            or what_if.evaluated_at < attempted_at
            or what_if.evaluated_at > completed_at
        ):
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence has an impossible approval chronology."
            )
        max_age_seconds = min(
            Decimal(settings.max_quote_age_seconds),
            MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
        )
        if any(
            not ShortPutDemoApprovalService._timestamp_is_fresh(
                value,
                now=completed_at,
                max_age_seconds=max_age_seconds,
            )
            for value in timestamps
        ):
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence expired before approval rehearsal completed."
            )
        return preview

    @staticmethod
    def _require_bounded_evidence(value: object, *, depth: int = 0) -> None:
        if depth > 64:
            raise _DemoApprovalEvidenceError(
                "Fresh M7 evidence exceeds the supported nesting bound."
            )
        if isinstance(value, str):
            if len(value) > MAX_FRESH_EVIDENCE_STRING_CHARACTERS or not value.isprintable():
                raise _DemoApprovalEvidenceError(
                    "Fresh M7 evidence contains an unsupported text value."
                )
            return
        if isinstance(value, Decimal):
            if not _decimal_has_bounded_representation(
                value,
                max_digits=MAX_FRESH_EVIDENCE_DECIMAL_DIGITS,
            ):
                raise _DemoApprovalEvidenceError(
                    "Fresh M7 evidence contains an unsupported decimal value."
                )
            return
        if isinstance(value, dict):
            for key, item in value.items():
                ShortPutDemoApprovalService._require_bounded_evidence(
                    key,
                    depth=depth + 1,
                )
                ShortPutDemoApprovalService._require_bounded_evidence(
                    item,
                    depth=depth + 1,
                )
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                ShortPutDemoApprovalService._require_bounded_evidence(
                    item,
                    depth=depth + 1,
                )

    @staticmethod
    def _timestamp_is_fresh(
        value: datetime,
        *,
        now: datetime,
        max_age_seconds: Decimal,
    ) -> bool:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return False
        delta = now - value
        if delta.total_seconds() < 0:
            return False
        age = (
            Decimal(delta.days) * Decimal("86400")
            + Decimal(delta.seconds)
            + Decimal(delta.microseconds) / Decimal("1000000")
        )
        return age <= max_age_seconds

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval rehearsal clock must return an aware datetime")
        return value

    @staticmethod
    def _withheld(
        request: ShortPutDemoApprovalRequest,
        *,
        evaluated_at: datetime,
        reason: str,
    ) -> ShortPutDemoApprovalResult:
        return ShortPutDemoApprovalResult(
            request=request,
            status=DemoApprovalStatus.WITHHELD,
            evaluated_at=evaluated_at,
            reasons=(reason,),
        )

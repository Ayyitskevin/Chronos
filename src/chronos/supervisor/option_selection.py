"""Pure autonomous option selection and canonical evidence receipts (ADR-0020).

This module is intentionally unable to reach a broker or an order service.  It
accepts one immutable, complete evidence value and returns one immutable
receipt.  The app plane owns read-only acquisition; the existing compiler,
risk, reconciliation, lease, and submission boundary own everything after a
selection.  Missing or contradictory facts are data, not exceptions: they
produce a typed ``NO_TRADE`` receipt.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from datetime import UTC, date, datetime
from decimal import (
    ROUND_CEILING,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
    localcontext,
)
from enum import Enum, StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, PositiveInt, field_validator, model_validator

from chronos.autonomy import (
    LIVE_AUTONOMY_MODES,
    AutonomyMode,
    AutonomyModel,
    DecisionKind,
    OrderForm,
    StrategyForm,
    TradableAssetClass,
    TradingSession,
)
from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
    MAX_OPTION_SELECTION_DECIMAL_DIGITS,
    MAX_OPTION_SELECTION_DECIMAL_EXPONENT,
    MAX_OPTION_SELECTION_DERIVED_DECIMAL_DIGITS,
    MAX_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT,
    MAX_OPTION_SELECTION_OTHER_ASSETS,
    MAX_OPTION_SELECTION_POLICY_CODES,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
    MAX_OPTION_SELECTION_REJECTIONS,
    MAX_OPTION_SELECTION_TEXT_CHARS,
    MAX_OPTION_SELECTION_WEIGHT,
    MIN_OPTION_SELECTION_DECIMAL_EXPONENT,
    MIN_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT,
    validate_option_selection_decimal,
)
from chronos.domain.enums import DataQuality, OptionRight, ProductFamily
from chronos.domain.models import (
    ChronosModel,
    Instrument,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    UnderlyingContract,
)
from chronos.services.liquid_hours import parse_liquid_hours
from chronos.services.trading_hours import session_for
from chronos.utils.identifiers import normalize_account_fingerprint

REQUEST_SCHEMA_VERSION = "option-selection-request-v1"
POLICY_SCHEMA_VERSION = "option-selection-policy-v1"
EVIDENCE_SCHEMA_VERSION = "option-selection-evidence-v1"
RECEIPT_SCHEMA_VERSION = "option-selection-receipt-v1"
RESOLVER_VERSION = "deterministic-option-selector-v1"
SCORING_VERSION = "option-score-v1"
CANONICALIZATION_VERSION = "chronos-canonical-json-v1"
BROKER_RESOLVER_VERSION: Literal["option-evidence-port-v1"] = "option-evidence-port-v1"
OPTION_SELECTION_STREAM = "autonomy.option-selections"
PROMOTION_SCHEMA_VERSION = "option-resolver-promotion-v1"

_ZERO_DIGEST = "0" * 64
_MARKET_PROTECTION_COLLAR = Decimal("0.01")
_SCORE_QUANTUM = Decimal("0.000000000001")
_ARITHMETIC_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN)


def _validate_receipt_value_tree(
    value: object,
    *,
    derived: bool = False,
) -> None:
    """Bound nested evidence values before canonical normalization or rendering."""

    if isinstance(value, Decimal):
        validate_option_selection_decimal(
            value,
            "option-selection evidence number",
            max_digits=(
                MAX_OPTION_SELECTION_DERIVED_DECIMAL_DIGITS
                if derived
                else MAX_OPTION_SELECTION_DECIMAL_DIGITS
            ),
            min_exponent=(
                MIN_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT
                if derived
                else MIN_OPTION_SELECTION_DECIMAL_EXPONENT
            ),
            max_exponent=(
                MAX_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT
                if derived
                else MAX_OPTION_SELECTION_DECIMAL_EXPONENT
            ),
        )
        return
    if isinstance(value, str):
        if len(value) > MAX_OPTION_SELECTION_TEXT_CHARS:
            raise ValueError("option-selection text exceeds the hard evidence bound")
        normalized = unicodedata.normalize("NFC", value)
        if len(normalized) > MAX_OPTION_SELECTION_TEXT_CHARS:
            raise ValueError("normalized option-selection text exceeds the hard evidence bound")
        return
    if isinstance(value, OptionMarketRule) and (
        len(value.price_increments) > MAX_OPTION_MARKET_RULE_INCREMENTS
    ):
        raise ValueError("option market-rule increments exceed the hard evidence bound")
    if (
        isinstance(value, OptionDeliverableFacts)
        and value.other_assets is not None
        and len(value.other_assets) > MAX_OPTION_SELECTION_OTHER_ASSETS
    ):
        raise ValueError("option deliverable assets exceed the hard evidence bound")
    if isinstance(value, ChronosModel):
        for field_name in type(value).model_fields:
            _validate_receipt_value_tree(getattr(value, field_name), derived=derived)
        return
    if isinstance(value, dict):
        for item in value.values():
            _validate_receipt_value_tree(item, derived=derived)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_receipt_value_tree(item, derived=derived)


class OptionResolverPromotionArtifact(AutonomyModel):
    """Owner-authored live promotion; runtime has no creator or writer."""

    schema_version: Literal["option-resolver-promotion-v1"] = "option-resolver-promotion-v1"
    account_fingerprint: str
    mandate_digest: str
    policy_digest: str
    resolver_compatibility_digest: str
    request_schema_version: str
    evidence_schema_version: str
    receipt_schema_version: str
    resolver_version: str
    scoring_version: str
    canonicalization_version: str
    broker_resolver_version: str
    permitted_modes: tuple[AutonomyMode, ...]
    effective_from: AwareDatetime
    expires_at: AwareDatetime
    owner_authorization_ref: str = Field(min_length=1, max_length=128)
    authored_at: AwareDatetime

    @field_validator("account_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)

    @field_validator(
        "mandate_digest",
        "policy_digest",
        "resolver_compatibility_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError("promotion digests must be 64-character lowercase hex")
        return normalized

    @model_validator(mode="after")
    def validate_window_and_modes(self) -> OptionResolverPromotionArtifact:
        if self.expires_at <= self.effective_from:
            raise ValueError("resolver promotion expiry must follow its effective time")
        if len(self.permitted_modes) != 1 or any(
            mode not in LIVE_AUTONOMY_MODES for mode in self.permitted_modes
        ):
            raise ValueError("resolver promotion must name exactly one CANARY/LIVE mode")
        return self


class SelectionStatus(StrEnum):
    SELECTED = "SELECTED"
    NO_TRADE = "NO_TRADE"


class SelectionRejectionCode(StrEnum):
    """Stable refusal vocabulary for activation, acquisition, and candidates."""

    FEATURE_DISABLED = "FEATURE_DISABLED"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    PROMOTION_REQUIRED = "PROMOTION_REQUIRED"
    PROMOTION_MISMATCH = "PROMOTION_MISMATCH"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"
    EVIDENCE_TRUNCATED = "EVIDENCE_TRUNCATED"
    OBSERVATION_SET_MISMATCH = "OBSERVATION_SET_MISMATCH"
    PACING_EXHAUSTED = "PACING_EXHAUSTED"
    DATA_PERMISSION_DENIED = "DATA_PERMISSION_DENIED"
    DATA_CLEANUP_FAILED = "DATA_CLEANUP_FAILED"
    UNDERLYING_MISSING = "UNDERLYING_MISSING"
    UNDERLYING_TYPE_MISMATCH = "UNDERLYING_TYPE_MISMATCH"
    UNDERLYING_SYMBOL_MISMATCH = "UNDERLYING_SYMBOL_MISMATCH"
    UNDERLYING_CURRENCY_MISMATCH = "UNDERLYING_CURRENCY_MISMATCH"
    UNDERLYING_QUOTE_MISSING = "UNDERLYING_QUOTE_MISSING"
    UNDERLYING_QUOTE_IDENTITY_MISMATCH = "UNDERLYING_QUOTE_IDENTITY_MISMATCH"
    UNDERLYING_QUOTE_FUTURE = "UNDERLYING_QUOTE_FUTURE"
    UNDERLYING_QUOTE_STALE = "UNDERLYING_QUOTE_STALE"
    UNDERLYING_DATA_QUALITY = "UNDERLYING_DATA_QUALITY"
    UNDERLYING_BOOK_INVALID = "UNDERLYING_BOOK_INVALID"
    CHAIN_MISSING = "CHAIN_MISSING"
    CHAIN_AMBIGUOUS = "CHAIN_AMBIGUOUS"
    CHAIN_INCOMPLETE = "CHAIN_INCOMPLETE"
    CHAIN_FUTURE = "CHAIN_FUTURE"
    CHAIN_STALE = "CHAIN_STALE"
    CHAIN_IDENTITY_MISMATCH = "CHAIN_IDENTITY_MISMATCH"
    CHAIN_EXCHANGE_NOT_PERMITTED = "CHAIN_EXCHANGE_NOT_PERMITTED"
    CHAIN_FAMILY_NOT_PERMITTED = "CHAIN_FAMILY_NOT_PERMITTED"
    CHAIN_MULTIPLIER_MISMATCH = "CHAIN_MULTIPLIER_MISMATCH"
    CHAIN_DUPLICATE_VALUE = "CHAIN_DUPLICATE_VALUE"
    DUPLICATE_CONTRACT_ID = "DUPLICATE_CONTRACT_ID"
    CONTRACT_TYPE_MISMATCH = "CONTRACT_TYPE_MISMATCH"
    CONTRACT_SYMBOL_MISMATCH = "CONTRACT_SYMBOL_MISMATCH"
    CONTRACT_UNDERLYING_MISMATCH = "CONTRACT_UNDERLYING_MISMATCH"
    CONTRACT_RIGHT_MISMATCH = "CONTRACT_RIGHT_MISMATCH"
    CONTRACT_CURRENCY_MISMATCH = "CONTRACT_CURRENCY_MISMATCH"
    CONTRACT_EXCHANGE_NOT_PERMITTED = "CONTRACT_EXCHANGE_NOT_PERMITTED"
    CONTRACT_FAMILY_NOT_PERMITTED = "CONTRACT_FAMILY_NOT_PERMITTED"
    CONTRACT_MULTIPLIER_MISMATCH = "CONTRACT_MULTIPLIER_MISMATCH"
    CONTRACT_CHAIN_IDENTITY_MISMATCH = "CONTRACT_CHAIN_IDENTITY_MISMATCH"
    CONTRACT_NOT_IN_CHAIN = "CONTRACT_NOT_IN_CHAIN"
    CONTRACT_DETAILS_STALE = "CONTRACT_DETAILS_STALE"
    EXPIRATION_OUTSIDE_RANGE = "EXPIRATION_OUTSIDE_RANGE"
    MONEYNESS_MISMATCH = "MONEYNESS_MISMATCH"
    DELTA_MISSING = "DELTA_MISSING"
    DELTA_INVALID = "DELTA_INVALID"
    DELTA_SIGN_MISMATCH = "DELTA_SIGN_MISMATCH"
    DELTA_OUTSIDE_RANGE = "DELTA_OUTSIDE_RANGE"
    DELIVERABLE_MISSING = "DELIVERABLE_MISSING"
    DELIVERABLE_NOT_AUTHORITATIVE = "DELIVERABLE_NOT_AUTHORITATIVE"
    DELIVERABLE_NONSTANDARD = "DELIVERABLE_NONSTANDARD"
    SESSION_EVIDENCE_MISSING = "SESSION_EVIDENCE_MISSING"
    SESSION_CLOSED = "SESSION_CLOSED"
    SESSION_NOT_PERMITTED = "SESSION_NOT_PERMITTED"
    QUOTE_MISSING = "QUOTE_MISSING"
    QUOTE_IDENTITY_MISMATCH = "QUOTE_IDENTITY_MISMATCH"
    QUOTE_FUTURE = "QUOTE_FUTURE"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_DATA_QUALITY = "QUOTE_DATA_QUALITY"
    QUOTE_ONE_SIDED = "QUOTE_ONE_SIDED"
    QUOTE_ZERO_BID = "QUOTE_ZERO_BID"
    QUOTE_CROSSED = "QUOTE_CROSSED"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    VOLUME_UNKNOWN = "VOLUME_UNKNOWN"
    VOLUME_TOO_LOW = "VOLUME_TOO_LOW"
    OPEN_INTEREST_UNKNOWN = "OPEN_INTEREST_UNKNOWN"
    OPEN_INTEREST_TOO_LOW = "OPEN_INTEREST_TOO_LOW"
    MARKET_RULE_MISSING = "MARKET_RULE_MISSING"
    MARKET_RULE_IDENTITY_MISMATCH = "MARKET_RULE_IDENTITY_MISMATCH"
    TICK_BAND_CROSSING = "TICK_BAND_CROSSING"
    PRICE_NOT_DERIVABLE = "PRICE_NOT_DERIVABLE"
    EVIDENCE_TIMESTAMP_CONFLICT = "EVIDENCE_TIMESTAMP_CONFLICT"
    ACQUISITION_PLAN_MISSING = "ACQUISITION_PLAN_MISSING"
    ACQUISITION_PLAN_CONFLICT = "ACQUISITION_PLAN_CONFLICT"
    CANDIDATE_SET_EMPTY = "CANDIDATE_SET_EMPTY"
    CANDIDATE_SET_MISMATCH = "CANDIDATE_SET_MISMATCH"


_SNAPSHOT_INTEGRITY_CODES = frozenset(
    {
        SelectionRejectionCode.EVIDENCE_PARTIAL,
        SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
        SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
        SelectionRejectionCode.DUPLICATE_CONTRACT_ID,
        SelectionRejectionCode.CONTRACT_TYPE_MISMATCH,
        SelectionRejectionCode.CONTRACT_SYMBOL_MISMATCH,
        SelectionRejectionCode.CONTRACT_UNDERLYING_MISMATCH,
        SelectionRejectionCode.CONTRACT_RIGHT_MISMATCH,
        SelectionRejectionCode.CONTRACT_CURRENCY_MISMATCH,
        SelectionRejectionCode.CONTRACT_EXCHANGE_NOT_PERMITTED,
        SelectionRejectionCode.CONTRACT_FAMILY_NOT_PERMITTED,
        SelectionRejectionCode.CONTRACT_MULTIPLIER_MISMATCH,
        SelectionRejectionCode.CONTRACT_CHAIN_IDENTITY_MISMATCH,
        SelectionRejectionCode.CONTRACT_NOT_IN_CHAIN,
        SelectionRejectionCode.CONTRACT_DETAILS_STALE,
        SelectionRejectionCode.DELTA_MISSING,
        SelectionRejectionCode.DELTA_INVALID,
        SelectionRejectionCode.DELTA_SIGN_MISMATCH,
        SelectionRejectionCode.DELIVERABLE_MISSING,
        SelectionRejectionCode.SESSION_EVIDENCE_MISSING,
        SelectionRejectionCode.QUOTE_MISSING,
        SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH,
        SelectionRejectionCode.QUOTE_FUTURE,
        SelectionRejectionCode.QUOTE_STALE,
        SelectionRejectionCode.VOLUME_UNKNOWN,
        SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,
        SelectionRejectionCode.MARKET_RULE_MISSING,
        SelectionRejectionCode.MARKET_RULE_IDENTITY_MISMATCH,
    }
)


class SelectionRejection(AutonomyModel):
    code: SelectionRejectionCode
    detail: str = Field(min_length=1, max_length=500)


class OptionSelectionRequest(AutonomyModel):
    """Economic option intent; structurally incapable of naming a contract."""

    schema_version: Literal["option-selection-request-v1"] = "option-selection-request-v1"
    decision_id: str = Field(min_length=1, max_length=128)
    decision_kind: Literal[DecisionKind.OPEN] = DecisionKind.OPEN
    asset_class: Literal[TradableAssetClass.EQUITY_OPTION] = TradableAssetClass.EQUITY_OPTION
    symbol: str = Field(min_length=1, max_length=32)
    strategy: StrategyForm
    right: OptionRight
    requested_contracts: Decimal | None = Field(default=None, gt=0)
    moneyness: Literal["OTM"] = "OTM"
    currency: Literal["USD"] = "USD"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("option-selection symbol must not be blank")
        return normalized

    @field_validator(
        "requested_contracts",
    )
    @classmethod
    def require_finite_request_numbers(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            validate_option_selection_decimal(value, "option-selection request number")
        return value

    @model_validator(mode="after")
    def validate_economic_shape(self) -> OptionSelectionRequest:
        expected = {
            StrategyForm.CASH_SECURED_PUT: OptionRight.PUT,
            StrategyForm.COVERED_CALL: OptionRight.CALL,
        }.get(self.strategy)
        if expected is not None and self.right is not expected:
            raise ValueError(f"{self.strategy.value} requires option right {expected.value}")
        return self


class OptionSelectionPolicy(AutonomyModel):
    """Owner/runtime constraints applied independently of the model request."""

    schema_version: Literal["option-selection-policy-v1"] = "option-selection-policy-v1"
    max_quote_age_seconds: Decimal = Field(gt=0)
    max_chain_age_seconds: Decimal = Field(gt=0)
    max_candidate_expirations: PositiveInt
    max_strikes_per_expiration: PositiveInt
    min_dte: int = Field(ge=0)
    target_dte: int = Field(ge=0)
    max_dte: int = Field(ge=0)
    min_abs_delta: Decimal = Field(gt=0, lt=1)
    target_abs_delta: Decimal = Field(gt=0, lt=1)
    max_abs_delta: Decimal = Field(gt=0, lt=1)
    permitted_data_qualities: tuple[DataQuality, ...]
    min_option_volume: int = Field(ge=0)
    min_open_interest: int = Field(ge=0)
    max_relative_spread: Decimal = Field(ge=0, le=1)
    permitted_exchanges: tuple[str, ...]
    permitted_trading_classes: tuple[str, ...]
    permitted_sessions: tuple[TradingSession, ...]
    required_multiplier: Decimal = Field(gt=0)
    order_form: OrderForm
    market_timezone: str = Field(default="America/New_York", min_length=1, max_length=128)
    delta_weight: Decimal = Field(ge=0, le=MAX_OPTION_SELECTION_WEIGHT)
    spread_weight: Decimal = Field(ge=0, le=MAX_OPTION_SELECTION_WEIGHT)
    dte_weight: Decimal = Field(ge=0, le=MAX_OPTION_SELECTION_WEIGHT)
    liquidity_weight: Decimal = Field(ge=0, le=MAX_OPTION_SELECTION_WEIGHT)

    @field_validator("permitted_exchanges", "permitted_trading_classes")
    @classmethod
    def normalize_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_OPTION_SELECTION_POLICY_CODES:
            raise ValueError("option-selection policy codes exceed the hard bound")
        for item in value:
            _validate_receipt_value_tree(item)
        return tuple(sorted({item.strip().upper() for item in value if item.strip()}))

    @field_validator(
        "max_quote_age_seconds",
        "max_chain_age_seconds",
        "max_relative_spread",
        "required_multiplier",
        "min_abs_delta",
        "target_abs_delta",
        "max_abs_delta",
        "delta_weight",
        "spread_weight",
        "dte_weight",
        "liquidity_weight",
    )
    @classmethod
    def require_finite_policy_numbers(cls, value: Decimal) -> Decimal:
        return validate_option_selection_decimal(value, "option-selection policy number")

    @field_validator("permitted_data_qualities")
    @classmethod
    def normalize_qualities(cls, value: tuple[DataQuality, ...]) -> tuple[DataQuality, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("permitted_sessions")
    @classmethod
    def normalize_sessions(cls, value: tuple[TradingSession, ...]) -> tuple[TradingSession, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @model_validator(mode="after")
    def validate_policy(self) -> OptionSelectionPolicy:
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("DTE bounds must satisfy min <= target <= max")
        if not self.min_abs_delta <= self.target_abs_delta <= self.max_abs_delta:
            raise ValueError("delta bounds must satisfy min <= target <= max")
        if (
            self.max_candidate_expirations > MAX_CANDIDATE_EXPIRATIONS
            or self.max_strikes_per_expiration > MAX_CANDIDATE_STRIKES_PER_EXPIRATION
            or self.max_candidate_expirations * self.max_strikes_per_expiration
            > MAX_CANDIDATE_REQUEST_CONTRACTS
        ):
            raise ValueError("candidate acquisition bounds exceed the hard request limit")
        if self.delta_weight + self.spread_weight + self.dte_weight + self.liquidity_weight <= 0:
            raise ValueError("at least one scoring weight must be positive")
        try:
            ZoneInfo(self.market_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("market_timezone must name an installed IANA timezone") from error
        _validate_receipt_value_tree(self)
        return self


class CandidateEvidence(AutonomyModel):
    """All observations for one qualified candidate, including unknowns."""

    contract: Instrument
    qualified_at: AwareDatetime
    quote: MarketQuote | None = None
    quote_fetched_at: AwareDatetime | None = None
    quote_observed_at: AwareDatetime | None = None
    market_rule: OptionMarketRule | None = None
    market_rule_observed_at: AwareDatetime | None = None
    deliverable: OptionDeliverableFacts | None = None
    deliverable_observed_at: AwareDatetime | None = None


class ObservedMarketRuleEvidence(AutonomyModel):
    """One raw rule observation, retained even when its identity is contaminating."""

    market_rule: OptionMarketRule
    observed_at: AwareDatetime


class ObservedDeliverableEvidence(AutonomyModel):
    """One raw deliverable observation before candidate-identity projection."""

    deliverable: OptionDeliverableFacts
    observed_at: AwareDatetime


class ObservedOptionQuoteEvidence(AutonomyModel):
    """One raw quote observation before candidate-identity projection."""

    quote: MarketQuote
    fetched_at: AwareDatetime
    observed_at: AwareDatetime


class CandidateAcquisitionPlan(AutonomyModel):
    """Deterministic bounded fan-out, including what the bounds excluded."""

    as_of: date
    spot: Decimal = Field(gt=0)
    max_expirations: PositiveInt
    max_strikes_per_expiration: PositiveInt
    eligible_expirations: tuple[date, ...]
    selected_expirations: tuple[date, ...]
    excluded_expirations: tuple[date, ...]
    selected_strikes: tuple[Decimal, ...]
    excluded_strikes: tuple[Decimal, ...]
    requested_specs: tuple[OptionContractSpec, ...]

    @field_validator("spot", "selected_strikes", "excluded_strikes")
    @classmethod
    def require_finite_plan_numbers(
        cls,
        value: Decimal | tuple[Decimal, ...],
    ) -> Decimal | tuple[Decimal, ...]:
        values = (value,) if isinstance(value, Decimal) else value
        for item in values:
            validate_option_selection_decimal(item, "candidate acquisition plan number")
        return value

    @model_validator(mode="after")
    def bound_plan_collections(self) -> CandidateAcquisitionPlan:
        if self.max_expirations > MAX_CANDIDATE_EXPIRATIONS:
            raise ValueError("candidate plan expiration bound exceeds the hard limit")
        if self.max_strikes_per_expiration > MAX_CANDIDATE_STRIKES_PER_EXPIRATION:
            raise ValueError("candidate plan strike bound exceeds the hard limit")
        if (
            len(self.eligible_expirations) > MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW
            or len(self.excluded_expirations) > MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW
            or len(self.selected_expirations) > MAX_CANDIDATE_EXPIRATIONS
            or len(self.selected_strikes) > MAX_CANDIDATE_STRIKES_PER_EXPIRATION
            or len(self.excluded_strikes) > MAX_OPTION_CHAIN_STRIKES_PER_ROW
            or len(self.requested_specs) > MAX_CANDIDATE_REQUEST_CONTRACTS
        ):
            raise ValueError("candidate acquisition plan exceeds a hard evidence bound")
        return self


class OptionSelectionEvidence(AutonomyModel):
    """One bounded, atomic evidence snapshot; incomplete is never rankable."""

    schema_version: Literal["option-selection-evidence-v1"] = "option-selection-evidence-v1"
    broker_resolver_version: Literal["option-evidence-port-v1"] = BROKER_RESOLVER_VERSION
    source: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    complete: bool
    truncated: bool = False
    completion_marker: str = Field(default="", max_length=128)
    acquisition_rejections: tuple[SelectionRejection, ...] = ()
    underlying: Instrument | None = None
    underlying_qualified_at: AwareDatetime | None = None
    underlying_quote: MarketQuote | None = None
    underlying_quote_fetched_at: AwareDatetime | None = None
    underlying_quote_observed_at: AwareDatetime | None = None
    chain_parameters: tuple[OptionChainParameters, ...] = ()
    chain_fetched_at: AwareDatetime | None = None
    chain_observed_at: AwareDatetime | None = None
    chain_completion_observed_at: AwareDatetime | None = None
    chain_completion_source: str | None = Field(default=None, min_length=1, max_length=128)
    acquisition_plan: CandidateAcquisitionPlan | None = None
    observed_market_rules: tuple[ObservedMarketRuleEvidence, ...] = ()
    observed_deliverables: tuple[ObservedDeliverableEvidence, ...] = ()
    observed_option_quotes: tuple[ObservedOptionQuoteEvidence, ...] = ()
    candidates: tuple[CandidateEvidence, ...] = ()

    @field_validator("acquisition_rejections")
    @classmethod
    def bound_acquisition_rejections(
        cls,
        value: tuple[SelectionRejection, ...],
    ) -> tuple[SelectionRejection, ...]:
        if len(value) > MAX_OPTION_SELECTION_REJECTIONS:
            raise ValueError("option acquisition rejections exceed the hard evidence bound")
        return value

    @field_validator(
        "observed_market_rules",
        "observed_deliverables",
        "observed_option_quotes",
    )
    @classmethod
    def bound_raw_observations(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("raw option observations exceed the hard request bound")
        return value

    @field_validator("chain_parameters")
    @classmethod
    def bound_chain_parameters(
        cls,
        value: tuple[OptionChainParameters, ...],
    ) -> tuple[OptionChainParameters, ...]:
        if len(value) > MAX_OPTION_CHAIN_ROWS or any(
            len(row.expirations) > MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW
            or len(row.strikes) > MAX_OPTION_CHAIN_STRIKES_PER_ROW
            for row in value
        ):
            raise ValueError("option-chain evidence exceeds a hard evidence bound")
        return value

    @field_validator("candidates")
    @classmethod
    def bound_candidates(
        cls,
        value: tuple[CandidateEvidence, ...],
    ) -> tuple[CandidateEvidence, ...]:
        if len(value) > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError("candidate evidence exceeds the hard request bound")
        return value

    @model_validator(mode="after")
    def bound_all_receipt_decimals(self) -> OptionSelectionEvidence:
        _validate_receipt_value_tree(self)
        return self


class ScoreComponent(AutonomyModel):
    name: Literal["delta", "spread", "dte", "liquidity"]
    normalized_value: Decimal = Field(ge=0)
    weight: Decimal = Field(ge=0)
    contribution: Decimal = Field(ge=0)


class TieBreakFacts(AutonomyModel):
    """The documented total order; ``con_id`` is intentionally last."""

    total_score: Decimal
    delta_error: Decimal
    relative_spread: Decimal
    dte_error: int
    negative_open_interest: int
    negative_volume: int
    expiration: date
    strike: Decimal
    con_id: PositiveInt

    def key(self) -> tuple[Decimal | int | date, ...]:
        return (
            self.total_score,
            self.delta_error,
            self.relative_spread,
            self.dte_error,
            self.negative_open_interest,
            self.negative_volume,
            self.expiration,
            self.strike,
            self.con_id,
        )


class CandidateEvaluation(AutonomyModel):
    evidence_digest: str
    con_id: int | None = None
    eligible: bool
    rejections: tuple[SelectionRejection, ...] = ()
    dte: int | None = None
    midpoint: Decimal | None = None
    relative_spread: Decimal | None = None
    abs_delta: Decimal | None = None
    applicable_tick: Decimal | None = None
    limit_price: Decimal | None = None
    score_components: tuple[ScoreComponent, ...] = ()
    total_score: Decimal | None = None
    tie_break: TieBreakFacts | None = None


class SelectedOption(AutonomyModel):
    contract: OptionContract
    quote: MarketQuote
    applicable_tick: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    sizing_reference_price: Decimal = Field(gt=0)
    deliverable_shares: Decimal = Field(gt=0)
    candidate_evidence_digest: str


class OptionSelectionReceipt(AutonomyModel):
    schema_version: Literal["option-selection-receipt-v1"] = "option-selection-receipt-v1"
    resolver_version: str = RESOLVER_VERSION
    scoring_version: str = SCORING_VERSION
    canonicalization_version: str = CANONICALIZATION_VERSION
    broker_resolver_version: str
    resolver_compatibility_digest: str
    mandate_digest: str
    promotion_digest: str | None = None
    evaluated_at: AwareDatetime
    request: OptionSelectionRequest
    policy: OptionSelectionPolicy
    evidence: OptionSelectionEvidence
    request_digest: str
    policy_digest: str
    evidence_digest: str
    canonical_input_digest: str
    global_rejections: tuple[SelectionRejection, ...] = ()
    candidates: tuple[CandidateEvaluation, ...] = ()
    status: SelectionStatus
    selected: SelectedOption | None = None
    no_trade_reasons: tuple[SelectionRejection, ...] = ()
    output_digest: str

    @field_validator(
        "resolver_compatibility_digest",
        "mandate_digest",
        "promotion_digest",
        "request_digest",
        "policy_digest",
        "evidence_digest",
        "canonical_input_digest",
        "output_digest",
    )
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError("receipt digests must be 64-character lowercase hex")
        return normalized

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> OptionSelectionReceipt:
        # The evidence boundary validates broker numbers, but a persisted or
        # externally supplied receipt can also carry decimals in derived score,
        # tie-break, and selected-price fields. Bound the complete receipt
        # before any canonical fixed-point rendering can allocate from a tiny
        # exponent-form input such as ``1E-1000000``.
        _validate_receipt_value_tree(self, derived=True)
        if self.status is SelectionStatus.SELECTED:
            if self.selected is None or self.no_trade_reasons:
                raise ValueError(
                    "a selected receipt requires one selection and no NO_TRADE reasons"
                )
        elif self.selected is not None or not self.no_trade_reasons:
            raise ValueError("a NO_TRADE receipt requires reasons and no selected contract")
        return self

    def digest_material_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(exclude={"output_digest"}))

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self)

    def verifies(self) -> bool:
        if hashlib.sha256(self.digest_material_bytes()).hexdigest() != self.output_digest:
            return False
        rebuilt = select_option(
            request=self.request,
            policy=self.policy,
            evidence=self.evidence,
            mandate_digest=self.mandate_digest,
            resolver_compatibility_digest=self.resolver_compatibility_digest,
            promotion_digest=self.promotion_digest,
        )
        return self.canonical_bytes() == rebuilt.canonical_bytes()


def canonical_bytes(value: object) -> bytes:
    """Strict, stable UTF-8 JSON for selection inputs and receipts."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, AutonomyModel):
        return _canonical_value(value.model_dump(mode="python"))
    # Domain models share the same strict Pydantic base but not AutonomyModel.
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        return _canonical_value(dumped)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON refuses non-finite Decimal values")
        if value == 0:
            return "0"
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("canonical JSON refuses naive datetimes")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise TypeError("canonical JSON refuses binary floating-point values")
    raise TypeError(f"canonical JSON does not support {type(value).__name__}")


def failure_evidence(
    *,
    observed_at: datetime,
    source: str,
    code: SelectionRejectionCode,
    detail: str,
) -> OptionSelectionEvidence:
    """Build explicit incomplete evidence for activation/acquisition refusal."""

    return OptionSelectionEvidence(
        source=source,
        observed_at=observed_at,
        complete=False,
        acquisition_rejections=(SelectionRejection(code=code, detail=detail),),
    )


def build_candidate_acquisition_plan(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    chain: OptionChainParameters,
    spot: Decimal,
    observed_at: datetime,
) -> CandidateAcquisitionPlan:
    """Plan the only broker qualification fan-out v1 may request.

    The full chain remains in evidence. This value records the deterministic
    bounded subset plus each excluded expiration/strike dimension, so replay
    can prove that the callback order or a hidden truncation did not choose the
    candidates.
    """

    with localcontext(_ARITHMETIC_CONTEXT):
        return _build_candidate_acquisition_plan(
            request=request,
            policy=policy,
            chain=chain,
            spot=spot,
            observed_at=observed_at,
        )


def _build_candidate_acquisition_plan(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    chain: OptionChainParameters,
    spot: Decimal,
    observed_at: datetime,
) -> CandidateAcquisitionPlan:
    if not spot.is_finite() or spot <= 0:
        raise ValueError("candidate acquisition requires a positive finite spot")
    as_of = observed_at.astimezone(ZoneInfo(policy.market_timezone)).date()
    all_expirations = tuple(sorted(set(chain.expirations)))
    eligible_expirations = tuple(
        expiration
        for expiration in all_expirations
        if policy.min_dte <= (expiration - as_of).days <= policy.max_dte
    )
    selected_expirations = tuple(
        sorted(
            sorted(
                eligible_expirations,
                key=lambda expiration: (
                    abs((expiration - as_of).days - policy.target_dte),
                    expiration,
                ),
            )[: policy.max_candidate_expirations]
        )
    )
    all_strikes = tuple(sorted(set(chain.strikes)))
    otm_strikes = tuple(
        strike
        for strike in all_strikes
        if (request.right is OptionRight.PUT and strike < spot)
        or (request.right is OptionRight.CALL and strike > spot)
    )
    selected_strikes = tuple(
        sorted(
            sorted(otm_strikes, key=lambda strike: (abs(strike - spot), strike))[
                : policy.max_strikes_per_expiration
            ]
        )
    )
    selected_expiration_set = frozenset(selected_expirations)
    selected_strike_set = frozenset(selected_strikes)
    requested_specs = tuple(
        OptionContractSpec(
            symbol=request.symbol,
            underlying_con_id=chain.underlying_con_id,
            expiration=expiration,
            strike=strike,
            right=request.right,
            exchange=chain.exchange,
            currency=request.currency,
            multiplier=chain.multiplier,
            trading_class=chain.trading_class,
        )
        for expiration in selected_expirations
        for strike in selected_strikes
    )
    return CandidateAcquisitionPlan(
        as_of=as_of,
        spot=spot,
        max_expirations=policy.max_candidate_expirations,
        max_strikes_per_expiration=policy.max_strikes_per_expiration,
        eligible_expirations=eligible_expirations,
        selected_expirations=selected_expirations,
        excluded_expirations=tuple(
            item for item in all_expirations if item not in selected_expiration_set
        ),
        selected_strikes=selected_strikes,
        excluded_strikes=tuple(item for item in all_strikes if item not in selected_strike_set),
        requested_specs=requested_specs,
    )


def select_option(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    mandate_digest: str,
    resolver_compatibility_digest: str,
    promotion_digest: str | None = None,
) -> OptionSelectionReceipt:
    """Evaluate every candidate and return a canonical SELECTED/NO_TRADE receipt."""

    try:
        with localcontext(_ARITHMETIC_CONTEXT):
            receipt = _select_option(
                request=request,
                policy=policy,
                evidence=evidence,
                mandate_digest=mandate_digest,
                resolver_compatibility_digest=resolver_compatibility_digest,
                promotion_digest=promotion_digest,
            )
    except DecimalException:
        fallback = failure_evidence(
            observed_at=evidence.observed_at,
            source="deterministic-selector-arithmetic-guard",
            code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
            detail="numeric evidence could not be evaluated inside the pinned decimal context",
        )
        with localcontext(_ARITHMETIC_CONTEXT):
            receipt = _select_option(
                request=request,
                policy=policy,
                evidence=fallback,
                mandate_digest=mandate_digest,
                resolver_compatibility_digest=resolver_compatibility_digest,
                promotion_digest=promotion_digest,
            )
    if len(receipt.canonical_bytes()) <= MAX_OPTION_SELECTION_RECEIPT_BYTES:
        return receipt
    size_fallback = failure_evidence(
        observed_at=evidence.observed_at,
        source="deterministic-selector-size-guard",
        code=SelectionRejectionCode.EVIDENCE_TRUNCATED,
        detail=(
            "canonical evidence exceeded the durable receipt bound; "
            f"original evidence digest {receipt.evidence_digest}, "
            f"chain rows {len(receipt.evidence.chain_parameters)}, "
            f"candidates {len(receipt.evidence.candidates)}"
        ),
    )
    with localcontext(_ARITHMETIC_CONTEXT):
        bounded = _select_option(
            request=request,
            policy=policy,
            evidence=size_fallback,
            mandate_digest=mandate_digest,
            resolver_compatibility_digest=resolver_compatibility_digest,
            promotion_digest=promotion_digest,
        )
    if len(bounded.canonical_bytes()) > MAX_OPTION_SELECTION_RECEIPT_BYTES:
        raise ValueError("request or policy exceeds the durable receipt bound")
    return bounded


def _select_option(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    mandate_digest: str,
    resolver_compatibility_digest: str,
    promotion_digest: str | None,
) -> OptionSelectionReceipt:

    request_digest = canonical_digest(request)
    policy_digest = canonical_digest(policy)
    normalized_evidence = _normalized_evidence(evidence)
    evidence_digest = canonical_digest(normalized_evidence)
    input_digest = canonical_digest(
        {
            "canonicalization_version": CANONICALIZATION_VERSION,
            "evidence_digest": evidence_digest,
            "mandate_digest": mandate_digest,
            "policy_digest": policy_digest,
            "promotion_digest": promotion_digest,
            "request_digest": request_digest,
            "resolver_compatibility_digest": resolver_compatibility_digest,
            "resolver_version": RESOLVER_VERSION,
        }
    )

    global_rejections, chain, spot = _global_rejections(request, policy, normalized_evidence)
    duplicate_ids = _duplicate_ids(normalized_evidence.candidates)
    evaluations: list[CandidateEvaluation] = []
    rankable: list[tuple[TieBreakFacts, CandidateEvidence, CandidateEvaluation]] = []
    for candidate in normalized_evidence.candidates:
        evaluation = _evaluate_candidate(
            request=request,
            policy=policy,
            evidence=normalized_evidence,
            candidate=candidate,
            chain=chain,
            spot=spot,
            global_rejections=global_rejections,
            duplicate_ids=duplicate_ids,
        )
        evaluations.append(evaluation)
        if evaluation.eligible and evaluation.tie_break is not None:
            rankable.append((evaluation.tie_break, candidate, evaluation))

    evaluations.sort(key=lambda item: (item.con_id or 0, item.evidence_digest))
    selected: SelectedOption | None = None
    status = SelectionStatus.NO_TRADE
    no_trade: tuple[SelectionRejection, ...]
    snapshot_integrity_failed = any(
        rejection.code in _SNAPSHOT_INTEGRITY_CODES
        for evaluation in evaluations
        for rejection in evaluation.rejections
    )
    if not global_rejections and not snapshot_integrity_failed and rankable:
        _, candidate, evaluation = min(rankable, key=lambda item: item[0].key())
        assert isinstance(candidate.contract, OptionContract)
        assert candidate.quote is not None
        assert candidate.deliverable is not None
        assert candidate.deliverable.share_quantity is not None
        assert evaluation.applicable_tick is not None
        assert evaluation.limit_price is not None
        selected_contract = candidate.contract.model_copy(
            update={
                "min_tick": evaluation.applicable_tick,
                "deliverable_verified": True,
                "deliverable_shares": candidate.deliverable.share_quantity,
            }
        )
        selected = SelectedOption(
            contract=selected_contract,
            quote=candidate.quote.model_copy(update={"contract": selected_contract}),
            applicable_tick=evaluation.applicable_tick,
            limit_price=evaluation.limit_price,
            sizing_reference_price=selected_contract.strike,
            deliverable_shares=candidate.deliverable.share_quantity,
            candidate_evidence_digest=evaluation.evidence_digest,
        )
        status = SelectionStatus.SELECTED
        no_trade = ()
    else:
        candidate_reasons = tuple(
            rejection for evaluation in evaluations for rejection in evaluation.rejections
        )
        no_trade = _sorted_rejections((*global_rejections, *candidate_reasons))
        if not no_trade:
            no_trade = (
                SelectionRejection(
                    code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
                    detail="no option candidate evidence was available for evaluation",
                ),
            )

    draft = OptionSelectionReceipt(
        broker_resolver_version=normalized_evidence.broker_resolver_version,
        resolver_compatibility_digest=resolver_compatibility_digest,
        mandate_digest=mandate_digest,
        promotion_digest=promotion_digest,
        evaluated_at=normalized_evidence.observed_at,
        request=request,
        policy=policy,
        evidence=normalized_evidence,
        request_digest=request_digest,
        policy_digest=policy_digest,
        evidence_digest=evidence_digest,
        canonical_input_digest=input_digest,
        global_rejections=global_rejections,
        candidates=tuple(evaluations),
        status=status,
        selected=selected,
        no_trade_reasons=no_trade,
        output_digest=_ZERO_DIGEST,
    )
    return draft.model_copy(
        update={"output_digest": hashlib.sha256(draft.digest_material_bytes()).hexdigest()}
    )


def _normalized_evidence(evidence: OptionSelectionEvidence) -> OptionSelectionEvidence:
    chains = tuple(
        sorted(
            (
                item.model_copy(
                    update={
                        "expirations": tuple(sorted(item.expirations)),
                        "strikes": tuple(sorted(item.strikes)),
                    }
                )
                for item in evidence.chain_parameters
            ),
            key=lambda item: (
                item.underlying_con_id,
                item.exchange,
                item.trading_class,
                item.multiplier,
                item.expirations,
                item.strikes,
            ),
        )
    )
    plan = evidence.acquisition_plan
    if plan is not None:
        plan = plan.model_copy(
            update={
                "eligible_expirations": tuple(sorted(plan.eligible_expirations)),
                "selected_expirations": tuple(sorted(plan.selected_expirations)),
                "excluded_expirations": tuple(sorted(plan.excluded_expirations)),
                "selected_strikes": tuple(sorted(plan.selected_strikes)),
                "excluded_strikes": tuple(sorted(plan.excluded_strikes)),
                "requested_specs": tuple(sorted(plan.requested_specs, key=_spec_key)),
            }
        )
    normalized_candidates: list[CandidateEvidence] = []
    for item in evidence.candidates:
        deliverable = item.deliverable
        if deliverable is not None and deliverable.other_assets is not None:
            deliverable = deliverable.model_copy(
                update={"other_assets": tuple(sorted(set(deliverable.other_assets)))}
            )
        normalized_candidates.append(item.model_copy(update={"deliverable": deliverable}))
    candidates = tuple(
        sorted(
            normalized_candidates,
            key=lambda item: (
                getattr(item.contract, "con_id", 0),
                canonical_digest(item),
            ),
        )
    )
    observed_market_rules = tuple(
        sorted(
            evidence.observed_market_rules,
            key=lambda item: (
                item.market_rule.con_id,
                item.observed_at,
                canonical_digest(item.market_rule),
            ),
        )
    )
    observed_deliverables = tuple(
        sorted(
            (
                item.model_copy(
                    update={
                        "deliverable": item.deliverable.model_copy(
                            update={
                                "other_assets": (
                                    tuple(sorted(set(item.deliverable.other_assets)))
                                    if item.deliverable.other_assets is not None
                                    else None
                                )
                            }
                        )
                    }
                )
                for item in evidence.observed_deliverables
            ),
            key=lambda item: (
                item.deliverable.con_id,
                item.observed_at,
                canonical_digest(item.deliverable),
            ),
        )
    )
    observed_option_quotes = tuple(
        sorted(
            evidence.observed_option_quotes,
            key=lambda item: (
                item.quote.contract.con_id,
                item.quote.timestamp,
                item.fetched_at,
                item.observed_at,
                canonical_digest(item.quote),
            ),
        )
    )
    return evidence.model_copy(
        update={
            "acquisition_rejections": _sorted_rejections(evidence.acquisition_rejections),
            "chain_parameters": chains,
            "acquisition_plan": plan,
            "observed_market_rules": observed_market_rules,
            "observed_deliverables": observed_deliverables,
            "observed_option_quotes": observed_option_quotes,
            "candidates": candidates,
        }
    )


def _global_rejections(
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
) -> tuple[tuple[SelectionRejection, ...], OptionChainParameters | None, Decimal | None]:
    reasons = list(evidence.acquisition_rejections)
    if request.strategy not in {
        StrategyForm.CASH_SECURED_PUT,
        StrategyForm.COVERED_CALL,
    }:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNSUPPORTED_REQUEST,
                "v1 selects only opening cash-secured puts and covered calls",
            )
        )
    if not evidence.complete:
        reasons.append(_reason(SelectionRejectionCode.EVIDENCE_PARTIAL, "evidence is incomplete"))
    elif not evidence.completion_marker.strip():
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_PARTIAL,
                "complete evidence lacks an explicit completion marker",
            )
        )
    if evidence.truncated:
        reasons.append(
            _reason(SelectionRejectionCode.EVIDENCE_TRUNCATED, "evidence was marked truncated")
        )
    _validate_raw_observation_sets(evidence, reasons)
    if any(
        len(chain.expirations) != len(set(chain.expirations))
        or len(chain.strikes) != len(set(chain.strikes))
        for chain in evidence.chain_parameters
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.CHAIN_DUPLICATE_VALUE,
                "chain evidence repeats an expiration or strike",
            )
        )
    underlying = evidence.underlying
    if underlying is None:
        reasons.append(_reason(SelectionRejectionCode.UNDERLYING_MISSING, "underlying is absent"))
    elif not isinstance(underlying, UnderlyingContract):
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_TYPE_MISMATCH,
                "qualified underlying is not an equity contract",
            )
        )
    else:
        if underlying.symbol != request.symbol:
            reasons.append(
                _reason(
                    SelectionRejectionCode.UNDERLYING_SYMBOL_MISMATCH,
                    "qualified underlying symbol differs from the request",
                )
            )
        if underlying.currency != request.currency:
            reasons.append(
                _reason(
                    SelectionRejectionCode.UNDERLYING_CURRENCY_MISMATCH,
                    "qualified underlying currency differs from the request",
                )
            )

    for label, timestamp, maximum_age in (
        (
            "underlying qualification",
            evidence.underlying_qualified_at,
            policy.max_chain_age_seconds,
        ),
        (
            "underlying quote fetch",
            evidence.underlying_quote_fetched_at,
            policy.max_quote_age_seconds,
        ),
        (
            "underlying quote observation",
            evidence.underlying_quote_observed_at,
            policy.max_quote_age_seconds,
        ),
    ):
        if timestamp is None:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_PARTIAL,
                    f"{label} lacks an explicit timestamp",
                )
            )
            continue
        age = Decimal(str((evidence.observed_at - timestamp).total_seconds()))
        if age < 0:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                    f"{label} timestamp is after evaluation time",
                )
            )
        elif age > maximum_age:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_PARTIAL,
                    f"{label} exceeds its freshness limit",
                )
            )
    for label, timestamp in (
        ("fetch", evidence.chain_fetched_at),
        ("observation", evidence.chain_observed_at),
        ("completion", evidence.chain_completion_observed_at),
    ):
        if timestamp is None:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_PARTIAL,
                    f"chain evidence lacks a {label} timestamp",
                )
            )
            continue
        chain_age = Decimal(str((evidence.observed_at - timestamp).total_seconds()))
        if chain_age < 0:
            reasons.append(
                _reason(
                    SelectionRejectionCode.CHAIN_FUTURE,
                    f"chain {label} timestamp is after evaluation time",
                )
            )
        elif chain_age > policy.max_chain_age_seconds:
            reasons.append(
                _reason(
                    SelectionRejectionCode.CHAIN_STALE,
                    "chain metadata exceeds the freshness limit",
                )
            )
    if (
        evidence.underlying_quote_fetched_at is not None
        and evidence.underlying_quote_observed_at is not None
        and evidence.underlying_quote_fetched_at > evidence.underlying_quote_observed_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "underlying quote fetch is after its observation timestamp",
            )
        )
    if (
        evidence.underlying_quote is not None
        and evidence.underlying_quote_observed_at is not None
        and evidence.underlying_quote.timestamp > evidence.underlying_quote_observed_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "underlying quote source timestamp is after its observation timestamp",
            )
        )
    if (
        evidence.chain_fetched_at is not None
        and evidence.chain_observed_at is not None
        and evidence.chain_fetched_at > evidence.chain_observed_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "chain fetch is after its observation timestamp",
            )
        )
    if not evidence.chain_completion_source:
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_PARTIAL,
                "chain evidence lacks a completion-signal source",
            )
        )
    if (
        evidence.chain_completion_observed_at is not None
        and evidence.chain_fetched_at is not None
        and evidence.chain_completion_observed_at > evidence.chain_fetched_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "chain completion signal is after the completed response fetch",
            )
        )

    spot = _validate_underlying_quote(request, policy, evidence, reasons)
    chain = _select_chain(request, policy, evidence, underlying, reasons)
    if evidence.complete and not evidence.acquisition_rejections:
        _validate_acquisition_plan(request, policy, evidence, chain, spot, reasons)
    return _sorted_rejections(reasons), chain, spot


def _validate_raw_observation_sets(
    evidence: OptionSelectionEvidence,
    reasons: list[SelectionRejection],
) -> None:
    """Reject duplicate, missing, or orphan raw observations when they are present."""

    candidate_ids = tuple(getattr(item.contract, "con_id", 0) for item in evidence.candidates)
    expected = set(candidate_ids)
    groups = (
        (
            "market-rule",
            tuple(item.market_rule.con_id for item in evidence.observed_market_rules),
        ),
        (
            "deliverable",
            tuple(item.deliverable.con_id for item in evidence.observed_deliverables),
        ),
        (
            "option-quote",
            tuple(item.quote.contract.con_id for item in evidence.observed_option_quotes),
        ),
    )
    mismatched_groups: set[str] = set()
    for label, observed_ids in groups:
        if not expected and not observed_ids:
            continue
        observed = set(observed_ids)
        duplicates = sorted(con_id for con_id, count in Counter(observed_ids).items() if count > 1)
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        if duplicates or missing or unexpected:
            mismatched_groups.add(label)
            reasons.append(
                _reason(
                    SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
                    f"{label} observation set mismatch; duplicate={duplicates}, "
                    f"missing={missing}, unexpected={unexpected}",
                )
            )

    if not expected:
        return

    candidate_by_id = {
        getattr(candidate.contract, "con_id", 0): candidate for candidate in evidence.candidates
    }
    if "market-rule" not in mismatched_groups:
        raw_rules = {item.market_rule.con_id: item for item in evidence.observed_market_rules}
        conflicts = sorted(
            con_id
            for con_id, candidate in candidate_by_id.items()
            if candidate.market_rule != raw_rules[con_id].market_rule
            or candidate.market_rule_observed_at != raw_rules[con_id].observed_at
        )
        if conflicts:
            reasons.append(
                _reason(
                    SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
                    "market-rule candidate projection differs from raw observations for "
                    f"conIds {conflicts}",
                )
            )
    if "deliverable" not in mismatched_groups:
        raw_deliverables = {
            item.deliverable.con_id: item for item in evidence.observed_deliverables
        }
        conflicts = sorted(
            con_id
            for con_id, candidate in candidate_by_id.items()
            if candidate.deliverable != raw_deliverables[con_id].deliverable
            or candidate.deliverable_observed_at != raw_deliverables[con_id].observed_at
        )
        if conflicts:
            reasons.append(
                _reason(
                    SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
                    "deliverable candidate projection differs from raw observations for "
                    f"conIds {conflicts}",
                )
            )
    if "option-quote" not in mismatched_groups:
        raw_quotes = {item.quote.contract.con_id: item for item in evidence.observed_option_quotes}
        conflicts = sorted(
            con_id
            for con_id, candidate in candidate_by_id.items()
            if candidate.quote != raw_quotes[con_id].quote
            or candidate.quote_fetched_at != raw_quotes[con_id].fetched_at
            or candidate.quote_observed_at != raw_quotes[con_id].observed_at
        )
        if conflicts:
            reasons.append(
                _reason(
                    SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
                    "option-quote candidate projection differs from raw observations for "
                    f"conIds {conflicts}",
                )
            )


def _validate_acquisition_plan(
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    chain: OptionChainParameters | None,
    spot: Decimal | None,
    reasons: list[SelectionRejection],
) -> None:
    plan = evidence.acquisition_plan
    if plan is None:
        reasons.append(
            _reason(
                SelectionRejectionCode.ACQUISITION_PLAN_MISSING,
                "complete evidence lacks the deterministic bounded acquisition plan",
            )
        )
        return
    if chain is None or spot is None:
        return
    expected = build_candidate_acquisition_plan(
        request=request,
        policy=policy,
        chain=chain,
        spot=spot,
        observed_at=evidence.observed_at,
    )
    if plan != expected:
        reasons.append(
            _reason(
                SelectionRejectionCode.ACQUISITION_PLAN_CONFLICT,
                "recorded candidate fan-out differs from deterministic request/policy/chain bounds",
            )
        )
        return
    if not plan.requested_specs:
        reasons.append(
            _reason(
                SelectionRejectionCode.CANDIDATE_SET_EMPTY,
                "no strictly OTM expiration/strike point falls inside the bounded request",
            )
        )
        return
    requested = Counter(_spec_key(item) for item in plan.requested_specs)
    returned_contracts: list[OptionContract] = []
    for item in evidence.candidates:
        if not isinstance(item.contract, OptionContract):
            reasons.append(
                _reason(
                    SelectionRejectionCode.CANDIDATE_SET_MISMATCH,
                    "candidate evidence contains a non-option instrument",
                )
            )
            return
        returned_contracts.append(item.contract)
    returned = Counter(_spec_key(contract) for contract in returned_contracts)
    if returned != requested:
        reasons.append(
            _reason(
                SelectionRejectionCode.CANDIDATE_SET_MISMATCH,
                "qualified candidate set is missing, duplicated, or differs from the "
                "requested fan-out",
            )
        )


def _validate_underlying_quote(
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    reasons: list[SelectionRejection],
) -> Decimal | None:
    quote = evidence.underlying_quote
    if quote is None:
        reasons.append(
            _reason(SelectionRejectionCode.UNDERLYING_QUOTE_MISSING, "underlying quote is absent")
        )
        return None
    underlying = evidence.underlying
    if (
        not isinstance(underlying, UnderlyingContract)
        or not isinstance(quote.contract, UnderlyingContract)
        or quote.contract != underlying
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_QUOTE_IDENTITY_MISMATCH,
                "underlying quote does not name the exact qualified contract",
            )
        )
    if quote.contract.symbol != request.symbol:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_SYMBOL_MISMATCH,
                "underlying quote identifies another symbol",
            )
        )
    age = Decimal(str((evidence.observed_at - quote.timestamp).total_seconds()))
    if age < 0:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_QUOTE_FUTURE,
                "underlying quote timestamp is after evaluation time",
            )
        )
    elif age > policy.max_quote_age_seconds:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_QUOTE_STALE,
                "underlying quote exceeds the freshness limit",
            )
        )
    if quote.data_quality not in policy.permitted_data_qualities:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_DATA_QUALITY,
                "underlying quote quality is not permitted",
            )
        )
    if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask < quote.bid:
        reasons.append(
            _reason(
                SelectionRejectionCode.UNDERLYING_BOOK_INVALID,
                "underlying quote is not a positive non-crossed two-sided book",
            )
        )
        return None
    return (quote.bid + quote.ask) / Decimal(2)


def _select_chain(
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    underlying: Instrument | None,
    reasons: list[SelectionRejection],
) -> OptionChainParameters | None:
    if not evidence.chain_parameters:
        reasons.append(_reason(SelectionRejectionCode.CHAIN_MISSING, "no chain metadata exists"))
        return None
    underlying_id = underlying.con_id if isinstance(underlying, UnderlyingContract) else None
    identity_rows = [
        row
        for row in evidence.chain_parameters
        if underlying_id is not None and row.underlying_con_id == underlying_id
    ]
    exchange_rows = [row for row in identity_rows if row.exchange in policy.permitted_exchanges]
    family_rows = [
        row for row in exchange_rows if row.trading_class in policy.permitted_trading_classes
    ]
    eligible = [row for row in family_rows if row.multiplier == policy.required_multiplier]
    if len(eligible) == 1:
        selected = eligible[0]
        if not selected.expirations or not selected.strikes:
            reasons.append(
                _reason(
                    SelectionRejectionCode.CHAIN_INCOMPLETE,
                    "selected chain row has no expirations or strikes",
                )
            )
            return None
        return selected
    if len(eligible) > 1:
        reasons.append(
            _reason(
                SelectionRejectionCode.CHAIN_AMBIGUOUS,
                "more than one chain row matches the permitted exact identity",
            )
        )
        return None
    if not identity_rows:
        code = SelectionRejectionCode.CHAIN_IDENTITY_MISMATCH
        detail = "no chain row references the qualified underlying"
    elif not exchange_rows:
        code = SelectionRejectionCode.CHAIN_EXCHANGE_NOT_PERMITTED
        detail = "no chain row uses a permitted exchange"
    elif not family_rows:
        code = SelectionRejectionCode.CHAIN_FAMILY_NOT_PERMITTED
        detail = "no chain row uses a permitted trading class"
    else:
        code = SelectionRejectionCode.CHAIN_MULTIPLIER_MISMATCH
        detail = "no chain row carries the required standard multiplier"
    reasons.append(_reason(code, detail))
    return None


def _evaluate_candidate(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    evidence: OptionSelectionEvidence,
    candidate: CandidateEvidence,
    chain: OptionChainParameters | None,
    spot: Decimal | None,
    global_rejections: tuple[SelectionRejection, ...],
    duplicate_ids: frozenset[int],
) -> CandidateEvaluation:
    candidate_digest = canonical_digest(candidate)
    reasons = list(global_rejections)
    contract = candidate.contract
    con_id = getattr(contract, "con_id", None)
    timestamp_values = (
        ("contract qualification", candidate.qualified_at, policy.max_chain_age_seconds),
        ("quote fetch", candidate.quote_fetched_at, policy.max_quote_age_seconds),
        ("quote observation", candidate.quote_observed_at, policy.max_quote_age_seconds),
        ("market rule", candidate.market_rule_observed_at, policy.max_chain_age_seconds),
        ("deliverable", candidate.deliverable_observed_at, policy.max_chain_age_seconds),
    )
    for label, timestamp, maximum_age in timestamp_values:
        if timestamp is None:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_PARTIAL,
                    f"candidate {label} lacks an explicit timestamp",
                )
            )
            continue
        age = Decimal(str((evidence.observed_at - timestamp).total_seconds()))
        if age < 0:
            reasons.append(
                _reason(
                    SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                    f"candidate {label} timestamp is after evaluation time",
                )
            )
        elif age > maximum_age:
            reasons.append(
                _reason(
                    SelectionRejectionCode.CONTRACT_DETAILS_STALE,
                    f"candidate {label} exceeds its freshness limit",
                )
            )
    if (
        candidate.quote_fetched_at is not None
        and candidate.quote_observed_at is not None
        and candidate.quote_fetched_at > candidate.quote_observed_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "candidate quote fetch is after its observation timestamp",
            )
        )
    if (
        candidate.quote is not None
        and candidate.quote_observed_at is not None
        and candidate.quote.timestamp > candidate.quote_observed_at
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
                "candidate quote source timestamp is after its observation timestamp",
            )
        )
    if con_id in duplicate_ids:
        reasons.append(
            _reason(
                SelectionRejectionCode.DUPLICATE_CONTRACT_ID,
                "more than one candidate carries this conId",
            )
        )
    if not isinstance(contract, OptionContract):
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_TYPE_MISMATCH,
                "candidate contract is not an equity option",
            )
        )
        return _rejected(candidate_digest, con_id, reasons)

    if contract.symbol != request.symbol:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_SYMBOL_MISMATCH,
                "candidate symbol differs from the request",
            )
        )
    underlying = evidence.underlying
    if (
        not isinstance(underlying, UnderlyingContract)
        or contract.underlying_con_id != underlying.con_id
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_UNDERLYING_MISMATCH,
                "candidate does not reference the qualified underlying",
            )
        )
    if contract.right is not request.right:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_RIGHT_MISMATCH,
                "candidate right differs from the strategy-derived right",
            )
        )
    if contract.currency != request.currency:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_CURRENCY_MISMATCH,
                "candidate currency differs from the request",
            )
        )
    if contract.exchange not in policy.permitted_exchanges:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_EXCHANGE_NOT_PERMITTED,
                "candidate exchange is outside owner scope",
            )
        )
    if contract.trading_class not in policy.permitted_trading_classes:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_FAMILY_NOT_PERMITTED,
                "candidate trading class is outside owner scope",
            )
        )
    if contract.multiplier != policy.required_multiplier:
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_MULTIPLIER_MISMATCH,
                "candidate multiplier differs from the required standard multiplier",
            )
        )
    if chain is not None and (
        contract.exchange != chain.exchange
        or contract.trading_class != chain.trading_class
        or contract.multiplier != chain.multiplier
        or contract.underlying_con_id != chain.underlying_con_id
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_CHAIN_IDENTITY_MISMATCH,
                "candidate route, family, multiplier, or underlying differs from the "
                "selected chain row",
            )
        )
    if chain is None or (
        contract.expiration not in chain.expirations or contract.strike not in chain.strikes
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.CONTRACT_NOT_IN_CHAIN,
                "candidate expiry/strike is absent from the selected chain row",
            )
        )

    local_date = evidence.observed_at.astimezone(ZoneInfo(policy.market_timezone)).date()
    dte = (contract.expiration - local_date).days
    if not policy.min_dte <= dte <= policy.max_dte:
        reasons.append(
            _reason(
                SelectionRejectionCode.EXPIRATION_OUTSIDE_RANGE,
                "candidate DTE is outside the requested range",
            )
        )
    if (
        spot is None
        or (request.right is OptionRight.PUT and contract.strike >= spot)
        or (request.right is OptionRight.CALL and contract.strike <= spot)
    ):
        reasons.append(
            _reason(
                SelectionRejectionCode.MONEYNESS_MISMATCH,
                "candidate is not strictly out-of-the-money",
            )
        )

    _validate_deliverable(contract, candidate.deliverable, reasons)
    _validate_session(contract, evidence.observed_at, policy, reasons)
    quote_values = _validate_quote(
        candidate,
        contract,
        evidence.observed_at,
        request,
        policy,
        reasons,
    )
    if quote_values is None:
        return _rejected(candidate_digest, con_id, reasons, dte=dte)
    midpoint, relative_spread, abs_delta = quote_values

    tick, limit_price = _validate_price_rule(candidate, policy, reasons)
    if reasons:
        return _rejected(
            candidate_digest,
            con_id,
            reasons,
            dte=dte,
            midpoint=midpoint,
            relative_spread=relative_spread,
            abs_delta=abs_delta,
            applicable_tick=tick,
            limit_price=limit_price,
        )

    assert candidate.quote is not None
    components, score, tie = _score(
        request=request,
        policy=policy,
        contract=contract,
        dte=dte,
        abs_delta=abs_delta,
        relative_spread=relative_spread,
        volume=candidate.quote.volume,
        open_interest=candidate.quote.open_interest,
    )
    return CandidateEvaluation(
        evidence_digest=candidate_digest,
        con_id=contract.con_id,
        eligible=True,
        dte=dte,
        midpoint=midpoint,
        relative_spread=relative_spread,
        abs_delta=abs_delta,
        applicable_tick=tick,
        limit_price=limit_price,
        score_components=components,
        total_score=score,
        tie_break=tie,
    )


def _validate_deliverable(
    contract: OptionContract,
    deliverable: OptionDeliverableFacts | None,
    reasons: list[SelectionRejection],
) -> None:
    if deliverable is None or deliverable.con_id != contract.con_id:
        reasons.append(
            _reason(
                SelectionRejectionCode.DELIVERABLE_MISSING,
                "exact deliverable evidence is absent or mismatched",
            )
        )
    elif not deliverable.authoritative:
        reasons.append(
            _reason(
                SelectionRejectionCode.DELIVERABLE_NOT_AUTHORITATIVE,
                "deliverable evidence is not an authoritative schedule reading",
            )
        )
    elif not deliverable.is_standard_for(contract):
        reasons.append(
            _reason(
                SelectionRejectionCode.DELIVERABLE_NONSTANDARD,
                "deliverable is not exact share-only standard contract economics",
            )
        )


def _validate_session(
    contract: OptionContract,
    evaluated_at: datetime,
    policy: OptionSelectionPolicy,
    reasons: list[SelectionRejection],
) -> None:
    if TradingSession.REGULAR not in policy.permitted_sessions:
        reasons.append(
            _reason(
                SelectionRejectionCode.SESSION_NOT_PERMITTED,
                "owner policy does not permit the regular session",
            )
        )
        return
    hours = parse_liquid_hours(contract.liquid_hours, time_zone_id=contract.time_zone_id)
    broker_open = hours.confirms_open(evaluated_at) if hours is not None else None
    session = session_for(
        ProductFamily.OPTION,
        now=evaluated_at,
        market_timezone=policy.market_timezone,
        broker_confirms_open=broker_open,
    )
    if broker_open is None:
        reasons.append(
            _reason(
                SelectionRejectionCode.SESSION_EVIDENCE_MISSING,
                "contract details do not prove the venue session open",
            )
        )
    elif not session.may_submit:
        reasons.append(
            _reason(
                SelectionRejectionCode.SESSION_CLOSED,
                "broker/calendar evidence does not permit submission now",
            )
        )


def _validate_quote(
    candidate: CandidateEvidence,
    contract: OptionContract,
    evaluated_at: datetime,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    reasons: list[SelectionRejection],
) -> tuple[Decimal, Decimal, Decimal] | None:
    quote = candidate.quote
    if quote is None:
        reasons.append(_reason(SelectionRejectionCode.QUOTE_MISSING, "option quote is absent"))
        return None
    if quote.contract != contract:
        reasons.append(
            _reason(
                SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH,
                "option quote names another contract",
            )
        )
    age = Decimal(str((evaluated_at - quote.timestamp).total_seconds()))
    if age < 0 or (
        candidate.quote_observed_at is not None and candidate.quote_observed_at > evaluated_at
    ):
        reasons.append(
            _reason(SelectionRejectionCode.QUOTE_FUTURE, "quote evidence is from the future")
        )
    elif age > policy.max_quote_age_seconds:
        reasons.append(
            _reason(SelectionRejectionCode.QUOTE_STALE, "quote exceeds the freshness limit")
        )
    if quote.data_quality not in policy.permitted_data_qualities:
        reasons.append(
            _reason(
                SelectionRejectionCode.QUOTE_DATA_QUALITY,
                "quote data quality is not explicitly permitted",
            )
        )
    if quote.bid is None or quote.ask is None:
        reasons.append(_reason(SelectionRejectionCode.QUOTE_ONE_SIDED, "quote is not two-sided"))
        return None
    if quote.bid <= 0:
        reasons.append(_reason(SelectionRejectionCode.QUOTE_ZERO_BID, "quote bid is not positive"))
    if quote.ask < quote.bid:
        reasons.append(_reason(SelectionRejectionCode.QUOTE_CROSSED, "quote is crossed"))
        return None
    midpoint = (quote.bid + quote.ask) / Decimal(2)
    if midpoint <= 0:
        return None
    relative_spread = (quote.ask - quote.bid) / midpoint
    if relative_spread > policy.max_relative_spread:
        reasons.append(
            _reason(
                SelectionRejectionCode.SPREAD_TOO_WIDE,
                "relative spread exceeds the owner policy",
            )
        )
    if quote.volume is None:
        reasons.append(_reason(SelectionRejectionCode.VOLUME_UNKNOWN, "option volume is unknown"))
    elif quote.volume is not None and quote.volume < policy.min_option_volume:
        reasons.append(
            _reason(SelectionRejectionCode.VOLUME_TOO_LOW, "option volume is below the floor")
        )
    if quote.open_interest is None:
        reasons.append(
            _reason(
                SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,
                "option open interest is unknown",
            )
        )
    elif quote.open_interest is not None and quote.open_interest < policy.min_open_interest:
        reasons.append(
            _reason(
                SelectionRejectionCode.OPEN_INTEREST_TOO_LOW,
                "option open interest is below the floor",
            )
        )
    delta = quote.greeks.delta if quote.greeks is not None else None
    if delta is None:
        reasons.append(_reason(SelectionRejectionCode.DELTA_MISSING, "option delta is absent"))
        abs_delta = Decimal(0)
    elif not delta.is_finite() or abs(delta) >= 1:
        reasons.append(_reason(SelectionRejectionCode.DELTA_INVALID, "option delta is invalid"))
        abs_delta = Decimal(0)
    else:
        abs_delta = abs(delta)
        correct_sign = delta < 0 if contract.right is OptionRight.PUT else delta > 0
        if not correct_sign:
            reasons.append(
                _reason(
                    SelectionRejectionCode.DELTA_SIGN_MISMATCH,
                    "option delta sign contradicts the contract right",
                )
            )
        if not policy.min_abs_delta <= abs_delta <= policy.max_abs_delta:
            reasons.append(
                _reason(
                    SelectionRejectionCode.DELTA_OUTSIDE_RANGE,
                    "absolute option delta is outside the requested range",
                )
            )
    return midpoint, relative_spread, abs_delta


def _validate_price_rule(
    candidate: CandidateEvidence,
    policy: OptionSelectionPolicy,
    reasons: list[SelectionRejection],
) -> tuple[Decimal | None, Decimal | None]:
    contract = candidate.contract
    quote = candidate.quote
    if not isinstance(contract, OptionContract) or quote is None:
        return None, None
    rule = candidate.market_rule
    if rule is None:
        reasons.append(
            _reason(SelectionRejectionCode.MARKET_RULE_MISSING, "market-rule evidence is absent")
        )
        return None, None
    if rule.con_id != contract.con_id or rule.exchange != contract.exchange:
        reasons.append(
            _reason(
                SelectionRejectionCode.MARKET_RULE_IDENTITY_MISMATCH,
                "market rule does not match the exact contract route",
            )
        )
        return None, None
    if quote.bid is None or quote.ask is None:
        return None, None
    if policy.order_form is OrderForm.MARKET:
        base = quote.bid * (Decimal(1) - _MARKET_PROTECTION_COLLAR)
    elif policy.order_form is OrderForm.MARKETABLE_LIMIT:
        base = quote.bid
    else:
        base = quote.ask
    if not base.is_finite() or base <= 0:
        reasons.append(
            _reason(SelectionRejectionCode.PRICE_NOT_DERIVABLE, "derived sell price is invalid")
        )
        return None, None
    tick = _tick_for_price(rule, base)
    if tick is None:
        reasons.append(
            _reason(
                SelectionRejectionCode.MARKET_RULE_MISSING,
                "market-rule evidence has no price band covering the derived price",
            )
        )
        return None, None
    try:
        with localcontext(Context(prec=64)):
            conformed = (base / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    except (InvalidOperation, ArithmeticError):
        reasons.append(_reason(SelectionRejectionCode.PRICE_NOT_DERIVABLE, "tick rounding failed"))
        return None, None
    if conformed <= 0:
        reasons.append(
            _reason(SelectionRejectionCode.PRICE_NOT_DERIVABLE, "tick-rounded price is invalid")
        )
        return None, None
    if _tick_for_price(rule, conformed) != tick:
        reasons.append(
            _reason(
                SelectionRejectionCode.TICK_BAND_CROSSING,
                "tick rounding crosses into a different price band",
            )
        )
        return tick, None
    return tick, conformed


def _tick_for_price(rule: OptionMarketRule, price: Decimal) -> Decimal | None:
    applicable = [item.increment for item in rule.price_increments if item.low_edge <= price]
    return applicable[-1] if applicable else None


def _score(
    *,
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
    contract: OptionContract,
    dte: int,
    abs_delta: Decimal,
    relative_spread: Decimal,
    volume: int | None,
    open_interest: int | None,
) -> tuple[tuple[ScoreComponent, ...], Decimal, TieBreakFacts]:
    delta_error = abs(abs_delta - policy.target_abs_delta)
    delta_span = max(policy.max_abs_delta - policy.min_abs_delta, Decimal("0.000001"))
    dte_error = abs(dte - policy.target_dte)
    dte_span = max(policy.max_dte - policy.min_dte, 1)
    liquidity_value = (
        Decimal(1)
        if volume is None or open_interest is None
        else Decimal(1) / Decimal(1 + volume + open_interest)
    )
    spread_denominator = max(policy.max_relative_spread, Decimal("0.000001"))
    normalized = (
        ("delta", delta_error / delta_span, policy.delta_weight),
        ("spread", relative_spread / spread_denominator, policy.spread_weight),
        ("dte", Decimal(dte_error) / Decimal(dte_span), policy.dte_weight),
        ("liquidity", liquidity_value, policy.liquidity_weight),
    )
    components = tuple(
        ScoreComponent(
            name=name,  # type: ignore[arg-type]
            normalized_value=value,
            weight=weight,
            contribution=(value * weight).quantize(_SCORE_QUANTUM),
        )
        for name, value, weight in normalized
    )
    score = sum((item.contribution for item in components), Decimal(0)).quantize(_SCORE_QUANTUM)
    tie = TieBreakFacts(
        total_score=score,
        delta_error=delta_error,
        relative_spread=relative_spread,
        dte_error=dte_error,
        negative_open_interest=0 if open_interest is None else -open_interest,
        negative_volume=0 if volume is None else -volume,
        expiration=contract.expiration,
        strike=contract.strike,
        con_id=contract.con_id,
    )
    return components, score, tie


def _rejected(
    evidence_digest: str,
    con_id: int | None,
    reasons: list[SelectionRejection],
    *,
    dte: int | None = None,
    midpoint: Decimal | None = None,
    relative_spread: Decimal | None = None,
    abs_delta: Decimal | None = None,
    applicable_tick: Decimal | None = None,
    limit_price: Decimal | None = None,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        evidence_digest=evidence_digest,
        con_id=con_id,
        eligible=False,
        rejections=_sorted_rejections(reasons),
        dte=dte,
        midpoint=midpoint,
        relative_spread=relative_spread,
        abs_delta=abs_delta,
        applicable_tick=applicable_tick,
        limit_price=limit_price,
    )


def _duplicate_ids(candidates: tuple[CandidateEvidence, ...]) -> frozenset[int]:
    counts = Counter(getattr(item.contract, "con_id", 0) for item in candidates)
    return frozenset(con_id for con_id, count in counts.items() if con_id > 0 and count > 1)


def _spec_key(spec: OptionContractSpec) -> tuple[object, ...]:
    return (
        spec.symbol,
        spec.underlying_con_id,
        spec.expiration,
        spec.strike,
        spec.right.value,
        spec.exchange,
        spec.currency,
        spec.multiplier,
        spec.trading_class,
    )


def _reason(code: SelectionRejectionCode, detail: str) -> SelectionRejection:
    return SelectionRejection(code=code, detail=detail)


def _sorted_rejections(
    values: tuple[SelectionRejection, ...] | list[SelectionRejection],
) -> tuple[SelectionRejection, ...]:
    unique = {(item.code.value, item.detail): item for item in values}
    return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "BROKER_RESOLVER_VERSION",
    "CANONICALIZATION_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "OPTION_SELECTION_STREAM",
    "POLICY_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "RESOLVER_VERSION",
    "SCORING_VERSION",
    "CandidateAcquisitionPlan",
    "CandidateEvaluation",
    "CandidateEvidence",
    "ObservedDeliverableEvidence",
    "ObservedMarketRuleEvidence",
    "ObservedOptionQuoteEvidence",
    "OptionSelectionEvidence",
    "OptionSelectionPolicy",
    "OptionSelectionReceipt",
    "OptionSelectionRequest",
    "ScoreComponent",
    "SelectedOption",
    "SelectionRejection",
    "SelectionRejectionCode",
    "SelectionStatus",
    "TieBreakFacts",
    "build_candidate_acquisition_plan",
    "canonical_bytes",
    "canonical_digest",
    "failure_evidence",
    "select_option",
]

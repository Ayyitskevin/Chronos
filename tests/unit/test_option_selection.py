"""Deterministic option-selection unit, property, and mutation tests."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from chronos.autonomy import OrderForm, StrategyForm, TradingSession
from chronos.config.limits import (
    MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW,
    MAX_OPTION_CHAIN_ROWS,
    MAX_OPTION_CHAIN_STRIKES_PER_ROW,
    MAX_OPTION_MARKET_RULE_INCREMENTS,
    MAX_OPTION_SELECTION_OTHER_ASSETS,
    MAX_OPTION_SELECTION_POLICY_CODES,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
    MAX_OPTION_SELECTION_TEXT_CHARS,
)
from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import (
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionDeliverableFacts,
    OptionMarketRule,
    OptionPriceIncrement,
    UnderlyingContract,
)
from chronos.supervisor.option_selection import (
    CandidateEvidence,
    ObservedDeliverableEvidence,
    ObservedMarketRuleEvidence,
    ObservedOptionQuoteEvidence,
    OptionSelectionEvidence,
    OptionSelectionPolicy,
    OptionSelectionReceipt,
    OptionSelectionRequest,
    SelectionRejection,
    SelectionRejectionCode,
    SelectionStatus,
    build_candidate_acquisition_plan,
    canonical_bytes,
    canonical_digest,
    select_option,
)

_NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York.
_EXPIRATION = date(2026, 9, 4)
_MANDATE_DIGEST = "1" * 64
_COMPATIBILITY_DIGEST = "2" * 64
_PROMOTION_DIGEST = "3" * 64
_EXPECTED_CANDIDATE_DIGEST = "662cddb1080fef59c4fd741a3c623e7f891f6cfbaf85e507c9d36ebd34e842b9"
_EXPECTED_OUTPUT_DIGEST = "e0dafd5b487886948a09e50b35870c2cb61e8e40e74b3d47e6a0b21e24d3bcd1"
_EXPECTED_CANONICAL_RECEIPT_HASH = (
    "ef7ae1e5ac3e6254741d8633ecd3ecf03f40a31e00062d708f2821cc8c7f53e3"
)


@dataclass(frozen=True)
class _Inputs:
    request: OptionSelectionRequest
    policy: OptionSelectionPolicy
    evidence: OptionSelectionEvidence


def _valid_inputs(
    strategy: StrategyForm = StrategyForm.CASH_SECURED_PUT,
) -> _Inputs:
    """One complete synthetic snapshot; no helper reads time, disk, or a broker."""

    is_put = strategy is StrategyForm.CASH_SECURED_PUT
    right = OptionRight.PUT if is_put else OptionRight.CALL
    strike = Decimal("190") if is_put else Decimal("210")
    delta = Decimal("-0.30") if is_put else Decimal("0.30")
    side = "P" if is_put else "C"

    underlying = UnderlyingContract(
        con_id=1001,
        symbol="AAPL",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )
    contract = OptionContract(
        con_id=2001,
        symbol="AAPL",
        underlying_con_id=underlying.con_id,
        expiration=_EXPIRATION,
        strike=strike,
        right=right,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol=f"AAPL  260904{side}{int(strike * 1000):08d}",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )
    underlying_quote = MarketQuote(
        contract=underlying,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("199.90"),
        ask=Decimal("200.10"),
        last=Decimal("200"),
    )
    quote = MarketQuote(
        contract=contract,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("2.03"),
        ask=Decimal("2.07"),
        last=Decimal("2.05"),
        volume=1000,
        open_interest=2000,
        greeks=ModelGreeks(delta=delta),
    )
    chain = OptionChainParameters(
        exchange="SMART",
        underlying_con_id=underlying.con_id,
        trading_class="AAPL",
        multiplier=Decimal("100"),
        expirations=(_EXPIRATION,),
        strikes=(strike,),
    )
    market_rule = OptionMarketRule(
        con_id=contract.con_id,
        exchange="SMART",
        market_rule_id=26,
        price_increments=(
            OptionPriceIncrement(low_edge=Decimal("0"), increment=Decimal("0.01")),
            OptionPriceIncrement(low_edge=Decimal("3"), increment=Decimal("0.05")),
        ),
        source="synthetic-test",
    )
    deliverable = OptionDeliverableFacts(
        con_id=contract.con_id,
        authoritative=True,
        source="synthetic-test",
        underlying_con_id=underlying.con_id,
        share_quantity=Decimal("100"),
        cash_amount=Decimal("0"),
        other_assets=(),
    )
    candidate = CandidateEvidence(
        contract=contract,
        qualified_at=_NOW,
        quote=quote,
        quote_fetched_at=_NOW,
        quote_observed_at=_NOW,
        market_rule=market_rule,
        market_rule_observed_at=_NOW,
        deliverable=deliverable,
        deliverable_observed_at=_NOW,
    )
    request = OptionSelectionRequest(
        decision_id="decision-1",
        symbol="AAPL",
        strategy=strategy,
        right=right,
        requested_contracts=Decimal("1"),
    )
    policy = OptionSelectionPolicy(
        max_quote_age_seconds=Decimal("30"),
        max_chain_age_seconds=Decimal("300"),
        max_candidate_expirations=4,
        max_strikes_per_expiration=12,
        min_dte=25,
        target_dte=32,
        max_dte=45,
        min_abs_delta=Decimal("0.20"),
        target_abs_delta=Decimal("0.30"),
        max_abs_delta=Decimal("0.40"),
        permitted_data_qualities=(DataQuality.LIVE,),
        min_option_volume=100,
        min_open_interest=500,
        max_relative_spread=Decimal("0.10"),
        permitted_exchanges=("SMART",),
        permitted_trading_classes=("AAPL",),
        permitted_sessions=(TradingSession.REGULAR,),
        required_multiplier=Decimal("100"),
        order_form=OrderForm.MARKETABLE_LIMIT,
        delta_weight=Decimal("4"),
        spread_weight=Decimal("2"),
        dte_weight=Decimal("1"),
        liquidity_weight=Decimal("1"),
    )
    evidence = OptionSelectionEvidence(
        source="synthetic-test",
        observed_at=_NOW,
        complete=True,
        completion_marker="END",
        underlying=underlying,
        underlying_qualified_at=_NOW,
        underlying_quote=underlying_quote,
        underlying_quote_fetched_at=_NOW,
        underlying_quote_observed_at=_NOW,
        chain_parameters=(chain,),
        chain_fetched_at=_NOW,
        chain_observed_at=_NOW,
        chain_completion_observed_at=_NOW,
        chain_completion_source="synthetic-test-end-callback",
        acquisition_plan=build_candidate_acquisition_plan(
            request=request,
            policy=policy,
            chain=chain,
            spot=Decimal("200"),
            observed_at=_NOW,
        ),
        observed_market_rules=(
            ObservedMarketRuleEvidence(market_rule=market_rule, observed_at=_NOW),
        ),
        observed_deliverables=(
            ObservedDeliverableEvidence(deliverable=deliverable, observed_at=_NOW),
        ),
        observed_option_quotes=(
            ObservedOptionQuoteEvidence(
                quote=quote,
                fetched_at=_NOW,
                observed_at=_NOW,
            ),
        ),
        candidates=(candidate,),
    )
    return _Inputs(request=request, policy=policy, evidence=evidence)


def _select(inputs: _Inputs) -> OptionSelectionReceipt:
    return select_option(
        request=inputs.request,
        policy=inputs.policy,
        evidence=inputs.evidence,
        mandate_digest=_MANDATE_DIGEST,
        resolver_compatibility_digest=_COMPATIBILITY_DIGEST,
        promotion_digest=_PROMOTION_DIGEST,
    )


def _with_evidence(inputs: _Inputs, **updates: object) -> _Inputs:
    return replace(inputs, evidence=inputs.evidence.model_copy(update=updates))


def _with_candidate(inputs: _Inputs, **updates: object) -> _Inputs:
    candidate = inputs.evidence.candidates[0].model_copy(update=updates)
    return _with_evidence(inputs, candidates=(candidate,))


def _with_contract(inputs: _Inputs, **updates: object) -> _Inputs:
    contract = inputs.evidence.candidates[0].contract
    assert isinstance(contract, OptionContract)
    return _with_candidate(inputs, contract=contract.model_copy(update=updates))


def _with_quote(inputs: _Inputs, **updates: object) -> _Inputs:
    quote = inputs.evidence.candidates[0].quote
    assert quote is not None
    return _with_candidate(inputs, quote=quote.model_copy(update=updates))


def _with_underlying(inputs: _Inputs, **updates: object) -> _Inputs:
    underlying = inputs.evidence.underlying
    assert isinstance(underlying, UnderlyingContract)
    return _with_evidence(inputs, underlying=underlying.model_copy(update=updates))


def _with_underlying_quote(inputs: _Inputs, **updates: object) -> _Inputs:
    quote = inputs.evidence.underlying_quote
    assert quote is not None
    return _with_evidence(inputs, underlying_quote=quote.model_copy(update=updates))


def _with_chain(inputs: _Inputs, **updates: object) -> _Inputs:
    chain = inputs.evidence.chain_parameters[0].model_copy(update=updates)
    return _with_evidence(inputs, chain_parameters=(chain,))


def _with_rule(inputs: _Inputs, **updates: object) -> _Inputs:
    rule = inputs.evidence.candidates[0].market_rule
    assert rule is not None
    return _with_candidate(inputs, market_rule=rule.model_copy(update=updates))


def _with_deliverable(inputs: _Inputs, **updates: object) -> _Inputs:
    deliverable = inputs.evidence.candidates[0].deliverable
    assert deliverable is not None
    return _with_candidate(inputs, deliverable=deliverable.model_copy(update=updates))


def _with_policy(inputs: _Inputs, **updates: object) -> _Inputs:
    return replace(inputs, policy=inputs.policy.model_copy(update=updates))


def _candidate_with_con_id(candidate: CandidateEvidence, con_id: int) -> CandidateEvidence:
    contract = candidate.contract
    assert isinstance(contract, OptionContract)
    quote = candidate.quote
    rule = candidate.market_rule
    deliverable = candidate.deliverable
    assert quote is not None and rule is not None and deliverable is not None
    updated_contract = contract.model_copy(update={"con_id": con_id})
    return candidate.model_copy(
        update={
            "contract": updated_contract,
            "quote": quote.model_copy(update={"contract": updated_contract}),
            "market_rule": rule.model_copy(update={"con_id": con_id}),
            "deliverable": deliverable.model_copy(update={"con_id": con_id}),
        }
    )


def _mutation(case: str) -> _Inputs:
    inputs = _valid_inputs()
    candidate = inputs.evidence.candidates[0]
    quote = candidate.quote
    assert quote is not None

    if case == "evidence_unavailable":
        reason = SelectionRejection(
            code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
            detail="bounded evidence acquisition was unavailable",
        )
        return _with_evidence(inputs, complete=False, acquisition_rejections=(reason,))
    if case == "evidence_partial":
        return _with_evidence(inputs, complete=False)
    if case == "evidence_truncated":
        return _with_evidence(inputs, truncated=True)
    if case == "observation_set_mismatch":
        rule = candidate.market_rule
        assert rule is not None
        orphan = rule.model_copy(update={"con_id": 9999})
        return _with_evidence(
            inputs,
            observed_market_rules=(
                ObservedMarketRuleEvidence(market_rule=orphan, observed_at=_NOW),
            ),
        )
    if case == "pacing_exhausted":
        reason = SelectionRejection(
            code=SelectionRejectionCode.PACING_EXHAUSTED,
            detail="bounded acquisition pacing budget exhausted",
        )
        return _with_evidence(inputs, acquisition_rejections=(reason,))
    if case == "acquisition_plan_missing":
        return _with_evidence(inputs, acquisition_plan=None)
    if case == "acquisition_plan_conflict":
        plan = inputs.evidence.acquisition_plan
        assert plan is not None
        return _with_evidence(inputs, acquisition_plan=plan.model_copy(update={"spot": 201}))
    if case == "candidate_set_empty":
        chain = inputs.evidence.chain_parameters[0].model_copy(
            update={"strikes": (Decimal("210"),)}
        )
        plan = build_candidate_acquisition_plan(
            request=inputs.request,
            policy=inputs.policy,
            chain=chain,
            spot=Decimal("200"),
            observed_at=_NOW,
        )
        return _with_evidence(
            inputs,
            chain_parameters=(chain,),
            acquisition_plan=plan,
            candidates=(),
        )
    if case == "candidate_set_mismatch":
        return _with_evidence(inputs, candidates=())
    if case == "unsupported_request":
        request = inputs.request.model_copy(update={"strategy": StrategyForm.LONG_PUT})
        return replace(inputs, request=request)
    if case == "underlying_missing":
        return _with_evidence(inputs, underlying=None)
    if case == "underlying_type":
        return _with_evidence(inputs, underlying=candidate.contract)
    if case == "underlying_symbol":
        return _with_underlying(inputs, symbol="MSFT")
    if case == "underlying_currency":
        return _with_underlying(inputs, currency="CAD")
    if case == "underlying_quote_missing":
        return _with_evidence(inputs, underlying_quote=None)
    if case == "underlying_quote_identity":
        underlying = inputs.evidence.underlying
        assert isinstance(underlying, UnderlyingContract)
        return _with_underlying_quote(
            inputs,
            contract=underlying.model_copy(update={"con_id": 9999}),
        )
    if case == "underlying_quote_future":
        return _with_underlying_quote(inputs, timestamp=_NOW + timedelta(seconds=1))
    if case == "underlying_quote_stale":
        return _with_underlying_quote(inputs, timestamp=_NOW - timedelta(seconds=31))
    if case == "underlying_quality":
        return _with_underlying_quote(inputs, data_quality=DataQuality.DELAYED)
    if case == "underlying_book":
        return _with_underlying_quote(inputs, bid=None)
    if case == "chain_missing":
        return _with_evidence(inputs, chain_parameters=())
    if case == "chain_ambiguous":
        chain = inputs.evidence.chain_parameters[0]
        return _with_evidence(inputs, chain_parameters=(chain, chain))
    if case == "chain_incomplete":
        return _with_chain(inputs, strikes=())
    if case == "chain_future":
        return _with_evidence(inputs, chain_fetched_at=_NOW + timedelta(seconds=1))
    if case == "chain_stale":
        return _with_evidence(inputs, chain_fetched_at=_NOW - timedelta(seconds=301))
    if case == "chain_identity":
        return _with_chain(inputs, underlying_con_id=9999)
    if case == "chain_exchange":
        return _with_chain(inputs, exchange="CBOE")
    if case == "chain_family":
        return _with_chain(inputs, trading_class="MSFT")
    if case == "chain_multiplier":
        return _with_chain(inputs, multiplier=Decimal("50"))
    if case == "chain_duplicate_value":
        chain = inputs.evidence.chain_parameters[0]
        return _with_chain(inputs, strikes=(*chain.strikes, chain.strikes[0]))
    if case == "duplicate_contract":
        return _with_evidence(inputs, candidates=(candidate, candidate))
    if case == "contract_type":
        underlying = inputs.evidence.underlying
        assert isinstance(underlying, UnderlyingContract)
        return _with_candidate(inputs, contract=underlying)
    if case == "contract_symbol":
        return _with_contract(inputs, symbol="MSFT")
    if case == "contract_underlying":
        return _with_contract(inputs, underlying_con_id=9999)
    if case == "contract_right":
        return _with_contract(inputs, right=OptionRight.CALL)
    if case == "contract_currency":
        return _with_contract(inputs, currency="CAD")
    if case == "contract_exchange":
        return _with_contract(inputs, exchange="CBOE")
    if case == "contract_family":
        return _with_contract(inputs, trading_class="MSFT")
    if case == "contract_multiplier":
        return _with_contract(inputs, multiplier=Decimal("50"))
    if case == "contract_chain_identity":
        policy = inputs.policy.model_copy(update={"permitted_exchanges": ("SMART", "CBOE")})
        return replace(_with_contract(inputs, exchange="CBOE"), policy=policy)
    if case == "contract_not_in_chain":
        return _with_chain(inputs, strikes=(Decimal("185"),))
    if case == "contract_stale":
        return _with_candidate(inputs, qualified_at=_NOW - timedelta(seconds=301))
    if case == "expiration":
        return _with_policy(inputs, min_dte=0, target_dte=1, max_dte=1)
    if case == "moneyness":
        return _with_underlying_quote(inputs, bid=Decimal("184.90"), ask=Decimal("185.10"))
    if case == "delta_missing":
        return _with_quote(inputs, greeks=None)
    if case == "delta_invalid":
        return _with_quote(inputs, greeks=ModelGreeks(delta=Decimal("-1")))
    if case == "delta_sign":
        return _with_quote(inputs, greeks=ModelGreeks(delta=Decimal("0.30")))
    if case == "delta_range":
        return _with_quote(inputs, greeks=ModelGreeks(delta=Decimal("-0.50")))
    if case == "deliverable_missing":
        return _with_candidate(inputs, deliverable=None)
    if case == "deliverable_untrusted":
        return _with_deliverable(inputs, authoritative=False)
    if case == "deliverable_nonstandard":
        return _with_deliverable(inputs, share_quantity=Decimal("99"))
    if case == "session_missing":
        return _with_contract(inputs, liquid_hours="")
    if case == "session_closed":
        return _with_contract(inputs, liquid_hours="20260803:CLOSED")
    if case == "session_not_permitted":
        return _with_policy(inputs, permitted_sessions=())
    if case == "quote_missing":
        return _with_candidate(inputs, quote=None)
    if case == "quote_identity":
        other_contract = candidate.contract.model_copy(update={"con_id": 9999})
        return _with_quote(inputs, contract=other_contract)
    if case == "quote_future":
        return _with_quote(inputs, timestamp=_NOW + timedelta(seconds=1))
    if case == "quote_stale":
        return _with_quote(inputs, timestamp=_NOW - timedelta(seconds=31))
    if case == "quote_quality":
        return _with_quote(inputs, data_quality=DataQuality.DELAYED)
    if case == "quote_one_sided":
        return _with_quote(inputs, bid=None)
    if case == "quote_zero_bid":
        return _with_quote(inputs, bid=Decimal("0"))
    if case == "quote_crossed":
        return _with_quote(inputs, bid=Decimal("2.10"), ask=Decimal("2.00"))
    if case == "spread":
        return _with_quote(inputs, bid=Decimal("1"), ask=Decimal("3"))
    if case == "volume_unknown":
        return _with_quote(inputs, volume=None)
    if case == "volume_low":
        return _with_quote(inputs, volume=99)
    if case == "open_interest_unknown":
        return _with_quote(inputs, open_interest=None)
    if case == "open_interest_low":
        return _with_quote(inputs, open_interest=499)
    if case == "market_rule_missing":
        return _with_candidate(inputs, market_rule=None)
    if case == "market_rule_identity":
        return _with_rule(inputs, con_id=9999)
    if case == "tick_band_crossing":
        increments = (
            OptionPriceIncrement(low_edge=Decimal("0"), increment=Decimal("0.03")),
            OptionPriceIncrement(low_edge=Decimal("2.04"), increment=Decimal("0.05")),
        )
        return _with_rule(inputs, price_increments=increments)
    if case == "timestamp_conflict":
        return _with_candidate(inputs, qualified_at=_NOW + timedelta(seconds=1))
    raise AssertionError(f"unknown mutation case: {case}")


_GATE_CASES: tuple[
    tuple[str, tuple[SelectionRejectionCode, ...]],
    ...,
] = (
    ("evidence_unavailable", (SelectionRejectionCode.EVIDENCE_UNAVAILABLE,)),
    ("evidence_partial", (SelectionRejectionCode.EVIDENCE_PARTIAL,)),
    ("evidence_truncated", (SelectionRejectionCode.EVIDENCE_TRUNCATED,)),
    ("observation_set_mismatch", (SelectionRejectionCode.OBSERVATION_SET_MISMATCH,)),
    ("pacing_exhausted", (SelectionRejectionCode.PACING_EXHAUSTED,)),
    ("acquisition_plan_missing", (SelectionRejectionCode.ACQUISITION_PLAN_MISSING,)),
    ("acquisition_plan_conflict", (SelectionRejectionCode.ACQUISITION_PLAN_CONFLICT,)),
    ("candidate_set_empty", (SelectionRejectionCode.CANDIDATE_SET_EMPTY,)),
    ("candidate_set_mismatch", (SelectionRejectionCode.CANDIDATE_SET_MISMATCH,)),
    ("unsupported_request", (SelectionRejectionCode.UNSUPPORTED_REQUEST,)),
    ("underlying_missing", (SelectionRejectionCode.UNDERLYING_MISSING,)),
    ("underlying_type", (SelectionRejectionCode.UNDERLYING_TYPE_MISMATCH,)),
    ("underlying_symbol", (SelectionRejectionCode.UNDERLYING_SYMBOL_MISMATCH,)),
    ("underlying_currency", (SelectionRejectionCode.UNDERLYING_CURRENCY_MISMATCH,)),
    ("underlying_quote_missing", (SelectionRejectionCode.UNDERLYING_QUOTE_MISSING,)),
    (
        "underlying_quote_identity",
        (SelectionRejectionCode.UNDERLYING_QUOTE_IDENTITY_MISMATCH,),
    ),
    ("underlying_quote_future", (SelectionRejectionCode.UNDERLYING_QUOTE_FUTURE,)),
    ("underlying_quote_stale", (SelectionRejectionCode.UNDERLYING_QUOTE_STALE,)),
    ("underlying_quality", (SelectionRejectionCode.UNDERLYING_DATA_QUALITY,)),
    ("underlying_book", (SelectionRejectionCode.UNDERLYING_BOOK_INVALID,)),
    ("chain_missing", (SelectionRejectionCode.CHAIN_MISSING,)),
    ("chain_ambiguous", (SelectionRejectionCode.CHAIN_AMBIGUOUS,)),
    ("chain_incomplete", (SelectionRejectionCode.CHAIN_INCOMPLETE,)),
    ("chain_future", (SelectionRejectionCode.CHAIN_FUTURE,)),
    ("chain_stale", (SelectionRejectionCode.CHAIN_STALE,)),
    ("chain_identity", (SelectionRejectionCode.CHAIN_IDENTITY_MISMATCH,)),
    ("chain_exchange", (SelectionRejectionCode.CHAIN_EXCHANGE_NOT_PERMITTED,)),
    ("chain_family", (SelectionRejectionCode.CHAIN_FAMILY_NOT_PERMITTED,)),
    ("chain_multiplier", (SelectionRejectionCode.CHAIN_MULTIPLIER_MISMATCH,)),
    ("chain_duplicate_value", (SelectionRejectionCode.CHAIN_DUPLICATE_VALUE,)),
    ("duplicate_contract", (SelectionRejectionCode.DUPLICATE_CONTRACT_ID,)),
    ("contract_type", (SelectionRejectionCode.CONTRACT_TYPE_MISMATCH,)),
    ("contract_symbol", (SelectionRejectionCode.CONTRACT_SYMBOL_MISMATCH,)),
    ("contract_underlying", (SelectionRejectionCode.CONTRACT_UNDERLYING_MISMATCH,)),
    ("contract_right", (SelectionRejectionCode.CONTRACT_RIGHT_MISMATCH,)),
    ("contract_currency", (SelectionRejectionCode.CONTRACT_CURRENCY_MISMATCH,)),
    ("contract_exchange", (SelectionRejectionCode.CONTRACT_EXCHANGE_NOT_PERMITTED,)),
    ("contract_family", (SelectionRejectionCode.CONTRACT_FAMILY_NOT_PERMITTED,)),
    ("contract_multiplier", (SelectionRejectionCode.CONTRACT_MULTIPLIER_MISMATCH,)),
    (
        "contract_chain_identity",
        (SelectionRejectionCode.CONTRACT_CHAIN_IDENTITY_MISMATCH,),
    ),
    ("contract_not_in_chain", (SelectionRejectionCode.CONTRACT_NOT_IN_CHAIN,)),
    ("contract_stale", (SelectionRejectionCode.CONTRACT_DETAILS_STALE,)),
    ("expiration", (SelectionRejectionCode.EXPIRATION_OUTSIDE_RANGE,)),
    ("moneyness", (SelectionRejectionCode.MONEYNESS_MISMATCH,)),
    ("delta_missing", (SelectionRejectionCode.DELTA_MISSING,)),
    ("delta_invalid", (SelectionRejectionCode.DELTA_INVALID,)),
    ("delta_sign", (SelectionRejectionCode.DELTA_SIGN_MISMATCH,)),
    ("delta_range", (SelectionRejectionCode.DELTA_OUTSIDE_RANGE,)),
    ("deliverable_missing", (SelectionRejectionCode.DELIVERABLE_MISSING,)),
    ("deliverable_untrusted", (SelectionRejectionCode.DELIVERABLE_NOT_AUTHORITATIVE,)),
    ("deliverable_nonstandard", (SelectionRejectionCode.DELIVERABLE_NONSTANDARD,)),
    ("session_missing", (SelectionRejectionCode.SESSION_EVIDENCE_MISSING,)),
    ("session_closed", (SelectionRejectionCode.SESSION_CLOSED,)),
    ("session_not_permitted", (SelectionRejectionCode.SESSION_NOT_PERMITTED,)),
    ("quote_missing", (SelectionRejectionCode.QUOTE_MISSING,)),
    ("quote_identity", (SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH,)),
    ("quote_future", (SelectionRejectionCode.QUOTE_FUTURE,)),
    ("quote_stale", (SelectionRejectionCode.QUOTE_STALE,)),
    ("quote_quality", (SelectionRejectionCode.QUOTE_DATA_QUALITY,)),
    ("quote_one_sided", (SelectionRejectionCode.QUOTE_ONE_SIDED,)),
    (
        "quote_zero_bid",
        (
            SelectionRejectionCode.QUOTE_ZERO_BID,
            SelectionRejectionCode.PRICE_NOT_DERIVABLE,
        ),
    ),
    ("quote_crossed", (SelectionRejectionCode.QUOTE_CROSSED,)),
    ("spread", (SelectionRejectionCode.SPREAD_TOO_WIDE,)),
    ("volume_unknown", (SelectionRejectionCode.VOLUME_UNKNOWN,)),
    ("volume_low", (SelectionRejectionCode.VOLUME_TOO_LOW,)),
    ("open_interest_unknown", (SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,)),
    ("open_interest_low", (SelectionRejectionCode.OPEN_INTEREST_TOO_LOW,)),
    ("market_rule_missing", (SelectionRejectionCode.MARKET_RULE_MISSING,)),
    ("market_rule_identity", (SelectionRejectionCode.MARKET_RULE_IDENTITY_MISMATCH,)),
    ("tick_band_crossing", (SelectionRejectionCode.TICK_BAND_CROSSING,)),
    ("timestamp_conflict", (SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,)),
)

# These codes are emitted before valid resolver inputs exist. Keeping the
# explanations beside the coverage proof makes omissions explicit and reviewable.
_NON_RESOLVER_CODES: dict[SelectionRejectionCode, str] = {
    SelectionRejectionCode.FEATURE_DISABLED: "runtime feature activation gate",
    SelectionRejectionCode.PROMOTION_REQUIRED: "runtime promotion-artifact gate",
    SelectionRejectionCode.PROMOTION_MISMATCH: "runtime promotion-artifact gate",
    SelectionRejectionCode.DATA_PERMISSION_DENIED: "broker evidence acquisition failure",
    SelectionRejectionCode.DATA_CLEANUP_FAILED: "broker evidence acquisition failure",
}


@pytest.mark.parametrize(
    ("strategy", "expected_right", "expected_strike"),
    (
        (StrategyForm.CASH_SECURED_PUT, OptionRight.PUT, Decimal("190")),
        (StrategyForm.COVERED_CALL, OptionRight.CALL, Decimal("210")),
    ),
)
def test_valid_csp_and_covered_call_select_exact_contract(
    strategy: StrategyForm,
    expected_right: OptionRight,
    expected_strike: Decimal,
) -> None:
    receipt = _select(_valid_inputs(strategy))

    assert receipt.status is SelectionStatus.SELECTED
    assert receipt.selected is not None
    assert receipt.selected.contract.right is expected_right
    assert receipt.selected.contract.strike == expected_strike
    assert receipt.selected.applicable_tick == Decimal("0.01")
    assert receipt.selected.limit_price == Decimal("2.03")
    assert receipt.selected.sizing_reference_price == expected_strike
    assert receipt.selected.deliverable_shares == Decimal("100")
    assert receipt.verifies()


def test_golden_csp_receipt_pins_selection_and_canonical_hashes() -> None:
    receipt = _select(_valid_inputs())

    assert receipt.selected is not None
    assert receipt.selected.candidate_evidence_digest == _EXPECTED_CANDIDATE_DIGEST
    assert receipt.output_digest == _EXPECTED_OUTPUT_DIGEST
    assert hashlib.sha256(receipt.canonical_bytes()).hexdigest() == (
        _EXPECTED_CANONICAL_RECEIPT_HASH
    )
    assert receipt.verifies()


def _schema_property_names(value: object) -> set[str]:
    if isinstance(value, dict):
        names = set(value.get("properties", {}))
        return names | set().union(*(_schema_property_names(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_schema_property_names(item) for item in value))
    return set()


def test_request_is_frozen_extra_forbid_and_cannot_express_broker_identity() -> None:
    request = _valid_inputs().request
    with pytest.raises(ValidationError, match="frozen"):
        request.symbol = "MSFT"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OptionSelectionRequest.model_validate({**request.model_dump(), "con_id": 9001})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OptionSelectionRequest.model_validate(
            {**request.model_dump(), "broker": {"contract": {"conId": 9001}}}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        request.model_copy(update={"broker": {"contract": {"conId": 9001}}})

    forbidden = {
        "accountid",
        "clientid",
        "conid",
        "exchange",
        "expiration",
        "expiry",
        "localsymbol",
        "marketruleid",
        "multiplier",
        "orderid",
        "routing",
        "strike",
        "tradingclass",
        "transmit",
    }
    recursive_names = {
        name.replace("_", "").lower()
        for name in _schema_property_names(OptionSelectionRequest.model_json_schema())
    }
    assert recursive_names.isdisjoint(forbidden)

    with pytest.raises(ValidationError, match="requires option right"):
        request.model_copy(update={"right": OptionRight.CALL})


@pytest.mark.parametrize(
    "pathological",
    (
        Decimal("1E-1000000"),
        Decimal("1E+1000000"),
        Decimal("123456789012345678901234567890123"),
    ),
)
def test_request_and_policy_reject_decimal_values_with_unbounded_canonical_width(
    pathological: Decimal,
) -> None:
    inputs = _valid_inputs()

    with pytest.raises(ValidationError, match="option-selection request number"):
        inputs.request.model_copy(update={"requested_contracts": pathological})
    with pytest.raises(ValidationError, match="option-selection policy number"):
        inputs.policy.model_copy(update={"max_quote_age_seconds": pathological})


def test_decimal_width_validation_ignores_canonical_trailing_zeroes() -> None:
    inputs = _valid_inputs()
    value = Decimal("0.1000000000000000000000000000000000000000")

    policy = inputs.policy.model_copy(update={"max_relative_spread": value})

    assert policy.max_relative_spread == value


def test_evidence_rejects_unbounded_market_rule_schedule() -> None:
    inputs = _valid_inputs()
    candidate = inputs.evidence.candidates[0]
    rule = candidate.market_rule
    assert rule is not None
    overbound = rule.model_copy(
        update={
            "price_increments": tuple(
                OptionPriceIncrement(
                    low_edge=Decimal(index),
                    increment=Decimal("0.01"),
                )
                for index in range(MAX_OPTION_MARKET_RULE_INCREMENTS + 1)
            )
        }
    )

    with pytest.raises(ValidationError, match="hard evidence bound"):
        inputs.evidence.model_copy(
            update={"candidates": (candidate.model_copy(update={"market_rule": overbound}),)}
        )


def test_evidence_rejects_broker_decimal_with_unbounded_canonical_width() -> None:
    inputs = _valid_inputs()
    quote = inputs.evidence.underlying_quote
    assert quote is not None
    pathological = quote.model_copy(update={"bid": Decimal("1E-1000000")})

    with pytest.raises(ValidationError, match="option-selection evidence number"):
        inputs.evidence.model_copy(update={"underlying_quote": pathological})


@pytest.mark.parametrize(
    "nested_field",
    ("underlying", "contract", "chain", "market_rule", "deliverable_asset"),
)
def test_evidence_rejects_unbounded_nested_broker_text_before_canonical_rendering(
    nested_field: str,
) -> None:
    inputs = _valid_inputs()
    evidence = inputs.evidence
    candidate = evidence.candidates[0]
    oversized = "x" * (MAX_OPTION_SELECTION_TEXT_CHARS + 1)
    updates: dict[str, object]
    if nested_field == "underlying":
        assert evidence.underlying is not None
        updates = {"underlying": evidence.underlying.model_copy(update={"liquid_hours": oversized})}
    elif nested_field == "contract":
        contract = candidate.contract
        assert isinstance(contract, OptionContract)
        updates = {
            "candidates": (
                candidate.model_copy(
                    update={"contract": contract.model_copy(update={"local_symbol": oversized})}
                ),
            )
        }
    elif nested_field == "chain":
        updates = {
            "chain_parameters": (
                evidence.chain_parameters[0].model_copy(update={"trading_class": oversized}),
            )
        }
    elif nested_field == "market_rule":
        assert candidate.market_rule is not None
        updates = {
            "candidates": (
                candidate.model_copy(
                    update={
                        "market_rule": candidate.market_rule.model_copy(
                            update={"exchange": oversized}
                        )
                    }
                ),
            )
        }
    else:
        assert candidate.deliverable is not None
        updates = {
            "candidates": (
                candidate.model_copy(
                    update={
                        "deliverable": candidate.deliverable.model_copy(
                            update={"other_assets": (oversized,)}
                        )
                    }
                ),
            )
        }

    with pytest.raises(ValidationError, match="text exceeds the hard evidence bound"):
        evidence.model_copy(update=updates)


def test_evidence_rejects_unbounded_deliverable_asset_count() -> None:
    inputs = _valid_inputs()
    candidate = inputs.evidence.candidates[0]
    assert candidate.deliverable is not None
    overbound = candidate.deliverable.model_copy(
        update={
            "other_assets": tuple(
                f"asset-{index}" for index in range(MAX_OPTION_SELECTION_OTHER_ASSETS + 1)
            )
        }
    )

    with pytest.raises(ValidationError, match="deliverable assets exceed"):
        inputs.evidence.model_copy(
            update={"candidates": (candidate.model_copy(update={"deliverable": overbound}),)}
        )


def test_policy_rejects_unbounded_code_collections_before_canonical_rendering() -> None:
    inputs = _valid_inputs()

    with pytest.raises(ValidationError, match="policy codes exceed the hard bound"):
        inputs.policy.model_copy(
            update={
                "permitted_exchanges": tuple(
                    f"EXCHANGE-{index}" for index in range(MAX_OPTION_SELECTION_POLICY_CODES + 1)
                )
            }
        )


@pytest.mark.parametrize(
    "raw_field",
    ("observed_market_rules", "observed_deliverables", "observed_option_quotes"),
)
def test_complete_candidate_set_requires_every_raw_observation_group(raw_field: str) -> None:
    inputs = _valid_inputs()

    receipt = _select(_with_evidence(inputs, **{raw_field: ()}))

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.OBSERVATION_SET_MISMATCH in {
        reason.code for reason in receipt.no_trade_reasons
    }
    assert receipt.verifies()


@pytest.mark.parametrize("raw_field", ("market_rule", "deliverable", "option_quote"))
def test_candidate_projection_must_exactly_match_raw_observation(raw_field: str) -> None:
    inputs = _valid_inputs()
    evidence = inputs.evidence
    if raw_field == "market_rule":
        raw = evidence.observed_market_rules[0]
        changed = raw.model_copy(
            update={"market_rule": raw.market_rule.model_copy(update={"source": "changed"})}
        )
        mutated = _with_evidence(inputs, observed_market_rules=(changed,))
    elif raw_field == "deliverable":
        raw_deliverable = evidence.observed_deliverables[0]
        changed_deliverable = raw_deliverable.model_copy(
            update={
                "deliverable": raw_deliverable.deliverable.model_copy(update={"source": "changed"})
            }
        )
        mutated = _with_evidence(inputs, observed_deliverables=(changed_deliverable,))
    else:
        raw_quote = evidence.observed_option_quotes[0]
        changed_quote = raw_quote.model_copy(
            update={"quote": raw_quote.quote.model_copy(update={"ask": Decimal("2.08")})}
        )
        mutated = _with_evidence(inputs, observed_option_quotes=(changed_quote,))

    receipt = _select(mutated)

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.OBSERVATION_SET_MISMATCH in {
        reason.code for reason in receipt.no_trade_reasons
    }
    assert receipt.verifies()


def test_receipt_rejects_unbounded_derived_decimal_before_canonical_rendering() -> None:
    receipt = _select(_valid_inputs())
    assert receipt.selected is not None
    pathological = receipt.selected.model_copy(update={"limit_price": Decimal("1E-1000000")})

    with pytest.raises(ValidationError, match="option-selection evidence number"):
        receipt.model_copy(update={"selected": pathological})


def test_canonical_json_normalizes_utc_and_decimal_spellings() -> None:
    east = timezone(timedelta(hours=-4))
    value = {
        "amount": Decimal("1.23000"),
        "instant": datetime(2026, 8, 3, 10, 0, tzinfo=east),
        "negative_zero": Decimal("-0.000"),
    }

    assert canonical_bytes(value) == (
        b'{"amount":"1.23","instant":"2026-08-03T14:00:00.000000Z","negative_zero":"0"}'
    )
    assert canonical_digest(value) == canonical_digest(
        {"amount": Decimal("1.23"), "instant": _NOW, "negative_zero": Decimal("0")}
    )


def test_canonical_decimal_bytes_do_not_depend_on_process_context() -> None:
    value = Decimal("123456789012345678901234567890.123450000")

    with localcontext() as context:
        context.prec = 6
        low_precision = canonical_bytes({"value": value})
    with localcontext() as context:
        context.prec = 80
        high_precision = canonical_bytes({"value": value})

    assert low_precision == high_precision
    assert low_precision == b'{"value":"123456789012345678901234567890.12345"}'


def test_receipt_arithmetic_is_independent_of_process_decimal_context() -> None:
    results: list[bytes] = []
    for precision, rounding in ((6, ROUND_DOWN), (28, ROUND_UP), (80, ROUND_DOWN)):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            results.append(_select(_valid_inputs()).canonical_bytes())

    assert results[0] == results[1] == results[2]


def test_selection_policy_rejects_unbounded_scoring_weights() -> None:
    inputs = _valid_inputs()

    with pytest.raises(ValidationError):
        inputs.policy.model_copy(update={"spread_weight": Decimal("1e60")})
    assert inputs.policy.model_copy(update={"spread_weight": Decimal("100")}).spread_weight == 100


def _respell(value: Any) -> Any:
    """Equivalent Decimal and datetime objects with different in-memory spellings."""

    if isinstance(value, datetime):
        return value.astimezone(timezone(timedelta(hours=-4)))
    if isinstance(value, Decimal):
        text = f"{value:f}"
        return Decimal(f"{text}000" if "." in text else f"{text}.000")
    if isinstance(value, dict):
        return {key: _respell(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_respell(item) for item in value)
    if isinstance(value, list):
        return [_respell(item) for item in value]
    return value


def _semantically_equivalent(inputs: _Inputs) -> _Inputs:
    return _Inputs(
        request=OptionSelectionRequest.model_validate(_respell(inputs.request.model_dump())),
        policy=OptionSelectionPolicy.model_validate(_respell(inputs.policy.model_dump())),
        evidence=OptionSelectionEvidence.model_validate(_respell(inputs.evidence.model_dump())),
    )


@settings(max_examples=36, derandomize=True, deadline=None)
@given(
    candidate_order=st.permutations((0, 1, 2)),
    chain_order=st.permutations((0, 1)),
    reason_order=st.permutations((0, 1)),
    respelled=st.booleans(),
)
def test_whole_receipt_is_stable_across_all_input_permutations(
    candidate_order: list[int],
    chain_order: list[int],
    reason_order: list[int],
    respelled: bool,
) -> None:
    inputs = _valid_inputs()
    first = inputs.evidence.candidates[0]
    second = _candidate_with_con_id(first, 2002)
    third = _candidate_with_con_id(second, 2003)
    third_quote = third.quote
    assert third_quote is not None
    third = third.model_copy(update={"quote": third_quote.model_copy(update={"volume": 1500})})
    candidates = (first, second, third)
    valid_chain = inputs.evidence.chain_parameters[0]
    irrelevant_chain = valid_chain.model_copy(update={"underlying_con_id": 9999})
    chains = (valid_chain, irrelevant_chain)
    reasons = (
        SelectionRejection(
            code=SelectionRejectionCode.PACING_EXHAUSTED,
            detail="pacing budget exhausted",
        ),
        SelectionRejection(
            code=SelectionRejectionCode.DATA_CLEANUP_FAILED,
            detail="read-only subscription cleanup failed",
        ),
    )
    reference_inputs = _with_evidence(
        inputs,
        candidates=candidates,
        chain_parameters=chains,
        acquisition_rejections=reasons,
    )
    permuted_inputs = _with_evidence(
        inputs,
        candidates=tuple(candidates[index] for index in candidate_order),
        chain_parameters=tuple(chains[index] for index in chain_order),
        acquisition_rejections=tuple(reasons[index] for index in reason_order),
    )
    if respelled:
        permuted_inputs = _semantically_equivalent(permuted_inputs)

    reference = _select(reference_inputs)
    actual = _select(permuted_inputs)
    assert actual.output_digest == reference.output_digest
    assert actual.canonical_bytes() == reference.canonical_bytes()


def test_documented_total_order_ends_in_con_id() -> None:
    receipt = _select(_valid_inputs())
    lower_tie = receipt.candidates[0].tie_break
    assert lower_tie is not None
    higher_tie = lower_tie.model_copy(update={"con_id": lower_tie.con_id + 1})
    assert lower_tie.key()[:-1] == higher_tie.key()[:-1]
    assert lower_tie.key()[-1] < higher_tie.key()[-1]


def test_multiple_contract_ids_for_one_requested_spec_are_ambiguous_not_ranked() -> None:
    inputs = _valid_inputs()
    first = inputs.evidence.candidates[0]
    second = _candidate_with_con_id(first, 2002)
    receipt = _select(_with_evidence(inputs, candidates=(second, first)))

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.CANDIDATE_SET_MISMATCH in {
        item.code for item in receipt.no_trade_reasons
    }


def test_quote_requires_the_entire_qualified_contract_identity() -> None:
    inputs = _valid_inputs()
    contract = inputs.evidence.candidates[0].contract
    assert isinstance(contract, OptionContract)
    mismatched = contract.model_copy(update={"local_symbol": "AAPL  260904P00191000"})

    receipt = _select(_with_quote(inputs, contract=mismatched))

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH in {
        item.code for item in receipt.no_trade_reasons
    }


def test_one_incomplete_candidate_poisoning_an_exact_fanout_blocks_selection() -> None:
    inputs = _valid_inputs()
    first = inputs.evidence.candidates[0]
    first_contract = first.contract
    first_quote = first.quote
    first_rule = first.market_rule
    first_deliverable = first.deliverable
    assert isinstance(first_contract, OptionContract)
    assert first_quote is not None and first_rule is not None and first_deliverable is not None
    second_contract = first_contract.model_copy(
        update={
            "con_id": 2002,
            "strike": Decimal("195"),
            "local_symbol": "AAPL  260904P00195000",
        }
    )
    second = first.model_copy(
        update={
            "contract": second_contract,
            "quote": first_quote.model_copy(update={"contract": second_contract}),
            "quote_fetched_at": _NOW - timedelta(seconds=31),
            "quote_observed_at": _NOW - timedelta(seconds=31),
            "market_rule": first_rule.model_copy(update={"con_id": 2002}),
            "deliverable": first_deliverable.model_copy(update={"con_id": 2002}),
        }
    )
    chain = inputs.evidence.chain_parameters[0].model_copy(
        update={"strikes": (Decimal("190"), Decimal("195"))}
    )
    plan = build_candidate_acquisition_plan(
        request=inputs.request,
        policy=inputs.policy,
        chain=chain,
        spot=Decimal("200"),
        observed_at=_NOW,
    )
    evidence = inputs.evidence.model_copy(
        update={
            "chain_parameters": (chain,),
            "acquisition_plan": plan,
            "candidates": (first, second),
        }
    )

    receipt = _select(replace(inputs, evidence=evidence))

    assert receipt.status is SelectionStatus.NO_TRADE
    assert receipt.selected is None
    assert SelectionRejectionCode.CONTRACT_DETAILS_STALE in {
        item.code for item in receipt.no_trade_reasons
    }


@pytest.mark.parametrize("conflicting", (False, True), ids=("duplicate", "conflicting"))
def test_duplicate_or_conflicting_same_con_id_is_rejected(conflicting: bool) -> None:
    inputs = _valid_inputs()
    first = inputs.evidence.candidates[0]
    second = first
    if conflicting:
        quote = first.quote
        assert quote is not None
        second = first.model_copy(
            update={"quote": quote.model_copy(update={"ask": Decimal("2.08")})}
        )

    receipt = _select(_with_evidence(inputs, candidates=(first, second)))
    assert receipt.status is SelectionStatus.NO_TRADE
    assert receipt.selected is None
    assert len(receipt.candidates) == 2
    assert all(
        SelectionRejectionCode.DUPLICATE_CONTRACT_ID
        in {reason.code for reason in evaluation.rejections}
        for evaluation in receipt.candidates
    )


@pytest.mark.parametrize(
    ("case", "expected_codes"),
    _GATE_CASES,
    ids=[case for case, _ in _GATE_CASES],
)
def test_each_resolver_gate_fails_a_valid_baseline_closed(
    case: str,
    expected_codes: tuple[SelectionRejectionCode, ...],
) -> None:
    baseline = _select(_valid_inputs())
    receipt = _select(_mutation(case))
    actual_codes = {reason.code for reason in receipt.no_trade_reasons}

    assert receipt.status is SelectionStatus.NO_TRADE
    assert receipt.selected is None
    assert set(expected_codes) <= actual_codes
    assert receipt.output_digest != baseline.output_digest
    assert receipt.verifies()


def test_rejection_code_coverage_is_complete_and_exclusions_are_explained() -> None:
    exercised = {code for _, codes in _GATE_CASES for code in codes}

    assert exercised == set(SelectionRejectionCode) - set(_NON_RESOLVER_CODES)
    assert all(explanation.strip() for explanation in _NON_RESOLVER_CODES.values())


def test_output_digest_detects_receipt_body_mutation() -> None:
    receipt = _select(_valid_inputs())
    tampered = receipt.model_copy(update={"canonical_input_digest": "f" * 64})

    assert receipt.verifies()
    assert not tampered.verifies()
    assert tampered.digest_material_bytes() != receipt.digest_material_bytes()


def test_verification_replays_semantics_even_if_a_tampered_body_is_rehashed() -> None:
    receipt = _select(_valid_inputs())
    tampered = receipt.model_copy(update={"candidates": ()})
    rehashed = tampered.model_copy(
        update={"output_digest": hashlib.sha256(tampered.digest_material_bytes()).hexdigest()}
    )

    assert hashlib.sha256(rehashed.digest_material_bytes()).hexdigest() == rehashed.output_digest
    assert not rehashed.verifies()


def test_unknown_liquidity_is_never_rankable_even_with_zero_floors() -> None:
    inputs = _valid_inputs()
    optional = _with_policy(inputs, min_option_volume=0, min_open_interest=0)
    optional = _with_quote(optional, volume=None, open_interest=None)
    receipt = _select(optional)

    assert receipt.status is SelectionStatus.NO_TRADE
    assert receipt.selected is None
    assert {item.code for item in receipt.no_trade_reasons} >= {
        SelectionRejectionCode.VOLUME_UNKNOWN,
        SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,
    }
    assert receipt.evidence.candidates[0].quote is not None
    assert receipt.evidence.candidates[0].quote.volume is None
    assert receipt.evidence.candidates[0].quote.open_interest is None


def test_golden_receipt_is_restart_deterministic_in_fresh_processes() -> None:
    root = Path(__file__).parents[2]
    script = """
import hashlib
import runpy

namespace = runpy.run_path("tests/unit/test_option_selection.py")
receipt = namespace["_select"](namespace["_valid_inputs"]())
print(receipt.output_digest)
print(hashlib.sha256(receipt.canonical_bytes()).hexdigest())
"""

    first = subprocess.check_output([sys.executable, "-c", script], cwd=root, text=True)
    second = subprocess.check_output([sys.executable, "-c", script], cwd=root, text=True)
    assert first == second
    assert first.splitlines() == [_EXPECTED_OUTPUT_DIGEST, _EXPECTED_CANONICAL_RECEIPT_HASH]


def test_declared_maximum_chain_becomes_a_durable_typed_refusal() -> None:
    inputs = _valid_inputs()
    base = inputs.evidence.chain_parameters[0]
    maximal_row = base.model_copy(
        update={
            "expirations": tuple(
                _NOW.date() + timedelta(days=index + 1)
                for index in range(MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW)
            ),
            "strikes": tuple(
                Decimal(index + 1) for index in range(MAX_OPTION_CHAIN_STRIKES_PER_ROW)
            ),
        }
    )
    evidence = inputs.evidence.model_copy(
        update={"chain_parameters": (maximal_row,) * MAX_OPTION_CHAIN_ROWS}
    )

    receipt = select_option(
        request=inputs.request,
        policy=inputs.policy,
        evidence=evidence,
        mandate_digest=_MANDATE_DIGEST,
        resolver_compatibility_digest=_COMPATIBILITY_DIGEST,
        promotion_digest=_PROMOTION_DIGEST,
    )

    assert receipt.status is SelectionStatus.NO_TRADE
    assert len(receipt.canonical_bytes()) <= MAX_OPTION_SELECTION_RECEIPT_BYTES
    assert receipt.evidence.source == "deterministic-selector-size-guard"
    assert SelectionRejectionCode.EVIDENCE_TRUNCATED in {
        rejection.code for rejection in receipt.no_trade_reasons
    }
    assert receipt.verifies() is True

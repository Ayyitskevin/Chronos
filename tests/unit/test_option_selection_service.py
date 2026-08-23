"""App-plane option selection activation, acquisition, and promotion tests.

Every port in this module is an in-memory fake.  No test opens a socket, reads
credentials, or reaches a broker process.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import re
import textwrap
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import chronos.api.option_selection as option_selection_api
from chronos.api.autonomy_wiring import BackendGatherers, _policy_for, _request_for
from chronos.api.option_selection import (
    PROMOTION_SCHEMA_VERSION,
    AutonomousOptionSelectionService,
    OptionResolverPromotionArtifact,
    _single_acquisition_chain,
    load_option_resolver_promotion,
)
from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionDirection,
    DecisionKind,
    DecisionProvenance,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    SessionPolicy,
    StrategyForm,
    TradableAssetClass,
    TradingSession,
    VersionPins,
)
from chronos.broker.base import BrokerDataError, OptionChainResponse
from chronos.broker.market_data import (
    ManagedQuote,
    MarketDataCancellationError,
    MarketDataPacingExhaustedError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
    OptionChainSnapshot,
)
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerAdapter, BrokerMode, DataQuality, OptionRight
from chronos.domain.models import (
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    OptionDeliverableFacts,
    OptionMarketRule,
    OptionPriceIncrement,
    UnderlyingContract,
)
from chronos.supervisor.option_selection import (
    BROKER_RESOLVER_VERSION,
    CANONICALIZATION_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
    RESOLVER_VERSION,
    SCORING_VERSION,
    OptionSelectionEvidence,
    SelectionRejectionCode,
    SelectionStatus,
    canonical_digest,
)

_NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York.
_EXPIRATION = date(2026, 9, 4)
_FINGERPRINT = "a" * 64
_MANDATE_DIGEST = "b" * 64
_OTHER_DIGEST = "c" * 64


def _promotion_level(mode: AutonomyMode) -> PromotionLevel:
    return {
        AutonomyMode.SHADOW: PromotionLevel.SHADOW,
        AutonomyMode.PAPER_AUTONOMOUS: PromotionLevel.PAPER_AUTONOMOUS,
        AutonomyMode.CANARY_LIVE_AUTONOMOUS: PromotionLevel.CANARY_LIVE_AUTONOMOUS,
        AutonomyMode.LIVE_AUTONOMOUS: PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
    }[mode]


def _mandate(mode: AutonomyMode = AutonomyMode.SHADOW) -> AutonomyMandate:
    return AutonomyMandate(
        mandate_id="option-selection-mandate",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=mode,
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY_OPTION,
                level=_promotion_level(mode),
            ),
        ),
        effective_from=_NOW - timedelta(hours=1),
        expires_at=_NOW + timedelta(days=1),
        versions=VersionPins(
            provider="test-provider",
            model_id="test-model",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY_OPTION,),
            symbols=("AAPL",),
            exchanges=("SMART",),
            contract_families=("AAPL",),
            strategies=(StrategyForm.CASH_SECURED_PUT, StrategyForm.COVERED_CALL),
            order_forms=(OrderForm.MARKETABLE_LIMIT,),
        ),
        capital=CapitalLimits(
            allocated_capital_usd=Decimal("50000"),
            max_order_notional_usd=Decimal("10000"),
            max_position_notional_usd=Decimal("20000"),
            max_gross_exposure_usd=Decimal("50000"),
            max_net_exposure_usd=Decimal("50000"),
            max_contracts_per_order=2,
            min_cash_floor_usd=Decimal("1000"),
            min_buying_power_usd=Decimal("500"),
        ),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal("30"),
            permitted_data_qualities=(DataQuality.LIVE,),
            min_option_volume=100,
            min_open_interest=500,
            max_relative_spread=Decimal("0.10"),
        ),
        sessions=SessionPolicy(permitted_sessions=(TradingSession.REGULAR,)),
        owner_authorization_ref="owner-option-selection",
        authored_at=_NOW - timedelta(hours=2),
    )


def _decision(
    strategy: StrategyForm = StrategyForm.CASH_SECURED_PUT,
) -> AITradeDecision:
    return AITradeDecision(
        decision_id="option-decision-1",
        kind=DecisionKind.OPEN,
        asset_class=TradableAssetClass.EQUITY_OPTION,
        symbol="AAPL",
        direction=DecisionDirection.SHORT,
        requested_strategy=strategy,
        requested_quantity=Decimal("1"),
        evidence=(
            EvidenceCitation(
                evidence_id="option-evidence-1",
                kind="option-chain",
                as_of=_NOW,
                digest="d" * 64,
            ),
        ),
        invalidation_conditions=("underlying leaves the stated setup",),
        provenance=DecisionProvenance(
            provider="test-provider",
            model_id="test-model",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
            evidence_bundle_id="option-bundle-1",
            evidence_bundle_digest="e" * 64,
            produced_at=_NOW,
        ),
    )


def _settings(
    *,
    enabled: bool,
    promotion_file: Path | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        enable_autonomy_option_selection=enabled,
        autonomy_option_resolver_promotion_file=promotion_file,
        broker_mode=BrokerMode.DEMO,
        broker_adapter=BrokerAdapter.DEMO,
        min_dte=25,
        target_dte=32,
        max_dte=45,
        min_abs_delta=Decimal("0.20"),
        target_abs_delta=Decimal("0.30"),
        max_abs_delta=Decimal("0.40"),
        max_expirations=3,
        max_strikes_per_expiration=5,
        min_option_volume=100,
        min_open_interest=500,
        max_relative_spread=Decimal("0.10"),
        max_quote_age_seconds=30,
        market_timezone="America/New_York",
        delta_weight=Decimal("4"),
        spread_weight=Decimal("2"),
        dte_weight=Decimal("1"),
        liquidity_weight=Decimal("1"),
    )


def _underlying() -> UnderlyingContract:
    return UnderlyingContract(
        con_id=1001,
        symbol="AAPL",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )


def _chain(**updates: object) -> OptionChainParameters:
    values: dict[str, object] = {
        "exchange": "SMART",
        "underlying_con_id": 1001,
        "trading_class": "AAPL",
        "multiplier": Decimal("100"),
        "expirations": (_EXPIRATION,),
        "strikes": (Decimal("190"),),
    }
    values.update(updates)
    return OptionChainParameters(**values)


def _option_contract(strike: str, con_id: int) -> OptionContract:
    return OptionContract(
        con_id=con_id,
        symbol="AAPL",
        underlying_con_id=1001,
        expiration=_EXPIRATION,
        strike=Decimal(strike),
        right=OptionRight.PUT,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol=f"AAPL  260904P{int(Decimal(strike) * 1000):08d}",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )


def _partial_stage_error(stage: str) -> MarketDataUnavailableError:
    first = _option_contract("190", 2001)
    values: tuple[object, ...]
    chain_response: OptionChainResponse | None = None
    if stage == "option_chain_parameters":
        values = ()
        chain_response = OptionChainResponse(
            parameters=(_chain(strikes=(Decimal("190"), Decimal("195"))),),
            complete=False,
            truncated=True,
            completion_marker="synthetic-partial-end",
            observed_at=_NOW,
            source="synthetic-partial-chain",
        )
    elif stage == "qualify_option_contracts":
        values = (first,)
    elif stage == "option_market_rules":
        values = (
            OptionMarketRule(
                con_id=first.con_id,
                exchange=first.exchange,
                market_rule_id=26,
                price_increments=(
                    OptionPriceIncrement(
                        low_edge=Decimal("0"),
                        increment=Decimal("0.01"),
                    ),
                ),
                source="synthetic-partial",
            ),
        )
    elif stage == "option_deliverable_facts":
        values = (
            OptionDeliverableFacts(
                con_id=first.con_id,
                authoritative=True,
                source="synthetic-partial",
                underlying_con_id=first.underlying_con_id,
                share_quantity=Decimal("100"),
                cash_amount=Decimal("0"),
                other_assets=(),
            ),
        )
    else:
        assert stage == "option_quotes"
        values = (
            ManagedQuote(
                quote=MarketQuote(
                    contract=first,
                    timestamp=_NOW,
                    data_quality=DataQuality.LIVE,
                    bid=Decimal("2.03"),
                    ask=Decimal("2.07"),
                    last=Decimal("2.05"),
                    volume=1000,
                    open_interest=2000,
                    greeks=ModelGreeks(delta=Decimal("-0.30")),
                ),
                fetched_at=_NOW,
                observed_at=_NOW,
                from_cache=False,
                source_age_seconds=0,
                cache_age_seconds=0,
                fresh=True,
                transmission_eligible=True,
            ),
        )
    return MarketDataUnavailableError(
        "synthetic one-of-two response",
        contract_ids=(2002,),
        observed_values=values,
        observed_at=_NOW,
        truncated=stage == "option_chain_parameters",
        chain_response=chain_response,
    )


class _FakeBroker:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error = error

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self.calls.append(("qualify_underlying", symbol))
        if self.error is not None:
            raise self.error
        return _underlying()


class _FakeMarketData:
    def __init__(
        self,
        parameters: tuple[OptionChainParameters, ...] | None = None,
        *,
        failure_at: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.parameters = parameters or (_chain(),)
        self.failure_at = failure_at
        self.error = error
        self.calls: list[str] = []
        self.narrow_arguments: dict[str, object] | None = None

    def _raise_at(self, operation: str) -> None:
        if self.failure_at == operation:
            assert self.error is not None
            raise self.error

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        self.calls.append("qualify_underlying")
        self._raise_at("qualify_underlying")
        assert symbol == "AAPL"
        return _underlying()

    async def underlying_quote(
        self,
        underlying: UnderlyingContract,
        *,
        force_refresh: bool,
    ) -> ManagedQuote:
        self.calls.append("underlying_quote")
        self._raise_at("underlying_quote")
        assert force_refresh is True
        quote = MarketQuote(
            contract=underlying,
            timestamp=_NOW,
            data_quality=DataQuality.LIVE,
            bid=Decimal("199.90"),
            ask=Decimal("200.10"),
            last=Decimal("200"),
        )
        return ManagedQuote(
            quote=quote,
            fetched_at=_NOW,
            observed_at=_NOW,
            from_cache=False,
            source_age_seconds=0,
            cache_age_seconds=0,
            fresh=True,
            transmission_eligible=True,
        )

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
        *,
        force_refresh: bool,
    ) -> OptionChainSnapshot:
        self.calls.append("option_chain_parameters")
        self._raise_at("option_chain_parameters")
        assert underlying.con_id == 1001
        assert force_refresh is True
        return OptionChainSnapshot(
            parameters=self.parameters,
            fetched_at=_NOW,
            observed_at=_NOW,
            from_cache=False,
            fresh=True,
            complete=True,
            truncated=False,
            completion_marker="synthetic-test-end",
            completion_observed_at=_NOW,
            completion_source="synthetic-test-completed-future",
        )

    def narrow_option_specs(
        self,
        chain: OptionChainParameters,
        *,
        symbol: str,
        spot: Decimal,
        right: OptionRight,
        as_of: date,
        min_dte: int,
        target_dte: int,
        max_dte: int,
        max_expirations: int,
        strikes_per_expiration: int,
        currency: str,
    ) -> tuple[OptionContractSpec, ...]:
        self.calls.append("narrow_option_specs")
        self.narrow_arguments = {
            "spot": spot,
            "right": right,
            "as_of": as_of,
            "min_dte": min_dte,
            "target_dte": target_dte,
            "max_dte": max_dte,
            "max_expirations": max_expirations,
            "strikes_per_expiration": strikes_per_expiration,
        }
        return (
            OptionContractSpec(
                symbol=symbol,
                underlying_con_id=chain.underlying_con_id,
                expiration=_EXPIRATION,
                strike=Decimal("190"),
                right=right,
                exchange=chain.exchange,
                currency=currency,
                multiplier=chain.multiplier,
                trading_class=chain.trading_class,
            ),
        )

    async def qualify_option_contracts(
        self,
        specs: tuple[OptionContractSpec, ...],
        *,
        force_refresh: bool,
    ) -> tuple[OptionContract, ...]:
        self.calls.append("qualify_option_contracts")
        self._raise_at("qualify_option_contracts")
        assert force_refresh is True
        return tuple(
            OptionContract(
                **spec.model_dump(),
                con_id=2001 + index,
                local_symbol=(f"AAPL  260904{spec.right.value}{int(spec.strike * 1000):08d}"),
                liquid_hours="20260803:0930-20260803:1600",
                time_zone_id="US/Eastern",
            )
            for index, spec in enumerate(specs)
        )

    async def option_market_rules(
        self,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[OptionMarketRule, ...]:
        self.calls.append("option_market_rules")
        self._raise_at("option_market_rules")
        return tuple(
            OptionMarketRule(
                con_id=contract.con_id,
                exchange="SMART",
                market_rule_id=26,
                price_increments=(
                    OptionPriceIncrement(
                        low_edge=Decimal("0"),
                        increment=Decimal("0.01"),
                    ),
                ),
                source="deterministic-fake",
            )
            for contract in contracts
        )

    async def option_deliverable_facts(
        self,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[OptionDeliverableFacts, ...]:
        self.calls.append("option_deliverable_facts")
        self._raise_at("option_deliverable_facts")
        return tuple(
            OptionDeliverableFacts(
                con_id=contract.con_id,
                authoritative=True,
                source="deterministic-fake",
                underlying_con_id=contract.underlying_con_id,
                share_quantity=Decimal("100"),
                cash_amount=Decimal("0"),
                other_assets=(),
            )
            for contract in contracts
        )

    async def option_quotes(
        self,
        contracts: tuple[OptionContract, ...],
        *,
        force_refresh: bool,
    ) -> tuple[ManagedQuote, ...]:
        self.calls.append("option_quotes")
        self._raise_at("option_quotes")
        assert force_refresh is True
        return tuple(
            ManagedQuote(
                quote=MarketQuote(
                    contract=contract,
                    timestamp=_NOW,
                    data_quality=DataQuality.LIVE,
                    bid=Decimal("2.03"),
                    ask=Decimal("2.07"),
                    last=Decimal("2.05"),
                    volume=1000,
                    open_interest=2000,
                    greeks=ModelGreeks(delta=Decimal("-0.30")),
                ),
                fetched_at=_NOW,
                observed_at=_NOW,
                from_cache=False,
                source_age_seconds=0,
                cache_age_seconds=0,
                fresh=True,
                transmission_eligible=True,
            )
            for contract in contracts
        )


class _ConnectionRunner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.timeouts: list[float | None] = []

    def run(self, operation: Any, *, timeout: float | None = None) -> OptionSelectionEvidence:
        self.calls += 1
        self.timeouts.append(timeout)
        if self.error is not None:
            operation.close()
            raise self.error
        return asyncio.run(operation)


def _runtime(
    *,
    enabled: bool,
    promotion_file: Path | None = None,
    chains: tuple[OptionChainParameters, ...] | None = None,
    connection_error: Exception | None = None,
    failure_at: str | None = None,
    stage_error: Exception | None = None,
) -> tuple[SimpleNamespace, _FakeBroker, _FakeMarketData, _ConnectionRunner]:
    broker = _FakeBroker()
    market_data = _FakeMarketData(
        chains,
        failure_at=failure_at,
        error=stage_error,
    )
    connection = _ConnectionRunner(connection_error)
    runtime = SimpleNamespace(
        settings=_settings(enabled=enabled, promotion_file=promotion_file),
        broker=broker,
        market_data=market_data,
        connection=connection,
    )
    return runtime, broker, market_data, connection


def _service(
    runtime: SimpleNamespace,
    mandate: AutonomyMandate,
) -> AutonomousOptionSelectionService:
    return AutonomousOptionSelectionService(
        runtime,
        autonomy_mode=mandate.mode.value,
        mandate_digest=_MANDATE_DIGEST,
        account_fingerprint=_FINGERPRINT,
    )


def _resolve(
    service: AutonomousOptionSelectionService,
    runtime: SimpleNamespace,
    mandate: AutonomyMandate,
    decision: AITradeDecision,
    *,
    evaluated_at: datetime,
):
    return service.resolve(
        request=_request_for(decision, runtime),
        policy=_policy_for(decision, mandate, runtime),
        evaluated_at=evaluated_at,
    )


def _resolve_new(
    runtime: SimpleNamespace,
    mandate: AutonomyMandate,
    decision: AITradeDecision,
    *,
    evaluated_at: datetime,
):
    return _resolve(
        _service(runtime, mandate),
        runtime,
        mandate,
        decision,
        evaluated_at=evaluated_at,
    )


def _codes(receipt: Any) -> set[SelectionRejectionCode]:
    return {reason.code for reason in receipt.no_trade_reasons}


def _artifact_values(
    service: AutonomousOptionSelectionService,
    runtime: SimpleNamespace,
    mandate: AutonomyMandate,
    decision: AITradeDecision,
) -> dict[str, object]:
    policy = _policy_for(decision, mandate, runtime)
    return {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "account_fingerprint": _FINGERPRINT,
        "mandate_digest": _MANDATE_DIGEST,
        "policy_digest": canonical_digest(policy),
        "resolver_compatibility_digest": service._compatibility_digest,
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "scoring_version": SCORING_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "broker_resolver_version": BROKER_RESOLVER_VERSION,
        "permitted_modes": (mandate.mode,),
        "effective_from": _NOW - timedelta(minutes=5),
        "expires_at": _NOW + timedelta(minutes=5),
        "owner_authorization_ref": "owner-promotion-1",
        "authored_at": _NOW - timedelta(minutes=10),
    }


def _write_artifact(path: Path, values: dict[str, object]) -> bytes:
    artifact = OptionResolverPromotionArtifact(**values)
    raw = (artifact.model_dump_json() + "\n").encode()
    path.write_bytes(raw)
    return raw


def test_feature_is_default_off_and_does_not_touch_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.model_fields["enable_autonomy_option_selection"].default is False
    runtime, broker, market_data, connection = _runtime(enabled=False)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.FEATURE_DISABLED in _codes(receipt)
    assert connection.calls == 0
    assert broker.calls == []
    assert market_data.calls == []


@pytest.mark.parametrize(
    "mode",
    (AutonomyMode.SHADOW, AutonomyMode.PAPER_AUTONOMOUS),
)
def test_shadow_and_paper_activate_without_a_resolver_promotion(
    mode: AutonomyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, broker, market_data, connection = _runtime(enabled=True)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate(mode)
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.SELECTED
    assert receipt.promotion_digest is None
    assert connection.calls == 1
    assert connection.timeouts == [600.0]
    assert broker.calls == []
    assert market_data.calls == [
        "qualify_underlying",
        "underlying_quote",
        "option_chain_parameters",
        "qualify_option_contracts",
        "option_market_rules",
        "option_deliverable_facts",
        "option_quotes",
    ]
    assert receipt.evidence.acquisition_plan is not None
    assert receipt.evidence.acquisition_plan.max_expirations == 3
    assert receipt.evidence.acquisition_plan.max_strikes_per_expiration == 5


@pytest.mark.parametrize(
    "mode",
    (AutonomyMode.CANARY_LIVE_AUTONOMOUS, AutonomyMode.LIVE_AUTONOMOUS),
)
def test_each_live_mode_requires_a_separate_owner_promotion(
    mode: AutonomyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, broker, market_data, connection = _runtime(enabled=True)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate(mode)
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert SelectionRejectionCode.PROMOTION_REQUIRED in _codes(receipt)
    assert receipt.promotion_digest is None
    assert connection.calls == 0
    assert broker.calls == []
    assert market_data.calls == []


@pytest.mark.parametrize(
    "mode",
    (AutonomyMode.CANARY_LIVE_AUTONOMOUS, AutonomyMode.LIVE_AUTONOMOUS),
)
def test_exact_owner_promotion_allows_only_its_named_live_mode(
    mode: AutonomyMode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resolver-promotion.json"
    runtime, broker, market_data, connection = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(mode)
    decision = _decision()
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, decision)
    raw = _write_artifact(path, values)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.SELECTED
    assert receipt.promotion_digest == hashlib.sha256(raw).hexdigest()
    assert receipt.policy_digest == values["policy_digest"]
    assert receipt.resolver_compatibility_digest == values["resolver_compatibility_digest"]
    assert connection.calls == 1
    assert broker.calls == []
    assert market_data.calls


def test_live_promotion_expiring_during_acquisition_returns_no_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resolver-promotion.json"
    runtime, _, _, connection = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    decision = _decision()
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, decision)
    values["expires_at"] = _NOW + timedelta(seconds=1)
    _write_artifact(path, values)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW + timedelta(seconds=2))

    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt

    assert connection.calls == 1
    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.PROMOTION_MISMATCH in _codes(receipt)


def test_live_promotion_replacement_during_acquisition_returns_no_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resolver-promotion.json"
    runtime, _, _, connection = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    decision = _decision()
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, decision)
    _write_artifact(path, values)
    original_run = connection.run

    def replace_after_read(
        operation: Any,
        *,
        timeout: float | None = None,
    ) -> OptionSelectionEvidence:
        evidence = original_run(operation, timeout=timeout)
        changed = dict(values)
        changed["owner_authorization_ref"] = "owner-promotion-replaced"
        _write_artifact(path, changed)
        return evidence

    monkeypatch.setattr(connection, "run", replace_after_read)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.PROMOTION_MISMATCH in _codes(receipt)


def test_live_handoff_revalidates_current_artifact_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resolver-promotion.json"
    runtime, _, _, _ = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    decision = _decision()
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, decision)
    values["expires_at"] = _NOW + timedelta(seconds=1)
    _write_artifact(path, values)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)
    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt
    assert receipt.status is SelectionStatus.SELECTED

    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW + timedelta(seconds=2))
    assert "effective_window" in service.validate_handoff(receipt)


def test_promotion_compatibility_manifest_equals_all_runtime_python_sources() -> None:
    package_root = Path(option_selection_api.__file__).resolve().parents[1]
    expected = tuple(
        sorted(
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*.py")
            if path.is_file()
        )
    )

    assert option_selection_api._runtime_source_files(package_root) == expected
    assert "api/option_selection.py" in expected
    assert "autonomy/__init__.py" in expected
    assert "supervisor/loop.py" in expected


_PROMOTION_MISMATCHES: tuple[tuple[str, object, str], ...] = (
    ("account_fingerprint", "f" * 64, "account_fingerprint"),
    ("mandate_digest", _OTHER_DIGEST, "mandate_digest"),
    ("policy_digest", _OTHER_DIGEST, "policy_digest"),
    ("resolver_compatibility_digest", _OTHER_DIGEST, "resolver_compatibility_digest"),
    ("request_schema_version", "request-v0", "request_schema_version"),
    ("evidence_schema_version", "evidence-v0", "evidence_schema_version"),
    ("receipt_schema_version", "receipt-v0", "receipt_schema_version"),
    ("resolver_version", "resolver-v0", "resolver_version"),
    ("scoring_version", "scoring-v0", "scoring_version"),
    ("canonicalization_version", "canonical-v0", "canonicalization_version"),
    ("broker_resolver_version", "broker-v0", "broker_resolver_version"),
    (
        "permitted_modes",
        (AutonomyMode.LIVE_AUTONOMOUS,),
        "permitted_modes",
    ),
    ("effective_from", _NOW + timedelta(seconds=1), "effective_window"),
    ("expires_at", _NOW, "effective_window"),
)


@pytest.mark.parametrize(
    ("field", "wrong_value", "mismatch_name"),
    _PROMOTION_MISMATCHES,
    ids=[
        "account-fingerprint",
        "raw-mandate-digest",
        "policy-digest",
        "compatibility-digest",
        "request-schema",
        "evidence-schema",
        "receipt-schema",
        "resolver-version",
        "scoring-version",
        "canonical-version",
        "broker-resolver-version",
        "mode",
        "not-yet-effective",
        "expired",
    ],
)
def test_live_promotion_is_bound_exactly_to_every_material_input(
    field: str,
    wrong_value: object,
    mismatch_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resolver-promotion.json"
    runtime, broker, market_data, connection = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    decision = _decision()
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, decision)
    values[field] = wrong_value
    raw = _write_artifact(path, values)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt

    mismatch = next(
        reason
        for reason in receipt.no_trade_reasons
        if reason.code is SelectionRejectionCode.PROMOTION_MISMATCH
    )
    assert mismatch_name in mismatch.detail
    assert receipt.promotion_digest == hashlib.sha256(raw).hexdigest()
    assert connection.calls == 0
    assert broker.calls == []
    assert market_data.calls == []


@pytest.mark.parametrize(
    "permitted_modes",
    (
        (),
        (AutonomyMode.SHADOW,),
        (
            AutonomyMode.CANARY_LIVE_AUTONOMOUS,
            AutonomyMode.LIVE_AUTONOMOUS,
        ),
    ),
)
def test_promotion_artifact_must_name_exactly_one_live_mode(
    permitted_modes: tuple[AutonomyMode, ...],
) -> None:
    runtime, _, _, _ = _runtime(enabled=True)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    service = _service(runtime, mandate)
    values = _artifact_values(service, runtime, mandate, _decision())
    values["permitted_modes"] = permitted_modes

    with pytest.raises(ValidationError, match="exactly one CANARY/LIVE mode"):
        OptionResolverPromotionArtifact(**values)


@pytest.mark.parametrize(
    "artifact_state",
    ("unset", "missing", "malformed", "missing-fields", "wrong-schema"),
)
def test_missing_or_malformed_live_promotion_fails_closed(
    artifact_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path: Path | None = None if artifact_state == "unset" else tmp_path / "promotion.json"
    runtime, broker, market_data, connection = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    decision = _decision()
    service = _service(runtime, mandate)
    if artifact_state == "malformed":
        assert path is not None
        path.write_text("{not-json")
    elif artifact_state == "missing-fields":
        assert path is not None
        path.write_text('{"schema_version":"option-resolver-promotion-v1"}')
    elif artifact_state == "wrong-schema":
        assert path is not None
        values = _artifact_values(service, runtime, mandate, decision)
        raw = OptionResolverPromotionArtifact(**values).model_dump_json()
        path.write_text(raw.replace(PROMOTION_SCHEMA_VERSION, "option-resolver-promotion-v0", 1))

    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)
    receipt = _resolve(service, runtime, mandate, decision, evaluated_at=_NOW).receipt

    assert SelectionRejectionCode.PROMOTION_REQUIRED in _codes(receipt)
    assert receipt.promotion_digest is None
    assert connection.calls == 0
    assert broker.calls == []
    assert market_data.calls == []


def test_loader_hashes_exact_bytes_and_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "promotion.json"
    runtime, _, _, _ = _runtime(enabled=True, promotion_file=path)
    mandate = _mandate(AutonomyMode.CANARY_LIVE_AUTONOMOUS)
    service = _service(runtime, mandate)
    raw = _write_artifact(path, _artifact_values(service, runtime, mandate, _decision()))
    before = path.stat()

    loaded = load_option_resolver_promotion(path)

    assert loaded is not None
    assert loaded.digest == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == raw
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_mode,
    )


def test_runtime_module_has_no_promotion_creator_or_file_writer() -> None:
    prohibited_api_words = ("create", "author", "write", "save", "persist")
    assert not {
        name
        for name in option_selection_api.__all__
        if any(word in name.lower() for word in prohibited_api_words)
    }

    source = inspect.getsource(option_selection_api)
    tree = ast.parse(source)
    mutating_path_methods = {
        "chmod",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
    called_mutators = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in mutating_path_methods
    }
    assert called_mutators == set()


_ACQUISITION_ERRORS: tuple[tuple[Exception, SelectionRejectionCode], ...] = (
    (
        MarketDataPacingExhaustedError("option-quotes", 3),
        SelectionRejectionCode.PACING_EXHAUSTED,
    ),
    (MarketDataPermissionError("permission denied"), SelectionRejectionCode.DATA_PERMISSION_DENIED),
    (
        MarketDataCancellationError("cleanup failed"),
        SelectionRejectionCode.DATA_CLEANUP_FAILED,
    ),
    (
        MarketDataUnavailableError("snapshot unavailable"),
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
    ),
    (BrokerDataError("broker data unavailable"), SelectionRejectionCode.EVIDENCE_UNAVAILABLE),
    (TimeoutError("bounded read timed out"), SelectionRejectionCode.EVIDENCE_UNAVAILABLE),
    (RuntimeError("unknown read failure"), SelectionRejectionCode.EVIDENCE_UNAVAILABLE),
)


@pytest.mark.parametrize(
    ("error", "expected"),
    _ACQUISITION_ERRORS,
    ids=(
        "pacing",
        "permission",
        "cleanup",
        "market-data-unavailable",
        "broker-data-unavailable",
        "timeout",
        "unknown-fails-unavailable",
    ),
)
def test_acquisition_exceptions_map_to_typed_fail_closed_receipts(
    error: Exception,
    expected: SelectionRejectionCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, broker, market_data, connection = _runtime(
        enabled=True,
        connection_error=error,
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert expected in _codes(receipt)
    assert connection.calls == 1
    assert broker.calls == []
    assert market_data.calls == []
    assert receipt.evidence.source == "demo:demo:" + BROKER_RESOLVER_VERSION


def test_wiring_converts_an_unexpected_resolver_exception_to_a_canonical_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = _runtime(enabled=True)
    mandate = _mandate()
    gatherers = BackendGatherers(
        runtime,
        _FINGERPRINT,
        7,
        mandate=mandate,
        mandate_digest=_MANDATE_DIGEST,
    )

    def explode(**_: object) -> None:
        raise RuntimeError("synthetic resolver failure")

    monkeypatch.setattr(gatherers._option_selection, "resolve", explode)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    facts = gatherers.instrument_facts(_decision())

    assert facts is not None
    assert facts.contract is None
    assert facts.selection_receipt is not None
    assert facts.selection_receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.EVIDENCE_UNAVAILABLE in _codes(facts.selection_receipt)
    assert facts.selection_receipt.verifies() is True
    assert facts.selection_activation_validator is not None


_STAGE_FAILURES: tuple[tuple[str, Exception, SelectionRejectionCode, int], ...] = (
    (
        "qualify_underlying",
        MarketDataUnavailableError("underlying unavailable"),
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
        -1,
    ),
    (
        "underlying_quote",
        MarketDataPermissionError("underlying permission denied"),
        SelectionRejectionCode.DATA_PERMISSION_DENIED,
        0,
    ),
    (
        "option_chain_parameters",
        MarketDataPacingExhaustedError("option-chain", 3),
        SelectionRejectionCode.PACING_EXHAUSTED,
        1,
    ),
    (
        "qualify_option_contracts",
        MarketDataUnavailableError("qualification unavailable"),
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
        2,
    ),
    (
        "option_market_rules",
        MarketDataCancellationError("market-rule cleanup failed"),
        SelectionRejectionCode.DATA_CLEANUP_FAILED,
        3,
    ),
    (
        "option_deliverable_facts",
        BrokerDataError("deliverable unavailable"),
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
        4,
    ),
    (
        "option_quotes",
        TimeoutError("option quotes timed out"),
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
        5,
    ),
)


@pytest.mark.parametrize(
    ("failure_at", "error", "expected", "preserved_level"),
    _STAGE_FAILURES,
    ids=(
        "underlying-qualification",
        "underlying-quote",
        "chain",
        "option-qualification",
        "market-rules",
        "deliverables",
        "option-quotes",
    ),
)
def test_each_acquisition_stage_preserves_facts_already_observed(
    failure_at: str,
    error: Exception,
    expected: SelectionRejectionCode,
    preserved_level: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, broker, market_data, connection = _runtime(
        enabled=True,
        failure_at=failure_at,
        stage_error=error,
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt
    evidence = receipt.evidence

    assert receipt.status is SelectionStatus.NO_TRADE
    assert evidence.complete is False
    assert evidence.acquisition_rejections[0].code is expected
    assert expected in _codes(receipt)
    assert connection.calls == 1
    assert broker.calls == []
    assert (evidence.underlying is not None) is (preserved_level >= 0)
    assert (evidence.underlying_quote is not None) is (preserved_level >= 1)
    assert bool(evidence.chain_parameters) is (preserved_level >= 2)
    assert (evidence.acquisition_plan is not None) is (preserved_level >= 2)
    assert bool(evidence.candidates) is (preserved_level >= 3)
    if preserved_level < 0:
        assert market_data.calls == ["qualify_underlying"]
        return

    assert market_data.calls[-1] == failure_at
    if evidence.candidates:
        candidate = evidence.candidates[0]
        assert candidate.quote is None
        assert (candidate.market_rule is not None) is (preserved_level >= 4)
        assert (candidate.deliverable is not None) is (preserved_level >= 5)


def test_mismatched_underlying_quote_remains_visible_in_partial_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = _underlying().model_copy(update={"con_id": 9999})
    observed = MarketQuote(
        contract=wrong,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("199.90"),
        ask=Decimal("200.10"),
    )
    error = MarketDataUnavailableError(
        "synthetic mismatched underlying quote",
        contract_ids=(1001,),
        observed_values=(observed,),
        observed_at=_NOW,
    )
    runtime, _, _, _ = _runtime(
        enabled=True,
        failure_at="underlying_quote",
        stage_error=error,
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve_new(runtime, _mandate(), _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert receipt.evidence.underlying_quote == observed
    assert SelectionRejectionCode.UNDERLYING_QUOTE_IDENTITY_MISMATCH in _codes(receipt)
    assert receipt.verifies() is True


def test_pathological_broker_decimal_becomes_a_small_canonical_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = MarketQuote(
        contract=_underlying(),
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("1E-1000000"),
        ask=Decimal("200.10"),
    )
    error = MarketDataUnavailableError(
        "synthetic pathological quote",
        observed_values=(observed,),
        observed_at=_NOW,
    )
    runtime, _, _, _ = _runtime(
        enabled=True,
        failure_at="underlying_quote",
        stage_error=error,
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve_new(runtime, _mandate(), _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.EVIDENCE_UNAVAILABLE in _codes(receipt)
    assert len(receipt.canonical_bytes()) < 10_000
    assert receipt.verifies() is True


@pytest.mark.parametrize(
    ("failure_at", "candidate_count", "observed_field"),
    (
        ("option_chain_parameters", 0, None),
        ("qualify_option_contracts", 1, "contract"),
        ("option_market_rules", 2, "market_rule"),
        ("option_deliverable_facts", 2, "deliverable"),
        ("option_quotes", 2, "quote"),
    ),
)
def test_partial_stage_results_retain_every_bounded_observation(
    failure_at: str,
    candidate_count: int,
    observed_field: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = _runtime(
        enabled=True,
        chains=(_chain(strikes=(Decimal("190"), Decimal("195"))),),
        failure_at=failure_at,
        stage_error=_partial_stage_error(failure_at),
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt
    evidence = receipt.evidence

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.EVIDENCE_UNAVAILABLE in _codes(receipt)
    assert SelectionRejectionCode.EVIDENCE_PARTIAL in _codes(receipt)
    assert "affected contract ids: 2002" in evidence.acquisition_rejections[0].detail
    assert len(evidence.candidates) == candidate_count
    if failure_at == "option_chain_parameters":
        assert len(evidence.chain_parameters) == 1
        assert evidence.chain_completion_source == "synthetic-partial-chain"
        assert evidence.chain_completion_observed_at == _NOW
        assert evidence.truncated is True
        assert SelectionRejectionCode.EVIDENCE_TRUNCATED in _codes(receipt)
    elif observed_field == "contract":
        assert evidence.candidates[0].contract == _option_contract("190", 2001)
    else:
        assert observed_field is not None
        assert (
            sum(getattr(candidate, observed_field) is not None for candidate in evidence.candidates)
            == 1
        )


def _contaminating_observation(stage: str, con_id: int, suffix: str) -> object:
    contract = _option_contract("190", con_id)
    if stage == "option_market_rules":
        return OptionMarketRule(
            con_id=con_id,
            exchange="SMART",
            market_rule_id=26 if suffix == "a" else 27,
            price_increments=(
                OptionPriceIncrement(
                    low_edge=Decimal("0"),
                    increment=Decimal("0.01"),
                ),
            ),
            source=f"synthetic-{suffix}",
        )
    if stage == "option_deliverable_facts":
        return OptionDeliverableFacts(
            con_id=con_id,
            authoritative=True,
            source=f"synthetic-{suffix}",
            underlying_con_id=1001,
            share_quantity=Decimal("100"),
            cash_amount=Decimal("0"),
            other_assets=(),
        )
    assert stage == "option_quotes"
    return ManagedQuote(
        quote=MarketQuote(
            contract=contract,
            timestamp=_NOW,
            data_quality=DataQuality.LIVE,
            bid=Decimal("2.03") if suffix == "a" else Decimal("2.04"),
            ask=Decimal("2.07"),
            volume=1000,
            open_interest=2000,
            greeks=ModelGreeks(delta=Decimal("-0.30")),
        ),
        fetched_at=_NOW,
        observed_at=_NOW,
        from_cache=False,
        source_age_seconds=0,
        cache_age_seconds=0,
        fresh=True,
        transmission_eligible=True,
    )


@pytest.mark.parametrize(
    ("stage", "raw_field", "identity_field"),
    (
        ("option_market_rules", "observed_market_rules", "market_rule"),
        ("option_deliverable_facts", "observed_deliverables", "deliverable"),
        ("option_quotes", "observed_option_quotes", "quote"),
    ),
)
@pytest.mark.parametrize("contamination", ("duplicate", "unexpected"))
def test_partial_receipt_preserves_duplicate_and_unexpected_raw_observations(
    stage: str,
    raw_field: str,
    identity_field: str,
    contamination: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_id = 2001 if contamination == "duplicate" else 9999
    error = MarketDataUnavailableError(
        "synthetic contaminated response",
        observed_values=(
            _contaminating_observation(stage, 2001, "a"),
            _contaminating_observation(stage, second_id, "b"),
        ),
        observed_at=_NOW,
    )
    runtime, _, _, _ = _runtime(
        enabled=True,
        chains=(_chain(strikes=(Decimal("190"), Decimal("195"))),),
        failure_at=stage,
        stage_error=error,
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    receipt = _resolve_new(runtime, _mandate(), _decision(), evaluated_at=_NOW).receipt
    raw = getattr(receipt.evidence, raw_field)
    raw_ids = tuple(
        (
            getattr(item, identity_field).contract.con_id
            if identity_field == "quote"
            else getattr(item, identity_field).con_id
        )
        for item in raw
    )

    assert len(raw) == 2
    assert raw_ids == tuple(sorted((2001, second_id)))
    assert tuple(candidate.contract.con_id for candidate in receipt.evidence.candidates) == (
        2001,
        2002,
    )
    assert SelectionRejectionCode.OBSERVATION_SET_MISMATCH in _codes(receipt)
    assert receipt.verifies() is True


def test_source_hash_failure_blocks_even_non_live_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, broker, market_data, connection = _runtime(enabled=True)

    def fail_hash() -> str:
        raise OSError("source disappeared during hashing")

    monkeypatch.setattr(option_selection_api, "resolver_compatibility_digest", fail_hash)
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)
    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert SelectionRejectionCode.PROMOTION_MISMATCH in _codes(receipt)
    assert receipt.resolver_compatibility_digest == "0" * 64
    assert connection.calls == 0
    assert broker.calls == []
    assert market_data.calls == []


def _codes_named_in(function: Any) -> set[SelectionRejectionCode]:
    source = textwrap.dedent(inspect.getsource(function))
    names = set(re.findall(r"SelectionRejectionCode\.([A-Z_]+)", source))
    return {SelectionRejectionCode[name] for name in names}


def test_every_runtime_and_acquisition_only_rejection_code_has_a_case() -> None:
    emitted = _codes_named_in(AutonomousOptionSelectionService._activation)
    emitted |= _codes_named_in(option_selection_api._acquisition_code)
    exercised = {
        SelectionRejectionCode.FEATURE_DISABLED,
        SelectionRejectionCode.PROMOTION_REQUIRED,
        SelectionRejectionCode.PROMOTION_MISMATCH,
        *(expected for _, expected in _ACQUISITION_ERRORS),
    }

    assert exercised == emitted


def test_single_acquisition_chain_requires_one_exact_policy_match() -> None:
    runtime, _, _, _ = _runtime(enabled=True)
    policy = _policy_for(_decision(), _mandate(), runtime)
    matching = _chain()
    irrelevant = (
        _chain(underlying_con_id=9999),
        _chain(exchange="CBOE"),
        _chain(trading_class="MSFT"),
        _chain(multiplier=Decimal("50")),
    )

    assert _single_acquisition_chain((*irrelevant, matching), 1001, policy) == matching
    assert _single_acquisition_chain(irrelevant, 1001, policy) is None
    assert _single_acquisition_chain((matching, matching), 1001, policy) is None


def test_policy_digest_binds_owner_dte_and_delta_configuration() -> None:
    runtime, _, _, _ = _runtime(enabled=True)
    mandate = _mandate()
    original = _policy_for(_decision(), mandate, runtime)

    runtime.settings.target_dte = 33
    runtime.settings.target_abs_delta = Decimal("0.31")
    changed = _policy_for(_decision(), mandate, runtime)

    assert canonical_digest(changed) != canonical_digest(original)
    assert changed.target_dte == 33
    assert changed.target_abs_delta == Decimal("0.31")


def test_ambiguous_acquisition_chains_never_reach_contract_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = _chain()
    runtime, broker, market_data, connection = _runtime(
        enabled=True,
        chains=(matching, matching),
    )
    monkeypatch.setattr(option_selection_api, "utc_now", lambda: _NOW)

    mandate = _mandate()
    receipt = _resolve_new(runtime, mandate, _decision(), evaluated_at=_NOW).receipt

    assert receipt.status is SelectionStatus.NO_TRADE
    assert SelectionRejectionCode.CHAIN_AMBIGUOUS in _codes(receipt)
    assert connection.calls == 1
    assert broker.calls == []
    assert "narrow_option_specs" not in market_data.calls
    assert "qualify_option_contracts" not in market_data.calls
    assert market_data.calls == [
        "qualify_underlying",
        "underlying_quote",
        "option_chain_parameters",
        "option_market_rules",
        "option_deliverable_facts",
        "option_quotes",
    ]


@pytest.mark.parametrize(
    ("strategy", "expected_right"),
    (
        (StrategyForm.CASH_SECURED_PUT, OptionRight.PUT),
        (StrategyForm.COVERED_CALL, OptionRight.CALL),
    ),
)
def test_request_contains_only_economics_and_derives_the_option_right(
    strategy: StrategyForm,
    expected_right: OptionRight,
) -> None:
    runtime, _, _, _ = _runtime(enabled=True)
    request = _request_for(_decision(strategy), runtime)

    assert request.right is expected_right
    assert request.strategy is strategy
    assert request.requested_contracts == Decimal("1")
    assert set(type(request).model_fields) == {
        "schema_version",
        "decision_id",
        "decision_kind",
        "asset_class",
        "symbol",
        "strategy",
        "right",
        "requested_contracts",
        "moneyness",
        "currency",
    }
    assert set(type(request).model_fields).isdisjoint(
        {
            "account_id",
            "account_fingerprint",
            "con_id",
            "expiration",
            "strike",
            "exchange",
            "trading_class",
            "local_symbol",
            "order_type",
            "route",
            "transmit",
            "limit_price",
        }
    )

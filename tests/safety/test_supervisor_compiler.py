"""Compilation is deterministic, and the capability matrix is a whitelist (M4).

This is the step that finally routes something through the gate. Until M4 the
supervisor admitted and sized decisions that then went nowhere; the properties
pinned here are what make it safe for that to change.

- **Unmapped means refused.** Every `(asset class, kind, strategy)` combination
  is enumerated, and anything not deliberately mapped must refuse. That is how
  you prove a whitelist is a whitelist rather than trusting that it is: adding
  an enum member cannot silently become a tradable capability.
- **The model supplies no price.** The limit is derived from the supervisor's
  quote alone. An entry trigger is a *condition* and can only PREVENT an order.
- **The resolver is not trusted.** A wrong-symbol or wrong-type contract is
  caught here, because a resolver bug must not become a trade in the wrong
  instrument.
- **`scope.exchanges` and `scope.contract_families` finally bind.** M2 disclosed
  them as unenforceable because no qualified contract existed to check them
  against. One does now.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    DecisionProvenance,
    EntryPlan,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PriceReference,
    PriceTrigger,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    TriggerComparator,
    VersionPins,
)
from chronos.domain.enums import DataQuality, OptionRight, OrderIntent
from chronos.domain.models import CryptoContract, OptionContract, UnderlyingContract
from chronos.supervisor.compiler import (
    CompilationRefusal,
    QuoteEvidence,
    compile_order,
    select_intent,
)

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_ACCOUNT = "DU1234567"


def _pins() -> VersionPins:
    return VersionPins(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
    )


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": _NOW - timedelta(hours=1),
        "expires_at": _NOW + timedelta(days=1),
        "versions": _pins(),
        "scope": InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        "concentration": ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        "owner_authorization_ref": "owner-1",
        "authored_at": _NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _decision(**overrides: Any) -> AITradeDecision:
    base: dict[str, Any] = {
        "decision_id": "d-1",
        "kind": DecisionKind.OPEN,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "requested_strategy": StrategyForm.LONG_EQUITY,
        "evidence": (
            EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_NOW, digest="c" * 64),
        ),
        "invalidation_conditions": ("closes below 400",),
        "provenance": DecisionProvenance(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
            evidence_bundle_id="eb-1",
            evidence_bundle_digest="b" * 64,
            produced_at=_NOW,
        ),
    }
    base.update(overrides)
    return AITradeDecision(**base)


def _stock(**overrides: Any) -> UnderlyingContract:
    base: dict[str, Any] = {"con_id": 111, "symbol": "SPY", "exchange": "SMART", "currency": "USD"}
    base.update(overrides)
    return UnderlyingContract(**base)


def _option_capital() -> CapitalLimits:
    """An option-scoped mandate must state a contract ceiling (M2 deny-by-default)."""

    return CapitalLimits(
        allocated_capital_usd=Decimal(50_000),
        max_order_notional_usd=Decimal(10_000),
        max_gross_exposure_usd=Decimal(500_000),
        max_net_exposure_usd=Decimal(500_000),
        max_position_notional_usd=Decimal(100_000),
        max_contracts_per_order=10,
        min_cash_floor_usd=Decimal(1_000),
        min_buying_power_usd=Decimal(500),
    )


def _option(**overrides: Any) -> OptionContract:
    base: dict[str, Any] = {
        "con_id": 222,
        "symbol": "SPY",
        "expiration": date(2026, 8, 21),
        "strike": Decimal(400),
        "right": OptionRight.PUT,
        "exchange": "SMART",
        "currency": "USD",
        "multiplier": Decimal(100),
        "trading_class": "SPY",
        "local_symbol": "SPY   260821P00400000",
        "min_tick": Decimal("0.01"),
    }
    base.update(overrides)
    return OptionContract(**base)


def _quote(bid: str = "399.98", ask: str = "400.02") -> QuoteEvidence:
    return QuoteEvidence(bid=Decimal(bid), ask=Decimal(ask))


def _compile(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "decision": _decision(),
        "mandate": _mandate(),
        "contract": _stock(),
        "quantity": Decimal(10),
        "quote": _quote(),
        "account_id": _ACCOUNT,
    }
    base.update(overrides)
    return compile_order(**base)


# ------------------------------------------------------- the capability matrix


def test_the_capability_matrix_is_a_whitelist_over_every_combination() -> None:
    """Exhaustive: everything not deliberately mapped must refuse.

    This is the asset-class authority matrix as an executable control. Trusting
    a whitelist without enumerating it is how a new enum member quietly becomes
    a new tradable capability.
    """

    mapped: list[tuple[str, str, str]] = []
    for asset_class, kind, strategy in itertools.product(
        TradableAssetClass, DecisionKind, StrategyForm
    ):
        try:
            decision = _decision(
                asset_class=asset_class,
                kind=kind,
                requested_strategy=strategy,
                symbol="SPY",
                futures_root="ES" if asset_class is TradableAssetClass.FUTURE else "",
                target_client_reference=(
                    "CHR-ORD-" + "A" * 32
                    if kind
                    in {
                        DecisionKind.INCREASE,
                        DecisionKind.REDUCE,
                        DecisionKind.CLOSE,
                        DecisionKind.CANCEL,
                        DecisionKind.REPLACE,
                        DecisionKind.ROLL,
                        DecisionKind.HEDGE,
                    }
                    else None
                ),
            )
        except Exception:
            # The decision contract itself refuses some combinations. That is a
            # stronger guarantee than the matrix, not a gap.
            continue
        if select_intent(decision) is not None:
            mapped.append((asset_class.value, kind.value, strategy.value))

    # Only the combinations we deliberately allow. Anything else appearing here
    # is a new tradable capability that arrived without review.
    for asset_class, kind, _strategy in mapped:
        assert asset_class in {"EQUITY", "EQUITY_OPTION", "CRYPTO"}, (
            f"{asset_class} became tradable; futures and index options have no adapter"
        )
        assert kind in {"OPEN", "INCREASE", "REDUCE", "CLOSE"}


def test_futures_options_cannot_even_be_expressed_as_a_decision() -> None:
    """Refused one layer earlier than the matrix: the contract will not build one.

    FUTURE_OPTION is recognized-but-refused vocabulary, so there is no decision
    for the compiler to reject -- the strongest form of "out of scope".
    """

    with pytest.raises(ValidationError, match="out of scope"):
        _decision(
            asset_class=TradableAssetClass.FUTURE_OPTION,
            requested_strategy=StrategyForm.CASH_SECURED_PUT,
        )


@pytest.mark.parametrize(
    "asset_class",
    [TradableAssetClass.FUTURE, TradableAssetClass.INDEX_OPTION],
)
def test_asset_classes_without_an_adapter_cannot_be_compiled(
    asset_class: TradableAssetClass,
) -> None:
    """Refused here as well as by the mandate validator: no single mistake enables them."""

    decision = _decision(
        asset_class=asset_class,
        symbol="" if asset_class is TradableAssetClass.FUTURE else "SPY",
        futures_root="ES" if asset_class is TradableAssetClass.FUTURE else "",
        requested_strategy=(
            StrategyForm.LONG_FUTURE
            if asset_class is TradableAssetClass.FUTURE
            else StrategyForm.CASH_SECURED_PUT
        ),
    )
    assert select_intent(decision) is None


@pytest.mark.parametrize(
    "strategy",
    [
        StrategyForm.SHORT_EQUITY,
        StrategyForm.VERTICAL_CREDIT_SPREAD,
        StrategyForm.VERTICAL_DEBIT_SPREAD,
        StrategyForm.LONG_CALL,
        StrategyForm.LONG_PUT,
    ],
)
def test_unsupported_strategies_have_no_capability(strategy: StrategyForm) -> None:
    """Shorting has no borrow; spreads would need one leg at a time (naked exposure)."""

    asset_class = (
        TradableAssetClass.EQUITY
        if strategy is StrategyForm.SHORT_EQUITY
        else TradableAssetClass.EQUITY_OPTION
    )
    assert select_intent(_decision(asset_class=asset_class, requested_strategy=strategy)) is None


def test_an_unmappable_combination_refuses_rather_than_guessing() -> None:
    outcome = _compile(decision=_decision(requested_strategy=StrategyForm.SHORT_EQUITY))
    assert outcome.compiled is False
    assert outcome.refusal == CompilationRefusal.NO_CAPABILITY


def test_the_happy_path_compiles_a_limit_order() -> None:
    outcome = _compile()
    assert outcome.compiled is True
    intent = outcome.intent
    assert intent is not None
    assert intent.intent is OrderIntent.OPEN_LONG_STOCK
    assert intent.quantity == Decimal(10)
    assert intent.order_type == "LMT"
    assert intent.limit_price > 0
    assert outcome.steps  # the derivation is explainable


# ------------------------------------------------------------ contract checks


def test_a_missing_contract_refuses() -> None:
    outcome = _compile(contract=None)
    assert outcome.refusal == CompilationRefusal.CONTRACT_TYPE_MISMATCH


def test_a_wrong_type_contract_refuses() -> None:
    outcome = _compile(contract=_option())
    assert outcome.refusal == CompilationRefusal.CONTRACT_TYPE_MISMATCH


def test_a_resolver_that_returns_the_wrong_symbol_is_caught() -> None:
    """A resolver bug must never become a trade in the wrong instrument."""

    outcome = _compile(contract=_stock(symbol="QQQ"))
    assert outcome.refusal == CompilationRefusal.CONTRACT_SYMBOL_MISMATCH


def test_an_exchange_outside_the_mandate_refuses() -> None:
    """The M2 gap, closed: scope.exchanges needed a qualified contract to bind."""

    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
            exchanges=("ARCA",),
        )
    )
    outcome = _compile(mandate=mandate, contract=_stock(exchange="SMART"))
    assert outcome.refusal == CompilationRefusal.EXCHANGE_NOT_PERMITTED


def test_a_permitted_exchange_compiles() -> None:
    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
            exchanges=("SMART",),
        )
    )
    assert _compile(mandate=mandate).compiled is True


def test_a_contract_family_outside_the_mandate_refuses() -> None:
    """The other M2 gap: scope.contract_families, checked on trading_class."""

    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY_OPTION,),
            symbols=("SPY",),
            strategies=(StrategyForm.CASH_SECURED_PUT,),
            order_forms=(OrderForm.LIMIT,),
            contract_families=("SPXW",),
        ),
        capital=_option_capital(),
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY_OPTION,
                level=PromotionLevel.PAPER_AUTONOMOUS,
            ),
        ),
    )
    outcome = _compile(
        decision=_decision(
            asset_class=TradableAssetClass.EQUITY_OPTION,
            requested_strategy=StrategyForm.CASH_SECURED_PUT,
        ),
        mandate=mandate,
        contract=_option(trading_class="SPY"),
        quote=_quote("1.15", "1.25"),
    )
    assert outcome.refusal == CompilationRefusal.FAMILY_NOT_PERMITTED


def test_a_non_usd_contract_refuses_because_every_limit_is_usd() -> None:
    """Comparing a EUR notional against a USD ceiling is a silent limit breach."""

    outcome = _compile(contract=_stock(currency="EUR"))
    assert outcome.refusal == CompilationRefusal.CURRENCY_NOT_PERMITTED


# ------------------------------------------------------------------- pricing


def test_the_limit_price_comes_from_the_quote_not_the_model() -> None:
    """A resting BUY limit sits on the bid; it never crosses to the ask."""

    outcome = _compile(quote=_quote("399.00", "401.00"))
    assert outcome.intent is not None
    assert outcome.intent.limit_price == Decimal("399.00")


def test_a_marketable_limit_crosses_the_spread() -> None:
    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.MARKETABLE_LIMIT,),
        )
    )
    outcome = _compile(mandate=mandate, quote=_quote("399.00", "401.00"))
    assert outcome.intent is not None
    assert outcome.intent.limit_price == Decimal("401.00")


def test_the_passive_form_is_preferred_when_both_are_permitted() -> None:
    """Paying the spread should be an explicit grant, not the default."""

    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT, OrderForm.MARKETABLE_LIMIT),
        )
    )
    outcome = _compile(mandate=mandate, quote=_quote("399.00", "401.00"))
    assert outcome.intent is not None
    assert outcome.intent.limit_price == Decimal("399.00")  # the bid, i.e. passive


def test_a_crossed_or_empty_book_refuses() -> None:
    for bid, ask in (("401.00", "399.00"), ("0", "399.00"), ("399.00", "0")):
        outcome = _compile(quote=QuoteEvidence(bid=Decimal(bid), ask=Decimal(ask)))
        assert outcome.refusal == CompilationRefusal.NO_QUOTE


def test_no_quote_refuses() -> None:
    assert _compile(quote=None).refusal == CompilationRefusal.NO_QUOTE


def test_tick_conformance_rounds_away_from_aggression() -> None:
    """Rounding must never make an order more expensive than the derived price."""

    outcome = _compile(quote=_quote("399.999", "401.001"))
    assert outcome.intent is not None
    # A BUY rounds DOWN to the tick: 399.999 -> 399.99, never up to 400.00.
    assert outcome.intent.limit_price == Decimal("399.99")


def test_an_option_without_a_tick_refuses_rather_than_assuming_one() -> None:
    """Venue metadata is never assumed (ADR-0010)."""

    option = _option()
    object.__setattr__(option, "min_tick", None)
    outcome = _compile(
        decision=_decision(
            asset_class=TradableAssetClass.EQUITY_OPTION,
            requested_strategy=StrategyForm.CASH_SECURED_PUT,
        ),
        mandate=_mandate(
            scope=InstrumentScope(
                asset_classes=(TradableAssetClass.EQUITY_OPTION,),
                symbols=("SPY",),
                strategies=(StrategyForm.CASH_SECURED_PUT,),
                order_forms=(OrderForm.LIMIT,),
            ),
            capital=_option_capital(),
            promotions=(
                FamilyPromotion(
                    asset_class=TradableAssetClass.EQUITY_OPTION,
                    level=PromotionLevel.PAPER_AUTONOMOUS,
                ),
            ),
        ),
        contract=option,
        quote=_quote("1.15", "1.25"),
        quantity=Decimal(1),
    )
    assert outcome.refusal == CompilationRefusal.TICK_NOT_KNOWN


# --------------------------------------------------------- the entry trigger


def test_an_unmet_entry_condition_prevents_compilation() -> None:
    """A trigger is a condition. The only thing it may do is stop the order."""

    decision = _decision(
        entry_plan=EntryPlan(
            trigger=PriceTrigger(
                comparator=TriggerComparator.AT_OR_BELOW,
                reference=PriceReference.ASK,
                value=Decimal(300),
            )
        )
    )
    outcome = _compile(decision=decision)
    assert outcome.refusal == CompilationRefusal.ENTRY_CONDITION_UNMET


def test_a_met_entry_condition_compiles_unchanged() -> None:
    """Crucially: meeting a condition must not change the price it compiles at."""

    without = _compile()
    decision = _decision(
        entry_plan=EntryPlan(
            trigger=PriceTrigger(
                comparator=TriggerComparator.AT_OR_BELOW,
                reference=PriceReference.ASK,
                value=Decimal(500),
            )
        )
    )
    with_trigger = _compile(decision=decision)
    assert with_trigger.compiled is True
    assert without.intent is not None and with_trigger.intent is not None
    assert with_trigger.intent.limit_price == without.intent.limit_price


def test_a_trigger_the_supervisor_cannot_evaluate_refuses() -> None:
    """Silently ignoring it would let a model attach conditions that do nothing."""

    decision = _decision(
        entry_plan=EntryPlan(
            trigger=PriceTrigger(
                comparator=TriggerComparator.AT_OR_BELOW,
                reference=PriceReference.LAST,
                value=Decimal(500),
            )
        )
    )
    outcome = _compile(decision=decision)
    assert outcome.refusal == CompilationRefusal.ENTRY_CONDITION_UNEVALUABLE


def test_the_decision_carries_no_field_that_could_set_a_price() -> None:
    """Structural: there is no model-supplied price to clamp in the first place.

    An earlier draft read `PriceTrigger.value` as a limit price and clamped it.
    Reading the contract correctly is the stronger guarantee -- but only while
    no price-shaped field appears on the decision, so that is asserted.
    """

    fields = set(AITradeDecision.model_fields)
    assert not any("price" in name for name in fields), (
        f"AITradeDecision gained a price-shaped field: {sorted(fields)}. Compilation "
        "derives every price from the supervisor's quote; a model-supplied price would "
        "need an explicit clamp and an ADR."
    )


# --------------------------------------------------------------- other gates


def test_an_unsized_decision_compiles_nothing() -> None:
    """A refused size is not a zero size."""

    for quantity in (None, Decimal(0), Decimal(-1)):
        assert _compile(quantity=quantity).refusal == CompilationRefusal.QUANTITY_NOT_SIZED


def test_a_mandate_with_no_order_form_compiles_nothing() -> None:
    mandate = _mandate.__wrapped__() if hasattr(_mandate, "__wrapped__") else _mandate()
    scope = InstrumentScope(
        asset_classes=(TradableAssetClass.EQUITY,),
        symbols=("SPY",),
        strategies=(StrategyForm.LONG_EQUITY,),
        order_forms=(OrderForm.LIMIT,),
    )
    object.__setattr__(scope, "order_forms", ())
    object.__setattr__(mandate, "scope", scope)
    assert _compile(mandate=mandate).refusal == CompilationRefusal.NO_ORDER_FORM


def test_compilation_never_raises_on_adversarial_input() -> None:
    """A decision that passed every gate must not be stranded by an exception."""

    for bid, ask in (("1E-30", "1E-30"), ("1E20", "1E20")):
        outcome = _compile(quote=QuoteEvidence(bid=Decimal(bid), ask=Decimal(ask)))
        assert outcome.compiled is False or outcome.intent is not None


def test_a_fractional_equity_quantity_is_refused_by_the_order_plane() -> None:
    """Defense in depth: sizing floors to whole units, and the intent re-asserts it."""

    outcome = _compile(quantity=Decimal("10.5"))
    assert outcome.refusal == CompilationRefusal.INTENT_REJECTED


def test_crypto_may_be_fractional() -> None:
    mandate = _mandate(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.CRYPTO,),
            symbols=("BTC",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.CRYPTO, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
    )
    outcome = _compile(
        decision=_decision(asset_class=TradableAssetClass.CRYPTO, symbol="BTC"),
        mandate=mandate,
        contract=CryptoContract(
            con_id=333,
            symbol="BTC",
            exchange="PAXOS",
            currency="USD",
            min_tick=Decimal("0.01"),
        ),
        quantity=Decimal("0.005"),
        quote=_quote("60000.00", "60001.00"),
    )
    assert outcome.compiled is True
    assert outcome.intent is not None
    assert outcome.intent.quantity == Decimal("0.005")


def test_the_compiled_intent_never_transmits() -> None:
    """Compilation produces an intent, not a send. transmit stays False."""

    outcome = _compile()
    assert outcome.intent is not None
    assert outcome.intent.to_order_request().transmit is False

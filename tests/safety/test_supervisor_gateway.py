"""The deterministic gateway admits, refuses, and re-sizes (ADR-0016 §2, M2).

M1 delivered contracts that *cannot express* an order. That is necessary and not
sufficient — a well-formed decision still has to be judged. This module pins the
judging:

- **Admission is deny-by-default.** Every refusal path is exercised
  individually, and each asserts its own refusal code so a future change cannot
  quietly collapse two failures into one.
- **The model's requested quantity is not executable.** Sizing is tested by
  asking for far more than the mandate allows and asserting the kernel returns
  its own smaller number, or refuses entirely.
- **A refusal cannot be routed around.** Replaying an admitted decision id is
  refused, which is the contract ADR-0016 §2 makes about repeated re-submission.
- **An AI failure never becomes permission to trade.** Degraded state refuses
  before any scope or promotion check can look like a green light.

Everything here is pure: no broker, no database, no model, no clock of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    DecisionProvenance,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.supervisor import (
    AccountEvidence,
    AdmissionRefusal,
    SupervisorState,
    admit,
    size_order,
)

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


def _pins(**overrides: str) -> VersionPins:
    base: dict[str, str] = {
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "1",
        "prompt_version": "1",
        "tool_schema_version": "1",
        "decision_schema_version": "1",
        "policy_version": "1",
    }
    base.update(overrides)
    return VersionPins(**base)


def _provenance(**overrides: Any) -> DecisionProvenance:
    base: dict[str, Any] = {
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "1",
        "prompt_version": "1",
        "tool_schema_version": "1",
        "decision_schema_version": "1",
        "evidence_bundle_id": "eb-1",
        "evidence_bundle_digest": "b" * 64,
        "produced_at": _NOW,
    }
    base.update(overrides)
    return DecisionProvenance(**base)


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
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
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
        "provenance": _provenance(),
    }
    base.update(overrides)
    return AITradeDecision(**base)


def _state(**overrides: Any) -> SupervisorState:
    base: dict[str, Any] = {"account_fingerprint": _FINGERPRINT, "now": _NOW}
    base.update(overrides)
    return SupervisorState(**base)


def _evidence(**overrides: Any) -> AccountEvidence:
    base: dict[str, Any] = {
        "net_liquidation_usd": Decimal(100_000),
        "total_cash_usd": Decimal(60_000),
        "buying_power_usd": Decimal(60_000),
    }
    base.update(overrides)
    return AccountEvidence(**base)


# ------------------------------------------------------------------- admission


def test_a_well_formed_decision_under_a_valid_mandate_is_admitted() -> None:
    outcome = admit(_decision(), _mandate(), _state())
    assert outcome.admitted is True
    assert outcome.refusal is None
    assert outcome.failed_checks == ()


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    [
        ("expired", {"expires_at": _NOW - timedelta(seconds=1)}, AdmissionRefusal.MANDATE_EXPIRED),
        (
            "not yet effective",
            {"effective_from": _NOW + timedelta(hours=1), "expires_at": _NOW + timedelta(days=2)},
            AdmissionRefusal.MANDATE_NOT_EFFECTIVE,
        ),
        (
            "wrong account",
            {"account_fingerprint": "b" * 64},
            AdmissionRefusal.ACCOUNT_MISMATCH,
        ),
        (
            "non-submitting mode",
            {"mode": AutonomyMode.SHADOW, "promotions": ()},
            AdmissionRefusal.MODE_CANNOT_SUBMIT,
        ),
    ],
)
def test_mandate_validation_refuses(label: str, kwargs: dict[str, Any], expected) -> None:
    outcome = admit(_decision(), _mandate(**kwargs), _state())
    assert outcome.admitted is False, label
    assert outcome.refusal is expected, label


def test_no_mandate_is_a_refusal_not_a_default() -> None:
    outcome = admit(_decision(), None, _state())
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.NO_ACTIVE_MANDATE


def test_degraded_state_refuses_before_anything_looks_like_a_green_light() -> None:
    """An AI failure -- or a broker/data/lease failure -- never becomes permission."""

    outcome = admit(_decision(), _mandate(), _state(degraded_reasons=("reconciliation stale",)))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.DEGRADED_STATE
    # And it refused early: nothing about scope or promotion was even reached.
    assert [check.name for check in outcome.checks][-1] == "system_not_degraded"


def test_replaying_an_admitted_decision_is_refused() -> None:
    """A model cannot route around a rejection by re-sending the same decision."""

    outcome = admit(_decision(), _mandate(), _state(admitted_decision_ids=frozenset({"d-1"})))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.DECISION_REPLAY


@pytest.mark.parametrize(
    "pin",
    ["provider", "model_id", "model_version", "prompt_version", "tool_schema_version"],
)
def test_an_unpinned_model_prompt_or_tool_schema_is_refused(pin: str) -> None:
    outcome = admit(_decision(provenance=_provenance(**{pin: "rogue"})), _mandate(), _state())
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.VERSION_PIN_MISMATCH


def test_a_decision_citing_an_unissued_evidence_bundle_is_refused() -> None:
    outcome = admit(_decision(), _mandate(), _state(expected_evidence_bundle_id="eb-2"))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH


def test_hold_is_recorded_and_produces_no_order() -> None:
    outcome = admit(
        _decision(kind=DecisionKind.HOLD, requested_strategy=None), _mandate(), _state()
    )
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.NOT_EXECUTABLE


def test_scope_refusals() -> None:
    off_allowlist = admit(_decision(symbol="TSLA"), _mandate(), _state())
    assert off_allowlist.refusal is AdmissionRefusal.INSTRUMENT_NOT_PERMITTED

    wrong_class = admit(
        _decision(asset_class=TradableAssetClass.CRYPTO, symbol="BTC"), _mandate(), _state()
    )
    assert wrong_class.refusal is AdmissionRefusal.ASSET_CLASS_NOT_PERMITTED

    wrong_strategy = admit(
        _decision(requested_strategy=StrategyForm.SHORT_EQUITY), _mandate(), _state()
    )
    assert wrong_strategy.refusal is AdmissionRefusal.STRATEGY_NOT_PERMITTED


def test_the_gateway_rechecks_promotion_instead_of_trusting_the_mandate() -> None:
    """Defence in depth: the gateway does not assume the mandate was validated.

    A well-formed mandate cannot omit a promotion for a scoped asset class — the
    contract validator refuses to construct one. But the gateway must not *rely*
    on that: a mandate rehydrated from storage, or a future strategy-level
    promotion model, could present one. ``model_construct`` bypasses validation
    exactly as a bad load would, and the gateway still refuses.
    """

    fields = dict(_mandate())
    fields["promotions"] = ()
    unvalidated = AutonomyMandate.model_construct(**fields)
    outcome = admit(_decision(), unvalidated, _state())
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.PROMOTION_INSUFFICIENT


def test_every_check_is_recorded_even_on_success() -> None:
    """A refusal must be explainable after the fact, so checks are always kept."""

    outcome = admit(_decision(), _mandate(), _state())
    names = [check.name for check in outcome.checks]
    assert "version_pins" in names
    assert "family_promoted" in names
    assert all(check.passed for check in outcome.checks)


# ---------------------------------------------------------------------- sizing


def test_the_models_requested_quantity_is_an_upper_bound_not_an_instruction() -> None:
    outcome = size_order(
        mandate=_mandate(),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(),
        requested_quantity=Decimal(10_000),  # far more than any limit allows
    )
    # max_order_notional_usd 10_000 / price 100 = 100 shares, and
    # max_shares_per_order is also 100.
    assert outcome.quantity == Decimal(100)
    assert outcome.was_clamped is True
    assert outcome.requested == Decimal(10_000)


def test_sizing_never_exceeds_what_the_model_asked_for() -> None:
    outcome = size_order(
        mandate=_mandate(),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(),
        requested_quantity=Decimal(7),
    )
    assert outcome.quantity == Decimal(7)
    assert outcome.was_clamped is False


def test_cash_floor_actually_reserves_capital() -> None:
    """A floor that is not subtracted is not a floor."""

    outcome = size_order(
        mandate=_mandate(
            capital=CapitalLimits(
                max_order_notional_usd=Decimal(1_000_000),
                min_cash_floor_usd=Decimal(950),
                min_buying_power_usd=Decimal(1),
            )
        ),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(total_cash_usd=Decimal(1_000), buying_power_usd=Decimal(1_000_000)),
        requested_quantity=Decimal(100),
    )
    # Only 50 USD is spendable above the floor: not even one share.
    assert outcome.quantity is None
    assert "no quantity survives" in outcome.refusal


def test_concentration_headroom_limits_size() -> None:
    outcome = size_order(
        mandate=_mandate(
            concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.10"))
        ),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(symbol_exposure_usd=Decimal(9_500)),
        requested_quantity=Decimal(100),
    )
    # 10% of 100_000 = 10_000 cap, 9_500 already held -> 500 headroom -> 5 shares.
    assert outcome.quantity == Decimal(5)


@pytest.mark.parametrize(
    ("price", "multiplier"),
    [(Decimal(0), Decimal(1)), (Decimal(-1), Decimal(1)), (Decimal(100), Decimal(0))],
)
def test_missing_or_absurd_contract_facts_refuse_rather_than_guess(
    price: Decimal, multiplier: Decimal
) -> None:
    outcome = size_order(
        mandate=_mandate(),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=price,
        multiplier=multiplier,
        evidence=_evidence(),
        requested_quantity=Decimal(10),
    )
    assert outcome.quantity is None
    assert outcome.refusal


def test_sizing_works_without_a_requested_quantity_at_all() -> None:
    """The kernel can size from mandate limits alone; the model need not ask."""

    outcome = size_order(
        mandate=_mandate(),
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(),
        requested_quantity=None,
    )
    assert outcome.quantity == Decimal(100)
    assert outcome.was_clamped is False


def test_option_contracts_use_the_contract_ceiling_and_multiplier() -> None:
    outcome = size_order(
        mandate=_mandate(
            scope=InstrumentScope(
                asset_classes=(TradableAssetClass.EQUITY_OPTION,),
                symbols=("SPY",),
                strategies=(StrategyForm.CASH_SECURED_PUT,),
                order_forms=(OrderForm.LIMIT,),
            ),
            promotions=(
                FamilyPromotion(
                    asset_class=TradableAssetClass.EQUITY_OPTION,
                    level=PromotionLevel.PAPER_AUTONOMOUS,
                ),
            ),
            capital=CapitalLimits(
                max_order_notional_usd=Decimal(50_000),
                max_contracts_per_order=3,
                min_cash_floor_usd=Decimal(1_000),
                min_buying_power_usd=Decimal(500),
            ),
        ),
        asset_class=TradableAssetClass.EQUITY_OPTION,
        reference_price=Decimal(5),
        multiplier=Decimal(100),
        evidence=_evidence(),
        requested_quantity=Decimal(50),
    )
    assert outcome.quantity == Decimal(3)  # max_contracts_per_order binds

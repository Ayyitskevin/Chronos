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

import inspect
from datetime import UTC, datetime, timedelta
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
    DecisionDirection,
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
    MAX_RESUBMISSIONS,
    AccountEvidence,
    AdmissionRefusal,
    MandateActivation,
    MarketDataEvidence,
    SupervisorState,
    admit,
    size_order,
)

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_TARGET_REF = "CHR-ORD-" + "A" * 32


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
            max_gross_exposure_usd=Decimal(500_000),
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
        "provenance": _provenance(),
    }
    base.update(overrides)
    return AITradeDecision(**base)


def _activation(**overrides: Any) -> MandateActivation:
    base: dict[str, Any] = {
        "owner_event_id": "owner-event-1",
        "activated_at": _NOW,
        "process_generation": 7,
    }
    base.update(overrides)
    return MandateActivation(**base)


def _market_data(**overrides: Any) -> MarketDataEvidence:
    base: dict[str, Any] = {
        "quote_age_seconds": Decimal(1),
        "quality": DataQuality.LIVE,
    }
    base.update(overrides)
    return MarketDataEvidence(**base)


def _state(**overrides: Any) -> SupervisorState:
    """A supervisor state with all evidence present, so scope checks are reachable.

    Admission is deny-by-default: absent activation, an unknown evidence bundle,
    or missing quote evidence each refuse on their own. Tests that want to reach
    a later check must therefore supply all of it.
    """

    base: dict[str, Any] = {
        "account_fingerprint": _FINGERPRINT,
        "now": _NOW,
        "activation": _activation(),
        "process_generation": 7,
        "expected_evidence_bundle_id": "eb-1",
        "expected_evidence_bundle_digest": "b" * 64,
        "market_data": _market_data(),
    }
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
    """Defence in depth for the promotion invariant **specifically**.

    A well-formed mandate cannot omit a promotion for a scoped asset class — the
    contract validator refuses to construct one. The gateway does not rely on
    that: ``model_construct`` bypasses validation exactly as a bad load would,
    and admission still refuses.

    Scope, stated precisely because the M2 review found the earlier docstring
    over-generalised: promotion, account, mode, effective window, scope,
    strategy, direction, and market data are all re-derived by admission. The
    live-duration cap, the permitted-data-quality restriction, the required
    ceilings and floors, and the FUTURE_OPTION refusal are still trusted to the
    contract validator alone — a mandate that bypassed validation could carry
    those violations past the gateway.
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


def test_every_exposure_creating_kind_must_name_a_permitted_strategy() -> None:
    """M2 review, HIGH: the strategy allowlist applied only to OPEN.

    A HEDGE, INCREASE, ROLL or REPLACE carrying no strategy was admitted with the
    check recorded as *passed*. That defeated the mitigation ADR-0016 §6
    publishes for shorting — "omit SHORT_EQUITY from scope" — because a
    SHORT-direction HEDGE never had a strategy compared against the scope at all.
    """

    for kind in (DecisionKind.HEDGE, DecisionKind.INCREASE, DecisionKind.ROLL):
        overrides: dict[str, Any] = {"kind": kind, "requested_strategy": None}
        if kind in (DecisionKind.INCREASE, DecisionKind.ROLL):
            overrides["target_client_reference"] = _TARGET_REF
        outcome = admit(_decision(**overrides), _mandate(), _state())
        assert outcome.admitted is False, kind
        assert outcome.refusal is AdmissionRefusal.STRATEGY_REQUIRED, kind


def test_the_reported_short_hedge_bypass_is_closed() -> None:
    """The exact reproduction from the M2 review."""

    outcome = admit(
        _decision(
            kind=DecisionKind.HEDGE,
            requested_strategy=None,
            direction=DecisionDirection.SHORT,
            requested_quantity=Decimal(99),
        ),
        _mandate(),  # scope.strategies is (LONG_EQUITY,) — SHORT_EQUITY omitted
        _state(),
    )
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.STRATEGY_REQUIRED


def test_a_short_direction_needs_an_explicitly_short_strategy() -> None:
    outcome = admit(
        _decision(direction=DecisionDirection.SHORT, requested_strategy=StrategyForm.LONG_EQUITY),
        _mandate(),
        _state(),
    )
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.DIRECTION_NOT_PERMITTED


def test_a_mandate_without_an_owner_activation_authorizes_nothing() -> None:
    """Authoring a mandate is not enabling it (ADR-0016 §4)."""

    outcome = admit(_decision(), _mandate(), _state(activation=None))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MANDATE_NOT_ACTIVATED


def test_a_revoked_mandate_is_refused() -> None:
    outcome = admit(_decision(), _mandate(), _state(activation=_activation(revoked=True)))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MANDATE_REVOKED


def test_restart_requires_reactivation_by_default() -> None:
    """RestartBehavior.REQUIRE_REACTIVATION is now enforced, not inert."""

    outcome = admit(_decision(), _mandate(), _state(activation=_activation(process_generation=6)))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MANDATE_NOT_ACTIVATED


def test_an_unknown_evidence_bundle_refuses_and_is_recorded_unevaluated() -> None:
    """M2 review, HIGH: this previously recorded a PASS when it had not run."""

    outcome = admit(_decision(), _mandate(), _state(expected_evidence_bundle_id=None))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_UNKNOWN
    bundle = next(c for c in outcome.checks if c.name == "evidence_bundle")
    assert bundle.passed is False
    assert bundle.evaluated is False


def test_a_forged_evidence_bundle_digest_is_refused() -> None:
    """The id matching is not enough: the digest must match too."""

    outcome = admit(
        _decision(provenance=_provenance(evidence_bundle_digest="f" * 64)),
        _mandate(),
        _state(),
    )
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH


def test_absent_market_data_refuses_rather_than_passing() -> None:
    outcome = admit(_decision(), _mandate(), _state(market_data=None))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MARKET_DATA_UNAVAILABLE


def test_stale_or_unpermitted_market_data_is_refused() -> None:
    stale = admit(
        _decision(), _mandate(), _state(market_data=_market_data(quote_age_seconds=Decimal(9)))
    )
    assert stale.refusal is AdmissionRefusal.MARKET_DATA_STALE
    wrong_quality = admit(
        _decision(),
        _mandate(),
        _state(market_data=_market_data(quality=DataQuality.DELAYED)),
    )
    assert wrong_quality.refusal is AdmissionRefusal.MARKET_DATA_STALE


def test_a_refused_decision_may_not_be_retried_forever() -> None:
    """R-31: repetition is not a way around a rejection."""

    outcome = admit(
        _decision(), _mandate(), _state(refused_decision_attempts={"d-1": MAX_RESUBMISSIONS})
    )
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.RESUBMISSION_EXHAUSTED


def test_no_check_is_ever_recorded_as_passed_without_being_evaluated() -> None:
    """Deny-by-default, stated as an invariant over the whole outcome."""

    for state in (
        _state(),
        _state(activation=None),
        _state(market_data=None),
        _state(expected_evidence_bundle_digest=None),
    ):
        outcome = admit(_decision(), _mandate(), state)
        for check in outcome.checks:
            if not check.evaluated:
                assert check.passed is False, check.name


def test_the_inert_mandate_limit_list_is_pinned() -> None:
    """A mandate field must be declared enforced or inert — it cannot just appear.

    The M2 review found four whole limit groups read by no code while the mandate
    docstring implied otherwise. This pins the honest list so adding a field
    forces a decision about it.
    """

    import chronos.supervisor.admission as admission_module
    import chronos.supervisor.sizing as sizing_module

    source = (admission_module.__doc__ or "") + inspect.getsource(sizing_module)

    inert_today = (
        "max_session_loss_usd",
        "max_daily_loss_usd",
        "max_peak_to_trough_drawdown_usd",
        "max_orders_per_session",
        "max_turnover_usd_per_session",
        "max_sector_exposure_pct",
        "max_family_exposure_pct",
        "max_correlated_exposure_pct",
        "max_leverage",
        "max_margin_utilization_pct",
    )
    for name in inert_today:
        assert name not in inspect.getsource(sizing_module), (
            f"{name} now appears enforced in sizing — update the disclosure in "
            "admission.py's docstring and this pin"
        )
    # The disclosure must actually name the inert groups.
    for phrase in ("LossLimits", "ActivityLimits", "scope.exchanges", "contract_families"):
        assert phrase in (admission_module.__doc__ or ""), (
            f"admission.py must disclose that {phrase} is not enforced"
        )
    assert source  # sanity


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
                allocated_capital_usd=Decimal(1_000_000),
                max_order_notional_usd=Decimal(1_000_000),
                max_gross_exposure_usd=Decimal(1_000_000),
                max_shares_per_order=1_000,
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


def test_a_zero_ceiling_authorizes_nothing_rather_than_everything() -> None:
    """Regression: zero ceilings must BIND at zero, not be skipped as "unset".

    The first version of `size_order` skipped any limit that was zero. That
    inverted deny-by-default exactly as the M1 review found for the floors: a
    mandate whose ceilings were all left at their zero defaults — one that
    authorizes nothing — sized to whatever cash allowed (590 shares in the
    reproduction). Caught by self-review before autonomy could consult it.

    The contract validator now refuses to *construct* such a mandate, so this
    uses `model_construct` to prove sizing itself is safe even if one arrived
    from storage or a future code path that skipped validation.
    """

    fields = dict(_mandate())
    fields["capital"] = CapitalLimits(
        min_cash_floor_usd=Decimal(1_000), min_buying_power_usd=Decimal(500)
    )
    fields["concentration"] = ConcentrationLimits()
    all_zero_ceilings = AutonomyMandate.model_construct(**fields)

    outcome = size_order(
        mandate=all_zero_ceilings,
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal(100),
        multiplier=Decimal(1),
        evidence=_evidence(),
        requested_quantity=None,
    )
    assert outcome.quantity is None, "a mandate authorizing nothing must size to nothing"
    assert "no quantity survives" in outcome.refusal


def test_a_submitting_mandate_must_state_its_ceilings() -> None:
    """The same defect, refused earlier: at authoring time rather than trade time."""

    for override in (
        {"allocated_capital_usd": Decimal(0)},
        {"max_order_notional_usd": Decimal(0)},
        {"max_gross_exposure_usd": Decimal(0)},
        {"max_shares_per_order": 0},
    ):
        fields: dict[str, Any] = {
            "allocated_capital_usd": Decimal(50_000),
            "max_order_notional_usd": Decimal(10_000),
            "max_gross_exposure_usd": Decimal(500_000),
            "max_shares_per_order": 100,
            "min_cash_floor_usd": Decimal(1_000),
            "min_buying_power_usd": Decimal(500),
        }
        fields.update(override)
        with pytest.raises(ValidationError):
            _mandate(capital=CapitalLimits(**fields))
    with pytest.raises(ValidationError):
        _mandate(concentration=ConcentrationLimits())


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
                allocated_capital_usd=Decimal(200_000),
                max_order_notional_usd=Decimal(50_000),
                max_gross_exposure_usd=Decimal(500_000),
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

"""Structural guarantees for controlled autonomous model authority (ADR-0016 / D-16).

D-11 forbade generative-model output from feeding any runtime decision, and ADR-0004 §5
conceded that the rule was enforced *by inspection* — no test asserted it. D-16 replaces
the prohibition with structure, so the structure has to be tested. This module is that
enforcement, and it is deliberately stricter than the rule it replaces:

1. **The model plane cannot reach a broker.** ``chronos.autonomy`` imports nothing from
   the order/broker/execution/risk/api/persistence planes — AST walk plus a subprocess
   ``sys.modules`` probe, the pattern already guarding the UI, ``histdata``, and the
   registry.
2. **A decision cannot express an order.** ``AITradeDecision`` and every model nested
   inside it carry no account, broker, routing, or transmit field, ``extra="forbid"``
   means one cannot be smuggled in, and a risk-reducing kind cannot carry a
   new-exposure payload.
3. **A mandate authorizes nothing by default, and never forever.** Frozen against
   ``model_copy(update=...)`` as well as assignment, expiring, deny-by-default,
   per-asset-family promoted, and required to set its floors explicitly.
4. **M1 adds no broker behavior.** Nothing outside the package imports it yet.

The M1 adversarial review found real holes in the first version of this file: the import
matcher was blind to ``from chronos import autonomy``, and the naked-short guarantee was a
substring check that a member named ``SHORT_CALL`` would have passed. Both are closed here.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

import chronos.autonomy as autonomy_pkg
from chronos.autonomy import (
    CONTEXT_DEPENDENT_DECISION_KINDS,
    DEFAULT_AUTONOMY_MODE,
    EXPOSURE_CREATING_DECISION_KINDS,
    LIVE_AUTONOMY_MODES,
    MAX_LIVE_MANDATE_DURATION,
    RISK_REDUCING_DECISION_KINDS,
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    DecisionDirection,
    DecisionKind,
    DecisionProvenance,
    EntryPlan,
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

# The model plane may not reach any of these, directly or transitively.
_FORBIDDEN = (
    "chronos.orders",
    "chronos.broker",
    "chronos.execution",
    "chronos.risk",
    "chronos.api",
    "chronos.persistence",
    "chronos.services",
    "chronos.control",
    "ib_async",
    "ibapi",
    "sqlalchemy",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src" / "chronos"

# Field names that would let a decision name an account, a broker object, a route,
# or the wire itself. None may appear anywhere in the decision contract tree.
_ORDER_CAPABLE_FIELD_NAMES = frozenset(
    {
        "account",
        "account_id",
        "account_fingerprint",
        "broker",
        "broker_order_id",
        "order_id",
        "perm_id",
        "permanent_id",
        "client_id",
        "transmit",
        "order_type",
        "order_ref",
        "exchange",
        "primary_exchange",
        "routing",
        "route",
        "con_id",
        "conid",
        "credentials",
        "api_key",
    }
)

_FIXED_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_TARGET_REF = "CHR-ORD-" + "A" * 32


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


def _provenance() -> DecisionProvenance:
    return DecisionProvenance(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
        produced_at=_FIXED_NOW,
    )


def _citation() -> EvidenceCitation:
    return EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_FIXED_NOW, digest="c" * 64)


def _decision(**overrides: Any) -> AITradeDecision:
    base: dict[str, Any] = {
        "decision_id": "d-1",
        "kind": DecisionKind.HOLD,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "provenance": _provenance(),
    }
    base.update(overrides)
    return AITradeDecision(**base)


def _open_decision(**overrides: Any) -> AITradeDecision:
    base: dict[str, Any] = {
        "kind": DecisionKind.OPEN,
        "evidence": (_citation(),),
        "invalidation_conditions": ("thesis breaks below 400",),
    }
    base.update(overrides)
    return _decision(**base)


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "mandate-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.SHADOW,
        "effective_from": _FIXED_NOW,
        "expires_at": _FIXED_NOW + timedelta(days=1),
        "versions": _pins(),
        "owner_authorization_ref": "owner-action-1",
        "authored_at": _FIXED_NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _live_scope() -> InstrumentScope:
    return InstrumentScope(
        asset_classes=(TradableAssetClass.EQUITY,),
        symbols=("SPY",),
        strategies=(StrategyForm.LONG_EQUITY,),
        order_forms=(OrderForm.LIMIT,),
    )


def _live_data() -> MarketDataRequirements:
    return MarketDataRequirements(
        max_quote_age_seconds=Decimal(5),
        permitted_data_qualities=(DataQuality.LIVE,),
    )


def _live_capital() -> CapitalLimits:
    return CapitalLimits(
        min_cash_floor_usd=Decimal(1000),
        min_buying_power_usd=Decimal(1000),
    )


def _submitting_mandate(mode: AutonomyMode, level: PromotionLevel, **overrides: Any):
    base: dict[str, Any] = {
        "mode": mode,
        "promotions": (FamilyPromotion(asset_class=TradableAssetClass.EQUITY, level=level),),
        "scope": _live_scope(),
        "market_data": _live_data(),
        "capital": _live_capital(),
    }
    base.update(overrides)
    return _mandate(**base)


def _autonomy_module_files() -> list[Path]:
    return sorted((_SRC / "autonomy").rglob("*.py"))


def _imported_names(source: str) -> list[str]:
    """Every module path an AST import can name.

    Emits both ``node.module`` and ``<module>.<alias>`` for ``ImportFrom``, because
    ``from chronos import autonomy`` names the subpackage in the *alias*, not the
    module — a blind spot the M1 adversarial review found in the original matcher,
    which silently defeated the isolation and milestone guards.
    """

    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _field_names(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    """Every field name in ``model`` and in every model nested inside it."""

    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name)
        annotation = field.annotation
        for candidate in (annotation, *get_args(annotation)):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate, seen)
            for inner in get_args(candidate):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    names |= _field_names(inner, seen)
    return names


# --------------------------------------------------------------------------- isolation


def test_autonomy_package_exists_and_is_scanned() -> None:
    names = {path.name for path in _autonomy_module_files()}
    assert {"decision.py", "mandate.py", "enums.py", "base.py", "__init__.py"} <= names


def test_import_matcher_sees_subpackage_aliases() -> None:
    """Guard the guard: the matcher must catch `from chronos import autonomy`."""

    found = _imported_names("from chronos import autonomy\n")
    assert "chronos.autonomy" in found


def test_autonomy_has_no_forbidden_ast_imports() -> None:
    for path in _autonomy_module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{path.name} imports forbidden module {name!r}"
                )


def test_importing_autonomy_leaks_no_broker_or_order_module() -> None:
    probe = (
        "import chronos.autonomy, sys; "
        f"bad=[m for m in sys.modules if m.startswith({_FORBIDDEN!r})]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], f"importing chronos.autonomy leaked forbidden modules: {leaked}"


def test_only_the_supervisor_consumes_the_contracts() -> None:
    """Exactly one plane may consume a model decision: the deterministic supervisor.

    This replaces M1's milestone guard (which asserted *nothing* imported the
    contracts, true only while the gateway did not exist). The permanent
    invariant is narrower and stronger: an `AITradeDecision` reaches the order
    pipeline through `chronos.supervisor` or not at all. If `chronos.orders`,
    `chronos.broker`, or the UI started reading decisions directly, the
    single-gateway guarantee in ADR-0016 §2 would be false.
    """

    permitted_prefixes = ("supervisor/",)
    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.is_relative_to(_SRC / "autonomy"):
            continue
        relative = str(path.relative_to(_SRC))
        if relative.startswith(permitted_prefixes):
            continue
        for name in _imported_names(path.read_text(encoding="utf-8")):
            if name == "chronos.autonomy" or name.startswith("chronos.autonomy."):
                importers.append(relative)
    assert importers == [], (
        f"only chronos.supervisor may consume model decisions, but {sorted(set(importers))} "
        "import the autonomy contracts"
    )


def test_the_supervisor_never_reaches_a_broker_directly() -> None:
    """The gateway decides; it does not transmit.

    The supervisor is allowed to know about orders (it hands off proposals) but
    must not import a broker adapter or the submission boundary — the transmit
    site stays reachable only through the existing order service.
    """

    forbidden = ("chronos.broker", "chronos.orders.submission", "ib_async", "ibapi")
    offenders: list[str] = []
    for path in sorted((_SRC / "supervisor").rglob("*.py")):
        for name in _imported_names(path.read_text(encoding="utf-8")):
            if any(name == item or name.startswith(item + ".") for item in forbidden):
                offenders.append(f"{path.name} imports {name}")
    assert offenders == [], f"the supervisor must not reach a broker: {offenders}"


# ------------------------------------------------------------ a decision cannot order


def test_decision_has_no_order_capable_field_anywhere() -> None:
    offending = _field_names(AITradeDecision) & _ORDER_CAPABLE_FIELD_NAMES
    assert offending == set(), f"AITradeDecision can express an order via {sorted(offending)}"


def test_decision_refuses_smuggled_fields() -> None:
    for smuggled in ({"transmit": True}, {"account_id": "U123456"}, {"broker_order_id": 7}):
        with pytest.raises(ValidationError):
            _decision(**smuggled)


def test_decision_may_not_name_a_broker_order_id() -> None:
    for bad in ("12345", "CHR-", "CHR-ORD-notahex", "CHR-ORD-" + "A" * 32 + "\r\nX"):
        with pytest.raises(ValidationError):
            _decision(kind=DecisionKind.CANCEL, target_client_reference=bad)


def test_decision_accepts_a_well_formed_chronos_reference() -> None:
    decision = _decision(kind=DecisionKind.CANCEL, target_client_reference=_TARGET_REF)
    assert decision.target_client_reference == _TARGET_REF


def test_targeted_decision_must_name_a_chronos_reference() -> None:
    with pytest.raises(ValidationError):
        _decision(kind=DecisionKind.CANCEL)


def test_exposure_creating_decision_requires_evidence_and_invalidation() -> None:
    with pytest.raises(ValidationError):
        _decision(kind=DecisionKind.OPEN, invalidation_conditions=("breaks 400",))
    with pytest.raises(ValidationError):
        _decision(kind=DecisionKind.OPEN, evidence=(_citation(),))
    assert _open_decision().kind is DecisionKind.OPEN


def test_risk_reducing_decision_may_not_carry_a_new_exposure_payload() -> None:
    """A CLOSE must not be able to wear an opening request's clothes."""

    for kind in (DecisionKind.CLOSE, DecisionKind.REDUCE, DecisionKind.CANCEL):
        with pytest.raises(ValidationError):
            _decision(
                kind=kind,
                target_client_reference=_TARGET_REF,
                requested_strategy=StrategyForm.LONG_EQUITY,
            )
        with pytest.raises(ValidationError):
            _decision(
                kind=kind,
                target_client_reference=_TARGET_REF,
                entry_plan=EntryPlan(valid_until=_FIXED_NOW),
            )
        with pytest.raises(ValidationError):
            _decision(
                kind=kind,
                target_client_reference=_TARGET_REF,
                requested_risk_budget_usd=Decimal(100),
            )


def test_hold_and_cancel_may_not_request_a_size() -> None:
    with pytest.raises(ValidationError):
        _decision(kind=DecisionKind.HOLD, requested_quantity=Decimal(1))
    with pytest.raises(ValidationError):
        _decision(
            kind=DecisionKind.CANCEL,
            target_client_reference=_TARGET_REF,
            requested_quantity=Decimal(1),
        )


def test_hold_may_not_express_a_direction() -> None:
    with pytest.raises(ValidationError):
        _decision(kind=DecisionKind.HOLD, direction=DecisionDirection.LONG)


def test_decision_numeric_requests_are_bounded() -> None:
    for field in ("requested_quantity", "requested_risk_budget_usd", "max_acceptable_loss_usd"):
        for bad in (Decimal("-1"), Decimal("0"), Decimal("1E-9"), Decimal(10) ** 12):
            with pytest.raises(ValidationError):
                _open_decision(**{field: bad})


def test_decision_instrument_alphabet_is_restricted() -> None:
    with pytest.raises(ValidationError):
        _decision(symbol="SPY@SMART/ISLAND")
    with pytest.raises(ValidationError):
        _decision(
            asset_class=TradableAssetClass.FUTURE, symbol="", futures_root="ES 20261218 GLOBEX"
        )


def test_decision_refuses_futures_options() -> None:
    """The decision-plane twin of the mandate's FUTURE_OPTION refusal."""

    with pytest.raises(ValidationError):
        _decision(asset_class=TradableAssetClass.FUTURE_OPTION, symbol="SPX")


def test_cancel_is_not_classified_risk_reducing_by_kind_alone() -> None:
    """Cancelling a *closing* order increases net risk (M1 review finding).

    The degraded-state rule (ADR-0016 §8) permits only risk-reducing behavior. If
    CANCEL were blanket-classified risk-reducing, a degraded system would be
    allowed to cancel exactly the protective/closing order it must not touch.
    """

    assert DecisionKind.CANCEL not in RISK_REDUCING_DECISION_KINDS
    assert DecisionKind.CANCEL in CONTEXT_DEPENDENT_DECISION_KINDS
    assert {
        DecisionKind.HOLD,
        DecisionKind.REDUCE,
        DecisionKind.CLOSE,
    } == RISK_REDUCING_DECISION_KINDS
    # Risk-reducing and exposure-creating stay disjoint.
    assert not (RISK_REDUCING_DECISION_KINDS & EXPOSURE_CREATING_DECISION_KINDS)


def test_narrative_fields_reject_control_characters() -> None:
    """Model-authored text reaches terminals, logs, and audit records."""

    for payload in ("bad\x00nul", "over\rwrite", "\x1b[31mred", "del\x7f"):
        with pytest.raises(ValidationError):
            _open_decision(thesis=payload)
        with pytest.raises(ValidationError):
            _open_decision(rationale=payload)
        with pytest.raises(ValidationError):
            _open_decision(invalidation_conditions=(payload,))
    # Newlines and tabs remain usable for genuine multi-line rationale.
    assert _open_decision(rationale="line one\n\tline two").rationale


def test_decision_evidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _open_decision(evidence=tuple(_citation() for _ in range(65)))


def test_decision_is_frozen_including_copy_with_update() -> None:
    decision = _open_decision()
    with pytest.raises(ValidationError):
        decision.symbol = "QQQ"  # type: ignore[misc]
    # A copy that drops the required evidence must not survive.
    with pytest.raises(ValidationError):
        decision.model_copy(update={"evidence": ()})


# ---------------------------------------------------------------- mandate guarantees


def test_mandate_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _mandate().mandate_id = "widened"  # type: ignore[misc]


def test_mandate_copy_with_update_cannot_escalate_authority() -> None:
    """model_copy(update=...) must re-run every validator (M1 review finding).

    Before this, a 1-day SHADOW mandate could be copied into a ten-year
    LIVE_AUTONOMOUS mandate with an empty scope and a SHADOW promotion rung.
    """

    mandate = _mandate()
    with pytest.raises(ValidationError):
        mandate.model_copy(update={"mode": AutonomyMode.LIVE_AUTONOMOUS})
    with pytest.raises(ValidationError):
        mandate.model_copy(
            update={
                "mode": AutonomyMode.LIVE_AUTONOMOUS,
                "expires_at": _FIXED_NOW + timedelta(days=3650),
            }
        )
    live = _submitting_mandate(AutonomyMode.LIVE_AUTONOMOUS, PromotionLevel.CAPPED_LIVE_AUTONOMOUS)
    with pytest.raises(ValidationError):  # expiry extension past the ceiling
        live.model_copy(update={"expires_at": _FIXED_NOW + timedelta(days=3650)})
    with pytest.raises(ValidationError):  # scope removal
        live.model_copy(update={"scope": InstrumentScope()})


def test_mandate_authorizes_nothing_by_default() -> None:
    mandate = _mandate()
    assert mandate.capital.allocated_capital_usd == 0
    assert mandate.capital.max_order_notional_usd == 0
    assert mandate.scope.symbols == ()
    assert mandate.scope.asset_classes == ()
    assert mandate.scope.order_forms == ()
    assert mandate.promotions == ()
    assert mandate.sessions.allow_overnight_holding is False


def test_mandate_must_expire_after_it_starts() -> None:
    for expiry in (_FIXED_NOW, _FIXED_NOW - timedelta(seconds=1)):
        with pytest.raises(ValidationError):
            _mandate(expires_at=expiry)


def test_mandate_window_is_a_time_predicate_only() -> None:
    mandate = _mandate()
    assert mandate.covers_instant(_FIXED_NOW) is True
    assert mandate.covers_instant(_FIXED_NOW + timedelta(days=2)) is False


def test_live_mandate_may_not_outlive_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        _submitting_mandate(
            AutonomyMode.LIVE_AUTONOMOUS,
            PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
            expires_at=_FIXED_NOW + MAX_LIVE_MANDATE_DURATION + timedelta(days=1),
        )
    within = _submitting_mandate(
        AutonomyMode.LIVE_AUTONOMOUS,
        PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
        expires_at=_FIXED_NOW + MAX_LIVE_MANDATE_DURATION,
    )
    assert within.mode in LIVE_AUTONOMY_MODES


def test_promotion_is_per_asset_family() -> None:
    """A stock promotion must not authorize futures (ADR-0016 §7)."""

    scope = InstrumentScope(
        asset_classes=(TradableAssetClass.EQUITY, TradableAssetClass.FUTURE),
        symbols=("SPY",),
        futures_roots=("MES",),
        strategies=(StrategyForm.LONG_EQUITY, StrategyForm.LONG_FUTURE),
        order_forms=(OrderForm.LIMIT,),
    )
    with pytest.raises(ValidationError):  # FUTURE has no promotion record
        _mandate(
            mode=AutonomyMode.PAPER_AUTONOMOUS,
            promotions=(
                FamilyPromotion(
                    asset_class=TradableAssetClass.EQUITY,
                    level=PromotionLevel.PAPER_AUTONOMOUS,
                ),
            ),
            scope=scope,
            market_data=_live_data(),
            capital=_live_capital(),
        )
    with pytest.raises(ValidationError):  # FUTURE promoted, but not far enough
        _mandate(
            mode=AutonomyMode.PAPER_AUTONOMOUS,
            promotions=(
                FamilyPromotion(
                    asset_class=TradableAssetClass.EQUITY,
                    level=PromotionLevel.PAPER_AUTONOMOUS,
                ),
                FamilyPromotion(asset_class=TradableAssetClass.FUTURE, level=PromotionLevel.SHADOW),
            ),
            scope=scope,
            market_data=_live_data(),
            capital=_live_capital(),
        )


def test_mandate_mode_may_not_exceed_the_family_rung() -> None:
    with pytest.raises(ValidationError):
        _submitting_mandate(AutonomyMode.LIVE_AUTONOMOUS, PromotionLevel.PAPER_AUTONOMOUS)


def test_submitting_mandate_must_state_its_scope_explicitly() -> None:
    with pytest.raises(ValidationError):  # silence is never a grant
        _mandate(mode=AutonomyMode.PAPER_AUTONOMOUS)


def test_submitting_mandate_must_set_its_floors() -> None:
    """A zero floor is the *most* permissive value, not deny-by-default."""

    with pytest.raises(ValidationError):  # no quote-age floor
        _submitting_mandate(
            AutonomyMode.PAPER_AUTONOMOUS,
            PromotionLevel.PAPER_AUTONOMOUS,
            market_data=MarketDataRequirements(permitted_data_qualities=(DataQuality.LIVE,)),
        )
    with pytest.raises(ValidationError):  # no cash / buying-power floor
        _submitting_mandate(
            AutonomyMode.PAPER_AUTONOMOUS,
            PromotionLevel.PAPER_AUTONOMOUS,
            capital=CapitalLimits(),
        )


def test_live_mandate_may_not_license_non_live_data() -> None:
    for quality in (DataQuality.FROZEN, DataQuality.DELAYED_FROZEN, DataQuality.DEMO):
        with pytest.raises(ValidationError):
            _submitting_mandate(
                AutonomyMode.LIVE_AUTONOMOUS,
                PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
                market_data=MarketDataRequirements(
                    max_quote_age_seconds=Decimal(5), permitted_data_qualities=(quality,)
                ),
            )


def test_no_mandate_may_license_known_bad_data() -> None:
    for quality in (DataQuality.STALE, DataQuality.UNKNOWN):
        with pytest.raises(ValidationError):
            MarketDataRequirements(permitted_data_qualities=(quality,))


def test_mandate_scope_cross_validates_strategies_against_asset_classes() -> None:
    with pytest.raises(ValidationError):  # futures strategy, no futures class
        InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_FUTURE,),
            order_forms=(OrderForm.LIMIT,),
        )
    with pytest.raises(ValidationError):  # futures roots, no futures class
        InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            futures_roots=("MES",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        )


def test_mandate_scope_rejects_routing_syntax_in_symbols() -> None:
    with pytest.raises(ValidationError):
        InstrumentScope(asset_classes=(TradableAssetClass.EQUITY,), symbols=("SPY@SMART/ISLAND",))


def test_futures_options_scope_is_refused() -> None:
    with pytest.raises(ValidationError):  # recognized vocabulary, refused in code
        InstrumentScope(asset_classes=(TradableAssetClass.FUTURE_OPTION,))


def test_mandate_requires_a_pseudonymous_account_scope() -> None:
    with pytest.raises(ValidationError):
        _mandate(account_fingerprint="U1234567")


def test_promotion_for_reports_the_family_rung() -> None:
    mandate = _submitting_mandate(AutonomyMode.PAPER_AUTONOMOUS, PromotionLevel.PAPER_AUTONOMOUS)
    assert mandate.promotion_for(TradableAssetClass.EQUITY) is PromotionLevel.PAPER_AUTONOMOUS
    assert mandate.promotion_for(TradableAssetClass.FUTURE) is None


# ------------------------------------------------------------- vocabulary guarantees


def test_strategy_vocabulary_is_pinned_and_has_no_uncovered_short_option() -> None:
    """Pinned to the exact member set, not a substring scan.

    A substring check for "NAKED"/"UNCOVERED" would pass a member named
    ``SHORT_CALL`` or ``SHORT_PUT`` — the realistic regression (M1 review).
    """

    assert {member.value for member in StrategyForm} == {
        "LONG_EQUITY",
        "SHORT_EQUITY",
        "CASH_SECURED_PUT",
        "COVERED_CALL",
        "LONG_CALL",
        "LONG_PUT",
        "VERTICAL_DEBIT_SPREAD",
        "VERTICAL_CREDIT_SPREAD",
        "LONG_FUTURE",
        "SHORT_FUTURE",
    }


def test_no_market_order_form_is_expressible() -> None:
    assert {member.value for member in OrderForm} == {"LIMIT", "MARKETABLE_LIMIT"}


def test_startup_default_mode_is_not_live() -> None:
    assert DEFAULT_AUTONOMY_MODE not in LIVE_AUTONOMY_MODES
    assert autonomy_pkg.DEFAULT_AUTONOMY_MODE is AutonomyMode.SHADOW

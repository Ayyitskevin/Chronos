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
   inside it carry no account, broker, routing, or transmit field, and ``extra="forbid"``
   means one cannot be smuggled in. This is ADR-0004 §1's technique — make the dangerous
   thing unrepresentable — applied to the model's output.
3. **A mandate authorizes nothing by default, and never forever.** Frozen, expiring,
   deny-by-default, and unable to claim more authority than its promotion rung earned.
4. **M1 adds no broker behavior.** Nothing outside the package imports it yet.
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
    DEFAULT_AUTONOMY_MODE,
    LIVE_AUTONOMY_MODES,
    MAX_LIVE_MANDATE_DURATION,
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    DecisionKind,
    DecisionProvenance,
    EvidenceCitation,
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


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "mandate-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.SHADOW,
        "promotion_level": PromotionLevel.SHADOW,
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


def _autonomy_module_files() -> list[Path]:
    return sorted((_SRC / "autonomy").rglob("*.py"))


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
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
    files = _autonomy_module_files()
    names = {path.name for path in files}
    assert {"decision.py", "mandate.py", "enums.py", "__init__.py"} <= names


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


def test_m1_wires_the_contracts_into_no_runtime_path() -> None:
    """M1 adds no broker behavior: nothing outside the package imports it yet.

    ADR-0016's milestone sequencing puts the ModelDecisionGateway in M2. When that
    lands, the supervisor legitimately imports these contracts and this test is
    replaced by the gateway's own admission tests — it is a milestone guard, not a
    permanent invariant.
    """

    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.is_relative_to(_SRC / "autonomy"):
            continue
        for name in _imported_names(path.read_text(encoding="utf-8")):
            if name == "chronos.autonomy" or name.startswith("chronos.autonomy."):
                importers.append(str(path.relative_to(_SRC)))
    assert importers == [], f"M1 must add no broker behavior, but {importers} import the contracts"


# ------------------------------------------------------------ a decision cannot order


def test_decision_has_no_order_capable_field_anywhere() -> None:
    offending = _field_names(AITradeDecision) & _ORDER_CAPABLE_FIELD_NAMES
    assert offending == set(), f"AITradeDecision can express an order via {sorted(offending)}"


def test_decision_refuses_smuggled_fields() -> None:
    for smuggled in ({"transmit": True}, {"account_id": "U123456"}, {"broker_order_id": 7}):
        with pytest.raises(ValidationError):
            AITradeDecision(
                decision_id="d-1",
                kind=DecisionKind.HOLD,
                asset_class=TradableAssetClass.EQUITY,
                symbol="SPY",
                provenance=_provenance(),
                **smuggled,
            )


def test_decision_may_not_name_a_broker_order_id() -> None:
    with pytest.raises(ValidationError):
        AITradeDecision(
            decision_id="d-1",
            kind=DecisionKind.CANCEL,
            asset_class=TradableAssetClass.EQUITY,
            symbol="SPY",
            provenance=_provenance(),
            target_client_reference="12345",
        )


def test_targeted_decision_must_name_a_chronos_reference() -> None:
    with pytest.raises(ValidationError):
        AITradeDecision(
            decision_id="d-1",
            kind=DecisionKind.CANCEL,
            asset_class=TradableAssetClass.EQUITY,
            symbol="SPY",
            provenance=_provenance(),
        )


def test_exposure_creating_decision_requires_evidence_and_invalidation() -> None:
    common: dict[str, Any] = {
        "decision_id": "d-1",
        "kind": DecisionKind.OPEN,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "provenance": _provenance(),
    }
    with pytest.raises(ValidationError):  # no evidence
        AITradeDecision(**common, invalidation_conditions=("breaks 400",))
    with pytest.raises(ValidationError):  # no invalidation conditions
        AITradeDecision(**common, evidence=(_citation(),))
    # Both present: accepted.
    decision = AITradeDecision(
        **common, evidence=(_citation(),), invalidation_conditions=("breaks 400",)
    )
    assert decision.kind is DecisionKind.OPEN


def test_hold_decision_may_not_request_a_size() -> None:
    with pytest.raises(ValidationError):
        AITradeDecision(
            decision_id="d-1",
            kind=DecisionKind.HOLD,
            asset_class=TradableAssetClass.EQUITY,
            symbol="SPY",
            provenance=_provenance(),
            requested_quantity=Decimal(1),
        )


def test_decision_is_frozen() -> None:
    decision = AITradeDecision(
        decision_id="d-1",
        kind=DecisionKind.HOLD,
        asset_class=TradableAssetClass.EQUITY,
        symbol="SPY",
        provenance=_provenance(),
    )
    with pytest.raises(ValidationError):
        decision.symbol = "QQQ"  # type: ignore[misc]


# ---------------------------------------------------------------- mandate guarantees


def test_mandate_is_frozen() -> None:
    mandate = _mandate()
    with pytest.raises(ValidationError):
        mandate.mandate_id = "widened"  # type: ignore[misc]


def test_mandate_authorizes_nothing_by_default() -> None:
    mandate = _mandate()
    assert mandate.capital == CapitalLimits()
    assert mandate.capital.allocated_capital_usd == 0
    assert mandate.capital.max_order_notional_usd == 0
    assert mandate.scope.symbols == ()
    assert mandate.scope.asset_classes == ()
    assert mandate.scope.order_forms == ()
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
    common: dict[str, Any] = {
        "mode": AutonomyMode.LIVE_AUTONOMOUS,
        "promotion_level": PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
        "scope": _live_scope(),
        "market_data": MarketDataRequirements(permitted_data_qualities=("LIVE",)),
    }
    with pytest.raises(ValidationError):
        _mandate(**common, expires_at=_FIXED_NOW + MAX_LIVE_MANDATE_DURATION + timedelta(days=1))
    within = _mandate(**common, expires_at=_FIXED_NOW + MAX_LIVE_MANDATE_DURATION)
    assert within.mode in LIVE_AUTONOMY_MODES


def test_mandate_mode_may_not_exceed_its_promotion_rung() -> None:
    with pytest.raises(ValidationError):
        _mandate(
            mode=AutonomyMode.LIVE_AUTONOMOUS,
            promotion_level=PromotionLevel.PAPER_AUTONOMOUS,
            scope=_live_scope(),
            market_data=MarketDataRequirements(permitted_data_qualities=("LIVE",)),
        )


def test_submitting_mandate_must_state_its_scope_explicitly() -> None:
    with pytest.raises(ValidationError):  # silence is never a grant
        _mandate(
            mode=AutonomyMode.PAPER_AUTONOMOUS,
            promotion_level=PromotionLevel.PAPER_AUTONOMOUS,
        )


def test_futures_scope_requires_a_root_and_futures_options_are_refused() -> None:
    with pytest.raises(ValidationError):
        _mandate(
            mode=AutonomyMode.PAPER_AUTONOMOUS,
            promotion_level=PromotionLevel.PAPER_AUTONOMOUS,
            scope=InstrumentScope(
                asset_classes=(TradableAssetClass.FUTURE,),
                strategies=(StrategyForm.LONG_FUTURE,),
                order_forms=(OrderForm.LIMIT,),
            ),
            market_data=MarketDataRequirements(permitted_data_qualities=("LIVE",)),
        )
    with pytest.raises(ValidationError):  # recognized vocabulary, refused in code
        InstrumentScope(asset_classes=(TradableAssetClass.FUTURE_OPTION,))


def test_mandate_may_not_license_trading_on_known_bad_data() -> None:
    for quality in (DataQuality.STALE, DataQuality.UNKNOWN):
        with pytest.raises(ValidationError):
            MarketDataRequirements(permitted_data_qualities=(quality,))


def test_mandate_requires_a_pseudonymous_account_scope() -> None:
    with pytest.raises(ValidationError):
        _mandate(account_fingerprint="U1234567")


# ------------------------------------------------------------- vocabulary guarantees


def test_no_naked_short_option_strategy_is_expressible() -> None:
    for member in StrategyForm:
        assert "NAKED" not in member.value
        assert "UNCOVERED" not in member.value


def test_no_market_order_form_is_expressible() -> None:
    assert "MARKET" not in OrderForm.__members__
    assert {member.value for member in OrderForm} == {"LIMIT", "MARKETABLE_LIMIT"}


def test_startup_default_mode_is_not_live() -> None:
    assert DEFAULT_AUTONOMY_MODE not in LIVE_AUTONOMY_MODES
    assert autonomy_pkg.DEFAULT_AUTONOMY_MODE is AutonomyMode.SHADOW

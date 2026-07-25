"""Deterministic admission of a model decision against an owner mandate (ADR-0016 §2).

This is the first half of the ModelDecisionGateway: given an
:class:`~chronos.autonomy.decision.AITradeDecision` and the
:class:`~chronos.autonomy.mandate.AutonomyMandate` in force, decide whether the
decision may proceed at all. It is **pure** — no broker, no database, no clock of
its own — so every refusal is reproducible from its inputs.

Three properties are deliberate:

- **Deny by default.** Admission requires every check to PASS. A check that
  cannot be evaluated is a refusal, never a pass; there is no default-allow
  branch and no "unknown means fine".
- **The supervisor binds the mandate, not the decision.** The decision does not
  name a mandate (`chronos.autonomy.decision` explains why), so the caller
  supplies the mandate in force. A model therefore cannot select the authority
  it is judged against.
- **Provenance is checked, not trusted.** The decision's `DecisionProvenance` is
  stamped by the deterministic queue writer, and admission compares it against
  the mandate's `VersionPins`. A decision from an unpinned model, prompt, tool
  schema, or decision schema is refused outright rather than downgraded.

Admission never sizes, never resolves a contract, and never submits. Sizing is
:mod:`chronos.supervisor.sizing`; submission remains the existing
`chronos.orders` boundary, which is unchanged and still the only transmit site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chronos.autonomy import (
    LIVE_AUTONOMY_MODES,
    MINIMUM_PROMOTION_FOR_MODE,
    SUBMITTING_AUTONOMY_MODES,
    AITradeDecision,
    AutonomyMandate,
    DecisionKind,
    TradableAssetClass,
    promotion_rank,
)


class AdmissionRefusal(StrEnum):
    """Why a decision was refused. One code per independent failure mode."""

    NO_ACTIVE_MANDATE = "NO_ACTIVE_MANDATE"
    MANDATE_NOT_EFFECTIVE = "MANDATE_NOT_EFFECTIVE"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MODE_CANNOT_SUBMIT = "MODE_CANNOT_SUBMIT"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    VERSION_PIN_MISMATCH = "VERSION_PIN_MISMATCH"
    EVIDENCE_BUNDLE_MISMATCH = "EVIDENCE_BUNDLE_MISMATCH"
    DECISION_REPLAY = "DECISION_REPLAY"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    ASSET_CLASS_NOT_PERMITTED = "ASSET_CLASS_NOT_PERMITTED"
    INSTRUMENT_NOT_PERMITTED = "INSTRUMENT_NOT_PERMITTED"
    STRATEGY_NOT_PERMITTED = "STRATEGY_NOT_PERMITTED"
    STRATEGY_REQUIRED = "STRATEGY_REQUIRED"
    PROMOTION_INSUFFICIENT = "PROMOTION_INSUFFICIENT"
    NO_ORDER_FORM_PERMITTED = "NO_ORDER_FORM_PERMITTED"
    DEGRADED_STATE = "DEGRADED_STATE"


@dataclass(frozen=True, slots=True)
class AdmissionCheck:
    """One named check and its verdict, recorded whether it passed or not."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """The result of admitting one decision. ``admitted`` is never a default."""

    admitted: bool
    checks: tuple[AdmissionCheck, ...]
    refusal: AdmissionRefusal | None = None
    detail: str = ""

    @property
    def failed_checks(self) -> tuple[AdmissionCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class SupervisorState:
    """Deterministic state the admission gate needs from the rest of the system.

    Every field is evidence the *supervisor* gathered, never anything the model
    asserted. ``degraded_reasons`` carries ADR-0016 §8's degraded-state rule: if
    the broker, market data, clock, database, lease, resolver, risk engine, or
    reconciliation state is unavailable, ambiguous, stale, or inconsistent, the
    caller records why here and no new exposure is admitted.
    """

    account_fingerprint: str
    now: datetime
    degraded_reasons: tuple[str, ...] = ()
    #: Decision ids already admitted; replay protection (ADR-0016 §8).
    admitted_decision_ids: frozenset[str] = frozenset()
    #: Evidence bundle the supervisor actually handed the model, if known.
    expected_evidence_bundle_id: str | None = None


def _fail(
    checks: list[AdmissionCheck],
    name: str,
    refusal: AdmissionRefusal,
    detail: str,
) -> AdmissionOutcome:
    checks.append(AdmissionCheck(name=name, passed=False, detail=detail))
    return AdmissionOutcome(admitted=False, checks=tuple(checks), refusal=refusal, detail=detail)


def _ok(checks: list[AdmissionCheck], name: str, detail: str = "") -> None:
    checks.append(AdmissionCheck(name=name, passed=True, detail=detail))


def admit(
    decision: AITradeDecision,
    mandate: AutonomyMandate | None,
    state: SupervisorState,
) -> AdmissionOutcome:
    """Decide whether ``decision`` may proceed under ``mandate``.

    Returns an outcome, never raises for a policy failure — a refusal is data
    the supervisor records and the owner can read, not an exception to catch.
    """

    checks: list[AdmissionCheck] = []

    # 1. There must be a mandate at all. Absence is refusal, not permission.
    if mandate is None:
        return _fail(
            checks,
            "active_mandate",
            AdmissionRefusal.NO_ACTIVE_MANDATE,
            "no AutonomyMandate is in force; the model has no trade-time authority",
        )
    _ok(checks, "active_mandate", f"mandate {mandate.mandate_id} v{mandate.mandate_version}")

    # 2. Degraded state: an AI failure never becomes permission to trade, and
    #    neither does a broker/data/lease failure. Refuse before anything else
    #    that might look like a green light.
    if state.degraded_reasons:
        return _fail(
            checks,
            "system_not_degraded",
            AdmissionRefusal.DEGRADED_STATE,
            "system state is degraded, so no new exposure may be created: "
            + "; ".join(state.degraded_reasons),
        )
    _ok(checks, "system_not_degraded")

    # 3. Effective window.
    if state.now < mandate.effective_from:
        return _fail(
            checks,
            "mandate_effective",
            AdmissionRefusal.MANDATE_NOT_EFFECTIVE,
            f"mandate becomes effective at {mandate.effective_from.isoformat()}",
        )
    if state.now >= mandate.expires_at:
        return _fail(
            checks,
            "mandate_effective",
            AdmissionRefusal.MANDATE_EXPIRED,
            f"mandate expired at {mandate.expires_at.isoformat()}; renewal is an owner action",
        )
    _ok(checks, "mandate_effective")

    # 4. Account scope. Compared on the pseudonymous fingerprint, never a raw id.
    if mandate.account_fingerprint != state.account_fingerprint:
        return _fail(
            checks,
            "account_scope",
            AdmissionRefusal.ACCOUNT_MISMATCH,
            "the mandate authorizes a different account than the connected one",
        )
    _ok(checks, "account_scope")

    # 5. The mode must be one that may submit at all.
    if mandate.mode not in SUBMITTING_AUTONOMY_MODES:
        return _fail(
            checks,
            "mode_may_submit",
            AdmissionRefusal.MODE_CANNOT_SUBMIT,
            f"mandate mode {mandate.mode.value} does not authorize submission",
        )
    _ok(checks, "mode_may_submit", mandate.mode.value)

    # 6. Replay protection: one decision id is admitted at most once, so a model
    #    cannot route around a rejection by re-sending the same decision.
    if decision.decision_id in state.admitted_decision_ids:
        return _fail(
            checks,
            "not_a_replay",
            AdmissionRefusal.DECISION_REPLAY,
            f"decision {decision.decision_id} was already admitted",
        )
    _ok(checks, "not_a_replay")

    # 7. Version pins: an unpinned model/prompt/tool/schema is refused outright.
    pins = mandate.versions
    provenance = decision.provenance
    mismatches = [
        f"{label}: mandate pins {pinned!r}, decision carries {actual!r}"
        for label, pinned, actual in (
            ("provider", pins.provider, provenance.provider),
            ("model_id", pins.model_id, provenance.model_id),
            ("model_version", pins.model_version, provenance.model_version),
            ("prompt_version", pins.prompt_version, provenance.prompt_version),
            ("tool_schema_version", pins.tool_schema_version, provenance.tool_schema_version),
            (
                "decision_schema_version",
                pins.decision_schema_version,
                provenance.decision_schema_version,
            ),
        )
        if pinned != actual
    ]
    if mismatches:
        return _fail(
            checks,
            "version_pins",
            AdmissionRefusal.VERSION_PIN_MISMATCH,
            "; ".join(mismatches),
        )
    _ok(checks, "version_pins")

    # 8. Evidence bundle identity, when the supervisor knows which one it issued.
    expected_bundle = state.expected_evidence_bundle_id
    if expected_bundle is not None and provenance.evidence_bundle_id != expected_bundle:
        return _fail(
            checks,
            "evidence_bundle",
            AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH,
            "the decision cites an evidence bundle the supervisor did not issue for this run",
        )
    _ok(checks, "evidence_bundle")

    # 9. HOLD is a valid decision and an explicitly non-executable one: it is
    #    recorded, and nothing is compiled or sent.
    if decision.kind is DecisionKind.HOLD:
        return _fail(
            checks,
            "executable_kind",
            AdmissionRefusal.NOT_EXECUTABLE,
            "HOLD is recorded and intentionally produces no order",
        )
    _ok(checks, "executable_kind", decision.kind.value)

    # 10. Instrument scope.
    scope = mandate.scope
    if decision.asset_class not in scope.asset_classes:
        return _fail(
            checks,
            "asset_class_permitted",
            AdmissionRefusal.ASSET_CLASS_NOT_PERMITTED,
            f"{decision.asset_class.value} is not in the mandate's permitted asset classes",
        )
    _ok(checks, "asset_class_permitted", decision.asset_class.value)

    if decision.asset_class is TradableAssetClass.FUTURE:
        permitted_instrument = decision.futures_root in scope.futures_roots
        instrument = decision.futures_root
    else:
        permitted_instrument = decision.symbol in scope.symbols
        instrument = decision.symbol
    if not permitted_instrument:
        return _fail(
            checks,
            "instrument_permitted",
            AdmissionRefusal.INSTRUMENT_NOT_PERMITTED,
            f"{instrument!r} is not on the mandate's allowlist",
        )
    _ok(checks, "instrument_permitted", instrument)

    # 11. Strategy scope. An exposure-creating decision must name a permitted
    #     strategy; a reducing one inherits the position's existing structure.
    if decision.requested_strategy is not None:
        if decision.requested_strategy not in scope.strategies:
            return _fail(
                checks,
                "strategy_permitted",
                AdmissionRefusal.STRATEGY_NOT_PERMITTED,
                f"{decision.requested_strategy.value} is not permitted by this mandate",
            )
        _ok(checks, "strategy_permitted", decision.requested_strategy.value)
    elif decision.kind is DecisionKind.OPEN:
        return _fail(
            checks,
            "strategy_permitted",
            AdmissionRefusal.STRATEGY_REQUIRED,
            "an OPEN decision must name the strategy it intends",
        )
    else:
        _ok(checks, "strategy_permitted", "not required for this kind")

    # 12. Per-family promotion: this family must itself have earned this mode.
    earned = mandate.promotion_for(decision.asset_class)
    minimum = MINIMUM_PROMOTION_FOR_MODE[mandate.mode]
    if earned is None or promotion_rank(earned) < promotion_rank(minimum):
        return _fail(
            checks,
            "family_promoted",
            AdmissionRefusal.PROMOTION_INSUFFICIENT,
            f"{decision.asset_class.value} is promoted to "
            f"{earned.value if earned else 'nothing'}, below {minimum.value} required by "
            f"mode {mandate.mode.value}",
        )
    _ok(checks, "family_promoted", earned.value)

    # 13. There must be a permitted order form for the compiler to select.
    if not scope.order_forms:
        return _fail(
            checks,
            "order_form_available",
            AdmissionRefusal.NO_ORDER_FORM_PERMITTED,
            "the mandate permits no order form",
        )
    _ok(checks, "order_form_available", ", ".join(form.value for form in scope.order_forms))

    live = mandate.mode in LIVE_AUTONOMY_MODES
    return AdmissionOutcome(
        admitted=True,
        checks=tuple(checks),
        detail=("live" if live else "paper") + " autonomous admission",
    )

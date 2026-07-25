"""Deterministic admission of a model decision against an owner mandate (ADR-0016 §2).

This is the first half of the ModelDecisionGateway: given an
:class:`~chronos.autonomy.decision.AITradeDecision` and the
:class:`~chronos.autonomy.mandate.AutonomyMandate` in force, decide whether the
decision may proceed at all. It is **pure** — no broker, no database, no clock of
its own — so every refusal is reproducible from its inputs.

Three properties are deliberate:

- **Deny by default.** Admission requires every check to PASS. A check whose
  evidence is absent is a refusal, never a pass. Where a check genuinely cannot
  be evaluated yet it is recorded with ``evaluated=False`` and ``passed=False``,
  so an unevaluated check can never be mistaken for a satisfied one.
- **The supervisor binds the mandate, not the decision.** The decision does not
  name a mandate (`chronos.autonomy.decision` explains why), so the caller
  supplies the mandate in force. A model therefore cannot select the authority
  it is judged against.
- **Provenance is compared, not trusted.** Admission compares the decision's
  ``DecisionProvenance`` against the mandate's ``VersionPins``. See the honest
  bound below: today that proves *agreement*, not *authorship*.

## What admission does NOT enforce (read this before relying on it)

The M2 adversarial review found that several mandate limit groups are read by no
code, while the mandate docstring implied the supervisor re-derived them all.
That is corrected here, and the honest list is kept in one place:

- **Not enforced anywhere, pending a supervisor that owns durable per-session
  state (M3):** ``LossLimits`` in full (session/daily loss, peak-to-trough
  drawdown) and ``ActivityLimits`` in full (orders, cancellations, replacements,
  turnover per session). ``SupervisorState.degraded_reasons`` is the only
  interim lever — a caller that has breached a loss or activity limit must pass
  a degraded reason. A breached loss limit should normally be reported with
  ``blocks_risk_reduction=False``, so the position can still be closed: that is
  what a loss limit is *for*.
- **Not enforced anywhere, pending contract compilation (M2 follow-on):**
  ``scope.exchanges`` and ``scope.contract_families``. A decision names no
  exchange by construction, so these can only be checked against a *qualified*
  contract, and no compilation step exists yet.
- **Not enforced by admission; deferred to the orders-plane risk engine and the
  ten-gate live stack, which run on every proposal:** margin, buying power,
  cash-secured collateral, and market-open session checks.
- **Partially enforced:** of ``ConcentrationLimits`` only
  ``max_symbol_exposure_pct`` binds (in :mod:`chronos.supervisor.sizing`);
  sector, family and correlated exposure do not. Of ``CapitalLimits``,
  ``max_leverage`` and ``max_margin_utilization_pct`` do not bind here.

``tests/safety/test_supervisor_gateway.py`` pins this list, so a mandate field
cannot be added without declaring whether it is enforced or inert.

**Honest bound on provenance.** ``DecisionProvenance`` is intended to be stamped
by the deterministic decision-queue writer. That writer does not exist yet (M4),
so today nothing *enforces* authorship: the version-pin check proves the decision
agrees with the mandate's pins, not that an approved model produced it. Until the
queue writer lands, treat the pin check as an integrity check, not authentication.

Admission never sizes, never resolves a contract, and never submits. Sizing is
:mod:`chronos.supervisor.sizing`; submission remains the existing
`chronos.orders` boundary, which is unchanged and still the only transmit site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from chronos.autonomy import (
    EXPOSURE_CREATING_DECISION_KINDS,
    MINIMUM_PROMOTION_FOR_MODE,
    RISK_REDUCING_DECISION_KINDS,
    SUBMITTING_AUTONOMY_MODES,
    AITradeDecision,
    AutonomyMandate,
    DecisionDirection,
    DecisionKind,
    StrategyForm,
    TradableAssetClass,
    promotion_rank,
)
from chronos.domain.enums import DataQuality

#: Strategies that can express short exposure in the underlying. Used to refuse a
#: SHORT-direction decision whose named strategy cannot be short — the hole the
#: M2 review found, where a SHORT HEDGE slipped past a mandate that deliberately
#: omitted SHORT_EQUITY from its scope.
_SHORT_CAPABLE_STRATEGIES: frozenset[StrategyForm] = frozenset(
    {StrategyForm.SHORT_EQUITY, StrategyForm.SHORT_FUTURE}
)

#: Asset classes where DecisionDirection maps directly onto underlying exposure.
#: Option strategies express a view through structure, so a SHORT direction there
#: is not by itself a short position and is not refused on that basis.
_DIRECTIONAL_ASSET_CLASSES: frozenset[TradableAssetClass] = frozenset(
    {TradableAssetClass.EQUITY, TradableAssetClass.FUTURE, TradableAssetClass.CRYPTO}
)

#: Market-data qualities admission will accept as current evidence.
_ACCEPTABLE_QUALITIES: frozenset[DataQuality] = frozenset(
    {DataQuality.LIVE, DataQuality.DELAYED, DataQuality.FROZEN, DataQuality.DEMO}
)

#: How many times one economic decision may be re-submitted after refusal before
#: the supervisor stops considering it at all (ADR-0016 §2 / R-31).
MAX_RESUBMISSIONS = 3


class AdmissionRefusal(StrEnum):
    """Why a decision was refused. One code per independent failure mode."""

    NO_ACTIVE_MANDATE = "NO_ACTIVE_MANDATE"
    MANDATE_NOT_ACTIVATED = "MANDATE_NOT_ACTIVATED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_NOT_EFFECTIVE = "MANDATE_NOT_EFFECTIVE"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MODE_CANNOT_SUBMIT = "MODE_CANNOT_SUBMIT"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    VERSION_PIN_MISMATCH = "VERSION_PIN_MISMATCH"
    EVIDENCE_BUNDLE_MISMATCH = "EVIDENCE_BUNDLE_MISMATCH"
    EVIDENCE_BUNDLE_UNKNOWN = "EVIDENCE_BUNDLE_UNKNOWN"
    DECISION_REPLAY = "DECISION_REPLAY"
    RESUBMISSION_EXHAUSTED = "RESUBMISSION_EXHAUSTED"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    ASSET_CLASS_NOT_PERMITTED = "ASSET_CLASS_NOT_PERMITTED"
    INSTRUMENT_NOT_PERMITTED = "INSTRUMENT_NOT_PERMITTED"
    STRATEGY_NOT_PERMITTED = "STRATEGY_NOT_PERMITTED"
    STRATEGY_REQUIRED = "STRATEGY_REQUIRED"
    DIRECTION_NOT_PERMITTED = "DIRECTION_NOT_PERMITTED"
    PROMOTION_INSUFFICIENT = "PROMOTION_INSUFFICIENT"
    NO_ORDER_FORM_PERMITTED = "NO_ORDER_FORM_PERMITTED"
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    DEGRADED_STATE = "DEGRADED_STATE"
    DEGRADED_RISK_REDUCTION_ONLY = "DEGRADED_RISK_REDUCTION_ONLY"


@dataclass(frozen=True, slots=True)
class AdmissionCheck:
    """One named check and its verdict.

    ``evaluated`` distinguishes "ran and passed" from "could not be run". An
    unevaluated check always has ``passed=False``: deny-by-default means silence
    is never a pass.
    """

    name: str
    passed: bool
    detail: str = ""
    evaluated: bool = True


@dataclass(frozen=True, slots=True)
class MarketDataEvidence:
    """Quote facts the supervisor observed. Never model-supplied."""

    quote_age_seconds: Decimal
    quality: DataQuality
    relative_spread: Decimal | None = None
    option_volume: int | None = None
    open_interest: int | None = None


@dataclass(frozen=True, slots=True)
class DegradedReason:
    """One subsystem that is unavailable, ambiguous, stale, or inconsistent.

    ADR-0016 §8's degraded-state rule has two halves, and the M2 review found
    only the first implemented: *create no new exposure*, **and** *permit
    deterministic risk-reducing behavior*. Refusing a CLOSE because the system
    is degraded is not conservative — it traps existing exposure at exactly the
    moment the operator most wants out.

    Which half applies depends on the subsystem, so the caller must say:

    - A stale quote feed or an unreachable model stops new exposure while
      leaving position truth intact, so closing is still safe:
      ``blocks_risk_reduction=False``.
    - Unreconciled positions, a lost lease, or an unreachable broker mean we do
      not know what we hold. "Closing" a position we are wrong about *opens* the
      opposite one, so nothing may proceed: ``blocks_risk_reduction=True``.

    The field defaults to ``True`` deliberately. A caller that has not thought
    about which kind of degradation this is gets the strictest behavior, and
    permitting risk reduction is an explicit, reviewable act.
    """

    subsystem: str
    detail: str = ""
    blocks_risk_reduction: bool = True

    def __str__(self) -> str:
        return f"{self.subsystem}: {self.detail}" if self.detail else self.subsystem


@dataclass(frozen=True, slots=True)
class MandateActivation:
    """The authenticated owner event that enabled this mandate.

    A mandate record is an authored document; being *in force* is a separate,
    revocable act (ADR-0016 §4). Admission refuses unless the supervisor can
    show the activation, so a mandate found in storage cannot authorize anything
    on its own.
    """

    owner_event_id: str
    activated_at: datetime
    revoked: bool = False
    #: The process generation this activation was made in. When
    #: ``RestartBehavior.REQUIRE_REACTIVATION`` applies, a mismatch refuses.
    process_generation: int = 0


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

    @property
    def unevaluated_checks(self) -> tuple[AdmissionCheck, ...]:
        return tuple(check for check in self.checks if not check.evaluated)


@dataclass(frozen=True, slots=True)
class SupervisorState:
    """Deterministic state the admission gate needs from the rest of the system.

    Every field is evidence the *supervisor* gathered, never anything the model
    asserted. ``degraded_reasons`` carries ADR-0016 §8's degraded-state rule: if
    the broker, market data, clock, database, lease, resolver, risk engine, or
    reconciliation state is unavailable, ambiguous, stale, or inconsistent — or a
    loss/activity limit has been breached, which admission does not itself track
    — the caller records why here and no new exposure is admitted. Each reason
    also declares whether risk-reducing decisions may still proceed; see
    :class:`DegradedReason`.
    """

    account_fingerprint: str
    now: datetime
    #: The owner event that put the mandate in force. Absent => refuse.
    activation: MandateActivation | None = None
    #: Current process generation, compared against the activation's.
    process_generation: int = 0
    degraded_reasons: tuple[DegradedReason, ...] = ()
    #: Decision ids already admitted; replay protection (ADR-0016 §8).
    admitted_decision_ids: frozenset[str] = frozenset()
    #: Decision ids already refused, with how many times each was re-submitted.
    #: A refusal that is retried indefinitely is itself an attack (R-31).
    refused_decision_attempts: dict[str, int] = field(default_factory=dict)
    #: Evidence bundle the supervisor issued for this run, and its digest.
    expected_evidence_bundle_id: str | None = None
    expected_evidence_bundle_digest: str | None = None
    #: Fresh quote evidence for the decision's instrument.
    market_data: MarketDataEvidence | None = None


def _fail(
    checks: list[AdmissionCheck],
    name: str,
    refusal: AdmissionRefusal,
    detail: str,
    *,
    evaluated: bool = True,
) -> AdmissionOutcome:
    checks.append(AdmissionCheck(name=name, passed=False, detail=detail, evaluated=evaluated))
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
    #    that might look like a green light — but permit deterministic risk
    #    reduction where the degradation still leaves position truth intact.
    outcome = _check_degraded(checks, decision, state)
    if outcome is not None:
        return outcome

    # 3. Activation and revocation. Authoring a mandate is not enabling it.
    outcome = _check_activation(checks, mandate, state)
    if outcome is not None:
        return outcome

    # 4. Effective window.
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

    # 5. Account scope. Compared on the pseudonymous fingerprint, never a raw id.
    if mandate.account_fingerprint != state.account_fingerprint:
        return _fail(
            checks,
            "account_scope",
            AdmissionRefusal.ACCOUNT_MISMATCH,
            "the mandate authorizes a different account than the connected one",
        )
    _ok(checks, "account_scope")

    # 6. The mode must be one that may submit at all.
    if mandate.mode not in SUBMITTING_AUTONOMY_MODES:
        return _fail(
            checks,
            "mode_may_submit",
            AdmissionRefusal.MODE_CANNOT_SUBMIT,
            f"mandate mode {mandate.mode.value} does not authorize submission",
        )
    _ok(checks, "mode_may_submit", mandate.mode.value)

    # 7. Replay and re-submission. A model cannot route around a rejection by
    #    re-sending, nor by retrying a refused decision indefinitely.
    outcome = _check_replay(checks, decision, state)
    if outcome is not None:
        return outcome

    # 8. Version pins: an unpinned model/prompt/tool/schema is refused outright.
    outcome = _check_version_pins(checks, decision, mandate)
    if outcome is not None:
        return outcome

    # 9. Evidence bundle identity AND digest. Both must be known and match.
    outcome = _check_evidence_bundle(checks, decision, state)
    if outcome is not None:
        return outcome

    # 10. HOLD is a valid decision and an explicitly non-executable one: it is
    #     recorded, and nothing is compiled or sent.
    if decision.kind is DecisionKind.HOLD:
        return _fail(
            checks,
            "executable_kind",
            AdmissionRefusal.NOT_EXECUTABLE,
            "HOLD is recorded and intentionally produces no order",
        )
    _ok(checks, "executable_kind", decision.kind.value)

    # 11. Instrument scope.
    outcome = _check_instrument(checks, decision, mandate)
    if outcome is not None:
        return outcome

    # 12. Strategy and direction scope.
    outcome = _check_strategy(checks, decision, mandate)
    if outcome is not None:
        return outcome

    # 13. Per-family promotion: this family must itself have earned this mode.
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

    # 14. There must be a permitted order form for the compiler to select.
    if not mandate.scope.order_forms:
        return _fail(
            checks,
            "order_form_available",
            AdmissionRefusal.NO_ORDER_FORM_PERMITTED,
            "the mandate permits no order form",
        )
    _ok(
        checks,
        "order_form_available",
        ", ".join(form.value for form in mandate.scope.order_forms),
    )

    # 15. Market-data freshness and liquidity, from supervisor evidence.
    outcome = _check_market_data(checks, mandate, state)
    if outcome is not None:
        return outcome

    return AdmissionOutcome(
        admitted=True,
        checks=tuple(checks),
        detail=f"{mandate.mode.value} admission for {decision.kind.value}",
    )


def _check_degraded(
    checks: list[AdmissionCheck], decision: AITradeDecision, state: SupervisorState
) -> AdmissionOutcome | None:
    """ADR-0016 §8's degraded-state rule, both halves.

    "Create no new exposure, permit only deterministic risk-reducing behavior."
    The M2 review found only the first half implemented: any degraded reason
    refused every decision kind, so a degraded system could not be unwound
    through the gateway at all.

    Precedence is deliberate. A single blocking reason overrides every
    non-blocking one, because the question is not "how many subsystems are
    unhealthy" but "do we know what we hold" — and one subsystem that leaves
    that unknown is enough.
    """

    if not state.degraded_reasons:
        _ok(checks, "system_not_degraded")
        return None

    blocking = tuple(reason for reason in state.degraded_reasons if reason.blocks_risk_reduction)
    if blocking:
        return _fail(
            checks,
            "system_not_degraded",
            AdmissionRefusal.DEGRADED_STATE,
            "system state is degraded in a way that leaves position truth unknown, so "
            "nothing may proceed — not even a close, which could open the opposite "
            "position if we are wrong about what we hold: "
            + "; ".join(str(reason) for reason in blocking),
        )

    # Every reason permits risk reduction. CANCEL is deliberately excluded:
    # it is CONTEXT_DEPENDENT, because cancelling a *closing* order increases
    # net risk, and admission cannot tell which kind of order is targeted.
    if decision.kind not in RISK_REDUCING_DECISION_KINDS:
        return _fail(
            checks,
            "system_not_degraded",
            AdmissionRefusal.DEGRADED_RISK_REDUCTION_ONLY,
            f"system state is degraded, so only risk-reducing decisions may proceed and "
            f"{decision.kind.value} is not one: "
            + "; ".join(str(reason) for reason in state.degraded_reasons),
        )
    _ok(
        checks,
        "system_not_degraded",
        "degraded; narrowed to risk reduction only: "
        + "; ".join(str(reason) for reason in state.degraded_reasons),
    )
    return None


def _check_activation(
    checks: list[AdmissionCheck], mandate: AutonomyMandate, state: SupervisorState
) -> AdmissionOutcome | None:
    activation = state.activation
    if activation is None:
        return _fail(
            checks,
            "mandate_activated",
            AdmissionRefusal.MANDATE_NOT_ACTIVATED,
            "no owner activation event accompanies this mandate; authoring a mandate "
            "does not put it in force",
        )
    if activation.revoked:
        return _fail(
            checks,
            "mandate_activated",
            AdmissionRefusal.MANDATE_REVOKED,
            f"mandate authority was revoked (owner event {activation.owner_event_id})",
        )
    from chronos.autonomy import RestartBehavior

    if (
        mandate.restart_behavior is RestartBehavior.REQUIRE_REACTIVATION
        and activation.process_generation != state.process_generation
    ):
        return _fail(
            checks,
            "mandate_activated",
            AdmissionRefusal.MANDATE_NOT_ACTIVATED,
            "this mandate requires reactivation after a restart; its activation belongs "
            f"to process generation {activation.process_generation}, not "
            f"{state.process_generation}",
        )
    _ok(checks, "mandate_activated", f"owner event {activation.owner_event_id}")
    return None


def _check_replay(
    checks: list[AdmissionCheck], decision: AITradeDecision, state: SupervisorState
) -> AdmissionOutcome | None:
    if decision.decision_id in state.admitted_decision_ids:
        return _fail(
            checks,
            "not_a_replay",
            AdmissionRefusal.DECISION_REPLAY,
            f"decision {decision.decision_id} was already admitted",
        )
    attempts = state.refused_decision_attempts.get(decision.decision_id, 0)
    if attempts >= MAX_RESUBMISSIONS:
        return _fail(
            checks,
            "not_a_replay",
            AdmissionRefusal.RESUBMISSION_EXHAUSTED,
            f"decision {decision.decision_id} was refused {attempts} times; a refusal "
            "may not be routed around by repetition",
        )
    _ok(checks, "not_a_replay", f"{attempts} prior refusals")
    return None


def _check_version_pins(
    checks: list[AdmissionCheck], decision: AITradeDecision, mandate: AutonomyMandate
) -> AdmissionOutcome | None:
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
            ("policy_version", pins.policy_version, provenance.policy_version),
        )
        if pinned != actual
    ]
    if mismatches:
        return _fail(
            checks, "version_pins", AdmissionRefusal.VERSION_PIN_MISMATCH, "; ".join(mismatches)
        )
    # Honest label: this proves agreement with the pins, not authorship, until
    # the deterministic decision-queue writer stamps provenance (M4).
    _ok(checks, "version_pins", "agrees with mandate pins (authorship not yet enforced)")
    return None


def _check_evidence_bundle(
    checks: list[AdmissionCheck], decision: AITradeDecision, state: SupervisorState
) -> AdmissionOutcome | None:
    expected_id = state.expected_evidence_bundle_id
    expected_digest = state.expected_evidence_bundle_digest
    if expected_id is None or expected_digest is None:
        # Deny-by-default: an unknown bundle is refused, and recorded as
        # unevaluated so it can never read as a satisfied check.
        return _fail(
            checks,
            "evidence_bundle",
            AdmissionRefusal.EVIDENCE_BUNDLE_UNKNOWN,
            "the supervisor cannot say which evidence bundle it issued for this run, "
            "so the decision's citations cannot be bound to it",
            evaluated=False,
        )
    provenance = decision.provenance
    if provenance.evidence_bundle_id != expected_id:
        return _fail(
            checks,
            "evidence_bundle",
            AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH,
            "the decision cites an evidence bundle the supervisor did not issue for this run",
        )
    if provenance.evidence_bundle_digest != expected_digest:
        return _fail(
            checks,
            "evidence_bundle",
            AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH,
            "the decision's evidence-bundle digest does not match the bundle the "
            "supervisor issued; the evidence it reasoned from is not the evidence on record",
        )
    _ok(checks, "evidence_bundle", expected_id)
    return None


def _check_instrument(
    checks: list[AdmissionCheck], decision: AITradeDecision, mandate: AutonomyMandate
) -> AdmissionOutcome | None:
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
        permitted = decision.futures_root in scope.futures_roots
        instrument = decision.futures_root
    else:
        permitted = decision.symbol in scope.symbols
        instrument = decision.symbol
    if not permitted:
        return _fail(
            checks,
            "instrument_permitted",
            AdmissionRefusal.INSTRUMENT_NOT_PERMITTED,
            f"{instrument!r} is not on the mandate's allowlist",
        )
    _ok(checks, "instrument_permitted", instrument)
    return None


def _check_strategy(
    checks: list[AdmissionCheck], decision: AITradeDecision, mandate: AutonomyMandate
) -> AdmissionOutcome | None:
    """Every exposure-creating decision must name a permitted strategy.

    The M2 review found this check applied only to OPEN, so a HEDGE, INCREASE,
    ROLL or REPLACE carrying no strategy was admitted with the check recorded as
    passed. That defeated the mitigation ADR-0016 §6 publishes for shorting —
    "omit SHORT_EQUITY from scope" — because a SHORT-direction HEDGE never had
    its strategy compared against the scope at all.
    """

    strategy = decision.requested_strategy
    if strategy is None:
        if decision.kind in EXPOSURE_CREATING_DECISION_KINDS:
            return _fail(
                checks,
                "strategy_permitted",
                AdmissionRefusal.STRATEGY_REQUIRED,
                f"a {decision.kind.value} decision creates or extends exposure and must "
                "name the strategy it intends, so it can be checked against the mandate",
            )
        _ok(checks, "strategy_permitted", "no strategy requested by a risk-reducing decision")
        return None

    if strategy not in mandate.scope.strategies:
        return _fail(
            checks,
            "strategy_permitted",
            AdmissionRefusal.STRATEGY_NOT_PERMITTED,
            f"{strategy.value} is not permitted by this mandate",
        )
    _ok(checks, "strategy_permitted", strategy.value)

    if (
        decision.direction is DecisionDirection.SHORT
        and decision.asset_class in _DIRECTIONAL_ASSET_CLASSES
        and strategy not in _SHORT_CAPABLE_STRATEGIES
    ):
        return _fail(
            checks,
            "direction_permitted",
            AdmissionRefusal.DIRECTION_NOT_PERMITTED,
            f"a SHORT {decision.asset_class.value} decision cannot be expressed by "
            f"{strategy.value}; short exposure requires an explicitly short strategy",
        )
    _ok(checks, "direction_permitted", decision.direction.value)
    return None


def _check_market_data(
    checks: list[AdmissionCheck], mandate: AutonomyMandate, state: SupervisorState
) -> AdmissionOutcome | None:
    requirements = mandate.market_data
    evidence = state.market_data
    if evidence is None:
        return _fail(
            checks,
            "market_data_fresh",
            AdmissionRefusal.MARKET_DATA_UNAVAILABLE,
            "no market-data evidence accompanies this decision; stale-data rejection is "
            "a deterministic guarantee and absence is refusal",
            evaluated=False,
        )
    if evidence.quality not in requirements.permitted_data_qualities:
        return _fail(
            checks,
            "market_data_fresh",
            AdmissionRefusal.MARKET_DATA_STALE,
            f"quote quality {evidence.quality.value} is not permitted by this mandate",
        )
    if evidence.quality not in _ACCEPTABLE_QUALITIES:
        return _fail(
            checks,
            "market_data_fresh",
            AdmissionRefusal.MARKET_DATA_STALE,
            f"quote quality {evidence.quality.value} is never acceptable evidence",
        )
    if evidence.quote_age_seconds > requirements.max_quote_age_seconds:
        return _fail(
            checks,
            "market_data_fresh",
            AdmissionRefusal.MARKET_DATA_STALE,
            f"quote is {evidence.quote_age_seconds}s old, older than the mandate's "
            f"{requirements.max_quote_age_seconds}s freshness floor",
        )
    if (
        requirements.max_relative_spread > 0
        and evidence.relative_spread is not None
        and evidence.relative_spread > requirements.max_relative_spread
    ):
        return _fail(
            checks,
            "market_data_fresh",
            AdmissionRefusal.MARKET_DATA_STALE,
            f"relative spread {evidence.relative_spread} exceeds the mandate's "
            f"{requirements.max_relative_spread}",
        )
    _ok(checks, "market_data_fresh", f"{evidence.quality.value} @ {evidence.quote_age_seconds}s")
    return None

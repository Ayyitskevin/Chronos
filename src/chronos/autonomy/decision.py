"""The AITradeDecision: the only shape a model's output may take to reach runtime.

ADR-0016 / DECISIONS.md D-16. Free-form chat, theses, summaries, and Markdown
are **never** parsed into orders; only a validated instance of
:class:`AITradeDecision` may enter the pipeline, and only through the single
deterministic ModelDecisionGateway (Milestone 2). This module defines the
contract shape only — no parsing of prose, no admission, no broker behavior.

What makes this type safe is what it cannot say:

- **No account, no broker, no wire.** There is no account id, account
  fingerprint, broker order id, permanent id, client id, exchange routing,
  ``transmit`` flag, or order-type field. A decision is structurally incapable
  of expressing a broker order, exactly as ``StrategyProposal`` is in the
  deterministic plane (ADR-0004 §1, which ADR-0016 preserves).
- **No self-selected authority.** A decision does not name a mandate. The
  supervisor binds each decision to the mandate in force, so a model can never
  choose the authority it is judged against.
- **Requests, not instructions.** ``requested_quantity`` and
  ``requested_risk_budget_usd`` are *requests*. Deterministic code independently
  resolves and qualifies the contract, computes and clamps the final quantity,
  selects a permitted order form, and may reduce or refuse outright. The
  kernel's veto is unconditional and a refusal may not be routed around.
- **Payload must match intent.** A risk-reducing decision cannot smuggle a
  new-exposure request: REDUCE/CLOSE/CANCEL/HOLD may not carry a strategy or an
  entry plan, and CANCEL/HOLD may not carry a size at all.
- **Narrative is narrative.** ``thesis``, ``rationale``, ``key_uncertainties``,
  and ``invalidation_conditions`` are recorded, displayed, and audited. They
  carry concise, decision-relevant reasoning — deliberately **not** hidden model
  chain-of-thought, which Chronos neither requests nor persists (ADR-0016 §5).
  Nothing in the runtime pipeline parses them into an order parameter. That is
  enforced by ``tests/safety/test_autonomy_contracts.py``: only
  ``chronos.supervisor`` may import these contracts
  (``test_only_the_supervisor_consumes_the_contracts``), and the supervisor
  reads none of the narrative attributes. The M1 milestone guard this used to
  cite — ``test_m1_wires_the_contracts_into_no_runtime_path`` — was correctly
  retired when M2 gave the contracts their first consumer; the M2 review found
  this reference still pointing at it.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.autonomy.base import AutonomyModel
from chronos.autonomy.enums import (
    EXPOSURE_CREATING_DECISION_KINDS,
    TARGETED_DECISION_KINDS,
    DecisionDirection,
    DecisionKind,
    PriceReference,
    StrategyForm,
    TimeHorizon,
    TradableAssetClass,
    TriggerComparator,
)

#: Chronos-owned reference shape. A decision may only ever name a Chronos
#: correlation reference drawn from its EvidenceBundle — never a broker order
#: id, which is the broker's namespace and not the model's to speak. The
#: pattern matches what ``chronos.utils.identifiers.new_correlation_id`` emits.
_CHRONOS_REFERENCE_PATTERN = r"^CHR-[A-Z0-9]+-[0-9A-F]{32}$"

# Numeric bounds mirroring the order plane's Numeric(20,8) persistence scale.
# They are restated here rather than imported: `chronos.autonomy` deliberately
# imports nothing from `chronos.orders` (ADR-0016 §3, asserted by an isolation
# test), so the model plane cannot reach the submission path even transitively.
_MAX_REQUESTED_QUANTITY = Decimal(10) ** 12
_MIN_QUANTITY_EXPONENT = -8

_ASSET_CLASSES_USING_SYMBOL = frozenset(
    {
        TradableAssetClass.EQUITY,
        TradableAssetClass.EQUITY_OPTION,
        TradableAssetClass.INDEX_OPTION,
        TradableAssetClass.CRYPTO,
    }
)

_SYMBOL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
_ROOT_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

#: Kinds that may never carry a size request: they either do nothing (HOLD) or
#: act on an existing order without resizing it (CANCEL).
_SIZELESS_KINDS = frozenset({DecisionKind.HOLD, DecisionKind.CANCEL})

#: Kinds that may never carry a strategy or an entry plan — anything that only
#: removes or holds exposure has no entry to describe.
_NO_ENTRY_KINDS = frozenset(
    {DecisionKind.HOLD, DecisionKind.REDUCE, DecisionKind.CLOSE, DecisionKind.CANCEL}
)


def _reject_control_characters(value: str, label: str) -> str:
    """Refuse control characters, ANSI escapes, and NUL in model-authored text.

    These fields are model-authored and end up in terminals, logs, audit
    records, and a dashboard. A NUL can truncate a C-side consumer, CR can
    overwrite a log line, and an ANSI escape can repaint an operator's terminal
    — so a decision could forge what a human sees while reviewing it. Newlines
    and tabs stay allowed because real rationale needs them (M1 review).
    """

    forbidden = {
        character for character in value if ord(character) < 32 and character not in "\n\t"
    }
    if forbidden or "\x7f" in value or "\x1b" in value:
        raise ValueError(f"{label} may not contain control characters or escape sequences")
    return value


def _validate_hex_digest(value: str, label: str) -> str:
    normalized = value.strip().lower()
    non_hex = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or non_hex:
        raise ValueError(f"{label} must be a 64-character lowercase hex digest")
    return normalized


def _validate_bounded_amount(value: Decimal, label: str) -> Decimal:
    """Positive, finite, and inside the Numeric(20,8) persistence envelope."""

    if not value.is_finite():
        raise ValueError(f"{label} must be a finite number")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < _MIN_QUANTITY_EXPONENT:
        raise ValueError(f"{label} is finer than the 1e-8 persistence scale")
    if value >= _MAX_REQUESTED_QUANTITY:
        raise ValueError(f"{label} exceeds the Numeric(20,8) persistence magnitude")
    return value


class DecisionProvenance(AutonomyModel):
    """Which model, prompt, tools, schema, and evidence produced this decision.

    **Ownership matters here.** These fields are stamped by the deterministic
    decision-queue writer from harness-held configuration — the process that
    actually loaded the prompt and called the provider. They are *not* a model
    self-report: a model asked to describe its own version could simply claim a
    pinned one, which would make the mandate's :class:`VersionPins` check a
    self-attestation rather than a control. The supervisor checks these against
    the mandate before admission and refuses a decision whose pins disagree.

    **Status after M4: authentication, not merely agreement.** The writer that
    stamps these fields exists — :func:`chronos.supervisor.queue.accept`. A
    model authors a :class:`ProposedDecision`, which has no provenance field at
    all, and the writer attaches provenance from configuration the model process
    never sees. So the pin check now proves an approved model produced the
    decision, rather than proving the decision merely *agrees* with the pins.

    (M2 and M3 disclosed the weaker claim honestly while the writer did not
    exist. This is the promised upgrade, not a re-description of the old one.)
    """

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    tool_schema_version: str = Field(min_length=1, max_length=64)
    decision_schema_version: str = Field(min_length=1, max_length=64)
    #: The policy revision in force when this decision was produced. The M2
    #: review found ``VersionPins.policy_version`` had no counterpart here and
    #: was therefore compared against nothing — a pin the mandate could set and
    #: no decision could ever violate. A decision produced under a policy the
    #: mandate does not pin is now refused like any other version mismatch.
    policy_version: str = Field(min_length=1, max_length=64)
    #: Which registered proposer's credential authenticated the submission
    #: (ADR-0023). Stamped by the writer from the route's *verified* match —
    #: never from the payload. Empty means the pre-registry static identity,
    #: kept as a distinguishable value rather than a plausible name.
    proposer_id: str = Field(default="", max_length=64)
    #: The immutable, versioned, redacted bundle the model was given.
    evidence_bundle_id: str = Field(min_length=1, max_length=128)
    #: ``None`` is the honest placeholder-era value (ADR-0023): no bundle
    #: machinery exists yet, and sixty-four zeros read as a computed digest
    #: that was never computed. Admission requires the claim to match the
    #: supervisor's attestation exactly, ``None`` included.
    evidence_bundle_digest: str | None = None
    produced_at: AwareDatetime

    @field_validator("evidence_bundle_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex_digest(value, "evidence_bundle_digest")


class EvidenceCitation(AutonomyModel):
    """One citation into the EvidenceBundle backing this decision."""

    evidence_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    as_of: AwareDatetime
    digest: str
    excerpt: str = Field(default="", max_length=1000)

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _validate_hex_digest(value, "digest")


class PriceTrigger(AutonomyModel):
    """A typed price condition.

    Conditions are structured rather than prose precisely so the kernel never
    has to parse language to act. A trigger is an input to deterministic
    compilation, not an order price.
    """

    comparator: TriggerComparator
    reference: PriceReference
    value: Decimal = Field(gt=0)

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: Decimal) -> Decimal:
        return _validate_bounded_amount(value, "trigger value")


class EntryPlan(AutonomyModel):
    """Conditions under which the decision intends an entry to become live."""

    trigger: PriceTrigger | None = None
    valid_until: AwareDatetime | None = None


class ExitPlan(AutonomyModel):
    """Intended exits, including any protective requirement the model asks for."""

    profit_target: PriceTrigger | None = None
    protective_stop: PriceTrigger | None = None
    time_exit: AwareDatetime | None = None


class ProposedDecision(AutonomyModel):
    """Everything a model may author. Deliberately **not** a decision yet.

    M4 splits the contract in two, and the split is the control.

    ``AITradeDecision`` carries ``decision_id`` and ``provenance``. If a model
    produced those itself it would be *self-attesting*: a model asked to state
    which model it is could simply claim a pinned one, and the mandate's
    ``VersionPins`` check would be a self-report dressed up as a control. That
    is exactly the honest bound M2 and M3 disclosed — the pin check proved
    *agreement*, not authorship.

    A model may only produce a ``ProposedDecision``, which has neither field.
    :func:`chronos.supervisor.queue.accept` — deterministic code, outside the
    model process — stamps both from harness-held configuration. The model
    therefore cannot say who it is, and cannot choose its own identity.

    Withholding ``decision_id`` matters for a second reason. If a model chose
    its id it could escape R-31's re-submission bound simply by picking a fresh
    one each time: the counters are keyed by id, so a new id is a new budget.
    The writer derives the id from the decision's *economic content* instead, so
    re-proposing the same trade yields the same id and is caught as a replay.
    That closes the dedup residual R-31 has carried since M2.

    Every validator lives here and is inherited, so a proposal is held to
    exactly the same standard as a decision — the split removes authority, not
    scrutiny.
    """

    kind: DecisionKind
    asset_class: TradableAssetClass
    #: Exactly one of ``symbol`` / ``futures_root`` is set, per asset class.
    symbol: str = Field(default="", max_length=32)
    futures_root: str = Field(default="", max_length=8)
    direction: DecisionDirection = DecisionDirection.NEUTRAL
    requested_strategy: StrategyForm | None = None
    #: A request, not an executable size. The kernel computes and clamps.
    requested_quantity: Decimal | None = None
    #: A request, not an allocation. The kernel sizes from mandate limits.
    requested_risk_budget_usd: Decimal | None = None
    time_horizon: TimeHorizon | None = None
    entry_plan: EntryPlan | None = None
    exit_plan: ExitPlan | None = None
    protective_order_required: bool = False
    max_acceptable_loss_usd: Decimal | None = None
    #: Chronos-owned reference to the order or position acted on. Never a
    #: broker order id.
    target_client_reference: str | None = Field(default=None, max_length=128)
    thesis: str = Field(default="", max_length=4000)
    rationale: str = Field(default="", max_length=4000)
    confidence: Decimal = Field(default=Decimal(0), ge=0, le=1)
    key_uncertainties: tuple[str, ...] = ()
    evidence: tuple[EvidenceCitation, ...] = Field(default=(), max_length=64)
    invalidation_conditions: tuple[str, ...] = ()
    reassess_at: AwareDatetime | None = None

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and not set(normalized) <= _SYMBOL_ALPHABET:
            raise ValueError(f"symbol {normalized!r} contains unsupported characters")
        return normalized

    @field_validator("futures_root")
    @classmethod
    def _normalize_root(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and not set(normalized) <= _ROOT_ALPHABET:
            raise ValueError(f"futures_root {normalized!r} contains unsupported characters")
        return normalized

    @field_validator("requested_quantity")
    @classmethod
    def _validate_quantity(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _validate_bounded_amount(value, "requested_quantity")

    @field_validator("requested_risk_budget_usd", "max_acceptable_loss_usd")
    @classmethod
    def _validate_money(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _validate_bounded_amount(value, "monetary amount")

    @field_validator("target_client_reference")
    @classmethod
    def _validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(_CHRONOS_REFERENCE_PATTERN, normalized):
            raise ValueError(
                "target_client_reference must be a Chronos-owned CHR-<PREFIX>-<32 hex> "
                "reference; a decision may never name a broker order id"
            )
        return normalized

    @field_validator("thesis", "rationale")
    @classmethod
    def _validate_narrative_text(cls, value: str) -> str:
        return _reject_control_characters(value, "narrative text")

    @field_validator("key_uncertainties", "invalidation_conditions")
    @classmethod
    def _validate_narrative_list(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            entry = _reject_control_characters(item.strip(), "narrative entry")
            if not entry:
                raise ValueError("narrative entries must not be blank")
            if len(entry) > 500:
                raise ValueError("narrative entries are limited to 500 characters")
            normalized.append(entry)
        if len(normalized) > 32:
            raise ValueError("at most 32 narrative entries are accepted")
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_decision(self) -> ProposedDecision:
        self._validate_instrument()
        self._validate_target_reference()
        self._validate_payload_matches_kind()
        if self.kind in EXPOSURE_CREATING_DECISION_KINDS:
            # Exposure may never be created on an unsupported assertion: a
            # decision that creates or extends risk must cite its evidence and
            # state what would prove it wrong.
            if not self.evidence:
                raise ValueError(f"a {self.kind.value} decision must cite at least one evidence id")
            if not self.invalidation_conditions:
                raise ValueError(
                    f"a {self.kind.value} decision must state its invalidation conditions"
                )
        return self

    def _validate_instrument(self) -> None:
        if self.asset_class is TradableAssetClass.FUTURE:
            if not self.futures_root:
                raise ValueError("FUTURE decisions require a futures_root")
            if self.symbol:
                raise ValueError("FUTURE decisions identify the instrument by futures_root only")
        elif self.asset_class in _ASSET_CLASSES_USING_SYMBOL:
            if not self.symbol:
                raise ValueError(f"{self.asset_class.value} decisions require a symbol")
            if self.futures_root:
                raise ValueError(f"{self.asset_class.value} decisions may not set a futures_root")
        else:
            # Reached by FUTURE_OPTION, which ADR-0016 §6 puts out of scope for
            # this release: recognized vocabulary, refused in code.
            raise ValueError(
                f"{self.asset_class.value} is out of scope in this release; enabling it "
                "requires its own ADR, tests, and promotion record"
            )

    def _validate_target_reference(self) -> None:
        if self.kind in TARGETED_DECISION_KINDS and self.target_client_reference is None:
            raise ValueError(
                f"a {self.kind.value} decision must name the Chronos reference it acts on"
            )
        if self.kind is DecisionKind.OPEN and self.target_client_reference is not None:
            raise ValueError("an OPEN decision may not name an existing order or position")

    def _validate_payload_matches_kind(self) -> None:
        """A risk-reducing decision may not carry a new-exposure request.

        Without this, a CLOSE or CANCEL could arrive carrying a strategy, an
        entry plan, a size and a LONG direction — a full opening request wearing
        a risk-reducing label. The kernel would still veto it, but the contract
        should not be able to say it in the first place (M1 adversarial review).
        """

        kind = self.kind.value
        if self.kind in _SIZELESS_KINDS and self.requested_quantity is not None:
            raise ValueError(f"a {kind} decision may not request a size")
        if self.kind in _NO_ENTRY_KINDS:
            if self.requested_strategy is not None:
                raise ValueError(f"a {kind} decision may not request a strategy")
            if self.entry_plan is not None:
                raise ValueError(f"a {kind} decision may not carry an entry plan")
            if self.requested_risk_budget_usd is not None:
                raise ValueError(f"a {kind} decision may not request a risk budget")
        if self.kind is DecisionKind.HOLD and self.direction is not DecisionDirection.NEUTRAL:
            raise ValueError("a HOLD decision may not express a direction")


class AITradeDecision(ProposedDecision):
    """A proposal that a deterministic writer has identified and attributed.

    The only shape that may enter the runtime pipeline. It is exactly a
    :class:`ProposedDecision` plus the two fields a model may never author:

    - ``decision_id``, derived by the writer from the proposal's economic
      content, so the same trade proposed twice is recognizably the same trade;
    - ``provenance``, stamped by the writer from harness-held configuration, so
      the mandate's ``VersionPins`` check compares against something the model
      did not write.

    Constructing one directly is possible in tests and in the writer, and that
    is deliberate — a type cannot enforce who instantiates it. What enforces the
    boundary is that the model process never receives a constructor for this:
    it emits ``ProposedDecision`` and the writer, which runs outside that
    process, produces this. The isolation tests pin that the model plane imports
    nothing that could submit one.
    """

    decision_id: str = Field(min_length=1, max_length=128)
    provenance: DecisionProvenance

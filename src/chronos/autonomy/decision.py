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
- **Narrative is narrative.** ``thesis``, ``rationale``, ``key_uncertainties``,
  and ``invalidation_conditions`` are recorded, displayed, and audited. Nothing
  in the runtime pipeline parses them into an order parameter; a test asserts
  no order-plane module reads them. They carry concise, decision-relevant
  reasoning — deliberately **not** hidden model chain-of-thought, which Chronos
  neither requests nor persists (ADR-0016 §5).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import AwareDatetime, Field, field_validator, model_validator

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
from chronos.domain.models import ChronosModel

#: Chronos-owned reference prefix. A decision may only ever name a Chronos
#: correlation reference drawn from its EvidenceBundle — never a broker order
#: id, which is the broker's namespace and not the model's to speak.
_CHRONOS_REFERENCE_PREFIX = "CHR-"

# Quantity bounds mirroring the order plane's Numeric(20,8) persistence scale.
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


def _validate_hex_digest(value: str, label: str) -> str:
    normalized = value.strip().lower()
    non_hex = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or non_hex:
        raise ValueError(f"{label} must be a 64-character lowercase hex digest")
    return normalized


class DecisionProvenance(ChronosModel):
    """Which model, prompt, tools, schema, and evidence produced this decision.

    The supervisor checks these against the mandate's :class:`VersionPins`
    before admission: a decision from an unpinned model, prompt, or tool schema
    is refused rather than downgraded.
    """

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    tool_schema_version: str = Field(min_length=1, max_length=64)
    decision_schema_version: str = Field(min_length=1, max_length=64)
    #: The immutable, versioned, redacted bundle the model was given.
    evidence_bundle_id: str = Field(min_length=1, max_length=128)
    evidence_bundle_digest: str
    produced_at: AwareDatetime

    @field_validator("evidence_bundle_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _validate_hex_digest(value, "evidence_bundle_digest")


class EvidenceCitation(ChronosModel):
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


class PriceTrigger(ChronosModel):
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
        if not value.is_finite():
            raise ValueError("trigger value must be a finite number")
        return value


class EntryPlan(ChronosModel):
    """Conditions under which the decision intends an entry to become live."""

    trigger: PriceTrigger | None = None
    valid_until: AwareDatetime | None = None


class ExitPlan(ChronosModel):
    """Intended exits, including any protective requirement the model asks for."""

    profit_target: PriceTrigger | None = None
    protective_stop: PriceTrigger | None = None
    time_exit: AwareDatetime | None = None


class AITradeDecision(ChronosModel):
    """One typed decision from an approved model. Holding one authorizes nothing."""

    decision_id: str = Field(min_length=1, max_length=128)
    kind: DecisionKind
    asset_class: TradableAssetClass
    #: Exactly one of ``symbol`` / ``futures_root`` is set, per asset class.
    symbol: str = Field(default="", max_length=32)
    futures_root: str = Field(default="", max_length=32)
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
    target_client_reference: str | None = None
    thesis: str = Field(default="", max_length=4000)
    rationale: str = Field(default="", max_length=4000)
    confidence: Decimal = Field(default=Decimal(0), ge=0, le=1)
    key_uncertainties: tuple[str, ...] = ()
    evidence: tuple[EvidenceCitation, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    reassess_at: AwareDatetime | None = None
    provenance: DecisionProvenance

    @field_validator("symbol", "futures_root")
    @classmethod
    def _normalize_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("requested_quantity")
    @classmethod
    def _validate_quantity(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite():
            raise ValueError("requested_quantity must be a finite number")
        if value <= 0:
            raise ValueError("requested_quantity must be positive")
        exponent = value.normalize().as_tuple().exponent
        if isinstance(exponent, int) and exponent < _MIN_QUANTITY_EXPONENT:
            raise ValueError("requested_quantity is finer than the 1e-8 persistence scale")
        if value >= _MAX_REQUESTED_QUANTITY:
            raise ValueError("requested_quantity exceeds the Numeric(20,8) persistence magnitude")
        return value

    @field_validator("requested_risk_budget_usd", "max_acceptable_loss_usd")
    @classmethod
    def _validate_money(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite():
            raise ValueError("monetary amounts must be finite")
        if value <= 0:
            raise ValueError("monetary amounts must be positive when supplied")
        return value

    @field_validator("target_client_reference")
    @classmethod
    def _validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized.startswith(_CHRONOS_REFERENCE_PREFIX):
            raise ValueError(
                "target_client_reference must be a Chronos-owned CHR- reference; "
                "a decision may never name a broker order id"
            )
        return normalized

    @field_validator("key_uncertainties", "invalidation_conditions")
    @classmethod
    def _validate_narrative_list(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            entry = item.strip()
            if not entry:
                raise ValueError("narrative entries must not be blank")
            if len(entry) > 500:
                raise ValueError("narrative entries are limited to 500 characters")
            normalized.append(entry)
        if len(normalized) > 32:
            raise ValueError("at most 32 narrative entries are accepted")
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_decision(self) -> AITradeDecision:
        self._validate_instrument()
        self._validate_target_reference()
        holds_a_request = self.requested_quantity is not None or self.requested_strategy is not None
        if self.kind is DecisionKind.HOLD and holds_a_request:
            raise ValueError("a HOLD decision may not request a size or a strategy")
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
        else:  # pragma: no cover - exhaustive over TradableAssetClass; fail closed
            raise ValueError(f"unsupported asset class {self.asset_class.value}")

    def _validate_target_reference(self) -> None:
        if self.kind in TARGETED_DECISION_KINDS and self.target_client_reference is None:
            raise ValueError(
                f"a {self.kind.value} decision must name the Chronos reference it acts on"
            )
        if self.kind is DecisionKind.OPEN and self.target_client_reference is not None:
            raise ValueError("an OPEN decision may not name an existing order or position")

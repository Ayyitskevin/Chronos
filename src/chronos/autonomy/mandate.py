"""The AutonomyMandate: the owner's bounded, expiring grant of trade-time authority.

ADR-0016 / DECISIONS.md D-16. A mandate is the **only** thing that can replace
per-order human confirmation, and it is authored by the owner, never by the
model. This module defines the contract shape only — no admission, no
evaluation, no broker behavior. Enforcement is the deterministic supervisor's
(Milestone 2).

Three properties are structural rather than procedural, and each is asserted by
a test:

1. **Immutable.** Every model here is frozen with ``extra="forbid"`` and
   inherits :class:`~chronos.autonomy.base.AutonomyModel`, so even
   ``model_copy(update=...)`` re-runs every validator. A mandate cannot be
   widened in place or by copy, and an unrecognized field is a load error
   rather than a silently-ignored grant. Creating, expanding, renewing,
   enabling, and revoking are authenticated owner *events* recorded against
   ``mandate_id`` by the supervisor; none of them mutates this record.
2. **Expiring.** ``expires_at`` is required and must follow ``effective_from``.
   Live and canary-live mandates additionally may not exceed
   :data:`MAX_LIVE_MANDATE_DURATION`. There is no perpetual live authority.
3. **Deny-by-default — with one honest caveat.** Every *ceiling* defaults to
   zero and every scope tuple to empty, so a default-constructed mandate
   authorizes nothing. But some fields are **floors** — ``min_cash_floor_usd``,
   ``min_buying_power_usd``, ``max_quote_age_seconds`` (read as a freshness
   floor), and the liquidity minimums ``min_option_volume`` /
   ``min_open_interest`` — where zero is the *most* permissive value, not the
   most restrictive. Defaulting a floor to zero is therefore not
   deny-by-default, so a submitting mandate must set the three load-bearing
   ones explicitly (see ``_validate_submitting_floors``); the two liquidity
   minimums stay advisory inputs to the kernel's own liquidity checks rather
   than mandate-level gates. The M1 adversarial review caught the original text
   claiming deny-by-default across the board.

The model may *read* a mandate. It can neither author one nor raise its own
limits: no tool in the model plane writes this type (ADR-0016 §3).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.autonomy.base import AutonomyModel
from chronos.autonomy.enums import (
    LIVE_AUTONOMY_MODES,
    MINIMUM_PROMOTION_FOR_MODE,
    SUBMITTING_AUTONOMY_MODES,
    AutonomyMode,
    OrderForm,
    PromotionLevel,
    RestartBehavior,
    StrategyForm,
    TradableAssetClass,
    TradingSession,
    promotion_rank,
)
from chronos.config.limits import validate_option_selection_decimal
from chronos.domain.enums import DataQuality
from chronos.utils.identifiers import normalize_account_fingerprint

#: Ceiling on how long a live or canary-live mandate may run before the owner
#: must renew it deliberately. ADR-0017 (owner-directed persistent authority)
#: raised this from 30 days to a year: the owner chose a standing mandate that
#: re-arms on restart, and a monthly re-authorship ritual added friction without
#: adding safety for a single-operator system. Renewal at the year boundary is
#: still a fresh owner action — there is still no perpetual live authority.
MAX_LIVE_MANDATE_DURATION: timedelta = timedelta(days=365)

#: Market-data qualities a live autonomous mandate may permit. Restated here
#: rather than imported: `chronos.autonomy` may not import `chronos.orders`
#: (ADR-0016 §3). It must stay a subset of the deterministic live gate's
#: `_LIVE_ACCEPTABLE_QUALITY` in `chronos/orders/submission.py` — a mandate may
#: not license data the kernel would refuse anyway. FROZEN, DELAYED_FROZEN and
#: DEMO are excluded: the first two are last-known-value rather than current,
#: and DEMO is synthetic.
LIVE_PERMITTED_DATA_QUALITIES: frozenset[DataQuality] = frozenset(
    {DataQuality.LIVE, DataQuality.DELAYED}
)

#: Qualities no mandate may ever permit, in any mode: data known to be bad.
NEVER_PERMITTED_DATA_QUALITIES: frozenset[DataQuality] = frozenset(
    {DataQuality.STALE, DataQuality.UNKNOWN}
)

#: Which asset classes each strategy form can be expressed in. Used to refuse a
#: scope that pairs a strategy with a class it cannot be traded in.
_STRATEGY_ASSET_CLASSES: dict[StrategyForm, frozenset[TradableAssetClass]] = {
    StrategyForm.LONG_EQUITY: frozenset({TradableAssetClass.EQUITY, TradableAssetClass.CRYPTO}),
    StrategyForm.SHORT_EQUITY: frozenset({TradableAssetClass.EQUITY}),
    StrategyForm.CASH_SECURED_PUT: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.COVERED_CALL: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.LONG_CALL: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.LONG_PUT: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.VERTICAL_DEBIT_SPREAD: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.VERTICAL_CREDIT_SPREAD: frozenset(
        {TradableAssetClass.EQUITY_OPTION, TradableAssetClass.INDEX_OPTION}
    ),
    StrategyForm.LONG_FUTURE: frozenset({TradableAssetClass.FUTURE}),
    StrategyForm.SHORT_FUTURE: frozenset({TradableAssetClass.FUTURE}),
}

_SYMBOL_ASSET_CLASSES: frozenset[TradableAssetClass] = frozenset(
    {
        TradableAssetClass.EQUITY,
        TradableAssetClass.EQUITY_OPTION,
        TradableAssetClass.INDEX_OPTION,
        TradableAssetClass.CRYPTO,
    }
)

_MAX_SCOPE_ENTRIES = 256
_SYMBOL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def _normalized_scope(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Upper-case, non-blank, duplicate-free, alphabet-checked scope entries.

    Duplicates are refused rather than collapsed: a mandate is an owner-authored
    authorization, and a scope the owner cannot read back exactly is not one.
    The alphabet check keeps routing syntax (``SPY@SMART/ISLAND``) and control
    characters out of a field the supervisor will match contracts against.
    """

    normalized: list[str] = []
    for value in values:
        entry = value.strip().upper()
        if not entry:
            raise ValueError(f"{label} entries must not be blank")
        if len(entry) > 32:
            raise ValueError(f"{label} entries are limited to 32 characters")
        if not set(entry) <= _SYMBOL_ALPHABET:
            raise ValueError(f"{label} entry {entry!r} contains unsupported characters")
        if entry in normalized:
            raise ValueError(f"{label} contains the duplicate entry {entry!r}")
        normalized.append(entry)
    if len(normalized) > _MAX_SCOPE_ENTRIES:
        raise ValueError(f"{label} exceeds {_MAX_SCOPE_ENTRIES} entries")
    return tuple(normalized)


def _unique(values: tuple[object, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} contains duplicate entries")


class VersionPins(AutonomyModel):
    """Exact versions this mandate authorizes.

    A material change to any pin invalidates the affected promotion record and
    returns that configuration to SHADOW or PAPER (ADR-0016 §7). The pins are
    required and non-blank so "which model was authorized" is never ambiguous.
    """

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    tool_schema_version: str = Field(min_length=1, max_length=64)
    decision_schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)


class FamilyPromotion(AutonomyModel):
    """The promotion rung one asset family has actually earned.

    Promotion is per family (ADR-0016 §7): a stock promotion authorizes neither
    futures nor options. A single scalar rung on the mandate could not express
    that — it would let one family's evidence license another family's live
    trading — so authority is carried per asset class instead.
    """

    asset_class: TradableAssetClass
    level: PromotionLevel


class InstrumentScope(AutonomyModel):
    """What may be traded. Empty means nothing — this is a grant, not a filter."""

    asset_classes: tuple[TradableAssetClass, ...] = ()
    symbols: tuple[str, ...] = ()
    futures_roots: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    contract_families: tuple[str, ...] = ()
    strategies: tuple[StrategyForm, ...] = ()
    order_forms: tuple[OrderForm, ...] = ()

    @field_validator("symbols")
    @classmethod
    def _validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_scope(value, "symbols")

    @field_validator("futures_roots")
    @classmethod
    def _validate_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_scope(value, "futures_roots")

    @field_validator("exchanges")
    @classmethod
    def _validate_exchanges(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_scope(value, "exchanges")

    @field_validator("contract_families")
    @classmethod
    def _validate_families(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_scope(value, "contract_families")

    @model_validator(mode="after")
    def _validate_scope(self) -> InstrumentScope:
        _unique(self.asset_classes, "asset_classes")
        _unique(self.strategies, "strategies")
        _unique(self.order_forms, "order_forms")
        if TradableAssetClass.FUTURE_OPTION in self.asset_classes:
            # Recognized vocabulary, refused in code (ADR-0016 §6) — the
            # ADR-0007 precedent: refusing in code beats refusing in config.
            raise ValueError(
                "FUTURE_OPTION is out of scope in this release; enabling it "
                "requires its own ADR, tests, and promotion record"
            )
        permitted = set(self.asset_classes)
        for strategy in self.strategies:
            # A strategy the permitted classes cannot express is either a
            # mistake or an attempt to smuggle authority past the class list.
            if not (_STRATEGY_ASSET_CLASSES[strategy] & permitted):
                raise ValueError(
                    f"strategy {strategy.value} is not expressible in the "
                    f"permitted asset classes; add the asset class or drop the strategy"
                )
        if self.futures_roots and TradableAssetClass.FUTURE not in permitted:
            raise ValueError("futures_roots requires FUTURE in asset_classes")
        if self.symbols and not (_SYMBOL_ASSET_CLASSES & permitted):
            raise ValueError("symbols require a symbol-based asset class in asset_classes")
        return self


class CapitalLimits(AutonomyModel):
    """Capital, size, exposure, and leverage ceilings, plus two floors.

    The ``max_*`` fields are ceilings: zero authorizes nothing — **unless** the
    mandate grants ``model_discretion`` (ADR-0017), in which case an unset
    ceiling is no ceiling and affordability is the bound, while any ceiling the
    owner did set still binds. The ``min_*`` fields are **floors**, where zero
    is the most permissive value — a submitting mandate must set them
    explicitly in every mode, discretion included: discretion over size is not
    discretion over the reserve.
    """

    #: ADR-0017: the owner's grant of model self-sizing. When True, the ceiling
    #: fields below become *optional overlays* — an explicitly-set positive
    #: ceiling still binds, but an unset one no longer reads as "authorizes
    #: nothing"; sizing is bounded by what the account can actually afford
    #: (cash and buying power net of the floors) instead. This is a deliberate,
    #: owner-written inversion of the zero-authorizes-nothing doctrine, scoped
    #: to mandates that state it. False keeps ADR-0016 semantics exactly.
    model_discretion: bool = False
    allocated_capital_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_order_notional_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_position_notional_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_gross_exposure_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_net_exposure_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_contracts_per_order: int = Field(default=0, ge=0)
    max_shares_per_order: int = Field(default=0, ge=0)
    max_leverage: Decimal = Field(default=Decimal(0), ge=0)
    max_margin_utilization_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    min_buying_power_usd: Decimal = Field(default=Decimal(0), ge=0)
    min_cash_floor_usd: Decimal = Field(default=Decimal(0), ge=0)


class LossLimits(AutonomyModel):
    """Session, daily, and peak-to-trough loss ceilings. Zero authorizes nothing."""

    max_session_loss_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_daily_loss_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_peak_to_trough_drawdown_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_peak_to_trough_drawdown_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)


class ConcentrationLimits(AutonomyModel):
    """Per-symbol, sector, family, and correlated-exposure ceilings, as fractions."""

    max_symbol_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_sector_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_family_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_correlated_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)


class ActivityLimits(AutonomyModel):
    """Order, cancellation, replacement, and turnover ceilings per session."""

    max_orders_per_session: int = Field(default=0, ge=0)
    max_cancellations_per_session: int = Field(default=0, ge=0)
    max_replacements_per_session: int = Field(default=0, ge=0)
    max_turnover_usd_per_session: Decimal = Field(default=Decimal(0), ge=0)


class MarketDataRequirements(AutonomyModel):
    """Freshness and liquidity floors below which the kernel creates no exposure.

    Note these are **floors**: ``max_quote_age_seconds`` of zero would accept a
    quote of any age were it read as "no requirement", so a submitting mandate
    must set it, and the mandate validator enforces that.
    """

    max_quote_age_seconds: Decimal = Field(default=Decimal(0), ge=0)
    permitted_data_qualities: tuple[DataQuality, ...] = ()
    min_option_volume: int = Field(default=0, ge=0)
    min_open_interest: int = Field(default=0, ge=0)
    max_relative_spread: Decimal = Field(default=Decimal(0), ge=0, le=1)

    @field_validator("max_quote_age_seconds", "max_relative_spread")
    @classmethod
    def _validate_receipt_decimal_width(cls, value: Decimal) -> Decimal:
        return validate_option_selection_decimal(value, "market-data requirement")

    @model_validator(mode="after")
    def _validate_qualities(self) -> MarketDataRequirements:
        _unique(self.permitted_data_qualities, "permitted_data_qualities")
        offending = sorted(
            q.value for q in self.permitted_data_qualities if q in NEVER_PERMITTED_DATA_QUALITIES
        )
        if offending:
            # Stale-data rejection is a deterministic guarantee ADR-0016 does not
            # supersede; a mandate may not license trading on data known bad.
            raise ValueError(f"permitted_data_qualities may not include {', '.join(offending)}")
        return self


class SessionPolicy(AutonomyModel):
    """Which sessions may trade, and whether positions may be carried overnight."""

    permitted_sessions: tuple[TradingSession, ...] = ()
    allow_overnight_holding: bool = False

    @model_validator(mode="after")
    def _validate_sessions(self) -> SessionPolicy:
        _unique(self.permitted_sessions, "permitted_sessions")
        return self


class AutonomyMandate(AutonomyModel):
    """One owner-authored, versioned, expiring, revocable grant of authority.

    Read the module docstring first: this is a contract, not a control. Holding
    an instance authorizes nothing.

    **Not every field is enforced yet.** As of M2 the deterministic supervisor
    independently re-derives the account, activation, mode, promotion, scope,
    strategy, direction, market-data freshness, and the capital ceilings and
    floors that bind sizing. It does **not** yet enforce ``LossLimits`` or
    ``ActivityLimits`` at all, nor ``scope.exchanges`` / ``scope.contract_families``
    (which need a qualified contract), nor sector/family/correlated concentration,
    leverage, or margin utilisation. The authoritative, milestone-attributed list
    lives in :mod:`chronos.supervisor.admission` and is pinned by
    ``tests/safety/test_supervisor_gateway.py`` so it cannot silently grow. An
    earlier version of this docstring claimed the supervisor re-derived *every*
    limit; the M2 adversarial review found that false and it is corrected here.
    """

    mandate_id: str = Field(min_length=1, max_length=128)
    mandate_version: int = Field(ge=1)
    #: Pseudonymous account scope — never a raw broker account id.
    account_fingerprint: str
    mode: AutonomyMode
    #: Per-asset-family promotion. A family absent here has earned nothing.
    promotions: tuple[FamilyPromotion, ...] = ()
    effective_from: AwareDatetime
    expires_at: AwareDatetime
    #: ADR-0017 flips the default to RESUME_UNTIL_EXPIRY: the owner chose a
    #: persistent mandate where bringing the process up is enough to trade.
    #: REQUIRE_REACTIVATION remains available for owners who want the stricter
    #: behavior back — the vocabulary lost nothing, only its default moved.
    restart_behavior: RestartBehavior = RestartBehavior.RESUME_UNTIL_EXPIRY
    versions: VersionPins
    scope: InstrumentScope = InstrumentScope()
    capital: CapitalLimits = CapitalLimits()
    loss: LossLimits = LossLimits()
    concentration: ConcentrationLimits = ConcentrationLimits()
    activity: ActivityLimits = ActivityLimits()
    market_data: MarketDataRequirements = MarketDataRequirements()
    sessions: SessionPolicy = SessionPolicy()
    #: Reference to the authenticated owner action that authored this mandate.
    owner_authorization_ref: str = Field(min_length=1, max_length=128)
    authored_at: AwareDatetime
    note: str = Field(default="", max_length=2000)

    @field_validator("account_fingerprint")
    @classmethod
    def _validate_fingerprint(cls, value: str) -> str:
        return normalize_account_fingerprint(value)

    @model_validator(mode="after")
    def _validate_mandate(self) -> AutonomyMandate:
        if self.expires_at <= self.effective_from:
            raise ValueError("expires_at must be after effective_from")

        runs_too_long = self.expires_at - self.effective_from > MAX_LIVE_MANDATE_DURATION
        if self.mode in LIVE_AUTONOMY_MODES and runs_too_long:
            raise ValueError(
                "a live autonomous mandate may not run longer than "
                f"{MAX_LIVE_MANDATE_DURATION.days} days; renew it deliberately instead"
            )

        seen: set[TradableAssetClass] = set()
        for promotion in self.promotions:
            if promotion.asset_class in seen:
                raise ValueError(f"duplicate promotion for {promotion.asset_class.value}")
            seen.add(promotion.asset_class)

        if self.mode in SUBMITTING_AUTONOMY_MODES:
            self._validate_submitting_scope()
            self._validate_submitting_promotions()
            self._validate_submitting_floors()
        if self.mode in LIVE_AUTONOMY_MODES:
            self._validate_live_data_qualities()
        return self

    def _validate_submitting_scope(self) -> None:
        """A submitting mandate must say what it permits. Silence is not a grant."""

        if not self.scope.asset_classes:
            raise ValueError(f"mode {self.mode.value} requires at least one asset class")
        if not self.scope.order_forms:
            raise ValueError(f"mode {self.mode.value} requires at least one order form")
        if not self.scope.strategies:
            raise ValueError(f"mode {self.mode.value} requires at least one strategy")
        if not self.market_data.permitted_data_qualities:
            raise ValueError(f"mode {self.mode.value} requires explicit permitted_data_qualities")
        symbol_classes = _SYMBOL_ASSET_CLASSES & set(self.scope.asset_classes)
        if symbol_classes and not self.scope.symbols:
            raise ValueError("symbol-based asset classes require a non-empty symbols scope")
        if TradableAssetClass.FUTURE in self.scope.asset_classes and not self.scope.futures_roots:
            raise ValueError("FUTURE requires a non-empty futures_roots scope")

    def _validate_submitting_promotions(self) -> None:
        """Every permitted family must itself have earned this mode's rung."""

        minimum = MINIMUM_PROMOTION_FOR_MODE[self.mode]
        earned = {promotion.asset_class: promotion.level for promotion in self.promotions}
        for asset_class in self.scope.asset_classes:
            level = earned.get(asset_class)
            if level is None:
                raise ValueError(
                    f"{asset_class.value} is in scope but has no promotion record; "
                    "promotion is per asset family (ADR-0016 §7)"
                )
            if promotion_rank(level) < promotion_rank(minimum):
                raise ValueError(
                    f"mode {self.mode.value} requires {asset_class.value} to be promoted to "
                    f"{minimum.value} or higher, not {level.value}"
                )

    def _validate_submitting_floors(self) -> None:
        """Floors must be set explicitly — a zero floor is the *most* permissive.

        Deny-by-default reasoning holds for ceilings but inverts for floors, so
        these cannot simply be defaulted (M1 adversarial review).
        """

        if self.market_data.max_quote_age_seconds <= 0:
            raise ValueError(
                f"mode {self.mode.value} requires a positive max_quote_age_seconds; "
                "zero would accept a quote of any age"
            )
        if self.capital.min_cash_floor_usd <= 0:
            raise ValueError(f"mode {self.mode.value} requires a positive min_cash_floor_usd")
        if self.capital.min_buying_power_usd <= 0:
            raise ValueError(f"mode {self.mode.value} requires a positive min_buying_power_usd")

        # ADR-0017: a mandate granting model discretion waives the ceiling
        # requirements below — affordability (cash/buying power net of the
        # floors just validated) becomes the bound, and any ceiling the owner
        # DID set still binds as an overlay. The floors above are still
        # required: discretion over size is not discretion over the reserve.
        if self.capital.model_discretion:
            return

        # Ceilings are deny-by-default too: a zero ceiling authorizes nothing, so
        # a submitting mandate that leaves one at its default would size every
        # order to zero. Failing here says so at authoring time, rather than
        # leaving the owner to discover it as a silent refusal at trade time.
        required_ceilings = (
            ("allocated_capital_usd", self.capital.allocated_capital_usd),
            ("max_order_notional_usd", self.capital.max_order_notional_usd),
            ("max_gross_exposure_usd", self.capital.max_gross_exposure_usd),
            ("max_symbol_exposure_pct", self.concentration.max_symbol_exposure_pct),
        )
        for name, value in required_ceilings:
            if value <= 0:
                raise ValueError(
                    f"mode {self.mode.value} requires a positive {name}; a zero ceiling "
                    "authorizes nothing and would refuse every order"
                )
        uses_contracts = any(
            item
            in {
                TradableAssetClass.EQUITY_OPTION,
                TradableAssetClass.INDEX_OPTION,
                TradableAssetClass.FUTURE,
            }
            for item in self.scope.asset_classes
        )
        uses_shares = any(
            item in {TradableAssetClass.EQUITY, TradableAssetClass.CRYPTO}
            for item in self.scope.asset_classes
        )
        if uses_contracts and self.capital.max_contracts_per_order <= 0:
            raise ValueError(
                f"mode {self.mode.value} permits contract-based asset classes and therefore "
                "requires a positive max_contracts_per_order"
            )
        if uses_shares and self.capital.max_shares_per_order <= 0:
            raise ValueError(
                f"mode {self.mode.value} permits share-based asset classes and therefore "
                "requires a positive max_shares_per_order"
            )

    def _validate_live_data_qualities(self) -> None:
        """A live mandate may not license data the deterministic gate refuses."""

        offending = sorted(
            q.value
            for q in self.market_data.permitted_data_qualities
            if q not in LIVE_PERMITTED_DATA_QUALITIES
        )
        if offending:
            raise ValueError(
                f"mode {self.mode.value} may not permit data qualities {', '.join(offending)}; "
                "live autonomy accepts only LIVE or DELAYED"
            )

    def promotion_for(self, asset_class: TradableAssetClass) -> PromotionLevel | None:
        """The rung ``asset_class`` has earned under this mandate, if any."""

        for promotion in self.promotions:
            if promotion.asset_class is asset_class:
                return promotion.level
        return None

    def covers_instant(self, instant: AwareDatetime) -> bool:
        """Whether ``instant`` falls inside this mandate's effective window.

        **This is a time-window predicate, not an authorization decision.** It
        deliberately knows nothing about activation, revocation, restart
        behavior, account match, kill-switch state, promotion evidence, or any
        limit. Never treat a ``True`` here as permission to trade; the
        deterministic supervisor evaluates all of that separately (M2).
        """

        return self.effective_from <= instant < self.expires_at

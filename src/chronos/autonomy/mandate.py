"""The AutonomyMandate: the owner's bounded, expiring grant of trade-time authority.

ADR-0016 / DECISIONS.md D-16. A mandate is the **only** thing that can replace
per-order human confirmation, and it is authored by the owner, never by the
model. This module defines the contract shape only — no admission, no
evaluation, no broker behavior. Enforcement is the deterministic supervisor's
(Milestone 2).

Three properties are structural rather than procedural, and each is asserted by
a test:

1. **Immutable.** Every model here is frozen with ``extra="forbid"``. A mandate
   cannot be widened in place, and an unrecognized field is a load error rather
   than a silently-ignored grant. Creating, expanding, renewing, enabling, and
   revoking are authenticated owner *events* recorded against ``mandate_id`` by
   the supervisor; none of them mutates this record.
2. **Expiring.** ``expires_at`` is required and must follow ``effective_from``.
   Live and canary-live mandates additionally may not exceed
   :data:`MAX_LIVE_MANDATE_DURATION`. There is no perpetual live authority.
3. **Deny-by-default.** Every limit defaults to zero and every scope tuple
   defaults to empty, so a default-constructed mandate authorizes nothing. A
   mandate grants exactly what it enumerates, mirroring the all-zeros
   ``config/risk.example.yaml`` doctrine.

The model may *read* a mandate. It can neither author one nor raise its own
limits: no tool in the model plane writes this type (ADR-0016 §3).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, Field, field_validator, model_validator

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
from chronos.domain.enums import DataQuality
from chronos.domain.models import ChronosModel
from chronos.utils.identifiers import normalize_account_fingerprint

#: Ceiling on how long a live or canary-live mandate may run before the owner
#: must renew it deliberately. Renewal is a fresh authenticated owner action, so
#: unattended live authority can never outlive the owner's attention by more
#: than this window (ADR-0016 §4).
MAX_LIVE_MANDATE_DURATION: timedelta = timedelta(days=30)

_MAX_SCOPE_ENTRIES = 256


def _normalized_scope(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Upper-case, non-blank, duplicate-free scope entries.

    Duplicates are refused rather than collapsed: a mandate is an owner-authored
    authorization, and a scope the owner cannot read back exactly is not one.
    """

    normalized: list[str] = []
    for value in values:
        entry = value.strip().upper()
        if not entry:
            raise ValueError(f"{label} entries must not be blank")
        if entry in normalized:
            raise ValueError(f"{label} contains the duplicate entry {entry!r}")
        normalized.append(entry)
    if len(normalized) > _MAX_SCOPE_ENTRIES:
        raise ValueError(f"{label} exceeds {_MAX_SCOPE_ENTRIES} entries")
    return tuple(normalized)


def _unique(values: tuple[object, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} contains duplicate entries")


class VersionPins(ChronosModel):
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


class InstrumentScope(ChronosModel):
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
        return self


class CapitalLimits(ChronosModel):
    """Capital, size, exposure, and leverage ceilings. Zero authorizes nothing."""

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


class LossLimits(ChronosModel):
    """Session, daily, and peak-to-trough loss ceilings. Zero authorizes nothing."""

    max_session_loss_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_daily_loss_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_peak_to_trough_drawdown_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_peak_to_trough_drawdown_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)


class ConcentrationLimits(ChronosModel):
    """Per-symbol, sector, family, and correlated-exposure ceilings, as fractions."""

    max_symbol_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_sector_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_family_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)
    max_correlated_exposure_pct: Decimal = Field(default=Decimal(0), ge=0, le=1)


class ActivityLimits(ChronosModel):
    """Order, cancellation, replacement, and turnover ceilings per session."""

    max_orders_per_session: int = Field(default=0, ge=0)
    max_cancellations_per_session: int = Field(default=0, ge=0)
    max_replacements_per_session: int = Field(default=0, ge=0)
    max_turnover_usd_per_session: Decimal = Field(default=Decimal(0), ge=0)


class MarketDataRequirements(ChronosModel):
    """Freshness and liquidity floors below which the kernel creates no exposure."""

    max_quote_age_seconds: Decimal = Field(default=Decimal(0), ge=0)
    permitted_data_qualities: tuple[DataQuality, ...] = ()
    min_option_volume: int = Field(default=0, ge=0)
    min_open_interest: int = Field(default=0, ge=0)
    max_relative_spread: Decimal = Field(default=Decimal(0), ge=0, le=1)

    @model_validator(mode="after")
    def _validate_qualities(self) -> MarketDataRequirements:
        _unique(self.permitted_data_qualities, "permitted_data_qualities")
        forbidden = {DataQuality.STALE, DataQuality.UNKNOWN}
        offending = sorted(q.value for q in self.permitted_data_qualities if q in forbidden)
        if offending:
            # Stale-data rejection is a deterministic guarantee ADR-0016 does not
            # supersede; a mandate may not license trading on data known bad.
            raise ValueError(f"permitted_data_qualities may not include {', '.join(offending)}")
        return self


class SessionPolicy(ChronosModel):
    """Which sessions may trade, and whether positions may be carried overnight."""

    permitted_sessions: tuple[TradingSession, ...] = ()
    allow_overnight_holding: bool = False

    @model_validator(mode="after")
    def _validate_sessions(self) -> SessionPolicy:
        _unique(self.permitted_sessions, "permitted_sessions")
        return self


class AutonomyMandate(ChronosModel):
    """One owner-authored, versioned, expiring, revocable grant of authority.

    Read the module docstring first: this is a contract, not a control. Holding
    an instance authorizes nothing. Every field here is an input to the
    deterministic supervisor's checks (M2), which independently re-derive the
    account, mode, promotion, scope, and every limit before any order is minted.
    """

    mandate_id: str = Field(min_length=1, max_length=128)
    mandate_version: int = Field(ge=1)
    #: Pseudonymous account scope — never a raw broker account id.
    account_fingerprint: str
    mode: AutonomyMode
    promotion_level: PromotionLevel
    effective_from: AwareDatetime
    expires_at: AwareDatetime
    restart_behavior: RestartBehavior = RestartBehavior.REQUIRE_REACTIVATION
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

        minimum = MINIMUM_PROMOTION_FOR_MODE[self.mode]
        if promotion_rank(self.promotion_level) < promotion_rank(minimum):
            raise ValueError(
                f"mode {self.mode.value} requires promotion level "
                f"{minimum.value} or higher, not {self.promotion_level.value}"
            )

        runs_too_long = self.expires_at - self.effective_from > MAX_LIVE_MANDATE_DURATION
        if self.mode in LIVE_AUTONOMY_MODES and runs_too_long:
            raise ValueError(
                "a live autonomous mandate may not run longer than "
                f"{MAX_LIVE_MANDATE_DURATION.days} days; renew it deliberately instead"
            )

        if self.mode in SUBMITTING_AUTONOMY_MODES:
            # A submitting mandate must say what it permits. Silence is not a
            # grant, and an unstated scope must never read as "everything".
            if not self.scope.asset_classes:
                raise ValueError(f"mode {self.mode.value} requires at least one asset class")
            if not self.scope.order_forms:
                raise ValueError(f"mode {self.mode.value} requires at least one order form")
            if not self.scope.strategies:
                raise ValueError(f"mode {self.mode.value} requires at least one strategy")
            if not self.market_data.permitted_data_qualities:
                raise ValueError(
                    f"mode {self.mode.value} requires explicit permitted_data_qualities"
                )
            self._validate_instrument_identifiers()
        return self

    def _validate_instrument_identifiers(self) -> None:
        """Each permitted asset class must name the instruments it covers."""

        symbol_classes = {
            TradableAssetClass.EQUITY,
            TradableAssetClass.EQUITY_OPTION,
            TradableAssetClass.INDEX_OPTION,
            TradableAssetClass.CRYPTO,
        }
        if any(item in symbol_classes for item in self.scope.asset_classes) and not (
            self.scope.symbols
        ):
            raise ValueError("symbol-based asset classes require a non-empty symbols scope")
        if TradableAssetClass.FUTURE in self.scope.asset_classes and not self.scope.futures_roots:
            raise ValueError("FUTURE requires a non-empty futures_roots scope")

    def covers_instant(self, instant: AwareDatetime) -> bool:
        """Whether ``instant`` falls inside this mandate's effective window.

        **This is a time-window predicate, not an authorization decision.** It
        deliberately knows nothing about activation, revocation, restart
        behavior, account match, kill-switch state, promotion evidence, or any
        limit. Never treat a ``True`` here as permission to trade; the
        deterministic supervisor evaluates all of that separately (M2).
        """

        return self.effective_from <= instant < self.expires_at

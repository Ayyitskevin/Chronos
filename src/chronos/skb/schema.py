"""Strategy Knowledge Base schema (AI Quant plan B1).

Controlled vocabularies plus the entity models the compiler validates every
record against. Loading fails closed on unknown enum values (``extra="forbid"``
on every model), so a corpus field the vocabulary does not cover cannot silently
enter the store.

Two entity levels are modelled:

- :class:`PineScriptEntry` — one per Pine script in the corpus (42), the join of
  the registry (identity + forensic flags) and the forensic findings (family,
  direction, integrity, feasibility, defects).
- :class:`DerivedStrategy` — one per canonical spec (currently 2), the ported
  Python derivation with its selection-candidacy and research results.

The controlled vocabularies for family/direction/timeframe/asset-class/
regime-tag/disposition are defined here; B1 populates the ones the corpus states
directly (family, direction) and derives disposition where a spec proves it
(PORTED). Timeframe/asset-class/regime-tag stay UNKNOWN/empty pending the B2
backfill — never guessed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- controlled vocabularies ------------------------------------------------


class Classification(StrEnum):
    """What the Pine artifact declares itself to be (from the forensic audit)."""

    STRATEGY = "strategy"
    STRATEGY_ADDON = "strategy_addon"
    INDICATOR = "indicator"
    STUDY = "study"
    LIBRARY = "library"
    DISPLAY_OVERLAY = "display_overlay"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    BIDIRECTIONAL = "bidirectional"
    NONE = "none"


class StrategyFamily(StrEnum):
    REGIME_DETECTION = "regime_detection"
    STATISTICAL_READOUT = "statistical_readout"
    VOLUME_ORDERFLOW = "volume_orderflow"
    MARKET_STRUCTURE = "market_structure"
    VOLATILITY = "volatility"
    MEAN_REVERSION = "mean_reversion"
    JOURNALING_VALIDATION = "journaling_validation"
    VALUATION_CONTEXT = "valuation_context"
    TREND_FOLLOWING = "trend_following"
    RISK_OVERLAY = "risk_overlay"
    PORTFOLIO_ALLOCATION = "portfolio_allocation"


class IntegrityStatus(StrEnum):
    PASS_WITH_CONSTRAINTS = "PASS_WITH_CONSTRAINTS"
    NON_EXECUTABLE_INDICATOR = "NON_EXECUTABLE_INDICATOR"
    REQUIRES_REWRITE = "REQUIRES_REWRITE"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Disposition(StrEnum):
    """Port disposition. B1 assigns PORTED (a spec derives it) or UNCLASSIFIED;
    B2 backfills DEFERRED / BLOCKED_ON / REJECTED with machine-readable reasons."""

    PORTED = "ported"
    DEFERRED = "deferred"
    BLOCKED_ON = "blocked_on"
    REJECTED = "rejected"
    UNCLASSIFIED = "unclassified"


class Timeframe(StrEnum):
    """Defined vocabulary; corpus states timeframe only in prose, so B1 leaves
    entries UNKNOWN — the B2 backfill assigns these, never a guess."""

    INTRADAY = "intraday"
    DAILY = "daily"
    SWING = "swing"
    WEEKLY = "weekly"
    MULTI = "multi"
    UNKNOWN = "unknown"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    FUTURES = "futures"
    FX = "fx"
    OPTIONS = "options"
    MULTI = "multi"
    UNKNOWN = "unknown"


class RegimeTag(StrEnum):
    """Defined vocabulary for the B2 backfill; B1 leaves regime_tags empty."""

    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    TREND = "trend"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"
    BREAKOUT = "breakout"
    MEAN_REVERT = "mean_revert"
    TRANSITION = "transition"


# --- entities ---------------------------------------------------------------


class PineForensicFlags(_Frozen):
    """The load-bearing safety/translation flags harvested from the registry."""

    uses_request_security: bool
    uses_lookahead_on: bool
    uses_barstate_isconfirmed: bool
    uses_calc_on_every_tick_true: bool
    uses_process_orders_on_close_true: bool
    uses_strategy_orders: bool
    pyramiding_gt_0: bool


class PineScriptEntry(_Frozen):
    """One Pine script: registry identity + forensic finding, jointly validated."""

    catalog_number: str
    filename: str
    title: str
    sha256: str
    bytes: int = Field(ge=0)
    lines: int = Field(ge=0)
    pine_version: str
    declaration: str
    cycle: str
    classification: Classification
    strategy_family: StrategyFamily
    direction: Direction
    integrity_status: IntegrityStatus
    confidence: Confidence
    deterministic_translation_feasibility: str
    known_defects: tuple[str, ...] = ()
    related_scripts: tuple[str, ...] = ()
    forensic_flags: PineForensicFlags
    # Controlled-vocabulary tags. B1 assigns disposition where a spec proves it;
    # timeframe/asset_class/regime_tags await the B2 backfill (never guessed).
    disposition: Disposition = Disposition.UNCLASSIFIED
    timeframe: Timeframe = Timeframe.UNKNOWN
    asset_class: AssetClass = AssetClass.UNKNOWN
    regime_tags: tuple[RegimeTag, ...] = ()


class StrategyResult(_Frozen):
    """One backtest run's headline metrics for a derived strategy (from results).

    ``win_rate``/``profit_factor`` are null when a run closed zero trades — kept
    as ``None`` rather than a misleading 0.0.
    """

    partition: str
    symbol: str
    trades: int = Field(ge=0)
    win_rate: float | None = None
    profit_factor: float | None = None
    total_return_fraction: float | None = None
    max_drawdown_fraction: float | None = None
    sharpe: float | None = None


class DerivedStrategy(_Frozen):
    """A canonical spec's Python derivation, its candidacy, and its results."""

    strategy_id: str
    version: str
    family: str
    status: str
    long_only: bool
    supported_symbols: tuple[str, ...]
    supported_intervals: tuple[str, ...]
    derived_from_catalog_numbers: tuple[str, ...]
    selection_candidate: bool
    results: tuple[StrategyResult, ...] = ()


class SelectionContext(_Frozen):
    """The frozen selection manifest: criteria and freeze provenance, verbatim."""

    purpose: str
    frozen_at_utc: str
    re_frozen_at_utc: str | None = None
    candidates: tuple[str, ...]
    baselines: tuple[str, ...]
    criteria_for_backtest_validated: tuple[str, ...]
    portfolio_criterion: str | None = None


class SourceHashes(_Frozen):
    """SHA-256 of every input file, so the store is provably a function of them."""

    strategy_registry_yaml: str
    pine_findings_json: str
    selection_manifest_json: str
    results_json: dict[str, str]
    spec_yaml: dict[str, str]


class SKBStore(_Frozen):
    """The complete validated Strategy Knowledge Base."""

    schema_version: int = 1
    corpus_hash: str
    source_hashes: SourceHashes
    pine_script_count: int = Field(ge=0)
    derived_strategy_count: int = Field(ge=0)
    pine_scripts: tuple[PineScriptEntry, ...]
    derived_strategies: tuple[DerivedStrategy, ...]
    selection_context: SelectionContext

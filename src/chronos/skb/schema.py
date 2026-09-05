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

Issue #181 adds two source-measured fields — ``max_concurrent_positions`` and
``timeframe_binding`` — which no corpus input states and which cannot be derived
from the existing categoricals. They are backfilled only for the scripts whose
Pine source was read line by line (see ``skb/source_properties.py``, whose every
claim carries a checked file/line citation); the rest keep the null/unknown
default. Notably ``forensic_flags.pyramiding_gt_0`` does NOT answer the position
question: the corpus's standalone strategies declare ``pyramiding = 3`` yet gate
every entry on ``strategy.position_size == 0``, so the setting buys three
same-bar legs of one scaled entry, not concurrent positions.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    """Port disposition. B1 assigned PORTED (a spec derives it) or UNCLASSIFIED;
    B2 backfills all 42 to PORTED / DEFERRED / BLOCKED_ON / REJECTED, each with a
    machine-readable :class:`DispositionReason`."""

    PORTED = "ported"
    DEFERRED = "deferred"
    BLOCKED_ON = "blocked_on"
    REJECTED = "rejected"
    UNCLASSIFIED = "unclassified"


class DispositionReason(StrEnum):
    """The machine-readable reason a script has its disposition (B2 backfill).

    Derived deterministically from the clean categoricals (integrity status,
    classification, direction) — never from prose. See ``skb/disposition.py``.
    """

    PORTED_TO_SPEC = "ported_to_spec"
    EXECUTABLE_STRATEGY_NOT_YET_PORTED = "executable_strategy_not_yet_ported"
    REQUIRES_REWRITE = "requires_rewrite"
    NON_EXECUTABLE_INDICATOR = "non_executable_indicator"
    STRATEGY_ADDON_NOT_STANDALONE = "strategy_addon_not_standalone"
    NOT_A_STANDALONE_STRATEGY = "not_a_standalone_strategy"
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


class TimeframeBinding(StrEnum):
    """How a script fixes the timeframe it evaluates on (issue #181).

    Kept separate from :class:`Timeframe` because "the script pins no timeframe"
    and "nobody has measured it" are different facts, and a single interval field
    cannot tell them apart — every entry reading ``unknown`` would be ambiguous
    between the two. Measurement of the corpus found the distinction is
    load-bearing: the standalone strategies read ``timeframe.period`` at every
    ``request.security`` call and so inherit whatever chart they run on, which is
    a positive finding, not a gap.

    ``PINNED`` is the only binding under which a concrete :class:`Timeframe`
    interval is meaningful; :class:`PineScriptEntry` enforces that.
    """

    PINNED = "pinned"
    CHART_TF = "chart_tf"
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
    # Controlled-vocabulary tags. B2 backfills disposition for all 42 with a
    # machine-readable reason; timeframe/asset_class/regime_tags remain
    # unknown/empty (corpus states them only in prose — never guessed).
    disposition: Disposition = Disposition.UNCLASSIFIED
    disposition_reason: DispositionReason = DispositionReason.UNCLASSIFIED
    disposition_detail: str = ""
    timeframe: Timeframe = Timeframe.UNKNOWN
    asset_class: AssetClass = AssetClass.UNKNOWN
    regime_tags: tuple[RegimeTag, ...] = ()
    # Source-measured properties (issue #181). Backfilled by the compiler from
    # `skb/source_properties.py` for the scripts whose Pine was actually read;
    # every other entry keeps the null/unknown default — never inferred.
    max_concurrent_positions: int | None = Field(default=None, ge=1)
    timeframe_binding: TimeframeBinding = TimeframeBinding.UNKNOWN
    source_property_citation: str = ""

    @model_validator(mode="after")
    def _interval_only_when_pinned(self) -> PineScriptEntry:
        """A concrete interval is only meaningful when the script pins one."""

        if (
            self.timeframe is not Timeframe.UNKNOWN
            and self.timeframe_binding is not TimeframeBinding.PINNED
        ):
            raise ValueError(
                f"catalog {self.catalog_number}: timeframe={self.timeframe.value} requires "
                f"timeframe_binding=pinned, got {self.timeframe_binding.value}"
            )
        return self


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

    schema_version: int = 2
    corpus_hash: str
    source_hashes: SourceHashes
    pine_script_count: int = Field(ge=0)
    derived_strategy_count: int = Field(ge=0)
    pine_scripts: tuple[PineScriptEntry, ...]
    derived_strategies: tuple[DerivedStrategy, ...]
    selection_context: SelectionContext

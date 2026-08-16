"""Research-only Five-Tool pairing feature and veto plane."""

from chronos.research.features.advisory_export import (
    ADVISORY_EXPORT_SCHEMA,
    project_pairing_frame,
)
from chronos.research.features.alignment import align_companions, latest_companion
from chronos.research.features.breadth import evaluate_breadth
from chronos.research.features.campaign import (
    CAMPAIGN_ID,
    PairingCampaignReport,
    validate_pairing_manifest,
)
from chronos.research.features.companions import (
    companion_certification_requirements,
    require_certified_companion_dataset,
)
from chronos.research.features.compose import PairingComposition, compose_pairing_frames
from chronos.research.features.gold_campaign import (
    GOLD_CAMPAIGN_ID,
    GoldPairingCampaignReport,
    validate_gold_pairing_manifest,
)
from chronos.research.features.intake import (
    INTAKE_SCHEMA,
    REQUIRED_INTAKE_SYMBOLS,
    CertifiedIntakeDeclaration,
    OwnerHoldoutDeclaration,
    intake_requirements,
    open_certified_intake,
    validate_intake_manifest,
)
from chronos.research.features.iv_regime import evaluate_iv_regime
from chronos.research.features.models import (
    GOLD_INERT_FAMILIES,
    GOLD_PAIRING_CAMPAIGN_SCHEMA,
    GOLD_PAIRING_SYMBOL,
    CompanionCatalogDeclaration,
    FeatureFamily,
    FeatureInputError,
    FeaturePolicy,
    FeatureSnapshot,
    IvState,
    PairingFrame,
    TailState,
    UsdState,
    VetoDecision,
    VetoStatus,
)
from chronos.research.features.pairing_replay import PairingReplayResult, replay_pairing
from chronos.research.features.rvol import evaluate_daily_rvol, evaluate_tod_rvol
from chronos.research.features.tail_risk import evaluate_tail_risk
from chronos.research.features.universe import (
    BOOK_SCHEMA,
    COMPANION_ONLY_SYMBOLS,
    RESEARCH_PROXY,
    TRADABLE_SYMBOLS,
    book_digest,
    is_tradable,
    research_series_for,
)
from chronos.research.features.usd_regime import evaluate_usd_regime, require_certified_uup
from chronos.research.features.veto import apply_vetoes, decide_veto

__all__ = [
    "ADVISORY_EXPORT_SCHEMA",
    "BOOK_SCHEMA",
    "CAMPAIGN_ID",
    "COMPANION_ONLY_SYMBOLS",
    "GOLD_CAMPAIGN_ID",
    "GOLD_INERT_FAMILIES",
    "GOLD_PAIRING_CAMPAIGN_SCHEMA",
    "GOLD_PAIRING_SYMBOL",
    "INTAKE_SCHEMA",
    "REQUIRED_INTAKE_SYMBOLS",
    "RESEARCH_PROXY",
    "TRADABLE_SYMBOLS",
    "CertifiedIntakeDeclaration",
    "CompanionCatalogDeclaration",
    "FeatureFamily",
    "FeatureInputError",
    "FeaturePolicy",
    "FeatureSnapshot",
    "GoldPairingCampaignReport",
    "IvState",
    "OwnerHoldoutDeclaration",
    "PairingCampaignReport",
    "PairingComposition",
    "PairingFrame",
    "PairingReplayResult",
    "TailState",
    "UsdState",
    "VetoDecision",
    "VetoStatus",
    "align_companions",
    "apply_vetoes",
    "book_digest",
    "companion_certification_requirements",
    "compose_pairing_frames",
    "decide_veto",
    "evaluate_breadth",
    "evaluate_daily_rvol",
    "evaluate_iv_regime",
    "evaluate_tail_risk",
    "evaluate_tod_rvol",
    "evaluate_usd_regime",
    "intake_requirements",
    "is_tradable",
    "latest_companion",
    "open_certified_intake",
    "project_pairing_frame",
    "replay_pairing",
    "require_certified_companion_dataset",
    "require_certified_uup",
    "research_series_for",
    "validate_gold_pairing_manifest",
    "validate_intake_manifest",
    "validate_pairing_manifest",
]

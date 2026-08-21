"""Blocked pairing-campaign compiler.  Authorizes zero executable trials."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chronos.research.features.intake import validate_intake_manifest
from chronos.research.features.models import (
    COMPANION_CATALOG_SCHEMA,
    FEATURE_POLICY_SCHEMA,
    PAIRING_CAMPAIGN_SCHEMA,
    CompanionCatalogDeclaration,
    FeatureInputError,
    FeaturePolicy,
    canonical_digest,
)
from chronos.research.features.universe import (
    BOOK_SCHEMA,
    COMPANION_ONLY_SYMBOLS,
    RESEARCH_PROXY,
    TRADABLE_SYMBOLS,
    book_digest,
)

CAMPAIGN_ID = "five-tool-pairing-v1-preregistered-001"
HYPOTHESIS_IDS = ("H-PAIR-TAIL", "H-PAIR-RVOL", "H-PAIR-VIX", "H-PAIR-BREADTH")
EXECUTION_BLOCKED = "blocked_until_identity_locks_resolve"
_REQUIRED_ROOT = {
    "blocked_before_first_data_read",
    "campaign_cells",
    "campaign_id",
    "certified_intake",
    "companion_catalog",
    "created_at_utc",
    "execution_state",
    "feature_policy_schema",
    "host_strategy",
    "hypothesis_ids",
    "performance_claims",
    "promotion_authority",
    "purpose",
    "schema_version",
    "tradable_universe",
}


@dataclass(frozen=True, slots=True)
class PairingCampaignReport:
    campaign_id: str
    execution_state: str
    executable_trial_count: int
    blockers: tuple[str, ...]
    policy_digest: str
    manifest_digest: str
    hypothesis_ids: tuple[str, ...]


def validate_pairing_manifest(manifest: Mapping[str, Any]) -> PairingCampaignReport:
    """Validate identity and refuse every data-touching execution path."""

    missing = sorted(_REQUIRED_ROOT.difference(manifest))
    if missing:
        raise FeatureInputError(f"pairing manifest missing keys: {missing}")
    if manifest["schema_version"] != PAIRING_CAMPAIGN_SCHEMA:
        raise FeatureInputError("unsupported pairing campaign schema")
    if manifest["campaign_id"] != CAMPAIGN_ID:
        raise FeatureInputError("unexpected pairing campaign id")
    if manifest["execution_state"] != EXECUTION_BLOCKED:
        raise FeatureInputError("pairing campaign must remain blocked in this slice")
    if manifest["blocked_before_first_data_read"] is not True:
        raise FeatureInputError("pairing campaign must block before the first data read")
    if manifest["performance_claims"] != []:
        raise FeatureInputError("pairing campaign cannot carry performance claims")
    if manifest["promotion_authority"] != "none":
        raise FeatureInputError("pairing campaign has no promotion authority")
    if manifest["feature_policy_schema"] != FEATURE_POLICY_SCHEMA:
        raise FeatureInputError("pairing campaign feature-policy schema mismatch")
    hypotheses = tuple(manifest["hypothesis_ids"])
    if hypotheses != HYPOTHESIS_IDS:
        raise FeatureInputError("pairing campaign hypothesis set is not the preregistered quartet")
    cells = manifest["campaign_cells"]
    if not isinstance(cells, list) or {cell.get("hypothesis_id") for cell in cells} != set(
        HYPOTHESIS_IDS
    ):
        raise FeatureInputError("pairing cells must declare exactly the preregistered hypotheses")
    for cell in cells:
        if cell.get("status") != "pending_resolution":
            raise FeatureInputError(f"{cell.get('hypothesis_id')} is not pending_resolution")
        if cell.get("executable") is not False:
            raise FeatureInputError(f"{cell.get('hypothesis_id')} cannot be marked executable")
    catalog = CompanionCatalogDeclaration(
        schema_version=str(
            manifest["companion_catalog"].get("schema_version", COMPANION_CATALOG_SCHEMA)
        ),
        status=str(manifest["companion_catalog"].get("status", "pending_certified_dataset")),
        required_symbols=tuple(manifest["companion_catalog"].get("required_symbols", ())),
        optional_symbols=tuple(manifest["companion_catalog"].get("optional_symbols", ())),
        dataset_id=manifest["companion_catalog"].get("dataset_id"),
        sha256=manifest["companion_catalog"].get("sha256"),
    )
    host = manifest["host_strategy"]
    if host.get("strategy_id") != "five_tool_confluence_v3_6":
        raise FeatureInputError("pairing host must remain five_tool_confluence_v3_6")
    if host.get("mutates_campaign_002") is not False:
        raise FeatureInputError("pairing campaign must not mutate Five-Tool campaign 002")
    universe = manifest["tradable_universe"]
    if not isinstance(universe, Mapping):
        raise FeatureInputError("tradable_universe must be an object")
    symbols = tuple(universe.get("symbols", ()))
    if symbols != TRADABLE_SYMBOLS:
        raise FeatureInputError("tradable universe must be exactly GLD, IWM, and QQQ")
    if any(name in symbols for name in ("QQQM", "SPY")):
        raise FeatureInputError("QQQM and SPY are not tradable in this book")
    companions = tuple(universe.get("companion_only", ()))
    if companions != COMPANION_ONLY_SYMBOLS:
        raise FeatureInputError("companion-only set must remain RSP, SPY, VIX, VIX3M")
    proxy = universe.get("research_proxy")
    if not isinstance(proxy, Mapping) or dict(proxy) != RESEARCH_PROXY:
        raise FeatureInputError("locked book has no research proxy")
    if universe.get("schema_version") != BOOK_SCHEMA:
        raise FeatureInputError("tradable book schema is locked")
    if universe.get("book_digest") != book_digest():
        raise FeatureInputError("tradable book digest does not match the locked identity")
    intake = manifest["certified_intake"]
    if not isinstance(intake, Mapping):
        raise FeatureInputError("certified_intake must be an object")
    validate_intake_manifest(intake)
    policy = FeaturePolicy()
    if policy.tradable_symbols != symbols:
        raise FeatureInputError("feature policy book drifted from the locked universe")
    blockers = (
        "blocked_until_identity_locks_resolve",
        "companion_catalog_pending_certified_dataset",
        "certified_intake_pending_certified_dataset",
        "tradable_book_locked_gld_iwm_qqq",
        "zero_executable_pairing_trials",
        f"companion_catalog:{catalog.status}",
    )
    return PairingCampaignReport(
        campaign_id=CAMPAIGN_ID,
        execution_state=EXECUTION_BLOCKED,
        executable_trial_count=0,
        blockers=blockers,
        policy_digest=policy.digest,
        manifest_digest=canonical_digest(dict(manifest)),
        hypothesis_ids=hypotheses,
    )

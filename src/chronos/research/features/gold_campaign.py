"""Blocked gold pairing campaign. Authorizes zero executable trials."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chronos.research.features.intake import validate_intake_manifest
from chronos.research.features.models import (
    COMPANION_CATALOG_SCHEMA,
    FEATURE_POLICY_SCHEMA,
    GOLD_INERT_FAMILIES,
    GOLD_PAIRING_CAMPAIGN_SCHEMA,
    GOLD_PAIRING_SYMBOL,
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

GOLD_CAMPAIGN_ID = "five-tool-pairing-gld-v1-preregistered-001"
GOLD_HYPOTHESIS_IDS = ("H-PAIR-GLD-TAIL", "H-PAIR-GLD-RVOL", "H-PAIR-GLD-USD")
EXECUTION_BLOCKED = "blocked_until_identity_locks_resolve"
FUTURE_GOLD_COMPANIONS = ("UUP",)
_REQUIRED_ROOT = {
    "blocked_before_first_data_read",
    "campaign_cells",
    "campaign_id",
    "certified_intake",
    "companion_catalog",
    "created_at_utc",
    "execution_state",
    "feature_policy_schema",
    "future_gold_companions",
    "host_strategy",
    "hypothesis_ids",
    "performance_claims",
    "primary_symbol",
    "promotion_authority",
    "purpose",
    "schema_version",
    "tradable_universe",
}


@dataclass(frozen=True, slots=True)
class GoldPairingCampaignReport:
    campaign_id: str
    execution_state: str
    executable_trial_count: int
    blockers: tuple[str, ...]
    policy_digest: str
    manifest_digest: str
    hypothesis_ids: tuple[str, ...]
    primary_symbol: str
    gold_inert_families: tuple[str, ...]


def validate_gold_pairing_manifest(manifest: Mapping[str, Any]) -> GoldPairingCampaignReport:
    """Validate the gold pairing identity and refuse every data-touching path."""

    missing = sorted(_REQUIRED_ROOT.difference(manifest))
    if missing:
        raise FeatureInputError(f"gold pairing manifest missing keys: {missing}")
    if manifest["schema_version"] != GOLD_PAIRING_CAMPAIGN_SCHEMA:
        raise FeatureInputError("unsupported gold pairing campaign schema")
    if manifest["campaign_id"] != GOLD_CAMPAIGN_ID:
        raise FeatureInputError("unexpected gold pairing campaign id")
    if manifest["execution_state"] != EXECUTION_BLOCKED:
        raise FeatureInputError("gold pairing campaign must remain blocked in this slice")
    if manifest["blocked_before_first_data_read"] is not True:
        raise FeatureInputError("gold pairing campaign must block before the first data read")
    if manifest["performance_claims"] != []:
        raise FeatureInputError("gold pairing campaign cannot carry performance claims")
    if manifest["promotion_authority"] != "none":
        raise FeatureInputError("gold pairing campaign has no promotion authority")
    if manifest["feature_policy_schema"] != FEATURE_POLICY_SCHEMA:
        raise FeatureInputError("gold pairing campaign feature-policy schema mismatch")
    if manifest["primary_symbol"] != GOLD_PAIRING_SYMBOL:
        raise FeatureInputError("gold pairing primary must remain GLD")
    hypotheses = tuple(manifest["hypothesis_ids"])
    if hypotheses != GOLD_HYPOTHESIS_IDS:
        raise FeatureInputError("gold pairing hypothesis set is not the preregistered trio")
    cells = manifest["campaign_cells"]
    if not isinstance(cells, list) or {cell.get("hypothesis_id") for cell in cells} != set(
        GOLD_HYPOTHESIS_IDS
    ):
        raise FeatureInputError(
            "gold pairing cells must declare exactly the preregistered hypotheses"
        )
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
        raise FeatureInputError("gold pairing host must remain five_tool_confluence_v3_6")
    if host.get("mutates_campaign_002") is not False:
        raise FeatureInputError("gold pairing campaign must not mutate Five-Tool campaign 002")
    universe = manifest["tradable_universe"]
    if not isinstance(universe, Mapping):
        raise FeatureInputError("tradable_universe must be an object")
    symbols = tuple(universe.get("symbols", ()))
    if symbols != TRADABLE_SYMBOLS:
        raise FeatureInputError("tradable universe must be exactly GLD, IWM, and QQQ")
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
    future = manifest["future_gold_companions"]
    if not isinstance(future, Mapping):
        raise FeatureInputError("future_gold_companions must be an object")
    if tuple(future.get("symbols", ())) != FUTURE_GOLD_COMPANIONS:
        raise FeatureInputError("future gold companions are locked to UUP")
    if future.get("status") != "pending_certified_dataset":
        raise FeatureInputError("future gold companions stay pending_certified_dataset")
    if future.get("downloads") is not False:
        raise FeatureInputError("gold pairing does not download UUP or other USD series")
    if future.get("dataset_id") is not None or future.get("sha256") is not None:
        raise FeatureInputError("future gold companion identities remain unset")
    policy = FeaturePolicy()
    gld_families = policy.enabled_families(GOLD_PAIRING_SYMBOL)
    equity_families = policy.enabled_families("QQQ")
    if set(gld_families) & set(GOLD_INERT_FAMILIES):
        raise FeatureInputError("GLD pairing must not enable equity VIX or breadth")
    if set(gld_families) != set(equity_families) - set(GOLD_INERT_FAMILIES):
        raise FeatureInputError("GLD pairing families drifted from the locked identity")
    blockers = (
        "blocked_until_identity_locks_resolve",
        "companion_catalog_pending_certified_dataset",
        "certified_intake_pending_certified_dataset",
        "future_gold_companions_pending_uup",
        "gold_equity_weather_inert",
        "zero_executable_pairing_trials",
        f"companion_catalog:{catalog.status}",
    )
    return GoldPairingCampaignReport(
        campaign_id=GOLD_CAMPAIGN_ID,
        execution_state=EXECUTION_BLOCKED,
        executable_trial_count=0,
        blockers=blockers,
        policy_digest=policy.digest,
        manifest_digest=canonical_digest(dict(manifest)),
        hypothesis_ids=hypotheses,
        primary_symbol=GOLD_PAIRING_SYMBOL,
        gold_inert_families=tuple(family.value for family in GOLD_INERT_FAMILIES),
    )

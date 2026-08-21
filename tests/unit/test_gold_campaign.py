from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronos.research.features.gold_campaign import (
    GOLD_CAMPAIGN_ID,
    GOLD_HYPOTHESIS_IDS,
    validate_gold_pairing_manifest,
)
from chronos.research.features.models import FeatureInputError
from chronos.research.features.universe import book_digest

_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "research"
    / "five_tool_pairing_gld_v1_campaign_manifest.json"
)


def test_checked_gold_manifest_authorizes_zero_trials() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    report = validate_gold_pairing_manifest(payload)
    assert report.campaign_id == GOLD_CAMPAIGN_ID
    assert report.executable_trial_count == 0
    assert report.hypothesis_ids == GOLD_HYPOTHESIS_IDS
    assert report.primary_symbol == "GLD"
    assert report.gold_inert_families == ("iv_regime", "breadth")
    assert "zero_executable_pairing_trials" in report.blockers
    assert "gold_equity_weather_inert" in report.blockers
    assert "future_gold_companions_pending_uup" in report.blockers
    assert payload["tradable_universe"]["book_digest"] == book_digest()
    assert payload["future_gold_companions"]["downloads"] is False
    usd = next(
        cell for cell in payload["campaign_cells"] if cell["hypothesis_id"] == "H-PAIR-GLD-USD"
    )
    assert usd["neighbor_axis"] == "usd_slope_lookback"
    assert usd["executable"] is False


def test_gold_manifest_refuses_ready_state_or_downloaded_uup() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["execution_state"] = "ready_for_certified_research"
    with pytest.raises(FeatureInputError, match="must remain blocked"):
        validate_gold_pairing_manifest(payload)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["future_gold_companions"]["downloads"] = True
    with pytest.raises(FeatureInputError, match="does not download"):
        validate_gold_pairing_manifest(payload)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["primary_symbol"] = "QQQ"
    with pytest.raises(FeatureInputError, match="GLD"):
        validate_gold_pairing_manifest(payload)

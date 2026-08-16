from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronos.research.features.campaign import (
    CAMPAIGN_ID,
    HYPOTHESIS_IDS,
    validate_pairing_manifest,
)
from chronos.research.features.models import FeatureInputError
from chronos.research.features.universe import book_digest

_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "research"
    / "five_tool_pairing_v1_campaign_manifest.json"
)


def test_checked_pairing_manifest_authorizes_zero_trials() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    report = validate_pairing_manifest(payload)
    assert report.campaign_id == CAMPAIGN_ID
    assert report.executable_trial_count == 0
    assert report.hypothesis_ids == HYPOTHESIS_IDS
    assert report.execution_state == "blocked_until_identity_locks_resolve"
    assert "zero_executable_pairing_trials" in report.blockers
    assert "certified_intake_pending_certified_dataset" in report.blockers
    assert "tradable_book_locked_gld_iwm_qqq" in report.blockers
    assert payload["tradable_universe"]["symbols"] == ["GLD", "IWM", "QQQ"]
    assert payload["tradable_universe"]["book_digest"] == book_digest()


def test_pairing_manifest_refuses_qqqm_or_a_forged_book_digest() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["tradable_universe"]["symbols"] = ["GLD", "IWM", "QQQM"]
    with pytest.raises(FeatureInputError, match="GLD, IWM, and QQQ"):
        validate_pairing_manifest(payload)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["tradable_universe"]["book_digest"] = "0" * 64
    with pytest.raises(FeatureInputError, match="book digest"):
        validate_pairing_manifest(payload)


def test_pairing_manifest_refuses_ready_state() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["execution_state"] = "ready_for_certified_research"
    with pytest.raises(FeatureInputError, match="must remain blocked"):
        validate_pairing_manifest(payload)


def test_pairing_manifest_refuses_missing_or_ready_intake() -> None:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    del payload["certified_intake"]
    with pytest.raises(FeatureInputError, match="certified_intake"):
        validate_pairing_manifest(payload)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["certified_intake"]["status"] = "ready"
    with pytest.raises(FeatureInputError, match="pending_certified_dataset"):
        validate_pairing_manifest(payload)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    payload["certified_intake"]["downloads"] = True
    with pytest.raises(FeatureInputError, match="does not download"):
        validate_pairing_manifest(payload)

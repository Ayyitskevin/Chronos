from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from chronos.research.features.intake import (
    INTAKE_STATUS,
    REQUIRED_INTAKE_SYMBOLS,
    CertifiedIntakeDeclaration,
    OwnerHoldoutDeclaration,
    intake_requirements,
    open_certified_intake,
    validate_intake_manifest,
)
from chronos.research.features.models import FeatureInputError
from chronos.research.features.universe import book_digest

_INTAKE = (
    Path(__file__).resolve().parent.parent.parent
    / "research"
    / "five_tool_certified_intake_v1.json"
)
_CAMPAIGN = (
    Path(__file__).resolve().parent.parent.parent
    / "research"
    / "five_tool_pairing_v1_campaign_manifest.json"
)


def _valid_holdout() -> dict[str, object]:
    return {
        "name": "book-clean-holdout-reserved",
        "start": "2024-07-01",
        "end": "2025-12-31",
        "symbols": list(REQUIRED_INTAKE_SYMBOLS),
        "reason": "reserved before certified bytes exist",
    }


def test_checked_intake_stays_pending_and_matches_the_campaign() -> None:
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    declaration = validate_intake_manifest(payload)
    assert declaration.status == INTAKE_STATUS
    assert declaration.owner_holdout is None
    assert declaration.dataset_id is None
    assert payload["downloads"] is False
    assert payload["book_digest"] == book_digest()
    campaign = json.loads(_CAMPAIGN.read_text(encoding="utf-8"))
    assert campaign["certified_intake"]["schema_version"] == payload["schema_version"]
    assert campaign["certified_intake"]["book_digest"] == payload["book_digest"]
    assert campaign["certified_intake"]["required_symbols"] == payload["required_symbols"]


def test_intake_refuses_downloads_ready_state_and_forged_identities() -> None:
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    payload["downloads"] = True
    with pytest.raises(FeatureInputError, match="does not download"):
        validate_intake_manifest(payload)
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    payload["status"] = "ready"
    with pytest.raises(FeatureInputError, match="pending_certified_dataset"):
        validate_intake_manifest(payload)
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    payload["sha256"] = "a" * 64
    with pytest.raises(FeatureInputError, match="remain unset"):
        validate_intake_manifest(payload)
    with pytest.raises(FeatureInputError, match="does not download"):
        open_certified_intake()


def test_owner_holdout_may_be_declared_but_still_cannot_open_bytes() -> None:
    holdout = OwnerHoldoutDeclaration(
        name="book-clean-holdout-reserved",
        start=date(2024, 7, 1),
        end=date(2025, 12, 31),
        symbols=REQUIRED_INTAKE_SYMBOLS,
    )
    declared = CertifiedIntakeDeclaration(owner_holdout=holdout)
    assert declared.owner_holdout is not None
    assert declared.status == INTAKE_STATUS
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    payload["owner_holdout"] = _valid_holdout()
    accepted = validate_intake_manifest(payload)
    assert accepted.owner_holdout is not None
    with pytest.raises(FeatureInputError, match="does not download"):
        open_certified_intake(payload)


def test_owner_holdout_cannot_reuse_the_consumed_qqq_window() -> None:
    with pytest.raises(FeatureInputError, match="consumed QQQ"):
        OwnerHoldoutDeclaration(
            name="reuse-burned",
            start=date(2022, 1, 1),
            end=date(2024, 1, 10),
            symbols=REQUIRED_INTAKE_SYMBOLS,
        )
    with pytest.raises(FeatureInputError, match="consumed QQQ"):
        OwnerHoldoutDeclaration(
            name="overlap-burned",
            start=date(2023, 6, 1),
            end=date(2024, 6, 1),
            symbols=REQUIRED_INTAKE_SYMBOLS,
        )
    with pytest.raises(FeatureInputError, match="missing"):
        OwnerHoldoutDeclaration(
            name="qqq-only",
            start=date(2024, 7, 1),
            end=date(2025, 12, 31),
            symbols=("QQQ",),
        )
    with pytest.raises(FeatureInputError, match="empty scope"):
        OwnerHoldoutDeclaration(
            name="empty-scope",
            start=date(2024, 7, 1),
            end=date(2025, 12, 31),
            symbols=(),
        )
    payload = json.loads(_INTAKE.read_text(encoding="utf-8"))
    payload["burned_holdouts"][0]["status"] = "clean"
    with pytest.raises(FeatureInputError, match="cannot be rewritten"):
        validate_intake_manifest(payload)


def test_intake_requirements_name_the_locked_book_and_no_dataset() -> None:
    requirements = intake_requirements()
    assert requirements["required_symbols"] == list(REQUIRED_INTAKE_SYMBOLS)
    assert requirements["owner_holdout"] is None
    assert requirements["dataset_id"] is None
    assert "QQQM" not in requirements["required_symbols"]

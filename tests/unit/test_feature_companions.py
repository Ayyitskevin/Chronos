from __future__ import annotations

import pytest

from chronos.research.features.companions import (
    companion_certification_requirements,
    require_certified_companion_dataset,
)
from chronos.research.features.models import CompanionCatalogDeclaration, FeatureInputError


def test_companion_requirements_stay_pending_and_do_not_download() -> None:
    requirements = companion_certification_requirements()
    assert requirements["status"] == "pending_certified_dataset"
    assert requirements["downloads"] is False
    assert requirements["dataset_id"] is None
    assert requirements["sha256"] is None
    assert "VIX" in requirements["required_symbols"]
    assert "RSP" in requirements["required_symbols"]
    assert requirements["tradable_symbols"] == ["GLD", "IWM", "QQQ"]
    assert requirements["research_proxy"] == {}
    assert "QQQM" not in requirements["tradable_symbols"]
    assert "SPY" not in requirements["tradable_symbols"]


def test_certified_companion_loader_is_blocked_even_with_a_forged_catalog() -> None:
    with pytest.raises(FeatureInputError, match="pending_certified_dataset"):
        require_certified_companion_dataset()
    with pytest.raises(FeatureInputError, match="cannot be marked ready"):
        CompanionCatalogDeclaration(status="ready", dataset_id="forged", sha256="a" * 64)
    with pytest.raises(FeatureInputError, match="pending_certified_dataset"):
        require_certified_companion_dataset({"status": "ready", "sha256": "a" * 64})

"""Certified companion-data contract. Delegates to the locked intake plane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from chronos.research.features.intake import (
    INTAKE_SCHEMA,
    intake_requirements,
    open_certified_intake,
)
from chronos.research.features.models import CompanionCatalogDeclaration

COMPANION_CERTIFICATION_SCHEMA = INTAKE_SCHEMA


def companion_certification_requirements() -> dict[str, Any]:
    """What owner-certified companion bytes must declare. Not a dataset."""

    return intake_requirements()


def require_certified_companion_dataset(
    declaration: CompanionCatalogDeclaration | Mapping[str, Any] | None = None,
) -> None:
    """Refuse every attempt to treat companions as certified."""

    del declaration
    open_certified_intake()

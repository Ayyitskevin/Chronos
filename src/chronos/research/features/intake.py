"""Certified book-and-companion intake. Owner bytes only; never a download.

This is the pre-catalog contract. It names the required overlapping series,
freezes the consumed QQQ holdout so it cannot be reused, and accepts an owner
holdout declaration only when that window does not overlap the burned range.
It does not authenticate a ``CertifiedDatasetCatalog``, open files, or fetch
market data. A later slice may accept owner-supplied catalog identities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from chronos.research.features.models import FeatureInputError, canonical_digest
from chronos.research.features.universe import (
    COMPANION_ONLY_SYMBOLS,
    OPTIONAL_INTERNALS,
    RESEARCH_PROXY,
    TRADABLE_SYMBOLS,
    book_digest,
)

INTAKE_SCHEMA = "chronos-five-tool-certified-intake-v1"
INTAKE_STATUS = "pending_certified_dataset"
REQUIRED_INTAKE_SYMBOLS = tuple(sorted(set(TRADABLE_SYMBOLS) | set(COMPANION_ONLY_SYMBOLS)))
BURNED_HOLDOUT_NAME = "qqq-2022-01-2024-01-consumed"
BURNED_HOLDOUT_START = date(2022, 1, 1)
BURNED_HOLDOUT_END = date(2024, 1, 10)


def burned_holdout_record() -> dict[str, object]:
    """The QQQ window that is already consumed. It is not a clean holdout."""

    return {
        "name": BURNED_HOLDOUT_NAME,
        "start": BURNED_HOLDOUT_START.isoformat(),
        "end": BURNED_HOLDOUT_END.isoformat(),
        "symbols": ["QQQ"],
        "status": "consumed",
        "reason": "declared consumed in FIVE_TOOL_RESEARCH_HYPOTHESES; not a clean holdout",
    }


def intake_requirements() -> dict[str, Any]:
    """What an owner-certified release must declare. Not a dataset."""

    return {
        "schema_version": INTAKE_SCHEMA,
        "status": INTAKE_STATUS,
        "downloads": False,
        "bar_status": "closed",
        "timestamp_timezone": "UTC",
        "source": "owner_certified_dataset_catalog",
        "book_digest": book_digest(),
        "required_symbols": list(REQUIRED_INTAKE_SYMBOLS),
        "optional_symbols": list(OPTIONAL_INTERNALS),
        "tradable_symbols": list(TRADABLE_SYMBOLS),
        "companion_only": list(COMPANION_ONLY_SYMBOLS),
        "research_proxy": dict(RESEARCH_PROXY),
        "burned_holdouts": [burned_holdout_record()],
        "owner_holdout": None,
        "dataset_id": None,
        "sha256": None,
        "catalog_id": None,
    }


@dataclass(frozen=True, slots=True)
class OwnerHoldoutDeclaration:
    """A clean holdout named before any certified bytes are readable."""

    name: str
    start: date
    end: date
    symbols: tuple[str, ...]
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise FeatureInputError("owner holdout name is required")
        if self.end < self.start:
            raise FeatureInputError("owner holdout end precedes start")
        normalized = tuple(item.strip().upper() for item in self.symbols)
        if not normalized:
            raise FeatureInputError(
                "owner holdout must name its symbols; an empty scope would also "
                "embargo the consumed QQQ window"
            )
        if any(not item for item in normalized):
            raise FeatureInputError("owner holdout symbols must be non-empty")
        if len(normalized) != len(set(normalized)):
            raise FeatureInputError("owner holdout symbols must be unique")
        missing = [item for item in REQUIRED_INTAKE_SYMBOLS if item not in normalized]
        if missing:
            raise FeatureInputError(
                "owner holdout must cover every required intake symbol before "
                f"bytes are readable; missing {missing}"
            )
        if _overlaps_burned(normalized, self.start, self.end):
            raise FeatureInputError(
                "owner holdout overlaps the consumed QQQ 2022-01 through 2024-01 "
                "window; that range is not a clean holdout"
            )
        object.__setattr__(self, "symbols", normalized)

    def as_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols),
            "reason": self.reason,
            "status": "declared_pending_bytes",
        }


@dataclass(frozen=True, slots=True)
class CertifiedIntakeDeclaration:
    """Blocked intake identity. Identities stay unset until owner certification."""

    schema_version: str = INTAKE_SCHEMA
    status: str = INTAKE_STATUS
    downloads: bool = False
    book_digest_value: str = ""
    required_symbols: tuple[str, ...] = REQUIRED_INTAKE_SYMBOLS
    optional_symbols: tuple[str, ...] = OPTIONAL_INTERNALS
    burned_holdouts: tuple[dict[str, object], ...] = ()
    owner_holdout: OwnerHoldoutDeclaration | None = None
    dataset_id: str | None = None
    sha256: str | None = None
    catalog_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != INTAKE_SCHEMA:
            raise FeatureInputError(f"unsupported certified-intake schema: {self.schema_version}")
        if self.status != INTAKE_STATUS:
            raise FeatureInputError("certified intake stays pending_certified_dataset")
        if self.downloads:
            raise FeatureInputError("certified intake does not download market data")
        expected_digest = book_digest()
        digest = self.book_digest_value or expected_digest
        if digest != expected_digest:
            raise FeatureInputError("certified intake book digest does not match the locked book")
        object.__setattr__(self, "book_digest_value", expected_digest)
        if self.required_symbols != REQUIRED_INTAKE_SYMBOLS:
            raise FeatureInputError("certified intake required symbols are locked")
        if self.optional_symbols != OPTIONAL_INTERNALS:
            raise FeatureInputError("certified intake optional symbols are locked")
        if self.dataset_id is not None or self.sha256 is not None or self.catalog_id is not None:
            raise FeatureInputError(
                "certified intake identities remain unset until owner certification"
            )
        burned = self.burned_holdouts or (burned_holdout_record(),)
        if burned != (burned_holdout_record(),):
            raise FeatureInputError("consumed QQQ holdout record is locked and cannot be rewritten")
        object.__setattr__(self, "burned_holdouts", burned)

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "book_digest": self.book_digest_value,
                "burned_holdouts": list(self.burned_holdouts),
                "catalog_id": self.catalog_id,
                "dataset_id": self.dataset_id,
                "downloads": self.downloads,
                "optional_symbols": self.optional_symbols,
                "owner_holdout": (
                    None if self.owner_holdout is None else self.owner_holdout.as_record()
                ),
                "required_symbols": self.required_symbols,
                "schema_version": self.schema_version,
                "sha256": self.sha256,
                "status": self.status,
            }
        )


def owner_holdout_from_mapping(payload: Mapping[str, Any]) -> OwnerHoldoutDeclaration:
    return OwnerHoldoutDeclaration(
        name=str(payload["name"]),
        start=date.fromisoformat(str(payload["start"])),
        end=date.fromisoformat(str(payload["end"])),
        symbols=tuple(str(item) for item in payload.get("symbols", ())),
        reason=str(payload.get("reason", "")),
    )


def validate_intake_manifest(manifest: Mapping[str, Any]) -> CertifiedIntakeDeclaration:
    """Validate one intake document. Never opens or certifies bytes."""

    required = {
        "book_digest",
        "burned_holdouts",
        "catalog_id",
        "dataset_id",
        "downloads",
        "owner_holdout",
        "required_symbols",
        "schema_version",
        "sha256",
        "status",
    }
    missing = sorted(required.difference(manifest))
    if missing:
        raise FeatureInputError(f"certified intake missing keys: {missing}")
    holdout_payload = manifest["owner_holdout"]
    holdout = None if holdout_payload is None else owner_holdout_from_mapping(holdout_payload)
    return CertifiedIntakeDeclaration(
        schema_version=str(manifest["schema_version"]),
        status=str(manifest["status"]),
        downloads=bool(manifest["downloads"]),
        book_digest_value=str(manifest["book_digest"]),
        required_symbols=tuple(manifest["required_symbols"]),
        optional_symbols=tuple(manifest.get("optional_symbols", OPTIONAL_INTERNALS)),
        burned_holdouts=tuple(manifest["burned_holdouts"]),
        owner_holdout=holdout,
        dataset_id=manifest["dataset_id"],
        sha256=manifest["sha256"],
        catalog_id=manifest["catalog_id"],
    )


def open_certified_intake(
    declaration: CertifiedIntakeDeclaration | Mapping[str, Any] | None = None,
) -> None:
    """Refuse every attempt to treat intake as certified or to read bytes."""

    if isinstance(declaration, Mapping):
        validate_intake_manifest(declaration)
    elif declaration is not None and not isinstance(declaration, CertifiedIntakeDeclaration):
        raise FeatureInputError("certified intake declaration is not a recognized object")
    raise FeatureInputError(
        "certified intake stays pending_certified_dataset until the owner "
        "declares a clean holdout and supplies certified overlapping bytes; "
        "this slice does not download, open, or certify market data"
    )


def _overlaps_burned(symbols: tuple[str, ...], start: date, end: date) -> bool:
    if "QQQ" not in symbols:
        return False
    return start <= BURNED_HOLDOUT_END and end >= BURNED_HOLDOUT_START

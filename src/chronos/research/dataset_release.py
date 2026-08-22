"""Freeze a certified export into immutable, content-addressed partitions.

Phase 3 requires a "clean/seen/burned holdout map complete and content-addressed" and
"immutable dataset versions". This module turns a certified export plus that map into
the artifact the rest of the research plane already knows how to consume: the exact
``chronos-certified-data-catalog-v1`` document ``research.certified_data`` authenticates,
over partition files whose bytes are their own identity.

Three rules fall out of the catalog's own contract and are enforced here rather than
discovered later.

**A partition may not straddle a classification boundary.** The catalog refuses to let
one path be both ordinary and holdout, and rightly — a file that is half untouched
holdout cannot be handed to ordinary research with the holdout half withheld. So the
release splits each symbol at its window edges and writes one file per window.

**The map must tile the range, exactly once.** A gap means some dates have no declared
status, and undeclared is precisely how a holdout gets read by accident; an overlap
means a date has two. Both refuse.

**``data_version`` is the content digest.** The catalog requires ``data_version ==
sha256`` — D-27's bytes-are-the-label applied to data. Re-freezing identical bytes
reproduces an identical manifest and an identical release digest, so a dataset version
is a fact about content rather than a label someone typed.

Classification mapping, stated once because it is the only judgement call here:
``CLEAN`` is the untouched holdout and becomes catalog ``holdout``; ``SEEN`` and
``BURNED`` become ``ordinary``, because both have already been exposed to research and
neither can serve as an untouched final test again. The distinction between them is
preserved in the release document — burned carries *why* it was consumed, which is the
record that stops it being re-proposed as a holdout later.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.research.certification import CertificationReport
from chronos.research.certified_data import CATALOG_SCHEMA_VERSION, DataClassification

# v2 added the per-release interval and the hourly partition schema. Bumped while
# zero production release digests existed; see certification.py's matching note.
RELEASE_SCHEMA_VERSION = "chronos-dataset-release-v2"

_BARS_HEADER = "date,open,high,low,close,volume"
_HOURLY_BARS_HEADER = "timestamp_utc,session_date,open,high,low,close,volume"


class DatasetReleaseError(RuntimeError):
    """A release could not be frozen as declared."""


class HoldoutStatus(StrEnum):
    """How much of research has already seen a span of dates."""

    CLEAN = "clean"
    SEEN = "seen"
    BURNED = "burned"

    @property
    def catalog_classification(self) -> DataClassification:
        if self is HoldoutStatus.CLEAN:
            return DataClassification.HOLDOUT
        return DataClassification.ORDINARY


@dataclass(frozen=True, slots=True)
class HoldoutSpan:
    """One inclusive date range of one symbol, with its declared status."""

    symbol: str
    name: str
    start: date
    end: date
    status: HoldoutStatus
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("HoldoutSpan.symbol must be non-empty")
        if not self.name:
            raise ValueError("HoldoutSpan.name must be non-empty")
        if self.start > self.end:
            raise ValueError(f"{self.symbol}/{self.name}: start must not follow end")
        if self.status is HoldoutStatus.BURNED and not self.reason.strip():
            raise ValueError(
                f"{self.symbol}/{self.name}: a burned span must record why it was consumed"
            )

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": str(self.status),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PartitionRelease:
    """One frozen partition file and the identity its bytes give it."""

    dataset_id: str
    partition: str
    symbol: str
    span: HoldoutSpan
    relative_path: str
    sha256: str
    byte_count: int
    row_count: int

    def catalog_entry(self, *, source_id: str, source_receipt_sha256: str) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "partition": self.partition,
            "data_version": self.sha256,
            "source_id": source_id,
            "source_receipt_sha256": source_receipt_sha256,
            "classification": str(self.span.status.catalog_classification),
            "path": self.relative_path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class DatasetRelease:
    """The frozen release: its partitions, its map, and the digest that names it."""

    interval: str
    dataset_id: str
    catalog_id: str
    source_id: str
    source_receipt_sha256: str
    partitions: tuple[PartitionRelease, ...]
    spans: tuple[HoldoutSpan, ...]
    certification_digest: str

    def catalog_manifest(self) -> dict[str, Any]:
        """The exact document ``CertifiedDatasetCatalog.from_manifest`` authenticates."""

        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_id": self.catalog_id,
            "entries": [
                partition.catalog_entry(
                    source_id=self.source_id,
                    source_receipt_sha256=self.source_receipt_sha256,
                )
                for partition in self.partitions
            ],
        }

    def catalog_manifest_bytes(self) -> bytes:
        return json.dumps(
            self.catalog_manifest(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def catalog_manifest_sha256(self) -> str:
        """The out-of-band trusted digest a reader must be given to open this catalog."""

        return hashlib.sha256(self.catalog_manifest_bytes()).hexdigest()

    def release_document(self) -> dict[str, Any]:
        return {
            "schema_version": RELEASE_SCHEMA_VERSION,
            "interval": self.interval,
            "dataset_id": self.dataset_id,
            "catalog_id": self.catalog_id,
            "source_id": self.source_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "certification_digest": self.certification_digest,
            "catalog_manifest_sha256": self.catalog_manifest_sha256,
            "holdout_map": [span.to_mapping() for span in self.spans],
            "partitions": [
                {
                    "partition": partition.partition,
                    "symbol": partition.symbol,
                    "status": str(partition.span.status),
                    "classification": str(partition.span.status.catalog_classification),
                    "path": partition.relative_path,
                    "sha256": partition.sha256,
                    "byte_count": partition.byte_count,
                    "row_count": partition.row_count,
                }
                for partition in self.partitions
            ],
        }

    def release_document_bytes(self) -> bytes:
        return json.dumps(
            self.release_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def release_digest(self) -> str:
        """One digest over certification, catalog, and map — the thing to hand over."""

        return hashlib.sha256(self.release_document_bytes()).hexdigest()


# --------------------------------------------------------------------------- internals


def _render_partition(series: BarSeries, span: HoldoutSpan) -> tuple[str, int]:
    """Render the bars inside ``span`` in the store's own CSV shape for the interval.

    Span membership keys on ``session_date`` for BOTH intervals, deliberately: a
    holdout boundary can therefore never split a trading session mid-day, and
    every bar of one session always lands in one classification — including a
    bar whose UTC close crossed midnight, which is why the timestamp is never
    the membership key. The hourly shape carries the real close timestamps; the
    daily renderer through the hourly schema (or vice versa) is unrepresentable
    by construction.
    """

    if series.interval not in (BarInterval.DAY_1, BarInterval.HOUR_1):
        # The daily branch below would render any other interval through the
        # date-keyed schema, discarding timestamps into duplicate date rows.
        # Certification refuses minutes today; refusing here too means a later
        # vocabulary widening cannot silently mint an unfaithful release.
        raise DatasetReleaseError(
            f"{series.symbol}: no partition schema for {series.interval} — a release "
            "may only freeze intervals whose bytes can faithfully round-trip"
        )
    hourly = series.interval is BarInterval.HOUR_1
    lines = [_HOURLY_BARS_HEADER if hourly else _BARS_HEADER]
    rows = 0
    for bar in series.bars:
        if not span.contains(bar.session_date):
            continue
        if hourly:
            lines.append(
                f"{bar.timestamp_utc.isoformat()},{bar.session_date.isoformat()},"
                f"{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}"
            )
        else:
            lines.append(
                f"{bar.session_date.isoformat()},{bar.open},{bar.high},"
                f"{bar.low},{bar.close},{bar.volume}"
            )
        rows += 1
    return "\n".join(lines) + "\n", rows


def _require_tiling(spans: Sequence[HoldoutSpan], symbol: str, start: date, end: date) -> None:
    """Every date in ``[start, end]`` is claimed by exactly one span."""

    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    if not ordered:
        raise DatasetReleaseError(f"{symbol}: no holdout map declared for the release window")
    if ordered[0].start > start:
        raise DatasetReleaseError(
            f"{symbol}: holdout map starts {ordered[0].start.isoformat()}, leaving "
            f"{start.isoformat()} undeclared — undeclared is how a holdout gets read"
        )
    if ordered[-1].end < end:
        raise DatasetReleaseError(
            f"{symbol}: holdout map ends {ordered[-1].end.isoformat()}, leaving "
            f"{end.isoformat()} undeclared — undeclared is how a holdout gets read"
        )
    previous = ordered[0]
    for span in ordered[1:]:
        if span.start <= previous.end:
            raise DatasetReleaseError(
                f"{symbol}: spans {previous.name!r} and {span.name!r} overlap; a date "
                "cannot hold two classifications"
            )
        if (span.start - previous.end).days != 1:
            raise DatasetReleaseError(
                f"{symbol}: gap between {previous.name!r} and {span.name!r} leaves "
                f"{(previous.end).isoformat()}..{span.start.isoformat()} undeclared"
            )
        previous = span


def freeze_release(
    *,
    dataset_id: str,
    catalog_id: str,
    source_id: str,
    source_receipt_sha256: str,
    certification: CertificationReport,
    series_by_symbol: Mapping[str, BarSeries],
    spans: Sequence[HoldoutSpan],
    output_root: Path,
) -> DatasetRelease:
    """Write the partition files and return the release that names them.

    Refuses an export that did not certify. A release is the artifact that says "these
    bytes passed the gates"; minting one over a failed certification would make the
    digest a label rather than evidence.
    """

    if not certification.certified:
        blocking = ", ".join(sorted({str(finding.kind) for finding in certification.findings}))
        raise DatasetReleaseError(
            f"refusing to freeze a release over a NOT_CERTIFIED export ({blocking})"
        )
    if not spans:
        raise DatasetReleaseError("a release requires a complete holdout map")

    by_symbol: dict[str, list[HoldoutSpan]] = {}
    for span in spans:
        by_symbol.setdefault(span.symbol, []).append(span)

    covered = {entry.symbol for entry in certification.coverage}
    declared = set(by_symbol)
    if declared != covered:
        raise DatasetReleaseError(
            "the holdout map and the certified export describe different symbols: "
            f"map-only={sorted(declared - covered)}, certified-only={sorted(covered - declared)}"
        )

    partitions: list[PartitionRelease] = []
    output_root.mkdir(parents=True, exist_ok=True)

    for symbol in sorted(by_symbol):
        series = series_by_symbol.get(symbol)
        if series is None or len(series) == 0:
            raise DatasetReleaseError(f"{symbol}: certified export supplied no bars to freeze")
        if series.interval is not certification.interval:
            raise DatasetReleaseError(
                f"{symbol}: series interval {series.interval} does not match the "
                f"certification's {certification.interval} — a release freezes exactly "
                "what was judged, never a lookalike"
            )
        symbol_spans = by_symbol[symbol]
        _require_tiling(
            symbol_spans,
            symbol,
            series.bars[0].session_date,
            series.bars[-1].session_date,
        )
        seen_names: set[str] = set()
        for span in sorted(symbol_spans, key=lambda item: item.start):
            if span.name in seen_names:
                raise DatasetReleaseError(f"{symbol}: duplicate span name {span.name!r}")
            seen_names.add(span.name)
            body, rows = _render_partition(series, span)
            payload = body.encode("utf-8")
            relative_path = f"{symbol}/{span.name}.csv"
            target = output_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            partitions.append(
                PartitionRelease(
                    dataset_id=dataset_id,
                    partition=f"{symbol}:{span.name}",
                    symbol=symbol,
                    span=span,
                    relative_path=relative_path,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    byte_count=len(payload),
                    row_count=rows,
                )
            )

    digests = {partition.sha256 for partition in partitions}
    if len(digests) != len(partitions):
        raise DatasetReleaseError(
            "two partitions have identical bytes, so one content digest would carry two "
            "classifications — the catalog refuses that and so does this"
        )

    return DatasetRelease(
        interval=str(certification.interval),
        dataset_id=dataset_id,
        catalog_id=catalog_id,
        source_id=source_id,
        source_receipt_sha256=source_receipt_sha256,
        partitions=tuple(partitions),
        spans=tuple(sorted(spans, key=lambda span: (span.symbol, span.start))),
        certification_digest=certification.certification_digest,
    )


__all__ = [
    "RELEASE_SCHEMA_VERSION",
    "DatasetRelease",
    "DatasetReleaseError",
    "HoldoutSpan",
    "HoldoutStatus",
    "PartitionRelease",
    "freeze_release",
]

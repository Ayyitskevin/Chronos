"""A release is only real if the reader that has been waiting for it can open it.

``research.certified_data`` has authenticated a ``chronos-certified-data-catalog-v1``
document since C3, and until now nothing in the repository produced one — the only
manifests in existence were hand-built inside tests. The round-trip case below is
therefore the load-bearing one: freeze a release, hand its own digest back as the
out-of-band trusted SHA-256, and read ordinary bytes through the broker seam.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries, BarStatus
from chronos.research.certification import (
    NoCorporateActionAttestation,
    SymbolWindow,
    certify_export,
)
from chronos.research.certified_data import (
    CertifiedDataRequest,
    CertifiedDatasetCatalog,
    DataClassification,
    HoldoutAccessRefused,
)
from chronos.research.dataset_release import (
    DatasetReleaseError,
    HoldoutSpan,
    HoldoutStatus,
    freeze_release,
)
from chronos.research.session_calendar import SessionCalendar

_CALENDAR = SessionCalendar()
_START = date(2024, 1, 2)
_SPLIT = date(2024, 3, 1)  # where clean holdout begins
_END = date(2024, 5, 31)
_SOURCE_RECEIPT = "b" * 64


def _series(symbol: str = "SPY") -> BarSeries:
    days = _CALENDAR.sessions(_START, _END)
    return BarSeries(
        symbol=symbol,
        interval=BarInterval.DAY_1,
        bars=tuple(
            Bar(
                symbol=symbol,
                interval=BarInterval.DAY_1,
                source="ibkr",
                timestamp_utc=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                + timedelta(hours=21),
                session_date=day,
                # Distinct bytes per bar, kept OHLC-consistent so the fixture itself
                # does not trip the quality gate it is meant to pass.
                open=100.0 + index * 0.01,
                high=100.0 + index * 0.01,
                low=100.0 + index * 0.01,
                close=100.0 + index * 0.01,
                volume=1_000_000,
                status=BarStatus.CLOSED,
                exchange="SMART",
            )
            for index, day in enumerate(days)
        ),
    )


def _certification(symbol: str = "SPY"):
    return certify_export(
        dataset_id="chronos-etf-daily-v1",
        windows=[SymbolWindow(symbol, _START, _END)],
        series_by_symbol={symbol: _series(symbol)},
        actions_by_symbol={},
        attestation=NoCorporateActionAttestation(
            source_id="official-sponsor-history-2026-08-26",
            windows=(SymbolWindow(symbol, _START, _END),),
        ),
        calendar=_CALENDAR,
    )


def _spans(symbol: str = "SPY") -> list[HoldoutSpan]:
    return [
        HoldoutSpan(
            symbol=symbol,
            name="train",
            start=_START,
            end=_SPLIT - timedelta(days=1),
            status=HoldoutStatus.SEEN,
            reason="ordinary research window",
        ),
        HoldoutSpan(
            symbol=symbol,
            name="final-test",
            start=_SPLIT,
            end=_END,
            status=HoldoutStatus.CLEAN,
            reason="untouched final test",
        ),
    ]


def _freeze(tmp_path: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "dataset_id": "chronos-etf-daily-v1",
        "catalog_id": "chronos-etf-daily-v1-release-001",
        "source_id": "ibkr-tws-historical",
        "source_receipt_sha256": _SOURCE_RECEIPT,
        "certification": _certification(),
        "series_by_symbol": {"SPY": _series()},
        "spans": _spans(),
        "output_root": tmp_path / "release",
    }
    kwargs.update(overrides)
    return freeze_release(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------- the round trip


def test_the_emitted_manifest_opens_in_the_reader_that_authenticates_it(
    tmp_path: Path,
) -> None:
    release = _freeze(tmp_path)
    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_bytes(release.catalog_manifest_bytes())

    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=release.catalog_manifest_sha256,
        dataset_root=tmp_path / "release",
    )
    assert catalog.catalog_id == "chronos-etf-daily-v1-release-001"
    assert catalog.manifest_sha256 == release.catalog_manifest_sha256


def test_ordinary_bytes_resolve_and_read_back_exactly(tmp_path: Path) -> None:
    release = _freeze(tmp_path)
    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_bytes(release.catalog_manifest_bytes())
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=release.catalog_manifest_sha256,
        dataset_root=tmp_path / "release",
    )
    seen = next(p for p in release.partitions if p.span.status is HoldoutStatus.SEEN)
    request = CertifiedDataRequest(
        dataset_id=seen.dataset_id,
        partition=seen.partition,
        data_version=seen.sha256,
        source_id="ibkr-tws-historical",
        source_receipt_sha256=_SOURCE_RECEIPT,
    )
    metadata = catalog.resolve_ordinary(request)
    assert metadata.classification is DataClassification.ORDINARY
    read = catalog._read_bytes_for_trial(request)
    assert read.content_sha256 == seen.sha256
    assert read.byte_count == seen.byte_count
    assert read.content.decode("utf-8").startswith("date,open,high,low,close,volume\n")


def test_the_clean_holdout_partition_is_refused_to_ordinary_research(
    tmp_path: Path,
) -> None:
    """The map's whole purpose: CLEAN becomes catalog ``holdout`` and cannot be addressed."""

    release = _freeze(tmp_path)
    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_bytes(release.catalog_manifest_bytes())
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=release.catalog_manifest_sha256,
        dataset_root=tmp_path / "release",
    )
    clean = next(p for p in release.partitions if p.span.status is HoldoutStatus.CLEAN)
    with pytest.raises(HoldoutAccessRefused):
        catalog.resolve_ordinary(
            CertifiedDataRequest(
                dataset_id=clean.dataset_id,
                partition=clean.partition,
                data_version=clean.sha256,
                source_id="ibkr-tws-historical",
                source_receipt_sha256=_SOURCE_RECEIPT,
            )
        )


def test_a_burned_span_stays_ordinary_and_keeps_its_reason(tmp_path: Path) -> None:
    spans = [
        HoldoutSpan("SPY", "train", _START, _SPLIT - timedelta(days=1), HoldoutStatus.SEEN),
        HoldoutSpan(
            "SPY",
            "burned-window",
            _SPLIT,
            _END,
            HoldoutStatus.BURNED,
            reason="consumed by the 2026 Five-Tool campaign",
        ),
    ]
    release = _freeze(tmp_path, spans=spans)
    burned = next(p for p in release.partitions if p.span.status is HoldoutStatus.BURNED)
    assert burned.span.status.catalog_classification is DataClassification.ORDINARY
    document = release.release_document()
    entry = next(s for s in document["holdout_map"] if s["status"] == "burned")
    assert entry["reason"] == "consumed by the 2026 Five-Tool campaign"


def test_a_burned_span_must_say_why_it_was_consumed() -> None:
    with pytest.raises(ValueError, match="why it was consumed"):
        HoldoutSpan("SPY", "burned", _START, _END, HoldoutStatus.BURNED)


# --------------------------------------------------------------- content addressing


def test_data_version_is_the_content_digest(tmp_path: Path) -> None:
    release = _freeze(tmp_path)
    for entry in release.catalog_manifest()["entries"]:
        assert entry["data_version"] == entry["sha256"]
        assert (tmp_path / "release" / entry["path"]).read_bytes().__len__() == entry["byte_count"]


def test_freezing_identical_bytes_reproduces_the_release_digest(tmp_path: Path) -> None:
    first = _freeze(tmp_path / "a")
    second = _freeze(tmp_path / "b")
    assert first.release_digest == second.release_digest
    assert first.catalog_manifest_sha256 == second.catalog_manifest_sha256


def test_a_different_boundary_produces_a_different_release(tmp_path: Path) -> None:
    moved = [
        HoldoutSpan("SPY", "train", _START, date(2024, 4, 1), HoldoutStatus.SEEN),
        HoldoutSpan("SPY", "final-test", date(2024, 4, 2), _END, HoldoutStatus.CLEAN),
    ]
    original = _freeze(tmp_path / "a").release_digest
    shifted = _freeze(tmp_path / "b", spans=moved).release_digest
    assert original != shifted


def test_the_release_document_carries_the_certification_it_rests_on(tmp_path: Path) -> None:
    release = _freeze(tmp_path)
    document = json.loads(release.release_document_bytes())
    assert document["certification_digest"] == _certification().certification_digest
    assert document["catalog_manifest_sha256"] == release.catalog_manifest_sha256


# ----------------------------------------------------------------- refusals: the map


def test_a_gap_in_the_map_refuses(tmp_path: Path) -> None:
    spans = [
        HoldoutSpan("SPY", "train", _START, date(2024, 2, 28), HoldoutStatus.SEEN),
        HoldoutSpan("SPY", "final-test", _SPLIT, _END, HoldoutStatus.CLEAN),
    ]
    with pytest.raises(DatasetReleaseError, match="undeclared"):
        _freeze(tmp_path, spans=spans)
    assert not (tmp_path / "release").exists()


def test_an_overlap_in_the_map_refuses(tmp_path: Path) -> None:
    spans = [
        HoldoutSpan("SPY", "train", _START, date(2024, 3, 15), HoldoutStatus.SEEN),
        HoldoutSpan("SPY", "final-test", _SPLIT, _END, HoldoutStatus.CLEAN),
    ]
    with pytest.raises(DatasetReleaseError, match="overlap"):
        _freeze(tmp_path, spans=spans)


def test_a_map_that_starts_late_refuses(tmp_path: Path) -> None:
    spans = [HoldoutSpan("SPY", "all", date(2024, 2, 1), _END, HoldoutStatus.SEEN)]
    with pytest.raises(DatasetReleaseError, match="undeclared"):
        _freeze(tmp_path, spans=spans)


def test_a_map_that_ends_early_refuses(tmp_path: Path) -> None:
    spans = [HoldoutSpan("SPY", "all", _START, date(2024, 5, 1), HoldoutStatus.SEEN)]
    with pytest.raises(DatasetReleaseError, match="undeclared"):
        _freeze(tmp_path, spans=spans)


def test_map_and_export_must_describe_the_same_symbols(tmp_path: Path) -> None:
    spans = _spans("QQQ")
    with pytest.raises(DatasetReleaseError, match="symbols differ"):
        _freeze(tmp_path, spans=spans)


def test_duplicate_span_names_refuse_before_any_release_write(tmp_path: Path) -> None:
    spans = _spans()
    spans[1] = HoldoutSpan("SPY", spans[0].name, spans[1].start, spans[1].end, spans[1].status)
    with pytest.raises(DatasetReleaseError, match="duplicate holdout span name"):
        _freeze(tmp_path, spans=spans)
    assert not (tmp_path / "release").exists()


# ------------------------------------------------------- refusals: the release itself


def test_a_failed_certification_cannot_be_frozen(tmp_path: Path) -> None:
    """A release digest must be evidence, not a label typed over a failure."""

    failed = certify_export(
        dataset_id="chronos-etf-daily-v1",
        windows=[SymbolWindow("SPY", _START, _END)],
        series_by_symbol={"SPY": _series()},
        actions_by_symbol={},
        attestation=None,
        calendar=_CALENDAR,
    )
    assert failed.certified is False
    with pytest.raises(DatasetReleaseError, match="NOT_CERTIFIED"):
        _freeze(tmp_path, certification=failed)


def test_identical_partition_bytes_refuse(tmp_path: Path) -> None:
    """One content digest cannot carry two classifications — the catalog says so too.

    Two spans past the end of the data both render header-only, so they would share a
    SHA-256. That is exactly the collision ``certified_data`` refuses at read time, and
    catching it at freeze time is cheaper than minting a manifest that cannot be opened.
    """

    spans = [
        HoldoutSpan("SPY", "train", _START, _SPLIT - timedelta(days=1), HoldoutStatus.SEEN),
        HoldoutSpan("SPY", "final-test", _SPLIT, _END, HoldoutStatus.CLEAN),
        HoldoutSpan(
            "SPY",
            "future-a",
            _END + timedelta(days=1),
            _END + timedelta(days=30),
            HoldoutStatus.CLEAN,
        ),
        HoldoutSpan(
            "SPY",
            "future-b",
            _END + timedelta(days=31),
            _END + timedelta(days=60),
            HoldoutStatus.CLEAN,
        ),
    ]
    with pytest.raises(DatasetReleaseError, match="identical bytes"):
        _freeze(tmp_path, spans=spans)


def test_a_release_needs_a_map(tmp_path: Path) -> None:
    with pytest.raises(DatasetReleaseError, match="complete holdout map"):
        _freeze(tmp_path, spans=[])

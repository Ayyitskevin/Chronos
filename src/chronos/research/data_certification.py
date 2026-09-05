"""Certified owner-delivery writes into the existing release and history stores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chronos.histdata import store
from chronos.research import data_intake, dataset_release
from chronos.research.certification import CertificationReport

HISTORY_ROOT = Path(__file__).resolve().parents[3] / "research/data/history"


class DataCertificationWriteError(RuntimeError):
    """A certified delivery could not be frozen or merged into the existing store."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DataCertificationResult:
    """The existing verdict and, only on success, the artifacts written from it."""

    intake: data_intake.IntakeDelivery
    report: CertificationReport
    release: dataset_release.DatasetRelease | None
    history_root: Path
    bars_added: int = 0
    actions_stored: int = 0


def _require_empty_release_target(output_root: Path) -> None:
    if not output_root.exists():
        return
    if not output_root.is_dir():
        raise DataCertificationWriteError(output_root, "release target is not a directory")
    try:
        occupied = next(output_root.iterdir(), None)
    except OSError as error:
        raise DataCertificationWriteError(
            output_root, f"release target is unreadable ({error.__class__.__name__})"
        ) from error
    if occupied is not None:
        raise DataCertificationWriteError(
            output_root, "release target is not empty; frozen releases are immutable"
        )


def certify_delivery(
    delivery: Path, *, output_root: Path, history_root: Path
) -> DataCertificationResult:
    """Certify first; only a CERTIFIED report may reach either existing writer."""

    intake = data_intake.load_intake(delivery)
    report = data_intake.certify_loaded_intake(intake, delivery=delivery)
    if not report.certified:
        return DataCertificationResult(
            intake=intake,
            report=report,
            release=None,
            history_root=history_root,
        )

    _require_empty_release_target(output_root)
    try:
        release = dataset_release.freeze_release(
            dataset_id=intake.delivery_id,
            catalog_id=intake.delivery_id,
            source_id=intake.provenance.source_id,
            source_receipt_sha256=intake.provenance.source_receipt_sha256,
            certification=report,
            series_by_symbol=intake.series_by_symbol,
            spans=intake.holdout_map,
            output_root=output_root,
        )
        (output_root / "catalog.json").write_bytes(release.catalog_manifest_bytes())
        (output_root / "release.json").write_bytes(release.release_document_bytes())
    except (dataset_release.DatasetReleaseError, OSError) as error:
        raise DataCertificationWriteError(
            output_root, f"release freeze failed ({error})"
        ) from error

    captured_at = intake.provenance.retrieved_at.isoformat()
    bars_added = 0
    actions_stored = 0
    try:
        for symbol in sorted(intake.series_by_symbol):
            result = store.write_bars(
                history_root,
                intake.series_by_symbol[symbol],
                source=intake.provenance.source_id,
                captured_at=captured_at,
                allow_correction=False,
            )
            bars_added += result.rows_added
        for symbol in sorted(intake.actions_by_symbol):
            actions = intake.actions_by_symbol[symbol]
            store.write_actions(
                history_root,
                symbol,
                actions,
                captured_at=captured_at,
            )
            actions_stored += len(actions)
    except (store.StoreError, OSError) as error:
        raise DataCertificationWriteError(
            history_root, f"historical-store merge failed ({error})"
        ) from error

    return DataCertificationResult(
        intake=intake,
        report=report,
        release=release,
        history_root=history_root,
        bars_added=bars_added,
        actions_stored=actions_stored,
    )


__all__ = [
    "HISTORY_ROOT",
    "DataCertificationResult",
    "DataCertificationWriteError",
    "certify_delivery",
]

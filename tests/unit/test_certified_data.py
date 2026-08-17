"""Authenticated ordinary-data catalog controls for future brokered trials."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest

from chronos.research.certified_data import (
    CATALOG_SCHEMA_VERSION,
    CatalogAuthenticationError,
    CatalogSchemaError,
    CertifiedDataRequest,
    CertifiedDatasetCatalog,
    DataContentDrift,
    DataIdentityMismatch,
    DataPathRefused,
    DataSourceDrift,
    HoldoutAccessRefused,
)

_SOURCE_RECEIPT = hashlib.sha256(b"licensed-source-receipt").hexdigest()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry(
    content: bytes,
    *,
    path: str = "nested/bars.bin",
    dataset_id: str = "spy-minute-bars",
    partition: str = "dev",
    classification: str = "ordinary",
) -> dict[str, object]:
    digest = _sha256(content)
    return {
        "dataset_id": dataset_id,
        "partition": partition,
        "data_version": digest,
        "source_id": "licensed-feed-v1",
        "source_receipt_sha256": _SOURCE_RECEIPT,
        "classification": classification,
        "path": path,
        "sha256": digest,
        "byte_count": len(content),
    }


def _document(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_id": "chronos-research-data-v1",
        "entries": entries,
    }


def _write_manifest(tmp_path: Path, document: object) -> tuple[Path, str]:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "catalog.json"
    path.write_bytes(raw)
    return path, _sha256(raw)


def _catalog(
    tmp_path: Path,
    *,
    content: bytes = b"timestamp,open,high,low,close\n",
    entry_overrides: dict[str, object] | None = None,
) -> tuple[CertifiedDatasetCatalog, CertifiedDataRequest, Path, dict[str, object]]:
    root = tmp_path / "dataset-root"
    (root / "nested").mkdir(parents=True)
    (root / "nested/bars.bin").write_bytes(content)
    entry = _entry(content)
    if entry_overrides:
        entry.update(entry_overrides)
    manifest_path, trusted_digest = _write_manifest(tmp_path, _document([entry]))
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=trusted_digest,
        dataset_root=root,
    )
    request = CertifiedDataRequest(
        dataset_id=str(entry["dataset_id"]),
        partition=str(entry["partition"]),
        data_version=str(entry["data_version"]),
        source_id=str(entry["source_id"]),
        source_receipt_sha256=str(entry["source_receipt_sha256"]),
    )
    return catalog, request, root, entry


def test_authenticated_catalog_reads_exact_ordinary_bytes_through_private_seam(
    tmp_path: Path,
) -> None:
    content = b"immutable ordinary research bytes"
    catalog, request, _root, entry = _catalog(tmp_path, content=content)

    assert catalog.catalog_id == "chronos-research-data-v1"
    metadata = catalog.resolve_ordinary(request)
    assert metadata.dataset_id == request.dataset_id
    assert metadata.sha256 == entry["sha256"]
    assert not hasattr(metadata, "relative_path")
    assert not hasattr(catalog, "dataset_root")

    result = catalog._read_bytes_for_trial(request)

    assert result.content == content
    assert result.content_sha256 == entry["sha256"]
    assert result.byte_count == len(content)
    assert result.catalog_manifest_sha256 == catalog.manifest_sha256
    assert result.request is request
    assert content.decode() not in repr(result)


def test_manifest_authentication_precedes_root_or_schema_trust(tmp_path: Path) -> None:
    manifest_path, _ = _write_manifest(tmp_path, {"attacker": "controlled"})

    with pytest.raises(CatalogAuthenticationError, match="independently trusted"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256="0" * 64,
            dataset_root=tmp_path / "does-not-exist",
        )


def test_request_requires_exact_mapping_and_source_receipt(tmp_path: Path) -> None:
    catalog, request, _, _ = _catalog(tmp_path)
    other_version = hashlib.sha256(b"other-version").hexdigest()

    with pytest.raises(DataIdentityMismatch):
        catalog.resolve_ordinary(
            CertifiedDataRequest(
                dataset_id=request.dataset_id,
                partition=request.partition,
                data_version=other_version,
                source_id=request.source_id,
                source_receipt_sha256=request.source_receipt_sha256,
            )
        )

    with pytest.raises(DataSourceDrift):
        catalog.resolve_ordinary(
            CertifiedDataRequest(
                dataset_id=request.dataset_id,
                partition=request.partition,
                data_version=request.data_version,
                source_id="different-source",
                source_receipt_sha256=request.source_receipt_sha256,
            )
        )


def test_holdout_is_refused_before_missing_target_or_source_checks(tmp_path: Path) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    content = b"bytes-that-must-not-be-opened"
    entry = _entry(
        content,
        path="absent/final.bin",
        partition="final",
        classification="holdout",
    )
    manifest_path, trusted_digest = _write_manifest(tmp_path, _document([entry]))
    catalog = CertifiedDatasetCatalog.from_manifest(
        manifest_path,
        trusted_manifest_sha256=trusted_digest,
        dataset_root=root,
    )
    request = CertifiedDataRequest(
        dataset_id=str(entry["dataset_id"]),
        partition=str(entry["partition"]),
        data_version=str(entry["data_version"]),
        source_id="deliberately-wrong-source",
        source_receipt_sha256="f" * 64,
    )

    with pytest.raises(HoldoutAccessRefused):
        catalog._read_bytes_for_trial(request)


@pytest.mark.parametrize(
    "unsafe_path",
    ["/tmp/outside", "../outside", "a/../outside", ".", "a\\b", "a//b", "a/./b", "a/"],
)
def test_catalog_rejects_noncanonical_or_escaping_paths(tmp_path: Path, unsafe_path: str) -> None:
    content = b"x"
    root = tmp_path / "dataset-root"
    root.mkdir()
    manifest_path, trusted_digest = _write_manifest(
        tmp_path,
        _document([_entry(content, path=unsafe_path)]),
    )

    with pytest.raises(CatalogSchemaError, match="path"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=root,
        )


@pytest.mark.parametrize("scope", ["catalog", "entry"])
def test_catalog_schema_rejects_extra_keys(tmp_path: Path, scope: str) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    entry = _entry(b"x", path="x")
    document = _document([entry])
    if scope == "catalog":
        document["extra"] = True
    else:
        entry["extra"] = True
    manifest_path, trusted_digest = _write_manifest(tmp_path, document)

    with pytest.raises(CatalogSchemaError, match="extra"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=root,
        )


def test_catalog_schema_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    raw = (
        b'{"schema_version":"chronos-certified-data-catalog-v1",'
        b'"catalog_id":"first","catalog_id":"second","entries":[]}'
    )
    manifest_path = tmp_path / "catalog.json"
    manifest_path.write_bytes(raw)

    with pytest.raises(CatalogSchemaError, match="duplicate key"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=_sha256(raw),
            dataset_root=root,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"data_version": "a" * 64},
        {"byte_count": True},
        {"classification": "maybe-holdout"},
    ],
)
def test_catalog_schema_rejects_ambiguous_entry_identity(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    entry = _entry(b"x", path="x")
    entry.update(mutation)
    manifest_path, trusted_digest = _write_manifest(tmp_path, _document([entry]))

    with pytest.raises(CatalogSchemaError):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=root,
        )


def test_catalog_schema_rejects_duplicate_dataset_partition_version(tmp_path: Path) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    entry = _entry(b"x", path="one")
    duplicate = dict(entry)
    duplicate["path"] = "two"
    manifest_path, trusted_digest = _write_manifest(
        tmp_path,
        _document([entry, duplicate]),
    )

    with pytest.raises(CatalogSchemaError, match="duplicate"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=root,
        )


@pytest.mark.parametrize("shared_identity", ["path", "content"])
def test_catalog_rejects_ordinary_holdout_aliases(tmp_path: Path, shared_identity: str) -> None:
    root = tmp_path / "dataset-root"
    root.mkdir()
    content = b"bytes cannot be both ordinary and holdout"
    ordinary = _entry(content, path="ordinary.bin", partition="dev")
    holdout = _entry(
        content if shared_identity == "content" else b"different holdout bytes",
        path="ordinary.bin" if shared_identity == "path" else "holdout.bin",
        partition="final",
        classification="holdout",
    )
    manifest_path, trusted_digest = _write_manifest(
        tmp_path,
        _document([ordinary, holdout]),
    )

    with pytest.raises(CatalogSchemaError, match=r"both ordinary and holdout"):
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=root,
        )


def test_manifest_and_dataset_root_symlinks_are_refused(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    real_manifest, trusted_digest = _write_manifest(tmp_path, _document([_entry(b"x", path="x")]))
    linked_manifest = tmp_path / "linked-catalog.json"
    linked_manifest.symlink_to(real_manifest)

    with pytest.raises(DataPathRefused, match="manifest"):
        CertifiedDatasetCatalog.from_manifest(
            linked_manifest,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=real_root,
        )
    with pytest.raises(DataPathRefused, match="root"):
        CertifiedDatasetCatalog.from_manifest(
            real_manifest,
            trusted_manifest_sha256=trusted_digest,
            dataset_root=linked_root,
        )


@pytest.mark.parametrize("target_kind", ["symlink", "directory", "fifo"])
def test_reader_rejects_symlink_and_nonregular_data_objects(
    tmp_path: Path, target_kind: str
) -> None:
    catalog, request, root, _ = _catalog(tmp_path)
    target = root / "nested/bars.bin"
    target.unlink()
    if target_kind == "symlink":
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"timestamp,open,high,low,close\n")
        target.symlink_to(outside)
    elif target_kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    with pytest.raises(DataPathRefused, match=r"regular|unsafe"):
        catalog._read_bytes_for_trial(request)


def test_reader_refuses_content_and_length_drift(tmp_path: Path) -> None:
    catalog, request, root, _ = _catalog(tmp_path, content=b"original")
    target = root / "nested/bars.bin"

    target.write_bytes(b"tampered")
    with pytest.raises(DataContentDrift):
        catalog._read_bytes_for_trial(request)

    target.write_bytes(b"longer-than-authenticated")
    with pytest.raises(DataContentDrift):
        catalog._read_bytes_for_trial(request)


def test_reader_refuses_replaced_fixed_root_even_with_matching_bytes(tmp_path: Path) -> None:
    content = b"same-content"
    catalog, request, root, _ = _catalog(tmp_path, content=content)
    displaced = tmp_path / "displaced-root"
    root.rename(displaced)
    (root / "nested").mkdir(parents=True)
    (root / "nested/bars.bin").write_bytes(content)

    with pytest.raises(DataPathRefused, match="replaced"):
        catalog._read_bytes_for_trial(request)


def test_catalog_exposes_no_public_raw_read_or_per_run_path_callback() -> None:
    public_names = {name for name in dir(CertifiedDatasetCatalog) if not name.startswith("_")}
    assert public_names.isdisjoint({"read", "read_bytes", "open", "open_bytes"})
    assert public_names.isdisjoint({"dataset_root", "entries"})
    assert tuple(inspect.signature(CertifiedDatasetCatalog._read_bytes_for_trial).parameters) == (
        "self",
        "request",
    )
    factory_parameters: dict[str, inspect.Parameter] = dict(
        inspect.signature(CertifiedDatasetCatalog.from_manifest).parameters
    )
    assert set(factory_parameters) == {
        "manifest_path",
        "trusted_manifest_sha256",
        "dataset_root",
    }
    assert "callback" not in factory_parameters
    assert "reader" not in factory_parameters


def test_catalog_constructor_cannot_accept_caller_supplied_entries() -> None:
    parameters: dict[str, Any] = dict(inspect.signature(CertifiedDatasetCatalog).parameters)
    assert "entries" not in parameters
    with pytest.raises(TypeError, match="from_manifest"):
        CertifiedDatasetCatalog(object(), _construction_token=object())  # type: ignore[arg-type]

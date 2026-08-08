"""Strict, capability-owned input catalog for future certified research.

This module is evidence infrastructure only.  It does not make a dataset certified,
authorize a campaign, count a trial, unlock a holdout, or evaluate a strategy.  The
catalog is authenticated by a trusted SHA-256 supplied out of band, binds one fixed
data root at construction, and maps an exact dataset/partition/version/source identity
to one immutable byte bundle.

There is deliberately no public byte-reading method.  A later trial broker is expected
to durably record ``trial_started`` in the canonical registry and only then call the
private ``_read_bytes_for_trial`` seam.  Ordinary research has no holdout override:
holdout entries are categorically refused before their target path is opened, and a
path or content digest cannot be declared on both sides of that classification boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

CATALOG_SCHEMA_VERSION = "chronos-certified-data-catalog-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}")
_READ_CHUNK_BYTES = 1024 * 1024


class CertifiedDataError(RuntimeError):
    """A catalog or requested data object failed a certified-data control."""


class CatalogAuthenticationError(CertifiedDataError):
    """The catalog bytes do not match the independently trusted digest."""


class CatalogSchemaError(CertifiedDataError):
    """The catalog is not the exact supported schema."""


class DataIdentityMismatch(CertifiedDataError):
    """No catalog entry matches the requested dataset/partition/version identity."""


class DataSourceDrift(CertifiedDataError):
    """The request's source identity differs from the authenticated catalog."""


class HoldoutAccessRefused(CertifiedDataError):
    """Ordinary research attempted to address holdout bytes."""


class DataPathRefused(CertifiedDataError):
    """A catalog or data path was unsafe, escaped its root, or was not regular."""


class DataContentDrift(CertifiedDataError):
    """The bytes at a catalog entry no longer match its content identity."""


class DataClassification(StrEnum):
    ORDINARY = "ordinary"
    HOLDOUT = "holdout"


@dataclass(frozen=True, slots=True)
class CertifiedDataRequest:
    """Exact content and source identity requested by a future trial broker."""

    dataset_id: str
    partition: str
    data_version: str
    source_id: str
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        _require_identity("dataset_id", self.dataset_id)
        _require_identity("partition", self.partition)
        _require_sha256("data_version", self.data_version)
        _require_identity("source_id", self.source_id)
        _require_sha256("source_receipt_sha256", self.source_receipt_sha256)


@dataclass(frozen=True, slots=True)
class CertifiedDatasetEntry:
    """Sanitized authenticated metadata for one immutable partition bundle.

    Filesystem paths are deliberately absent so metadata resolution cannot be composed
    with a public root to route around the private broker-owned reader.
    """

    dataset_id: str
    partition: str
    data_version: str
    source_id: str
    source_receipt_sha256: str
    classification: DataClassification
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class CertifiedDataRead:
    """Receipt-bound bytes returned only through the private trial-reader seam."""

    request: CertifiedDataRequest
    catalog_manifest_sha256: str
    content_sha256: str
    byte_count: int
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    metadata: CertifiedDatasetEntry
    relative_path: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AuthenticatedCatalogState:
    catalog_id: str
    manifest_sha256: str
    dataset_root: Path
    root_device: int
    root_inode: int
    entries: tuple[_CatalogEntry, ...]


_CATALOG_CONSTRUCTION_TOKEN = object()


class CertifiedDatasetCatalog:
    """Authenticated exact mapping over one fixed dataset root.

    Construction validates only the catalog and path *syntax*.  It never opens a data
    entry, which is important for holdout entries.  The future trial broker performs the
    private byte read after its durable start record exists.
    """

    def __init__(
        self,
        state: _AuthenticatedCatalogState,
        *,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _CATALOG_CONSTRUCTION_TOKEN:
            raise TypeError("use CertifiedDatasetCatalog.from_manifest()")
        self._catalog_id = state.catalog_id
        self._manifest_sha256 = state.manifest_sha256
        self._dataset_root = state.dataset_root
        self._root_device = state.root_device
        self._root_inode = state.root_inode
        self._entries = state.entries
        self._by_identity = {
            (
                entry.metadata.dataset_id,
                entry.metadata.partition,
                entry.metadata.data_version,
            ): entry
            for entry in state.entries
        }

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        *,
        trusted_manifest_sha256: str,
        dataset_root: Path,
    ) -> CertifiedDatasetCatalog:
        """Authenticate and parse one exact catalog without opening dataset bytes."""

        _require_sha256("trusted_manifest_sha256", trusted_manifest_sha256)
        manifest_bytes = _read_path_without_following_symlinks(manifest_path)
        actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_manifest_sha256 != trusted_manifest_sha256:
            raise CatalogAuthenticationError(
                "catalog bytes do not match the independently trusted SHA-256"
            )
        root, root_device, root_inode = _validated_directory(dataset_root)
        document = _decode_exact_json_object(manifest_bytes, context="certified data catalog")
        _require_exact_keys(
            document,
            {"schema_version", "catalog_id", "entries"},
            "certified data catalog",
        )
        if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise CatalogSchemaError(
                f"unsupported catalog schema {document.get('schema_version')!r}"
            )
        catalog_id = document.get("catalog_id")
        if not isinstance(catalog_id, str):
            raise CatalogSchemaError("catalog_id must be a string")
        _require_identity("catalog_id", catalog_id)
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise CatalogSchemaError("entries must be a non-empty list")

        entries: list[_CatalogEntry] = []
        seen: set[tuple[str, str, str]] = set()
        path_classifications: dict[str, DataClassification] = {}
        content_classifications: dict[str, DataClassification] = {}
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                raise CatalogSchemaError(f"entries[{index}] must be an object")
            entry = _parse_entry(raw, index=index)
            key = (
                entry.metadata.dataset_id,
                entry.metadata.partition,
                entry.metadata.data_version,
            )
            if key in seen:
                raise CatalogSchemaError(f"duplicate dataset/partition/version mapping: {key!r}")
            seen.add(key)
            _require_consistent_classification(
                identity_name="path",
                identity=entry.relative_path,
                classification=entry.metadata.classification,
                classifications=path_classifications,
            )
            _require_consistent_classification(
                identity_name="content SHA-256",
                identity=entry.metadata.sha256,
                classification=entry.metadata.classification,
                classifications=content_classifications,
            )
            entries.append(entry)
        return cls(
            _AuthenticatedCatalogState(
                catalog_id=catalog_id,
                manifest_sha256=actual_manifest_sha256,
                dataset_root=root,
                root_device=root_device,
                root_inode=root_inode,
                entries=tuple(entries),
            ),
            _construction_token=_CATALOG_CONSTRUCTION_TOKEN,
        )

    @property
    def catalog_id(self) -> str:
        return self._catalog_id

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def resolve_ordinary(self, request: CertifiedDataRequest) -> CertifiedDatasetEntry:
        """Resolve sanitized ordinary metadata and categorically refuse holdouts."""

        return self._resolve_ordinary_entry(request).metadata

    def _resolve_ordinary_entry(self, request: CertifiedDataRequest) -> _CatalogEntry:
        """Resolve the private path-bearing entry used only by the broker read seam."""

        if not isinstance(request, CertifiedDataRequest):
            raise TypeError("request must be CertifiedDataRequest")
        entry = self._by_identity.get((request.dataset_id, request.partition, request.data_version))
        if entry is None:
            raise DataIdentityMismatch(
                "no authenticated catalog entry matches dataset_id, partition, and data_version"
            )
        metadata = entry.metadata
        if metadata.classification is DataClassification.HOLDOUT:
            raise HoldoutAccessRefused(
                "ordinary certified-data access cannot address a holdout entry"
            )
        if (
            request.source_id != metadata.source_id
            or request.source_receipt_sha256 != metadata.source_receipt_sha256
        ):
            raise DataSourceDrift(
                "requested source identity differs from the authenticated catalog"
            )
        return entry

    def _read_bytes_for_trial(self, request: CertifiedDataRequest) -> CertifiedDataRead:
        """Private seam: read exact bytes after a broker has recorded trial start.

        This method intentionally remains private and has no path, callback, or holdout
        override parameter.  Calling it directly does not create a valid research trial;
        the later broker must supply that ordering and canonical-registry evidence.
        """

        entry = self._resolve_ordinary_entry(request)
        content = _read_relative_regular_file(
            self._dataset_root,
            entry.relative_path,
            maximum_bytes=entry.metadata.byte_count,
            expected_root_device=self._root_device,
            expected_root_inode=self._root_inode,
        )
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != entry.metadata.byte_count or actual_sha256 != entry.metadata.sha256:
            raise DataContentDrift(
                "dataset bytes differ from the authenticated catalog size or SHA-256"
            )
        return CertifiedDataRead(
            request=request,
            catalog_manifest_sha256=self._manifest_sha256,
            content_sha256=actual_sha256,
            byte_count=len(content),
            content=content,
        )


def _parse_entry(raw: dict[str, object], *, index: int) -> _CatalogEntry:
    context = f"entries[{index}]"
    _require_exact_keys(
        raw,
        {
            "dataset_id",
            "partition",
            "data_version",
            "source_id",
            "source_receipt_sha256",
            "classification",
            "path",
            "sha256",
            "byte_count",
        },
        context,
    )
    dataset_id = _string_field(raw, "dataset_id", context)
    partition = _string_field(raw, "partition", context)
    data_version = _string_field(raw, "data_version", context)
    source_id = _string_field(raw, "source_id", context)
    source_receipt = _string_field(raw, "source_receipt_sha256", context)
    relative_path = _string_field(raw, "path", context)
    sha256 = _string_field(raw, "sha256", context)
    _require_identity(f"{context}.dataset_id", dataset_id)
    _require_identity(f"{context}.partition", partition)
    _require_sha256(f"{context}.data_version", data_version)
    _require_identity(f"{context}.source_id", source_id)
    _require_sha256(f"{context}.source_receipt_sha256", source_receipt)
    _require_sha256(f"{context}.sha256", sha256)
    if data_version != sha256:
        raise CatalogSchemaError(
            f"{context}.data_version must equal sha256 for its immutable partition bundle"
        )
    _validate_relative_path(relative_path, context=f"{context}.path")
    raw_classification = raw.get("classification")
    if not isinstance(raw_classification, str):
        raise CatalogSchemaError(f"{context}.classification must be a string")
    try:
        classification = DataClassification(raw_classification)
    except ValueError as error:
        raise CatalogSchemaError(f"{context}.classification must be ordinary or holdout") from error
    byte_count = raw.get("byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise CatalogSchemaError(f"{context}.byte_count must be an integer >= 0")
    return _CatalogEntry(
        metadata=CertifiedDatasetEntry(
            dataset_id=dataset_id,
            partition=partition,
            data_version=data_version,
            source_id=source_id,
            source_receipt_sha256=source_receipt,
            classification=classification,
            sha256=sha256,
            byte_count=byte_count,
        ),
        relative_path=relative_path,
    )


def _require_consistent_classification(
    *,
    identity_name: str,
    identity: str,
    classification: DataClassification,
    classifications: dict[str, DataClassification],
) -> None:
    prior = classifications.setdefault(identity, classification)
    if prior is not classification:
        raise CatalogSchemaError(
            f"catalog {identity_name} {identity!r} cannot be classified as both "
            f"{prior.value} and {classification.value}"
        )


def _validated_directory(path: Path) -> tuple[Path, int, int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise DataPathRefused(f"dataset root does not exist: {path}") from error
    except OSError as error:
        raise DataPathRefused(f"dataset root must be a non-symlink directory: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DataPathRefused(f"dataset root must be a non-symlink directory: {path}")
        fixed_path = Path(os.path.abspath(path))
        return fixed_path, metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _read_path_without_following_symlinks(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise DataPathRefused(f"catalog manifest does not exist: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DataPathRefused(f"catalog manifest must be a non-symlink regular file: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DataPathRefused(
            f"catalog manifest must remain a non-symlink regular file: {path}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise DataPathRefused(f"catalog manifest is no longer a regular file: {path}")
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _read_relative_regular_file(
    root: Path,
    relative: str,
    *,
    maximum_bytes: int,
    expected_root_device: int,
    expected_root_inode: int,
) -> bytes:
    parts = _validate_relative_path(relative, context="catalog entry path").parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        root_metadata = os.fstat(current)
        if (
            root_metadata.st_dev != expected_root_device
            or root_metadata.st_ino != expected_root_inode
        ):
            raise DataPathRefused("the fixed dataset root was replaced after catalog load")
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        try:
            target_metadata = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError as error:
            raise DataPathRefused(f"catalog data object is missing: {relative}") from error
        if not stat.S_ISREG(target_metadata.st_mode):
            raise DataPathRefused(f"catalog data object is not a regular file: {relative}")
        target = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(target)
        opened = os.fstat(target)
        if not stat.S_ISREG(opened.st_mode):
            raise DataPathRefused(f"catalog data object is not regular after open: {relative}")
        content = _read_descriptor(target, maximum_bytes=maximum_bytes)
        after = os.fstat(target)
        if (
            opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise DataContentDrift("dataset object changed while it was being read")
        return content
    except OSError as error:
        raise DataPathRefused(f"refused unsafe catalog data path {relative!r}: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_descriptor(descriptor: int, *, maximum_bytes: int | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if maximum_bytes is not None and total > maximum_bytes:
            raise DataContentDrift("dataset object exceeds its authenticated byte count")
        chunks.append(chunk)


def _validate_relative_path(value: str, *, context: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CatalogSchemaError(f"{context} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CatalogSchemaError(f"{context} must not be absolute or contain dot segments")
    if path.as_posix() != value:
        raise CatalogSchemaError(f"{context} must use canonical POSIX spelling")
    return path


def _decode_exact_json_object(raw: bytes, *, context: str) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CatalogSchemaError(f"{context} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise CatalogSchemaError(f"{context} contains non-finite number {value}")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise CatalogSchemaError(f"{context} is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise CatalogSchemaError(f"{context} is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise CatalogSchemaError(f"{context} root must be an object")
    return decoded


def _require_exact_keys(value: dict[str, object], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CatalogSchemaError(
            f"{context} keys differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _string_field(value: dict[str, object], key: str, context: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise CatalogSchemaError(f"{context}.{key} must be a string")
    return item


def _require_identity(name: str, value: str) -> None:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise CatalogSchemaError(f"{name} must be a bounded canonical identity string")


def _require_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CatalogSchemaError(f"{name} must be a lowercase SHA-256 digest")

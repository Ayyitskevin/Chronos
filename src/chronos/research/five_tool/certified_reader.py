"""Digest-locked certified dataset reader: the only Five-Tool path that can prove a read.

The Five-Tool trial broker's reader has always been an arbitrary callback.  It could
promise which bytes it handed over and nothing could check the promise, which is why
``chronos.research.five_tool_trials`` records ``certified_reader: False`` on every
canonical ADR-0013 run it registers.  This module is the capability that makes that field
a real discriminator.

**What "certified" means here, and what it does not.**  A certification manifest binds a
dataset *identity*: every file, its exact SHA-256, its exact byte length, and one overall
dataset digest that is **derived from the bytes**, never asserted.  It does not attest
provenance, licensing, vendor fidelity, corporate-action treatment, or fitness for a
hypothesis.  Deciding that a dataset's provenance is acceptable — and freezing its digest
into a campaign manifest's ``data.dataset_version_lock`` — is an owner act.  Building the
reader is engineering; certifying a dataset is not, and this module certifies nothing.

**The fail-closed contract.**

1. *Digest-locked.*  A reader opens a dataset root only through
   ``CERTIFICATION.json``.  An absent root or manifest, a malformed manifest, a file on
   disk the manifest does not digest, a manifest file absent from disk, a file whose bytes
   disagree with its declared digest or length, a symbolic link anywhere under the root, a
   declared dataset digest that disagrees with the bytes, or a dataset digest that
   disagrees with the campaign's ``dataset_version_lock`` each refuse with a distinct
   message.  Nothing is repaired, skipped, or warned about.
2. *Proves what it touched.*  Every read appends a :class:`CertifiedReadReceipt` naming
   the exact files, digests, and payload digest that were produced.  The broker compares
   the receipt against the pre-read attestation and against the bytes it actually received
   before a trial may complete, so ``certified_reader: True`` is backed by a check rather
   than by a claim.
3. *The overall digest is the payload digest.*  ``dataset_sha256`` is the SHA-256 of the
   canonical payload (:func:`certified_payload`), so the trial broker's existing
   "reader bytes must hash to the campaign-authorized ``data_version``" check binds the
   whole certified file set for free.  Renaming, reordering, truncating, adding, or
   removing a file all change it.
4. *Partitions are an authorization list, not a byte-level slice.*  The manifest names the
   partitions this certified dataset may be read under, and a partition outside that list
   is refused here as well as by the campaign identity check — defense in depth with a
   distinct message.  Holdout vocabulary may never appear in that list.  **Disclosed
   residual:** the reader authorizes a partition *label*; it does not carve holdout bars
   out of a dataset that contains them.  A certified dataset must therefore not contain
   holdout bytes, which is part of what the owner decides when certifying one.

Research only.  This module imports nothing but the standard library — no broker, order,
execution, registry, or holdout-guardian code — so possessing a certified reader confers
no unmasking or execution authority of any kind.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

CERTIFICATION_SCHEMA_VERSION = "chronos-five-tool-dataset-certification-v1"
CERTIFICATION_MANIFEST_NAME = "CERTIFICATION.json"
# Framing version prefix of the canonical payload.  Changing it changes every dataset
# digest, which is why it is a constant with a version in its name rather than a literal.
PAYLOAD_FRAMING_HEADER = b"chronos-five-tool-certified-payload-v1\n"

_SHA256_HEX_LENGTH = 64
_SHA256_ALPHABET = frozenset("0123456789abcdef")
# Deliberately duplicated from ``five_tool_trials._DEFAULT_HOLDOUT_PARTITIONS`` rather
# than imported: importing the trial broker here would make the reader depend on the
# module that depends on it.  tests/safety/test_five_tool_certified_reader_exercised.py
# pins the two copies equal so the duplication cannot drift.
_HOLDOUT_PARTITION_VOCABULARY = frozenset(
    {"holdout", "final", "reserved", "reserved_final", "final_holdout"}
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "certified_at_utc",
        "accessible_partitions",
        "dataset_sha256",
        "files",
    }
)
_FILE_KEYS = frozenset({"path", "sha256", "size_bytes"})


class CertifiedReaderError(RuntimeError):
    """A certified read could not be proven, so no bytes are returned."""


class CertificationManifestMissing(CertifiedReaderError):
    """The dataset root or its certification manifest does not exist."""


class CertificationManifestInvalid(CertifiedReaderError):
    """The certification manifest exists but does not describe a certifiable dataset."""


class DatasetIdentityMismatch(CertifiedReaderError):
    """The certified dataset is not the dataset the campaign asked for."""


class PartitionNotCertified(CertifiedReaderError):
    """The requested partition is outside the certification manifest's access list."""


class DatasetDigestMismatch(CertifiedReaderError):
    """A digest lock disagrees with the bytes or with the campaign dataset lock."""


class UncertifiedFilePresent(CertifiedReaderError):
    """The dataset root holds a file the certification manifest does not digest."""


class CertifiedFileMissing(CertifiedReaderError):
    """A file the certification manifest digests is absent from the dataset root."""


class DatasetRequest(Protocol):
    """The three request fields a certified read is locked to.

    Structural on purpose: ``five_tool_trials.DataAccessRequest`` satisfies it without
    this module importing the trial broker.
    """

    @property
    def dataset_id(self) -> str: ...

    @property
    def partition(self) -> str: ...

    @property
    def data_version(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CertifiedFile:
    """One digest-locked file: its dataset-relative POSIX path, SHA-256, and length."""

    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CertificationManifest:
    """A validated certification manifest bound to the root it was loaded from."""

    dataset_id: str
    certified_at_utc: str
    accessible_partitions: frozenset[str]
    dataset_sha256: str
    files: tuple[CertifiedFile, ...]
    manifest_sha256: str
    root: Path

    @property
    def file_digests(self) -> dict[str, str]:
        """Path → SHA-256 for every digest-locked file, in canonical path order."""

        return {entry.path: entry.sha256 for entry in self.files}


@dataclass(frozen=True, slots=True)
class CertifiedReadReceipt:
    """Proof of one completed certified read: exactly what was opened and what it hashed."""

    dataset_id: str
    partition: str
    dataset_sha256: str
    certification_manifest_sha256: str
    payload_sha256: str
    files: tuple[CertifiedFile, ...]

    @property
    def file_digests(self) -> dict[str, str]:
        return {entry.path: entry.sha256 for entry in self.files}


def certified_payload(files: Sequence[tuple[str, bytes]]) -> bytes:
    """Serialize ``(relative path, bytes)`` pairs into the one canonical payload framing.

    Length-prefixed and name-prefixed so that no rename, reorder, split, or concatenation
    of file contents can produce the same bytes as a different file set.
    """

    ordered = sorted(files, key=lambda item: item[0])
    if len({path for path, _ in ordered}) != len(ordered):
        raise ValueError("certified payload cannot contain duplicate paths")
    chunks: list[bytes] = [PAYLOAD_FRAMING_HEADER]
    for path, data in ordered:
        chunks.append(f"{path}\n{len(data)}\n".encode())
        chunks.append(data)
        chunks.append(b"\n")
    return b"".join(chunks)


def build_certification_manifest(
    root: Path,
    *,
    dataset_id: str,
    accessible_partitions: Sequence[str],
    certified_at_utc: str,
) -> dict[str, object]:
    """Compute a certification manifest mapping by hashing whatever is under ``root``.

    This attests **identity only** — the digests are a function of the bytes present, so
    it can no more vouch for a dataset's provenance than ``sha256sum`` can.  Running it
    against a real dataset, reviewing what it produced, and freezing the resulting digest
    into a campaign manifest is an owner decision (the certified-dataset sourcing item in
    the owner queue), not something a research session performs in passing.  It writes
    nothing; the caller decides whether the result deserves to exist on disk.
    """

    if not root.is_dir():
        raise CertificationManifestMissing(f"certified dataset root {root} is absent")
    partitions = _validated_partitions(accessible_partitions)
    _require_utc_timestamp("certified_at_utc", certified_at_utc)
    entries: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise CertificationManifestInvalid(
                f"certified dataset root contains a symbolic link {relative!r}; "
                "a certified dataset is exactly the bytes under its own root"
            )
        if not path.is_file() or relative == CERTIFICATION_MANIFEST_NAME:
            continue
        entries.append((relative, path.read_bytes()))
    if not entries:
        raise CertificationManifestInvalid(
            f"certified dataset root {root} holds no files to certify"
        )
    return {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "certified_at_utc": certified_at_utc,
        "accessible_partitions": sorted(partitions),
        "dataset_sha256": hashlib.sha256(certified_payload(entries)).hexdigest(),
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
            for path, data in entries
        ],
    }


def load_certification_manifest(root: Path) -> CertificationManifest:
    """Parse and fully validate ``root/CERTIFICATION.json``; read no dataset bytes."""

    if not root.is_dir():
        raise CertificationManifestMissing(
            f"certified dataset root {root} is absent; a certified dataset is provisioned "
            "deliberately, never conjured by a research run"
        )
    manifest_path = root / CERTIFICATION_MANIFEST_NAME
    if not manifest_path.is_file():
        raise CertificationManifestMissing(
            f"no certification manifest at {manifest_path}; an uncertified dataset cannot "
            "be read by a certified reader"
        )
    raw = manifest_path.read_bytes()
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CertificationManifestInvalid(
            f"certification manifest {manifest_path} is not readable JSON: {type(error).__name__}"
        ) from error
    if not isinstance(decoded, dict):
        raise CertificationManifestInvalid("certification manifest must be a JSON object")
    _require_exact_keys(decoded, _MANIFEST_KEYS, "certification manifest")
    if decoded.get("schema_version") != CERTIFICATION_SCHEMA_VERSION:
        raise CertificationManifestInvalid(
            f"certification manifest schema_version must be {CERTIFICATION_SCHEMA_VERSION!r}"
        )
    dataset_id = _required_string(decoded, "dataset_id")
    if not dataset_id.strip():
        raise CertificationManifestInvalid("certification manifest dataset_id must be non-empty")
    certified_at_utc = _required_string(decoded, "certified_at_utc")
    _require_utc_timestamp("certification manifest certified_at_utc", certified_at_utc)
    raw_partitions = decoded.get("accessible_partitions")
    if not isinstance(raw_partitions, list):
        raise CertificationManifestInvalid(
            "certification manifest accessible_partitions must be a list"
        )
    partitions = _validated_partitions(raw_partitions)
    dataset_sha256 = _required_string(decoded, "dataset_sha256")
    _require_sha256("certification manifest dataset_sha256", dataset_sha256)
    files = _validated_files(decoded.get("files"))
    return CertificationManifest(
        dataset_id=dataset_id,
        certified_at_utc=certified_at_utc,
        accessible_partitions=partitions,
        dataset_sha256=dataset_sha256,
        files=files,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        root=root,
    )


class CertifiedDatasetReader:
    """A digest-locked reader that records exactly which certified bytes it produced.

    Only this exact class earns ``certified_reader: True`` in a registered run.  A
    subclass could override ``__call__``, ``attest``, or ``receipts``, so the trial broker
    compares the concrete type rather than using ``isinstance`` — a reader that might be
    something else proves nothing, and proving nothing is the state this capability exists
    to end.
    """

    def __init__(self, root: Path) -> None:
        self._manifest = load_certification_manifest(root)
        self._receipts: list[CertifiedReadReceipt] = []

    @property
    def manifest(self) -> CertificationManifest:
        return self._manifest

    @property
    def receipts(self) -> tuple[CertifiedReadReceipt, ...]:
        """Every completed read, oldest first.  A refused read appends nothing."""

        return tuple(self._receipts)

    def attest(self, request: DatasetRequest) -> dict[str, object]:
        """Bind this reader to one request before any dataset byte is opened.

        Returns the provenance evidence a registered run records.  Every conjunct here is
        checkable from the manifest alone, which is what lets registration precede the
        read without the record becoming a promise: the read is then verified against it.
        """

        manifest = self._manifest
        if manifest.dataset_id != request.dataset_id:
            raise DatasetIdentityMismatch(
                f"certified dataset {manifest.dataset_id!r} is not the campaign dataset "
                f"{request.dataset_id!r}"
            )
        partition = _normalized_partition(request.partition)
        if partition in _HOLDOUT_PARTITION_VOCABULARY:
            raise PartitionNotCertified(
                f"partition {request.partition!r} is holdout vocabulary; a certified reader "
                "has no unlock capability"
            )
        if partition not in manifest.accessible_partitions:
            raise PartitionNotCertified(
                f"partition {request.partition!r} is not certified-accessible for dataset "
                f"{manifest.dataset_id!r}; certified partitions are "
                f"{sorted(manifest.accessible_partitions)}"
            )
        if manifest.dataset_sha256 != request.data_version:
            raise DatasetDigestMismatch(
                f"certified dataset digest {manifest.dataset_sha256} does not match the "
                f"campaign dataset_version_lock {request.data_version}"
            )
        return {
            "dataset_sha256": manifest.dataset_sha256,
            "partition": request.partition,
            "certified_reader": True,
            "certification_manifest_sha256": manifest.manifest_sha256,
            "files": dict(sorted(manifest.file_digests.items())),
        }

    def __call__(self, request: DatasetRequest) -> bytes:
        """Read the certified dataset, verifying every declared digest against the bytes.

        ``attest`` runs again here so that a reader used outside the trial broker enforces
        the same locks, and a receipt is appended only after every check has passed.
        """

        attested = self.attest(request)
        payload, files = self._read_certified_payload()
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        if payload_sha256 != self._manifest.dataset_sha256:  # pragma: no cover - defense
            raise DatasetDigestMismatch("certified payload digest changed between checks")
        self._receipts.append(
            CertifiedReadReceipt(
                dataset_id=self._manifest.dataset_id,
                partition=request.partition,
                dataset_sha256=str(attested["dataset_sha256"]),
                certification_manifest_sha256=self._manifest.manifest_sha256,
                payload_sha256=payload_sha256,
                files=files,
            )
        )
        return payload

    def _read_certified_payload(self) -> tuple[bytes, tuple[CertifiedFile, ...]]:
        manifest = self._manifest
        declared = {entry.path for entry in manifest.files}
        for path in sorted(manifest.root.rglob("*")):
            relative = path.relative_to(manifest.root).as_posix()
            if path.is_symlink():
                raise UncertifiedFilePresent(
                    f"certified dataset root holds a symbolic link {relative!r}; a certified "
                    "read is exactly the bytes under its own root"
                )
            if not path.is_file() or relative == CERTIFICATION_MANIFEST_NAME:
                continue
            if relative not in declared:
                raise UncertifiedFilePresent(
                    f"certified dataset root holds undigested file {relative!r}; a dataset "
                    "the manifest does not fully describe cannot be certified-read"
                )
        chunks: list[bytes] = [PAYLOAD_FRAMING_HEADER]
        read: list[CertifiedFile] = []
        for entry in sorted(manifest.files, key=lambda item: item.path):
            path = manifest.root / entry.path
            if not path.is_file():
                raise CertifiedFileMissing(
                    f"certified file {entry.path!r} is absent from {manifest.root}"
                )
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry.sha256:
                raise DatasetDigestMismatch(
                    f"certified file {entry.path!r} digest mismatch: manifest {entry.sha256}, "
                    f"bytes {digest}"
                )
            if len(data) != entry.size_bytes:
                raise DatasetDigestMismatch(
                    f"certified file {entry.path!r} length mismatch: manifest "
                    f"{entry.size_bytes}, bytes {len(data)}"
                )
            chunks.append(f"{entry.path}\n{len(data)}\n".encode())
            chunks.append(data)
            chunks.append(b"\n")
            read.append(entry)
        payload = b"".join(chunks)
        computed = hashlib.sha256(payload).hexdigest()
        if computed != manifest.dataset_sha256:
            raise DatasetDigestMismatch(
                f"certified dataset digest mismatch: manifest {manifest.dataset_sha256}, "
                f"bytes {computed}"
            )
        return payload, tuple(read)


def _validated_partitions(partitions: Sequence[object]) -> frozenset[str]:
    if not partitions:
        raise CertificationManifestInvalid(
            "a certified dataset must name at least one accessible partition"
        )
    normalized: list[str] = []
    for value in partitions:
        if not isinstance(value, str) or not value.strip():
            raise CertificationManifestInvalid(
                "each accessible partition must be a non-empty string"
            )
        normalized.append(_normalized_partition(value))
    if len(set(normalized)) != len(normalized):
        raise CertificationManifestInvalid("accessible partitions must be unique")
    forbidden = sorted(set(normalized) & _HOLDOUT_PARTITION_VOCABULARY)
    if forbidden:
        raise CertificationManifestInvalid(
            f"holdout vocabulary {forbidden} cannot be certified-accessible; unmasking a "
            "holdout is the holdout guardian's act, never a reader's"
        )
    return frozenset(normalized)


def _validated_files(raw: object) -> tuple[CertifiedFile, ...]:
    if not isinstance(raw, list) or not raw:
        raise CertificationManifestInvalid("certification manifest files must be a non-empty list")
    entries: list[CertifiedFile] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CertificationManifestInvalid(
                f"certification manifest files[{index}] must be an object"
            )
        _require_exact_keys(item, _FILE_KEYS, f"certification manifest files[{index}]")
        path = _required_string(item, "path")
        _require_dataset_relative_path(index, path)
        digest = _required_string(item, "sha256")
        _require_sha256(f"certification manifest files[{index}].sha256", digest)
        size = item.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CertificationManifestInvalid(
                f"certification manifest files[{index}].size_bytes must be a non-negative integer"
            )
        entries.append(CertifiedFile(path=path, sha256=digest, size_bytes=size))
    paths = [entry.path for entry in entries]
    if len(set(paths)) != len(paths):
        raise CertificationManifestInvalid("certification manifest files must have unique paths")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _require_dataset_relative_path(index: int, path: str) -> None:
    context = f"certification manifest files[{index}].path"
    if not path.strip():
        raise CertificationManifestInvalid(f"{context} must be non-empty")
    if path != path.strip():
        raise CertificationManifestInvalid(f"{context} must not be padded with whitespace")
    if "\\" in path or path.startswith("/"):
        raise CertificationManifestInvalid(f"{context} must be a relative POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CertificationManifestInvalid(
            f"{context} must not contain empty or traversal segments"
        )


def _require_exact_keys(
    mapping: Mapping[str, object], expected: frozenset[str], context: str
) -> None:
    present = set(mapping)
    if present != expected:
        raise CertificationManifestInvalid(
            f"{context} keys do not match schema; "
            f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}"
        )


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise CertificationManifestInvalid(f"certification manifest {key} must be a string")
    return value


def _require_sha256(name: str, value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(char not in _SHA256_ALPHABET for char in value):
        raise CertificationManifestInvalid(
            f"{name} must be a lowercase 64-character SHA-256 digest"
        )


def _require_utc_timestamp(name: str, value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CertificationManifestInvalid(f"{name} must be ISO-8601") from error
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise CertificationManifestInvalid(f"{name} must be UTC")


def _normalized_partition(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise CertificationManifestInvalid("partition identity must be non-empty")
    return normalized

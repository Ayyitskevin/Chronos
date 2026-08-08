"""Immutable content-addressed evidence objects for future trial replay.

This module preserves bytes and binds one trial attempt's replay identities.  It is
not a campaign re-execution engine, an environment attestation, or evidence that a
campaign is certified or ready for promotion.  A later trial broker must own registry
ordering and pass the resulting canonical receipt identities into :class:`ReplayEnvelope`.

Objects are addressed by SHA-256 and published without overwrite.  Every read verifies
the expected byte count, digest, owner, permissions, file type, and stable inode.  The
store's root is fixed at construction and all descendant traversal uses directory file
descriptors with symlink following disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPLAY_ENVELOPE_SCHEMA = "chronos-research-replay-envelope-v1"
CANONICAL_REPLAY_STORE_ROOT = Path("research/replay_store")
DEFAULT_MAX_OBJECT_BYTES = 1024 * 1024 * 1024
_MAX_ENVELOPE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CODE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ATTEMPT_ID_RE = re.compile(r"[0-9a-f]{32}")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_ENVELOPE_KEYS = {
    "schema_version",
    "campaign_id",
    "campaign_manifest_sha256",
    "trial_id",
    "attempt_id",
    "start_sequence",
    "start_record_hash",
    "code_commit",
    "config_digest",
    "criteria_digest",
    "data_catalog_sha256",
    "dataset_id",
    "partition",
    "data_version",
    "evaluator_id",
    "evaluator_digest",
    "inputs",
    "outputs",
}
_ARTIFACT_KEYS = {"role", "sha256", "byte_count"}


class ReplayStoreError(RuntimeError):
    """The replay store could not safely preserve or verify an object."""


class ReplayStorePathRefused(ReplayStoreError):
    """A store path was unsafe, replaced, or not private to its owner."""


class ReplayObjectMissing(ReplayStoreError):
    """A referenced content-addressed object is absent from the store."""


class ReplayStoreCorruption(ReplayStoreError):
    """Stored bytes or metadata do not match their immutable identity."""


class ReplayEnvelopeError(ReplayStoreError):
    """A replay envelope is not the exact supported canonical schema."""


@dataclass(frozen=True, slots=True, order=True)
class ReplayObjectRef:
    """Content identity and authenticated length of one stored byte object."""

    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _require_sha256("sha256", self.sha256, error_type=ValueError)
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("byte_count must be an integer >= 0")


@dataclass(frozen=True, slots=True)
class ReplayArtifact:
    """A semantic role bound to one content-addressed replay object."""

    role: str
    object_ref: ReplayObjectRef

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or _ROLE_RE.fullmatch(self.role) is None:
            raise ValueError("artifact role must be a bounded lowercase canonical identity")
        if not isinstance(self.object_ref, ReplayObjectRef):
            raise TypeError("object_ref must be ReplayObjectRef")


@dataclass(frozen=True, slots=True)
class ReplayEnvelope:
    """Canonical identity binding for one durably started trial attempt.

    The first six fields mirror the immutable identity of the canonical start receipt.
    Remaining fields bind the exact code, configuration, criteria, authenticated data,
    evaluator, and stored inputs/outputs used by that attempt.
    """

    campaign_id: str
    campaign_manifest_sha256: str
    trial_id: str
    attempt_id: str
    start_sequence: int
    start_record_hash: str
    code_commit: str
    config_digest: str
    criteria_digest: str
    data_catalog_sha256: str
    dataset_id: str
    partition: str
    data_version: str
    evaluator_id: str
    evaluator_digest: str
    inputs: tuple[ReplayArtifact, ...]
    outputs: tuple[ReplayArtifact, ...]

    def __post_init__(self) -> None:
        for name in ("campaign_id", "trial_id", "dataset_id", "partition", "evaluator_id"):
            _require_identity(name, getattr(self, name))
        for name in (
            "campaign_manifest_sha256",
            "start_record_hash",
            "config_digest",
            "criteria_digest",
            "data_catalog_sha256",
            "data_version",
            "evaluator_digest",
        ):
            _require_sha256(name, getattr(self, name), error_type=ValueError)
        if (
            not isinstance(self.attempt_id, str)
            or _ATTEMPT_ID_RE.fullmatch(self.attempt_id) is None
        ):
            raise ValueError("attempt_id must be 32 lowercase hexadecimal characters")
        if (
            not isinstance(self.code_commit, str)
            or _CODE_COMMIT_RE.fullmatch(self.code_commit) is None
        ):
            raise ValueError("code_commit must be a complete lowercase 40-character Git SHA")
        if (
            isinstance(self.start_sequence, bool)
            or not isinstance(self.start_sequence, int)
            or self.start_sequence < 0
        ):
            raise ValueError("start_sequence must be an integer >= 0")
        object.__setattr__(self, "inputs", _canonical_artifacts("inputs", self.inputs))
        object.__setattr__(self, "outputs", _canonical_artifacts("outputs", self.outputs))

    def to_bytes(self) -> bytes:
        """Return the unique compact JSON representation used as object bytes."""

        document: dict[str, object] = {
            "schema_version": REPLAY_ENVELOPE_SCHEMA,
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "start_sequence": self.start_sequence,
            "start_record_hash": self.start_record_hash,
            "code_commit": self.code_commit,
            "config_digest": self.config_digest,
            "criteria_digest": self.criteria_digest,
            "data_catalog_sha256": self.data_catalog_sha256,
            "dataset_id": self.dataset_id,
            "partition": self.partition,
            "data_version": self.data_version,
            "evaluator_id": self.evaluator_id,
            "evaluator_digest": self.evaluator_digest,
            "inputs": [_artifact_document(artifact) for artifact in self.inputs],
            "outputs": [_artifact_document(artifact) for artifact in self.outputs],
        }
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReplayEnvelope:
        """Parse an exact canonical envelope, rejecting aliases and extensions."""

        if not isinstance(raw, bytes):
            raise TypeError("raw envelope must be bytes")
        if len(raw) > _MAX_ENVELOPE_BYTES:
            raise ReplayEnvelopeError("replay envelope exceeds the supported size bound")
        document = _decode_exact_json_object(raw, context="replay envelope")
        _require_exact_keys(document, _ENVELOPE_KEYS, "replay envelope")
        if document.get("schema_version") != REPLAY_ENVELOPE_SCHEMA:
            raise ReplayEnvelopeError(
                f"unsupported replay envelope schema {document.get('schema_version')!r}"
            )
        inputs = _parse_artifacts(document.get("inputs"), context="inputs")
        outputs = _parse_artifacts(document.get("outputs"), context="outputs")
        try:
            envelope = cls(
                campaign_id=_string_field(document, "campaign_id"),
                campaign_manifest_sha256=_string_field(document, "campaign_manifest_sha256"),
                trial_id=_string_field(document, "trial_id"),
                attempt_id=_string_field(document, "attempt_id"),
                start_sequence=_integer_field(document, "start_sequence"),
                start_record_hash=_string_field(document, "start_record_hash"),
                code_commit=_string_field(document, "code_commit"),
                config_digest=_string_field(document, "config_digest"),
                criteria_digest=_string_field(document, "criteria_digest"),
                data_catalog_sha256=_string_field(document, "data_catalog_sha256"),
                dataset_id=_string_field(document, "dataset_id"),
                partition=_string_field(document, "partition"),
                data_version=_string_field(document, "data_version"),
                evaluator_id=_string_field(document, "evaluator_id"),
                evaluator_digest=_string_field(document, "evaluator_digest"),
                inputs=inputs,
                outputs=outputs,
            )
        except (TypeError, ValueError) as error:
            raise ReplayEnvelopeError(f"invalid replay envelope identity: {error}") from error
        if envelope.to_bytes() != raw:
            raise ReplayEnvelopeError("replay envelope bytes are not in canonical JSON form")
        return envelope


class ReplayObjectStore:
    """One fixed-root, immutable SHA-256 object store for replay evidence."""

    def __init__(self, *, max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES) -> None:
        """Open the one production replay store rooted in the trusted workspace."""

        self._initialize(
            Path(os.path.abspath(CANONICAL_REPLAY_STORE_ROOT)),
            max_object_bytes=max_object_bytes,
        )

    @classmethod
    def _for_tests(
        cls,
        root: Path,
        *,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
    ) -> ReplayObjectStore:
        """Private arbitrary-root seam for isolated tests only."""

        instance = cls.__new__(cls)
        instance._initialize(root, max_object_bytes=max_object_bytes)
        return instance

    def _initialize(self, root: Path, *, max_object_bytes: int) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be pathlib.Path")
        if (
            isinstance(max_object_bytes, bool)
            or not isinstance(max_object_bytes, int)
            or max_object_bytes <= 0
        ):
            raise ValueError("max_object_bytes must be an integer > 0")
        fixed_root = Path(os.path.abspath(root))
        if fixed_root.parent == fixed_root:
            raise ReplayStorePathRefused("the filesystem root cannot be a replay store")
        descriptor, created = _open_or_create_directory_path(
            fixed_root,
            context="replay store root",
        )
        try:
            if created:
                os.fchmod(descriptor, 0o700)
            metadata = _require_private_directory(descriptor, context="replay store root")
        finally:
            os.close(descriptor)

        self._root = fixed_root
        self._root_device = metadata.st_dev
        self._root_inode = metadata.st_ino
        self._owner_uid = metadata.st_uid
        self._max_object_bytes = max_object_bytes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_object_bytes(self) -> int:
        return self._max_object_bytes

    def put_bytes(self, content: bytes) -> ReplayObjectRef:
        """Publish bytes once and return their immutable content reference."""

        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if len(content) > self._max_object_bytes:
            raise ValueError("content exceeds this replay store's object-size bound")
        object_ref = ReplayObjectRef(
            sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
        )
        descriptors = self._open_object_bucket(object_ref.sha256, create=True)
        bucket = descriptors[-1]
        temporary_name: str | None = None
        try:
            try:
                self._read_object_from_bucket(bucket, object_ref)
            except ReplayObjectMissing:
                pass
            else:
                # A prior publication may have returned an fsync error after linking
                # this valid object. Re-sync the directory before treating it as
                # durably idempotent.
                _fsync(bucket, context="existing replay object bucket")
                return object_ref

            temporary_name = f".tmp-{os.getpid()}-{secrets.token_hex(16)}"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            temporary = os.open(temporary_name, flags, 0o600, dir_fd=bucket)
            try:
                os.fchmod(temporary, 0o600)
                _write_all(temporary, content)
                _fsync(temporary, context="replay object temporary file")
            finally:
                os.close(temporary)

            try:
                os.link(
                    temporary_name,
                    object_ref.sha256,
                    src_dir_fd=bucket,
                    dst_dir_fd=bucket,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._read_object_from_bucket(bucket, object_ref)
            except OSError as error:
                raise ReplayStoreError(
                    "replay object could not be atomically published without overwrite"
                ) from error
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=bucket)
                temporary_name = None
            _fsync(bucket, context="replay object bucket")
            self._read_object_from_bucket(bucket, object_ref)
            return object_ref
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=bucket)
                    _fsync(bucket, context="replay object cleanup")
                except FileNotFoundError:
                    pass
            _close_all(descriptors)

    def get_bytes(self, object_ref: ReplayObjectRef) -> bytes:
        """Return bytes only after verifying their complete immutable identity."""

        if not isinstance(object_ref, ReplayObjectRef):
            raise TypeError("object_ref must be ReplayObjectRef")
        if object_ref.byte_count > self._max_object_bytes:
            raise ReplayStoreCorruption(
                "object reference exceeds this replay store's object-size bound"
            )
        descriptors = self._open_object_bucket(object_ref.sha256, create=False)
        try:
            return self._read_object_from_bucket(descriptors[-1], object_ref)
        finally:
            _close_all(descriptors)

    def put_envelope(self, envelope: ReplayEnvelope) -> ReplayObjectRef:
        """Verify every referenced object, then preserve the canonical envelope."""

        if not isinstance(envelope, ReplayEnvelope):
            raise TypeError("envelope must be ReplayEnvelope")
        self._verify_artifact_objects(envelope)
        return self.put_bytes(envelope.to_bytes())

    def load_envelope(self, object_ref: ReplayObjectRef) -> ReplayEnvelope:
        """Load a canonical envelope and verify every object it binds."""

        envelope = ReplayEnvelope.from_bytes(self.get_bytes(object_ref))
        self._verify_artifact_objects(envelope)
        return envelope

    def load_envelope_by_sha256(self, digest: str) -> ReplayEnvelope:
        """Restart-safe lookup from a canonical terminal record's evidence digest.

        The registry intentionally retains the envelope SHA-256 without store-local
        length metadata.  Length is therefore discovered only from a safely opened,
        private regular object under this store's fixed root; the complete SHA-256 and
        every artifact reference are still verified before anything is returned.
        """

        _require_sha256("digest", digest, error_type=ValueError)
        descriptors = self._open_object_bucket(digest, create=False)
        try:
            bucket = descriptors[-1]
            object_ref = self._discover_object_ref(bucket, digest)
            envelope = ReplayEnvelope.from_bytes(self._read_object_from_bucket(bucket, object_ref))
        finally:
            _close_all(descriptors)
        self._verify_artifact_objects(envelope)
        return envelope

    def _verify_artifact_objects(self, envelope: ReplayEnvelope) -> None:
        for object_ref in sorted(
            {artifact.object_ref for artifact in (*envelope.inputs, *envelope.outputs)}
        ):
            self.get_bytes(object_ref)

    def _open_root(self) -> int:
        descriptor = _open_directory_path(self._root, context="replay store root")
        try:
            metadata = _require_private_directory(descriptor, context="replay store root")
            if (
                metadata.st_dev != self._root_device
                or metadata.st_ino != self._root_inode
                or metadata.st_uid != self._owner_uid
            ):
                raise ReplayStorePathRefused(
                    "the fixed replay store root was replaced after construction"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_object_bucket(self, digest: str, *, create: bool) -> list[int]:
        descriptors = [self._open_root()]
        try:
            for name in ("objects", "sha256", digest[:2]):
                descriptor = _open_private_child_directory(
                    descriptors[-1],
                    name,
                    create=create,
                    owner_uid=self._owner_uid,
                )
                descriptors.append(descriptor)
            return descriptors
        except BaseException:
            _close_all(descriptors)
            raise

    def _read_object_from_bucket(
        self, bucket_descriptor: int, object_ref: ReplayObjectRef
    ) -> bytes:
        try:
            path_metadata = os.stat(
                object_ref.sha256,
                dir_fd=bucket_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise ReplayObjectMissing(f"replay object {object_ref.sha256} is missing") from error
        _require_private_regular_file(
            path_metadata,
            owner_uid=self._owner_uid,
            context=f"replay object {object_ref.sha256}",
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(object_ref.sha256, flags, dir_fd=bucket_descriptor)
        except OSError as error:
            raise ReplayStoreCorruption(
                f"replay object {object_ref.sha256} could not be safely opened"
            ) from error
        try:
            opened = os.fstat(descriptor)
            _require_private_regular_file(
                opened,
                owner_uid=self._owner_uid,
                context=f"replay object {object_ref.sha256}",
            )
            if (
                path_metadata.st_dev != opened.st_dev
                or path_metadata.st_ino != opened.st_ino
                or opened.st_size != object_ref.byte_count
            ):
                raise ReplayStoreCorruption(
                    f"replay object {object_ref.sha256} identity or length changed"
                )
            content = _read_exact_object(descriptor, maximum_bytes=object_ref.byte_count)
            after = os.fstat(descriptor)
            if (
                opened.st_dev != after.st_dev
                or opened.st_ino != after.st_ino
                or opened.st_size != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns
            ):
                raise ReplayStoreCorruption(
                    f"replay object {object_ref.sha256} changed during verification"
                )
        finally:
            os.close(descriptor)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != object_ref.byte_count or actual_sha256 != object_ref.sha256:
            raise ReplayStoreCorruption(
                f"replay object {object_ref.sha256} failed byte-count or SHA-256 verification"
            )
        return content

    def _discover_object_ref(
        self,
        bucket_descriptor: int,
        digest: str,
    ) -> ReplayObjectRef:
        try:
            metadata = os.stat(
                digest,
                dir_fd=bucket_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise ReplayObjectMissing(f"replay object {digest} is missing") from error
        _require_private_regular_file(
            metadata,
            owner_uid=self._owner_uid,
            context=f"replay object {digest}",
        )
        if metadata.st_size > _MAX_ENVELOPE_BYTES:
            raise ReplayStoreCorruption(f"replay envelope {digest} exceeds the envelope-size bound")
        if metadata.st_size > self._max_object_bytes:
            raise ReplayStoreCorruption(
                f"replay object {digest} exceeds this store's object-size bound"
            )
        return ReplayObjectRef(sha256=digest, byte_count=metadata.st_size)


def _canonical_artifacts(
    name: str, artifacts: tuple[ReplayArtifact, ...]
) -> tuple[ReplayArtifact, ...]:
    if not isinstance(artifacts, tuple) or not artifacts:
        raise TypeError(f"{name} must be a non-empty tuple of ReplayArtifact")
    if len(artifacts) > 128:
        raise ValueError(f"{name} exceeds the supported artifact-count bound")
    if not all(isinstance(artifact, ReplayArtifact) for artifact in artifacts):
        raise TypeError(f"{name} must contain only ReplayArtifact values")
    roles = [artifact.role for artifact in artifacts]
    if len(set(roles)) != len(roles):
        raise ValueError(f"{name} contains duplicate artifact roles")
    return tuple(sorted(artifacts, key=lambda artifact: artifact.role))


def _artifact_document(artifact: ReplayArtifact) -> dict[str, object]:
    return {
        "role": artifact.role,
        "sha256": artifact.object_ref.sha256,
        "byte_count": artifact.object_ref.byte_count,
    }


def _parse_artifacts(value: object, *, context: str) -> tuple[ReplayArtifact, ...]:
    if not isinstance(value, list):
        raise ReplayEnvelopeError(f"{context} must be a list")
    artifacts: list[ReplayArtifact] = []
    for index, raw in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(raw, dict):
            raise ReplayEnvelopeError(f"{item_context} must be an object")
        _require_exact_keys(raw, _ARTIFACT_KEYS, item_context)
        role = _string_field(raw, "role")
        sha256 = _string_field(raw, "sha256")
        byte_count = _integer_field(raw, "byte_count")
        try:
            artifacts.append(
                ReplayArtifact(
                    role=role,
                    object_ref=ReplayObjectRef(sha256=sha256, byte_count=byte_count),
                )
            )
        except (TypeError, ValueError) as error:
            raise ReplayEnvelopeError(f"invalid {item_context}: {error}") from error
    return tuple(artifacts)


def _open_directory_path(path: Path, *, context: str) -> int:
    """Open an absolute directory path without following any component symlink."""

    absolute = Path(os.path.abspath(path))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(os.sep, flags)
        descriptors.append(current)
        for component in absolute.parts[1:]:
            current = os.open(component, flags, dir_fd=current)
            descriptors.append(current)
        result = descriptors.pop()
        return result
    except OSError as error:
        raise ReplayStorePathRefused(
            f"{context} and every ancestor must be non-symlink directories"
        ) from error
    finally:
        _close_all(descriptors)


def _open_or_create_directory_path(path: Path, *, context: str) -> tuple[int, bool]:
    """Open/create a directory path one no-follow component at a time."""

    absolute = Path(os.path.abspath(path))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptors: list[int] = []
    final_created = False
    try:
        current = os.open(os.sep, flags)
        descriptors.append(current)
        components = absolute.parts[1:]
        for index, component in enumerate(components):
            created = False
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    # A racing creator is accepted only if the following no-follow open
                    # proves it published a real directory.
                    pass
                except OSError as error:
                    raise ReplayStorePathRefused(
                        f"{context} could not be created under an authenticated parent"
                    ) from error
                try:
                    child = os.open(component, flags, dir_fd=current)
                except OSError as error:
                    raise ReplayStorePathRefused(
                        f"{context} and every ancestor must be non-symlink directories"
                    ) from error
            except OSError as error:
                raise ReplayStorePathRefused(
                    f"{context} and every ancestor must be non-symlink directories"
                ) from error
            if created:
                os.fchmod(child, 0o700)
                _fsync(child, context=f"new replay directory {component!r}")
                _fsync(current, context=f"parent of replay directory {component!r}")
            descriptors.append(child)
            current = child
            if index == len(components) - 1:
                final_created = created
        result = descriptors.pop()
        return result, final_created
    except ReplayStorePathRefused:
        raise
    except OSError as error:
        raise ReplayStorePathRefused(
            f"{context} and every ancestor must be non-symlink directories"
        ) from error
    finally:
        _close_all(descriptors)


def _open_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    owner_uid: int,
) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ReplayStorePathRefused(
                f"could not create private replay store directory {name!r}"
            ) from error
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError as error:
        raise ReplayObjectMissing("replay object namespace is missing") from error
    except OSError as error:
        raise ReplayStorePathRefused(f"replay store directory {name!r} is unsafe") from error
    try:
        if created:
            os.fchmod(descriptor, 0o700)
        metadata = _require_private_directory(
            descriptor,
            context=f"replay store directory {name!r}",
        )
        if metadata.st_uid != owner_uid:
            raise ReplayStorePathRefused(f"replay store directory {name!r} changed ownership")
        if created:
            _fsync(descriptor, context=f"new replay store directory {name!r}")
            _fsync(parent_descriptor, context="replay store parent directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_private_directory(descriptor: int, *, context: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReplayStorePathRefused(f"{context} is not a directory")
    if metadata.st_uid != os.geteuid():
        raise ReplayStorePathRefused(f"{context} is not owned by the effective user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ReplayStorePathRefused(f"{context} permissions must be exactly 0700")
    return metadata


def _require_private_regular_file(
    metadata: os.stat_result,
    *,
    owner_uid: int,
    context: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ReplayStoreCorruption(f"{context} is not a regular file")
    if metadata.st_uid != owner_uid:
        raise ReplayStoreCorruption(f"{context} is not owned by the store owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ReplayStoreCorruption(f"{context} permissions must be exactly 0600")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise ReplayStoreError("replay object write made no progress")
        offset += written


def _read_exact_object(descriptor: int, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum_bytes:
            raise ReplayStoreCorruption("replay object exceeds its authenticated byte count")
        chunks.append(chunk)


def _fsync(descriptor: int, *, context: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise ReplayStoreError(f"fsync failed for {context}") from error


def _close_all(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _decode_exact_json_object(raw: bytes, *, context: str) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReplayEnvelopeError(f"{context} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ReplayEnvelopeError(f"{context} contains non-finite number {value}")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise ReplayEnvelopeError(f"{context} is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise ReplayEnvelopeError(f"{context} is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ReplayEnvelopeError(f"{context} root must be an object")
    return decoded


def _require_exact_keys(value: dict[str, object], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ReplayEnvelopeError(
            f"{context} keys differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _string_field(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ReplayEnvelopeError(f"{key} must be a string")
    return item


def _integer_field(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ReplayEnvelopeError(f"{key} must be an integer")
    return item


def _require_identity(name: str, value: str) -> None:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded canonical identity string")


def _require_sha256(
    name: str,
    value: str,
    *,
    error_type: type[ValueError] = ValueError,
) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise error_type(f"{name} must be a lowercase SHA-256 digest")

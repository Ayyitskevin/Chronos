"""Immutable SHA-256 replay objects and per-attempt identity envelopes."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from chronos.research.replay_store import (
    CANONICAL_REPLAY_STORE_ROOT,
    REPLAY_ENVELOPE_SCHEMA,
    ReplayArtifact,
    ReplayEnvelope,
    ReplayEnvelopeError,
    ReplayObjectMissing,
    ReplayObjectRef,
    ReplayObjectStore,
    ReplayStoreCorruption,
    ReplayStorePathRefused,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64
_DIGEST_E = "e" * 64


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _object_path(store: ReplayObjectStore, object_ref: ReplayObjectRef) -> Path:
    return store.root / "objects" / "sha256" / object_ref.sha256[:2] / object_ref.sha256


def _artifact(role: str, object_ref: ReplayObjectRef) -> ReplayArtifact:
    return ReplayArtifact(role=role, object_ref=object_ref)


def _envelope(
    input_ref: ReplayObjectRef,
    output_ref: ReplayObjectRef,
    **overrides: Any,
) -> ReplayEnvelope:
    values: dict[str, Any] = {
        "campaign_id": "five-tool-v3.6",
        "campaign_manifest_sha256": _DIGEST_A,
        "trial_id": "5t-trial-001",
        "attempt_id": "1" * 32,
        "start_sequence": 7,
        "start_record_hash": _DIGEST_B,
        "code_commit": "c" * 40,
        "config_digest": _DIGEST_C,
        "criteria_digest": _DIGEST_D,
        "data_catalog_sha256": _DIGEST_E,
        "dataset_id": "spy-minute-bars",
        "partition": "dev",
        "data_version": input_ref.sha256,
        "evaluator_id": "five-tool-evaluator-v1",
        "evaluator_digest": _DIGEST_A,
        "inputs": (_artifact("certified-data", input_ref),),
        "outputs": (_artifact("evaluation-evidence", output_ref),),
    }
    values.update(overrides)
    return ReplayEnvelope(**values)


def test_public_store_owns_one_canonical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_workspace = tmp_path / "initial"
    later_workspace = tmp_path / "later"
    initial_workspace.mkdir()
    later_workspace.mkdir()
    monkeypatch.chdir(initial_workspace)
    store = ReplayObjectStore()
    expected = initial_workspace / CANONICAL_REPLAY_STORE_ROOT

    monkeypatch.chdir(later_workspace)
    assert store.put_bytes(b"stable-root").sha256 == _sha256(b"stable-root")
    assert store.root == expected
    assert inspect.signature(ReplayObjectStore).parameters["max_object_bytes"].kind.name == (
        "KEYWORD_ONLY"
    )
    with pytest.raises(TypeError):
        ReplayObjectStore(tmp_path / "caller-selected")  # type: ignore[call-arg]


def test_put_get_is_idempotent_content_addressed_and_owner_only(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    content = b"exact replay evidence"

    first = store.put_bytes(content)
    second = store.put_bytes(content)

    assert first == second == ReplayObjectRef(_sha256(content), len(content))
    assert store.get_bytes(first) == content
    object_path = _object_path(store, first)
    assert stat.S_IMODE(object_path.stat().st_mode) == 0o600
    for directory in (
        store.root,
        store.root / "objects",
        store.root / "objects/sha256",
        object_path.parent,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(object_path.parent.glob(".tmp-*")) == []


def test_empty_object_has_a_stable_verified_identity(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")

    object_ref = store.put_bytes(b"")

    assert object_ref.sha256 == hashlib.sha256(b"").hexdigest()
    assert object_ref.byte_count == 0
    assert store.get_bytes(object_ref) == b""


def test_idempotent_existing_object_reasserts_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    content = b"already published"
    expected = store.put_bytes(content)
    calls = 0
    original_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    assert store.put_bytes(content) == expected
    assert calls >= 1


def test_object_size_and_input_type_are_bounded(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects", max_object_bytes=4)

    with pytest.raises(ValueError, match="size bound"):
        store.put_bytes(b"12345")
    with pytest.raises(TypeError, match="bytes"):
        store.put_bytes(bytearray(b"1234"))  # type: ignore[arg-type]
    with pytest.raises(ReplayStoreCorruption, match="size bound"):
        store.get_bytes(ReplayObjectRef("0" * 64, 5))


def test_concurrent_identical_puts_publish_one_complete_object(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    content = b"one immutable object under a publication race" * 4096

    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = tuple(executor.map(store.put_bytes, [content] * 24))

    assert len(set(refs)) == 1
    assert store.get_bytes(refs[0]) == content
    object_path = _object_path(store, refs[0])
    assert [path.name for path in object_path.parent.iterdir()] == [refs[0].sha256]


def test_tampered_existing_object_is_refused_and_never_overwritten(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    content = b"original immutable object"
    object_ref = store.put_bytes(content)
    object_path = _object_path(store, object_ref)
    tampered = b"x" * len(content)
    object_path.write_bytes(tampered)
    os.chmod(object_path, 0o600)

    with pytest.raises(ReplayStoreCorruption, match="SHA-256"):
        store.get_bytes(object_ref)
    with pytest.raises(ReplayStoreCorruption, match="SHA-256"):
        store.put_bytes(content)
    assert object_path.read_bytes() == tampered


def test_object_permission_drift_is_refused(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    object_ref = store.put_bytes(b"private evidence")
    object_path = _object_path(store, object_ref)
    os.chmod(object_path, 0o644)

    with pytest.raises(ReplayStoreCorruption, match="0600"):
        store.get_bytes(object_ref)


@pytest.mark.parametrize("replacement", ["symlink", "directory", "fifo"])
def test_nonregular_object_replacement_is_refused(tmp_path: Path, replacement: str) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    object_ref = store.put_bytes(b"replace-me")
    object_path = _object_path(store, object_ref)
    object_path.unlink()
    if replacement == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"replace-me")
        object_path.symlink_to(outside)
    elif replacement == "directory":
        object_path.mkdir()
    else:
        os.mkfifo(object_path)

    with pytest.raises(ReplayStoreCorruption, match="regular"):
        store.get_bytes(object_ref)


def test_missing_object_is_not_silently_treated_as_empty(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    missing = b"missing"

    with pytest.raises(ReplayObjectMissing):
        store.get_bytes(ReplayObjectRef(_sha256(missing), len(missing)))


def test_store_rejects_symlink_and_nonprivate_roots(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    broad_root = tmp_path / "broad-root"
    broad_root.mkdir(mode=0o700)
    os.chmod(broad_root, 0o755)

    with pytest.raises(ReplayStorePathRefused):
        ReplayObjectStore._for_tests(linked_root)
    with pytest.raises(ReplayStorePathRefused, match="0700"):
        ReplayObjectStore._for_tests(broad_root)


@pytest.mark.parametrize("precreated_target", [False, True])
def test_store_refuses_symlinked_parent_before_any_outside_mutation(
    tmp_path: Path,
    precreated_target: bool,
) -> None:
    lexical_parent = tmp_path / "lexical-parent"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    escaped_root = outside / "replay-objects"
    marker = outside / "marker"
    marker.write_bytes(b"outside-must-remain-byte-identical")
    os.chmod(marker, 0o600)
    if precreated_target:
        escaped_root.mkdir(mode=0o700)
        sentinel = escaped_root / "sentinel"
        sentinel.write_bytes(b"preexisting-tree")
        os.chmod(sentinel, 0o600)
    lexical_parent.symlink_to(outside, target_is_directory=True)

    before_entries = sorted(path.relative_to(outside) for path in outside.rglob("*"))
    before_marker = marker.read_bytes()
    before_marker_mode = stat.S_IMODE(marker.stat().st_mode)

    with pytest.raises(ReplayStorePathRefused, match="ancestor"):
        ReplayObjectStore._for_tests(lexical_parent / "replay-objects")

    assert sorted(path.relative_to(outside) for path in outside.rglob("*")) == before_entries
    assert marker.read_bytes() == before_marker
    assert stat.S_IMODE(marker.stat().st_mode) == before_marker_mode
    assert escaped_root.exists() is precreated_target
    assert not (escaped_root / "objects").exists()


def test_store_refuses_replaced_fixed_root(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    displaced = tmp_path / "displaced-store"
    store.root.rename(displaced)
    store.root.mkdir(mode=0o700)

    with pytest.raises(ReplayStorePathRefused, match="replaced"):
        store.put_bytes(b"must-not-enter-replacement")


def test_store_refuses_parent_replacement_with_symlink(tmp_path: Path) -> None:
    lexical_parent = tmp_path / "lexical-parent"
    lexical_parent.mkdir(mode=0o700)
    store = ReplayObjectStore._for_tests(lexical_parent / "replay-objects")
    displaced = tmp_path / "displaced-parent"
    lexical_parent.rename(displaced)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "replay-objects").mkdir(mode=0o700)
    lexical_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReplayStorePathRefused, match="ancestor"):
        store.put_bytes(b"must-not-enter-replaced-parent")

    assert not (outside / "replay-objects" / "objects").exists()


def test_replay_envelope_is_deterministic_exact_and_round_trips(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_a = store.put_bytes(b"input-a")
    input_z = store.put_bytes(b"input-z")
    output = store.put_bytes(b"output")
    unordered = _envelope(
        input_a,
        output,
        inputs=(
            _artifact("z-input", input_z),
            _artifact("a-input", input_a),
        ),
    )
    ordered = _envelope(
        input_a,
        output,
        inputs=(
            _artifact("a-input", input_a),
            _artifact("z-input", input_z),
        ),
    )

    assert unordered.inputs == ordered.inputs
    assert unordered.to_bytes() == ordered.to_bytes()
    assert ReplayEnvelope.from_bytes(unordered.to_bytes()) == unordered
    document = json.loads(unordered.to_bytes())
    assert document["schema_version"] == REPLAY_ENVELOPE_SCHEMA
    assert document["campaign_manifest_sha256"] == _DIGEST_A
    assert document["attempt_id"] == "1" * 32
    assert document["start_sequence"] == 7
    assert document["start_record_hash"] == _DIGEST_B
    assert document["code_commit"] == "c" * 40
    assert document["config_digest"] == _DIGEST_C
    assert document["criteria_digest"] == _DIGEST_D
    assert document["data_catalog_sha256"] == _DIGEST_E
    assert document["data_version"] == input_a.sha256
    assert document["evaluator_digest"] == _DIGEST_A


def test_put_and_load_envelope_verify_all_bound_objects(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"certified input")
    output_ref = store.put_bytes(b"evaluation output")
    envelope = _envelope(input_ref, output_ref)

    envelope_ref = store.put_envelope(envelope)

    assert store.get_bytes(envelope_ref) == envelope.to_bytes()
    assert store.load_envelope(envelope_ref) == envelope


def test_envelope_is_restart_retrievable_from_terminal_digest_alone(tmp_path: Path) -> None:
    root = tmp_path / "replay-objects"
    first_process = ReplayObjectStore._for_tests(root)
    input_ref = first_process.put_bytes(b"certified input")
    output_ref = first_process.put_bytes(b"evaluation output")
    envelope = _envelope(input_ref, output_ref)
    terminal_evidence_digest = first_process.put_envelope(envelope).sha256

    restarted_store = ReplayObjectStore._for_tests(root)

    assert restarted_store.load_envelope_by_sha256(terminal_evidence_digest) == envelope


def test_digest_addressed_envelope_lookup_refuses_missing_object(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")

    with pytest.raises(ReplayObjectMissing):
        store.load_envelope_by_sha256("0" * 64)


def test_digest_addressed_envelope_lookup_refuses_tampered_bytes(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"certified input")
    output_ref = store.put_bytes(b"evaluation output")
    envelope_ref = store.put_envelope(_envelope(input_ref, output_ref))
    envelope_path = _object_path(store, envelope_ref)
    envelope_path.write_bytes(b"x" * envelope_ref.byte_count)
    os.chmod(envelope_path, 0o600)

    with pytest.raises(ReplayStoreCorruption, match="SHA-256"):
        store.load_envelope_by_sha256(envelope_ref.sha256)


def test_digest_addressed_envelope_rejects_oversize_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReplayObjectStore._for_tests(
        tmp_path / "replay-objects",
        max_object_bytes=2 * 1024 * 1024,
    )
    oversized = store.put_bytes(b"x" * (1024 * 1024 + 1))
    read_attempted = False

    def poison_read(*_args: object, **_kwargs: object) -> bytes:
        nonlocal read_attempted
        read_attempted = True
        raise AssertionError("oversized envelope bytes must not be materialized")

    monkeypatch.setattr(store, "_read_object_from_bucket", poison_read)

    with pytest.raises(ReplayStoreCorruption, match="envelope-size bound"):
        store.load_envelope_by_sha256(oversized.sha256)
    assert read_attempted is False


def test_put_envelope_refuses_missing_bound_object(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    output_ref = store.put_bytes(b"evaluation output")
    missing_content = b"never-preserved-input"
    missing_ref = ReplayObjectRef(_sha256(missing_content), len(missing_content))

    with pytest.raises(ReplayObjectMissing):
        store.put_envelope(_envelope(missing_ref, output_ref))


def test_load_envelope_refuses_later_artifact_tampering(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"certified input")
    output_ref = store.put_bytes(b"evaluation output")
    envelope_ref = store.put_envelope(_envelope(input_ref, output_ref))
    input_path = _object_path(store, input_ref)
    input_path.write_bytes(b"x" * input_ref.byte_count)
    os.chmod(input_path, 0o600)

    with pytest.raises(ReplayStoreCorruption):
        store.load_envelope(envelope_ref)


@pytest.mark.parametrize("mutation", ["extra", "missing", "empty-inputs", "nonfinite"])
def test_envelope_parser_rejects_nonexact_schema(tmp_path: Path, mutation: str) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"input")
    output_ref = store.put_bytes(b"output")
    document = json.loads(_envelope(input_ref, output_ref).to_bytes())
    if mutation == "extra":
        document["extension"] = "not-allowed"
    elif mutation == "missing":
        document.pop("criteria_digest")
    elif mutation == "empty-inputs":
        document["inputs"] = []
    else:
        document["start_sequence"] = float("nan")
    raw = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=True,
    ).encode()

    with pytest.raises(ReplayEnvelopeError):
        ReplayEnvelope.from_bytes(raw)


def test_envelope_parser_rejects_duplicate_keys_and_noncanonical_bytes(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"input")
    output_ref = store.put_bytes(b"output")
    raw = _envelope(input_ref, output_ref).to_bytes()
    duplicate = raw.replace(
        b'{"attempt_id":',
        b'{"attempt_id":"22222222222222222222222222222222","attempt_id":',
        1,
    )

    with pytest.raises(ReplayEnvelopeError, match="duplicate"):
        ReplayEnvelope.from_bytes(duplicate)
    with pytest.raises(ReplayEnvelopeError, match="canonical"):
        ReplayEnvelope.from_bytes(b" " + raw)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("attempt_id", "not-an-attempt"),
        ("code_commit", "unknown"),
        ("code_commit", "d" * 64),
        ("start_sequence", True),
        ("evaluator_digest", "A" * 64),
        ("inputs", ()),
    ],
)
def test_envelope_constructor_refuses_ambiguous_identities(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    input_ref = store.put_bytes(b"input")
    output_ref = store.put_bytes(b"output")

    with pytest.raises((TypeError, ValueError)):
        _envelope(input_ref, output_ref, **{field: bad_value})


def test_duplicate_artifact_roles_are_refused(tmp_path: Path) -> None:
    store = ReplayObjectStore._for_tests(tmp_path / "replay-objects")
    first = store.put_bytes(b"first")
    second = store.put_bytes(b"second")
    output = store.put_bytes(b"output")

    with pytest.raises(ValueError, match="duplicate"):
        _envelope(
            first,
            output,
            inputs=(
                _artifact("same-role", first),
                _artifact("same-role", second),
            ),
        )

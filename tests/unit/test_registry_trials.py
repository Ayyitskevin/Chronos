"""Canonical trial lifecycle, multiplicity, and shared-writer safety tests."""

from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from chronos.registry import (
    CANONICAL_REGISTRY_LEDGER_PATH,
    KIND_RUN,
    KIND_TRIAL_STARTED,
    KIND_TRIAL_TERMINAL,
    TRIAL_SCHEMA_VERSION,
    CanonicalTrialError,
    CanonicalTrialReceipt,
    CanonicalTrialRegistry,
    RegistryIntegrityError,
    RegistryLedger,
    RunStage,
    register_run,
)
from chronos.registry.ledger import verified_registry_transaction

_MANIFEST_SHA = "a" * 64
_CONFIG_SHA = "b" * 64
_COMMIT = "c" * 40
_EVIDENCE_SHA = "e" * 64

_START_KEYS = {
    "schema_version",
    "attempt_id",
    "trial_id",
    "campaign_id",
    "campaign_manifest_sha256",
    "stage",
    "strategy_id",
    "config_hash",
    "code_commit",
    "data_hashes",
    "criteria_ref",
    "touched_data",
}
_TERMINAL_KEYS = {
    "schema_version",
    "attempt_id",
    "trial_id",
    "campaign_id",
    "campaign_manifest_sha256",
    "start_sequence",
    "start_record_hash",
    "outcome",
    "evidence_digest",
    "error_type",
}


def _registry(tmp_path: Path) -> CanonicalTrialRegistry:
    return CanonicalTrialRegistry._for_tests(tmp_path / "registry.jsonl")


def _start(
    registry: CanonicalTrialRegistry,
    trial_id: str,
    *,
    attempt_id: str | None = None,
) -> CanonicalTrialReceipt:
    return registry.start_trial(
        trial_id=trial_id,
        campaign_id="campaign-1",
        campaign_manifest_sha256=_MANIFEST_SHA,
        stage=RunStage.DEV,
        strategy_id="five-tool",
        config_hash=_CONFIG_SHA,
        code_commit=_COMMIT,
        data_hashes={"dataset": {"sha256": "d" * 64}},
        criteria_ref="criteria@v1",
        attempt_id=attempt_id,
    )


def _process_start(path: str, index: int) -> None:
    registry = CanonicalTrialRegistry._for_tests(Path(path))
    _start(registry, f"process-trial-{index}")


def test_public_capability_has_one_non_configurable_canonical_path(tmp_path: Path) -> None:
    assert inspect.signature(CanonicalTrialRegistry).parameters == {}
    assert CanonicalTrialRegistry().ledger_path == Path(
        os.path.abspath(CANONICAL_REGISTRY_LEDGER_PATH)
    )
    with pytest.raises(TypeError):
        CanonicalTrialRegistry(tmp_path / "caller-selected.jsonl")  # type: ignore[call-arg]


def test_canonical_path_is_frozen_across_working_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_workspace = tmp_path / "initial"
    later_workspace = tmp_path / "later"
    initial_workspace.mkdir()
    later_workspace.mkdir()
    monkeypatch.chdir(initial_workspace)
    registry = CanonicalTrialRegistry()
    expected_path = initial_workspace / CANONICAL_REGISTRY_LEDGER_PATH

    monkeypatch.chdir(later_workspace)
    receipt = _start(registry, "trial-after-chdir")
    registry.terminalize_failed(receipt, error_type="ExpectedFailure")

    assert registry.ledger_path == expected_path
    assert expected_path.exists()
    assert not (later_workspace / CANONICAL_REGISTRY_LEDGER_PATH).exists()
    assert registry.multiplicity_snapshot().count == 1


def test_fresh_canonical_parent_directories_are_fsynced_before_start_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    registry = CanonicalTrialRegistry()
    directory_fsyncs: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def tracked_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            directory_fsyncs.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    _start(registry, "durable-first-start")

    research = workspace / "research"
    registry_parent = research / "registry"
    identities = [
        (path.stat().st_dev, path.stat().st_ino)
        for path in (research, workspace, registry_parent, research)
    ]
    assert directory_fsyncs[:4] == identities


def test_canonical_capability_refuses_a_symlinked_workspace_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    victim = tmp_path / "victim"
    workspace.mkdir()
    victim.mkdir()
    (workspace / "research").symlink_to(victim, target_is_directory=True)
    monkeypatch.chdir(workspace)

    with pytest.raises(RegistryIntegrityError, match=r"symlink|real directory"):
        CanonicalTrialRegistry()
    assert list(victim.iterdir()) == []


def test_canonical_capability_refuses_workspace_replacement_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    displaced = tmp_path / "displaced"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    registry = CanonicalTrialRegistry()
    monkeypatch.chdir(tmp_path)
    workspace.rename(displaced)
    workspace.mkdir()

    with pytest.raises(RegistryIntegrityError, match=r"replaced|disappeared"):
        _start(registry, "must-not-write")
    assert list(workspace.iterdir()) == []
    assert list(displaced.iterdir()) == []


class _AfterAppendError(RuntimeError):
    pass


def test_verified_transaction_rechecks_after_an_append_then_exception(tmp_path: Path) -> None:
    path = tmp_path / "registry.jsonl"
    with (
        pytest.raises(RegistryIntegrityError, match="verification"),
        verified_registry_transaction(path) as ledger,
    ):
        ledger.append("test_record", {"value": "original"})
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["payload"]["value"] = "tampered"
        path.write_text(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        raise _AfterAppendError("body failed after its append")


def test_start_is_the_counting_event_and_has_the_exact_v1_schema(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    receipt = _start(registry, "trial-1", attempt_id="1" * 32)

    records = RegistryLedger(registry.ledger_path).records()
    assert len(records) == 1
    start = records[0]
    assert start.kind == KIND_TRIAL_STARTED
    assert set(start.payload) == _START_KEYS
    assert start.payload["schema_version"] == TRIAL_SCHEMA_VERSION
    assert start.payload["touched_data"] is True
    assert receipt.start_sequence == 0
    assert receipt.start_record_hash == start.record_hash

    # No reader or evaluator outcome is needed for the durable start to count.
    assert registry.multiplicity_snapshot().count == 1


def test_failures_orphans_and_retries_remain_counted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    failed = _start(registry, "same-semantic-trial", attempt_id="1" * 32)
    failure = registry.terminalize_failed(failed, error_type="DatasetReadError")
    assert set(failure.payload) == _TERMINAL_KEYS
    assert failure.payload["outcome"] == "failed"
    assert registry.multiplicity_snapshot().count == 1

    completed_retry = _start(registry, "same-semantic-trial", attempt_id="2" * 32)
    completion = registry._complete_with_retained_evidence(
        completed_retry,
        evidence_digest=_EVIDENCE_SHA,
    )
    assert completion.kind == KIND_TRIAL_TERMINAL
    assert set(completion.payload) == _TERMINAL_KEYS
    assert completion.payload["evidence_digest"] == _EVIDENCE_SHA

    _start(registry, "interrupted-orphan", attempt_id="3" * 32)
    snapshot = registry.multiplicity_snapshot()
    assert snapshot.count == 3
    assert snapshot.record_count == 5
    assert snapshot.head_hash == RegistryLedger(registry.ledger_path).records()[-1].record_hash


def test_completed_attempt_returns_full_verified_start_and_terminal_identity(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    failed = _start(registry, "failed", attempt_id="1" * 32)
    registry.terminalize_failed(failed, error_type="ExpectedFailure")
    receipt = _start(registry, "completed", attempt_id="2" * 32)
    terminal = registry._complete_with_retained_evidence(
        receipt,
        evidence_digest=_EVIDENCE_SHA,
    )

    assert registry.completed_attempt(failed.attempt_id) is None
    assert registry.completed_attempt("9" * 32) is None
    completed = registry.completed_attempt(receipt.attempt_id)
    assert completed is not None
    assert completed.receipt == receipt
    assert completed.stage is RunStage.DEV
    assert completed.strategy_id == "five-tool"
    assert completed.config_hash == _CONFIG_SHA
    assert completed.code_commit == _COMMIT
    assert completed.data_hashes == {"dataset": {"sha256": "d" * 64}}
    assert completed.criteria_ref == "criteria@v1"
    assert completed.evidence_digest == _EVIDENCE_SHA
    assert completed.terminal_record_hash == terminal.record_hash
    with pytest.raises(TypeError):
        completed.data_hashes["mutated"] = True  # type: ignore[index]


def test_duplicate_attempt_and_second_terminal_are_refused(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    receipt = _start(registry, "trial-1", attempt_id="1" * 32)
    with pytest.raises(CanonicalTrialError, match="already registered"):
        _start(registry, "different-trial", attempt_id=receipt.attempt_id)

    registry._complete_with_retained_evidence(
        receipt,
        evidence_digest=_EVIDENCE_SHA,
    )
    with pytest.raises(CanonicalTrialError, match="already has a terminal"):
        registry._complete_with_retained_evidence(
            receipt,
            evidence_digest=_EVIDENCE_SHA,
        )
    assert registry.multiplicity_snapshot().count == 1


def test_explicit_empty_attempt_id_is_refused_instead_of_replaced(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ValueError, match="attempt_id"):
        _start(registry, "trial-1", attempt_id="")
    assert not registry.ledger_path.exists()


def test_terminal_is_bound_to_the_exact_start_receipt(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    receipt = _start(registry, "trial-1", attempt_id="1" * 32)
    forged = replace(receipt, start_record_hash="f" * 64)
    with pytest.raises(CanonicalTrialError, match="disagrees"):
        registry._complete_with_retained_evidence(
            forged,
            evidence_digest=_EVIDENCE_SHA,
        )

    missing = replace(receipt, attempt_id="9" * 32)
    with pytest.raises(CanonicalTrialError, match="no canonical durable start"):
        registry.terminalize_failed(missing, error_type="EvaluationError")


def test_multiplicity_includes_legacy_touches_without_double_counting_shared_ids(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    shared_id = "1" * 32
    _start(registry, "trial-1", attempt_id=shared_id)
    stale_legacy_writer = RegistryLedger(registry.ledger_path)

    register_run(
        stale_legacy_writer,
        stage=RunStage.DEV,
        strategy_id="five-tool",
        config_hash=_CONFIG_SHA,
        code_commit=_COMMIT,
        data_hashes={"dataset": {"sha256": "d" * 64}},
        criteria_ref="criteria@v1",
        touched_data=True,
        experiment_id=shared_id,
    )
    register_run(
        stale_legacy_writer,
        stage=RunStage.DEV,
        strategy_id="legacy",
        config_hash="legacy-cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "legacy"}},
        criteria_ref="legacy@v1",
        touched_data=True,
        experiment_id="unique-legacy-touch",
    )
    register_run(
        stale_legacy_writer,
        stage=RunStage.DEV,
        strategy_id="legacy",
        config_hash="legacy-cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "legacy"}},
        criteria_ref="legacy@v1",
        touched_data=False,
        experiment_id="metadata-only",
    )

    snapshot = registry.multiplicity_snapshot()
    assert snapshot.count == 2
    assert snapshot.record_count == 4
    assert RegistryLedger(registry.ledger_path).verify()[0] is True


def test_cross_kind_shared_id_requires_exact_provenance_match(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    shared_id = "1" * 32
    _start(registry, "trial-1", attempt_id=shared_id)
    register_run(
        RegistryLedger(registry.ledger_path),
        stage=RunStage.HOLDOUT,
        strategy_id="unrelated-strategy",
        config_hash="unrelated-config",
        code_commit="abc123",
        data_hashes={"QQQ": {"bars_sha": "different"}},
        criteria_ref="other-criteria",
        touched_data=True,
        experiment_id=shared_id,
    )

    with pytest.raises(CanonicalTrialError, match="mismatched provenance"):
        registry.multiplicity_snapshot()


@pytest.mark.parametrize(
    ("start_value", "legacy_value"),
    ((True, 1), (1, 1.0)),
    ids=("bool-is-not-int", "int-is-not-float"),
)
def test_cross_kind_mirror_uses_type_exact_canonical_json(
    tmp_path: Path,
    start_value: object,
    legacy_value: object,
) -> None:
    registry = _registry(tmp_path)
    shared_id = "1" * 32
    registry.start_trial(
        trial_id="trial-1",
        campaign_id="campaign-1",
        campaign_manifest_sha256=_MANIFEST_SHA,
        stage=RunStage.DEV,
        strategy_id="five-tool",
        config_hash=_CONFIG_SHA,
        code_commit=_COMMIT,
        data_hashes={"dataset": {"sha256": "d" * 64, "aliased": start_value}},
        criteria_ref="criteria@v1",
        attempt_id=shared_id,
    )
    register_run(
        RegistryLedger(registry.ledger_path),
        stage=RunStage.DEV,
        strategy_id="five-tool",
        config_hash=_CONFIG_SHA,
        code_commit=_COMMIT,
        data_hashes={"dataset": {"sha256": "d" * 64, "aliased": legacy_value}},
        criteria_ref="criteria@v1",
        touched_data=True,
        experiment_id=shared_id,
    )

    with pytest.raises(CanonicalTrialError, match="mismatched provenance"):
        registry.multiplicity_snapshot()


def test_duplicate_legacy_experiment_ids_are_refused_or_fail_closed(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    ledger = RegistryLedger(registry.ledger_path)
    first = register_run(
        ledger,
        stage=RunStage.DEV,
        strategy_id="legacy",
        config_hash="legacy-cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "legacy"}},
        criteria_ref="legacy@v1",
        experiment_id="one-touch-one-id",
    )
    with pytest.raises(ValueError, match="already registered"):
        register_run(
            ledger,
            stage=RunStage.DEV,
            strategy_id="legacy",
            config_hash="legacy-cfg",
            code_commit="abc123",
            data_hashes={"SPY": {"bars_sha": "legacy"}},
            criteria_ref="legacy@v1",
            experiment_id="one-touch-one-id",
        )

    # A pre-existing or directly forged duplicate must invalidate the canonical
    # snapshot instead of being silently collapsed by set arithmetic.
    RegistryLedger(registry.ledger_path).append(KIND_RUN, dict(first.payload))
    with pytest.raises(CanonicalTrialError, match="duplicate legacy"):
        registry.multiplicity_snapshot()


def test_legacy_id_already_in_use_blocks_a_v1_start(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    identifier = "1" * 32
    register_run(
        RegistryLedger(registry.ledger_path),
        stage=RunStage.DEV,
        strategy_id="legacy",
        config_hash="legacy-cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "legacy"}},
        criteria_ref="legacy@v1",
        experiment_id=identifier,
    )
    with pytest.raises(CanonicalTrialError, match="already registered"):
        _start(registry, "trial-1", attempt_id=identifier)


def test_hash_tampering_and_semantic_corruption_both_fail_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _start(registry, "trial-1")
    lines = registry.ledger_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["payload"]["trial_id"] = "tampered"
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    registry.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RegistryIntegrityError, match="verification"):
        registry.multiplicity_snapshot()
    with pytest.raises(RegistryIntegrityError, match="verification"):
        _start(registry, "trial-2")

    malformed = _registry(tmp_path / "semantic")
    RegistryLedger(malformed.ledger_path).append(KIND_TRIAL_STARTED, {"bad": True})
    with pytest.raises(CanonicalTrialError, match="keys do not match schema"):
        malformed.multiplicity_snapshot()


def test_threaded_starts_and_legacy_writes_share_one_intact_chain(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    stale_legacy_writer = RegistryLedger(registry.ledger_path)

    def write_start(index: int) -> None:
        _start(registry, f"thread-trial-{index}")

    def write_legacy(index: int) -> None:
        register_run(
            stale_legacy_writer,
            stage=RunStage.DEV,
            strategy_id="legacy",
            config_hash="legacy-cfg",
            code_commit="abc123",
            data_hashes={"SPY": {"bars_sha": "legacy"}},
            criteria_ref="legacy@v1",
            experiment_id=f"legacy-thread-{index}",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(write_start, index) for index in range(12)]
        futures.extend(pool.submit(write_legacy, index) for index in range(12))
        for future in futures:
            future.result()

    ledger = RegistryLedger(registry.ledger_path)
    assert ledger.verify()[0] is True
    assert [record.sequence for record in ledger.records()] == list(range(24))
    assert registry.multiplicity_snapshot().count == 24


def test_concurrent_duplicate_attempt_has_one_winner_and_one_refusal(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    attempt_id = "1" * 32

    def contend(index: int) -> str:
        try:
            return _start(
                registry,
                f"contending-trial-{index}",
                attempt_id=attempt_id,
            ).attempt_id
        except CanonicalTrialError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contend, range(2)))

    assert sorted(outcomes) == [attempt_id, "refused"]
    assert registry.multiplicity_snapshot().count == 1
    assert RegistryLedger(registry.ledger_path).verify()[0] is True


def test_process_concurrent_starts_share_one_intact_chain(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_process_start, args=(str(registry.ledger_path), index))
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    ledger = RegistryLedger(registry.ledger_path)
    assert ledger.verify()[0] is True
    assert registry.multiplicity_snapshot().count == 4

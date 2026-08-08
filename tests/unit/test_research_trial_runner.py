"""Canonical start-before-read + retained-evidence integration tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from chronos.registry import (
    KIND_TRIAL_STARTED,
    KIND_TRIAL_TERMINAL,
    CanonicalTrialRegistry,
    RegistryLedger,
    RunStage,
)
from chronos.research.certified_data import (
    CATALOG_SCHEMA_VERSION,
    CertifiedDataRequest,
    CertifiedDatasetCatalog,
    DataContentDrift,
    HoldoutAccessRefused,
)
from chronos.research.replay_store import ReplayObjectStore
from chronos.research.trial_runner import (
    BrokeredResearchTrialRunner,
    BrokeredTrialDefinition,
    BrokeredTrialError,
    BrokeredTrialEvaluation,
    TrialArtifactOutput,
)

_DATA = b"date,open,high,low,close,volume\n2026-01-02,100,101,99,100.5,10\n"
_DATA_SHA = hashlib.sha256(_DATA).hexdigest()
_SOURCE_RECEIPT = "b" * 64


def _definition() -> BrokeredTrialDefinition:
    return BrokeredTrialDefinition(
        campaign_id="campaign-one",
        campaign_manifest_sha256="c" * 64,
        cell_id="cell-one",
        hypothesis_id="hypothesis-one",
        stage=RunStage.VALIDATION,
        strategy_id="five-tool-v3-6",
        config_digest="d" * 64,
        code_commit="1" * 40,
        criteria_digest="e" * 64,
        evaluator_id="five-tool-replay-v1",
        evaluator_digest="f" * 64,
    )


def _request(*, partition: str = "validation") -> CertifiedDataRequest:
    return CertifiedDataRequest(
        dataset_id="certified-daily-v1",
        partition=partition,
        data_version=_DATA_SHA,
        source_id="owner-capture-v1",
        source_receipt_sha256=_SOURCE_RECEIPT,
    )


def _catalog(
    tmp_path: Path,
    *,
    classification: str = "ordinary",
    partition: str = "validation",
) -> tuple[CertifiedDatasetCatalog, Path]:
    root = tmp_path / f"dataset-{classification}-{partition}"
    root.mkdir()
    data_path = root / "partition.csv"
    data_path.write_bytes(_DATA)
    document = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_id": "catalog-one",
        "entries": [
            {
                "dataset_id": "certified-daily-v1",
                "partition": partition,
                "data_version": _DATA_SHA,
                "source_id": "owner-capture-v1",
                "source_receipt_sha256": _SOURCE_RECEIPT,
                "classification": classification,
                "path": "partition.csv",
                "sha256": _DATA_SHA,
                "byte_count": len(_DATA),
            }
        ],
    }
    manifest_bytes = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / f"catalog-{classification}-{partition}.json"
    manifest_path.write_bytes(manifest_bytes)
    return (
        CertifiedDatasetCatalog.from_manifest(
            manifest_path,
            trusted_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            dataset_root=root,
        ),
        data_path,
    )


def _runner(
    tmp_path: Path,
    catalog: CertifiedDatasetCatalog,
) -> tuple[BrokeredResearchTrialRunner, CanonicalTrialRegistry, ReplayObjectStore]:
    registry = CanonicalTrialRegistry._for_tests(tmp_path / "registry.jsonl")
    store = ReplayObjectStore._for_tests(tmp_path / "replay")
    return (
        BrokeredResearchTrialRunner(
            registry=registry,
            catalog=catalog,
            replay_store=store,
        ),
        registry,
        store,
    )


def _evaluation(value: str = "ok") -> BrokeredTrialEvaluation:
    return BrokeredTrialEvaluation(
        outputs=(
            TrialArtifactOutput(
                role="five_tool_replay_summary",
                content=(
                    b'{"execution_parity":"UNVERIFIED","result":"' + value.encode("utf-8") + b'"}'
                ),
            ),
        ),
    )


def test_output_artifact_repr_does_not_expose_retained_bytes() -> None:
    artifact = TrialArtifactOutput(role="secret_evidence", content=b"private-result-bytes")

    assert "private-result-bytes" not in repr(artifact)


def test_runner_refuses_cross_workspace_public_capabilities_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_workspace = tmp_path / "registry-workspace"
    store_workspace = tmp_path / "store-workspace"
    registry_workspace.mkdir()
    store_workspace.mkdir()
    catalog, _ = _catalog(tmp_path)
    monkeypatch.chdir(registry_workspace)
    registry = CanonicalTrialRegistry()
    monkeypatch.chdir(store_workspace)
    store = ReplayObjectStore()

    with pytest.raises(BrokeredTrialError, match="different workspaces"):
        BrokeredResearchTrialRunner(
            registry=registry,
            catalog=catalog,
            replay_store=store,
        )

    assert not registry.ledger_path.exists()


def test_evaluation_has_no_free_floating_unretained_value() -> None:
    with pytest.raises(TypeError):
        BrokeredTrialEvaluation(  # type: ignore[call-arg]
            value="PROMOTE",
            outputs=(TrialArtifactOutput(role="decision", content=b'"REJECT"'),),
        )


def test_canonical_start_precedes_open_and_completed_evidence_is_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, store = _runner(tmp_path, catalog)
    original_read = CertifiedDatasetCatalog._read_bytes_for_trial

    def asserting_read(
        instance: CertifiedDatasetCatalog,
        request: CertifiedDataRequest,
    ) -> object:
        snapshot = registry.multiplicity_snapshot()
        assert snapshot.count == 1
        assert snapshot.record_count == 1
        return original_read(instance, request)

    monkeypatch.setattr(CertifiedDatasetCatalog, "_read_bytes_for_trial", asserting_read)
    result = runner.run(
        _definition(),
        _request(),
        evaluator=lambda _data, _receipt: _evaluation(),
    )

    assert registry.multiplicity_snapshot().count == 1
    records = RegistryLedger(registry.ledger_path).records()
    assert [record.kind for record in records] == [
        KIND_TRIAL_STARTED,
        KIND_TRIAL_TERMINAL,
    ]
    assert records[-1].payload["evidence_digest"] == result.replay_envelope.sha256
    envelope = store.load_envelope(result.replay_envelope)
    assert envelope.attempt_id == result.receipt.attempt_id
    assert envelope.start_record_hash == result.receipt.start_record_hash
    assert store.get_bytes(envelope.inputs[0].object_ref) == _DATA
    assert store.get_bytes(envelope.outputs[0].object_ref).startswith(b'{"execution_parity"')
    restarted_store = ReplayObjectStore._for_tests(store.root)
    recovered = restarted_store.load_envelope_by_sha256(str(records[-1].payload["evidence_digest"]))
    assert recovered == envelope


def test_restarted_runner_verifies_registry_catalog_and_retained_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, store = _runner(tmp_path, catalog)
    result = runner.run(
        _definition(),
        _request(),
        evaluator=lambda *_: _evaluation("retained"),
    )
    restarted_store = ReplayObjectStore._for_tests(store.root)
    restarted = BrokeredResearchTrialRunner(
        registry=CanonicalTrialRegistry._for_tests(registry.ledger_path),
        catalog=catalog,
        replay_store=restarted_store,
    )

    def poison_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("restart verification must not reopen dataset bytes")

    monkeypatch.setattr(CertifiedDatasetCatalog, "_read_bytes_for_trial", poison_read)
    envelope = restarted.load_completed_evidence(
        _definition(),
        _request(),
        attempt_id=result.receipt.attempt_id,
    )

    assert envelope.attempt_id == result.receipt.attempt_id
    assert envelope.outputs[0].role == "five_tool_replay_summary"
    assert restarted_store.get_bytes(envelope.outputs[0].object_ref).endswith(b'"retained"}')


def test_restart_verification_refuses_wrong_trial_identity_and_failed_attempt(
    tmp_path: Path,
) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, _ = _runner(tmp_path, catalog)
    result = runner.run(_definition(), _request(), evaluator=lambda *_: _evaluation())

    with pytest.raises(BrokeredTrialError, match="registry identity"):
        runner.load_completed_evidence(
            replace(_definition(), evaluator_digest="0" * 64),
            _request(),
            attempt_id=result.receipt.attempt_id,
        )

    def fail(*_args: object) -> BrokeredTrialEvaluation:
        raise _EvaluationFailure("expected failure")

    with pytest.raises(_EvaluationFailure):
        runner.run(_definition(), _request(), evaluator=fail)
    failed_attempt_id = str(
        RegistryLedger(registry.ledger_path).records()[-2].payload["attempt_id"]
    )
    with pytest.raises(BrokeredTrialError, match="no verified completed"):
        runner.load_completed_evidence(
            _definition(),
            _request(),
            attempt_id=failed_attempt_id,
        )


class _EvaluationFailure(RuntimeError):
    pass


def test_evaluator_failure_is_terminal_and_still_counts(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, _ = _runner(tmp_path, catalog)

    def fail(_data: bytes, _receipt: object) -> BrokeredTrialEvaluation:
        raise _EvaluationFailure("sensitive detail is never stored")

    with pytest.raises(_EvaluationFailure):
        runner.run(_definition(), _request(), evaluator=fail)

    assert registry.multiplicity_snapshot().count == 1
    terminal = RegistryLedger(registry.ledger_path).records()[-1]
    assert terminal.kind == KIND_TRIAL_TERMINAL
    assert terminal.payload["outcome"] == "failed"
    assert terminal.payload["error_type"] == "_EvaluationFailure"
    assert "sensitive" not in json.dumps(terminal.payload)


def test_content_drift_fails_after_start_without_calling_evaluator(tmp_path: Path) -> None:
    catalog, data_path = _catalog(tmp_path)
    runner, registry, _ = _runner(tmp_path, catalog)
    data_path.write_bytes(_DATA + b"tampered")
    called = False

    def poison(_data: bytes, _receipt: object) -> BrokeredTrialEvaluation:
        nonlocal called
        called = True
        return _evaluation()

    with pytest.raises(DataContentDrift):
        runner.run(_definition(), _request(), evaluator=poison)

    assert called is False
    assert registry.multiplicity_snapshot().count == 1
    assert RegistryLedger(registry.ledger_path).records()[-1].payload["outcome"] == "failed"


def test_holdout_is_refused_before_start_and_before_evaluator(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path, classification="holdout", partition="holdout")
    runner, registry, _ = _runner(tmp_path, catalog)
    called = False

    def poison(_data: bytes, _receipt: object) -> BrokeredTrialEvaluation:
        nonlocal called
        called = True
        return _evaluation()

    with pytest.raises(HoldoutAccessRefused):
        runner.run(_definition(), _request(partition="holdout"), evaluator=poison)

    assert called is False
    assert registry.multiplicity_snapshot().count == 0
    assert not registry.ledger_path.exists()


def test_ordinary_runner_refuses_holdout_stage_before_start(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, _ = _runner(tmp_path, catalog)

    with pytest.raises(BrokeredTrialError, match=r"cannot claim.*holdout"):
        runner.run(
            replace(_definition(), stage=RunStage.HOLDOUT),
            _request(),
            evaluator=lambda *_: _evaluation(),
        )

    assert not registry.ledger_path.exists()


def test_retries_share_semantic_trial_id_but_each_attempt_counts(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    runner, registry, _ = _runner(tmp_path, catalog)
    first = runner.run(_definition(), _request(), evaluator=lambda *_: _evaluation("first"))
    second = runner.run(_definition(), _request(), evaluator=lambda *_: _evaluation("second"))

    assert first.receipt.trial_id == second.receipt.trial_id
    assert first.receipt.attempt_id != second.receipt.attempt_id
    snapshot = registry.multiplicity_snapshot()
    assert snapshot.count == 2
    assert snapshot.record_count == 4

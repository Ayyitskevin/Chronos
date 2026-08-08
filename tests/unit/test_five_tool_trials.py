"""Five-Tool readiness and explicitly ledger-local trial-accounting tests."""

from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from chronos.registry.ledger import RegistryLedger
from chronos.research.five_tool.contract import input_contract_digest
from chronos.research.five_tool_trials import (
    EXECUTION_READY,
    INTERRUPTED_ATTEMPT_ERROR,
    KIND_CAMPAIGN_SEALED,
    KIND_TRIAL_STARTED,
    KIND_TRIAL_TERMINAL,
    TRIAL_SCHEMA_VERSION,
    CampaignExecutionBlocked,
    CampaignIdentityMismatch,
    CampaignSealed,
    DataAccessRequest,
    DataVersionMismatch,
    EvaluationEvidence,
    FiveToolTrialBroker,
    FiveToolTrialError,
    ReviewedVarianceEvidence,
    TrialDefinition,
    TrialEvaluation,
    TrialOutcome,
    TrialReceipt,
    _score_rows_digest,
    _validated_completed_rows,
    campaign_manifest_digest,
    deterministic_trial_id,
    ledger_trial_multiplicity,
    seal_ledger_local_score_inputs,
    validate_campaign_manifest,
)

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_CRITERIA_PATH = _ROOT / "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md"
_DATA = b"content-addressed-certified-five-tool-dataset-v1\n"
_DATA_SHA256 = hashlib.sha256(_DATA).hexdigest()
_CODE_COMMIT = "1" * 40
_VARIANCE = ReviewedVarianceEvidence(
    0.04,
    "reviewed-sample-variance-v1",
    "f" * 64,
)


def _checked_manifest() -> dict[str, Any]:
    loaded = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _synthetic_ready_manifest_for_tests(*, data: bytes = _DATA) -> dict[str, Any]:
    """Build synthetic identity only; this carries no public readiness authority."""

    manifest = _checked_manifest()
    manifest["execution_state"] = EXECUTION_READY
    manifest["blocked_before_first_data_read"] = []
    manifest["code_commit_lock"] = {
        "git_commit": _CODE_COMMIT,
        "status": "resolved",
        "required_before_execution": True,
    }
    manifest["criteria_lock"] = {
        "path": manifest["criteria_document"],
        "sha256": hashlib.sha256(_CRITERIA_PATH.read_bytes()).hexdigest(),
        "status": "resolved",
        "required_before_execution": True,
    }
    manifest["data"]["dataset_version_lock"] = {
        "dataset_id": "five-tool-certified-daily-v1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "resolved",
        "required_before_execution": True,
    }
    return manifest


def _cell(manifest: dict[str, Any], cell_id: str) -> dict[str, Any]:
    matches = [cell for cell in manifest["campaign_cells"] if cell["cell_id"] == cell_id]
    assert len(matches) == 1
    return cast(dict[str, Any], matches[0])


def _definition(
    manifest: dict[str, Any],
    *,
    cell_id: str = "5t-full-default-reference",
    config: dict[str, object] | None = None,
) -> TrialDefinition:
    cell = _cell(manifest, cell_id)
    return TrialDefinition(
        campaign_id=manifest["campaign_id"],
        cell_id=cell_id,
        hypothesis_id=cell["hypothesis_id"],
        strategy_id=manifest["strategy"]["strategy_id"],
        semantic_config=copy.deepcopy(cell["config_overlay"]) if config is None else config,
        code_commit=manifest["code_commit_lock"]["git_commit"],
        criteria_digest=manifest["criteria_lock"]["sha256"],
        input_contract_digest=manifest["strategy"]["input_contract"]["sha256"],
    )


def _request(
    manifest: dict[str, Any],
    *,
    dataset_id: str | None = None,
    partition: str = "validation",
    data_version: str | None = None,
) -> DataAccessRequest:
    lock = manifest["data"]["dataset_version_lock"]
    return DataAccessRequest(
        dataset_id=dataset_id or lock["dataset_id"],
        partition=partition,
        data_version=data_version or lock["sha256"],
    )


def _evidence(index: int = 0) -> EvaluationEvidence:
    return EvaluationEvidence(
        artifact_bytes=json.dumps(
            {"cell_index": index, "metric": "raw-score-evidence"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        observed_sharpe=0.10 + index / 100.0,
        observations=500 + index,
        skew=0.01 * index,
        kurtosis=3.0 + index / 100.0,
    )


def _evaluation[T](value: T, index: int = 0) -> TrialEvaluation[T]:
    return TrialEvaluation(value=value, evidence=_evidence(index))


def _ready_broker(path: Path, manifest: dict[str, Any] | None = None) -> FiveToolTrialBroker:
    return FiveToolTrialBroker._from_synthetic_manifest_for_tests(
        path,
        manifest or _synthetic_ready_manifest_for_tests(),
    )


def _run_cell(
    broker: FiveToolTrialBroker,
    manifest: dict[str, Any],
    cell_id: str,
    *,
    evidence_index: int,
) -> TrialReceipt:
    return broker.run(
        _definition(manifest, cell_id=cell_id),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=lambda _data, receipt: _evaluation(receipt, evidence_index),
    )


def _process_attempt(ledger_path: str, index: int) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    broker = _ready_broker(Path(ledger_path), manifest)
    _run_cell(
        broker,
        manifest,
        "5t-full-default-reference",
        evidence_index=index,
    )


def test_data_version_must_be_a_content_digest() -> None:
    with pytest.raises(ValueError, match="data_version"):
        DataAccessRequest(
            dataset_id="certified-daily-v1",
            partition="validation",
            data_version="latest",
        )


def test_direct_construction_has_no_read_authority(tmp_path: Path) -> None:
    path = tmp_path / "direct.jsonl"
    manifest = _synthetic_ready_manifest_for_tests()
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CampaignExecutionBlocked, match=r"direct .* no certified"):
        FiveToolTrialBroker(path).run(
            _definition(manifest),
            _request(manifest),
            reader=poison,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    assert opened is False
    assert not path.exists()


def test_checked_manifest_is_valid_but_blocks_before_reader_and_ledger(tmp_path: Path) -> None:
    manifest = _checked_manifest()
    validate_campaign_manifest(manifest)
    path = tmp_path / "blocked.jsonl"
    broker = FiveToolTrialBroker.from_campaign_manifest(path, manifest)
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CampaignExecutionBlocked, match="blocked_until_identity"):
        broker.run(
            TrialDefinition(
                campaign_id=manifest["campaign_id"],
                cell_id="5t-full-default-reference",
                hypothesis_id="H-5T-001-TREND",
                strategy_id="five_tool_confluence_v3_6",
                semantic_config={},
                code_commit=_CODE_COMMIT,
                criteria_digest="a" * 64,
                input_contract_digest=input_contract_digest(),
            ),
            DataAccessRequest("anything", "development", "b" * 64),
            reader=poison,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    assert opened is False
    assert not path.exists()


def test_public_ready_manifest_cannot_authorize_reader_or_ledger(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "public-ready.jsonl"

    with pytest.raises(CampaignExecutionBlocked, match="EXECUTION_READY is not implemented"):
        validate_campaign_manifest(manifest)
    with pytest.raises(CampaignExecutionBlocked, match="EXECUTION_READY is not implemented"):
        campaign_manifest_digest(manifest)
    with pytest.raises(CampaignExecutionBlocked, match="EXECUTION_READY is not implemented"):
        FiveToolTrialBroker.from_campaign_manifest(path, manifest)

    assert not path.exists()


def test_synthetic_harness_starts_durably_before_reader(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "ready.jsonl"
    broker = _ready_broker(path, manifest)
    expected_manifest_digest = broker._require_binding().manifest_sha256

    def reader(request: DataAccessRequest) -> bytes:
        assert request.data_version == _DATA_SHA256
        ledger = RegistryLedger(path)
        assert ledger.verify()[0] is True
        records = ledger.records()
        assert [record.kind for record in records] == [KIND_TRIAL_STARTED]
        assert records[0].payload["campaign_manifest_sha256"] == expected_manifest_digest
        return _DATA

    receipt = broker.run(
        _definition(manifest),
        _request(manifest),
        reader=reader,
        evaluator=lambda _data, started: _evaluation(started),
    )
    assert isinstance(receipt, TrialReceipt)
    assert [record.kind for record in RegistryLedger(path).records()] == [
        KIND_TRIAL_STARTED,
        KIND_TRIAL_TERMINAL,
    ]


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("campaign_id", "other", "campaign_id"),
        ("strategy_id", "other", "strategy_id"),
        ("code_commit", "2" * 40, "code_commit"),
        ("criteria_digest", "2" * 64, "criteria_digest"),
        ("input_contract_digest", "2" * 64, "input_contract_digest"),
        ("cell_id", "unknown-cell", "cell_id"),
    ],
)
def test_definition_identity_drift_refuses_before_reader(
    tmp_path: Path, field: str, replacement: str, message: str
) -> None:
    from dataclasses import replace

    manifest = _synthetic_ready_manifest_for_tests()
    changes = cast(Any, {field: replacement})
    definition = replace(_definition(manifest), **changes)
    path = tmp_path / f"drift-{field}.jsonl"
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CampaignIdentityMismatch, match=message):
        _ready_broker(path, manifest).run(
            definition,
            _request(manifest),
            reader=poison,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    assert opened is False
    assert not path.exists()


def test_data_identity_drift_refuses_before_reader(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "data-drift.jsonl"
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CampaignIdentityMismatch, match="dataset_id"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest, dataset_id="other"),
            reader=poison,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    assert opened is False
    assert not path.exists()


def test_reader_failure_is_terminal_and_counts_in_local_ledger(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "reader-failure.jsonl"

    def failing_reader(_request: DataAccessRequest) -> bytes:
        raise OSError("fixture read failed")

    with pytest.raises(OSError, match="fixture read failed"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest),
            reader=failing_reader,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["data_sha256"] is None
    assert ledger_trial_multiplicity(path) == 1


def test_unusual_callback_error_name_still_gets_bounded_terminal(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "unusual-error-name.jsonl"
    broker = _ready_broker(path, manifest)
    unusual_error = type("not a bounded error name", (RuntimeError,), {})("callback failed")

    def failing_reader(_request: DataAccessRequest) -> bytes:
        raise unusual_error

    with pytest.raises(RuntimeError, match="callback failed"):
        broker.run(
            _definition(manifest),
            _request(manifest),
            reader=failing_reader,
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )

    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED.value
    assert terminal.payload["error_type"] == "UnclassifiedCallbackError"


def test_reader_bytes_must_match_authorized_data_version(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "data-mismatch.jsonl"
    evaluated = False

    def evaluator(_data: bytes, receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        nonlocal evaluated
        evaluated = True
        return _evaluation(receipt)

    wrong = b"different bytes"
    with pytest.raises(DataVersionMismatch, match="do not match"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest),
            reader=lambda _: wrong,
            evaluator=evaluator,
        )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["data_sha256"] == hashlib.sha256(wrong).hexdigest()
    assert terminal.payload["evidence_digest"] is None
    assert evaluated is False
    assert ledger_trial_multiplicity(path) == 1


def test_evaluator_failure_is_terminal_and_counts(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "evaluator-failure.jsonl"

    def failing_evaluator(_data: bytes, _receipt: TrialReceipt) -> TrialEvaluation[None]:
        raise ArithmeticError("statistics failed")

    with pytest.raises(ArithmeticError, match="statistics failed"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=failing_evaluator,
        )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["data_sha256"] == _DATA_SHA256
    assert terminal.payload["error_type"] == "ArithmeticError"
    assert ledger_trial_multiplicity(path) == 1


def test_completed_terminal_binds_computed_typed_evidence(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "typed-evidence.jsonl"
    evidence = _evidence(3)
    _ready_broker(path, manifest).run(
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=lambda _data, receipt: TrialEvaluation(receipt, evidence),
    )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["evidence_artifact_sha256"] == evidence.artifact_sha256
    assert terminal.payload["evidence_digest"] == evidence.evidence_digest
    assert terminal.payload["observed_sharpe"] == evidence.observed_sharpe
    assert terminal.payload["observations"] == evidence.observations


def test_malformed_evaluation_cannot_create_completed_terminal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "malformed-evidence.jsonl"

    def malformed(_data: bytes, _receipt: TrialReceipt) -> TrialEvaluation[None]:
        evaluation = object.__new__(TrialEvaluation)
        object.__setattr__(evaluation, "value", None)
        object.__setattr__(evaluation, "evidence", None)
        return cast(TrialEvaluation[None], evaluation)

    with pytest.raises(TypeError, match="evidence must be EvaluationEvidence"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=malformed,
        )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["evidence_digest"] is None
    assert ledger_trial_multiplicity(path) == 1


def test_post_init_bypassed_evidence_cannot_create_completed_terminal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "spoofed-evidence.jsonl"
    evidence = object.__new__(EvaluationEvidence)
    object.__setattr__(evidence, "artifact_bytes", b"spoofed")
    object.__setattr__(evidence, "observed_sharpe", "not-a-number")
    object.__setattr__(evidence, "observations", 20)
    object.__setattr__(evidence, "skew", 0.0)
    object.__setattr__(evidence, "kurtosis", 3.0)
    evaluation = object.__new__(TrialEvaluation)
    object.__setattr__(evaluation, "value", None)
    object.__setattr__(evaluation, "evidence", evidence)

    with pytest.raises(ValueError, match="observed_sharpe"):
        _ready_broker(path, manifest).run(
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=lambda _data, _receipt: cast(TrialEvaluation[None], evaluation),
        )
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["evidence_digest"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observations", 3.0),
        ("observed_sharpe", "0.5"),
        ("skew", True),
    ],
)
def test_evaluation_evidence_rejects_runtime_type_spoofing(field: str, value: object) -> None:
    values: dict[str, object] = {
        "artifact_bytes": b"typed-evidence",
        "observed_sharpe": 0.5,
        "observations": 20,
        "skew": 0.0,
        "kurtosis": 3.0,
    }
    values[field] = value
    with pytest.raises(ValueError):
        EvaluationEvidence(**cast(Any, values))


def test_trial_identity_is_deterministic_but_attempts_are_unique(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "attempts.jsonl"
    broker = _ready_broker(path, manifest)
    definition = _definition(manifest)
    request = _request(manifest)
    first = broker.run(
        definition,
        request,
        reader=lambda _: _DATA,
        evaluator=lambda _data, receipt: _evaluation(receipt, 1),
    )
    second = broker.run(
        definition,
        request,
        reader=lambda _: _DATA,
        evaluator=lambda _data, receipt: _evaluation(receipt, 2),
    )
    assert deterministic_trial_id(definition, request) == first.trial_id == second.trial_id
    assert first.attempt_id != second.attempt_id
    assert ledger_trial_multiplicity(path) == 2


def test_semantic_fingerprint_is_frozen_against_caller_mutation() -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    config: dict[str, object] = {"threshold": 0.85}
    definition = _definition(manifest, config=config)
    before = definition.semantic_config_fingerprint
    config["threshold"] = 0.1
    assert definition.semantic_config_fingerprint == before


def test_concurrent_starts_keep_chain_and_anchor_consistent(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "threads.jsonl"

    def attempt(index: int) -> str:
        return _run_cell(
            _ready_broker(path, manifest),
            manifest,
            "5t-full-default-reference",
            evidence_index=index,
        ).attempt_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        attempt_ids = tuple(executor.map(attempt, range(24)))
    assert len(set(attempt_ids)) == 24
    assert ledger_trial_multiplicity(path) == 24
    ledger = RegistryLedger(path)
    assert len(ledger.records()) == 48
    assert ledger.verify()[0] is True


def test_concurrent_processes_keep_chain_and_anchor_consistent(tmp_path: Path) -> None:
    path = tmp_path / "processes.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_process_attempt, args=(str(path), index)) for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert ledger_trial_multiplicity(path) == 8
    ledger = RegistryLedger(path)
    assert len(ledger.records()) == 16
    assert ledger.verify()[0] is True


def test_ledger_trial_count_is_explicitly_path_local(tmp_path: Path) -> None:
    first_manifest = _synthetic_ready_manifest_for_tests()
    second_manifest = copy.deepcopy(first_manifest)
    second_manifest["campaign_id"] = "five-tool-v3.6-preregistered-002"
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"

    _run_cell(
        _ready_broker(first_path, first_manifest),
        first_manifest,
        "5t-full-default-reference",
        evidence_index=0,
    )
    _run_cell(
        _ready_broker(second_path, second_manifest),
        second_manifest,
        "5t-full-default-reference",
        evidence_index=0,
    )

    assert ledger_trial_multiplicity(first_path) == 1
    assert ledger_trial_multiplicity(second_path) == 1
    # There is deliberately no public Five-Tool cross-path aggregator. Canonical
    # ADR-0013 registry integration remains a Phase-3 blocker rather than being simulated.


def _complete_campaign(
    path: Path,
    manifest: dict[str, Any],
    *,
    reverse: bool = False,
    failed_first: bool = False,
) -> tuple[FiveToolTrialBroker, tuple[Any, ...]]:
    broker = _ready_broker(path, manifest)
    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    index_by_cell = {cell_id: index for index, cell_id in enumerate(cells)}
    if failed_first:
        with pytest.raises(ArithmeticError):
            broker.run(
                _definition(manifest, cell_id=cells[0]),
                _request(manifest),
                reader=lambda _: _DATA,
                evaluator=lambda _data, _receipt: (_ for _ in ()).throw(
                    ArithmeticError("preregistered attempt failed")
                ),
            )
    for cell_id in reversed(cells) if reverse else cells:
        _run_cell(
            broker,
            manifest,
            cell_id,
            evidence_index=index_by_cell[cell_id],
        )
    return broker, seal_ledger_local_score_inputs(broker, reviewed_variance=_VARIANCE)


def _economic_score_view(rows: tuple[Any, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.cell_id,
            row.semantic_config_fingerprint,
            row.evidence_digest,
            row.observed_sharpe,
            row.observations,
            row.skew,
            row.kurtosis,
            row.ledger_trial_count,
            row.reviewed_cross_trial_variance,
            row.variance_estimator,
            row.variance_evidence_digest,
        )
        for row in rows
    )


def _failed_terminal_payload(receipt: TrialReceipt) -> dict[str, object]:
    return {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "campaign_id": receipt.campaign_id,
        "campaign_manifest_sha256": receipt.campaign_manifest_sha256,
        "trial_id": receipt.trial_id,
        "cell_id": receipt.cell_id,
        "hypothesis_id": receipt.hypothesis_id,
        "semantic_config_fingerprint": receipt.semantic_config_fingerprint,
        "attempt_id": receipt.attempt_id,
        "start_sequence": receipt.start_sequence,
        "start_record_hash": receipt.start_record_hash,
        "outcome": TrialOutcome.FAILED.value,
        "dataset_id": receipt.dataset_id,
        "partition": receipt.partition,
        "data_version": receipt.data_version,
        "data_sha256": None,
        "evidence_artifact_sha256": None,
        "evidence_digest": None,
        "observed_sharpe": None,
        "observations": None,
        "skew": None,
        "kurtosis": None,
        "error_type": INTERRUPTED_ATTEMPT_ERROR,
    }


def test_ledger_local_count_is_order_invariant_and_seal_is_idempotent(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    first_broker, first = _complete_campaign(tmp_path / "forward.jsonl", manifest)
    _, reverse = _complete_campaign(tmp_path / "reverse.jsonl", manifest, reverse=True)
    assert _economic_score_view(first) == _economic_score_view(reverse)
    assert len(first) == len(manifest["campaign_cells"])
    assert all(row.ledger_trial_count == len(first) for row in first)
    assert first_broker.seal_ledger_local_score_inputs(_VARIANCE) == first
    records = RegistryLedger(first_broker.ledger_path).records()
    assert records[-1].kind == KIND_CAMPAIGN_SEALED
    assert sum(record.kind == KIND_CAMPAIGN_SEALED for record in records) == 1


def test_interrupted_start_recovery_records_bounded_failure_and_allows_seal(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "interrupted.jsonl"
    broker = _ready_broker(path, manifest)
    interrupted = broker._start_for_interruption_test(
        _definition(manifest),
        _request(manifest),
    )

    assert broker._recover_interrupted_starts_for_tests() == (interrupted.attempt_id,)
    assert broker._recover_interrupted_starts_for_tests() == ()
    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED.value
    assert terminal.payload["error_type"] == INTERRUPTED_ATTEMPT_ERROR
    assert terminal.payload["data_sha256"] is None

    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    for index, cell_id in enumerate(cells):
        _run_cell(broker, manifest, cell_id, evidence_index=index)
    rows = broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert all(row.ledger_trial_count == len(cells) + 1 for row in rows)


def test_identical_completed_retry_is_canonical_and_still_counts(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "identical-retry.jsonl"
    broker = _ready_broker(path, manifest)
    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    first_receipts: dict[str, TrialReceipt] = {}
    for index, cell_id in enumerate(cells):
        first_receipts[cell_id] = _run_cell(
            broker,
            manifest,
            cell_id,
            evidence_index=index,
        )
    repeated_cell = cells[0]
    retry = _run_cell(broker, manifest, repeated_cell, evidence_index=0)

    rows = broker.seal_ledger_local_score_inputs(_VARIANCE)

    assert all(row.ledger_trial_count == len(cells) + 1 for row in rows)
    assert len(rows) == len(cells)
    canonical = next(row for row in rows if row.cell_id == repeated_cell)
    assert canonical.attempt_id == first_receipts[repeated_cell].attempt_id
    assert canonical.attempt_id != retry.attempt_id


def test_divergent_completed_retry_blocks_seal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "divergent-retry.jsonl"
    broker = _ready_broker(path, manifest)
    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    for index, cell_id in enumerate(cells):
        _run_cell(broker, manifest, cell_id, evidence_index=index)
    _run_cell(broker, manifest, cells[0], evidence_index=99)

    with pytest.raises(FiveToolTrialError, match="divergent completed results"):
        broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert not RegistryLedger(path).records_of(KIND_CAMPAIGN_SEALED)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unknown_key", "keys do not match schema"),
        ("inconsistent_trial_id", "trial_id is inconsistent"),
    ],
)
def test_malformed_start_semantics_block_finalization(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / f"malformed-start-{case}.jsonl"
    broker = _ready_broker(path, manifest)
    broker._start_for_interruption_test(
        _definition(manifest),
        _request(manifest),
    )
    start = RegistryLedger(path).records_of(KIND_TRIAL_STARTED)[0]
    payload = dict(start.payload)
    payload["attempt_id"] = "f" * 32
    if case == "unknown_key":
        payload["unexpected"] = True
    else:
        payload["trial_id"] = "5t-" + "0" * 64
    RegistryLedger(path).append(KIND_TRIAL_STARTED, payload)

    with pytest.raises(FiveToolTrialError, match=message):
        broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert not RegistryLedger(path).records_of(KIND_CAMPAIGN_SEALED)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unknown_outcome", "outcome is unsupported"),
        ("wrong_start_sequence", "start_sequence"),
        ("boolean_start_sequence", "start_sequence"),
        ("failed_with_evidence", "failed terminal cannot carry evidence_digest"),
        ("failed_without_error", "failed terminal requires a bounded error type"),
        ("completed_with_error", "completed terminal cannot carry an error type"),
        ("unknown_key", "keys do not match schema"),
    ],
)
def test_malformed_terminal_semantics_block_seal(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / f"malformed-terminal-{case}.jsonl"
    broker = _ready_broker(path, manifest)
    receipt = broker._start_for_interruption_test(
        _definition(manifest),
        _request(manifest),
    )
    payload = _failed_terminal_payload(receipt)
    if case == "unknown_outcome":
        payload["outcome"] = "not-a-terminal-outcome"
    elif case == "wrong_start_sequence":
        payload["start_sequence"] = receipt.start_sequence + 1
    elif case == "boolean_start_sequence":
        assert receipt.start_sequence == 0
        payload["start_sequence"] = False
    elif case == "failed_with_evidence":
        payload["evidence_digest"] = "0" * 64
    elif case == "failed_without_error":
        payload["error_type"] = None
    elif case == "completed_with_error":
        evidence = _evidence()
        payload.update(
            {
                "outcome": TrialOutcome.COMPLETED.value,
                "data_sha256": receipt.data_version,
                "evidence_artifact_sha256": evidence.artifact_sha256,
                "evidence_digest": evidence.evidence_digest,
                "observed_sharpe": evidence.observed_sharpe,
                "observations": evidence.observations,
                "skew": evidence.skew,
                "kurtosis": evidence.kurtosis,
            }
        )
    else:
        payload["unexpected"] = True
    RegistryLedger(path).append(KIND_TRIAL_TERMINAL, payload)

    with pytest.raises(FiveToolTrialError, match=message):
        broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert not RegistryLedger(path).records_of(KIND_CAMPAIGN_SEALED)


def test_failed_attempts_remain_in_ledger_local_count_at_seal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    broker, rows = _complete_campaign(
        tmp_path / "failed-counts.jsonl",
        manifest,
        failed_first=True,
    )
    assert all(row.ledger_trial_count == len(manifest["campaign_cells"]) + 1 for row in rows)
    assert ledger_trial_multiplicity(broker.ledger_path) == len(manifest["campaign_cells"]) + 1


def test_data_mismatch_failure_remains_in_local_count_without_blocking_seal(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "data-failure-counts.jsonl"
    broker = _ready_broker(path, manifest)
    with pytest.raises(DataVersionMismatch):
        broker.run(
            _definition(manifest),
            _request(manifest),
            reader=lambda _: b"wrong dataset bytes",
            evaluator=lambda _data, receipt: _evaluation(receipt),
        )
    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    for index, cell_id in enumerate(cells):
        _run_cell(broker, manifest, cell_id, evidence_index=index)
    rows = broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert all(row.ledger_trial_count == len(cells) + 1 for row in rows)


def test_incomplete_campaign_cannot_seal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / "incomplete.jsonl"
    broker = _ready_broker(path, manifest)
    _run_cell(broker, manifest, "5t-full-default-reference", evidence_index=0)
    with pytest.raises(FiveToolTrialError, match="campaign is incomplete"):
        broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert not RegistryLedger(path).records_of(KIND_CAMPAIGN_SEALED)


def test_seal_rejects_every_later_attempt_without_advancing_ledger(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    broker, _ = _complete_campaign(tmp_path / "sealed.jsonl", manifest)
    before = RegistryLedger(broker.ledger_path).records()
    with pytest.raises(CampaignSealed, match="later attempts"):
        _run_cell(
            _ready_broker(broker.ledger_path, manifest),
            manifest,
            "5t-full-default-reference",
            evidence_index=99,
        )
    after = RegistryLedger(broker.ledger_path).records()
    assert after == before


def test_existing_seal_rejects_different_variance_identity(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    broker, _ = _complete_campaign(tmp_path / "variance.jsonl", manifest)
    changed = ReviewedVarianceEvidence(0.05, _VARIANCE.estimator, _VARIANCE.evidence_digest)
    with pytest.raises(FiveToolTrialError, match="reviewed_cross_trial_variance"):
        broker.seal_ledger_local_score_inputs(changed)


def test_reviewed_variance_rejects_boolean_value() -> None:
    with pytest.raises(ValueError, match="reviewed variance must be finite"):
        ReviewedVarianceEvidence(True, _VARIANCE.estimator, _VARIANCE.evidence_digest)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("ledger_trial_count", "ledger_trial_count must be an integer"),
        ("record_count_before_seal", "record_count_before_seal must be an integer"),
        ("reviewed_cross_trial_variance", "reviewed_cross_trial_variance must be finite"),
    ],
)
def test_hash_valid_existing_seal_rejects_boolean_scalars(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    manifest = _synthetic_ready_manifest_for_tests()
    path = tmp_path / f"boolean-seal-{field}.jsonl"
    broker = _ready_broker(path, manifest)
    for index, cell_id in enumerate(sorted(cell["cell_id"] for cell in manifest["campaign_cells"])):
        _run_cell(broker, manifest, cell_id, evidence_index=index)

    ledger = RegistryLedger(path)
    records = ledger.records()
    binding = broker._require_binding()
    rows = _validated_completed_rows(records, binding)
    variance = ReviewedVarianceEvidence(
        1.0,
        _VARIANCE.estimator,
        _VARIANCE.evidence_digest,
    )
    payload: dict[str, object] = {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "campaign_id": binding.campaign_id,
        "campaign_manifest_sha256": binding.manifest_sha256,
        "ledger_trial_count": len(rows),
        "record_count_before_seal": len(records),
        "head_before_seal": records[-1].record_hash,
        "score_inputs_digest": _score_rows_digest(rows),
        "reviewed_cross_trial_variance": variance.value,
        "variance_estimator": variance.estimator,
        "variance_evidence_digest": variance.evidence_digest,
    }
    payload[field] = True
    ledger.append(KIND_CAMPAIGN_SEALED, payload)
    assert ledger.verify()[0] is True

    with pytest.raises(FiveToolTrialError, match=message):
        broker.seal_ledger_local_score_inputs(variance)


def test_manifest_digest_binds_every_valid_policy_change() -> None:
    original = _checked_manifest()
    changed = copy.deepcopy(original)
    changed["costs"]["commission_bps_per_fill"] = 4.0
    validate_campaign_manifest(changed)
    assert campaign_manifest_digest(changed) != campaign_manifest_digest(original)


def test_manifest_rejects_material_identity_and_policy_mutations() -> None:
    base = _checked_manifest()
    mutations: list[tuple[str, Any]] = [
        ("promotion", lambda item: item.__setitem__("promotion_authority", "live")),
        ("scope", lambda item: item["strategy"].__setitem__("scope", "live")),
        ("strategy", lambda item: item["strategy"].__setitem__("strategy_id", "other")),
        (
            "nonfinite cost",
            lambda item: item["costs"].__setitem__("commission_bps_per_fill", float("nan")),
        ),
        ("empty fill", lambda item: item.__setitem__("fill_policy", {})),
        ("empty statistics", lambda item: item.__setitem__("statistics", {})),
        ("empty accounting", lambda item: item.__setitem__("trial_accounting", {})),
        (
            "duplicate cells",
            lambda item: item["campaign_cells"][1].__setitem__(
                "cell_id", item["campaign_cells"][0]["cell_id"]
            ),
        ),
        (
            "duplicate instruments",
            lambda item: item["data"].__setitem__("primary_instruments", ["SPY", "SPY", "SPY"]),
        ),
        ("wrong benchmark", lambda item: item["data"].__setitem__("benchmark", "DIA")),
        (
            "holdout accessible",
            lambda item: item["data"].__setitem__("accessible_partitions", ["holdout"]),
        ),
        ("unknown root", lambda item: item.__setitem__("unfrozen", True)),
        (
            "wrong criteria",
            lambda item: item["criteria_lock"].__setitem__("sha256", "0" * 64),
        ),
    ]
    for _name, mutate in mutations:
        changed = copy.deepcopy(base)
        mutate(changed)
        with pytest.raises(ValueError):
            validate_campaign_manifest(changed)

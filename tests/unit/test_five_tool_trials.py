"""Five-Tool trial lifecycle, holdout seal, and final-N invariance tests."""

from __future__ import annotations

import copy
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chronos.registry.ledger import RegistryLedger
from chronos.research.five_tool_trials import (
    KIND_TRIAL_STARTED,
    KIND_TRIAL_TERMINAL,
    DataAccessRequest,
    FiveToolTrialBroker,
    HoldoutAccessRefused,
    RawScoreEvidence,
    TrialDefinition,
    TrialOutcome,
    TrialReceipt,
    deterministic_trial_id,
    finalize_score_inputs,
    global_trial_multiplicity,
    validate_campaign_manifest,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64


def _definition(config: dict[str, object] | None = None) -> TrialDefinition:
    return TrialDefinition(
        campaign_id="five-tool-v3.6-preregistered-001",
        hypothesis_id="H-5T-001",
        strategy_id="five_tool_confluence_v3_6",
        semantic_config=config or {"regime": {"enabled": True}, "threshold": 0.85},
        code_commit="47a8d72",
        criteria_digest=_DIGEST_A,
        input_contract_digest=_DIGEST_B,
    )


def _request(
    *, dataset_id: str = "certified-daily-v1", partition: str = "validation"
) -> DataAccessRequest:
    return DataAccessRequest(
        dataset_id=dataset_id,
        partition=partition,
        data_version=_DIGEST_C,
    )


def _process_attempt(ledger_path: str, index: int) -> None:
    broker = FiveToolTrialBroker(Path(ledger_path))
    broker.run(
        _definition(),
        _request(),
        reader=lambda _: f"process-bars-{index}".encode(),
        evaluator=lambda _data, receipt: receipt,
    )


def test_trial_started_is_durable_and_verified_before_reader_runs(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    broker = FiveToolTrialBroker(path)

    def reader(request: DataAccessRequest) -> bytes:
        assert request.partition == "validation"
        ledger = RegistryLedger(path)
        assert ledger.verify()[0] is True
        records = ledger.records()
        assert [record.kind for record in records] == [KIND_TRIAL_STARTED]
        assert records[0].payload["touched_data"] is True
        return b"closed-bar-data"

    receipt = broker.run(
        _definition(),
        _request(),
        reader=reader,
        evaluator=lambda data, started: started,
    )
    assert isinstance(receipt, TrialReceipt)
    records = RegistryLedger(path).records()
    assert [record.kind for record in records] == [KIND_TRIAL_STARTED, KIND_TRIAL_TERMINAL]
    assert records[-1].payload["outcome"] == TrialOutcome.COMPLETED
    assert global_trial_multiplicity(path) == 1


def test_reader_failure_is_terminal_and_counts_toward_multiplicity(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    broker = FiveToolTrialBroker(path)

    def failing_reader(_request: DataAccessRequest) -> bytes:
        raise OSError("fixture read failed")

    with pytest.raises(OSError, match="fixture read failed"):
        broker.run(
            _definition(),
            _request(),
            reader=failing_reader,
            evaluator=lambda data, receipt: (data, receipt),
        )

    records = RegistryLedger(path).records()
    assert len(records) == 2
    assert records[-1].payload == {
        **records[-1].payload,
        "outcome": TrialOutcome.FAILED,
        "error_type": "OSError",
        "data_sha256": None,
    }
    assert global_trial_multiplicity(path) == 1


def test_evaluator_failure_records_terminal_failure_with_data_digest(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    broker = FiveToolTrialBroker(path)

    def failing_evaluator(_data: bytes, _receipt: TrialReceipt) -> None:
        raise ArithmeticError("statistics failed")

    with pytest.raises(ArithmeticError, match="statistics failed"):
        broker.run(
            _definition(),
            _request(),
            reader=lambda request: request.dataset_id.encode(),
            evaluator=failing_evaluator,
        )

    terminal = RegistryLedger(path).records_of(KIND_TRIAL_TERMINAL)[0]
    assert terminal.payload["outcome"] == TrialOutcome.FAILED
    assert terminal.payload["error_type"] == "ArithmeticError"
    assert isinstance(terminal.payload["data_sha256"], str)
    assert len(str(terminal.payload["data_sha256"])) == 64
    assert global_trial_multiplicity(path) == 1


@pytest.mark.parametrize(
    ("dataset_id", "partition"),
    [
        ("future-owner-holdout-v1", "validation"),
        ("certified-daily-v1", "HOLDOUT"),
        ("certified-daily-v1", "reserved-final"),
    ],
)
def test_declared_holdout_poison_reader_is_never_opened(
    tmp_path: Path,
    dataset_id: str,
    partition: str,
) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        raise AssertionError("holdout reader was called")

    broker = FiveToolTrialBroker(
        path,
        declared_holdout_datasets={"future-owner-holdout-v1"},
    )
    with pytest.raises(HoldoutAccessRefused, match="declared holdout"):
        broker.run(
            _definition(),
            _request(dataset_id=dataset_id, partition=partition),
            reader=poison,
            evaluator=lambda data, receipt: (data, receipt),
        )
    assert opened is False
    assert not path.exists()


def test_trial_identity_is_deterministic_but_attempts_are_unique(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    broker = FiveToolTrialBroker(path)
    definition_a = _definition({"alpha": 1, "nested": {"a": True, "b": [1, 2]}})
    definition_b = _definition({"nested": {"b": [1, 2], "a": True}, "alpha": 1})
    request = _request()

    first = broker.run(
        definition_a,
        request,
        reader=lambda _: b"same",
        evaluator=lambda _data, receipt: receipt,
    )
    second = broker.run(
        definition_b,
        request,
        reader=lambda _: b"same",
        evaluator=lambda _data, receipt: receipt,
    )

    assert definition_a.semantic_config_fingerprint == definition_b.semantic_config_fingerprint
    assert deterministic_trial_id(definition_a, request) == deterministic_trial_id(
        definition_b, request
    )
    assert first.trial_id == second.trial_id
    assert first.attempt_id != second.attempt_id
    assert global_trial_multiplicity(path) == 2


def test_semantic_fingerprint_is_frozen_against_caller_mutation() -> None:
    config: dict[str, object] = {"threshold": 0.85}
    definition = _definition(config)
    before = definition.semantic_config_fingerprint
    config["threshold"] = 0.1
    assert definition.semantic_config_fingerprint == before


def test_concurrent_starts_keep_chain_and_head_consistent(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    definition = _definition()
    request = _request()

    def attempt(index: int) -> str:
        # Separate broker objects exercise stale-head avoidance, not just one object's state.
        broker = FiveToolTrialBroker(path)
        receipt = broker.run(
            definition,
            request,
            reader=lambda _: f"bars-{index}".encode(),
            evaluator=lambda _data, started: started,
        )
        return receipt.attempt_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        attempt_ids = tuple(executor.map(attempt, range(24)))

    assert len(set(attempt_ids)) == 24
    assert global_trial_multiplicity(path) == 24
    ledger = RegistryLedger(path)
    assert len(ledger.records()) == 48
    assert ledger.verify()[0] is True


def test_concurrent_processes_keep_chain_and_head_consistent(tmp_path: Path) -> None:
    path = tmp_path / "five_tool_trials.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_process_attempt, args=(str(path), index)) for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert global_trial_multiplicity(path) == 8
    ledger = RegistryLedger(path)
    assert len(ledger.records()) == 16
    assert ledger.verify()[0] is True


def _raw(fingerprint: str, label: str, sharpe: float) -> RawScoreEvidence:
    return RawScoreEvidence(
        semantic_config_fingerprint=fingerprint,
        evidence_digest=("d" if fingerprint == _DIGEST_A else "e") * 64,
        observed_sharpe=sharpe,
        observations=500,
        skew=0.1,
        kurtosis=3.2,
        candidate_label=label,
    )


def test_final_n_inputs_ignore_candidate_order_and_rename() -> None:
    original = [_raw(_DIGEST_A, "trend-a", 0.2), _raw(_DIGEST_B, "momentum-b", 0.1)]
    renamed_reversed = [
        _raw(_DIGEST_B, "renamed completely", 0.1),
        _raw(_DIGEST_A, "also renamed", 0.2),
    ]
    expected = finalize_score_inputs(
        original,
        global_trial_count=19,
        reviewed_cross_trial_variance=0.04,
    )
    actual = finalize_score_inputs(
        renamed_reversed,
        global_trial_count=19,
        reviewed_cross_trial_variance=0.04,
    )
    assert actual == expected
    assert [row.semantic_config_fingerprint for row in actual] == [_DIGEST_A, _DIGEST_B]
    assert all(row.global_trial_count == 19 for row in actual)


def test_final_n_inputs_refuse_duplicate_semantic_identity() -> None:
    with pytest.raises(ValueError, match="duplicate semantic identity"):
        finalize_score_inputs(
            [_raw(_DIGEST_A, "a", 0.2), _raw(_DIGEST_A, "renamed-a", 0.2)],
            global_trial_count=2,
            reviewed_cross_trial_variance=0.01,
        )


def test_checked_in_manifest_is_valid_and_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "research/five_tool_v3_6_campaign_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_campaign_manifest(manifest)

    performance_claim = copy.deepcopy(manifest)
    performance_claim["performance_claims"] = ["positive expectancy"]
    with pytest.raises(ValueError, match="performance_claims"):
        validate_campaign_manifest(performance_claim)

    unlocked_placeholder = copy.deepcopy(manifest)
    unlocked_placeholder["strategy"]["input_contract"]["sha256"] = None
    unlocked_placeholder["strategy"]["input_contract"]["status"] = "resolved"
    with pytest.raises(ValueError, match="null digest"):
        validate_campaign_manifest(unlocked_placeholder)


def test_manifest_bound_broker_refuses_its_declared_holdout_before_open(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "research/five_tool_v3_6_campaign_manifest.json").read_text(encoding="utf-8")
    )
    path = tmp_path / "five_tool_trials.jsonl"
    broker = FiveToolTrialBroker.from_campaign_manifest(path, manifest)
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return b"must not open"

    with pytest.raises(HoldoutAccessRefused, match="declared holdout"):
        broker.run(
            _definition(),
            _request(dataset_id="five-tool-certified-holdout-2026q4"),
            reader=poison,
            evaluator=lambda data, receipt: (data, receipt),
        )
    assert opened is False
    assert not path.exists()

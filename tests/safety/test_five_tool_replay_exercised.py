"""A Five-Tool trial is re-executable byte-for-byte, or it is not a sealed trial.

Track B.1 made every attempt count. Track B.2 made every certified read prove what it
touched. Both left the same residual standing, and this file's predecessor named it in the
registry suite: ``test_no_replay_artifact_capability_exists`` asserted that the evaluator's
evidence bytes were "digested but never persisted anywhere on disk". The ledger recorded
``evidence_artifact_sha256`` and threw the bytes away, so a completed trial could be
counted, sealed, and cited while being impossible to re-derive by anyone who did not
already hold the artifact. A digest of something nobody kept is a receipt for a parcel that
was never mailed.

``chronos.research.five_tool.replay`` is the store that closes it and
``chronos.research.five_tool_trials.replay_trial`` is the entry point that consumes it. An
attempt that gets as far as opening data now persists a **content-addressed** artifact
carrying everything needed to re-execute it: campaign and attempt identity, the engine
identity **including the semantic configuration itself**, the input locks and the digest of
the bytes read, the certified attestation and receipt digests, the durable start, and the
outcome — for a completed attempt the evidence bytes themselves. The terminal ledger record
names that artifact's digest, and the artifact is written *before* the terminal, so a
completed trial that cannot be replayed cannot exist.

Every refusal below is driven in the rejecting direction and then repaired, so a refusal
caused by something else cannot pass as this control firing — the guard-the-guard pattern
of ``test_five_tool_holdout_refusal_exercised.py``, ``test_five_tool_registry_exercised.py``
and ``test_five_tool_certified_reader_exercised.py``. Composition with all three is
asserted rather than assumed.

**This reproduces nothing that matters yet.** Reproducing a number proves the number is
deterministic, never that it is true, profitable, or in-limits. No hypothesis is tested
here, no dataset is certified, the campaign manifest stays blocked, the repository ships no
replay artifacts, and every store below lives in a pytest temporary directory.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chronos.registry.ledger import KIND_RUN, RegistryLedger
from chronos.research.five_tool.certified_reader import (
    CERTIFICATION_MANIFEST_NAME,
    CertifiedDatasetReader,
    build_certification_manifest,
)
from chronos.research.five_tool.replay import (
    ARTIFACT_FILENAME_PREFIX,
    REPLAY_ARTIFACT_SCHEMA_VERSION,
    ReplayArtifactDigestMismatch,
    ReplayArtifactInvalid,
    ReplayArtifactMissing,
    ReplayArtifactUnavailable,
    ReplayDivergence,
    ReplayDivergenceReason,
    artifact_digest,
    artifact_path,
    canonical_artifact_bytes,
    compare_replay_bodies,
    evidence_digest,
    load_replay_artifact,
    validate_artifact_body,
    write_replay_artifact,
)
from chronos.research.five_tool_trials import (
    EXECUTION_READY,
    KIND_TRIAL_TERMINAL,
    MISSING_CERTIFIED_RESEARCH_CAPABILITIES,
    CampaignExecutionBlocked,
    DataAccessRequest,
    EvaluationEvidence,
    FiveToolTrialBroker,
    FiveToolTrialError,
    HoldoutAccessRefused,
    ReplayArtifactBindingBroken,
    ReviewedVarianceEvidence,
    TrialDefinition,
    TrialEvaluation,
    TrialReceipt,
    ledger_trial_multiplicity,
    registered_trial_count,
    replay_trial,
    seal_ledger_local_score_inputs,
    validate_campaign_manifest,
)

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_CRITERIA_PATH = _ROOT / "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md"
_DATA = b"content-addressed-certified-five-tool-dataset-v1\n"
_DATA_SHA256 = hashlib.sha256(_DATA).hexdigest()
_CODE_COMMIT = "1" * 40
_REFERENCE_CELL = "5t-trend-directional-paired"
_ARTIFACT = b'{"metric":"raw-score-evidence-that-is-now-persisted"}'
_VARIANCE = ReviewedVarianceEvidence(0.04, "reviewed-sample-variance-v1", "f" * 64)
_DATASET_FILES: dict[str, bytes] = {
    "validation/AAA.csv": (
        b"date,open,high,low,close,volume\n2020-01-02,11.0,11.5,10.5,11.25,2000\n"
    ),
}


# --------------------------------------------------------------------------------------
# Fixtures: the synthetic lifecycle harness, with an artifact store beside its ledger.
# --------------------------------------------------------------------------------------


def _committed_manifest() -> dict[str, Any]:
    loaded = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _synthetic_ready_manifest(*, dataset_sha256: str = _DATA_SHA256) -> dict[str, Any]:
    """Resolve the committed manifest's locks for the private lifecycle harness only.

    This carries no readiness authority: the public API refuses ``EXECUTION_READY``
    outright (``test_the_public_refusal_now_names_exactly_one_capability`` below).
    """

    manifest = _committed_manifest()
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
        "sha256": dataset_sha256,
        "status": "resolved",
        "required_before_execution": True,
    }
    manifest["execution_bindings"] = {
        "schema_version": "chronos-five-tool-execution-bindings-v1",
        "status": "resolved",
        "catalog_manifest_sha256": "a" * 64,
        "partition_stage_map": {"validation": "validation"},
        "requests": [
            {
                "request_id": "synthetic-validation",
                "dataset_id": "five-tool-certified-daily-v1",
                "partition": "validation",
                "data_version": dataset_sha256,
                "source_id": "synthetic-lifecycle-source",
                "source_receipt_sha256": "b" * 64,
            }
        ],
        "evaluator": {
            "schema_version": "chronos-five-tool-evaluator-v1",
            "evaluator_id": "synthetic-lifecycle-evaluator",
            "sha256": "c" * 64,
        },
        "resolution_blockers": [],
    }
    return manifest


def _definition(
    manifest: dict[str, Any],
    *,
    cell_id: str = _REFERENCE_CELL,
    config: dict[str, object] | None = None,
    code_commit: str | None = None,
    criteria_digest: str | None = None,
    input_contract_digest: str | None = None,
) -> TrialDefinition:
    cells = [cell for cell in manifest["campaign_cells"] if cell["cell_id"] == cell_id]
    assert len(cells) == 1
    return TrialDefinition(
        campaign_id=manifest["campaign_id"],
        cell_id=cell_id,
        hypothesis_id=cells[0]["hypothesis_id"],
        strategy_id=manifest["strategy"]["strategy_id"],
        semantic_config=(
            copy.deepcopy(cells[0]["ablation_policy"]) if config is None else copy.deepcopy(config)
        ),
        code_commit=code_commit or manifest["code_commit_lock"]["git_commit"],
        criteria_digest=criteria_digest or manifest["criteria_lock"]["sha256"],
        input_contract_digest=(
            input_contract_digest or manifest["strategy"]["input_contract"]["sha256"]
        ),
    )


def _request(manifest: dict[str, Any], *, partition: str = "validation") -> DataAccessRequest:
    lock = manifest["data"]["dataset_version_lock"]
    return DataAccessRequest(
        dataset_id=lock["dataset_id"],
        partition=partition,
        data_version=lock["sha256"],
    )


def _evidence(*, artifact: bytes = _ARTIFACT, sharpe: float = 0.10) -> EvaluationEvidence:
    return EvaluationEvidence(
        artifact_bytes=artifact,
        observed_sharpe=sharpe,
        observations=500,
        skew=0.0,
        kurtosis=3.0,
    )


def _evaluate(_data: bytes, receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
    return TrialEvaluation(value=receipt, evidence=_evidence())


def _broker(
    tmp_path: Path,
    manifest: dict[str, Any],
    *,
    trial_name: str = "trials.jsonl",
    artifacts: Path | None = None,
) -> FiveToolTrialBroker:
    return FiveToolTrialBroker._from_synthetic_manifest_for_tests(
        tmp_path / trial_name,
        manifest,
        registry_ledger_path=tmp_path / "registry.jsonl",
        artifact_root=artifacts if artifacts is not None else tmp_path / "replay_artifacts",
    )


def _store(tmp_path: Path) -> Path:
    return tmp_path / "replay_artifacts"


def _only_artifact(tmp_path: Path) -> Path:
    files = sorted(_store(tmp_path).glob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))
    assert len(files) == 1, f"expected exactly one artifact, found {files}"
    return files[0]


def _bound_digest(broker: FiveToolTrialBroker) -> str:
    terminals = RegistryLedger(broker.ledger_path).records_of(KIND_TRIAL_TERMINAL)
    assert len(terminals) == 1
    digest = terminals[-1].payload["replay_artifact_sha256"]
    assert isinstance(digest, str)
    return digest


def _run_once(tmp_path: Path, manifest: dict[str, Any]) -> tuple[FiveToolTrialBroker, str]:
    broker = _broker(tmp_path, manifest)
    broker.run(
        _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
    )
    return broker, _bound_digest(broker)


def _certified_dataset(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "certified-dataset"
    for relative, data in _DATASET_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    certification = build_certification_manifest(
        root,
        dataset_id="five-tool-certified-daily-v1",
        accessible_partitions=("validation",),
        certified_at_utc="2026-08-09T00:00:00Z",
    )
    (root / CERTIFICATION_MANIFEST_NAME).write_text(
        json.dumps(certification, indent=2, sort_keys=True), encoding="utf-8"
    )
    return root, certification


# --------------------------------------------------------------------------------------
# The capability itself: the bytes are kept, content-addressed, and bound to the ledger.
# --------------------------------------------------------------------------------------


def test_a_completed_trial_persists_the_bytes_the_ledger_only_digested(tmp_path: Path) -> None:
    """The former absence proof, inverted: the artifact bytes now exist on disk."""

    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)

    written = b"".join(path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file())
    assert hashlib.sha256(_ARTIFACT).hexdigest().encode() in written
    assert base64.b64encode(_ARTIFACT) in written, "the evidence bytes are not recoverable"

    artifact = load_replay_artifact(_store(tmp_path), digest)
    assert artifact.output_bytes == _ARTIFACT
    assert artifact.outcome == "completed"
    assert artifact.evidence_digest == _evidence().evidence_digest
    # The dataset is NOT copied into the store: input identity stays content-addressed,
    # exactly as chronos.research.repro records dataset digests rather than CSV bytes.
    assert _DATA not in written
    assert artifact.document["inputs"]["data_sha256"] == _DATA_SHA256
    assert broker.artifact_root == _store(tmp_path)


def test_the_artifact_is_addressed_by_the_digest_of_its_own_canonical_bytes(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)

    path = _only_artifact(tmp_path)
    assert path.name == f"{ARTIFACT_FILENAME_PREFIX}{digest}.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    document = json.loads(raw)
    assert canonical_artifact_bytes(document) == raw
    assert artifact_digest(document) == digest
    assert document["schema_version"] == REPLAY_ARTIFACT_SCHEMA_VERSION


def test_the_artifact_carries_the_configuration_not_only_its_fingerprint(
    tmp_path: Path,
) -> None:
    """A fingerprint identifies a config to whoever already has it; a replay artifact is
    for whoever does not."""

    manifest = _synthetic_ready_manifest()
    definition = _definition(manifest)
    _, digest = _run_once(tmp_path, manifest)
    engine = load_replay_artifact(_store(tmp_path), digest).document["engine"]

    assert engine["semantic_config"] == json.loads(definition.semantic_config_json)
    assert engine["semantic_config_fingerprint"] == definition.semantic_config_fingerprint
    assert engine["code_commit"] == _CODE_COMMIT
    assert engine["criteria_digest"] == manifest["criteria_lock"]["sha256"]
    assert engine["input_contract_digest"] == manifest["strategy"]["input_contract"]["sha256"]


def test_the_artifact_is_written_before_the_terminal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revert the ordering: if the store refuses, no completed terminal may exist."""

    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)

    def refuse(_root: Path | None, _document: object) -> None:
        raise OSError("artifact store is unwritable")

    monkeypatch.setattr("chronos.research.five_tool_trials.write_replay_artifact", refuse)
    with pytest.raises(OSError, match="artifact store is unwritable"):
        broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
        )

    assert RegistryLedger(broker.ledger_path).records_of(KIND_TRIAL_TERMINAL) == ()
    # The attempt still counts: it opened data. Under-counting N is the failure B.1 exists
    # to prevent, and an unwritable artifact store does not license it.
    assert ledger_trial_multiplicity(broker.ledger_path) == 1
    assert registered_trial_count(tmp_path / "registry.jsonl") == 1

    monkeypatch.undo()
    repaired = _broker(tmp_path, manifest, trial_name="repaired.jsonl")
    repaired.run(
        _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
    )
    assert len(RegistryLedger(repaired.ledger_path).records_of(KIND_TRIAL_TERMINAL)) == 1


def test_an_attempt_that_died_after_reading_still_persists_a_replay_artifact(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)

    def explode(_data: bytes, _receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        raise ArithmeticError("preregistered attempt failed")

    with pytest.raises(ArithmeticError):
        broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=explode
        )

    digest = _bound_digest(broker)
    artifact = load_replay_artifact(_store(tmp_path), digest)
    assert artifact.outcome == "failed"
    assert artifact.output_bytes is None
    assert artifact.document["outputs"]["error_type"] == "ArithmeticError"
    assert artifact.document["inputs"]["data_sha256"] == _DATA_SHA256


def test_an_attempt_that_never_opened_data_persists_no_artifact_and_names_none(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)

    def refuse(_request: DataAccessRequest) -> bytes:
        raise PermissionError("reader refused before returning bytes")

    with pytest.raises(PermissionError):
        broker.run(_definition(manifest), _request(manifest), reader=refuse, evaluator=_evaluate)

    terminals = RegistryLedger(broker.ledger_path).records_of(KIND_TRIAL_TERMINAL)
    assert [record.payload["outcome"] for record in terminals] == ["failed"]
    assert terminals[-1].payload["replay_artifact_sha256"] is None
    assert not list(_store(tmp_path).glob("*")) if _store(tmp_path).exists() else True


def test_replaying_the_recorded_attempt_reproduces_it_byte_for_byte(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)

    report = replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )
    assert report.artifact_sha256 == digest
    assert report.outcome == "completed"
    assert report.data_sha256 == _DATA_SHA256
    assert report.certified_reader is False
    assert report.campaign_id == manifest["campaign_id"]


def test_a_failed_attempt_replays_to_the_same_bounded_failure(tmp_path: Path) -> None:
    """ "Aborted after read" is replayable too: the failure classification must reproduce."""

    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)

    def explode(_data: bytes, _receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        raise ArithmeticError("preregistered attempt failed")

    with pytest.raises(ArithmeticError):
        broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=explode
        )
    digest = _bound_digest(broker)

    report = replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=explode,
    )
    assert report.outcome == "failed"

    # A different failure is a divergence, not a pass: the classification is compared.
    def other_failure(_data: bytes, _receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        raise LookupError("a different failure")

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=other_failure,
        )
    assert ReplayDivergenceReason.OUTCOME_DRIFT in divergence.value.reasons
    assert "ArithmeticError" in str(divergence.value)


def test_a_replay_registers_no_trial_and_writes_no_ledger_record(tmp_path: Path) -> None:
    """A reproducibility probe is not a selection run — the repro.py stance, asserted."""

    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)
    registry = tmp_path / "registry.jsonl"
    runs_before = len(RegistryLedger(registry).records_of(KIND_RUN))
    ledger_before = RegistryLedger(broker.ledger_path).records()

    replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )

    assert len(RegistryLedger(registry).records_of(KIND_RUN)) == runs_before == 1
    assert RegistryLedger(broker.ledger_path).records() == ledger_before
    assert registered_trial_count(registry) == 1
    assert len(list(_store(tmp_path).glob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))) == 1


# --------------------------------------------------------------------------------------
# Conjunct: no artifact store, no trial — checked before the registry and the reader.
# --------------------------------------------------------------------------------------


def test_an_unwired_artifact_store_refuses_the_trial_before_it_is_counted(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    broker = FiveToolTrialBroker._from_synthetic_manifest_for_tests(
        tmp_path / "unwired.jsonl",
        manifest,
        registry_ledger_path=tmp_path / "registry.jsonl",
    )
    # Reach past the harness default to the state production wiring can actually be in.
    broker._artifact_root = None
    opened: list[str] = []

    with pytest.raises(ReplayArtifactUnavailable, match="no replay-artifact store is wired"):
        broker.run(
            _definition(manifest),
            _request(manifest),
            reader=lambda request: opened.append(request.dataset_id) or _DATA,
            evaluator=_evaluate,
        )

    assert opened == [], "the reader ran for an attempt that could never be replayed"
    assert not (tmp_path / "unwired.jsonl").exists()
    assert not (tmp_path / "registry.jsonl").exists()

    broker._artifact_root = _store(tmp_path)
    broker.run(
        _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
    )
    assert opened == []
    assert ledger_trial_multiplicity(tmp_path / "unwired.jsonl") == 1


def test_an_absent_artifact_store_root_refuses_the_trial_and_is_not_created(
    tmp_path: Path,
) -> None:
    """A research run may not conjure the place its own reproducibility is kept."""

    manifest = _synthetic_ready_manifest()
    absent = tmp_path / "never-provisioned" / "replay_artifacts"
    broker = _broker(tmp_path, manifest, trial_name="absent.jsonl", artifacts=absent)

    with pytest.raises(ReplayArtifactUnavailable, match="is absent; the store is provisioned"):
        broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
        )
    assert not absent.exists()
    assert not absent.parent.exists()

    absent.parent.mkdir()
    broker.run(
        _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
    )
    assert absent.is_dir()
    assert len(list(absent.glob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))) == 1


# --------------------------------------------------------------------------------------
# Conjunct: artifact missing, tampered, and self-inconsistent each refuse distinctly.
# --------------------------------------------------------------------------------------


def test_a_missing_artifact_refuses_both_the_replay_and_the_seal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)
    path = _only_artifact(tmp_path)
    kept = path.read_bytes()
    path.unlink()

    with pytest.raises(ReplayArtifactMissing, match="no replay artifact at"):
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )
    with pytest.raises(ReplayArtifactMissing, match="cannot be sealed"):
        _seal_reference_cell(broker, manifest)

    path.write_bytes(kept)
    replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )


def test_tampered_artifact_bytes_no_longer_hash_to_their_content_address(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)
    path = _only_artifact(tmp_path)
    kept = path.read_bytes()

    document = json.loads(kept)
    document["outputs"]["observed_sharpe"] = 9.99
    path.write_bytes(canonical_artifact_bytes(document))

    with pytest.raises(ReplayArtifactDigestMismatch, match="not to the content address"):
        load_replay_artifact(_store(tmp_path), digest)
    with pytest.raises(ReplayArtifactDigestMismatch):
        _seal_reference_cell(broker, manifest)

    # Re-serializing the *unmodified* document with different framing is caught too: the
    # content address is over exact bytes, not over an equivalent JSON value.
    path.write_bytes(json.dumps(json.loads(kept), indent=2).encode("utf-8"))
    with pytest.raises(ReplayArtifactDigestMismatch):
        load_replay_artifact(_store(tmp_path), digest)

    path.write_bytes(kept)
    assert load_replay_artifact(_store(tmp_path), digest).artifact_sha256 == digest
    assert _seal_reference_cell(broker, manifest)[0].replay_artifact_sha256 == digest


@pytest.mark.parametrize(
    ("clause", "doctor", "message"),
    [
        (
            "a config fingerprint that does not fingerprint its config",
            lambda document: document["engine"].__setitem__("semantic_config", {"tampered": True}),
            "semantic_config_fingerprint disagrees with the config it fingerprints",
        ),
        (
            "an artifact digest that does not digest its bytes",
            lambda document: document["outputs"].__setitem__(
                "artifact_base64", base64.b64encode(b"other bytes").decode("ascii")
            ),
            "artifact_sha256 disagrees with the embedded output bytes",
        ),
        (
            "an evidence digest over statistics it does not describe",
            lambda document: document["outputs"].__setitem__("skew", 0.5),
            "evidence_digest disagrees with the artifact and statistics it digests",
        ),
        (
            "a completed outcome with no bytes at all",
            lambda document: document["outputs"].__setitem__("artifact_base64", None),
            "must embed its evidence bytes",
        ),
        (
            "a failed outcome that kept an output",
            lambda document: document["outputs"].__setitem__("outcome", "failed"),
            "a failed attempt cannot record outputs",
        ),
        (
            "an unresolved code commit",
            lambda document: document["engine"].__setitem__("code_commit", "unknown"),
            "code_commit must be resolved",
        ),
        (
            "an uncertified read claiming certified digests",
            lambda document: document["inputs"]["certified_read"].__setitem__(
                "certification_manifest_sha256", "0" * 64
            ),
            "an uncertified read cannot carry certification manifest",
        ),
        (
            "a schema the store does not speak",
            lambda document: document.__setitem__("schema_version", "chronos-something-else"),
            "schema_version must be",
        ),
        (
            "an unknown field smuggled into the schema",
            lambda document: document["outputs"].__setitem__("promotion_authority", "granted"),
            "keys do not match schema",
        ),
    ],
)
def test_an_artifact_whose_own_digests_disagree_with_its_own_payload_is_refused(
    tmp_path: Path,
    clause: str,
    doctor: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    """Guard the guard: an internal digest that is decoration refuses on its own clause.

    Each body is rewritten at *its own* new content address, so the file-level digest check
    passes and only the self-consistency clause under test can fire.
    """

    manifest = _synthetic_ready_manifest()
    _run_once(tmp_path, manifest)
    document = json.loads(_only_artifact(tmp_path).read_bytes())
    assert validate_artifact_body(document) == document, clause

    doctor(document)
    payload = canonical_artifact_bytes(document)
    tampered = artifact_path(_store(tmp_path), hashlib.sha256(payload).hexdigest())
    tampered.write_bytes(payload)

    with pytest.raises(ReplayArtifactInvalid, match=message):
        load_replay_artifact(_store(tmp_path), hashlib.sha256(payload).hexdigest())


def test_a_content_address_is_never_overwritten_with_different_bytes(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    _run_once(tmp_path, manifest)
    path = _only_artifact(tmp_path)
    document = json.loads(path.read_bytes())

    # Writing the same document again is idempotent, not an error.
    assert write_replay_artifact(_store(tmp_path), document).path == path

    path.write_bytes(b"{}")
    with pytest.raises(ReplayArtifactInvalid, match="already holds different bytes"):
        write_replay_artifact(_store(tmp_path), document)


# --------------------------------------------------------------------------------------
# Conjunct: the ledger binding is checked in both directions before a campaign seals.
# --------------------------------------------------------------------------------------


def _seal_reference_cell(
    broker: FiveToolTrialBroker,
    manifest: dict[str, Any],
) -> tuple[Any, ...]:
    """Seal a one-cell campaign so the seal path can be exercised on one attempt."""

    manifest["campaign_cells"] = [
        cell for cell in manifest["campaign_cells"] if cell["cell_id"] == _REFERENCE_CELL
    ]
    binding = broker._require_binding()
    object.__setattr__(
        broker,
        "_binding",
        type(binding)(
            campaign_id=binding.campaign_id,
            manifest_sha256=binding.manifest_sha256,
            strategy_id=binding.strategy_id,
            code_commit=binding.code_commit,
            criteria_digest=binding.criteria_digest,
            input_contract_digest=binding.input_contract_digest,
            dataset_id=binding.dataset_id,
            accessible_partitions=binding.accessible_partitions,
            request_identities=binding.request_identities,
            cells=tuple(cell for cell in binding.cells if cell.cell_id == _REFERENCE_CELL),
        ),
    )
    return seal_ledger_local_score_inputs(broker, reviewed_variance=_VARIANCE)


def test_an_artifact_for_another_attempt_cannot_be_bound_to_this_trial(
    tmp_path: Path,
) -> None:
    """Content addressing stops a swap; this stops a *valid* artifact being mis-bound."""

    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)

    # A second, genuinely valid artifact — same store, different attempt.
    other = _broker(tmp_path, manifest, trial_name="other.jsonl")
    other.run(
        _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
    )
    other_digest = _bound_digest(other)
    assert other_digest != digest

    ledger = RegistryLedger(broker.ledger_path)
    terminal = ledger.records_of(KIND_TRIAL_TERMINAL)[-1]
    swapped = dict(terminal.payload)
    swapped["replay_artifact_sha256"] = other_digest
    # Rewrite the ledger's terminal in place, then re-anchor, so only the binding is wrong.
    _rewrite_last_record(broker.ledger_path, swapped)

    with pytest.raises(ReplayArtifactBindingBroken, match="disagrees with its trial record"):
        _seal_reference_cell(broker, manifest)

    _rewrite_last_record(broker.ledger_path, dict(terminal.payload))
    assert _seal_reference_cell(broker, manifest)[0].replay_artifact_sha256 == digest


def _rewrite_last_record(ledger_path: Path, payload: dict[str, Any]) -> None:
    """Rebuild the ledger with one replaced terminal payload and a fresh, valid chain.

    Rebuilding re-stamps every record hash, so the replacement's start binding is
    recomputed against the freshly appended start.  That keeps the chain, the anchor, and
    the start binding all genuinely correct — leaving exactly one thing wrong, which is the
    thing under test.  Editing bytes in place instead would trip the chain verification
    and the refusal could not be attributed to the replay control.
    """

    records = RegistryLedger(ledger_path).records()
    rebuilt = [
        (record.kind, dict(record.payload) if record.sequence != records[-1].sequence else payload)
        for record in records
    ]
    ledger_path.unlink()
    for extra in (
        ledger_path.parent / f"{ledger_path.name}.anchor",
        ledger_path.parent / f"{ledger_path.name}.lock",
        ledger_path.parent / f"{ledger_path.stem}.head.json",
    ):
        extra.unlink(missing_ok=True)
    fresh = RegistryLedger(ledger_path)
    starts: dict[str, Any] = {}
    for kind, item in rebuilt:
        if kind == KIND_TRIAL_TERMINAL:
            start = starts[str(item["attempt_id"])]
            item["start_sequence"] = start.sequence
            item["start_record_hash"] = start.record_hash
        appended = fresh.append(kind, item)
        if kind != KIND_TRIAL_TERMINAL:
            starts[str(item["attempt_id"])] = appended


def test_a_completed_trial_record_that_names_no_artifact_cannot_seal(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)
    terminal = RegistryLedger(broker.ledger_path).records_of(KIND_TRIAL_TERMINAL)[-1]

    unbound = dict(terminal.payload)
    unbound["replay_artifact_sha256"] = None
    _rewrite_last_record(broker.ledger_path, unbound)
    with pytest.raises(FiveToolTrialError, match="completed terminal has no replay artifact"):
        _seal_reference_cell(broker, manifest)

    _rewrite_last_record(broker.ledger_path, dict(terminal.payload))
    assert _seal_reference_cell(broker, manifest)[0].replay_artifact_sha256 == digest


def test_a_sealed_campaigns_score_rows_name_the_artifacts_that_reproduce_them(
    tmp_path: Path,
) -> None:
    manifest = _synthetic_ready_manifest()
    broker, digest = _run_once(tmp_path, manifest)
    rows = _seal_reference_cell(broker, manifest)

    assert [row.replay_artifact_sha256 for row in rows] == [digest]
    # Re-sealing is idempotent and re-verifies the store rather than trusting the seal.
    assert _seal_reference_cell(broker, manifest) == rows
    _only_artifact(tmp_path).unlink()
    with pytest.raises(ReplayArtifactMissing):
        _seal_reference_cell(broker, manifest)


# --------------------------------------------------------------------------------------
# Conjunct: replay divergence, one distinct named reason per axis that can move.
# --------------------------------------------------------------------------------------


def test_an_input_digest_that_changed_is_named_as_an_input_divergence(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: b"different dataset bytes\n",
            evaluator=_evaluate,
        )
    reasons = divergence.value.reasons
    assert ReplayDivergenceReason.INPUT_DIGEST_DRIFT in reasons
    assert "inputs.data_sha256" in str(divergence.value)

    replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )


def test_a_configuration_that_changed_is_named_as_a_config_divergence(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)
    drifted = _definition(manifest, config={"tuned_after_the_fact": True})

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            drifted,
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )
    assert ReplayDivergenceReason.CONFIG_DRIFT in divergence.value.reasons
    assert "engine.semantic_config" in str(divergence.value)


@pytest.mark.parametrize(
    ("axis", "kwargs", "reason", "field"),
    [
        (
            "code commit",
            {"code_commit": "2" * 40},
            ReplayDivergenceReason.COMMIT_DRIFT,
            "engine.code_commit",
        ),
        (
            "criteria digest",
            {"criteria_digest": "3" * 64},
            ReplayDivergenceReason.CRITERIA_DRIFT,
            "engine.criteria_digest",
        ),
        (
            "input contract digest",
            {"input_contract_digest": "4" * 64},
            ReplayDivergenceReason.INPUT_CONTRACT_DRIFT,
            "engine.input_contract_digest",
        ),
    ],
)
def test_each_engine_identity_axis_diverges_under_its_own_name(
    tmp_path: Path,
    axis: str,
    kwargs: dict[str, str],
    reason: ReplayDivergenceReason,
    field: str,
) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest, **kwargs),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )
    assert reason in divergence.value.reasons, axis
    assert field in str(divergence.value)


def test_an_output_that_changed_is_named_as_an_output_divergence(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)

    def different_output(_data: bytes, receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        return TrialEvaluation(value=receipt, evidence=_evidence(artifact=b'{"metric":"other"}'))

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=different_output,
        )
    assert ReplayDivergenceReason.OUTPUT_DRIFT in divergence.value.reasons
    message = str(divergence.value)
    assert "outputs.artifact_base64" in message
    assert "outputs.artifact_sha256" in message
    assert "outputs.evidence_digest" in message

    # A statistic alone is enough — identical bytes with a different Sharpe still diverge.
    def different_statistic(_data: bytes, receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
        return TrialEvaluation(value=receipt, evidence=_evidence(sharpe=0.11))

    with pytest.raises(ReplayDivergence) as second:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=different_statistic,
        )
    assert "outputs.observed_sharpe" in str(second.value)


def test_the_digest_backstop_catches_a_field_the_comparison_forgot(tmp_path: Path) -> None:
    """Guard the guard: "any byte divergence" must survive the schema growing."""

    manifest = _synthetic_ready_manifest()
    _run_once(tmp_path, manifest)
    recorded = json.loads(_only_artifact(tmp_path).read_bytes())
    observed = copy.deepcopy(recorded)
    observed["start"]["sequence"] = recorded["start"]["sequence"] + 1

    assert compare_replay_bodies(recorded, recorded) == ()
    findings = compare_replay_bodies(recorded, observed)
    assert [finding.reason for finding in findings] == [
        ReplayDivergenceReason.UNCOMPARED_FIELD_DRIFT
    ]


# --------------------------------------------------------------------------------------
# Composition: Track A, B.1 and B.2 still hold, and provenance is recorded as proven.
# --------------------------------------------------------------------------------------


def test_a_certified_read_is_recorded_in_the_artifact_and_replays_certified(
    tmp_path: Path,
) -> None:
    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    broker = _broker(tmp_path, manifest)
    broker.run(
        _definition(manifest),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )
    digest = _bound_digest(broker)
    certified_block = load_replay_artifact(_store(tmp_path), digest).document["inputs"][
        "certified_read"
    ]

    assert certified_block["certified_reader"] is True
    assert certified_block["certification_manifest_sha256"] is not None
    assert certified_block["files"] == {
        path: hashlib.sha256(data).hexdigest() for path, data in _DATASET_FILES.items()
    }
    assert certified_block["receipt_payload_sha256"] == certification["dataset_sha256"]

    report = replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )
    assert report.certified_reader is True


def test_replaying_certified_bytes_through_an_uncertified_reader_diverges(
    tmp_path: Path,
) -> None:
    """Identical bytes with a weaker provenance story are still a divergence."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    broker = _broker(tmp_path, manifest)
    payload = CertifiedDatasetReader(root)(_request(manifest))
    broker.run(
        _definition(manifest),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )
    digest = _bound_digest(broker)

    with pytest.raises(ReplayDivergence) as divergence:
        replay_trial(
            _store(tmp_path),
            digest,
            _definition(manifest),
            _request(manifest),
            reader=lambda _: payload,
            evaluator=_evaluate,
        )
    assert ReplayDivergenceReason.CERTIFIED_READ_DRIFT in divergence.value.reasons
    assert "inputs.certified_read" in str(divergence.value)

    replay_trial(
        _store(tmp_path),
        digest,
        _definition(manifest),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )


def test_an_unproven_certified_read_never_becomes_a_certified_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attestation is a commitment; only the receipt makes the artifact say certified."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    payload = CertifiedDatasetReader(root)(_request(manifest))
    broker = _broker(tmp_path, manifest)
    monkeypatch.setattr(CertifiedDatasetReader, "__call__", lambda _self, _request: payload)

    with pytest.raises(FiveToolTrialError):
        broker.run(
            _definition(manifest),
            _request(manifest),
            reader=CertifiedDatasetReader(root),
            evaluator=_evaluate,
        )
    monkeypatch.undo()

    artifacts = sorted(_store(tmp_path).glob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))
    assert len(artifacts) == 1
    document = json.loads(artifacts[0].read_bytes())
    assert document["inputs"]["certified_read"]["certified_reader"] is False
    assert document["inputs"]["certified_read"]["receipt_payload_sha256"] is None
    assert document["outputs"]["outcome"] == "failed"


def test_a_declared_holdout_is_still_refused_before_any_artifact_exists(
    tmp_path: Path,
) -> None:
    """Track A's refusal composes: nothing is persisted for a request that never runs."""

    manifest = _synthetic_ready_manifest()
    # Track A's shape: a holdout carved from the campaign's own dataset id, which the
    # identity check cannot distinguish and only ``_refuse_holdout`` catches.
    manifest["data"]["declared_holdouts"][0]["dataset_id"] = manifest["data"][
        "dataset_version_lock"
    ]["dataset_id"]
    broker = _broker(tmp_path, manifest)

    with pytest.raises(HoldoutAccessRefused):
        broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
        )
    assert not _store(tmp_path).exists()
    assert not broker.ledger_path.exists()


def test_the_registry_still_records_the_attempt_before_the_artifact_is_written(
    tmp_path: Path,
) -> None:
    """B.1's ordering is unchanged: register, then read, then persist, then terminalize."""

    manifest = _synthetic_ready_manifest()
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)
    seen: list[int] = []

    def reader(_request: DataAccessRequest) -> bytes:
        seen.append(len(RegistryLedger(registry).records_of(KIND_RUN)))
        seen.append(len(list(_store(tmp_path).glob("*"))) if _store(tmp_path).exists() else 0)
        return _DATA

    broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate)
    assert seen == [1, 0], "the canonical record must precede the read and the artifact follow it"
    assert len(list(_store(tmp_path).glob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))) == 1


# --------------------------------------------------------------------------------------
# The duplicated digest formula, and the vocabulary shared with the walk-forward plane.
# --------------------------------------------------------------------------------------


def test_the_replay_stores_evidence_digest_matches_the_trial_ledgers(tmp_path: Path) -> None:
    """The store cannot import the broker, so the two formulas are pinned equal here."""

    evidence = _evidence()
    assert (
        evidence_digest(
            artifact_sha256=evidence.artifact_sha256,
            observed_sharpe=evidence.observed_sharpe,
            observations=evidence.observations,
            skew=evidence.skew,
            kurtosis=evidence.kurtosis,
        )
        == evidence.evidence_digest
    )
    assert tmp_path.is_dir()


def test_the_replay_vocabulary_matches_the_walk_forward_planes() -> None:
    """One repository, one language for byte-identity — without coupling the planes."""

    from chronos.research.repro import CompareReason

    shared = {"config_drift", "commit_drift", "output_drift"}
    assert shared <= {reason.value for reason in CompareReason}
    assert shared <= {reason.value for reason in ReplayDivergenceReason}
    replay_source = (_ROOT / "src/chronos/research/five_tool/replay.py").read_text(encoding="utf-8")
    assert "import" in replay_source
    assert "from chronos.research.repro" not in replay_source
    assert "chronos.research.runner" not in replay_source


# --------------------------------------------------------------------------------------
# Recognized, not unblocking: exactly one capability remains, and only the owner has it.
# --------------------------------------------------------------------------------------


def test_the_public_refusal_now_names_exactly_one_capability(tmp_path: Path) -> None:
    """A store that reproduces a number is not a limit, a power calculation, or economics."""

    assert MISSING_CERTIFIED_RESEARCH_CAPABILITIES == ("owner evidence",)
    manifest = _synthetic_ready_manifest()

    with pytest.raises(CampaignExecutionBlocked) as blocked:
        validate_campaign_manifest(manifest)
    message = str(blocked.value)
    assert "EXECUTION_READY is not implemented" in message
    assert "the missing owner evidence capability" in message
    for landed in ("replay", "artifact", "certified", "registry", "adr-0013"):
        assert landed not in message.casefold()

    committed = _committed_manifest()
    assert committed["execution_state"] == "blocked_until_identity_locks_resolve"
    blocked_broker = FiveToolTrialBroker.from_campaign_manifest(
        tmp_path / "blocked.jsonl", committed
    )
    with pytest.raises(CampaignExecutionBlocked, match="no owner evidence capability exists"):
        blocked_broker.run(
            _definition(manifest), _request(manifest), reader=lambda _: _DATA, evaluator=_evaluate
        )
    assert not (tmp_path / "blocked.jsonl").exists()


def test_the_repository_ships_no_replay_artifacts() -> None:
    """The capability landed; nothing has run, so nothing is stored under research/."""

    assert importlib.util.find_spec("chronos.research.five_tool.replay") is not None
    assert not list(_ROOT.joinpath("research").rglob(f"{ARTIFACT_FILENAME_PREFIX}*.json"))
    assert not list(_ROOT.joinpath("research").rglob("replay_artifacts"))
    assert not _ROOT.joinpath("research/registry").exists()


def test_replaying_a_trial_never_loads_the_holdout_unlock_capability(tmp_path: Path) -> None:
    """ADR-0013 §7 in the behavioural direction, for the replay path as well."""

    manifest = _synthetic_ready_manifest()
    _, digest = _run_once(tmp_path, manifest)
    probe = (
        "import sys; from pathlib import Path; "
        "from chronos.research.five_tool.replay import load_replay_artifact; "
        f"artifact = load_replay_artifact(Path({str(_store(tmp_path))!r}), {digest!r}); "
        "assert artifact.output_bytes; "
        "print('LOADED' if 'chronos.registry.holdout_guardian' in sys.modules else 'ABSENT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "ABSENT"

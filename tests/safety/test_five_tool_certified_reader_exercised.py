"""A Five-Tool read must PROVE what it touched, or it is not a certified read.

Track B.1 made every attempt count in the canonical ADR-0013 registry. It left the
residual it disclosed about itself: "the arbitrary reader callback still cannot prove what
data it touched" (RISK_REGISTER R-44 residual (b)). Every registered run therefore recorded
``certified_reader: False`` **by construction** — a constant, not a discriminator. A
constant field that looks like provenance is the R-24..R-27 defect class in a new costume.

``chronos.research.five_tool.certified_reader`` closes it, and this file is the exercised
proof. The reader is digest-locked: it opens a dataset only under a certification manifest
that binds every file to a SHA-256 and the dataset to one overall digest **derived from
the bytes**. Each way that lock can fail refuses with its own message, and each refusal
below is driven in the rejecting direction and then repaired, so a refusal caused by
something else cannot pass as this control firing (the guard-the-guard pattern of
``test_five_tool_holdout_refusal_exercised.py`` and ``test_five_tool_registry_exercised.py``).

The property that matters most is two-directional. ``certified_reader: True`` appears in a
registered run **only** when the bytes came through the exact certified type and the
post-read receipt matches the pre-read attestation; an arbitrary callback, a subclass, a
wrapper, mixed certified/uncertified access, or a reader that stops recording what it read
all stay ``False`` or fail the trial closed. Identical bytes with a weaker provenance story
still get the weaker record — that is the whole point.

Composition is asserted, not assumed: a declared holdout is still refused before the
reader is consulted at all, registration still precedes the read, and a partition outside
the certification manifest is refused by the reader as defense in depth beside the
campaign's own partition check.

**This certifies nothing.** No certification manifest is produced for
``research/data/raw`` or any real dataset — the dataset the campaign needs does not exist
and its sourcing is an owner decision. Every dataset below lives in a pytest temporary
directory, the campaign manifest stays blocked, and no hypothesis is tested here.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
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
    CERTIFICATION_SCHEMA_VERSION,
    CertificationManifestInvalid,
    CertificationManifestMissing,
    CertifiedDatasetReader,
    CertifiedFileMissing,
    CertifiedReadReceipt,
    DatasetDigestMismatch,
    DatasetIdentityMismatch,
    PartitionNotCertified,
    UncertifiedFilePresent,
    build_certification_manifest,
    certified_payload,
    load_certification_manifest,
)
from chronos.research.five_tool_trials import (
    _DEFAULT_HOLDOUT_PARTITIONS,
    EXECUTION_READY,
    MISSING_CERTIFIED_RESEARCH_CAPABILITIES,
    CampaignExecutionBlocked,
    CampaignIdentityMismatch,
    CertifiedProvenanceUnproven,
    DataAccessRequest,
    DataVersionMismatch,
    EvaluationEvidence,
    FiveToolTrialBroker,
    HoldoutAccessRefused,
    TrialDefinition,
    TrialEvaluation,
    TrialReceipt,
    _require_proven_certified_read,
    registered_trial_count,
    validate_campaign_manifest,
)

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_CRITERIA_PATH = _ROOT / "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md"
_CODE_COMMIT = "1" * 40
_STRATEGY_ID = "five_tool_confluence_v3_6"
_REFERENCE_CELL = "5t-trend-directional-paired"
_DATASET_ID = "five-tool-certified-daily-v1"
_CERTIFIED_AT = "2026-08-09T00:00:00Z"
_ARTIFACT = b'{"metric":"raw-score-evidence-that-is-never-persisted"}'
# Two tiny synthetic daily files. They are shaped like the corpus discipline in
# research/data/raw/MANIFEST.json (per-file bytes bound to a SHA-256) and contain no real
# market data: nothing here is, or stands in for, a certified campaign dataset.
_DATASET_FILES: dict[str, bytes] = {
    "development/AAA.csv": (
        b"date,open,high,low,close,volume\n2019-01-02,10.0,10.5,9.5,10.25,1000\n"
    ),
    "validation/AAA.csv": (
        b"date,open,high,low,close,volume\n2020-01-02,11.0,11.5,10.5,11.25,2000\n"
    ),
}


# --------------------------------------------------------------------------------------
# Fixtures: a certified dataset and a campaign locked to its digest, both tmp_path-local.
# --------------------------------------------------------------------------------------


def _write_dataset(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _certify(
    root: Path,
    *,
    dataset_id: str = _DATASET_ID,
    partitions: tuple[str, ...] = ("development", "validation"),
) -> dict[str, Any]:
    manifest = build_certification_manifest(
        root,
        dataset_id=dataset_id,
        accessible_partitions=list(partitions),
        certified_at_utc=_CERTIFIED_AT,
    )
    (root / CERTIFICATION_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _certified_dataset(
    tmp_path: Path,
    *,
    name: str = "certified",
    files: dict[str, bytes] | None = None,
    dataset_id: str = _DATASET_ID,
    partitions: tuple[str, ...] = ("development", "validation"),
) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / name
    root.mkdir()
    _write_dataset(root, files if files is not None else _DATASET_FILES)
    return root, _certify(root, dataset_id=dataset_id, partitions=partitions)


def _rewrite_certification(root: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = root / CERTIFICATION_MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _committed_manifest() -> dict[str, Any]:
    loaded = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _synthetic_ready_manifest(
    *,
    dataset_sha256: str,
    dataset_id: str = _DATASET_ID,
) -> dict[str, Any]:
    """Resolve the committed manifest's locks for the private lifecycle harness only.

    This carries no readiness authority: the public API still refuses ``EXECUTION_READY``
    (``test_the_public_refusal_now_names_exactly_two_capabilities`` below). The synthetic
    seam exists so the certified read path can be observed at all.
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
        "dataset_id": dataset_id,
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
                "dataset_id": dataset_id,
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


def _definition(manifest: dict[str, Any], *, cell_id: str = _REFERENCE_CELL) -> TrialDefinition:
    cells = [cell for cell in manifest["campaign_cells"] if cell["cell_id"] == cell_id]
    assert len(cells) == 1
    return TrialDefinition(
        campaign_id=manifest["campaign_id"],
        cell_id=cell_id,
        hypothesis_id=cells[0]["hypothesis_id"],
        strategy_id=manifest["strategy"]["strategy_id"],
        semantic_config=copy.deepcopy(cells[0]["ablation_policy"]),
        code_commit=manifest["code_commit_lock"]["git_commit"],
        criteria_digest=manifest["criteria_lock"]["sha256"],
        input_contract_digest=manifest["strategy"]["input_contract"]["sha256"],
    )


def _request(manifest: dict[str, Any], *, partition: str = "validation") -> DataAccessRequest:
    lock = manifest["data"]["dataset_version_lock"]
    return DataAccessRequest(
        dataset_id=lock["dataset_id"],
        partition=partition,
        data_version=lock["sha256"],
    )


def _evaluate(_data: bytes, receipt: TrialReceipt) -> TrialEvaluation[TrialReceipt]:
    return TrialEvaluation(
        value=receipt,
        evidence=EvaluationEvidence(
            artifact_bytes=_ARTIFACT,
            observed_sharpe=0.10,
            observations=500,
            skew=0.0,
            kurtosis=3.0,
        ),
    )


def _broker(
    tmp_path: Path,
    manifest: dict[str, Any],
    *,
    trial_name: str = "trials.jsonl",
) -> FiveToolTrialBroker:
    return FiveToolTrialBroker._from_synthetic_manifest_for_tests(
        tmp_path / trial_name,
        manifest,
        registry_ledger_path=tmp_path / "registry.jsonl",
    )


def _canonical_runs(registry: Path) -> tuple[dict[str, object], ...]:
    return tuple(record.payload for record in RegistryLedger(registry).records_of(KIND_RUN))


def _data_hashes(registry: Path) -> dict[str, object]:
    runs = _canonical_runs(registry)
    assert len(runs) == 1
    hashes = runs[0]["data_hashes"]
    assert isinstance(hashes, dict)
    entry = hashes[_DATASET_ID]
    assert isinstance(entry, dict)
    return entry


# --------------------------------------------------------------------------------------
# The capability: a certified read completes and its provenance is recorded as evidence.
# --------------------------------------------------------------------------------------


def test_a_certified_read_records_real_provenance_not_a_constant(tmp_path: Path) -> None:
    """The outcome that had never been observed: ``certified_reader`` is finally True."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"
    reader = CertifiedDatasetReader(root)
    broker = _broker(tmp_path, manifest)

    receipt = broker.run(
        _definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate
    )

    evidence = _data_hashes(registry)
    assert evidence["certified_reader"] is True
    assert evidence["dataset_sha256"] == certification["dataset_sha256"] == receipt.data_version
    assert evidence["partition"] == "validation"
    assert (
        evidence["certification_manifest_sha256"]
        == hashlib.sha256((root / CERTIFICATION_MANIFEST_NAME).read_bytes()).hexdigest()
    )
    # Every file that exists is named with the digest of its own bytes — no summary stands
    # in for the file list, so a dropped file cannot hide inside an aggregate.
    assert evidence["files"] == {
        relative: hashlib.sha256(data).hexdigest()
        for relative, data in sorted(_DATASET_FILES.items())
    }
    # And the reader's own receipt agrees with what was registered.
    assert len(reader.receipts) == 1
    assert reader.receipts[0].file_digests == evidence["files"]
    assert reader.receipts[0].payload_sha256 == certification["dataset_sha256"]
    assert RegistryLedger(registry).verify()[0] is True


def test_the_certified_record_exists_before_the_reader_opens_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B.1's ordering survives certification: register-then-read, now with real evidence.

    The registry is inspected from inside the read, so the certified provenance is proven
    durable *before* any dataset byte is handed over — not written afterwards. The probe
    wraps the real ``__call__`` on the class, so the reader is still the exact certified
    type and the run is still certified.
    """

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"
    reader = CertifiedDatasetReader(root)
    seen_at_read_time: list[dict[str, object]] = []
    original = CertifiedDatasetReader.__call__

    def observing(self: CertifiedDatasetReader, request: DataAccessRequest) -> bytes:
        seen_at_read_time.extend(_canonical_runs(registry))
        return original(self, request)

    monkeypatch.setattr(CertifiedDatasetReader, "__call__", observing)
    _broker(tmp_path, manifest).run(
        _definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate
    )

    assert len(seen_at_read_time) == 1
    hashes = seen_at_read_time[0]["data_hashes"]
    assert isinstance(hashes, dict)
    assert hashes[_DATASET_ID]["certified_reader"] is True


def test_the_dataset_digest_is_derived_from_bytes_not_asserted(tmp_path: Path) -> None:
    """One changed byte is a different dataset, and the payload framing says why."""

    root, certification = _certified_dataset(tmp_path)
    expected = hashlib.sha256(
        certified_payload([(name, data) for name, data in _DATASET_FILES.items()])
    ).hexdigest()
    assert certification["dataset_sha256"] == expected

    other_root, other = _certified_dataset(
        tmp_path,
        name="one-byte-different",
        files={
            "development/AAA.csv": _DATASET_FILES["development/AAA.csv"],
            "validation/AAA.csv": _DATASET_FILES["validation/AAA.csv"][:-1] + b"1\n",
        },
    )
    assert other["dataset_sha256"] != certification["dataset_sha256"]
    # Renaming a file changes it too: the framing binds names, not only contents.
    renamed_root, renamed = _certified_dataset(
        tmp_path,
        name="renamed",
        files={
            "development/BBB.csv": _DATASET_FILES["development/AAA.csv"],
            "validation/AAA.csv": _DATASET_FILES["validation/AAA.csv"],
        },
    )
    assert renamed["dataset_sha256"] != certification["dataset_sha256"]
    assert {root, other_root, renamed_root} == {
        tmp_path / "certified",
        tmp_path / "one-byte-different",
        tmp_path / "renamed",
    }


# --------------------------------------------------------------------------------------
# Conjunct 1 — digest-locked. Each way the lock fails, in the rejecting direction.
# --------------------------------------------------------------------------------------


def test_an_absent_dataset_root_or_manifest_refuses_before_a_reader_exists(
    tmp_path: Path,
) -> None:
    with pytest.raises(CertificationManifestMissing, match=r"dataset root .* is absent"):
        CertifiedDatasetReader(tmp_path / "never-provisioned")

    uncertified = tmp_path / "uncertified"
    uncertified.mkdir()
    _write_dataset(uncertified, _DATASET_FILES)
    with pytest.raises(CertificationManifestMissing, match="no certification manifest at"):
        CertifiedDatasetReader(uncertified)
    assert not (uncertified / CERTIFICATION_MANIFEST_NAME).exists(), (
        "the refused reader wrote the certification it lacked"
    )
    # Non-vacuity: certifying the identical bytes makes the identical call succeed.
    _certify(uncertified)
    assert CertifiedDatasetReader(uncertified).manifest.dataset_id == _DATASET_ID


@pytest.mark.parametrize(
    ("shape", "mutate", "expected_message"),
    [
        (
            "unknown schema version",
            lambda manifest: manifest.__setitem__("schema_version", "something-else-v9"),
            "schema_version must be",
        ),
        (
            "an extra key nobody validates",
            lambda manifest: manifest.__setitem__("trust_me", True),
            r"keys do not match schema.*unknown=\['trust_me'\]",
        ),
        (
            "a dropped key",
            lambda manifest: manifest.pop("dataset_sha256"),
            r"keys do not match schema.*missing=\['dataset_sha256'\]",
        ),
        (
            "no accessible partitions at all",
            lambda manifest: manifest.__setitem__("accessible_partitions", []),
            "must name at least one accessible partition",
        ),
        (
            "holdout vocabulary declared accessible",
            lambda manifest: manifest.__setitem__(
                "accessible_partitions", ["development", "holdout"]
            ),
            "cannot be certified-accessible",
        ),
        (
            "an empty file list",
            lambda manifest: manifest.__setitem__("files", []),
            "files must be a non-empty list",
        ),
        (
            "a path escaping the dataset root",
            lambda manifest: manifest["files"][0].__setitem__("path", "../../etc/passwd"),
            "must not contain empty or traversal segments",
        ),
        (
            "an absolute path",
            lambda manifest: manifest["files"][0].__setitem__("path", "/etc/passwd"),
            "must be a relative POSIX path",
        ),
        (
            "a digest that is not a SHA-256",
            lambda manifest: manifest["files"][0].__setitem__("sha256", "deadbeef"),
            "must be a lowercase 64-character SHA-256 digest",
        ),
        (
            "a negative byte length",
            lambda manifest: manifest["files"][0].__setitem__("size_bytes", -1),
            "size_bytes must be a non-negative integer",
        ),
        (
            "a local-time certification timestamp",
            lambda manifest: manifest.__setitem__("certified_at_utc", "2026-08-09T00:00:00-04:00"),
            "must be UTC",
        ),
    ],
)
def test_a_manifest_that_does_not_describe_a_certifiable_dataset_is_refused(
    tmp_path: Path,
    shape: str,
    mutate: Callable[[dict[str, Any]], None],
    expected_message: str,
) -> None:
    """Each schema conjunct reverted alone, each with its own distinct message."""

    root, _ = _certified_dataset(tmp_path)
    intact = (root / CERTIFICATION_MANIFEST_NAME).read_bytes()
    _rewrite_certification(root, mutate)

    with pytest.raises(CertificationManifestInvalid, match=expected_message):
        CertifiedDatasetReader(root)

    # Non-vacuity: restore the manifest byte-for-byte and the same construction succeeds.
    (root / CERTIFICATION_MANIFEST_NAME).write_bytes(intact)
    assert CertifiedDatasetReader(root).manifest.dataset_id == _DATASET_ID, shape


def test_a_manifest_that_is_not_json_is_refused_rather_than_partially_parsed(
    tmp_path: Path,
) -> None:
    root, _ = _certified_dataset(tmp_path)
    intact = (root / CERTIFICATION_MANIFEST_NAME).read_bytes()
    (root / CERTIFICATION_MANIFEST_NAME).write_text("{not-json", encoding="utf-8")

    with pytest.raises(CertificationManifestInvalid, match="is not readable JSON"):
        CertifiedDatasetReader(root)

    (root / CERTIFICATION_MANIFEST_NAME).write_bytes(intact)
    assert CertifiedDatasetReader(root).manifest.files


@pytest.mark.parametrize(
    ("shape", "tamper", "expected_error", "expected_message"),
    [
        (
            "a file the manifest never digested",
            lambda root: (root / "development" / "SMUGGLED.csv").write_bytes(b"date,close\n"),
            UncertifiedFilePresent,
            "undigested file 'development/SMUGGLED.csv'",
        ),
        (
            "a symlink reaching outside the certified root",
            lambda root: (root / "validation" / "LINK.csv").symlink_to(root.parent / "outside.csv"),
            UncertifiedFilePresent,
            "symbolic link",
        ),
        (
            "a digested file deleted from disk",
            lambda root: (root / "validation" / "AAA.csv").unlink(),
            CertifiedFileMissing,
            r"certified file 'validation/AAA.csv' is absent",
        ),
        (
            "a digested file whose bytes changed",
            lambda root: (root / "validation" / "AAA.csv").write_bytes(b"date,close\n2020,999\n"),
            DatasetDigestMismatch,
            r"certified file 'validation/AAA.csv' digest mismatch",
        ),
    ],
)
def test_a_tampered_certified_dataset_refuses_the_read_and_still_costs_a_trial(
    tmp_path: Path,
    shape: str,
    tamper: Callable[[Path], None],
    expected_error: type[BaseException],
    expected_message: str,
) -> None:
    """Read-time locks fire after registration, which is the honest place for them.

    The reader was constructed and the attempt registered before the dataset was opened,
    so a dataset that turns out to be untrustworthy still burns a counted trial. That is
    register-then-read working as designed, not a leak.
    """

    root, certification = _certified_dataset(tmp_path)
    (tmp_path / "outside.csv").write_bytes(b"date,close\n1999,1\n")
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"
    reader = CertifiedDatasetReader(root)
    broker = _broker(tmp_path, manifest)
    tamper(root)

    with pytest.raises(expected_error, match=expected_message):
        broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate)

    assert reader.receipts == (), f"{shape}: a refused read still issued a receipt"
    assert registered_trial_count(registry, strategy_id=_STRATEGY_ID) == 1, shape
    terminals = RegistryLedger(broker.ledger_path).records_of("trial_terminal")
    assert [record.payload["outcome"] for record in terminals] == ["failed"], shape
    # The registered run says the read was meant to be certified; no completed trial
    # carries the claim, which is the direction that matters.
    assert _data_hashes(registry)["certified_reader"] is True


def test_a_declared_dataset_digest_that_disagrees_with_the_bytes_is_refused(
    tmp_path: Path,
) -> None:
    """The overall digest cannot be asserted: it is recomputed from what was read."""

    root, certification = _certified_dataset(tmp_path)
    honest = certification["dataset_sha256"]
    lie = hashlib.sha256(b"a digest nobody derived").hexdigest()
    _rewrite_certification(root, lambda manifest: manifest.__setitem__("dataset_sha256", lie))
    manifest = _synthetic_ready_manifest(dataset_sha256=lie)
    reader = CertifiedDatasetReader(root)
    broker = _broker(tmp_path, manifest)

    with pytest.raises(DatasetDigestMismatch, match="certified dataset digest mismatch"):
        broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate)

    # Non-vacuity: restore the derived digest and the identical campaign runs.
    _rewrite_certification(root, lambda payload: payload.__setitem__("dataset_sha256", honest))
    repaired = _synthetic_ready_manifest(dataset_sha256=honest)
    _broker(tmp_path, repaired, trial_name="repaired.jsonl").run(
        _definition(repaired),
        _request(repaired),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )


def test_a_declared_length_that_disagrees_with_the_bytes_is_refused(tmp_path: Path) -> None:
    """Length is bound independently, so a digest collision story still has to explain it."""

    root, _ = _certified_dataset(tmp_path)
    _rewrite_certification(root, lambda manifest: manifest["files"][0].__setitem__("size_bytes", 1))
    reader = CertifiedDatasetReader(root)

    with pytest.raises(DatasetDigestMismatch, match="length mismatch"):
        reader(_request(_synthetic_ready_manifest(dataset_sha256=reader.manifest.dataset_sha256)))
    assert reader.receipts == ()


def test_a_certified_dataset_that_is_not_the_campaign_dataset_is_refused(tmp_path: Path) -> None:
    """Two locks, two distinct refusals: the dataset identity and the dataset digest."""

    root, certification = _certified_dataset(tmp_path, dataset_id="some-other-dataset")
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)

    with pytest.raises(DatasetIdentityMismatch, match="is not the campaign dataset"):
        broker.run(
            _definition(manifest),
            _request(manifest),
            reader=CertifiedDatasetReader(root),
            evaluator=_evaluate,
        )
    assert not registry.exists(), "an identity-refused attempt was still registered"
    assert not broker.ledger_path.exists()

    # Same shape, other lock: the right dataset id under a digest the campaign never froze.
    right_id_root, right_id = _certified_dataset(tmp_path, name="right-id")
    stale = _synthetic_ready_manifest(
        dataset_sha256=hashlib.sha256(b"a dataset version nobody certified").hexdigest()
    )
    stale_broker = _broker(tmp_path, stale, trial_name="stale.jsonl")
    with pytest.raises(DatasetDigestMismatch, match="campaign dataset_version_lock"):
        stale_broker.run(
            _definition(stale),
            _request(stale),
            reader=CertifiedDatasetReader(right_id_root),
            evaluator=_evaluate,
        )
    assert not stale_broker.ledger_path.exists()

    # Non-vacuity: the campaign locked to this dataset's real digest runs the same request.
    repaired = _synthetic_ready_manifest(dataset_sha256=right_id["dataset_sha256"])
    _broker(tmp_path, repaired, trial_name="repaired.jsonl").run(
        _definition(repaired),
        _request(repaired),
        reader=CertifiedDatasetReader(right_id_root),
        evaluator=_evaluate,
    )
    assert _data_hashes(tmp_path / "registry.jsonl")["certified_reader"] is True


# --------------------------------------------------------------------------------------
# Conjunct 2 — proves what it touched. The post-read wall, reverted one clause at a time.
# --------------------------------------------------------------------------------------


def test_a_reader_that_stops_recording_what_it_read_cannot_complete_a_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revert the receipt: identical bytes, no proof, no completed trial."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    reader = CertifiedDatasetReader(root)
    payload = certified_payload(list(_DATASET_FILES.items()))
    assert hashlib.sha256(payload).hexdigest() == certification["dataset_sha256"]
    broker = _broker(tmp_path, manifest)

    monkeypatch.setattr(
        CertifiedDatasetReader,
        "__call__",
        lambda _self, _request: payload,
    )
    with pytest.raises(CertifiedProvenanceUnproven, match="did not record exactly one read"):
        broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate)

    terminals = RegistryLedger(broker.ledger_path).records_of("trial_terminal")
    assert [record.payload["outcome"] for record in terminals] == ["failed"]

    # Non-vacuity: restore the recording and the same bytes complete the same trial.
    monkeypatch.undo()
    value = _broker(tmp_path, manifest, trial_name="repaired.jsonl").run(
        _definition(manifest),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )
    assert value.data_version == certification["dataset_sha256"]


@pytest.mark.parametrize(
    ("shape", "doctor", "expected_message"),
    [
        (
            "a receipt for a different request",
            lambda receipt: dataclasses.replace(receipt, partition="development"),
            "bound to a different request",
        ),
        (
            "a receipt over other bytes",
            lambda receipt: dataclasses.replace(receipt, payload_sha256="0" * 64),
            "not the certified payload the reader recorded",
        ),
        (
            "a receipt claiming another dataset version",
            lambda receipt: dataclasses.replace(receipt, dataset_sha256="0" * 64),
            "disagrees with the campaign dataset lock",
        ),
        (
            "a receipt under another certification manifest",
            lambda receipt: dataclasses.replace(receipt, certification_manifest_sha256="0" * 64),
            "different certification manifest than it attested",
        ),
        (
            "a receipt over a smaller file set",
            lambda receipt: dataclasses.replace(receipt, files=receipt.files[:1]),
            "touched a different file set than it attested",
        ),
    ],
)
def test_every_clause_of_the_post_read_proof_is_load_bearing(
    tmp_path: Path,
    shape: str,
    doctor: Callable[[CertifiedReadReceipt], CertifiedReadReceipt],
    expected_message: str,
) -> None:
    """Guard the guard: each clause reverted alone raises its own distinct refusal."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    request = _request(manifest)
    reader = CertifiedDatasetReader(root)
    attested = reader.attest(request)
    data = reader(request)

    # The honest receipt passes — the control below is not vacuously true.
    _require_proven_certified_read(reader, request, data, attested, reads_before=0)

    reader._receipts[-1] = doctor(reader.receipts[0])
    with pytest.raises(CertifiedProvenanceUnproven, match=expected_message):
        _require_proven_certified_read(reader, request, data, attested, reads_before=0)
    assert shape


# --------------------------------------------------------------------------------------
# The both-directions property: identical bytes, weaker provenance, weaker record.
# --------------------------------------------------------------------------------------


def test_a_subclassed_reader_returns_the_same_bytes_and_earns_no_certified_claim(
    tmp_path: Path,
) -> None:
    """Only the exact certified type may claim certification; a subclass may override it."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"

    class _AlmostCertifiedReader(CertifiedDatasetReader):
        pass

    broker = _broker(tmp_path, manifest)
    broker.run(
        _definition(manifest),
        _request(manifest),
        reader=_AlmostCertifiedReader(root),
        evaluator=_evaluate,
    )

    # The trial completed on byte-identical data, and the record still says uncertified.
    assert _data_hashes(registry) == {
        "dataset_sha256": certification["dataset_sha256"],
        "partition": "validation",
        "certified_reader": False,
    }
    terminals = RegistryLedger(broker.ledger_path).records_of("trial_terminal")
    assert [record.payload["outcome"] for record in terminals] == ["completed"]

    # Non-vacuity: the exact type, same dataset, same request — now it is certified.
    second = _broker(tmp_path, manifest, trial_name="exact.jsonl")
    second.run(
        _definition(manifest, cell_id="5t-momentum-score-paired"),
        _request(manifest),
        reader=CertifiedDatasetReader(root),
        evaluator=_evaluate,
    )
    runs = _canonical_runs(registry)
    assert len(runs) == 2
    hashes = runs[1]["data_hashes"]
    assert isinstance(hashes, dict)
    assert hashes[_DATASET_ID]["certified_reader"] is True


def test_mixed_certified_and_uncertified_access_stays_uncertified_and_fails(
    tmp_path: Path,
) -> None:
    """A wrapper that adds one uncertified byte is not a certified read of anything."""

    root, certification = _certified_dataset(tmp_path)
    smuggled = tmp_path / "not-in-the-dataset.csv"
    smuggled.write_bytes(b"date,close\n2026-01-02,42\n")
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"
    certified = CertifiedDatasetReader(root)

    def mixed(request: DataAccessRequest) -> bytes:
        return certified(request) + smuggled.read_bytes()

    broker = _broker(tmp_path, manifest)
    with pytest.raises(DataVersionMismatch, match="do not match the campaign-authorized"):
        broker.run(_definition(manifest), _request(manifest), reader=mixed, evaluator=_evaluate)

    assert _data_hashes(registry)["certified_reader"] is False
    terminals = RegistryLedger(broker.ledger_path).records_of("trial_terminal")
    assert [record.payload["outcome"] for record in terminals] == ["failed"]
    # The certified reader did read, honestly, and says exactly what it read — the mixing
    # happened outside it, which is why the record it could not vouch for says False.
    assert len(certified.receipts) == 1
    assert certified.receipts[0].payload_sha256 == certification["dataset_sha256"]


def test_an_arbitrary_callback_is_still_recorded_as_uncertified(tmp_path: Path) -> None:
    """The B.1 default is unchanged: a callback that promises nothing records nothing."""

    root, certification = _certified_dataset(tmp_path)
    payload = certified_payload(list(_DATASET_FILES.items()))
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    registry = tmp_path / "registry.jsonl"

    _broker(tmp_path, manifest).run(
        _definition(manifest),
        _request(manifest),
        reader=lambda _request: payload,
        evaluator=_evaluate,
    )

    assert _data_hashes(registry) == {
        "dataset_sha256": certification["dataset_sha256"],
        "partition": "validation",
        "certified_reader": False,
    }
    assert not [path for path in root.rglob("*") if path.name.startswith("receipt")]


# --------------------------------------------------------------------------------------
# Conjunct 3 — composition. The older refusals still fire first, and one is added.
# --------------------------------------------------------------------------------------


def test_a_declared_holdout_is_refused_before_the_certified_reader_is_consulted(
    tmp_path: Path,
) -> None:
    """Track A's wall stands in front of the new capability, not behind it."""

    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    # The Track A shape: a holdout carved from the campaign's own dataset id, which the
    # identity check cannot distinguish, so the holdout refusal is the sole guard.
    manifest["data"]["declared_holdouts"][0]["dataset_id"] = _DATASET_ID
    registry = tmp_path / "registry.jsonl"
    reader = CertifiedDatasetReader(root)
    broker = _broker(tmp_path, manifest)

    with pytest.raises(HoldoutAccessRefused, match="no unlock capability"):
        broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate)

    assert reader.receipts == ()
    assert not registry.exists(), "a holdout-refused attempt reached the registry"
    assert not broker.ledger_path.exists()
    # And the reader refuses holdout vocabulary on its own, without the broker in front.
    with pytest.raises(PartitionNotCertified, match="holdout vocabulary"):
        reader(_request(manifest, partition="holdout"))
    assert reader.receipts == ()


def test_a_partition_outside_the_certification_manifest_is_refused_as_defense_in_depth(
    tmp_path: Path,
) -> None:
    """The campaign allows it, the certification does not, and the reader says so.

    This is the only shape where the reader's partition check is the sole remaining
    guard: the campaign manifest lists ``development`` as accessible, so
    ``_validate_identity`` passes and a distinct refusal has to come from the reader.
    """

    root, certification = _certified_dataset(tmp_path, partitions=("validation",))
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])
    assert "development" in manifest["data"]["accessible_partitions"]
    registry = tmp_path / "registry.jsonl"
    reader = CertifiedDatasetReader(root)
    broker = _broker(tmp_path, manifest)

    with pytest.raises(
        (PartitionNotCertified, CampaignIdentityMismatch),
        match=r"is not certified-accessible|data request identity disagrees",
    ):
        broker.run(
            _definition(manifest),
            _request(manifest, partition="development"),
            reader=reader,
            evaluator=_evaluate,
        )

    assert reader.receipts == ()
    assert not registry.exists()
    assert not broker.ledger_path.exists()
    # Non-vacuity: a certified partition runs the identical request through the same reader.
    broker.run(
        _definition(manifest),
        _request(manifest, partition="validation"),
        reader=reader,
        evaluator=_evaluate,
    )
    assert _data_hashes(registry)["certified_reader"] is True

    # And the campaign's own check still fires first for a partition it never declared,
    # so the two guards are independent rather than one wearing the other's message.
    with pytest.raises(CampaignIdentityMismatch, match="not campaign-accessible"):
        broker.run(
            _definition(manifest),
            _request(manifest, partition="exploration"),
            reader=reader,
            evaluator=_evaluate,
        )


def test_the_reader_holdout_vocabulary_matches_the_broker_definition() -> None:
    """The duplicated constant cannot drift into a hole between the two guards."""

    from chronos.research.five_tool import certified_reader as module

    assert module._HOLDOUT_PARTITION_VOCABULARY == _DEFAULT_HOLDOUT_PARTITIONS


# --------------------------------------------------------------------------------------
# Conjunct 4 — recognized, not unblocking. The owner's evidence remains, and still refuses.
# --------------------------------------------------------------------------------------


def test_the_public_refusal_still_refuses_after_the_reader_landed(tmp_path: Path) -> None:
    """A reader that can prove a read is not a dataset, an artifact store, or a limit.

    Renamed and re-pinned 2026-08-09 (Track B.3): this test was
    ``test_the_public_refusal_now_names_exactly_two_capabilities`` and asserted the exact
    pair ``("replay artifacts", "owner evidence")``.  The replay-artifact capability then
    landed and left the list — the forced edit is the list shrinking by one name, not the
    property changing.  What this file is actually here to prove is unchanged and asserted
    below: possessing a certified reader does not unblock anything, and the refusal never
    names the certified reader.
    """

    assert MISSING_CERTIFIED_RESEARCH_CAPABILITIES == ("owner evidence",)
    root, certification = _certified_dataset(tmp_path)
    manifest = _synthetic_ready_manifest(dataset_sha256=certification["dataset_sha256"])

    with pytest.raises(CampaignExecutionBlocked) as blocked:
        validate_campaign_manifest(manifest)
    message = str(blocked.value)
    assert "EXECUTION_READY is not implemented" in message
    assert "the missing owner evidence capability" in message
    assert "certified" not in message.casefold()

    # Possessing a certified reader does not move the checked-in manifest either.
    committed = _committed_manifest()
    assert committed["execution_state"] == "blocked_until_identity_locks_resolve"
    assert committed["data"]["dataset_version_lock"]["sha256"] is None
    blocked_broker = FiveToolTrialBroker.from_campaign_manifest(
        tmp_path / "blocked.jsonl", committed
    )
    with pytest.raises(CampaignExecutionBlocked, match="no owner evidence capability exists"):
        blocked_broker.run(
            _definition(manifest),
            _request(manifest),
            reader=CertifiedDatasetReader(root),
            evaluator=_evaluate,
        )
    assert not (tmp_path / "blocked.jsonl").exists()


def test_the_repository_certifies_no_real_dataset(tmp_path: Path) -> None:
    """The capability landed; no dataset was certified, which is a Track C decision."""

    assert not list(_ROOT.joinpath("research").rglob(CERTIFICATION_MANIFEST_NAME))
    raw = _ROOT / "research/data/raw"
    assert (raw / "MANIFEST.json").is_file(), "the existing provenance manifest moved"
    with pytest.raises(CertificationManifestMissing, match="no certification manifest at"):
        CertifiedDatasetReader(raw)
    assert not (raw / CERTIFICATION_MANIFEST_NAME).exists()
    assert tmp_path.is_dir()


def test_the_reader_consumes_the_per_file_digest_discipline_the_corpus_already_uses(
    tmp_path: Path,
) -> None:
    """The certification schema is the raw corpus's own shape: bytes bound to a SHA-256.

    Built here from a *copy* of two rows of that discipline, not from the corpus itself —
    ``research/`` is left byte-identical and nothing in it is certified.
    """

    corpus = json.loads((_ROOT / "research/data/raw/MANIFEST.json").read_text(encoding="utf-8"))
    assert all("output_sha256" in entry for entry in corpus["files"].values())

    root = tmp_path / "shaped-like-the-corpus"
    root.mkdir()
    files = {
        "SPY.csv": b"date,close\n2010-01-04,113.33\n",
        "QQQ.csv": b"date,close\n2010-01-04,46.30\n",
    }
    _write_dataset(root, files)
    certification = _certify(root, partitions=("development",))
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    assert {entry["path"]: entry["sha256"] for entry in certification["files"]} == expected
    assert certification["schema_version"] == CERTIFICATION_SCHEMA_VERSION
    manifest = load_certification_manifest(root)
    assert manifest.file_digests == expected
    assert manifest.accessible_partitions == frozenset({"development"})


# --------------------------------------------------------------------------------------
# Research plane only: reading certified data grants no unmasking capability.
# --------------------------------------------------------------------------------------


def test_a_certified_read_never_loads_the_holdout_unlock_capability(tmp_path: Path) -> None:
    """ADR-0013 §7 in the behavioural direction, for the reader as well as the counter."""

    root, certification = _certified_dataset(tmp_path)
    probe = (
        "import sys; from pathlib import Path; "
        "from chronos.research.five_tool.certified_reader import CertifiedDatasetReader; "
        "from chronos.research.five_tool_trials import DataAccessRequest; "
        f"reader = CertifiedDatasetReader(Path({str(root)!r})); "
        "payload = reader(DataAccessRequest(dataset_id="
        f"{_DATASET_ID!r}, partition='validation', "
        f"data_version={certification['dataset_sha256']!r})); "
        "assert payload; "
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

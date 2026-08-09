"""The Five-Tool holdout refusal must FIRE, not merely exist.

Merge-time adversarial review of ``codex/five-tool-confluence-v36`` found that deleting
the whole body of ``FiveToolTrialBroker._refuse_holdout`` left the entire suite green
(2735 passed).  The control was implemented and documented — the campaign manifest
declares ``"ordinary_research_access": "forbidden"`` for the 2026-Q4 window — but no test
had ever observed it refuse.  That is the R-25/R-26/R-27 defect class: a protection
control that passes review while structurally unobserved.

Why it was invisible: ``run`` calls ``_validate_identity`` *before* ``_refuse_holdout``,
and for every request the existing tests build, the identity check rejects first (an
off-manifest ``dataset_id`` or a partition outside ``accessible_partitions``).  The
holdout refusal is the SOLE remaining guard only for manifests that are structurally
valid yet declare a holdout the identity check cannot distinguish:

* a declared holdout carved from the campaign's own ``dataset_version_lock.dataset_id``;
* a declared holdout on a custom partition name that is also listed accessible —
  manifest validation only forbids ``accessible_partitions`` intersecting the *default*
  holdout vocabulary, not a campaign-specific partition label.

Both shapes are exercised below, in the rejecting direction, asserting the outcome that
had never been observed: ``HoldoutAccessRefused`` raised BEFORE the reader is called and
BEFORE any ledger record exists.  The final test pins the guard's non-vacuity — the same
broker and the same request identity succeed once the holdout declaration is removed — so
a refusal caused by something other than the holdout identity cannot pass as this control
firing.

This proves a refusal path only.  It is not evidence that any Five-Tool hypothesis was
tested, and no hypothesis test is run here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chronos.research.five_tool_trials import (
    EXECUTION_READY,
    DataAccessRequest,
    EvaluationEvidence,
    FiveToolTrialBroker,
    HoldoutAccessRefused,
    TrialDefinition,
    TrialEvaluation,
    TrialReceipt,
)

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_CRITERIA_PATH = _ROOT / "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md"
_DATA = b"content-addressed-certified-five-tool-dataset-v1\n"
_CODE_COMMIT = "1" * 40
_REFERENCE_CELL = "5t-full-default-reference"


def _committed_manifest() -> dict[str, Any]:
    loaded = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _synthetic_ready_manifest() -> dict[str, Any]:
    """Resolve the committed manifest's locks for the private lifecycle harness only.

    This carries no readiness authority: the public API refuses ``EXECUTION_READY``
    outright (``test_public_ready_manifest_cannot_authorize_reader_or_ledger`` in
    tests/unit/test_five_tool_trials.py).  The synthetic seam exists so refusal paths
    downstream of the campaign block can be observed at all.
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
        "sha256": hashlib.sha256(_DATA).hexdigest(),
        "status": "resolved",
        "required_before_execution": True,
    }
    return manifest


def _definition(manifest: dict[str, Any]) -> TrialDefinition:
    cells = [cell for cell in manifest["campaign_cells"] if cell["cell_id"] == _REFERENCE_CELL]
    assert len(cells) == 1
    cell = cells[0]
    return TrialDefinition(
        campaign_id=manifest["campaign_id"],
        cell_id=_REFERENCE_CELL,
        hypothesis_id=cell["hypothesis_id"],
        strategy_id=manifest["strategy"]["strategy_id"],
        semantic_config=copy.deepcopy(cell["config_overlay"]),
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
            artifact_bytes=b'{"metric":"raw-score-evidence"}',
            observed_sharpe=0.10,
            observations=500,
            skew=0.0,
            kurtosis=3.0,
        ),
    )


def _share_dataset_id_with_the_campaign(manifest: dict[str, Any]) -> None:
    manifest["data"]["declared_holdouts"][0]["dataset_id"] = manifest["data"][
        "dataset_version_lock"
    ]["dataset_id"]


def _declare_a_custom_holdout_partition_as_accessible(manifest: dict[str, Any]) -> None:
    manifest["data"]["declared_holdouts"][0]["partition"] = "q4_2026"
    manifest["data"]["accessible_partitions"] = ["development", "validation", "q4_2026"]


def test_the_committed_manifest_declares_its_holdout_forbidden_and_unopened() -> None:
    """The refusal below is only meaningful if the manifest still declares a holdout."""

    declared = _committed_manifest()["data"]["declared_holdouts"]
    assert len(declared) == 1
    window = declared[0]
    assert window["dataset_id"] == "five-tool-certified-holdout-2026q4"
    assert window["partition"] == "holdout"
    assert window["status"] == "future_uncollected_and_unopened"
    assert window["ordinary_research_access"] == "forbidden"


@pytest.mark.parametrize(
    ("shape", "mutate", "partition", "expected_message"),
    [
        (
            "holdout carved from the campaign's own dataset id",
            _share_dataset_id_with_the_campaign,
            "validation",
            "dataset 'five-tool-certified-daily-v1' is a declared holdout",
        ),
        (
            "custom holdout partition also listed accessible",
            _declare_a_custom_holdout_partition_as_accessible,
            "q4_2026",
            "partition 'q4_2026' is a declared holdout",
        ),
    ],
)
def test_a_declared_holdout_is_refused_before_the_reader_and_before_the_ledger(
    tmp_path: Path,
    shape: str,
    mutate: Callable[[dict[str, Any]], None],
    partition: str,
    expected_message: str,
) -> None:
    manifest = _synthetic_ready_manifest()
    mutate(manifest)
    ledger_path = tmp_path / f"{partition}.jsonl"
    broker = FiveToolTrialBroker._from_synthetic_manifest_for_tests(ledger_path, manifest)
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(HoldoutAccessRefused) as refusal:
        broker.run(
            _definition(manifest),
            _request(manifest, partition=partition),
            reader=poison,
            evaluator=_evaluate,
        )

    assert expected_message in str(refusal.value), shape
    assert "no unlock capability" in str(refusal.value)
    # Fail-closed direction: the bytes were never handed over, and the attempt never
    # became a durable trial, so a refused holdout cannot appear in any count.
    assert opened is False, f"{shape}: reader ran despite the holdout refusal"
    assert not ledger_path.exists(), f"{shape}: refused holdout still wrote a ledger record"


def test_the_refusal_is_caused_by_the_holdout_declaration_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Guard the guard: the identical request succeeds once the holdout is not declared.

    Without this, an unrelated failure on the same request could masquerade as the
    holdout control firing.
    """

    manifest = _synthetic_ready_manifest()
    _share_dataset_id_with_the_campaign(manifest)
    control = _synthetic_ready_manifest()
    assert (
        control["data"]["declared_holdouts"][0]["dataset_id"]
        != control["data"]["dataset_version_lock"]["dataset_id"]
    )

    opened = False

    def reader(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    refused_path = tmp_path / "refused.jsonl"
    with pytest.raises(HoldoutAccessRefused):
        FiveToolTrialBroker._from_synthetic_manifest_for_tests(refused_path, manifest).run(
            _definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate
        )
    assert opened is False

    allowed_path = tmp_path / "allowed.jsonl"
    receipt = FiveToolTrialBroker._from_synthetic_manifest_for_tests(allowed_path, control).run(
        _definition(control), _request(control), reader=reader, evaluator=_evaluate
    )
    assert opened is True
    assert receipt.campaign_id == control["campaign_id"]
    assert allowed_path.exists()

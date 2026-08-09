"""Five-Tool trials must be COUNTED by the canonical ADR-0013 registry, or refused.

ADR-0013 §5 derives the multiple-testing N from *registered* runs, and its §11 discloses
the residual this file closes: "completeness of the **trial count** (it is derived from
*registered* runs; auto-registration of the runner is a follow-on)". A registry that only
counts the runs someone remembered to register is multiplicity laundering with extra
steps — the ledger says N=3 while the world did 40.

So every Five-Tool attempt now writes an ``experiment_run`` record into the hash-chained
registry **before** the durable local start, which is itself before any read. The tests
below drive that path end-to-end and assert the outcomes that had never been observed:

* the canonical record exists *while the reader is running*, not after it returns;
* an attempt that dies after opening the data is still counted;
* an absent, unwired, unreadable, chain-broken, or truncated registry refuses the trial
  with a distinct reason and the reader is never called;
* a campaign whose attempts were not registered cannot be sealed.

Every refusal is pinned in the rejecting direction and then repaired, so a refusal caused
by something other than the registry cannot pass as this control firing (the
guard-the-guard pattern of ``test_five_tool_holdout_refusal_exercised.py``).

This proves counting and refusal paths only. No hypothesis is tested here, the campaign
manifest stays blocked, and the repository's own ``research/registry/registry.jsonl``
still ships empty — every ledger below lives in a pytest temporary directory.
"""

from __future__ import annotations

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
from chronos.registry.runs import trial_count
from chronos.research.five_tool_trials import (
    EXECUTION_READY,
    MISSING_CERTIFIED_RESEARCH_CAPABILITIES,
    CampaignExecutionBlocked,
    CampaignIdentityMismatch,
    CanonicalRegistryUnavailable,
    DataAccessRequest,
    EvaluationEvidence,
    FiveToolTrialBroker,
    FiveToolTrialError,
    ReviewedVarianceEvidence,
    TrialDefinition,
    TrialEvaluation,
    TrialReceipt,
    campaign_manifest_digest,
    registered_trial_count,
    validate_campaign_manifest,
)

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "research/five_tool_v3_6_campaign_manifest.json"
_CRITERIA_PATH = _ROOT / "docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md"
_FIVE_TOOL_PACKAGE = _ROOT / "src/chronos/research/five_tool"
_DATA = b"content-addressed-certified-five-tool-dataset-v1\n"
_CODE_COMMIT = "1" * 40
_STRATEGY_ID = "five_tool_confluence_v3_6"
_REFERENCE_CELL = "5t-full-default-reference"
_ARTIFACT = b'{"metric":"raw-score-evidence-that-is-never-persisted"}'
_VARIANCE = ReviewedVarianceEvidence(0.04, "reviewed-sample-variance-v1", "f" * 64)


def _committed_manifest() -> dict[str, Any]:
    loaded = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _synthetic_ready_manifest() -> dict[str, Any]:
    """Resolve the committed manifest's locks for the private lifecycle harness only.

    This carries no readiness authority: the public API refuses ``EXECUTION_READY``
    outright (``test_the_public_ready_refusal_names_each_remaining_capability`` below).
    The synthetic seam exists so the registry path can be observed at all.
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


def _definition(manifest: dict[str, Any], *, cell_id: str = _REFERENCE_CELL) -> TrialDefinition:
    cells = [cell for cell in manifest["campaign_cells"] if cell["cell_id"] == cell_id]
    assert len(cells) == 1
    return TrialDefinition(
        campaign_id=manifest["campaign_id"],
        cell_id=cell_id,
        hypothesis_id=cells[0]["hypothesis_id"],
        strategy_id=manifest["strategy"]["strategy_id"],
        semantic_config=copy.deepcopy(cells[0]["config_overlay"]),
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
    registry: Path | None = None,
) -> FiveToolTrialBroker:
    return FiveToolTrialBroker._from_synthetic_manifest_for_tests(
        tmp_path / trial_name,
        manifest,
        registry_ledger_path=registry if registry is not None else tmp_path / "registry.jsonl",
    )


def _canonical_runs(registry: Path) -> tuple[dict[str, object], ...]:
    return tuple(record.payload for record in RegistryLedger(registry).records_of(KIND_RUN))


# --------------------------------------------------------------------------------------
# The capability itself: registration lands, precedes the read, and survives verification.
# --------------------------------------------------------------------------------------


def test_the_canonical_record_exists_before_the_reader_sees_a_byte(tmp_path: Path) -> None:
    """Register-then-read. The reader inspects the registry from inside the read."""

    manifest = _synthetic_ready_manifest()
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)
    seen_at_read_time: list[dict[str, object]] = []

    def reader(_request: DataAccessRequest) -> bytes:
        seen_at_read_time.extend(_canonical_runs(registry))
        return _DATA

    receipt = broker.run(
        _definition(manifest), _request(manifest), reader=reader, evaluator=_evaluate
    )

    # The record was already durable when the bytes were handed over — not written after.
    assert len(seen_at_read_time) == 1
    payload = seen_at_read_time[0]
    assert payload["experiment_id"] == f"{receipt.trial_id}:{receipt.attempt_id}"
    assert payload["strategy_id"] == _STRATEGY_ID
    assert payload["stage"] == "validation"
    assert payload["code_commit"] == _CODE_COMMIT
    assert payload["criteria_ref"] == manifest["criteria_lock"]["sha256"]
    assert payload["config_hash"] == receipt.semantic_config_fingerprint
    assert payload["touched_data"] is True
    assert payload["data_hashes"] == {
        receipt.dataset_id: {
            "dataset_sha256": receipt.data_version,
            "partition": receipt.partition,
            "certified_reader": False,
        }
    }
    # And it survives a chain + head-anchor verification, which is what makes it evidence.
    ok, detail = RegistryLedger(registry).verify()
    assert ok, detail
    assert "1 records" in detail


def test_the_trial_count_is_derived_from_the_registry_not_self_reported(tmp_path: Path) -> None:
    manifest = _synthetic_ready_manifest()
    registry = tmp_path / "registry.jsonl"
    cells = sorted(cell["cell_id"] for cell in manifest["campaign_cells"])
    broker = _broker(tmp_path, manifest)
    for cell_id in cells:
        broker.run(
            _definition(manifest, cell_id=cell_id),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )

    assert registered_trial_count(registry) == len(cells)
    assert registered_trial_count(registry, strategy_id=_STRATEGY_ID) == len(cells)
    assert registered_trial_count(registry, strategy_id="some_other_strategy") == 0
    # The same number the canonical ADR-0013 derivation reports, from the same records.
    assert trial_count(RegistryLedger(registry), strategy_id=_STRATEGY_ID) == len(cells)

    # A second campaign on its own trial ledger still accumulates into the one registry:
    # multiplicity is a property of the registry, not of any caller-selected path.
    second = copy.deepcopy(manifest)
    second["campaign_id"] = "five-tool-v3.6-preregistered-002"
    _broker(tmp_path, second, trial_name="second.jsonl").run(
        _definition(second),
        _request(second),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )
    assert registered_trial_count(registry, strategy_id=_STRATEGY_ID) == len(cells) + 1


@pytest.mark.parametrize(
    ("shape", "reader", "evaluator", "expected_error"),
    [
        (
            "the reader itself fails after opening the data",
            lambda _request: (_ for _ in ()).throw(TimeoutError("dataset went away mid-read")),
            _evaluate,
            TimeoutError,
        ),
        (
            "the evaluator fails on data the reader already handed over",
            lambda _request: _DATA,
            lambda _data, _receipt: (_ for _ in ()).throw(ArithmeticError("scoring blew up")),
            ArithmeticError,
        ),
        (
            "the reader returns bytes the campaign never authorized",
            lambda _request: b"some other dataset entirely",
            _evaluate,
            FiveToolTrialError,
        ),
    ],
)
def test_an_aborted_attempt_is_still_a_counted_trial(
    tmp_path: Path,
    shape: str,
    reader: Callable[[DataAccessRequest], bytes],
    evaluator: Callable[[bytes, TrialReceipt], TrialEvaluation[TrialReceipt]],
    expected_error: type[BaseException],
) -> None:
    """Register-then-read means a failed attempt cannot vanish from the multiplicity."""

    manifest = _synthetic_ready_manifest()
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)

    with pytest.raises(expected_error):
        broker.run(_definition(manifest), _request(manifest), reader=reader, evaluator=evaluator)

    assert registered_trial_count(registry, strategy_id=_STRATEGY_ID) == 1, shape
    assert RegistryLedger(registry).verify()[0] is True


# --------------------------------------------------------------------------------------
# No registry, no trial. Each conjunct in the rejecting direction, then repaired.
# --------------------------------------------------------------------------------------


def test_an_unwired_canonical_registry_refuses_the_trial(tmp_path: Path) -> None:
    """Revert the wiring: a broker that cannot count may not read."""

    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)
    broker._registry_ledger_path = None  # simulate the capability never having been wired
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(
        CanonicalRegistryUnavailable, match="no canonical ADR-0013 registry is wired"
    ):
        broker.run(_definition(manifest), _request(manifest), reader=poison, evaluator=_evaluate)

    assert opened is False
    assert not broker.ledger_path.exists()
    # Non-vacuity: the identical request succeeds once a registry is wired again.
    broker._registry_ledger_path = tmp_path / "registry.jsonl"
    broker.run(_definition(manifest), _request(manifest), reader=poison, evaluator=_evaluate)
    assert opened is True


def test_an_absent_registry_root_refuses_the_trial_and_is_not_created(tmp_path: Path) -> None:
    """A research run may not conjure the registry it is supposed to be judged by."""

    manifest = _synthetic_ready_manifest()
    root = tmp_path / "never-provisioned"
    broker = _broker(tmp_path, manifest, registry=root / "registry.jsonl")
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CanonicalRegistryUnavailable, match=r"registry root .* is absent"):
        broker.run(_definition(manifest), _request(manifest), reader=poison, evaluator=_evaluate)

    assert opened is False
    assert not root.exists(), "the refused run provisioned the registry root it lacked"
    assert not broker.ledger_path.exists()
    # Non-vacuity: provisioning the root — a deliberate act — lets the same request run.
    root.mkdir()
    broker.run(_definition(manifest), _request(manifest), reader=poison, evaluator=_evaluate)
    assert opened is True
    assert registered_trial_count(root / "registry.jsonl") == 1


def _make_unreadable(registry: Path) -> None:
    """Corrupt the head record so the ledger cannot even be opened to append past it."""

    registry.write_text(registry.read_text(encoding="utf-8") + "{not-json\n", encoding="utf-8")


def _break_the_chain(registry: Path) -> None:
    """Edit a record in place: still parseable, no longer hashing to its own digest."""

    lines = registry.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["payload"]["strategy_id"] = "a_strategy_nobody_registered"
    lines[-1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _truncate_the_tail(registry: Path) -> None:
    """Delete the trailing record — a valid prefix is a valid chain (ADR-0013 F1)."""

    lines = registry.read_text(encoding="utf-8").splitlines()
    registry.write_text("".join(f"{line}\n" for line in lines[:-1]), encoding="utf-8")


@pytest.mark.parametrize(
    ("shape", "tamper", "expected_message"),
    [
        ("unreadable head record", _make_unreadable, "is unreadable"),
        ("in-place edit", _break_the_chain, "failed verification: line 1: hash mismatch"),
        ("tail truncation", _truncate_the_tail, "tail truncation/rollback"),
    ],
)
def test_a_registry_that_cannot_be_verified_refuses_the_next_trial(
    tmp_path: Path,
    shape: str,
    tamper: Callable[[Path], None],
    expected_message: str,
) -> None:
    """Verify before trust, the way the holdout guardian does — then fail closed."""

    manifest = _synthetic_ready_manifest()
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)
    broker.run(
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )
    intact = registry.read_bytes()
    trial_records_before = len(RegistryLedger(broker.ledger_path).records())
    tamper(registry)
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CanonicalRegistryUnavailable, match=expected_message):
        broker.run(
            _definition(manifest, cell_id="5t-trend-only"),
            _request(manifest),
            reader=poison,
            evaluator=_evaluate,
        )

    assert opened is False, f"{shape}: the reader ran against an unverifiable registry"
    assert len(RegistryLedger(broker.ledger_path).records()) == trial_records_before, (
        f"{shape}: the refused attempt still became a durable trial"
    )
    # Non-vacuity: restore the registry byte-for-byte and the same attempt succeeds.
    registry.write_bytes(intact)
    broker.run(
        _definition(manifest, cell_id="5t-trend-only"),
        _request(manifest),
        reader=poison,
        evaluator=_evaluate,
    )
    assert opened is True
    assert registered_trial_count(registry, strategy_id=_STRATEGY_ID) == 2


def test_an_unclassifiable_partition_is_refused_rather_than_registered_as_a_guess(
    tmp_path: Path,
) -> None:
    """A partition with no ADR-0013 stage cannot be recorded, so it cannot be run."""

    manifest = _synthetic_ready_manifest()
    manifest["data"]["accessible_partitions"] = ["development", "validation", "exploration"]
    registry = tmp_path / "registry.jsonl"
    broker = _broker(tmp_path, manifest)
    opened = False

    def poison(_request: DataAccessRequest) -> bytes:
        nonlocal opened
        opened = True
        return _DATA

    with pytest.raises(CampaignIdentityMismatch, match="no canonical ADR-0013 research stage"):
        broker.run(
            _definition(manifest),
            _request(manifest, partition="exploration"),
            reader=poison,
            evaluator=_evaluate,
        )

    assert opened is False
    assert not registry.exists()
    assert not broker.ledger_path.exists()
    # Non-vacuity: a partition the registry can classify runs the same request.
    broker.run(_definition(manifest), _request(manifest), reader=poison, evaluator=_evaluate)
    assert opened is True
    assert _canonical_runs(registry)[0]["stage"] == "validation"


def test_a_campaign_whose_attempts_were_not_registered_cannot_seal(tmp_path: Path) -> None:
    """Cross-ledger completeness: the derived N may never be short of the real attempts."""

    manifest = _synthetic_ready_manifest()
    counted = tmp_path / "registry.jsonl"
    uncounted = tmp_path / "empty"
    uncounted.mkdir()
    broker = _broker(tmp_path, manifest)
    for cell_id in sorted(cell["cell_id"] for cell in manifest["campaign_cells"]):
        broker.run(
            _definition(manifest, cell_id=cell_id),
            _request(manifest),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )

    broker._registry_ledger_path = uncounted / "registry.jsonl"
    with pytest.raises(FiveToolTrialError, match="registry under-counts this campaign"):
        broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert not RegistryLedger(broker.ledger_path).records_of("campaign_sealed")

    # Non-vacuity: the registry that actually counted these attempts seals them.
    broker._registry_ledger_path = counted
    rows = broker.seal_ledger_local_score_inputs(_VARIANCE)
    assert len(rows) == len(manifest["campaign_cells"])


# --------------------------------------------------------------------------------------
# The manifest stays blocked, now naming exactly the two capabilities that are missing.
# --------------------------------------------------------------------------------------


def test_the_canonical_registry_capability_is_no_longer_named_as_missing() -> None:
    """Updated 2026-08-09 (Track B.2): the certified reader left this list too."""

    named = " | ".join(MISSING_CERTIFIED_RESEARCH_CAPABILITIES).casefold()
    assert "registry" not in named
    assert "adr-0013" not in named
    assert "certified" not in named
    assert MISSING_CERTIFIED_RESEARCH_CAPABILITIES == (
        "replay artifacts",
        "owner evidence",
    )


@pytest.mark.parametrize("capability", list(MISSING_CERTIFIED_RESEARCH_CAPABILITIES))
def test_the_public_ready_refusal_names_each_remaining_capability(
    tmp_path: Path,
    capability: str,
) -> None:
    """Every public entry point still refuses, and says which capability is missing."""

    manifest = _synthetic_ready_manifest()
    path = tmp_path / "public-ready.jsonl"

    for call in (
        lambda: validate_campaign_manifest(manifest),
        lambda: campaign_manifest_digest(manifest),
        lambda: FiveToolTrialBroker.from_campaign_manifest(path, manifest),
    ):
        with pytest.raises(CampaignExecutionBlocked) as blocked:
            call()
        message = str(blocked.value)
        assert "EXECUTION_READY is not implemented" in message
        assert capability in message
        assert "ADR-0013" not in message
    assert not path.exists()


def test_the_blocked_broker_reason_names_the_same_remaining_capabilities(tmp_path: Path) -> None:
    broker = FiveToolTrialBroker.from_campaign_manifest(
        tmp_path / "blocked.jsonl", _committed_manifest()
    )
    with pytest.raises(CampaignExecutionBlocked) as blocked:
        broker.run(
            _definition(_synthetic_ready_manifest()),
            _request(_synthetic_ready_manifest()),
            reader=lambda _: _DATA,
            evaluator=_evaluate,
        )
    message = str(blocked.value)
    assert "blocked_until_identity_locks_resolve" in message
    for capability in MISSING_CERTIFIED_RESEARCH_CAPABILITIES:
        assert capability in message
    assert "registry" not in message.casefold()


def test_the_refusal_message_is_built_from_the_capability_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard the guard: a hardcoded sentence would not track what is actually missing."""

    monkeypatch.setattr(
        "chronos.research.five_tool_trials.MISSING_CERTIFIED_RESEARCH_CAPABILITIES",
        ("a capability nobody has built",),
    )
    with pytest.raises(CampaignExecutionBlocked, match="a capability nobody has built"):
        validate_campaign_manifest(_synthetic_ready_manifest())


def test_the_certified_reader_capability_now_exists() -> None:
    """Replaces the former absence proof (Track B.2, 2026-08-09).

    This file's original ``test_no_certified_reader_capability_exists`` asserted that no
    reader module existed, which is why "certified reader" was named as missing above.
    That capability landed, so the absence proof is replaced by the capability check
    rather than deleted: the module must exist, be importable, and be the thing the trial
    broker consults.  Its behaviour — every refusal conjunct and the both-directions
    provenance property — is exercised in
    ``tests/safety/test_five_tool_certified_reader_exercised.py``.
    """

    assert importlib.util.find_spec("chronos.research.five_tool.certified_reader") is not None
    assert [path.name for path in _FIVE_TOOL_PACKAGE.glob("*.py") if "certified" in path.name] == [
        "certified_reader.py"
    ]
    trials_source = (_ROOT / "src/chronos/research/five_tool_trials.py").read_text(encoding="utf-8")
    assert "from chronos.research.five_tool.certified_reader import" in trials_source


def test_no_replay_artifact_capability_exists(tmp_path: Path) -> None:
    """Independent proof of conjunct one: the evidence bytes are digested, never kept.

    The ledger records ``evidence_artifact_sha256``; the artifact itself is not persisted
    anywhere, so a completed trial cannot be replayed from what the ledger holds.
    """

    manifest = _synthetic_ready_manifest()
    broker = _broker(tmp_path, manifest)
    broker.run(
        _definition(manifest),
        _request(manifest),
        reader=lambda _: _DATA,
        evaluator=_evaluate,
    )

    written = b"".join(path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file())
    assert hashlib.sha256(_ARTIFACT).hexdigest().encode() in written
    assert _ARTIFACT not in written, (
        "an artifact store appeared; update the missing-capability list"
    )
    assert _DATA not in written


def test_the_owner_evidence_the_campaign_needs_is_still_unfrozen() -> None:
    """Independent proof of conjunct two: only the owner can supply these."""

    manifest = _committed_manifest()
    assert manifest["statistics"]["drawdown_cvar_concentration_limits"] == (
        "owner-frozen limits required before data access"
    )
    blockers = manifest["blocked_before_first_data_read"]
    assert "Freeze owner drawdown, CVaR, turnover, and concentration limits." in blockers
    assert any("Freeze the power calculation" in blocker for blocker in blockers)
    assert manifest["data"]["dataset_version_lock"]["sha256"] is None


# --------------------------------------------------------------------------------------
# The registry is research-plane only: counting a trial grants no unlock capability.
# --------------------------------------------------------------------------------------


def test_counting_trials_never_loads_the_holdout_unlock_capability(tmp_path: Path) -> None:
    """ADR-0013 §7 in the behavioural direction, not just the AST direction."""

    probe = (
        "import sys; from pathlib import Path; "
        "from chronos.research.five_tool_trials import registered_trial_count; "
        f"registered_trial_count(Path({str(tmp_path / 'probe.jsonl')!r})); "
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

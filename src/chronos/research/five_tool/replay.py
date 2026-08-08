"""Content-addressed replay artifacts: a Five-Tool trial is reproducible, or it is not sealed.

The Five-Tool trial ledger has always recorded ``evidence_artifact_sha256`` — the SHA-256
of the evaluator's evidence bytes — and then thrown the bytes away.  A digest of something
nobody kept proves that two people who *already have* the artifact hold the same one; it
cannot re-derive it.  So a completed trial could be counted, sealed, and cited while being
impossible to re-execute, which is the gap
``tests/safety/test_five_tool_registry_exercised.py::test_no_replay_artifact_capability_exists``
observed in its own words: "artifact bytes digested but never persisted anywhere on disk".

This module is the store that closes it, and it is deliberately only the *format*: writing,
loading, validating, and comparing artifact bodies.  The lifecycle — building a body from a
real attempt, binding its digest into the trial ledger, and re-executing from it — lives in
``chronos.research.five_tool_trials``, which imports this module.  The dependency runs one
way only, which is why the shapes below are validated structurally instead of by importing
the trial contracts.

**What an artifact is.**  Everything needed to re-execute one attempt byte-identically:
the campaign/trial/attempt identity, the engine identity (code commit, criteria digest,
input-contract digest, and the semantic config itself — not only its fingerprint), the
input locks and the digest of the bytes actually read, the certified-read attestation and
receipt digests from
:mod:`chronos.research.five_tool.certified_reader`, the durable start the attempt was bound
to, and the outcome: for a completed attempt the evidence bytes themselves, base64-framed,
plus every statistic derived from them; for an attempt that died after opening data, the
bounded error classification it died with.

**What an artifact is not.**  It is not the dataset.  Input identity is content-addressed
(``data_version``, ``data_sha256``, and the certified file digests), exactly as
``chronos.research.repro`` records dataset SHA-256s rather than copying CSVs.  A replay
re-reads through the reader it is given and refuses if the bytes hash differently.  An
artifact is also not evidence of anything: reproducing a number says the number is
deterministic, never that it is true or profitable.

**Fail-closed properties.**

1. *Content-addressed.*  The file name is the SHA-256 of the canonical body bytes.  Editing
   the file breaks that equality (:class:`ReplayArtifactDigestMismatch`); re-serializing it
   with different whitespace or key order breaks it too, because loading requires the file
   bytes to be exactly the canonical encoding.
2. *Self-consistent.*  The body's own digests are recomputed from its own payload on both
   write and load: ``semantic_config_fingerprint`` from the semantic config,
   ``artifact_sha256`` from the embedded output bytes, and ``evidence_digest`` from the
   artifact digest plus the four statistics.  An artifact whose internal claims disagree
   with its internal payload is refused (:class:`ReplayArtifactInvalid`), so a digest can
   never be a decoration.
3. *Never overwritten.*  A write to an existing content address must be byte-identical;
   anything else refuses rather than replacing what is already there.
4. *Durable.*  Temp file, ``fsync``, atomic ``os.replace``, directory ``fsync`` — the same
   pattern the kill switch and halt file use, because an artifact that vanishes on power
   loss is an unsealed campaign.
5. *Provisioned, not conjured.*  An unwired store, or one whose parent directory does not
   exist, refuses; a research run may not invent the place its own reproducibility is kept.

Research only.  This module imports nothing but the standard library, so possessing a
replay artifact confers no read, unlock, or execution authority of any kind.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPLAY_ARTIFACT_SCHEMA_VERSION = "chronos-five-tool-replay-artifact-v1"
ARTIFACT_FILENAME_PREFIX = "5t-replay-"
ARTIFACT_FILENAME_SUFFIX = ".json"

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"

_SHA256_HEX_LENGTH = 64
_SHA256_ALPHABET = frozenset("0123456789abcdef")
_ATTEMPT_ID_RE = re.compile(r"[0-9a-f]{32}")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

_BODY_KEYS = frozenset({"schema_version", "campaign", "engine", "inputs", "start", "outputs"})
_CAMPAIGN_KEYS = frozenset(
    {
        "campaign_id",
        "campaign_manifest_sha256",
        "cell_id",
        "hypothesis_id",
        "strategy_id",
        "trial_id",
        "attempt_id",
    }
)
_ENGINE_KEYS = frozenset(
    {
        "code_commit",
        "criteria_digest",
        "input_contract_digest",
        "semantic_config_fingerprint",
        "semantic_config",
    }
)
_INPUT_KEYS = frozenset(
    {"dataset_id", "partition", "data_version", "data_sha256", "certified_read"}
)
_CERTIFIED_READ_KEYS = frozenset(
    {
        "certified_reader",
        "dataset_sha256",
        "partition",
        "certification_manifest_sha256",
        "files",
        "receipt_payload_sha256",
    }
)
_START_KEYS = frozenset({"sequence", "record_hash"})
_OUTPUT_KEYS = frozenset(
    {
        "outcome",
        "error_type",
        "artifact_base64",
        "artifact_sha256",
        "evidence_digest",
        "observed_sharpe",
        "observations",
        "skew",
        "kurtosis",
    }
)
# ``start`` reconstructs the receipt the evaluator is handed, so it is replay *input*
# rather than a comparison axis: a replay deliberately re-executes the recorded attempt
# instead of manufacturing a new durable start.  It is still covered by the artifact
# digest and by the ledger's hash chain, and ``compare_replay_bodies`` proves nothing
# escapes comparison by digesting both bodies after the field-level pass.
_UNCOMPARED_BLOCKS = frozenset({"start", "schema_version"})


class ReplayArtifactError(RuntimeError):
    """A replay artifact could not be produced, trusted, or reproduced."""


class ReplayArtifactUnavailable(ReplayArtifactError):
    """No replay-artifact store is wired, or its root was never provisioned."""


class ReplayArtifactMissing(ReplayArtifactError):
    """A bound replay artifact does not exist at its content address."""


class ReplayArtifactInvalid(ReplayArtifactError):
    """A replay artifact exists but does not describe a replayable attempt."""


class ReplayArtifactDigestMismatch(ReplayArtifactError):
    """A replay artifact's bytes do not hash to the content address holding them."""


class ReplayDivergenceReason(StrEnum):
    """Precise, actionable reason codes for one replay comparison.

    Deliberately shaped like ``chronos.research.repro.CompareReason`` — the walk-forward
    plane's produce/replay/compare vocabulary — so one repository speaks one language about
    byte-identity.  The two planes stay separate code: that module replays a named-backtest
    slice through the strategy platform, this one replays one registered Five-Tool attempt,
    and coupling them would drag the backtest engine into an import-isolated research
    package for the sake of an enum.
    """

    IDENTITY_DRIFT = "identity_drift"
    COMMIT_DRIFT = "commit_drift"
    CRITERIA_DRIFT = "criteria_drift"
    INPUT_CONTRACT_DRIFT = "input_contract_drift"
    CONFIG_DRIFT = "config_drift"
    DATASET_LOCK_DRIFT = "dataset_lock_drift"
    INPUT_DIGEST_DRIFT = "input_digest_drift"
    CERTIFIED_READ_DRIFT = "certified_read_drift"
    OUTCOME_DRIFT = "outcome_drift"
    OUTPUT_DRIFT = "output_drift"
    UNCOMPARED_FIELD_DRIFT = "uncompared_field_drift"


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    """One named divergence between a recorded artifact and an observed re-execution."""

    reason: ReplayDivergenceReason
    field: str
    recorded: object
    observed: object

    def describe(self) -> str:
        return (
            f"{self.reason.value}: {self.field} "
            f"recorded={self.recorded!r} observed={self.observed!r}"
        )


class ReplayDivergence(ReplayArtifactError):
    """A replay did not reproduce its artifact; every divergence is named."""

    def __init__(self, findings: Sequence[ReplayFinding]) -> None:
        if not findings:  # pragma: no cover - constructed only from a non-empty list
            raise ValueError("a replay divergence must name at least one finding")
        self.findings = tuple(findings)
        self.reasons = tuple(dict.fromkeys(finding.reason for finding in self.findings))
        detail = "; ".join(finding.describe() for finding in self.findings)
        super().__init__(f"replay diverged from its artifact: {detail}")


@dataclass(frozen=True, slots=True)
class ReplayArtifact:
    """A validated artifact bound to the content address it was loaded from."""

    artifact_sha256: str
    path: Path
    document: dict[str, object]

    @property
    def outcome(self) -> str:
        return str(_block(self.document, "outputs")["outcome"])

    @property
    def attempt_id(self) -> str:
        return str(_block(self.document, "campaign")["attempt_id"])

    @property
    def evidence_digest(self) -> str | None:
        value = _block(self.document, "outputs")["evidence_digest"]
        return None if value is None else str(value)

    @property
    def output_bytes(self) -> bytes | None:
        """The evaluator's evidence bytes, or ``None`` for an attempt that produced none."""

        encoded = _block(self.document, "outputs")["artifact_base64"]
        return None if encoded is None else base64.b64decode(str(encoded), validate=True)


def canonical_artifact_bytes(body: Mapping[str, object]) -> bytes:
    """The one canonical encoding a replay artifact is content-addressed by."""

    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ReplayArtifactInvalid(f"replay artifact is not canonical JSON: {error}") from error
    return text.encode("utf-8")


def artifact_digest(body: Mapping[str, object]) -> str:
    """SHA-256 over the canonical body bytes; this is the artifact's whole identity."""

    return hashlib.sha256(canonical_artifact_bytes(body)).hexdigest()


def evidence_digest(
    *,
    artifact_sha256: str,
    observed_sharpe: float,
    observations: int,
    skew: float,
    kurtosis: float,
) -> str:
    """Recompute the trial ledger's evidence digest from the artifact's own payload.

    Deliberately duplicated from ``five_tool_trials.EvaluationEvidence.evidence_digest``
    rather than imported: importing the trial broker here would invert this module's
    dependency direction.  ``tests/safety/test_five_tool_replay_exercised.py`` pins the two
    computations equal so the duplication cannot drift.
    """

    payload = {
        "artifact_sha256": artifact_sha256,
        "observed_sharpe": observed_sharpe,
        "observations": observations,
        "skew": skew,
        "kurtosis": kurtosis,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_artifact_root(root: Path | None) -> Path:
    """Refuse an unwired or unprovisioned artifact store: no store, no replayable trial."""

    if root is None:
        raise ReplayArtifactUnavailable(
            "no replay-artifact store is wired; a Five-Tool attempt whose evidence could "
            "not be replayed may not start"
        )
    if not root.parent.is_dir():
        raise ReplayArtifactUnavailable(
            f"replay-artifact store root {root.parent} is absent; the store is provisioned "
            "deliberately, never conjured by a research run"
        )
    return root


def artifact_path(root: Path, artifact_sha256: str) -> Path:
    _require_sha256("replay artifact sha256", artifact_sha256)
    return root / f"{ARTIFACT_FILENAME_PREFIX}{artifact_sha256}{ARTIFACT_FILENAME_SUFFIX}"


def write_replay_artifact(root: Path | None, body: Mapping[str, object]) -> ReplayArtifact:
    """Persist one artifact at its content address, fail-closed and never overwriting."""

    validated = validate_artifact_body(body)
    payload = canonical_artifact_bytes(validated)
    digest = hashlib.sha256(payload).hexdigest()
    store = require_artifact_root(root)
    store.mkdir(exist_ok=True)
    target = artifact_path(store, digest)
    if target.exists():
        existing = target.read_bytes()
        if existing != payload:
            raise ReplayArtifactInvalid(
                f"replay artifact {target.name} already holds different bytes; a content "
                "address is never overwritten"
            )
        return ReplayArtifact(artifact_sha256=digest, path=target, document=validated)
    temp = store / f".{digest}.tmp"
    descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, target)
    directory = os.open(str(store), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return ReplayArtifact(artifact_sha256=digest, path=target, document=validated)


def load_replay_artifact(root: Path | None, artifact_sha256: str) -> ReplayArtifact:
    """Load one artifact by content address, refusing missing, tampered, or invalid bytes."""

    store = require_artifact_root(root)
    target = artifact_path(store, artifact_sha256)
    if not target.is_file():
        raise ReplayArtifactMissing(
            f"no replay artifact at {target}; a trial whose artifact is gone cannot be "
            "replayed and cannot be sealed"
        )
    raw = target.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != artifact_sha256:
        raise ReplayArtifactDigestMismatch(
            f"replay artifact {target.name} bytes hash to {actual}, not to the content "
            f"address {artifact_sha256} holding them"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayArtifactInvalid(
            f"replay artifact {target.name} is not readable JSON: {type(error).__name__}"
        ) from error
    if not isinstance(decoded, dict):
        raise ReplayArtifactInvalid("replay artifact root must be a JSON object")
    validated = validate_artifact_body(decoded)
    if canonical_artifact_bytes(validated) != raw:
        raise ReplayArtifactInvalid(
            f"replay artifact {target.name} is not stored in its canonical encoding"
        )
    return ReplayArtifact(artifact_sha256=artifact_sha256, path=target, document=validated)


def compare_replay_bodies(
    recorded: Mapping[str, object],
    observed: Mapping[str, object],
) -> tuple[ReplayFinding, ...]:
    """Name every way an observed re-execution disagrees with its recorded artifact.

    The field-level pass runs first so divergences are reported in the vocabulary a reader
    can act on.  The digest pass then runs over the whole body, so a field added to the
    schema and forgotten here surfaces as ``uncompared_field_drift`` rather than silently
    passing — the guard-the-guard direction, since "refuses on ANY byte divergence" must
    stay literally true as the schema grows.
    """

    findings: list[ReplayFinding] = []

    def note(reason: ReplayDivergenceReason, field: str, left: object, right: object) -> None:
        findings.append(ReplayFinding(reason=reason, field=field, recorded=left, observed=right))

    def compare(
        reason: ReplayDivergenceReason,
        block: str,
        keys: Sequence[str],
    ) -> None:
        left = _block(recorded, block)
        right = _block(observed, block)
        for key in keys:
            if left[key] != right[key]:
                note(reason, f"{block}.{key}", left[key], right[key])

    compare(
        ReplayDivergenceReason.IDENTITY_DRIFT,
        "campaign",
        sorted(_CAMPAIGN_KEYS),
    )
    compare(ReplayDivergenceReason.COMMIT_DRIFT, "engine", ["code_commit"])
    compare(ReplayDivergenceReason.CRITERIA_DRIFT, "engine", ["criteria_digest"])
    compare(ReplayDivergenceReason.INPUT_CONTRACT_DRIFT, "engine", ["input_contract_digest"])
    compare(
        ReplayDivergenceReason.CONFIG_DRIFT,
        "engine",
        ["semantic_config", "semantic_config_fingerprint"],
    )
    compare(
        ReplayDivergenceReason.DATASET_LOCK_DRIFT,
        "inputs",
        ["dataset_id", "partition", "data_version"],
    )
    compare(ReplayDivergenceReason.INPUT_DIGEST_DRIFT, "inputs", ["data_sha256"])
    compare(ReplayDivergenceReason.CERTIFIED_READ_DRIFT, "inputs", ["certified_read"])
    compare(ReplayDivergenceReason.OUTCOME_DRIFT, "outputs", ["outcome", "error_type"])
    compare(
        ReplayDivergenceReason.OUTPUT_DRIFT,
        "outputs",
        [
            "artifact_base64",
            "artifact_sha256",
            "evidence_digest",
            "observed_sharpe",
            "observations",
            "skew",
            "kurtosis",
        ],
    )

    if not findings and artifact_digest(recorded) != artifact_digest(observed):
        uncompared = sorted(
            key
            for key in set(recorded) | set(observed)
            if key not in _UNCOMPARED_BLOCKS and recorded.get(key) != observed.get(key)
        )
        note(
            ReplayDivergenceReason.UNCOMPARED_FIELD_DRIFT,
            ", ".join(uncompared) or "artifact",
            artifact_digest(recorded),
            artifact_digest(observed),
        )
    return tuple(findings)


def validate_artifact_body(body: Mapping[str, object]) -> dict[str, object]:
    """Fully validate one artifact body, recomputing every digest it asserts."""

    _require_exact_keys(body, _BODY_KEYS, "replay artifact")
    if body.get("schema_version") != REPLAY_ARTIFACT_SCHEMA_VERSION:
        raise ReplayArtifactInvalid(
            f"replay artifact schema_version must be {REPLAY_ARTIFACT_SCHEMA_VERSION!r}"
        )
    campaign = _validated_campaign(_required_mapping(body, "campaign"))
    engine = _validated_engine(_required_mapping(body, "engine"))
    inputs = _validated_inputs(_required_mapping(body, "inputs"))
    start = _validated_start(_required_mapping(body, "start"))
    outputs = _validated_outputs(_required_mapping(body, "outputs"), inputs)
    return {
        "schema_version": REPLAY_ARTIFACT_SCHEMA_VERSION,
        "campaign": campaign,
        "engine": engine,
        "inputs": inputs,
        "start": start,
        "outputs": outputs,
    }


def _validated_campaign(campaign: Mapping[str, object]) -> dict[str, object]:
    _require_exact_keys(campaign, _CAMPAIGN_KEYS, "replay artifact campaign")
    for key in ("campaign_id", "cell_id", "hypothesis_id", "strategy_id", "trial_id"):
        _require_nonempty_string(campaign, f"campaign.{key}", key)
    _require_sha256(
        "campaign.campaign_manifest_sha256",
        _required_string(campaign, "campaign_manifest_sha256"),
    )
    attempt_id = _required_string(campaign, "attempt_id")
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise ReplayArtifactInvalid("campaign.attempt_id is not canonical UUID hex")
    return dict(sorted(campaign.items()))


def _validated_engine(engine: Mapping[str, object]) -> dict[str, object]:
    _require_exact_keys(engine, _ENGINE_KEYS, "replay artifact engine")
    commit = _required_string(engine, "code_commit")
    if not commit.strip() or commit.casefold() == "unknown":
        raise ReplayArtifactInvalid("engine.code_commit must be resolved, never 'unknown'")
    for key in ("criteria_digest", "input_contract_digest", "semantic_config_fingerprint"):
        _require_sha256(f"engine.{key}", _required_string(engine, key))
    config = engine.get("semantic_config")
    if not isinstance(config, dict):
        raise ReplayArtifactInvalid("engine.semantic_config must be a JSON object")
    encoded = canonical_artifact_bytes(config)
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if fingerprint != engine["semantic_config_fingerprint"]:
        raise ReplayArtifactInvalid(
            "engine.semantic_config_fingerprint disagrees with the config it fingerprints: "
            f"recorded {engine['semantic_config_fingerprint']}, bytes {fingerprint}"
        )
    return dict(sorted(engine.items()))


def _validated_inputs(inputs: Mapping[str, object]) -> dict[str, object]:
    _require_exact_keys(inputs, _INPUT_KEYS, "replay artifact inputs")
    for key in ("dataset_id", "partition"):
        _require_nonempty_string(inputs, f"inputs.{key}", key)
    _require_sha256("inputs.data_version", _required_string(inputs, "data_version"))
    data_sha256 = inputs.get("data_sha256")
    if data_sha256 is not None:
        if not isinstance(data_sha256, str):
            raise ReplayArtifactInvalid("inputs.data_sha256 must be a digest or null")
        _require_sha256("inputs.data_sha256", data_sha256)
    certified = _validated_certified_read(_required_mapping(inputs, "certified_read"), inputs)
    return {
        "certified_read": certified,
        "data_sha256": data_sha256,
        "data_version": inputs["data_version"],
        "dataset_id": inputs["dataset_id"],
        "partition": inputs["partition"],
    }


def _validated_certified_read(
    certified: Mapping[str, object],
    inputs: Mapping[str, object],
) -> dict[str, object]:
    _require_exact_keys(certified, _CERTIFIED_READ_KEYS, "replay artifact inputs.certified_read")
    proven = certified.get("certified_reader")
    if not isinstance(proven, bool):
        raise ReplayArtifactInvalid("inputs.certified_read.certified_reader must be a boolean")
    _require_sha256(
        "inputs.certified_read.dataset_sha256",
        _required_string(certified, "dataset_sha256"),
    )
    if certified["dataset_sha256"] != inputs["data_version"]:
        raise ReplayArtifactInvalid(
            "inputs.certified_read.dataset_sha256 disagrees with the campaign data_version"
        )
    _require_nonempty_string(certified, "inputs.certified_read.partition", "partition")
    if certified["partition"] != inputs["partition"]:
        raise ReplayArtifactInvalid(
            "inputs.certified_read.partition disagrees with the requested partition"
        )
    manifest_digest = certified.get("certification_manifest_sha256")
    files = certified.get("files")
    receipt = certified.get("receipt_payload_sha256")
    if not proven:
        if manifest_digest is not None or files is not None or receipt is not None:
            raise ReplayArtifactInvalid(
                "an uncertified read cannot carry certification manifest, file, or receipt "
                "digests; the artifact records what was proven, never what was assumed"
            )
        return dict(sorted(certified.items()))
    _require_sha256("inputs.certified_read.certification_manifest_sha256", str(manifest_digest))
    if not isinstance(files, dict) or not files:
        raise ReplayArtifactInvalid(
            "a certified read must record the non-empty file digest set it attested"
        )
    for path, digest in files.items():
        if not isinstance(path, str) or not path.strip():
            raise ReplayArtifactInvalid("certified file paths must be non-empty strings")
        if not isinstance(digest, str):
            raise ReplayArtifactInvalid(f"certified file {path!r} digest must be a string")
        _require_sha256(f"inputs.certified_read.files[{path}]", digest)
    _require_sha256("inputs.certified_read.receipt_payload_sha256", str(receipt))
    return {
        "certification_manifest_sha256": manifest_digest,
        "certified_reader": True,
        "dataset_sha256": certified["dataset_sha256"],
        "files": dict(sorted(files.items())),
        "partition": certified["partition"],
        "receipt_payload_sha256": receipt,
    }


def _validated_start(start: Mapping[str, object]) -> dict[str, object]:
    _require_exact_keys(start, _START_KEYS, "replay artifact start")
    sequence = start.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ReplayArtifactInvalid("start.sequence must be a non-negative integer")
    _require_sha256("start.record_hash", _required_string(start, "record_hash"))
    return dict(sorted(start.items()))


def _validated_outputs(
    outputs: Mapping[str, object],
    inputs: Mapping[str, object],
) -> dict[str, object]:
    _require_exact_keys(outputs, _OUTPUT_KEYS, "replay artifact outputs")
    outcome = outputs.get("outcome")
    if outcome not in {OUTCOME_COMPLETED, OUTCOME_FAILED}:
        raise ReplayArtifactInvalid(
            f"outputs.outcome must be {OUTCOME_COMPLETED!r} or {OUTCOME_FAILED!r}"
        )
    if outcome == OUTCOME_FAILED:
        for key in (
            "artifact_base64",
            "artifact_sha256",
            "evidence_digest",
            "observed_sharpe",
            "observations",
            "skew",
            "kurtosis",
        ):
            if outputs.get(key) is not None:
                raise ReplayArtifactInvalid(f"a failed attempt cannot record outputs.{key}")
        error_type = outputs.get("error_type")
        if not isinstance(error_type, str) or _ERROR_TYPE_RE.fullmatch(error_type) is None:
            raise ReplayArtifactInvalid(
                "a failed attempt must record one bounded outputs.error_type"
            )
        return dict(sorted(outputs.items()))

    if outputs.get("error_type") is not None:
        raise ReplayArtifactInvalid("a completed attempt cannot record outputs.error_type")
    if inputs.get("data_sha256") != inputs.get("data_version"):
        raise ReplayArtifactInvalid(
            "a completed attempt must record bytes matching the authorized data_version"
        )
    encoded = outputs.get("artifact_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ReplayArtifactInvalid(
            "a completed attempt must embed its evidence bytes; a digest of bytes nobody "
            "kept is not a replay artifact"
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ReplayArtifactInvalid(
            f"outputs.artifact_base64 is not strict base64: {type(error).__name__}"
        ) from error
    if not payload:
        raise ReplayArtifactInvalid("outputs.artifact_base64 must decode to non-empty bytes")
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise ReplayArtifactInvalid(
            "outputs.artifact_base64 is not the canonical encoding of its own bytes"
        )
    computed = hashlib.sha256(payload).hexdigest()
    _require_sha256("outputs.artifact_sha256", _required_string(outputs, "artifact_sha256"))
    if computed != outputs["artifact_sha256"]:
        raise ReplayArtifactInvalid(
            "outputs.artifact_sha256 disagrees with the embedded output bytes: recorded "
            f"{outputs['artifact_sha256']}, bytes {computed}"
        )
    observations = outputs.get("observations")
    if isinstance(observations, bool) or not isinstance(observations, int) or observations < 2:
        raise ReplayArtifactInvalid("outputs.observations must be an integer >= 2")
    # The raw JSON value is kept, never coerced: ``EvaluationEvidence`` digests whatever it
    # was handed, so re-digesting ``0`` as ``0.0`` would manufacture a false divergence.
    statistics: dict[str, int | float] = {}
    for key in ("observed_sharpe", "skew", "kurtosis"):
        value = outputs.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ReplayArtifactInvalid(f"outputs.{key} must be a finite number")
        if not math.isfinite(float(value)):
            raise ReplayArtifactInvalid(f"outputs.{key} must be a finite number")
        statistics[key] = value
    _require_sha256("outputs.evidence_digest", _required_string(outputs, "evidence_digest"))
    expected = evidence_digest(
        artifact_sha256=computed,
        observed_sharpe=statistics["observed_sharpe"],
        observations=observations,
        skew=statistics["skew"],
        kurtosis=statistics["kurtosis"],
    )
    if expected != outputs["evidence_digest"]:
        raise ReplayArtifactInvalid(
            "outputs.evidence_digest disagrees with the artifact and statistics it digests: "
            f"recorded {outputs['evidence_digest']}, computed {expected}"
        )
    return dict(sorted(outputs.items()))


def _block(body: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = body.get(name)
    if not isinstance(value, dict):  # pragma: no cover - bodies are validated before use
        raise ReplayArtifactInvalid(f"replay artifact {name} must be an object")
    return value


def _required_mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ReplayArtifactInvalid(f"replay artifact {key} must be an object")
    return value


def _required_string(parent: Mapping[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str):
        raise ReplayArtifactInvalid(f"replay artifact {key} must be a string")
    return value


def _require_nonempty_string(parent: Mapping[str, object], name: str, key: str) -> None:
    if not _required_string(parent, key).strip():
        raise ReplayArtifactInvalid(f"replay artifact {name} must be non-empty")


def _require_sha256(name: str, value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(char not in _SHA256_ALPHABET for char in value):
        raise ReplayArtifactInvalid(f"{name} must be a lowercase 64-character SHA-256 digest")


def _require_exact_keys(
    mapping: Mapping[str, object], expected: frozenset[str], context: str
) -> None:
    present = set(mapping)
    if present != expected:
        raise ReplayArtifactInvalid(
            f"{context} keys do not match schema; "
            f"missing={sorted(expected - present)}, unknown={sorted(present - expected)}"
        )


# Signal-to-ledger adapter lives in signal_replay.py. Re-export so both the
# default-branch artifact store and PR-73 replay_five_tool tests share this module.
from chronos.research.five_tool.signal_replay import (  # noqa: E402
    FiveToolReplayPolicy,
    FiveToolReplayResult,
    IncompleteReplayError,
    ReplayBar,
    ReplayInputError,
    TerminalPositionPolicy,
    replay_five_tool,
)

__all__ = (
    "ARTIFACT_FILENAME_PREFIX",
    "FiveToolReplayPolicy",
    "FiveToolReplayResult",
    "IncompleteReplayError",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "REPLAY_ARTIFACT_SCHEMA_VERSION",
    "ReplayArtifact",
    "ReplayArtifactDigestMismatch",
    "ReplayArtifactError",
    "ReplayArtifactInvalid",
    "ReplayArtifactMissing",
    "ReplayArtifactUnavailable",
    "ReplayBar",
    "ReplayDivergence",
    "ReplayDivergenceReason",
    "ReplayFinding",
    "ReplayInputError",
    "TerminalPositionPolicy",
    "artifact_digest",
    "compare_replay_bodies",
    "load_replay_artifact",
    "replay_five_tool",
    "require_artifact_root",
    "validate_artifact_body",
    "write_replay_artifact",
)

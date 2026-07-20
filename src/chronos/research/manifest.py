"""Deterministic research-run manifests (config / data / code provenance).

A cold reader must be able to reproduce or reject a research run from the
manifest alone. Hashes are content-addressed and ordered so identical inputs
yield identical JSON (byte-stable under ``json.dumps(..., sort_keys=True)``).

Pure research-plane module: no broker or order imports
(``tests/safety/test_research_isolation.py``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chronos.research.campaign import CampaignReport
from chronos.research.walkforward import WalkForwardReport, WalkForwardVerdict


def stable_hash(payload: Mapping[str, Any] | dict[str, Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace ambiguity)."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchRunManifest:
    """Auditable provenance binding for one research campaign (or cell)."""

    schema_version: str
    code_commit: str
    policy_version: str
    policy_hash: str
    stage_end: str
    seed: int
    warmup: int
    test_window: int
    min_trades: int
    data_hashes: dict[str, str]  # symbol -> window_sha256 (holdout-free)
    config_hash: str  # hash of the non-data provenance fields below
    overall_verdict: str  # PASS | FAIL | INSUFFICIENT_EVIDENCE | MIXED | EMPTY
    verdict_counts: dict[str, int]
    cell_count: int
    excluded_count: int
    errored_count: int

    def to_dict(self) -> dict[str, Any]:
        """Deterministic dict suitable for JSON serialization and re-hashing."""

        return {
            "schema_version": self.schema_version,
            "code_commit": self.code_commit,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "stage_end": self.stage_end,
            "seed": self.seed,
            "warmup": self.warmup,
            "test_window": self.test_window,
            "min_trades": self.min_trades,
            "data_hashes": dict(sorted(self.data_hashes.items())),
            "config_hash": self.config_hash,
            "overall_verdict": self.overall_verdict,
            "verdict_counts": dict(sorted(self.verdict_counts.items())),
            "cell_count": self.cell_count,
            "excluded_count": self.excluded_count,
            "errored_count": self.errored_count,
        }

    def fingerprint(self) -> str:
        """Content hash of the full manifest dict (includes results summary)."""

        return stable_hash(self.to_dict())


def _overall_verdict(counts: Mapping[str, int]) -> str:
    """Aggregate cell verdicts without inventing a PASS from thin evidence.

    Rules (fail-closed / honesty-first):
    - no cells with a recorded verdict → EMPTY
    - any FAIL and no PASS → FAIL (rejection is evidence)
    - any PASS and no FAIL → PASS only if every cell is PASS
    - mix of PASS and FAIL → MIXED
    - only INSUFFICIENT_EVIDENCE (or that + empty others) → INSUFFICIENT_EVIDENCE
    - PASS present alongside INSUFFICIENT_EVIDENCE only → MIXED (partial evidence)
    """

    pass_n = counts.get(WalkForwardVerdict.PASS.value, 0) + counts.get("pass", 0)
    fail_n = counts.get(WalkForwardVerdict.FAIL.value, 0) + counts.get("fail", 0)
    insuf_n = counts.get(WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value, 0) + counts.get(
        "insufficient_evidence", 0
    )
    total = pass_n + fail_n + insuf_n
    if total == 0:
        return "empty"
    if pass_n > 0 and fail_n > 0:
        return "mixed"
    if pass_n > 0 and insuf_n > 0:
        return "mixed"
    if pass_n > 0 and pass_n == total:
        return WalkForwardVerdict.PASS.value
    if fail_n > 0 and insuf_n == 0:
        return WalkForwardVerdict.FAIL.value
    if fail_n > 0 and insuf_n > 0 and pass_n == 0:
        # Failures dominate when paired only with insufficient cells: still
        # not a PASS; report FAIL so rejection is not washed out.
        return WalkForwardVerdict.FAIL.value
    return WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value


def _count_verdicts(report: CampaignReport) -> dict[str, int]:
    counts: dict[str, int] = {
        WalkForwardVerdict.PASS.value: 0,
        WalkForwardVerdict.FAIL.value: 0,
        WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value: 0,
    }
    for row in report.verdict_table:
        key = str(row.verdict)
        # Normalize enum string / value forms.
        if key.startswith("WalkForwardVerdict."):
            key = key.split(".", 1)[1].lower()
        if key == "pass":
            counts[WalkForwardVerdict.PASS.value] += 1
        elif key == "fail":
            counts[WalkForwardVerdict.FAIL.value] += 1
        else:
            counts[WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value] += 1
    return counts


def manifest_from_campaign(report: CampaignReport) -> ResearchRunManifest:
    """Build a deterministic manifest from a completed campaign report."""

    data_hashes: dict[str, str] = {}
    for cell in report.cells:
        if cell.data_fingerprint is not None and cell.symbol not in data_hashes:
            data_hashes[cell.symbol] = cell.data_fingerprint

    verdict_counts = _count_verdicts(report)
    config_payload = {
        "code_commit": report.code_commit,
        "policy_version": report.policy_version,
        "policy_hash": report.policy_hash,
        "stage_end": report.stage_end,
        "seed": report.seed,
        "warmup": report.warmup,
        "test_window": report.test_window,
        "min_trades": report.min_trades,
        "data_hashes": dict(sorted(data_hashes.items())),
    }
    return ResearchRunManifest(
        schema_version="research-manifest-v1",
        code_commit=report.code_commit,
        policy_version=report.policy_version,
        policy_hash=report.policy_hash,
        stage_end=report.stage_end,
        seed=report.seed,
        warmup=report.warmup,
        test_window=report.test_window,
        min_trades=report.min_trades,
        data_hashes=data_hashes,
        config_hash=stable_hash(config_payload),
        overall_verdict=_overall_verdict(verdict_counts),
        verdict_counts=verdict_counts,
        cell_count=len(report.cells),
        excluded_count=len(report.excluded),
        errored_count=len(report.errored),
    )


def manifest_from_walkforward(
    report: WalkForwardReport,
    *,
    code_commit: str,
    policy_version: str,
    policy_hash: str,
    data_hashes: Mapping[str, str],
    stage_end: str,
    warmup: int,
    test_window: int,
    min_trades: int,
) -> ResearchRunManifest:
    """Build a single-cell manifest from a walk-forward report."""

    verdict = str(report.verdict)
    if hasattr(report.verdict, "value"):
        verdict = report.verdict.value
    counts = {
        WalkForwardVerdict.PASS.value: 0,
        WalkForwardVerdict.FAIL.value: 0,
        WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value: 0,
    }
    if verdict == WalkForwardVerdict.PASS.value:
        counts[WalkForwardVerdict.PASS.value] = 1
    elif verdict == WalkForwardVerdict.FAIL.value:
        counts[WalkForwardVerdict.FAIL.value] = 1
    else:
        counts[WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value] = 1

    sorted_hashes = dict(sorted(dict(data_hashes).items()))
    config_payload = {
        "code_commit": code_commit,
        "policy_version": policy_version,
        "policy_hash": policy_hash,
        "stage_end": stage_end,
        "seed": report.seed,
        "warmup": warmup,
        "test_window": test_window,
        "min_trades": min_trades,
        "data_hashes": sorted_hashes,
    }
    return ResearchRunManifest(
        schema_version="research-manifest-v1",
        code_commit=code_commit,
        policy_version=policy_version,
        policy_hash=policy_hash,
        stage_end=stage_end,
        seed=report.seed,
        warmup=warmup,
        test_window=test_window,
        min_trades=min_trades,
        data_hashes=sorted_hashes,
        config_hash=stable_hash(config_payload),
        overall_verdict=_overall_verdict(counts),
        verdict_counts=counts,
        cell_count=1,
        excluded_count=0,
        errored_count=0,
    )


def write_manifest(manifest: ResearchRunManifest, path: Path | str) -> None:
    """Write manifest JSON atomically-enough for research artifacts."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.to_dict()
    payload["manifest_fingerprint"] = manifest.fingerprint()
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

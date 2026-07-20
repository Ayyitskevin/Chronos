"""Deterministic research-run reproducibility (produce / replay / compare).

Pure research-plane module: builds an auditable run manifest, re-runs the
supported deterministic named-backtest slice from that manifest, and compares
two manifests with precise pass/fail reasons.

Never records secrets or brokerage account identifiers. Never imports
``chronos.orders`` or ``chronos.broker`` (see ``tests/safety/test_research_isolation.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from chronos.research.runner import STRATEGY_FACTORIES, current_commit, run_named_backtest

SCHEMA_VERSION = "research-run-manifest-v1"
DEFAULT_TIMEZONE = "UTC"

# Keys (case-insensitive, substring) that must never appear with real values
# in a research-run manifest. Values are replaced with a redaction token.
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential|"
    r"private[_-]?key|account[_-]?id|ib[_-]?account|username|client[_-]?id|"
    r"access[_-]?key|refresh[_-]?token|bearer)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"

# Fields required for a complete, replayable manifest.
_REQUIRED_TOP_LEVEL = (
    "schema_version",
    "code_commit",
    "timezone",
    "seed",
    "strategy_id",
    "strategy_version",
    "config",
    "config_hash",
    "policy_version",
    "policy_hash",
    "datasets",
    "date_window",
    "outputs",
    "output_fingerprint",
)


class CompareStatus(StrEnum):
    """Terminal outcome of a manifest comparison."""

    PASS = "pass"
    FAIL = "fail"


class CompareReason(StrEnum):
    """Precise, actionable reason codes for produce/replay/compare."""

    MATCH = "match"
    INCOMPLETE_MANIFEST = "incomplete_manifest"
    UNSUPPORTED_LEGACY = "unsupported_legacy"
    MISSING_DATA = "missing_data"
    CHECKSUM_DRIFT = "checksum_drift"
    CONFIG_DRIFT = "config_drift"
    SEED_DRIFT = "seed_drift"
    TIMEZONE_DRIFT = "timezone_drift"
    DATE_WINDOW_DRIFT = "date_window_drift"
    STRATEGY_DRIFT = "strategy_drift"
    COMMIT_DRIFT = "commit_drift"
    NONDETERMINISM = "nondeterminism"
    OUTPUT_DRIFT = "output_drift"
    POLICY_DRIFT = "policy_drift"


class ReproError(ValueError):
    """Fail-closed error for incomplete, legacy, or unusable research-run records."""

    def __init__(self, reason: CompareReason, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {detail}")


def stable_hash(payload: Mapping[str, Any] | list[Any] | dict[str, Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys, compact separators)."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """Content hash of a file (streamed)."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-like keys; never raise on nested structure."""

    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _SECRET_KEY_RE.search(key_str):
                out[key_str] = _REDACTED
            else:
                out[key_str] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def normalize_timezone(tz: str | None) -> str:
    """Research identity is always UTC; aliases of UTC normalize, others are rejected.

    Wall-clock local zones are not part of research identity — they would make
    the same bars appear to produce different timestamps. Callers that need a
    local display layer must convert *after* the manifest is written.
    """

    if tz is None or str(tz).strip() == "":
        return DEFAULT_TIMEZONE
    cleaned = str(tz).strip()
    if cleaned.upper() in {"UTC", "Z", "ETC/UTC", "GMT", "ETC/GMT"}:
        return DEFAULT_TIMEZONE
    raise ReproError(
        CompareReason.TIMEZONE_DRIFT,
        f"research-run timezone must normalize to UTC, got {tz!r}",
    )


def normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets and return a plain dict with stable key ordering via sort on dump."""

    redacted = redact_secrets(dict(config))
    if not isinstance(redacted, dict):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "config must be an object")
    # Round-trip through canonical JSON so types are JSON-native and order-stable.
    loaded: Any = json.loads(json.dumps(redacted, sort_keys=True, default=str))
    if not isinstance(loaded, dict):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "config must be an object")
    return loaded


def _identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Fields that define run identity (excludes produced_at, artifacts, fingerprint)."""

    return {
        "schema_version": manifest["schema_version"],
        "code_commit": manifest["code_commit"],
        "timezone": manifest["timezone"],
        "seed": manifest["seed"],
        "strategy_id": manifest["strategy_id"],
        "strategy_version": manifest["strategy_version"],
        "config": manifest["config"],
        "config_hash": manifest["config_hash"],
        "policy_version": manifest["policy_version"],
        "policy_hash": manifest["policy_hash"],
        "datasets": manifest["datasets"],
        "date_window": manifest["date_window"],
        "outputs": manifest["outputs"],
        "output_fingerprint": manifest["output_fingerprint"],
    }


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    return stable_hash(_identity_payload(manifest))


def build_output_fingerprint(outputs: Mapping[str, Any]) -> str:
    """Hash of the canonical output payload (metrics + summary fields)."""

    return stable_hash(dict(outputs))


def _dataset_entry(
    *,
    path: Path,
    symbol: str,
    sha256: str | None = None,
    dataset_id: str | None = None,
) -> dict[str, str]:
    resolved = path.resolve()
    digest = sha256 if sha256 is not None else file_sha256(resolved)
    return {
        "dataset_id": dataset_id or f"{symbol.upper()}.csv",
        "symbol": symbol.upper(),
        "path": str(path),
        "sha256": digest,
    }


def _require_complete(manifest: Mapping[str, Any]) -> None:
    missing = [key for key in _REQUIRED_TOP_LEVEL if key not in manifest]
    if missing:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            f"missing required fields: {', '.join(missing)}",
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReproError(
            CompareReason.UNSUPPORTED_LEGACY,
            f"unsupported schema_version {manifest.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION!r}",
        )
    if not isinstance(manifest.get("datasets"), list) or not manifest["datasets"]:
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "datasets must be a non-empty list")
    if not isinstance(manifest.get("config"), Mapping):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "config must be an object")
    if not isinstance(manifest.get("date_window"), Mapping):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "date_window must be an object")
    if not isinstance(manifest.get("outputs"), Mapping):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "outputs must be an object")
    for key in ("strategy_id", "strategy_version", "policy_version", "policy_hash", "config_hash"):
        if not str(manifest.get(key) or "").strip():
            raise ReproError(CompareReason.INCOMPLETE_MANIFEST, f"{key} must be non-empty")
    commit = str(manifest.get("code_commit") or "").strip()
    if not commit or commit == "unknown":
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            "code_commit must be a resolvable git SHA (not empty/unknown)",
        )
    try:
        int(manifest["seed"])
    except (TypeError, ValueError) as error:
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "seed must be an integer") from error
    normalize_timezone(str(manifest.get("timezone")))


def load_manifest(path: Path | str) -> dict[str, Any]:
    """Load and validate a research-run manifest (fail closed)."""

    target = Path(path)
    if not target.exists():
        raise ReproError(CompareReason.MISSING_DATA, f"manifest not found: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST, f"manifest is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "manifest root must be an object")
    # Legacy / foreign shapes without our schema version.
    if "schema_version" not in payload:
        raise ReproError(
            CompareReason.UNSUPPORTED_LEGACY,
            "manifest lacks schema_version; unsupported legacy research artifact",
        )
    _require_complete(payload)
    # Recompute fingerprint for integrity of the identity payload.
    expected_fp = manifest_fingerprint(payload)
    recorded = payload.get("manifest_fingerprint")
    if recorded is not None and recorded != expected_fp:
        raise ReproError(
            CompareReason.CHECKSUM_DRIFT,
            f"manifest_fingerprint mismatch: recorded={recorded} computed={expected_fp}",
        )
    return payload


def write_manifest(manifest: Mapping[str, Any], path: Path | str) -> dict[str, Any]:
    """Validate, attach fingerprint, and write canonical JSON."""

    payload = dict(manifest)
    payload["timezone"] = normalize_timezone(str(payload.get("timezone")))
    if not isinstance(payload.get("config"), Mapping):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "config must be an object")
    payload["config"] = normalize_config(payload["config"])
    # Recompute config_hash from the normalized config so identity is self-consistent.
    identity_config = {
        "strategy_id": payload.get("strategy_id"),
        "strategy_version": payload.get("strategy_version"),
        "seed": int(payload.get("seed", 0)),
        "timezone": payload["timezone"],
        "config": payload["config"],
        "policy_version": payload.get("policy_version"),
        "policy_hash": payload.get("policy_hash"),
        "date_window": payload.get("date_window"),
        "datasets": payload.get("datasets"),
    }
    payload["config_hash"] = stable_hash(identity_config)
    if not payload.get("output_fingerprint"):
        outputs = payload.get("outputs")
        if not isinstance(outputs, Mapping):
            raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "outputs must be an object")
        payload["output_fingerprint"] = build_output_fingerprint(outputs)
    _require_complete(payload)
    payload["manifest_fingerprint"] = manifest_fingerprint(payload)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _canonical_outputs_from_backtest(result: Mapping[str, Any]) -> dict[str, Any]:
    """Strip non-deterministic / non-essential fields from a named-backtest result."""

    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        metrics = {}
    # Metrics dataclasses may contain floats; force JSON-native via dump/load.
    metrics_payload = json.loads(json.dumps(dict(metrics), sort_keys=True, default=str))
    date_range = result.get("date_range") or []
    return {
        "bars": result.get("bars"),
        "date_range": list(date_range),
        "metrics": metrics_payload,
        "risk_rejections": result.get("risk_rejections"),
        "skipped_conversions": result.get("skipped_conversions"),
        "data_quality_blocking": result.get("data_quality_blocking"),
        "data_quality_issues": result.get("data_quality_issues"),
    }


def produce_named_backtest_run(
    *,
    run_dir: Path,
    strategy_id: str,
    symbol: str,
    data_dir: Path,
    policy_path: Path,
    initial_cash: float = 3000.0,
    slippage_bps: float = 2.0,
    date_start: str | None = None,
    date_end: str | None = None,
    seed: int = 0,
    timezone: str = DEFAULT_TIMEZONE,
    code_commit: str | None = None,
    halt_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the deterministic named-backtest slice and write a run bundle.

    Bundle layout::

        run_dir/
          config.json
          output.json
          manifest.json
    """

    tz = normalize_timezone(timezone)
    if strategy_id not in STRATEGY_FACTORIES:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGY_FACTORIES)}",
        )
    csv_path = data_dir / f"{symbol.upper()}.csv"
    if not csv_path.exists():
        raise ReproError(CompareReason.MISSING_DATA, f"no data file {csv_path}")

    result = run_named_backtest(
        strategy_name=strategy_id,
        symbol=symbol,
        data_dir=data_dir,
        policy_path=policy_path,
        initial_cash=initial_cash,
        slippage_bps=slippage_bps,
        halt_path=halt_path or (run_dir / "halt.json"),
        date_start=date_start,
        date_end=date_end,
    )
    commit = code_commit if code_commit is not None else current_commit()
    if commit in {"", "unknown"}:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            "code_commit must be a resolvable git SHA; run from a git checkout",
        )

    strategy_version = str(result.get("strategy_version") or "")
    policy_version = str(result.get("policy_version") or "")
    policy_hash = str(result.get("policy_hash") or "")
    data_sha = str(result.get("data_sha256") or "")
    if not data_sha:
        data_sha = file_sha256(csv_path)

    raw_range = result.get("date_range")
    date_range: list[Any] = list(raw_range) if isinstance(raw_range, (list, tuple)) else []
    window_start = date_start or (date_range[0] if len(date_range) > 0 else None)
    window_end = date_end or (date_range[1] if len(date_range) > 1 else None)
    date_window = {"start": window_start, "end": window_end}

    config = normalize_config(
        {
            "slice": "named_backtest",
            "strategy_id": strategy_id,
            "symbol": symbol.upper(),
            "data_dir": str(data_dir),
            "policy_path": str(policy_path),
            "initial_cash_usd": initial_cash,
            "slippage_bps_per_side": slippage_bps,
            "date_start": date_start,
            "date_end": date_end,
            "seed": int(seed),
            "timezone": tz,
        }
    )
    datasets = [
        _dataset_entry(path=csv_path, symbol=symbol.upper(), sha256=data_sha),
    ]
    # Identity outputs are path-free so two run dirs with the same slice match.
    outputs = _canonical_outputs_from_backtest(result)
    output_fp = build_output_fingerprint(outputs)
    outputs_with_hash = {
        **outputs,
        "artifact_sha256": stable_hash(outputs),
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "config.json"
    output_path = run_dir / "output.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_path.write_text(
        json.dumps(outputs_with_hash, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "code_commit": commit,
        "timezone": tz,
        "seed": int(seed),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "config": config,
        "config_hash": "",  # filled by write_manifest
        "policy_version": policy_version,
        "policy_hash": policy_hash,
        "datasets": datasets,
        "date_window": date_window,
        # Path-free identity payload (metrics + counts only).
        "outputs": outputs_with_hash,
        "output_fingerprint": output_fp,
        # Non-identity bookkeeping (excluded from fingerprint helpers below).
        "artifacts": {
            "output_json": {
                "path": "output.json",
                "sha256": file_sha256(output_path),
            },
            "config_json": {
                "path": "config.json",
                "sha256": file_sha256(config_path),
            },
        },
        "produced_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return write_manifest(manifest, run_dir / "manifest.json")


def produce_from_existing_run(run_dir: Path) -> dict[str, Any]:
    """Build or refresh a manifest for an existing local research-run bundle.

    Requires ``config.json`` and ``output.json`` written by a prior produce, or
    an existing complete ``manifest.json`` that is re-validated and re-fingerprinted.
    """

    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    config_path = run_dir / "config.json"
    output_path = run_dir / "output.json"

    if manifest_path.exists() and not config_path.exists():
        # Re-validate and re-stamp fingerprint only.
        return write_manifest(load_manifest(manifest_path), manifest_path)

    if not config_path.exists() or not output_path.exists():
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            f"existing run requires config.json and output.json under {run_dir}",
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        outputs = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST, f"run artifacts are not valid JSON: {error}"
        ) from error
    if not isinstance(config, dict) or not isinstance(outputs, dict):
        raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "config/output must be objects")

    # Prefer re-running produce when we have enough config to do so — ensures
    # checksums match live data. If data paths are missing, fail closed.
    strategy_id = str(config.get("strategy_id") or "")
    symbol = str(config.get("symbol") or "")
    data_dir = Path(str(config.get("data_dir") or ""))
    policy_path = Path(str(config.get("policy_path") or ""))
    if not strategy_id or not symbol or not data_dir or not policy_path:
        raise ReproError(
            CompareReason.INCOMPLETE_MANIFEST,
            "config.json missing strategy_id/symbol/data_dir/policy_path for re-produce",
        )
    return produce_named_backtest_run(
        run_dir=run_dir,
        strategy_id=strategy_id,
        symbol=symbol,
        data_dir=data_dir,
        policy_path=policy_path,
        initial_cash=float(config.get("initial_cash_usd", 3000.0)),
        slippage_bps=float(config.get("slippage_bps_per_side", 2.0)),
        date_start=config.get("date_start"),
        date_end=config.get("date_end"),
        seed=int(config.get("seed", 0)),
        timezone=str(config.get("timezone") or DEFAULT_TIMEZONE),
    )


def verify_datasets(manifest: Mapping[str, Any], *, base_dir: Path | None = None) -> None:
    """Fail closed if any declared dataset is missing or checksum-drifted."""

    base = base_dir or Path.cwd()
    for entry in manifest["datasets"]:
        if not isinstance(entry, Mapping):
            raise ReproError(CompareReason.INCOMPLETE_MANIFEST, "dataset entry must be an object")
        rel = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        if not rel or not expected:
            raise ReproError(
                CompareReason.INCOMPLETE_MANIFEST, "dataset entry missing path or sha256"
            )
        path = Path(rel)
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            raise ReproError(CompareReason.MISSING_DATA, f"dataset missing: {path}")
        actual = file_sha256(path)
        if actual != expected:
            raise ReproError(
                CompareReason.CHECKSUM_DRIFT,
                f"dataset {entry.get('dataset_id', rel)} sha256 drift: "
                f"expected={expected} actual={actual}",
            )


def replay_from_manifest(
    manifest: Mapping[str, Any],
    *,
    run_dir: Path,
    data_dir: Path | None = None,
    policy_path: Path | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Recompute the supported deterministic slice using manifest inputs."""

    _require_complete(manifest)
    if manifest.get("config", {}).get("slice") not in {None, "named_backtest"}:
        # Only named_backtest is supported in this milestone; anything else is legacy/unsupported.
        slice_name = manifest.get("config", {}).get("slice")
        if slice_name not in {"named_backtest"}:
            raise ReproError(
                CompareReason.UNSUPPORTED_LEGACY,
                f"unsupported run slice {slice_name!r}; only named_backtest is replayable",
            )

    config = dict(manifest["config"])
    strategy_id = str(manifest["strategy_id"])
    symbol = str(config.get("symbol") or "")
    if not symbol and manifest.get("datasets"):
        symbol = str(manifest["datasets"][0].get("symbol") or "")
    resolved_data = data_dir or Path(str(config.get("data_dir") or "research/data/raw"))
    default_policy = "config/risk.research.yaml"
    resolved_policy = policy_path or Path(str(config.get("policy_path") or default_policy))

    verify_datasets(manifest, base_dir=Path.cwd())

    # Use only the recorded config date bounds (do not inject date_window values —
    # those may be observed bar edges from a full-file run and would rewrite config).
    raw_start = config.get("date_start")
    raw_end = config.get("date_end")
    date_start = str(raw_start) if raw_start not in (None, "") else None
    date_end = str(raw_end) if raw_end not in (None, "") else None

    return produce_named_backtest_run(
        run_dir=run_dir,
        strategy_id=strategy_id,
        symbol=symbol,
        data_dir=resolved_data,
        policy_path=resolved_policy,
        initial_cash=float(config.get("initial_cash_usd", 3000.0)),
        slippage_bps=float(config.get("slippage_bps_per_side", 2.0)),
        date_start=date_start,
        date_end=date_end,
        seed=int(manifest["seed"]),
        timezone=str(manifest.get("timezone") or DEFAULT_TIMEZONE),
        code_commit=code_commit if code_commit is not None else str(manifest["code_commit"]),
    )


@dataclass(frozen=True, slots=True)
class CompareFinding:
    reason: CompareReason
    field: str
    expected: object
    actual: object
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CompareReport:
    status: CompareStatus
    reasons: tuple[CompareReason, ...]
    findings: tuple[CompareFinding, ...]
    detail: str

    @property
    def ok(self) -> bool:
        return self.status is CompareStatus.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "reasons": [r.value for r in self.reasons],
            "findings": [f.to_dict() for f in self.findings],
            "detail": self.detail,
        }


def compare_manifests(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    require_same_commit: bool = False,
) -> CompareReport:
    """Compare two validated manifests; return precise pass/fail reasons.

    Input identity (datasets, config, seed, timezone, strategy, policy, window)
    must match. Output fingerprint must match for a PASS (proves determinism).
    ``code_commit`` drift is reported but does not fail unless
    ``require_same_commit`` is true (replay on a later commit may still match
    outputs if the research code path is unchanged).
    """

    findings: list[CompareFinding] = []

    def note(
        reason: CompareReason,
        field: str,
        exp: object,
        act: object,
        detail: str,
    ) -> None:
        findings.append(
            CompareFinding(reason=reason, field=field, expected=exp, actual=act, detail=detail)
        )

    try:
        _require_complete(expected)
    except ReproError as error:
        note(error.reason, "expected", None, None, str(error))
        return CompareReport(
            status=CompareStatus.FAIL,
            reasons=(error.reason,),
            findings=tuple(findings),
            detail=str(error),
        )
    try:
        _require_complete(actual)
    except ReproError as error:
        note(error.reason, "actual", None, None, str(error))
        return CompareReport(
            status=CompareStatus.FAIL,
            reasons=(error.reason,),
            findings=tuple(findings),
            detail=str(error),
        )

    if (
        expected["strategy_id"] != actual["strategy_id"]
        or expected["strategy_version"] != actual["strategy_version"]
    ):
        note(
            CompareReason.STRATEGY_DRIFT,
            "strategy",
            {
                "id": expected["strategy_id"],
                "version": expected["strategy_version"],
            },
            {"id": actual["strategy_id"], "version": actual["strategy_version"]},
            "strategy id/version differ",
        )

    if int(expected["seed"]) != int(actual["seed"]):
        note(
            CompareReason.SEED_DRIFT,
            "seed",
            expected["seed"],
            actual["seed"],
            "random seed differs",
        )

    exp_tz = normalize_timezone(str(expected.get("timezone")))
    act_tz = normalize_timezone(str(actual.get("timezone")))
    if exp_tz != act_tz:
        note(
            CompareReason.TIMEZONE_DRIFT,
            "timezone",
            exp_tz,
            act_tz,
            "timezone identity differs after UTC normalization",
        )

    if expected.get("date_window") != actual.get("date_window"):
        note(
            CompareReason.DATE_WINDOW_DRIFT,
            "date_window",
            expected.get("date_window"),
            actual.get("date_window"),
            "requested date window differs",
        )

    if expected.get("policy_hash") != actual.get("policy_hash") or expected.get(
        "policy_version"
    ) != actual.get("policy_version"):
        note(
            CompareReason.POLICY_DRIFT,
            "policy",
            {
                "version": expected.get("policy_version"),
                "hash": expected.get("policy_hash"),
            },
            {
                "version": actual.get("policy_version"),
                "hash": actual.get("policy_hash"),
            },
            "risk policy version/hash differ",
        )

    # Config compare uses config_hash first (covers redacted normalized config).
    if expected.get("config_hash") != actual.get("config_hash"):
        note(
            CompareReason.CONFIG_DRIFT,
            "config_hash",
            expected.get("config_hash"),
            actual.get("config_hash"),
            "normalized non-secret configuration hash differs",
        )
    elif expected.get("config") != actual.get("config"):
        note(
            CompareReason.CONFIG_DRIFT,
            "config",
            expected.get("config"),
            actual.get("config"),
            "normalized configuration objects differ",
        )

    # Dataset id + checksum.
    exp_ds = {
        str(d.get("dataset_id") or d.get("path")): d
        for d in expected.get("datasets", [])
        if isinstance(d, Mapping)
    }
    act_ds = {
        str(d.get("dataset_id") or d.get("path")): d
        for d in actual.get("datasets", [])
        if isinstance(d, Mapping)
    }
    if set(exp_ds) != set(act_ds):
        note(
            CompareReason.MISSING_DATA,
            "datasets",
            sorted(exp_ds),
            sorted(act_ds),
            "dataset identifier sets differ",
        )
    for key in sorted(set(exp_ds) & set(act_ds)):
        if exp_ds[key].get("sha256") != act_ds[key].get("sha256"):
            note(
                CompareReason.CHECKSUM_DRIFT,
                f"datasets[{key}].sha256",
                exp_ds[key].get("sha256"),
                act_ds[key].get("sha256"),
                f"input dataset checksum drift for {key}",
            )

    if expected.get("code_commit") != actual.get("code_commit"):
        note(
            CompareReason.COMMIT_DRIFT,
            "code_commit",
            expected.get("code_commit"),
            actual.get("code_commit"),
            "git commit differs",
        )

    if expected.get("output_fingerprint") != actual.get("output_fingerprint"):
        # Same inputs → nondeterminism; different inputs already flagged above.
        input_findings = [
            f
            for f in findings
            if f.reason
            in {
                CompareReason.CHECKSUM_DRIFT,
                CompareReason.CONFIG_DRIFT,
                CompareReason.SEED_DRIFT,
                CompareReason.DATE_WINDOW_DRIFT,
                CompareReason.STRATEGY_DRIFT,
                CompareReason.POLICY_DRIFT,
                CompareReason.MISSING_DATA,
            }
        ]
        reason = CompareReason.OUTPUT_DRIFT if input_findings else CompareReason.NONDETERMINISM
        note(
            reason,
            "output_fingerprint",
            expected.get("output_fingerprint"),
            actual.get("output_fingerprint"),
            "output artifact fingerprint differs",
        )

    # Commit-only drift is advisory unless require_same_commit is set.
    if require_same_commit:
        blocking = [f.reason for f in findings]
    else:
        blocking = [f.reason for f in findings if f.reason is not CompareReason.COMMIT_DRIFT]

    if not blocking:
        reasons: tuple[CompareReason, ...]
        if findings:
            reasons = tuple(dict.fromkeys(f.reason for f in findings))
            detail = "outputs match; advisory: " + ", ".join(r.value for r in reasons)
        else:
            reasons = (CompareReason.MATCH,)
            detail = "manifests match (inputs and output fingerprint)"
        return CompareReport(
            status=CompareStatus.PASS,
            reasons=reasons if findings else (CompareReason.MATCH,),
            findings=tuple(findings),
            detail=detail if findings else "manifests match (inputs and output fingerprint)",
        )

    unique = tuple(dict.fromkeys(blocking))
    return CompareReport(
        status=CompareStatus.FAIL,
        reasons=unique,
        findings=tuple(findings),
        detail="compare failed: " + ", ".join(r.value for r in unique),
    )

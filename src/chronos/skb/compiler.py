"""SKB compiler (AI Quant plan B1).

Joins the five corpus inputs into one validated :class:`SKBStore`:

- ``research/strategy_registry.yaml`` — 42 Pine scripts (identity + forensic flags)
- ``research/pine_findings.json``     — 42 forensic findings (family/direction/…)
- ``specs/*.yaml``                    — canonical Python derivations
- ``research/selection_manifest.json``— the frozen selection criteria
- ``research/results/research_all.json`` — backtest results (all partitions)

Fails closed on any unjoinable record: the registry↔findings join must be a
complete 1:1 over the corpus, every spec Pine-reference must resolve to a real
registry entry, and every categorical must be in the controlled vocabulary
(``extra="forbid"`` + enum validation). The store is a deterministic function of
its inputs — no timestamps are generated — and carries a ``corpus_hash`` plus
per-input SHA-256s so a drift in any source is detectable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chronos.skb.disposition import classify_disposition
from chronos.skb.schema import (
    DerivedStrategy,
    PineForensicFlags,
    PineScriptEntry,
    SelectionContext,
    SKBStore,
    SourceHashes,
    StrategyResult,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PATH = Path("research/skb/skb.json")
CATALOG_PATH = Path("research/skb/CATALOG.md")


class SKBCompileError(RuntimeError):
    """A corpus record could not be joined or validated; the compile fails closed."""


def _basename(name: str) -> str:
    return Path(name).name


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _flags(script: dict[str, Any]) -> PineForensicFlags:
    return PineForensicFlags(
        uses_request_security=bool(script.get("uses_request_security", False)),
        uses_lookahead_on=bool(script.get("uses_lookahead_on", False)),
        uses_barstate_isconfirmed=bool(script.get("uses_barstate_isconfirmed", False)),
        uses_calc_on_every_tick_true=bool(script.get("uses_calc_on_every_tick_true", False)),
        uses_process_orders_on_close_true=bool(
            script.get("uses_process_orders_on_close_true", False)
        ),
        uses_strategy_orders=bool(script.get("uses_strategy_orders", False)),
        pyramiding_gt_0=bool(script.get("pyramiding_gt_0", False)),
    )


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if not isinstance(value, list):
        raise SKBCompileError(f"expected a list of strings, got {type(value).__name__}")
    return tuple(str(item) for item in value)


def compile_skb(root: Path = REPO_ROOT) -> SKBStore:
    """Compile the SKB from the corpus rooted at ``root``. Fails closed."""

    registry_path = root / "research/strategy_registry.yaml"
    findings_path = root / "research/pine_findings.json"
    manifest_path = root / "research/selection_manifest.json"
    results_path = root / "research/results/research_all.json"
    specs_dir = root / "specs"

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    findings_doc = json.loads(findings_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results_doc = json.loads(results_path.read_text(encoding="utf-8"))

    scripts: list[dict[str, Any]] = registry["scripts"]
    findings: list[dict[str, Any]] = findings_doc["findings"]

    # --- registry <-> findings join, on normalized basename, must be 1:1 ------
    registry_by_base = {_basename(s["filename"]): s for s in scripts}
    findings_by_base = {_basename(f["filename"]): f for f in findings}
    if len(registry_by_base) != len(scripts):
        raise SKBCompileError("registry has duplicate filenames after basename normalization")
    if len(findings_by_base) != len(findings):
        raise SKBCompileError("findings have duplicate filenames after basename normalization")
    only_registry = set(registry_by_base) - set(findings_by_base)
    only_findings = set(findings_by_base) - set(registry_by_base)
    if only_registry or only_findings:
        raise SKBCompileError(
            "registry/findings do not join 1:1 — "
            f"registry-only={sorted(only_registry)} findings-only={sorted(only_findings)}"
        )

    # --- specs: which catalog numbers are ported, and the derived strategies --
    spec_docs: list[dict[str, Any]] = []
    ported_catalog_numbers: set[str] = set()
    for spec_path in sorted(specs_dir.glob("*.yaml")):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise SKBCompileError(f"{spec_path.name}: spec must be a mapping")
        spec["__path__"] = spec_path.name
        spec_docs.append(spec)
        for ref in spec.get("pine_references", ()) or ():
            catalog_number = str(ref["catalog_number"])
            if catalog_number not in {str(s["catalog_number"]) for s in scripts}:
                raise SKBCompileError(
                    f"{spec_path.name}: pine_reference catalog_number "
                    f"{catalog_number!r} is not in the registry"
                )
            ported_catalog_numbers.add(catalog_number)

    # --- build the Pine-script entries (sorted by catalog number) -------------
    pine_scripts: list[PineScriptEntry] = []
    for script in sorted(scripts, key=lambda s: str(s["catalog_number"])):
        base = _basename(script["filename"])
        finding = findings_by_base[base]
        catalog_number = str(script["catalog_number"])
        try:
            entry = PineScriptEntry(
                catalog_number=catalog_number,
                filename=base,
                title=str(script["title"]),
                sha256=str(script["sha256"]),
                bytes=int(script["bytes"]),
                lines=int(script["lines"]),
                pine_version=str(script["pine_version"]),
                declaration=str(script["declaration"]),
                cycle=str(script.get("cycle", "")),
                classification=finding["classification"],
                strategy_family=finding["strategy_family"],
                direction=finding["direction"],
                integrity_status=finding["integrity_status"],
                confidence=finding["confidence"],
                deterministic_translation_feasibility=str(
                    finding["deterministic_translation_feasibility"]
                ),
                known_defects=_tuple_of_str(finding.get("known_defects")),
                related_scripts=_tuple_of_str(finding.get("related_scripts")),
                forensic_flags=_flags(script),
            )
        except (ValidationError, KeyError) as error:
            raise SKBCompileError(
                f"catalog {catalog_number} ({base}) failed schema validation: {error}"
            ) from error
        disposition, reason, detail = classify_disposition(
            entry, is_ported=catalog_number in ported_catalog_numbers
        )
        entry = entry.model_copy(
            update={
                "disposition": disposition,
                "disposition_reason": reason,
                "disposition_detail": detail,
            }
        )
        pine_scripts.append(entry)

    # --- build the derived strategies -----------------------------------------
    candidates = set(_tuple_of_str(manifest.get("candidates")))
    runs: list[dict[str, Any]] = results_doc.get("runs", [])
    derived: list[DerivedStrategy] = []
    for spec in sorted(spec_docs, key=lambda s: str(s["strategy_id"])):
        strategy_id = str(spec["strategy_id"])
        results = _strategy_results(strategy_id, runs)
        try:
            derived.append(
                DerivedStrategy(
                    strategy_id=strategy_id,
                    version=str(spec["version"]),
                    family=str(spec["family"]),
                    status=str(spec.get("status", "draft")),
                    long_only=bool(spec.get("long_only", True)),
                    supported_symbols=_tuple_of_str(spec.get("supported_symbols")),
                    supported_intervals=_tuple_of_str(spec.get("supported_intervals")),
                    derived_from_catalog_numbers=tuple(
                        sorted(
                            str(ref["catalog_number"])
                            for ref in spec.get("pine_references", ()) or ()
                        )
                    ),
                    selection_candidate=strategy_id in candidates,
                    results=results,
                )
            )
        except ValidationError as error:
            raise SKBCompileError(
                f"derived strategy {strategy_id} failed schema validation: {error}"
            ) from error

    selection_context = _selection_context(manifest)

    corpus_hash = _corpus_hash(scripts)
    source_hashes = SourceHashes(
        strategy_registry_yaml=_sha256_file(registry_path),
        pine_findings_json=_sha256_file(findings_path),
        selection_manifest_json=_sha256_file(manifest_path),
        results_json={results_path.name: _sha256_file(results_path)},
        spec_yaml={path.name: _sha256_file(path) for path in sorted(specs_dir.glob("*.yaml"))},
    )

    return SKBStore(
        corpus_hash=corpus_hash,
        source_hashes=source_hashes,
        pine_script_count=len(pine_scripts),
        derived_strategy_count=len(derived),
        pine_scripts=tuple(pine_scripts),
        derived_strategies=tuple(derived),
        selection_context=selection_context,
    )


def _strategy_results(strategy_id: str, runs: list[dict[str, Any]]) -> tuple[StrategyResult, ...]:
    out: list[StrategyResult] = []
    for run in runs:
        if str(run.get("strategy")) != strategy_id:
            continue
        metrics = run.get("metrics", {})
        out.append(
            StrategyResult(
                partition=str(run.get("tag", "")),
                symbol=str(run.get("symbol", "")),
                trades=int(metrics.get("trades", 0) or 0),
                win_rate=_opt_float(metrics.get("win_rate")),
                profit_factor=_opt_float(metrics.get("profit_factor")),
                total_return_fraction=_opt_float(metrics.get("total_return_fraction")),
                max_drawdown_fraction=_opt_float(metrics.get("max_drawdown_fraction")),
                sharpe=_opt_float(metrics.get("sharpe")),
            )
        )
    out.sort(key=lambda r: (r.partition, r.symbol, r.total_return_fraction or 0.0))
    return tuple(out)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _selection_context(manifest: dict[str, Any]) -> SelectionContext:
    return SelectionContext(
        purpose=str(manifest["purpose"]),
        frozen_at_utc=str(manifest["frozen_at_utc"]),
        re_frozen_at_utc=(
            str(manifest["re_frozen_at_utc"]) if manifest.get("re_frozen_at_utc") else None
        ),
        candidates=_tuple_of_str(manifest.get("candidates")),
        baselines=_tuple_of_str(manifest.get("baselines")),
        criteria_for_backtest_validated=_tuple_of_str(
            manifest.get("criteria_for_backtest_validated")
        ),
        portfolio_criterion=(
            str(manifest["portfolio_criterion"]) if manifest.get("portfolio_criterion") else None
        ),
    )


def _corpus_hash(scripts: list[dict[str, Any]]) -> str:
    """A deterministic hash of the corpus identity: sorted (catalog, sha256)."""

    pairs = sorted((str(s["catalog_number"]), str(s["sha256"])) for s in scripts)
    payload = json.dumps(pairs, separators=(",", ":"), sort_keys=True)
    return _sha256_bytes(payload.encode("utf-8"))


def store_to_json(store: SKBStore) -> str:
    """Deterministic JSON serialization (sorted keys, stable separators)."""

    return json.dumps(store.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_store(store: SKBStore, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store_to_json(store), encoding="utf-8")


def load_store(path: Path) -> SKBStore:
    return SKBStore.model_validate_json(path.read_text(encoding="utf-8"))

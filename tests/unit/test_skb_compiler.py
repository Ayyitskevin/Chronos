"""SKB compiler + schema tests (AI Quant plan B1).

Proves the join is complete over the whole corpus, fails closed on unjoinable
records and out-of-vocabulary categoricals, derives PORTED disposition from the
specs, is deterministic + hash-pinned, and that the committed store is current.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from chronos.skb.compiler import (
    REPO_ROOT,
    STORE_PATH,
    SKBCompileError,
    compile_skb,
    load_store,
    store_to_json,
    write_store,
)
from chronos.skb.schema import Disposition, PineForensicFlags, PineScriptEntry, StrategyFamily

# Golden pin: the corpus identity hash. If this changes, the Pine corpus changed
# — regenerate `research/skb/skb.json` and review the diff; do NOT edit this pin
# without a corresponding, reviewed corpus change.
_GOLDEN_CORPUS_HASH = "94482faffd7205055363beb209b0ee86611a3588917b3637490f5af79fe67d8a"


def _copy_corpus(dst: Path) -> Path:
    """Copy the five SKB inputs into a fresh root for mutation tests."""

    for rel in (
        "research/strategy_registry.yaml",
        "research/pine_findings.json",
        "research/selection_manifest.json",
        "research/results/research_all.json",
    ):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, target)
    specs_dst = dst / "specs"
    specs_dst.mkdir(parents=True, exist_ok=True)
    for spec in (REPO_ROOT / "specs").glob("*.yaml"):
        shutil.copy(spec, specs_dst / spec.name)
    return dst


def _first_strategy_spec(root: Path) -> Path:
    for path in sorted((root / "specs").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if document.get("document_kind", "strategy_spec") == "strategy_spec":
            return path
    raise AssertionError("test corpus has no executable strategy spec")


def test_compile_joins_the_full_corpus() -> None:
    store = compile_skb()
    assert store.pine_script_count == 42
    assert len(store.pine_scripts) == 42
    assert store.derived_strategy_count == 2
    catalog_numbers = [p.catalog_number for p in store.pine_scripts]
    assert len(set(catalog_numbers)) == 42  # unique
    assert catalog_numbers == sorted(catalog_numbers)  # deterministic order


def test_basename_normalization_joins_path_prefixed_findings() -> None:
    # 12 findings carry a `research/pine/...` path form; catalog 06 is one of
    # them, and it must still join (its family comes from the finding).
    store = compile_skb()
    by_catalog = {p.catalog_number: p for p in store.pine_scripts}
    assert "06" in by_catalog
    assert by_catalog["06"].filename == "06_noise_area_bands.pine"
    # A finding-sourced field is populated, proving the join landed.
    assert by_catalog["06"].strategy_family in set(StrategyFamily)


def test_ported_disposition_is_derived_from_specs() -> None:
    store = compile_skb()
    by_catalog = {p.catalog_number: p for p in store.pine_scripts}
    # The two specs derive catalog 01 and 11; PORTED wins over any other rule.
    assert by_catalog["01"].disposition is Disposition.PORTED
    assert by_catalog["11"].disposition is Disposition.PORTED
    ported = {p.catalog_number for p in store.pine_scripts if p.disposition is Disposition.PORTED}
    assert ported == {"01", "11"}
    # After the B2 backfill no script is left UNCLASSIFIED (see
    # test_skb_disposition for the full deterministic split).
    assert all(p.disposition is not Disposition.UNCLASSIFIED for p in store.pine_scripts)


def test_derived_strategies_carry_candidacy_and_results() -> None:
    store = compile_skb()
    by_id = {d.strategy_id: d for d in store.derived_strategies}
    assert set(by_id) == {"regime_trend_v1", "mean_reversion_v1"}
    rt = by_id["regime_trend_v1"]
    assert rt.selection_candidate is True
    assert rt.derived_from_catalog_numbers == ("01",)
    assert rt.results  # backtest runs attached
    # Every attached run carries a partition tag and a symbol.
    assert all(r.partition and r.symbol for r in rt.results)
    # The once-run final-test partition is present (research_all is the superset).
    assert any(r.partition == "final" for r in rt.results)


def test_deterministic_and_hash_pinned() -> None:
    first = compile_skb()
    second = compile_skb()
    assert first.corpus_hash == _GOLDEN_CORPUS_HASH
    assert store_to_json(first) == store_to_json(second)  # byte-identical


def test_committed_store_is_current() -> None:
    # The committed store must equal a fresh compile — the CI guard that a corpus
    # change is never silently un-regenerated (mirrors `build_skb.py --check`).
    committed = (REPO_ROOT / STORE_PATH).read_text(encoding="utf-8")
    assert committed == store_to_json(compile_skb())


def test_store_roundtrips(tmp_path: Path) -> None:
    store = compile_skb()
    path = tmp_path / "skb.json"
    write_store(store, path)
    assert load_store(path) == store


def test_controlled_vocabulary_rejects_out_of_vocab_family() -> None:
    with pytest.raises(ValidationError):
        PineScriptEntry(
            catalog_number="99",
            filename="x.pine",
            title="x",
            sha256="0" * 64,
            bytes=1,
            lines=1,
            pine_version="6",
            declaration="strategy",
            cycle="",
            classification="strategy",
            strategy_family="NOT_A_REAL_FAMILY",  # out of vocabulary
            direction="long",
            integrity_status="PASS_WITH_CONSTRAINTS",
            confidence="high",
            deterministic_translation_feasibility="x",
            forensic_flags=PineForensicFlags(
                uses_request_security=False,
                uses_lookahead_on=False,
                uses_barstate_isconfirmed=False,
                uses_calc_on_every_tick_true=False,
                uses_process_orders_on_close_true=False,
                uses_strategy_orders=False,
                pyramiding_gt_0=False,
            ),
        )


def test_fail_closed_on_unjoinable_finding(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    findings_path = root / "research/pine_findings.json"
    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    doc["findings"].append({**doc["findings"][0], "filename": "99_phantom.pine"})
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SKBCompileError, match="do not join 1:1"):
        compile_skb(root)


def test_fail_closed_on_out_of_vocab_finding(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    findings_path = root / "research/pine_findings.json"
    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    doc["findings"][0]["strategy_family"] = "totally_made_up_family"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SKBCompileError, match="failed schema validation"):
        compile_skb(root)


def test_fail_closed_on_unresolvable_spec_reference(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    spec_path = _first_strategy_spec(root)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["pine_references"] = [{"catalog_number": "77", "filename": "77_ghost.pine", "sha256": "z"}]
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(SKBCompileError, match="not in the registry"):
        compile_skb(root)

"""Build the Phase 1 strategy registry from the fetched Pine corpus.

Deterministic static extraction only — no interpretation. Semantic findings
live in research/pine_findings.json (Phase 2 audit). Outputs:

- research/strategy_registry.yaml
- research/strategy_catalog.csv
- research/strategy_catalog.json

Usage: .venv/bin/python scripts/build_strategy_registry.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PINE_DIR = ROOT / "research" / "pine"

# Catalog metadata from the Notion Master Index (fetched 2026-07-17).
CATALOG: dict[str, dict[str, str]] = {
    "00": {"title": "Five-Tool Confluence AIO v3.6", "cycle": "flagship"},
    "01": {"title": "Markov Regime BULL+ v1.1", "cycle": "1"},
    "02": {"title": "Markov Regime BEAR+ v1.1", "cycle": "1"},
    "03": {"title": "KLQuant Shared Library v1.1", "cycle": "foundation"},
    "04": {"title": "SIP RVOL Screener v1.1", "cycle": "2"},
    "05": {"title": "Session Structure & ORB v1.1", "cycle": "3-4"},
    "06": {"title": "Noise-Area Bands v1.1", "cycle": "5"},
    "07": {"title": "Squeeze & Exhaustion Sentinel v1.1", "cycle": "6"},
    "08": {"title": "Gap & Overnight Risk Classifier v1.1", "cycle": "7"},
    "09": {"title": "Breadth & Internals Proxy v1.1", "cycle": "8"},
    "10": {"title": "Volume Profile Lite v1.1", "cycle": "9"},
    "11": {"title": "Mean-Reversion Extremes Study v1.1", "cycle": "10"},
    "12": {"title": "Expectancy Journal Module v1.1", "cycle": "all"},
    "13": {"title": "SMC / ICT Concepts Study v1.1", "cycle": "11"},
    "14": {"title": "Fibonacci Confluence Mapper v1.0", "cycle": "desk-kit"},
    "15": {"title": "Buffett Desk Lens v1.0", "cycle": "desk-kit"},
    "16": {"title": "Pullback-to-Value Playbook v1.0", "cycle": "desk-kit"},
    "17": {"title": "HMM Regime Probability Filter v1.0", "cycle": "qg-t1"},
    "18": {"title": "Volatility Regime Switching — Markov Chain v1.0", "cycle": "qg-t1"},
    "19": {"title": "Markov Property Regime Persistence Gauge v1.0", "cycle": "qg-t1"},
    "20": {"title": "Adaptive Regime Transition Probability Bands v1.0", "cycle": "qg-t1"},
    "21": {"title": "Kalman Filter MR Detector v1.0", "cycle": "qg-t1"},
    "22": {"title": "Kelly Criterion Sizing Overlay v1.0", "cycle": "qg-t1"},
    "23": {"title": "Regime-Dependent Vol Clustering Overlay v1.0", "cycle": "qg-t2"},
    "24": {"title": "Dynamic Squeeze Exhaustion w/ Regime Context v1.0", "cycle": "qg-t2"},
    "25": {"title": "Walk-Forward Regime Stability Analyzer v1.0", "cycle": "qg-t2"},
    "26": {"title": "Time-of-Day RVOL Regime Filter v1.0", "cycle": "qg-t2"},
    "27": {"title": "Profitable vs Tradable Robustness Tester v1.0", "cycle": "qg-t2"},
    "28": {"title": "Backtest Realism Simulator — Cost Stress v1.0", "cycle": "qg-t2"},
    "29": {"title": "Regime-Aware Risk Parity Allocator v1.0", "cycle": "qg-t2"},
    "30": {"title": "Drawdown-Controlled Regime Exit Engine v1.0", "cycle": "qg-t2"},
    "31": {"title": "IV Regime Detector & Crush Proxy v1.0", "cycle": "qg-t3"},
    "32": {"title": "Tail-Risk Volatility Regime Filter v1.0", "cycle": "qg-t3"},
    "33": {"title": "Overnight Gap Regime Transition Detector v1.0", "cycle": "qg-t3"},
    "34": {"title": "Session Structure w/ Markov Probability Bands v1.0", "cycle": "qg-t3"},
    "35": {"title": "Statistical Edge Validation Dashboard v1.0", "cycle": "qg-t3"},
    "36": {"title": "Regime-Conditioned Kalman MR v1.0", "cycle": "qg-t3"},
    "37": {"title": "Adaptive MR w/ Regime Probability v1.0", "cycle": "qg-t3"},
    "38": {"title": "Value Zone Reversion Confluence Scanner v1.0", "cycle": "qg-t3"},
    "39": {
        "title": "Monte Carlo Option-Inspired Equity Probability Bands v1.0",
        "cycle": "qg-t3",
    },
    "40": {
        "title": "Black-Scholes Regime-Adjusted Implied Edge Gauge v1.0",
        "cycle": "qg-t3",
    },
    "0A": {"title": "Confluence Swing Strategy v1.0 (ARCHIVED)", "cycle": "archived"},
}

_DECL = re.compile(r"^(indicator|strategy|library)\s*\(", re.MULTILINE)
_TITLE = re.compile(r"""(?:indicator|strategy|library)\s*\(\s*\n?\s*['"]([^'"]+)['"]""")


def _flag(source: str, pattern: str) -> bool:
    return re.search(pattern, source) is not None


def analyze(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    source = raw.decode("utf-8", errors="replace")
    number = path.name.split("_", 1)[0]
    decl_match = _DECL.search(source)
    title_match = _TITLE.search(source)
    version_match = re.search(r"//@version=(\d+)", source)
    strategy_args = ""
    if decl_match and decl_match.group(1) == "strategy":
        open_index = source.find("(", decl_match.start())
        strategy_args = source[open_index : source.find(")", open_index) + 1].replace("\n", " ")

    inputs = len(re.findall(r"\binput(?:\.\w+)?\s*\(", source))
    return {
        "catalog_number": number,
        "title": CATALOG.get(number, {}).get("title", "UNKNOWN — not in master index"),
        "path": str(path.relative_to(ROOT)),
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": source.count("\n") + 1,
        "pine_version": version_match.group(1) if version_match else "unknown",
        "declaration": decl_match.group(1) if decl_match else "unknown",
        "declared_title": title_match.group(1) if title_match else "",
        "input_count": inputs,
        "uses_request_security": _flag(source, r"request\.security"),
        "uses_lookahead_on": _flag(source, r"lookahead\s*=\s*barmerge\.lookahead_on"),
        "uses_barstate_isconfirmed": _flag(source, r"barstate\.isconfirmed"),
        "uses_barstate_isrealtime": _flag(source, r"barstate\.isrealtime"),
        "uses_calc_on_every_tick_true": _flag(source, r"calc_on_every_tick\s*=\s*true"),
        "uses_process_orders_on_close_true": _flag(source, r"process_orders_on_close\s*=\s*true"),
        "uses_bar_magnifier": _flag(source, r"use_bar_magnifier\s*=\s*true"),
        "uses_varip": _flag(source, r"\bvarip\b"),
        "uses_arrays": _flag(source, r"\barray\.\w+"),
        "uses_matrices": _flag(source, r"\bmatrix\.\w+"),
        "uses_alerts": _flag(source, r"\balert(condition)?\s*\("),
        "uses_session_functions": _flag(source, r"\bsession\.|time\s*\(\s*timeframe"),
        "uses_strategy_orders": _flag(source, r"strategy\.(entry|order|exit|close)\s*\("),
        "pyramiding_gt_0": _flag(source, r"pyramiding\s*=\s*[1-9]"),
        "commission_declared": _flag(source, r"commission_value\s*="),
        "slippage_declared": _flag(source, r"slippage\s*="),
        "strategy_declaration_args": strategy_args[:600],
        "cycle": CATALOG.get(number, {}).get("cycle", ""),
    }


def main() -> int:
    entries = [analyze(path) for path in sorted(PINE_DIR.glob("*.pine"))]
    missing = sorted(set(CATALOG) - {str(e["catalog_number"]) for e in entries})
    registry = {
        "generated_by": "scripts/build_strategy_registry.py",
        "source_of_truth": (
            "Notion: Command Center > Trading Library > Pine Quant Library — Master Index"
        ),
        "corpus_note": (
            "The build brief described ~77 Pine scripts; the authoritative Master Index "
            "catalogs 42 artifacts (00-40 plus archived 0A). See ASSUMPTIONS.md A-01."
        ),
        "script_count": len(entries),
        "missing_from_disk": missing,
        "scripts": entries,
    }
    research = ROOT / "research"
    (research / "strategy_registry.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    (research / "strategy_catalog.json").write_text(
        json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (research / "strategy_catalog.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(entries[0].keys()))
        writer.writeheader()
        writer.writerows(entries)
    print(f"registry written: {len(entries)} scripts, missing from disk: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

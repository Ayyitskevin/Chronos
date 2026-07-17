"""Merge the registry and audit findings into the Phase 1/2 documents.

Inputs:  research/strategy_registry.yaml, research/pine_findings.json
Outputs: docs/STRATEGY_CATALOG.md, docs/PINE_AUDIT.md

Usage: .venv/bin/python scripts/build_audit_docs.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    registry = yaml.safe_load(
        (ROOT / "research" / "strategy_registry.yaml").read_text(encoding="utf-8")
    )
    findings_raw = json.loads(
        (ROOT / "research" / "pine_findings.json").read_text(encoding="utf-8")
    )
    findings = {f["filename"].split("/")[-1]: f for f in findings_raw["findings"]}
    scripts = registry["scripts"]

    by_status = Counter()
    by_family = Counter()
    catalog_rows = []
    audit_sections = []
    for script in scripts:
        finding = findings.get(script["filename"], {})
        status = finding.get("integrity_status", "INSUFFICIENT_INFORMATION")
        family = finding.get("strategy_family", "unclassified")
        by_status[status] += 1
        by_family[family] += 1
        catalog_rows.append(
            f"| {script['catalog_number']} | {script['title']} | "
            f"{finding.get('classification', script['declaration'])} | {family} | "
            f"{finding.get('direction', '—')} | {script['pine_version']} | "
            f"{script['lines']} | `{status}` |"
        )
        lines = [
            f"### {script['catalog_number']} · {script['title']}",
            "",
            f"- File: `{script['path']}` · SHA-256 `{script['sha256'][:16]}…` · "
            f"{script['lines']} lines · Pine v{script['pine_version']} · "
            f"{script['input_count']} inputs",
            f"- Classification: **{finding.get('classification', script['declaration'])}**"
            f" · Family: **{family}** · Direction: {finding.get('direction', '—')}"
            f" · Confidence: {finding.get('confidence', '—')}",
            f"- Integrity: **`{status}`**",
            f"- Explanation: {finding.get('status_explanation', 'audit finding missing')}",
        ]
        for key, label in (
            ("timeframe_assumptions", "Timeframes"),
            ("symbol_assumptions", "Symbols"),
            ("external_data", "External data"),
            ("entry_logic_summary", "Entries"),
            ("exit_logic_summary", "Exits"),
            ("sizing_summary", "Sizing"),
            ("alert_behavior", "Alerts"),
            ("state_machine_notes", "State"),
            ("deterministic_translation_feasibility", "Translation feasibility"),
        ):
            value = finding.get(key)
            if value:
                lines.append(f"- {label}: {value}")
        for key, label in (
            ("repaint_risks", "Repaint risks"),
            ("lookahead_risks", "Look-ahead analysis"),
            ("session_timezone_risks", "Session/timezone"),
            ("tradingview_only_dependencies", "TradingView-only"),
            ("known_defects", "Known defects"),
            ("related_scripts", "Related scripts"),
        ):
            values = finding.get(key) or []
            if values:
                lines.append(f"- {label}:")
                lines.extend(f"  - {value}" for value in values)
        audit_sections.append("\n".join(lines))

    status_table = "\n".join(
        f"| `{status}` | {count} |" for status, count in by_status.most_common()
    )
    family_table = "\n".join(f"| {family} | {count} |" for family, count in by_family.most_common())

    catalog = f"""# Strategy Catalog — Pine Quant Library

Source of truth: Notion → Command Center → Trading Library → Pine Quant
Library — Master Index, fetched byte-exact on 2026-07-17 (hashes in
`research/strategy_registry.yaml`; verify with
`python -m chronos.cli verify-corpus`).

**Corpus size note:** the build brief described "approximately 77 Pine
scripts". The authoritative Master Index catalogs **{len(scripts)} artifacts**
(00-40 plus archived 0A). No other Pine sources exist in the Trading Library.
See ASSUMPTIONS.md A-01.

## Integrity status distribution

| Status | Count |
|---|---|
{status_table}

## Family distribution

| Family | Count |
|---|---|
{family_table}

## Catalog

| # | Title | Kind | Family | Direction | Pine | Lines | Integrity |
|---|-------|------|--------|-----------|------|-------|-----------|
{chr(10).join(catalog_rows)}

Detailed per-script findings: [PINE_AUDIT.md](PINE_AUDIT.md).
"""

    audit = f"""# Pine Forensic Audit (Phase 2)

Method: every script in `research/pine/` was read in full by a dedicated
audit agent against a fixed semantic checklist (request.security semantics,
barstate discipline, repainting, session/timezone, persistent state, order
semantics, alert discipline, executability, translation feasibility), with
findings returned in a structured schema and compiled by
`scripts/build_audit_docs.py`. Machine-readable findings:
`research/pine_findings.json`. Compilation on TradingView could NOT be
validated from this environment; compile status is `UNVERIFIED` for all
scripts (ASSUMPTIONS A-02).

Status meanings follow the build brief. `NON_EXECUTABLE_INDICATOR` is not a
defect: it marks tools with no trading rules.

## Summary

| Status | Count |
|---|---|
{status_table}

## Per-script findings

{chr(10).join(audit_sections)}
"""

    (ROOT / "docs" / "STRATEGY_CATALOG.md").write_text(catalog, encoding="utf-8")
    (ROOT / "docs" / "PINE_AUDIT.md").write_text(audit, encoding="utf-8")
    print(f"catalog + audit written for {len(scripts)} scripts; statuses: {dict(by_status)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

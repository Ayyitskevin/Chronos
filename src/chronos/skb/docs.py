"""Human-readable SKB catalog generation (AI Quant plan B2).

Renders the compiled store to a deterministic Markdown catalog
(``research/skb/CATALOG.md``) so the corpus's disposition is browsable without
loading the JSON. Pure function of the store — regenerated alongside it.
"""

from __future__ import annotations

from chronos.skb import query
from chronos.skb.schema import Disposition, SKBStore

_DISPOSITION_ORDER = (
    Disposition.PORTED,
    Disposition.DEFERRED,
    Disposition.BLOCKED_ON,
    Disposition.REJECTED,
    Disposition.UNCLASSIFIED,
)

_DISPOSITION_BLURB = {
    Disposition.PORTED: "translated to a canonical Python spec",
    Disposition.DEFERRED: "executable standalone strategy, portable, not yet ported",
    Disposition.BLOCKED_ON: "cannot be assessed/ported until a dependency clears",
    Disposition.REJECTED: "not a standalone tradable strategy",
    Disposition.UNCLASSIFIED: "not yet classified",
}


def render_catalog(store: SKBStore) -> str:
    lines: list[str] = []
    lines.append("# Strategy Knowledge Base — catalog")
    lines.append("")
    lines.append(
        "Generated from `research/skb/skb.json` by `scripts/build_skb.py` "
        "(`chronos.skb.docs`). Do not hand-edit — regenerate."
    )
    lines.append("")
    lines.append(f"- Pine scripts: **{store.pine_script_count}**")
    lines.append(f"- Derived strategies: **{store.derived_strategy_count}**")
    lines.append(f"- Corpus hash: `{store.corpus_hash}`")
    lines.append("")

    lines.append("## Dispositions")
    lines.append("")
    lines.append("| Disposition | Count | Meaning |")
    lines.append("|---|---:|---|")
    counts = query.disposition_counts(store)
    for disposition in _DISPOSITION_ORDER:
        n = counts.get(disposition.value, 0)
        if n:
            lines.append(f"| `{disposition.value}` | {n} | {_DISPOSITION_BLURB[disposition]} |")
    lines.append("")

    for disposition in _DISPOSITION_ORDER:
        scripts = query.query_scripts(store, disposition=disposition)
        if not scripts:
            continue
        lines.append(f"## {disposition.value} ({len(scripts)})")
        lines.append("")
        lines.append("| # | Title | Family | Direction | Integrity | Reason |")
        lines.append("|---|---|---|---|---|---|")
        for e in scripts:
            title = e.title.replace("|", "\\|")
            lines.append(
                f"| {e.catalog_number} | {title} | {e.strategy_family.value} | "
                f"{e.direction.value} | {e.integrity_status.value} | "
                f"{e.disposition_reason.value} |"
            )
        lines.append("")

    lines.append("## Derived strategies")
    lines.append("")
    lines.append("| id | version | family | status | candidate | from | runs |")
    lines.append("|---|---|---|---|---|---|---:|")
    for d in store.derived_strategies:
        lines.append(
            f"| {d.strategy_id} | {d.version} | {d.family} | {d.status} | "
            f"{'yes' if d.selection_candidate else 'no'} | "
            f"{', '.join(d.derived_from_catalog_numbers)} | {len(d.results)} |"
        )
    lines.append("")
    return "\n".join(lines)

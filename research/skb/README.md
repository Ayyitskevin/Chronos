# Strategy Knowledge Base (SKB)

`skb.json` is the compiled Strategy Knowledge Base — a single validated store that
joins the whole Pine corpus with its research context (AI Quant plan Phase B, B1/B2).
`CATALOG.md` is the human-readable view of the same store, rendered from it — browse
that; query `skb.json` from code or the `chronos skb` CLI.

## What it joins

| Input | Contributes |
|---|---|
| `research/strategy_registry.yaml` | 42 Pine scripts: identity (catalog #, sha256, bytes/lines) + forensic flags |
| `research/pine_findings.json` | forensic findings: family, direction, integrity status, feasibility, defects |
| `specs/*.yaml` | canonical Python derivations (currently `regime_trend_v1`, `mean_reversion_v1`) |
| `research/selection_manifest.json` | the frozen selection criteria + candidacy |
| `research/results/research_all.json` | backtest results (all partitions incl. the once-run final test) |

Two entity levels: **Pine scripts** (42) and **derived strategies** (2). B2
backfills a port **disposition** on every script — `ported` (2, a spec derives it),
`deferred` (4, executable standalone strategies not yet ported), `blocked_on` (1,
integrity `REQUIRES_REWRITE`), `rejected` (35, not a standalone tradable strategy) —
each with a machine-readable `disposition_reason`. The disposition is a pure function
of the clean categoricals (integrity status, classification, direction), never prose,
so it is reproducible and reviewable (`chronos/skb/disposition.py`). Timeframe /
asset-class / regime-tags remain defined vocabularies left `unknown`/empty — the
corpus states them only in prose, never guessed.

## Guarantees

- **Fail-closed.** The registry↔findings join must be a complete 1:1 over the
  corpus (filenames normalized to basename first — 12 findings carry a path form);
  every spec Pine-reference must resolve to a real registry entry; every
  categorical must be in the controlled vocabulary. Any violation aborts the compile.
- **Deterministic + hash-pinned.** The store is a pure function of its inputs (no
  timestamps generated), so an unchanged corpus recompiles byte-for-byte. It carries
  a `corpus_hash` (over the sorted catalog#/sha256 pairs) and a SHA-256 of every
  input file, so any source drift is detectable. `test_skb_compiler.py` golden-pins
  the corpus hash and asserts the committed store equals a fresh compile.

## Regenerate

```bash
python scripts/build_skb.py           # rewrite skb.json AND CATALOG.md
python scripts/build_skb.py --check   # CI-style: fail if either committed artifact is stale
```

Do not hand-edit `skb.json` or `CATALOG.md`; change a source input and regenerate.

## Query (read-only)

```bash
chronos skb stats                              # counts by disposition + family
chronos skb query --disposition deferred       # the not-yet-ported executable strategies
chronos skb query --tradable long --format ids # tradable long (folds in bidirectional)
```

The query surface (`chronos/skb/query.py`) is a pure filter/aggregation over the
store — it imports no runtime/order code and places no orders. `tradable_direction`
matches a side inclusively (a `bidirectional` script satisfies both `long` and
`short`). Honesty note: timeframe/data dependencies live only in prose, so a
question like "blocked on intraday data" is **not** answerable as a structured
query — the disposition/reason vocabulary is the structured surface.

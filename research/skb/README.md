# Strategy Knowledge Base (SKB)

`skb.json` is the compiled Strategy Knowledge Base — a single validated store that
joins the whole Pine corpus with its research context (AI Quant plan Phase B, B1).

## What it joins

| Input | Contributes |
|---|---|
| `research/strategy_registry.yaml` | 42 Pine scripts: identity (catalog #, sha256, bytes/lines) + forensic flags |
| `research/pine_findings.json` | forensic findings: family, direction, integrity status, feasibility, defects |
| `specs/*.yaml` | canonical Python derivations (currently `regime_trend_v1`, `mean_reversion_v1`) |
| `research/selection_manifest.json` | the frozen selection criteria + candidacy |
| `research/results/research_all.json` | backtest results (all partitions incl. the once-run final test) |

Two entity levels: **Pine scripts** (42) and **derived strategies** (2). A script a
spec derives from is marked `disposition: ported`; the rest are `unclassified`
pending the B2 backfill. Timeframe / asset-class / regime-tags are defined
vocabularies left `unknown`/empty in B1 — never guessed from prose.

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
python scripts/build_skb.py           # rewrite skb.json
python scripts/build_skb.py --check   # CI-style: fail if the committed store is stale
```

Do not hand-edit `skb.json`; change a source input and regenerate. B2 adds a query
surface (`chronos skb query ...`) and the full per-script disposition backfill.

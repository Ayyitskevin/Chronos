# Research-run reproducibility

Read-only tooling so a cold reviewer can **produce**, **replay**, and **compare**
a deterministic research slice from an auditable manifest. This does **not**
enable paper or live trading, connect to Interactive Brokers, or download market
data.

Related: [RESEARCH_READINESS](RESEARCH_READINESS.md) (if present), campaign CLI
(`research campaign`), ADR-0014 / ADR-0015, [safety.md](safety.md).

## What a manifest records

Schema: `research-run-manifest-v1` (`chronos.research.repro`).

| Field | Purpose |
|-------|---------|
| `code_commit` | `git rev-parse HEAD` at produce time (required; `unknown` rejected) |
| `timezone` | Always normalized to **UTC** (local zones rejected) |
| `seed` | Integer seed bound into identity |
| `strategy_id` / `strategy_version` | Named strategy + version string |
| `config` | Normalized **non-secret** run configuration |
| `config_hash` | SHA-256 of identity-relevant config + datasets + window |
| `policy_version` / `policy_hash` | Research risk policy provenance |
| `datasets[]` | `dataset_id`, `symbol`, content `sha256` (identity); optional `path` (operator metadata only) |
| `date_window` | Requested `{start,end}` ISO dates (may be null if full file) |
| `outputs` | Canonical metrics summary + artifact path checksum |
| `output_fingerprint` | SHA-256 of canonical output payload |
| `manifest_fingerprint` | SHA-256 of the full identity payload |

Secret-like keys (`password`, `token`, `api_key`, `account_id`, `ib_account`,
…) are replaced with `[REDACTED]` and never written with real values.

## Run bundle layout

```text
<path/to/run>/
  config.json      # redacted normalized config used for the run
  output.json      # canonical metrics / summary
  manifest.json    # research-run-manifest-v1
```

## Commands

From a repo checkout with the package importable (`.venv` or `PYTHONPATH=src`):

```bash
# 1) Produce a manifest for a deterministic named-backtest slice
.venv/bin/python -m chronos.cli research repro produce \
  --run-dir /tmp/chronos-run-a \
  --strategy baseline_buy_hold \
  --symbol SPY \
  --data-dir research/data/raw \
  --policy config/risk.research.yaml \
  --seed 0 \
  --slippage-bps 0 \
  --date-start 2019-01-02 \
  --date-end 2021-12-31

# 2) Replay / recompute from that manifest into a new directory
.venv/bin/python -m chronos.cli research repro replay \
  --manifest /tmp/chronos-run-a/manifest.json \
  --run-dir /tmp/chronos-run-b \
  --data-dir research/data/raw \
  --policy config/risk.research.yaml

# 3) Compare with precise pass/fail reasons
.venv/bin/python -m chronos.cli research repro compare \
  --expected /tmp/chronos-run-a/manifest.json \
  --actual /tmp/chronos-run-b/manifest.json
# exit 0 => pass; exit 1 => fail (see JSON "reasons")
```

Rebuild a manifest from an existing bundle (requires `config.json` + `output.json`):

```bash
.venv/bin/python -m chronos.cli research repro produce \
  --run-dir /tmp/chronos-run-a \
  --strategy baseline_buy_hold \
  --symbol SPY \
  --from-existing
```

## Compare reason codes

| Code | Meaning |
|------|---------|
| `match` | Inputs and `output_fingerprint` agree |
| `incomplete_manifest` | Required fields missing or invalid |
| `unsupported_legacy` | Missing/foreign `schema_version` or unsupported slice |
| `missing_data` | Declared dataset path absent or dataset id set differs |
| `checksum_drift` | Input file SHA-256 differs |
| `config_drift` | Normalized config / `config_hash` differs |
| `seed_drift` | Seed differs |
| `timezone_drift` | Timezone identity differs (after UTC normalize) |
| `date_window_drift` | Requested window differs |
| `strategy_drift` | Strategy id/version differs |
| `policy_drift` | Policy version/hash differs |
| `commit_drift` | Git commit differs (**advisory** unless `--require-same-commit`) |
| `nondeterminism` | Same inputs, different `output_fingerprint` |
| `output_drift` | Outputs differ when inputs already differ |

## Supported slice

This milestone supports the **named-backtest** path only
(`chronos.research.runner.run_named_backtest`): one strategy × one symbol ×
local CSV × research risk policy. Walk-forward / full campaign grid
reproducibility can consume the same schema later; those artifacts without
`schema_version: research-run-manifest-v1` fail closed as
`unsupported_legacy`.

## Limitations

- Does not connect to IBKR, purchase data, or mutate production state.
- Does not place, preview, or configure paper/live orders.
- Research identity is **UTC-only**; do not encode local session wall clocks
  into the manifest.
- `code_commit` must be a real git SHA at produce time.
- Dataset **identity** is content-addressed (`dataset_id` + `sha256`); host paths
  are operator metadata and do not participate in `config_hash` / compare.
- When `--data-dir` is passed to replay, that directory is **authoritative**:
  files are verified under the override only (not the original recorded path).
- Full multi-cell campaign JSON under `research/results/` is **not** auto-replayed
  by this CLI; use `research campaign` for generation and this tool for
  per-slice auditability.

## Tests

```bash
.venv/bin/pytest tests/unit/test_research_repro.py tests/safety/test_research_isolation.py -q
```

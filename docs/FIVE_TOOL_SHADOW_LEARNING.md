# Five-Tool shadow learning loop

Status: **code-only / SHADOW journal**. No paper, live, or promotion authority.
`gate_advanced: none`.

This is the path Chronos will accept for a model that trains on its own
closed-bar facts. It does not manufacture more trades by weakening
`min_score` or pairing vetoes.

```
5T traces + pairing snapshots
  → advisory export (research, no autonomy import)
  → EvidenceBundle 1.1 (autonomy-native facts, digest-pinned)
  → WorkerRequest (job id, digest, pins, expiry)
  → external worker or deterministic reference worker
  → ProposedDecision JSON (no provenance / decision_id)
  → supervisor ingress
  → shadow journal (admission not_attempted, transmit=false)
```

## 1. Advisory evidence pack

`chronos.research.features.advisory_export` projects one `PairingFrame` plus
its host `FiveToolTrace` into a JSON object. Size, stop, equity, and
risk-budget keys are dropped. `chronos.autonomy.advisory` validates the same
schema string independently. Those facts land on `EvidenceBundle` as
`five_tool_signals`, `feature_snapshots`, and `pairing_vetoes`, each marked
`advisory=True`. The kernel does not size or protect from them.

A non-empty advisory pack requires `bundle_version="1.1"`. Changing the facts
changes the digest.

## 2. Model worker contract

Chronos still does not call a model. `WorkerRequest` binds one job to one
issued digest, expected pins, and an expiry. The worker returns one
`ProposedDecision`. Ingress refuses `provenance` and `decision_id`.

`chronos.autonomy.reference_worker` is a pinned stub, not a live LLM:

- HOLD unless exactly one Five-Tool signal is `enter_long` / `enter_short`
  and the matching pairing veto is `allow`
- OPEN carries no `requested_quantity`

A real worker is an operational process the owner runs against the same JSON
contract.

## 3. Shadow mandate / journal

`chronos.supervisor.shadow_learning.journal_reference_worker` parses the
payload through the existing ingress and appends a JSONL row. It does not
stamp provenance, admit, size, compile, or submit. Omitting `submit` on
`run_cycle` remains the SHADOW handoff rule; this helper is narrower and
cannot reach that handoff.

Journal identity pins are Chronos-owned (`chronos-reference` /
`pairing-allow-enter-v1`). They are not a worker self-report. ADR-0023's
ingress credential gap is unchanged: this slice binds the real bundle digest
on the job and the journal, and does not invent a per-worker credential.

## 4. Certified companions and the tradable book

Still `pending_certified_dataset`. Schema
`chronos-five-tool-certified-intake-v1` names the overlapping release the
owner must certify before any pairing cell can read bytes: **GLD**, **IWM**,
**QQQ**, **RSP**, **SPY**, **VIX**, **VIX3M**. Optional internals remain
TICK, ADD, VOLD. `open_certified_intake` and
`require_certified_companion_dataset` refuse every call, including a forged
digest. Chronos does not download market data, does not open
`CertifiedDatasetCatalog`, and does not write `research/data/history/HOLDOUTS.json`.

The QQQ window 2022-01-01 through 2024-01-10 is locked as consumed
(`qqq-2022-01-2024-01-consumed`). An owner holdout may be declared only when
it names every required intake symbol and does not overlap that range. No
owner holdout dates are chosen in this slice.

On **GLD**, the reference worker sees the gold pairing identity: equity VIX
and breadth are inert, so ENTER plus same-symbol ALLOW can OPEN even when
VIX is `STRESS`. That is still SHADOW only. `require_external_worker`,
`require_paper_authority`, and `require_live_authority` refuse. Chronos does
not call a model.

The tradable book is locked: **GLD**, **IWM**, and **QQQ**. Schema
`chronos-five-tool-tradable-book-v1`. Changing the set is a new digest.
SPY is the benchmark and is never traded. QQQM is not in the book. QQQ is
both a tradable and the Nasdaq-100 breadth series. The reference worker
HOLDs any other symbol, including SPY and QQQM, even when Five-Tool ENTER
and pairing ALLOW agree. In-repo IWM is 2019–2021, dividend-adjusted, and
not certified. Three names are not three-instrument promotion evidence
until certified overlapping history exists.

## 5. "More liberal" later, not fewer gates

If the owner wants more decisions to learn from, the lawful widenings are
more symbols, more sessions, or a Neutral-only Kalman sleeve with its own
identity. They are not implemented here. Loosening pairing vetoes or
Five-Tool `min_score` to harvest trades is untracked experimentation.

## 6. Promotion still binds identity

Phase 4 already requires a promotion artifact to bind account, commit,
mandate, strategy-policy, model/prompt/tools, evidence/data versions, and
criteria digest. For this loop that means at least:

- Five-Tool Pine SHA-256 and 219-input contract digest
- pairing `FeaturePolicy` digest
- companion catalog dataset id and SHA-256 once certified
- worker pins (`provider`, `model_id`, `model_version`, `prompt_version`,
  `tool_schema_version`, `decision_schema_version`, `policy_version`)
- `EvidenceBundle` `bundle_version` and digest

This slice does not add a second promotion path. A shadow journal is not a
promotion artifact.

# ADR-0013: Experiment registry + holdout guardian (Milestone C2)

Status: accepted (two-reviewer adversarial review completed post-merge; findings
remediated — §11 records them and corrects the overclaims)
Date: 2026-07-19

## Context

AI Quant plan C2: every research run recorded (config hash, data hashes, criteria
version, stage, git commit); the multiple-testing trial count **derived automatically
from the registry** (self-reported N is theater); holdout reads **mediated by the
registry** — consuming a holdout requires an explicit, logged, **once-only unlock
typed by the owner**, and **no scheduled job, proposal-execution path, or copilot
artifact can invoke it**; a budget policy rations unlocks against newly accrued data.
The M5 review's **"burned holdout"** failure class — a holdout window silently reused
as if fresh — must become **structurally impossible**.

This builds on two existing pieces (discovery-confirmed):

- **`chronos.auditlog.AuditLog`** — the existing file-based, `fsync`'d, hash-chained
  JSONL record format
  (`AuditRecord{sequence, at_utc, kind, payload, previous_hash, record_hash}`;
  `_hash_record = sha256("{seq}|{at}|{kind}|{payload_json}|{prev}")`; `verify_chain`
  detects sequence-gap / chain-break / hash-mismatch). The registry keeps this
  record/hash format but implements hardened descriptor-bound I/O and exact-schema
  verification in `RegistryLedger`; it does not inherit `AuditLog`'s path semantics.
- **`chronos.histdata.holdout`** (C1) — `HoldoutWindow` + `embargoed_view(...,
  unlocked=False)` default-masks declared windows; today `unlocked=True` is a plain
  boolean with **no token, no logging, no once-only enforcement** (its docstring names
  that gap as C2's job).

Two facts constrain the design:

- **There is no TTY/`getpass`/`isatty` guard anywhere in `src/`.** The codebase already
  distinguishes owner-typed from automated actions **structurally**: typed phrases enter
  only through the UI/HTTP boundary, and the automated service loop simply never calls
  `arm()`/`confirm()`. C2 follows that doctrine — a module-constant phrase plus a
  structural test that automated modules cannot import the unlock — rather than
  inventing a runtime interactivity check the rest of the system doesn't use.
- **No "once-only / consumed" flag exists today.** `LiveArmingService` (fixed
  `REQUIRED_ARM_PHRASE` constant, `hmac.compare_digest`, TTL, never-logged-raw) is
  *reusable* within its TTL; per-order confirmations are single-use only by
  approximation (order-bound hash + short TTL). C2 **adds** genuine consume-and-invalidate.

## Decision

A new **research-plane** package `chronos.registry`. It records runs and guards
holdouts; it opens no trading database, places no order, and imports no
order/broker/execution module.

### 1. The registry ledger — hash-chained, append-only, AuditLog-compatible records

`registry/ledger.py` preserves the `AuditLog` record/hash format over
`research/registry/registry.jsonl` (separate from the trading
`data/platform_audit.jsonl`) while performing registry I/O through a hardened,
descriptor-bound `RegistryLedger`. Typed record kinds:

- `experiment_run` — one research run (§2).
- `trial_started` — one canonical Phase-3 data-touching attempt, durably written before
  the brokered reader may return bytes (§9).
- `trial_terminal` — the completed/failed outcome bound to exactly one canonical start;
  terminal records describe outcomes but do not reduce multiplicity (§9).
- `holdout_unlock` — an owner-typed unlock was granted for a window (§3).
- `holdout_consume` — a holdout window was read under a granted unlock; the window is
  now **burned** (§4).

Every record retains the chain's tamper-evidence; `RegistryLedger.verify()` (and CLI
`registry verify`) proves the ledger was not edited, reordered, or truncated. The
ledger is the **single source of truth** for trial counts and burned-holdout status —
not any in-memory or self-reported value.

### 2. Experiment-run recording + honest data fingerprints

`register_run(ledger, *, stage, strategy_id, config_hash, code_commit, data_hashes,
criteria_ref, touched_data)` appends an `experiment_run`. Fields:

- `stage` ∈ {`dev`, `validation`, `holdout`} — which data partition the run consumed.
- `config_hash` — the run config (params) hash; `code_commit` — `git rev-parse HEAD`
  (reusing `research.runner.current_commit`); `criteria_ref` — the frozen-criteria
  identity (the `selection_manifest.json` `frozen_at_utc` / `re_frozen_at_utc` pair, the
  repo's existing "criteria version" — there is no version integer, so the freeze
  timestamps are the reference).
- `data_hashes` — **honest, per the discovery gap**: a `data_fingerprint(root, symbols)`
  helper hashes the C1 store's **bars + corporate-actions** pair per symbol (from the
  histdata `MANIFEST.json`), a dict `{symbol: {bars_sha, actions_sha}}` — not the
  legacy single-CSV sha the runner stamps today. A run that used the legacy raw corpus
  records that sha under a `legacy_raw` key, labeled, so the two provenance regimes are
  never conflated.
- `touched_data: bool` — whether the run consumed data (see §5).

### 3. The holdout guardian — once-only, owner-typed, logged

`registry/holdout_guardian.py`:

- **Phrase:** `REQUIRED_HOLDOUT_UNLOCK_PHRASE` — a **module constant**, never a setting
  (so it can never land in a serialized/logged config), validated with
  `hmac.compare_digest`; the raw phrase is never stored, logged, or echoed. Mirrors
  `orders.arming`.
- `request_unlock(ledger, history_root, window_name, *, typed_phrase, reason, now,
  accrued_sessions, ...)` → validates the phrase; checks the window is **declared**,
  does not overlap already-burned scope, and has no overlapping active grant (§4);
  checks the **budget** (§6); then appends a `holdout_unlock` carrying a fresh random
  `unlock_id` (`secrets.token_hex`), the reason (never the phrase), expiry, canonical
  window definition, name-independent scope digest, relevant stored-bar digest, and
  full canonical HOLDOUT set digest. A grant is **single-use** and bound to those exact
  definitions and bytes.
- `mediated_holdout_read(ledger, history_root, symbol, *, grant, now)` → verifies the
  exact durable grant, expiry, immutable bindings, canonical symbol path, and absence
  of a prior consume; appends `holdout_consume` (burning the scope) **before** reading;
  then calls the private C1 selective-unmask helper for only the authorized window.
  Every other applicable holdout remains masked. Shipped source contains no call to
  the broad `embargoed_view(..., unlocked=True)` bypass.

### 4. "Burned" is structural — the ledger is the authority

A window with a `holdout_consume` record is **burned**. `is_burned(ledger, window)` and
`burned_windows(ledger)` derive from the ledger. Consequences enforced in code:

- `request_unlock` **refuses** a burned or overlapping scope (fail-closed), including a
  renamed declaration. A legacy burn without sufficient scope evidence blocks all new
  unlocks until a future explicit contamination/reset protocol exists.
- A consumed grant cannot be replayed (`mediated_holdout_read` refuses a second consume
  of the same `unlock_id`).
- Changing the target definition, another declared holdout, or the relevant stored bytes
  between grant and consume invalidates the grant before unmasking.
- The M5 failure ("holdout read once, then silently treated as fresh in a later run")
  cannot occur: the ledger records the burn, and any run over a burned window is
  self-evidently over seen data.

### 5. Trial counting — derived, not declared

`trial_count(ledger, *, strategy_id=None, since_criteria=None)` remains the compatibility
view over legacy `experiment_run` records. The Phase-3
`CanonicalTrialRegistry.multiplicity_snapshot()` derives one head-bound count from unique
canonical `trial_started` attempts plus legacy data-touching `experiment_run` identities,
deduplicated across record kinds (§9). A start counts whether it later completes, fails, or
is interrupted; retries use distinct attempt IDs and each count. Final campaign scoring
must consume one snapshot only after every candidate has run. The registry supplies that
global-N fact, but order-invariant scoring and the reviewed cross-trial variance estimator
remain separate Phase-3 work.

### 6. Budget — unlocks rationed against newly accrued data

`registry/budget.py`: `available_budget(ledger, *, accrued_sessions, policy)` grants
one unlock credit per `sessions_per_unlock` (setting) of **newly accrued** capture
sessions since the last unlock (accrual measured from the C1 store, e.g. new option
snapshot dates / bar sessions), capped at `max_outstanding_unlocks`. `request_unlock`
spends a credit; with zero budget it fails closed. This rations holdout consumption
against real new information rather than letting an operator burn every window at once.

### 7. Structural isolation — the unlock is unreachable from automated paths

Two test layers (mirroring `test_histdata_isolation` + `test_single_transmit_site`):

- **Package isolation:** `chronos.registry` imports nothing from
  `chronos.{orders,api,services,service,execution,risk,control,broker,runtime,ui}` /
  `sqlalchemy` / `sqlite3` (AST + subprocess `sys.modules` probe). It may import
  `chronos.{auditlog,histdata,config,utils}`.
- **Unlock unreachability:** an AST walk over the **scheduler / service / proposal /
  promotion / submission** modules (`service/*`, `services/*`, `control/promotion.py`,
  `execution/engine.py`, `orders/submission.py`) asserts none import
  `chronos.registry.holdout_guardian` nor call `request_unlock` / `mediated_holdout_read`
  by name. **No `copilot` module exists yet** — the test's forbidden list is written to
  include the future copilot package path prospectively, and the assumption is flagged
  so D3/D4 inherit the bar.

  **Updated 2026-07-25 (ADR-0016 / D-16).** The model plane shipped for real, as
  `chronos.autonomy`, not as `chronos.copilot`. The bar is retargeted rather than
  relaxed: `chronos.autonomy` is now part of the scanned automated tree, so no module in
  it may import the registry or call the unlock — the guarantee this section makes is
  preserved and now covers a plane that actually exists. `chronos.autonomy` is
  deliberately *not* added to the forbidden-import list, because the deterministic
  supervisor must import the `AITradeDecision` contract in order to judge it; the
  prospective bar on `chronos.copilot` remains.

### 8. CLI (owner-run) + settings

- `chronos registry stats|verify` reports/verifies the canonical lifecycle and global-N
  snapshot. Compatibility-only `legacy-stats|legacy-verify` commands are explicitly
  named and never presented as the canonical count.
- `chronos holdout status` (declared/burned windows, budget) and `chronos holdout
  unlock --window W --reason R` — the **owner-run** unlock, which reads the phrase and
  calls `request_unlock`. Following the arming precedent, the phrase is not a CLI flag
  echoed into process listings where avoidable.
- Settings: `holdout_unlock_ttl_minutes` (`Field(gt=0, le=120)`), `sessions_per_unlock`
  (`Field(ge=1)`), `max_outstanding_unlocks` (`Field(ge=0)`). The phrase stays a module
  constant, never a setting.

### 9. Phase-3 canonical trial and replay-evidence extension (2026-08-08)

Phase 3 adds a narrower production research path without widening the guardian:

- `CanonicalTrialRegistry()` owns the fixed `research/registry/registry.jsonl` path. Its
  private temporary-path constructor exists only for tests. A canonical start binds the
  campaign, manifest, stage, strategy, config, code, criteria, and data identities and is
  durable before the brokered reader may open the partition.
- `CertifiedDatasetCatalog` is constructed from an out-of-band trusted manifest digest and
  a fixed dataset root. Ordinary callers receive sanitized metadata only. The path-bearing
  entry and byte-opening operation stay private to the broker; declared holdouts are
  categorically refused, and the catalog rejects any path or content digest classified on
  both the ordinary and holdout sides.
- `BrokeredResearchTrialRunner` sequences start -> read -> evaluate -> retain -> terminal.
  The evaluator receives bytes, not a path or reader capability. Reader and evaluator
  failures receive a failed terminal when the registry remains writable, while their start
  still counts. Completion is recorded only after all evidence is retained.
- `ReplayObjectStore` retains immutable SHA-256-addressed input/output objects and a
  canonical envelope binding the trial receipt, data catalog, dataset version, evaluator,
  code, config, and criteria. The completed terminal stores the envelope digest.
  `load_completed_evidence` verifies the terminal-bound registry identity, envelope, and
  every referenced object after restart; a completed terminal by itself is not sufficient
  replay evidence.
- Every registry mutation uses a fresh descriptor-bound `RegistryLedger` inside one
  thread- and OS-file-locked, verify-before/after transaction. This avoids stale cached
  head/sequence state and path-replacement races across concurrent writers; the existing
  anchor still supplies rollback/truncation detection.

This is infrastructure, not a certified campaign verdict. Registry and replay-store paths
are frozen from each capability's construction-time working directory, and the runner
refuses capabilities from different workspaces before recording a start. Constructing both
from the trusted Chronos workspace remains an operational boundary. Path traversal is
descriptor-relative and rejects symlinked/replaced components. The local anchor is unsigned.
A Python evaluator is not a sandbox and must be reviewed. Legacy `research.campaign` and
`research.walkforward` paths still touch data before their legacy registration; they are not
brokered or certified. The Five-Tool manifest remains blocked pending real data/code/criteria
locks and evaluator authorization. No final-N seal, reviewed variance estimator, untouched
holdout result, or promotion artifact is created by this extension.

## Honesty bounds

- **"Typed by the owner" is enforced structurally + by phrase**, not by a runtime
  interactivity check (the codebase has none). An owner who scripts the phrase into an
  automated caller defeats it — but the structural test guarantees no *shipped*
  scheduled/proposal/copilot path can, which is the DoD's actual requirement.
- **Legacy research paths are not brokered.** The Phase-3 runner in §9 enforces canonical
  start-before-read and evidence retention for callers that use it; existing campaign,
  walk-forward, generic runner, and shadow paths are unchanged and cannot claim that
  guarantee.
- **The budget policy is a first cut** (linear accrual credits); it rations, it does not
  model statistical power — that lands with C3/C4.
- **No copilot module exists**; its bar is prospective (§7).

## What proves it

- Ledger: append + `RegistryLedger.verify()` passes; a tampered line fails with the right class;
  records round-trip.
- Guardian: a correct phrase grants an immutable scope/data/set-bound unlock (logged, no
  raw phrase in the record); a wrong phrase fails; a grant authorizes exactly **one**
  consume; expiry, definition drift, data drift, renaming, overlap, and replay are refused;
  the private selective helper unmasks only that scope after the burn is recorded.
- Budget: zero budget fails closed; accrual grants exactly the rationed number.
- Trial count: the canonical snapshot equals unique starts plus legacy data-touching runs,
  is bound to one verified ledger head, and counts completed, failed, retried, and orphaned
  starts rather than a self-reported field.
- Structural: `chronos.registry` imports nothing forbidden; the scheduler / service /
  proposal / promotion / submission modules import neither the guardian module nor call
  its unlock functions; subprocess probe leaks nothing.
- Burned-holdout scenario: read a window under a grant → it is burned → a subsequent
  unlock request and a subsequent "fresh" run over it are both refused/flagged.

## 11. Two-reviewer review remediation (record)

A safety + a correctness reviewer ran to completion (post-merge) and both broke the
original "structurally impossible" framing. All confirmed findings were remediated; the
claims below are the corrected, honest ones.

- **F1/safety-1 (CRITICAL) — chain truncation un-burned a window undetected.** A bare
  hash chain can't detect tail deletion (a valid prefix is a valid chain), so dropping
  the trailing `holdout_consume` line un-burned a window while `verify()` still said
  "intact". **Fix:** an out-of-band **head anchor** (`registry.head.json`: expected
  count + last hash) that `verify()` checks, so truncation / whole-file deletion /
  rollback are detected.
- **F2/safety-1 (HIGH) — nothing verified before trusting.** The guardian read the
  ledger without verifying it. **Fix:** `request_unlock` and `mediated_holdout_read`
  call `verify()` first and **fail closed**; `registry stats` / `holdout status` exit
  non-zero on a broken chain.
- **safety-2 (HIGH) — TOCTOU let one grant be consumed twice (double unmask).** The
  arming service it mirrors uses a lock; the guardian had dropped it. **Fix:** the
  read-verify-append critical section holds an exclusive **OS file lock** (`fcntl.flock`).
- **safety-3 (HIGH) — the guardian was bypassable.** `embargoed_view(unlocked=True)` had
  no single-site guard. **Fix:** shipped source is forbidden from passing that broad flag;
  `test_single_unmask_site.py` permits the private selective-unmask helper at exactly one
  call site in the guardian.
- **safety-4 (HIGH) — the no-automated-unlock test had coverage holes.** **Fix:** it now
  scans the whole automated tree (`service`/`services`/`control`/`execution`/`orders` +
  `runtime.py`), not a hand-picked list.
- **F4/F5/F7 (MEDIUM) — honesty.** Budget now counts burns + *active* grants (an expired
  unused grant is refunded); `register_run` fails closed on null provenance (`""`/
  `"unknown"` commit, empty criteria); `data_fingerprint` carries an `actions_captured`
  flag and takes the history root directly.

**Corrected claims (the honest guarantee).** The M5 burned-holdout failure is **detected
and refused**, not "structurally impossible" in an absolute sense: the anchor + verify
catch accidental/incidental truncation, deletion, in-place edits, and rollback, and no
*shipped* automated path can invoke the unlock. Out of scope (disclosed): an actor who
rewrites **both** the ledger and its anchor consistently (the anchor is not a signed,
off-host root of trust), a determined **runtime-reflection** evasion of the unlock guard,
and completeness of the **trial count** outside the §9 brokered path (it is derived from
registered records; legacy research paths remain a follow-on). Registry writers use fresh
state inside the shared file lock; single-writer concurrency is enforced by that lock.
§3's `mediated_holdout_read(ledger, history_root, symbol, ...)` argument order is
authoritative.

> **Update (2026-08-09) — the trial-count residual is narrowed for one path, not closed.**
> The Five-Tool trial broker (`src/chronos/research/five_tool_trials.py`) now writes an
> `experiment_run` record into this registry **before** it starts an attempt and before
> its reader is called, verifying chain **and** anchor first and refusing the trial when
> the registry is unwired, unprovisioned, unreadable, or unverifiable — "no registry, no
> trial". An attempt that dies after opening data is therefore still counted, and a
> campaign whose attempts were not registered cannot be sealed. Evidence:
> `tests/safety/test_five_tool_registry_exercised.py`.
>
> What is **not** closed: `research/walkforward.py` still calls `register_run` *last*, on
> purpose — it is handed an already-read series, so it cannot register before the read,
> and it declines to count a cell that raised mid-statistics. Any run outside both paths
> still counts only if its caller registers it. The two orderings disagree, deliberately;
> that disagreement is recorded here and in `docs/limitations.md` rather than resolved by
> an agent, because changing which runs count changes a frozen multiple-testing input.
> The registry itself still ships **empty** — this is a capability, not evidence.

## Consequences

The research plane gains a tamper-evident, anchor-verified registry whose ledger is the
authority on how many trials were **registered** and which holdouts are spent, and a
holdout guardian that makes the M5 burned-holdout failure **detected and refused**: no
shipped automated path can unlock a holdout, an unlock is owner-typed, single-use, and
file-locked, and a consumed window is burned in a hash-chained log whose truncation is
caught by the head anchor. The honest residuals are recorded in §11 and
`docs/limitations.md`. Nothing in the trading/live plane changes; the single
`transmit=True` boundary and the C1 isolation are untouched; the registry ships empty.

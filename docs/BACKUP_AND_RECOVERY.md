# Backup and Recovery

What to back up, how to do it safely with SQLite, and how to restore. ~~The design premise:
restore must never auto-resume trading, and the code guarantees it — a restored (or missing) halt
file reads as HALTED (`src/chronos/control/halt.py`), and submission is refused until
reconciliation passes again (`src/chronos/execution/engine.py`).~~

> **Corrected 2026-08-02 — that guarantee holds for the deterministic platform only.** It is
> true and unchanged for `chronos.execution`/`chronos.risk`: a restored or missing
> `data/platform_halt.json` reads as HALTED, and submission is refused until reconciliation
> passes. It is **not** true of the `chronos.orders` live plane, which is the plane that can
> place an order:
>
> - A **missing** `data/live_kill_switch.json` reads as **DISENGAGED**
>   (`src/chronos/orders/kill_switch.py:83-85`) — the opposite default. Restoring a backup
>   that omits this file therefore comes up with the emergency stop **disarmed**. (A
>   *corrupt* file reads as ENGAGED, so only absence is dangerous.)
> - A valid, account-matching `AUTONOMY_MANDATE_FILE` **auto-activates on boot** (ADR-0017),
>   so restoring one and starting the backend re-arms autonomy without any operator action.
>   Revocation, by contrast, survives restart.
>
> The vision plan's required end state is that recovery always boots kill-engaged,
> read-only, and unreconciled (`docs/VISION_COMPLETION_PLAN.md` §6, finding 3 — **open**;
> the code does not do this yet). Until that lands, the manual step in the restore procedure
> below is the compensating control. Changing the code's boot defaults is a
> safety-mechanism modification requiring owner review, not a documentation fix.

## What to back up

| Path | What it is | Loss impact |
|---|---|---|
| `data/platform_ledger.db` | Platform order ledger (intents, transitions, fills) | Lose the platform's own order history; reconciliation can no longer explain broker state |
| `data/platform_halt.json` | Persistent halt state (deterministic platform) | Low (missing file fails closed to HALTED), but back it up to preserve the recorded reason/note |
| `data/live_kill_switch.json` | **Live order-plane kill switch** (`live_kill_switch_file`) — *added 2026-08-02, previously missing from this table* | **HIGH, and inverted from the row above: a missing file reads as DISENGAGED**, so restoring without it brings the system up with the emergency stop disarmed. Always restore it, or engage the kill switch again before starting the backend. |
| your `AUTONOMY_MANDATE_FILE` (if configured) | Owner-authored autonomy grant — *added 2026-08-02* | Restoring it **re-arms autonomy on the next boot** (ADR-0017 auto-activation). Treat it as an authority document: back it up securely, and move it aside during any recovery you do not intend to resume trading from. |
| `data/platform_audit.jsonl` | Hash-chained audit trail | Lose the tamper-evident record of decisions and operator actions |
| `config/` | Risk policies (`risk.example.yaml` plus your local `risk.yaml`) | Lose the exact limits runs were made under; policy hashes in results become unverifiable |
| `specs/` | Canonical strategy specifications | Versioned in git, but back up local edits |
| `research/strategy_registry.yaml`, `research/strategy_catalog.*` | Pine corpus registry with pinned SHA-256 hashes | Lose corpus integrity verification (`verify-corpus`) |
| `research/pine/` | The audited Pine sources | Lose the audited corpus itself |
| `research/data/raw/` incl. `MANIFEST.json` | Historical data + provenance manifests | Lose reproducibility of research results |
| `data/chronos.db` | Wheel dashboard ledger (separate system, ADR-0003) | Lose wheel cycles/evidence/notes |
| `data/promotions/` (if you create it) | Promotion records | Lose the promotion paper trail |
| `.env` (store securely) | Your local configuration | Reconstructable, but back up to avoid mistakes on redeploy |
| your `deploy-freeze-*.txt` | Resolved dependency snapshot (docs/DEPLOYMENT.md) | Lose environment reproducibility (no lockfile exists) |

Code and most docs live in git; the list above is the state git does not hold.

## SQLite-safe backup

Both `data/platform_ledger.db` and `data/chronos.db` are SQLite. The platform ledger runs in WAL
mode (`PRAGMA journal_mode=WAL`, `src/chronos/execution/sqlite_ledger.py`), which means part of
the recent data lives in the `-wal` sidecar file until checkpointed.

**Preferred — online, consistent, works even while a process is running:**

```bash
sqlite3 data/platform_ledger.db ".backup 'backups/platform_ledger-$(date +%F).db'"
sqlite3 data/chronos.db          ".backup 'backups/chronos-$(date +%F).db'"
```

`.backup` produces a consistent snapshot regardless of WAL state.

**Acceptable — plain copy, but ONLY while every Chronos process is stopped:**

```bash
# All processes stopped. Copy the -wal and -shm sidecars too if present:
cp data/platform_ledger.db data/platform_ledger.db-wal data/platform_ledger.db-shm backups/ 2>/dev/null
```

Never plain-copy a live WAL database without its sidecars — you get a stale or torn snapshot.

The JSON/JSONL/YAML files (`platform_halt.json`, `platform_audit.jsonl`, configs, manifests) are
plain files; copy them normally. For the audit log, prefer copying while stopped, or accept that a
mid-append copy may end in a truncated last line (the verifier will point at exactly that line).

A complete cold backup, simplest form:

```bash
# all Chronos processes stopped
tar czf chronos-backup-$(date +%F).tar.gz data/ config/ specs/ research/ .env
```

Keep at least one copy off the machine. The audit log's tamper evidence is only as good as an
off-machine copy to compare against (docs/SECURITY.md).

## Restore procedure

1. **Stop everything.** No Chronos process may be running.
2. **Restore files** into place (`data/`, `config/`, `specs/`, `research/`). For SQLite files
   restored from `.backup` snapshots, just put the `.db` file in place; delete any leftover
   `-wal`/`-shm` sidecars from the dead instance so they cannot be replayed over the restored
   snapshot.
3. **The deterministic platform starts HALTED — by design.** Whatever the halt file says now
   stands; if the halt file was not restored, the missing file reads as HALTED (`NEVER_ARMED`).
   Nothing trades **in that plane**.

   **BEFORE starting the backend, do these three by hand** *(added 2026-08-02 — the live plane
   does not fail closed on its own; see the corrected premise at the top of this document)*:

   ```bash
   # a. Confirm the live kill switch exists; a MISSING file means DISENGAGED.
   ls -l data/live_kill_switch.json || echo "MISSING → emergency stop is DISARMED"
   # b. Move any autonomy mandate aside so boot cannot auto-activate it.
   #    (Restore it deliberately, later, when you intend to resume.)
   grep -n '^AUTONOMY_MANDATE_FILE' .env || echo "no mandate configured — nothing to move"
   # c. Take a read-only state inventory before the process runs.
   python3 .claude/skills/chronos-diagnostics/scripts/state_inventory.py
   ```

   If the kill-switch file is missing, engage the kill switch immediately after the backend
   starts (`POST /live/kill`, see `docs/INCIDENT_RESPONSE.md`) and confirm the file appears —
   or start with live capability unconfigured until you have.
4. **Verify the audit chain:**
   ```bash
   python -m chronos.cli verify-audit-log
   ```
   `OK — chain intact (N records)` or a precise first-failure line. If it fails, treat it as an
   incident (docs/INCIDENT_RESPONSE.md) — a restore from an older backup legitimately has fewer
   records, but a broken chain within the restored file is corruption or tamper.
5. **Verify corpus integrity** (if research state was restored):
   ```bash
   python -m chronos.cli verify-corpus
   ```
6. **Reconcile against the broker** before any future submission window: broker open orders (by
   `orderRef`) and positions must be explained by the restored ledger
   (`src/chronos/execution/reconciliation.py`; queries in docs/OPERATIONS.md). A restore from an
   older ledger snapshot can legitimately produce `UNEXPLAINED_POSITION` or
   `UNKNOWN_BROKER_ORDER` discrepancies — resolve them at the broker and document them
   (docs/INCIDENT_RESPONSE.md playbooks).
7. **Only then rearm, explicitly:**
   ```bash
   python -m chronos.cli rearm --note "restore from backup <date>; audit chain OK; recon clean: <evidence>"
   ```

## Restore never auto-resumes trading — in the deterministic platform

*(Scoped 2026-08-02. This section was written for `chronos.execution`/`chronos.risk` and stated
as a repository-wide guarantee; it is not one.)*

For the **deterministic platform**, restoring from backup NEVER re-enables order generation on
its own. Fail-closed halt reads, the mandatory operator rearm with note, the mode lock's evidence
requirements, and the reconciliation gate each independently block it. If you ever observe a
restored deterministic platform generating orders without a fresh rearm, that is a critical bug —
halt, capture evidence, and stop using the build.

For the **`chronos.orders` live plane**, the equivalent claim is **not** true today and must not
be relied on:

| Restored artifact | Effect on boot |
|---|---|
| `data/live_kill_switch.json` present + engaged | Stop holds (correct). |
| `data/live_kill_switch.json` **absent** | Reads **DISENGAGED** — the stop is disarmed. |
| Valid `AUTONOMY_MANDATE_FILE` present | Autonomy **auto-activates** (ADR-0017). |
| Mandate previously revoked | Stays revoked across restart (correct). |

Live submission still requires the full ADR-0009 conjunction, a current session arm,
reconciliation, and the writer lease — so a restore alone does not place an order. But "the code
guarantees a restored system cannot resume" is false for this plane, and the manual steps in the
restore procedure above are what close the gap. Making recovery boot kill-engaged, read-only, and
unreconciled is open finding 3 in `docs/VISION_COMPLETION_PLAN.md` §6 and requires owner review.

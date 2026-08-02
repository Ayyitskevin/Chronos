# Incident Response

For the single operator. The theme of every playbook: halt first (always safe), gather evidence,
resolve at the broker manually, document, and only then rearm with a note. The platform never
auto-flattens and never auto-resumes; those are features, not gaps.

## Severity levels

| Level | Definition | Examples |
|---|---|---|
| SEV-1 | Money or account integrity in question | suspected duplicate order, unknown position, unexplained fill, any sign of live-account contact |
| SEV-2 | Safety machinery degraded | audit-chain verification failure, halt-file corruption, ledger write failure, illegal state transition halt |
| SEV-3 | Operational disruption, safety intact | gateway disconnects, pacing violations, data-quality blocks, stuck-but-explained orders |

SEV-1/SEV-2: do not trade (do not rearm) until the incident is explained and documented.

## Immediate actions (any incident)

> **Corrected 2026-08-02 — this section previously named only the deterministic
> platform's halt.** Chronos has **two independent stop mechanisms**, and the halt below
> does not touch the plane that can actually place an order. Engage **both**. Full
> procedures and the reasoning live in `docs/live_trading_runbook.md`; the two mechanisms
> are compared there and in the `chronos-run-and-operate` skill.

1. **Stop the live order plane — do this FIRST if any live/paper capability is configured.**
   The kill switch is deliberately reachable without the writer lease, because an emergency
   stop must always work:
   ```bash
   curl -fsS -X POST http://127.0.0.1:8765/live/kill \
     -H "X-Chronos-Token: $CHRONOS_API_TOKEN"
   ```
   It persists to `data/live_kill_switch.json` (`live_kill_switch_file`) and is cleared only
   by an explicit `POST /live/kill/disengage`. **Verify the file exists afterwards** — see
   the warning below.
2. **Halt the deterministic platform. Always safe, never harmful:**
   ```bash
   python -m chronos.cli halt --reason "SEV-n: <one line>"
   ```
   The halt persists across restarts and blocks all new order generation **in the
   deterministic platform** (`chronos.execution`/`chronos.risk`). It does **not** stop the
   `chronos.orders` live plane — that is step 1. Cancelling existing working orders, if
   needed, is a manual action in TWS (the platform's DAY orders expire at end of day
   regardless).
3. **If an autonomy mandate is configured, revoke it or move the file aside.** A valid,
   account-matching `AUTONOMY_MANDATE_FILE` **auto-activates on every boot** (ADR-0017), so
   restarting is not a stop. Revocation survives restart:
   ```bash
   curl -fsS -X POST http://127.0.0.1:8765/terminal/mandate/revoke \
     -H "X-Chronos-Token: $CHRONOS_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"reason":"SEV-n: <one line>"}'
   ```

> **The two stop mechanisms fail in OPPOSITE directions — do not assume symmetry.** A
> missing `data/platform_halt.json` reads as **HALTED** (safe;
> `src/chronos/control/halt.py:102-109`). A missing `data/live_kill_switch.json` reads as
> **DISENGAGED** (`src/chronos/orders/kill_switch.py:83-85`) — deleting or failing to
> restore that file **disarms** the emergency stop. After engaging, confirm the file is
> present; after any restore, engage it again before starting the backend.
2. **Stop making changes.** No code edits, no config edits, no cleanup, until evidence is
   captured.

## Evidence capture

Copy, don't move; timestamp everything:

```bash
mkdir -p incidents/$(date +%F-%H%M)
cp data/platform_audit.jsonl incidents/$(date +%F-%H%M)/
sqlite3 data/platform_ledger.db ".backup 'incidents/$(date +%F-%H%M)/platform_ledger.db'"
cp data/platform_halt.json incidents/$(date +%F-%H%M)/ 2>/dev/null
python -m chronos.cli status > incidents/$(date +%F-%H%M)/status.txt 2>&1
```

Also collect:

- TWS/IB Gateway API logs and trade logs (TWS: Account → Trade Log; API logs are under the
  gateway's settings/log directory) — export before the gateway's daily restart rotates them.
- The wheel dashboard log (`logs/chronos.log`) if that subsystem was involved.
- A written timeline while memory is fresh: what you did, what you saw, wall-clock times.

## Playbooks

### Duplicate order suspected

Design context: duplicates are suppressed at four layers — deterministic intent ids (UUIDv5 of
economic content), the risk engine's per-instance duplicate check, the ledger primary-key refusal,
and `orderRef` at the broker (`src/chronos/execution/intents.py`, `sqlite_ledger.py`,
`brokers/ibkr_paper.py`).

1. Halt.
2. In the ledger: does the intent id appear more than once in `intents`? (It cannot — PK — so two
   suspicious broker orders should map to two DIFFERENT intent ids. Diff their economic content.)
   ```bash
   sqlite3 -readonly data/platform_ledger.db \
     "SELECT intent_id, symbol, side, quantity, limit_price, source_bar_sequence_id, created_at_utc
      FROM intents ORDER BY created_at_utc DESC LIMIT 10;"
   ```
3. At the broker: list open/executed orders and compare their `orderRef` fields. Two broker
   orders with the SAME orderRef is broker-side duplication — capture it and contact IBKR if
   real. Two orders with different orderRefs means the platform generated two intents — the
   `source_bar_sequence_id` and timestamps show why.
4. Cancel the unwanted working order manually in TWS if there is one.
5. Document; rearm only when you can state which layer failed and why it will not recur.

### Unknown position

Reconciliation reports `UNEXPLAINED_POSITION`; trading is blocked; **no auto-flatten exists**
(`src/chronos/execution/reconciliation.py`).

1. Halt (reconciliation already blocks, but the halt records your involvement).
2. Identify the position's origin at the broker: TWS trade log, execution reports, other API
   client ids, manual trades. Check whether the ledger has fills for that symbol:
   ```bash
   sqlite3 -readonly data/platform_ledger.db \
     "SELECT f.* FROM fills f JOIN intents i USING(intent_id) WHERE i.symbol='<SYM>' ORDER BY f.id;"
   ```
3. Resolve at the broker, by your own manual decision: keep it (and account for it outside
   Chronos) or close it manually in TWS. The platform will not place the closing order.
4. Document origin, decision, and resulting broker state.
5. Rearm with that note once broker and ledger agree.

### Unexplained fill

A fill event or commission report for something the ledger cannot match.

1. Halt.
2. Pull the broker execution report (time, symbol, quantity, price, orderRef).
3. Match against `intents`/`fills` by orderRef (= intent id). No match: which client id placed
   it? Another API client or a manual TWS action is the usual answer; the platform-side
   equivalent (`UNKNOWN_ORDER` halt on events for unknown intents,
   `src/chronos/execution/engine.py`) will already have fired if it arrived through the adapter.
4. Treat the resulting position under "Unknown position" above.

### Stale-data trading attempt

Should be impossible: the risk engine denies on stale evidence — `STALE_MARKET_DATA` when quote
or bar age exceeds the policy limit, and a zero (default) limit denies everything; missing
snapshots deny as `MARKET_STATE_MISSING`/`ACCOUNT_STATE_MISSING` (`src/chronos/risk/engine.py`).

1. If you suspect an order was generated on stale data, check the decision trail. Shadow scans
   append the full risk decision — codes such as `STALE_MARKET_DATA` plus explanations — to the
   audit log as `shadow_scan` records. Backtest summaries count rejections in `risk_rejections`.
   Note the ledger only ever contains intents that passed risk and reached submission; a rejected
   intent leaves no ledger row, so its absence there is expected.
2. Verify the policy actually in force: `python -m chronos.cli risk-show --policy <path>` (check
   `max_quote_age_seconds` / `max_bar_age_seconds` are the values you intended — zero means deny
   everything, not "no limit").
3. If an order truly passed with stale data, that is a SEV-1 code bug: halt, capture evidence, do
   not rearm until the check is fixed and covered by a test.

### Halt-file corruption

By design this fails closed: a corrupt/unreadable `data/platform_halt.json` reads as HALTED with
reason `STATE_CORRUPTION` (`src/chronos/control/halt.py`). There is no window where corruption
means "armed".

1. Nothing is trading; confirm with `python -m chronos.cli status`.
2. Investigate cause (disk full? crash mid-write? — writes are atomic temp+rename, so torn files
   should not occur; their appearance suggests filesystem trouble).
3. Restore the halt file from backup if you want the previous reason/note preserved
   (docs/BACKUP_AND_RECOVERY.md); otherwise the corrupt state simply stands as HALTED.
4. Rearm with a note describing the corruption and what you checked.

### Audit-chain verification failure

`python -m chronos.cli verify-audit-log` reports a sequence gap, chain break, hash mismatch, or
unreadable record with its line number (`src/chronos/auditlog/log.py`).

**Treat as a tamper-or-corruption incident. Do not trade until explained.**

1. Halt.
2. Preserve the file immediately (copy with timestamps) before anything appends to it.
3. Compare against your most recent off-machine backup: the chain should be a prefix match up to
   the backup's last record. Divergence before that point means the file was modified after the
   fact; a truncated final line with an intact prefix suggests a crash mid-append.
4. Rule out benign causes (disk full, crash during append, restore mixing files from different
   backup generations) before considering tamper.
5. If tamper cannot be ruled out: assume the machine is compromised. Change broker credentials
   from a different device, review broker statements directly at IBKR, and rebuild the
   environment from clean sources before running anything again.
6. Whatever the outcome, keep the damaged file; start a fresh audit log only after documenting
   the incident, and record the old file's final good hash in your incident note.

## After any incident

- Write the incident note (what happened, evidence, resolution, prevention) and keep it with the
  evidence directory.
- Update RISK_REGISTER.md if the incident revealed a new risk or changed a mitigation's status
  (owner action — this file is maintained by hand).
- Rearm only with a note that references the incident:
  `python -m chronos.cli rearm --note "incident <date> resolved: <summary>"`.

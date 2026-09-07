# The autonomy SHADOW campaign

**This campaign proves the loop, not a strategy.** It runs the autonomy stack — model worker,
supervisor ingress, admission, journal — unattended on free local inference against the demo
broker, and the thing it produces is a distribution of *refusals*. It produces no edge, no
track record, and no evidence that Chronos is closer to trading. Read §7 before reading
anything else as a result.

Status: **runbook only.** Nothing in this repository installs, enables, or starts any of it.
`gate_advanced: none`. Related decision: **D-71**.

## 0. Four different things in this repository are called "shadow"

They belong to different subsystems and share only the word. Getting them confused is the
most likely way to misread this document.

| Name | Subsystem | What it is |
|---|---|---|
| **the autonomy SHADOW campaign** (this document) | `chronos.autonomy` / `chronos.supervisor` + `worker/` | a mandate whose `mode` is `SHADOW`, driving the model worker and the supervisor loop |
| `chronos-shadow.service`, `python -m chronos.service --mode shadow` | `chronos.execution` / `chronos.research` | the **deterministic platform's** shadow loop (`docs/DEPLOYMENT.md` §Shadow/paper service) |
| `python -m chronos.cli shadow-scan` | `chronos.research` | the deterministic platform's one-shot after-close scan (`docs/OPERATIONS.md` §Shadow scan) |
| `docs/FIVE_TOOL_SHADOW_LEARNING.md` | research lane | the five-tool learning loop |

The units in `docs/ops/` are `chronos-backend.service` and `chronos-worker.service`. They are
**not** `chronos-shadow.service` and do not replace it.

## 1. What the owner authors

Four artifacts. Three are the campaign; the fourth is §2.

1. **The SHADOW mandate** (`AUTONOMY_MANDATE_FILE`). Mode `SHADOW`, with real capital,
   concentration and market-data numbers even though nothing can spend them. A mandate
   authored with permissive zeros is a different document from the one that will eventually be
   promoted, and `mandate check` flags them.

   > **The instrument scope must be drawn from the demo broker's deterministic contract set,
   > or the campaign gathers nothing.** That set is **AAPL, MSFT, SPY, AMD, NVDA, IWM, TSLA**
   > (`src/chronos/broker/demo.py:511-518`; quotes at `:581-595`). A mandate scoping anything
   > else fails the *whole* gather on the first tick — `BackendGatherers.cycle_facts` requests
   > an underlying quote for the scope, and one unknown symbol takes the cycle with it. This is
   > the runbook's own §7 failure in miniature: it looks healthy. Observed on a demo boot with
   > a mandate scoped to `GLD`:
   >
   > ```text
   > ERROR chronos.api.autonomy  Autonomy fact gathering failed
   >     ↳ chronos.broker.base.BrokerDataError: No deterministic demo contract for GLD
   > WARNING OWNER ALERT runtime.no_facts: the supervisor could not gather the facts a cycle needs
   > ```
   >
   > — while `/health` read `startup_faults: []` and `autonomy: RUNNING`. Ninety days of that
   > produces none of §4's products and never once looks wrong on the daily check.
   >
   > **Re-verify the set rather than trusting this list**, the same way §3 asks you to
   > re-verify the model tag: read `_make_underlyings` at the head you are running. Check the
   > quality too — the demo reports `DataQuality.DEMO`, and **NVDA is deliberately `STALE`**
   > (`demo.py:586-592`) — so the mandate's `permitted_data_qualities` and
   > `max_quote_age_seconds` have to admit what the demo actually serves, not what a live feed
   > would.
2. **The proposer registry** (`AUTONOMY_PROPOSERS_FILE`) — one entry for this worker, from
   `python -m chronos.cli proposer mint`. The worker holds only the returned credential; the
   registry file is the owner's.
3. **Evidence binding on** — `AUTONOMY_EVIDENCE_BUNDLES=1`.
4. **A decision about `CHRONOS_WORKER_FORWARD`** — §2.

### Why 2 and 3, when SHADOW does not require them (D-71)

SHADOW runs on the static posture. **The campaign should not.** Run it on the posture you
intend to promote into, or ninety days later you hold evidence about a configuration that can
never ship:

- **ADR-0051 makes the static posture structurally un-promotable.** A PAPER- or LIVE-capable
  mandate refuses to assemble unless the registry *and* evidence binding are both configured.
  On promotion day the posture changes underneath you, and the ninety days were run on the
  other one.
- **On the static posture, admission check 9 cannot refuse.** Both sides of its comparison are
  two reads of the single `INGRESS_IDENTITY` constant, so it compares a constant against
  itself. A campaign that "exercised check 9" on that posture exercised nothing.
- **ADR-0048's credential-epoch binding has nothing to bind.** The epoch and registration
  digest stamped at enqueue and resolved at drain are inert without a credential, and so is
  any claim that the campaign tested them.

Cost: one `proposer mint` and one environment variable.

**Mint the credential with an expiry past the campaign's end.** The default is 90 days and the
campaign is ≥90 days; a credential that expires in week thirteen ends the campaign in a way
that looks like a bug. Either set `--expires-days` beyond the end, or plan the rotation as a
deliberate, recorded mid-campaign event.

## 2. `CHRONOS_WORKER_FORWARD`, and the honest relabel rule

`CHRONOS_WORKER_FORWARD` is an owner-only flag (`docs/AGENT_PROTOCOL.md` §9), built inert. No
agent sets it. The templates in `docs/ops/` ship it false, and a test enforces that.

With it **false** the worker still runs a full cycle and still contacts the backend; what it
does not do is propose. The ordering is the thing to know, because it is not what the flag's
name suggests — **every cycle begins with the evidence request, and what follows depends on the
backend's binding posture** (`worker/cycle.py`):

1. `POST /autonomy/evidence` — always, and **before** `forward` is ever consulted.
2. If the backend **issues a bundle** (binding on, which D-71 requires), the worker consumes
   that bundle directly and makes **no separate reads**. One POST per cycle is the whole
   conversation.
3. If the backend answers **404** — binding disabled — the worker falls back to composing
   locally, and *only then* does it `GET /account/summary`, `/account/positions`, `/orders`
   and `/terminal/bars`.
4. If the backend is **unreachable**, the cycle returns `NO_EVIDENCE` at step 1, before any
   GET and before any model call.

Only `POST /autonomy/proposals` is suppressed by `forward`.

**So Phase A is worth more than "the process stayed up".** It exercises the evidence-issuance
path end to end — the credential, the registry lookup, the bundle record and its hash chain —
and with binding on it writes durable rows in `autonomy_evidence_bundles`. What it does **not**
produce is the decision half: no proposal ingress, no drain, no admission, no decision attempt,
no supervisor journal row, and no owner alert from a cycle. Phase B adds exactly that path.
The honesty rule is unchanged by Phase A being worth more: **evidence-issuance rows are not
rung-1 evidence.**

**Both halves are proved offline, in the repository, against the real application.** Neither
test binds TCP, starts a process or unit, validates a real model, or contacts a broker; both
run the actual routes, auth and file-backed persistence with only the model response
simulated. They are what "the loop works" means here — and they are not campaign evidence,
because no calendar time and no owner grant is involved.

- **`tests/integration/test_worker_phase_a.py`** — the *offline Phase A integration test*.
  Three cases against one real worker cycle with forwarding off: **binding-on** (the issued
  bundle is consumed directly, one `POST /autonomy/evidence` and no separate reads),
  **binding-off** (a real 404 then the four GETs), and **unreachable** (`NO_EVIDENCE` at the
  first request, before any GET and before any model call). **Every case asserts zero
  proposals** — none attempted, none received at ingress, and no committed queue or
  decision-attempt row.
- **`tests/integration/test_worker_phase_b.py`** — the *offline Phase B SHADOW path test*.
  Forwarding on **only inside a private dict passed to `load_config`** — no environment, unit,
  file or default sets it, so this is not the operator switch of §2. It follows one proposal
  worker → ingress → drain → admission, where a SHADOW mandate refuses it with
  `MODE_CANNOT_SUBMIT` as the *first* failing check, journaled with the posture that judged it;
  plus evidence expiry before drain and a rollback after provisional admission. **Zero broker
  submissions in every case.**

What they do not establish: an admitted trade, reservation survival at the order-plane handoff
(that is #151's fault-injection evidence), sustained operation, or promotion eligibility. Phase
A remains the forwarding-off evidence proof; Phase B proves the refusal path, not a trade.

> **The relabel rule.** A run with `CHRONOS_WORKER_FORWARD=false` is a **worker-stability
> campaign**. It is never counted as promotion-ladder rung 1, in any document, summary or
> promotion record. Rung 1 requires decision, receipt, scheduler and alert evidence; a worker
> that proposes nothing produces none of *that*, whatever else it produces. Evidence-issuance
> rows are not decision evidence, and a count of them is not a rung.

**What turning it on would and would not risk.** `SHADOW` is a member of
`NON_SUBMITTING_AUTONOMY_MODES` — "no broker order may be transmitted for any reason"
(`src/chronos/autonomy/enums.py`). Forwarding under a SHADOW mandate moves a proposal from the
worker to a loopback ingress that structurally cannot reach a broker, and the backend is on the
demo broker with no live capability configured. That is the specific, bounded thing the flag
would authorise here. It remains the owner's act.

**The two phases, so the decision is small and reversible:**

- **Phase A — 7 to 14 days, forwarding false.** Proves the units come up, the model stays
  loaded, the loop survives restarts and a reboot, the token budget is what we thought, and the
  logs are legible. Cheap to abandon. **The ≥90-day clock does not start here.**
- **Phase B — the owner turns forwarding on.** Nothing else changes: same mandate, same
  registry, same model. **The ≥90-day clock starts here**, because this is the first day the
  loop produces the decision evidence the ladder asks for.

**How Phase B is performed, and why it is an edit to the unit rather than to a file.**
`docs/ops/chronos-worker.service` carries `UnsetEnvironment=CHRONOS_WORKER_FORWARD`, not
`Environment=CHRONOS_WORKER_FORWARD=false`. systemd.exec(5) is explicit that settings from
`EnvironmentFile=` **override** `Environment=`, so an `Environment=…=false` line would not
have been a guarantee — the private environment file, which is not in this repository and
which no test can see, would win. `UnsetEnvironment=` is applied last and removes the variable
outright; the worker's own parser returns its default of `False` for an absent variable. Phase
B is therefore: edit the installed unit, `systemctl --user daemon-reload`, restart the worker.
A reviewed file and a visible act.

**Record the act when it happens.** §6's reset rule depends on knowing day zero, and nothing
reconstructs it later. Write down, at the moment of the flip: the **date**, the exact
**model tag and digest** then running (`ollama show --modelfile <tag>` or the digest from
`/api/tags`), the mandate's digest, and the proposer registration in force — in the campaign
record, and as a `DECISIONS.md` row so it is in the repository rather than only in an operator's
notes. That row is day zero.

If Phase B never happens, the campaign is honestly a worker-stability run and the ladder's
shadow rung stays unstarted. Say that; do not let calendar time on Phase A become a claim.

## 3. The two units

Templates: `docs/ops/chronos-backend.service`, `docs/ops/chronos-worker.service`,
`docs/ops/README.md`. User units under `~/.config/systemd/user/`, no root, with
`loginctl enable-linger` so they survive logout. Each takes a private `0600` environment file
that lives **outside this repository**.

### Backend environment

| Variable | Value | Why |
|---|---|---|
| `BROKER_MODE` | `demo` | no broker, no TWS, no gateway |
| `SYMBOL_ALLOWLIST` | a subset of the demo contract set | must be drawn from **AAPL, MSFT, SPY, AMD, NVDA, IWM, TSLA** (`src/chronos/broker/demo.py:511-518`) — see §1. The default `AAPL,MSFT,SPY` is already inside it |
| `DATABASE_URL` | `sqlite:///<state>/chronos.db` | the campaign's journal |
| `LIVE_KILL_SWITCH_FILE`, `SESSION_BASELINE_FILE` | under `<state>/` | keep them with the database: ADR-0054's two installation witnesses must travel together, and a state directory that disagrees with the database boots the backend under a recovery hold |
| `BACKEND_TOKEN_FILE` | `<state>/backend_api_token` | the worker is given this file's **value**, never its path |
| `AUTONOMY_MANDATE_FILE` | the SHADOW mandate | ADR-0017 auto-activates it on boot |
| `AUTONOMY_PROPOSERS_FILE` | the registry | §1 |
| `AUTONOMY_EVIDENCE_BUNDLES` | `1` | §1 |
| `AUTONOMY_ALERT_FILE` | `<state>/owner_alerts.jsonl` | the trail that survives a database problem |
| *unset* | every live-capable setting, every `*_FORWARD` | absence is the control |

`Restart=on-failure`, never `Restart=always`: a backend that refuses to start is usually
refusing for a reason a restart cannot fix — a recovery hold (ADR-0054), an invalid mandate, a
broken registry — and a restart loop turns one legible refusal into noise.

### Worker environment

| Variable | Value | Why |
|---|---|---|
| `CHRONOS_WORKER_PROVIDER` | `local` | ADR-0050 |
| `CHRONOS_WORKER_LOCAL_BASE_URL` | `http://127.0.0.1:11434/v1` | the default; loopback is enforced in code, not merely conventional |
| `CHRONOS_WORKER_MODEL` | **an explicit tag** | `local` deliberately has no default — "a local roster changes without notice, so there is no tag this code could guess that is not either missing or wrong" |
| `CHRONOS_WORKER_LOCAL_API_KEY` | unset | Ollama authenticates nothing; unset sends no `Authorization` header rather than an empty bearer |
| `CHRONOS_WORKER_BACKEND_URL` | **`http://127.0.0.1:8765`, set explicitly** | see the warning below |
| `CHRONOS_WORKER_API_TOKEN` | the backend token's value | loopback-checked so it cannot go to a remote host |
| `CHRONOS_WORKER_PROPOSER_TOKEN` | the minted credential | required once the registry is on |
| `CHRONOS_WORKER_SYMBOLS`, `_KINDS` | explicit, matching the mandate's scope **and drawn from the demo contract set** (§1) | an empty allowlist "until it is configured" is the inert-control shape this repository was burned by four times. The worker validates the *alphabet*, not membership — a symbol the demo cannot quote is accepted here and fails at the backend |
| `CHRONOS_WORKER_FORWARD` | `false` | §2 |
| `CHRONOS_WORKER_MAX_DAILY_TOKENS` | set it | local inference is free, not infinite; an unbounded loop is still a bug |

> **Set `CHRONOS_WORKER_BACKEND_URL` explicitly; do not accept the default. A wrong URL fails
> Phase A on the very first cycle — loudly.** The backend's `backend_port` is **8765**
> (`src/chronos/config/settings.py`); the worker's built-in default backend URL is **port
> 8000** (`worker/config.py`). Because every cycle begins with the evidence request (§2), an
> unreachable listener fails at step 1: the worker logs `Evidence issuance is unreachable:
> ConnectError`, the cycle returns `NO_EVIDENCE`, and **the model is never called** — zero
> inference, every cycle, from the first one. It does not hide until Phase B and it does not
> fail quietly; a runbook reader who sees `NO_EVIDENCE` repeating has this defect and nothing
> else.
>
> This callout says "Phase A" rather than "Phase B" because of Astra's A1 finding, whose lane
> lands the real fix (aligning the worker's default with `backend_port`). Until it does, the
> explicit variable is the whole control, and the mismatched default remains a separate work
> item against `worker/` rather than something this runbook edits.

**Before the first start, verify on the host rather than assuming** — the fleet's model roster
churns, and a tag that was present last week is not evidence:

```bash
curl -s http://127.0.0.1:11434/api/tags | jq -r '.models[].name'   # must contain your exact tag
curl -s http://127.0.0.1:11434/api/ps                             # what is already resident
```

Pin the model **digest** in the campaign record, not only the tag: `ollama pull` moves a tag
under a fixed name, and §6's reset rule turns on that.

## 4. What the campaign produces, and where it lands

Every sink already exists; the campaign adds none.

| Evidence | Where | What it is worth |
|---|---|---|
| proposals with credential epoch + registration digest | `autonomy_proposal_queue` | ADR-0048's binding, exercised for real |
| issued evidence bundles, hash-chained | `autonomy_evidence_bundles` | ADR-0028's record, with two independent origins for check 9. **Written in Phase A as well as Phase B** — issuance precedes the forwarding check (§2) |
| per-decision admit/refuse counts | `autonomy_decision_attempts` | R-31's durable attempt budget, across restarts |
| owner alerts | `autonomy_owner_alerts` **and** `AUTONOMY_ALERT_FILE` | the file is what survives a database problem |
| mandate activations | `autonomy_mandate_activations` | which document authorised each boot, by digest |
| session counters | `autonomy_session_counters` | the budget arithmetic |
| tamper-evident chain | `hash_chain_records` | **no CLI verifier exists yet** — see §5; `verify-audit-log` verifies a *different* chain |
| cycle stage and refusal codes | the supervisor journal | **the campaign's actual product** |
| task liveness, clock health, startup faults | `GET /health` | proves the scheduler ran, not merely that the process existed |
| both processes' structured logs | journald | `journalctl --user -u chronos-backend -u chronos-worker` |

**The headline artifact is the refusal distribution.** At day 90 the question the evidence
answers is *which gate refused, how often, and did any gate never fire at all* — and a gate
that never fired in ninety days is either unreachable or untested by this campaign. Saying
which is worth more than any count of decisions produced.

## 5. The daily owner check

Every command below was run against a demo boot of this exact backend, and the output shown is
what it printed — not what it ought to print.

```bash
systemctl --user is-active chronos-backend chronos-worker

curl -fsS http://127.0.0.1:8765/health \
  | jq '{status, reconciliation_status,
         startup_faults: .observations.startup_faults,
         tasks: [.observations.tasks[] | {name, state}]}'

python -m chronos.cli mandate check --file "$AUTONOMY_MANDATE_FILE"

python -m chronos.cli proposer check --file "$AUTONOMY_PROPOSERS_FILE" \
  --database-url "$DATABASE_URL"

tail -n 20 "$AUTONOMY_ALERT_FILE"
```

**The `jq` selector is load-bearing and easy to get wrong.** `HealthResponse` has `status`,
`reconciliation_status`, `liveness`, `service_readiness`, `trading_capability` and
`observations`; **`startup_faults` and `tasks` live under `observations`**. Selecting them at
the top level is not an error — `jq` prints `null`, an operator reads "no faults", and §6's
first stop condition can never fire. Observed, on a healthy boot:

```json
{
  "status": "ok",
  "reconciliation_status": "RECONCILED",
  "startup_faults": [],
  "tasks": [
    { "name": "autonomy", "state": "RUNNING" },
    { "name": "lease_heartbeat", "state": "RUNNING" },
    { "name": "reconciliation", "state": "RUNNING" }
  ]
}
```

`startup_faults: []` is the healthy reading. `null` means the selector is wrong, not that the
backend is well.

**The verb is `mandate check`, two words** — `mandate-check` is not a command and argparse
refuses it. `--file` defaults to `AUTONOMY_MANDATE_FILE` and `--account-id` to `IB_ACCOUNT_ID`,
so both may be omitted; `--strict` is available. Observed head of a valid SHADOW mandate:

```text
MANDATE FILE       .../mandate.json
STATUS             VALID — the document parses and its invariants hold

GRANT
  mandate          m-shadow-campaign v1
  mode             SHADOW
  window           2026-09-05T01:58:31+00:00 → 2027-01-03T02:58:31+00:00
  restart          RESUME_UNTIL_EXPIRY
  account          b7fac9de5823… (pseudonym)
  promotions       EQUITY=SHADOW
...
NOT ENFORCED BY THE SUPERVISOR TODAY
  concentration.max_sector_exposure_pct
```

This is the load-bearing check: a `BLOCKING` finding means the mandate and the posture have
drifted apart, which is the failure this campaign is most likely to hit — see §1 on credential
expiry.

**Pass `--database-url` to `proposer check`, or it cannot see revocations.** Without it the
command says so itself and every entry reads `UNVERIFIED`:

```text
STATUS   VALID — digest 2d2e7917882cb5dc…, 1 registration(s)
NOTE     the revocation ledger could not be read (NoLedgerFile); an entry shown
         UNVERIFIED may have been revoked and this command cannot tell
  shadow-worker  UNVERIFIED provider=local model=qwen3.8:27b@1 prompt=1
                 policy=b6ac5639ccc1a4a1 expires=2027-02-02T02:59:07+00:00
```

### First run: `campaign preflight`, once the backend has booted

Everything §1–§3 asks the owner to get right by hand, `python -m chronos.cli campaign preflight`
checks in one read-only pass (PR #167). It opens no socket, starts nothing, reads no `.env`, and
writes nothing — every path and value is an explicit argument:

```bash
python -m chronos.cli campaign preflight \
  --mandate "$AUTONOMY_MANDATE_FILE" \
  --registry "$AUTONOMY_PROPOSERS_FILE" \
  --state-dir data \
  --policy worker/policy.md \
  --model "$CHRONOS_WORKER_MODEL" \
  --worker-backend-url "$CHRONOS_WORKER_BACKEND_URL" \
  --worker-symbols "$CHRONOS_WORKER_SYMBOLS" \
  --backend-symbols "$SYMBOL_ALLOWLIST" \
  --evidence
```

`--unit` defaults to `docs/ops/chronos-worker.service`, `--provider` to `local`, `--backend-host`
to `127.0.0.1` and `--backend-port` to `8765`; pass them when yours differ. `--evidence` is a
flag, and it asserts the posture D-71 requires — omit it and the run fails, which is the point.

**Run it after the backend has booted once as writer, not before.** The eighth check compares
ADR-0054's two installation witnesses, and neither exists until the first writer boot creates
them — so before that boot the command reports `UNVERIFIED`, by design. That is not a defect and
not something to work around: the witnesses are seeded by the backend, and a read-only command
will not seed them for you.

Three verdicts, **two exit codes**:

| verdict | means | exit |
|---|---|---|
| `PREFLIGHT PASS` | all eight checks hold — the local inputs are coherent | **0** |
| `FAIL … UNVERIFIED; …` | a check **could not be evaluated** — no witnesses yet, or the 0012 adoption sentinel is still pending. The message names the repair, usually *"boot the backend writer once, then re-run preflight"* | **1** |
| `FAIL [SHADOW_CAMPAIGN §n] …` | a check was evaluated and **failed**; the bracket names the section of this runbook that fixes it | **1** |

`UNVERIFIED` shares its exit code with `FAIL` deliberately: a check that was not examined must
not read as a check that passed, so both stop the sequence. Read the word, not just the code —
they call for different actions.

Observed on this backend: after a first writer boot, exit **0** with eight `PASS` lines; against
an empty state directory, exit **1** with
`UNVERIFIED; missing state_generation marker and installation_identity row`; with a worker URL on
the wrong port, exit **1** with `backend URL: http://127.0.0.1:<other-port> does not match
http://127.0.0.1:8765`.

**What a PASS does not mean.** It is a statement about local files and configuration only. It
does not prove a backend is listening, a model tag is installed, a worker can reach anything, or
that a campaign has begun — the backend-URL check is an exact configuration comparison and says
`(not probed)` in its own output. Reachability is still §5's health check and the facts check
below.

### First run: confirm the first tick actually gathered facts

A scope the demo broker cannot quote fails *silently* on the daily check above (§1), so check
once, explicitly, before trusting anything:

```bash
journalctl --user -u chronos-backend --since "-10 min" \
  | grep -E "fact gathering failed|no_facts|No deterministic demo contract" || echo "facts OK"
```

Two smaller things that bite on day one, both observed:

- **`mandate template` output is not a JSON file.** Its stdout is the object followed by a `#`
  comment block, so `mandate template > mandate.json` then `mandate check` reports
  `INVALID — Invalid JSON: trailing characters`. Strip the block, or write only the object.
- **`AUTONOMY_ALERT_FILE` does not exist until the first alert is delivered**, so a bare `tail`
  fails on day one. Use `[ -f "$AUTONOMY_ALERT_FILE" ] && tail -n 20 "$AUTONOMY_ALERT_FILE"`.
- **A stale alert is delivered on the next boot**, and the file sink stamps `raised_at` with the
  *delivery* time while the database row keeps the true one. When §6's "any CRITICAL alert →
  stop" fires just after a restart, read `raised_at` from `/terminal/alerts` or the table before
  concluding it is new.

### Weekly, and the chain the campaign actually produces

`python -m chronos.cli verify-audit-log` verifies the **deterministic platform's** audit-log
file (`chronos.auditlog.verify_chain` over `--audit-file`). It is worth running, and it is
**not this campaign's chain.** On a fresh state directory it prints:

```text
audit log: ABSENT — no audit log yet
```

The campaign's decision stream is the hash chain in **`hash_chain_records`**, verified in code
by `chronos.persistence.hash_chain.verify(session, stream)` — which **has no CLI entry point
today**. Its only in-tree callers are `supervisor/position_management.py` and the terminal
journal view's per-row recomputation. So until a verifier ships, the operator's surface for the
decision stream is the terminal journal (`GET /terminal/journal`), which recomputes each row as
it renders it. **A `chronos.cli` verifier for `hash_chain_records` is a named follow-up, not
something this runbook can stand in for**; do not read a green `verify-audit-log` as evidence
about the campaign's own chain.

Also weekly: one look at the refusal-code distribution (§4).

## 6. Stop conditions

Stop, and do not restart until the cause is understood:

- `/health` reports any `startup_fault` — check `.observations.startup_faults`, and read `[]`
  as healthy and `null` as a broken selector (§5). `autonomy_posture_unauthenticated` is the
  one to expect. `recovery_unverified` (ADR-0054) means this host's state directory and its
  database disagree — a restore, a lost volume, or a replaced database — and the backend has
  booted read-only and unreconciled until an operator acknowledges it with a note. The
  wholesale-restore residual stays manual: see `docs/BACKUP_AND_RECOVERY.md`.
  `evidence_posture_invalid` means `AUTONOMY_EVIDENCE_BUNDLES` is on with no
  `AUTONOMY_PROPOSERS_FILE` — a bundle is issued *to* a credential, so there is no
  author to issue to, and every proposal refuses until one of the two settings changes
  (`api/main.py:269`). `submission_reconciliation_failed` means the startup submission
  reconciliation raised: submission remains locked, while inspection, cancellation and
  recovery stay available (`api/main.py:353`).
- `mandate check` returns a `BLOCKING` finding; the proposer credential expires or is revoked.
- The kill switch reads ENGAGED with no operator having engaged it.
- `verify-audit-log` reports a broken platform chain, or a terminal journal row fails its own recomputation (§5 — these are two different chains).
- **`runtime.no_facts` recurring.** One is a transient; a run of them means the mandate's scope
  is outside what the demo broker can quote (§1), and the campaign is producing nothing while
  reading healthy.
- A CRITICAL owner alert — on a restart check `raised_at` first (§5); a stale one is redelivered.
- The refusal distribution changes shape with no change to explain it.
- Anything reaches for a broker. There is nothing to reach with — if it ever happens it is an
  incident, not a campaign observation.

> **The reset rule.** Any change to the model tag or digest, the policy file, the mandate, or
> the posture is a material change and **resets the ninety-day clock**
> (`docs/VISION_COMPLETION_PLAN.md` §13). This is the rule most likely to be broken quietly: a
> model swapped "to try something better" in week six ends the campaign and starts a new one.

## 7. What would make the evidence worthless

The research programme's honest output is that **zero strategies are selected**, and that this
is a real computed result whose blocker is the corpus, not the code
(`docs/STRATEGY_SELECTION.md`). Nothing in this campaign changes it. Each item below is
someone forgetting that:

- **Reading the worker's decisions as a track record.** The worker runs a discretionary
  day-trader prompt, not a frozen strategy policy. Its output cannot earn a promotion artifact
  (`docs/VISION_COMPLETION_PLAN.md` §6 item 8), and "the model was right N times", measured on
  data with no holdout, is exactly the bias the research freeze exists to prevent.
- **Treating demo fills as execution evidence.** The demo broker is synthetic. Nothing here
  measures spread, slippage, fill probability or market impact, and calendar time does not
  change that. Both real IBKR adapters still return non-authoritative option deliverables, so
  real option selection remains `NO_TRADE` by construction.
- **Counting a forwarding-off run as rung 1** (§2's relabel rule).
- **Running on the static posture** and later claiming the authenticated path was exercised (§1).
- **Silent mid-campaign changes**, including an `ollama pull` that moves a tag (§6).
- **Believing ninety days changed the corpus.** It does not. SPY still ends 2019-11, QQQ's
  holdout is still spent, and IWM/GLD/TLT are still 757 two-decimal transcribed bars. A finished
  campaign leaves the mission's critical path exactly where it was.

The honest claim at day 90 is: *the autonomy loop ran unattended for ninety days on free local
inference, refused everything it should have, and here is the distribution of what refused and
what never fired.* Nothing about edge; nothing about readiness to trade.

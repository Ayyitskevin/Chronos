# B1 swap runbook — moving from the demo broker to the read-only paper gateway

**Audience: the owner, on B1 day.** Status: **no real IBKR gateway has ever been connected to
Chronos** (`docs/ops/m4-read-only-gate-session-checklist.md:3-5`). This runbook turns the parts
of the swap that are *not* configuration into an ordered checklist. It authorizes nothing, runs
nothing and holds no secret: **every command below is TEXT ONLY — NOT RUN** by the agent that
wrote it, and every "expected output" is derived from the code or documents cited, not
observed against a gateway. Where it says *expected*, write down what you actually see.

Companion documents: the session checklist (`docs/ops/m4-read-only-gate-session-checklist.md`,
"the checklist") owns the capture sessions after this day; `docs/IBKR_RUNBOOK.md` owns
reconnects and incidents; `docs/ibkr_setup.md` owns the API install. Line numbers are at the
commit this file was written on and drift; the tests named in each step pin the facts.

**Why a runbook at all.** Moving to the real paper adapter is **not "config only"**: ten keys
change (checklist `:87-101`) plus one the checklist's block omits (a `DATABASE_URL` of its own,
step 6); the factory builds a different adapter with three safety hooks
(`src/chronos/runtime.py:296-326`); and five things are owner acts, not configuration (steps
2-6 and your word in step 10). Contract and rehearsal: `tests/unit/test_b1_swap_contract.py`.

## Conventions
- Run everything from the repository root, in the Chronos venv (`.venv/bin/...`).
- Never paste a secret, a password or an account id into a chat, a ticket or a repository file.
  Never put an IBKR username or password in any Chronos file (checklist `:103`).
- **Stop rules.** A live port (7496 TWS / 4001 Gateway) listening, a banner that does not say
  paper/simulated, or any `True` where this runbook expects `False`: stop, do not connect.

## Ordered steps

### Step 1 — A clean process environment
**Why.** The process environment outranks the `.env` file, in both directions
(`src/chronos/config/settings.py:90-92`; pinned by
`test_an_exported_variable_silently_beats_the_env_file`). A stale `export IB_PORT=7496` in a
shell profile defeats everything below.
```text
# NOT RUN
env | grep -E '^(BROKER_|IB_|ALLOW_|AUTONOMY_|CHRONOS_)' | cut -d= -f1
```
**Expected:** no output. Any name printed: unset it in this shell and find where it is exported
(profile, systemd unit `Environment=`, `.envrc`). The command prints **names only** (`cut`), so
an exported account id or pepper is not echoed. The prefix list is the preflight's P12 list.

### Step 2 — A paper account (owner act)
You provide an IBKR **paper** account; credentials and 2FA stay with you and never enter a file
(checklist `:69-70`, VCP §11 `docs/VISION_COMPLETION_PLAN.md:676-689`). Paper ids match
`D[UF]\d{4,}` (`docs/IBKR_RUNBOOK.md` §1; the same pattern is enforced in
`src/chronos/control/modes.py:139-143`); anything else is treated as live/paper confusion and
fails closed.
```text
# NOT RUN — shape check only, on a string you type (it is not saved by this command)
read -r -p "paper id: " ID; printf '%s' "$ID" | grep -Eq '^D[UF][0-9]{4,}$' && echo "paper-shaped" || echo "NOT paper-shaped"
```
**Expected:** `paper-shaped`. Anything else: stop.

### Step 3 — The gateway, logged in to paper, API settings (owner act)
Log in to TWS or IB Gateway with the **paper** credentials; the banner must say
paper/simulated. Configure → API → Settings (checklist `:72-81`; `docs/IBKR_RUNBOOK.md` §2):
enable socket clients; keep **"Allow connections from localhost only"**; check **Read-Only API**.
Note the socket port: **7497** (TWS) or **4002** (Gateway). The adapter refuses any other port
for paper (`src/chronos/broker/official_ibkr.py:269-270`, `:886-896`) — do not rely on that:
fix the session.
```text
# NOT RUN
ss -tlnp | grep -E '7497|4002'                       # the paper API socket
ss -tlnp | grep -E '7496|4001' || echo "no live port listening"
```
**Expected:** one `LISTEN` line on exactly one paper port; then `no live port listening`. A live
port listening means you are in a live session: stop.

### Step 4 — Market-data entitlements (owner act)
Enable market data for at least the **first** symbol in `SYMBOL_ALLOWLIST` (the smoke test
qualifies only the first symbol and fails on an `UNKNOWN`/empty quote by design; checklist
`:82-84`; `docs/ibkr_setup.md`). There is no offline check. Missing permissions surface at first
contact as explicit missing-data states (`UNKNOWN` quote quality; checklist `:199`
"quote permissions").

### Step 5 — The official TWS API, installed and pinned (owner act)
`ibapi` is **not on PyPI** and deliberately not in the lockfile
(`docs/ibkr_setup.md`, "Installing the official TWS API"). Without it the adapter's constructor
fails first, before any other check (`src/chronos/broker/official_ibkr.py:918`, `_load_ibapi`).
Install from IBKR's distribution, then **record the version string — your note is the pin**.
```text
# NOT RUN — install (owner), from the unzipped IBJts directory
cd IBJts/source/pythonclient && /path/to/Chronos/.venv/bin/pip install .
# verify (from the Chronos root)
.venv/bin/python -c "import ibapi; print(ibapi.__file__)"
.venv/bin/pip show ibapi | grep -E '^(Name|Version):'
```
**Expected:** a path inside the Chronos `.venv` `site-packages`; then `Name: ibapi` and a
`Version:` line. Write the version in your session evidence doc (checklist `:254-262`).
**Rollback:** `.venv/bin/pip uninstall ibapi` (reversible; Chronos runs in demo mode without it).

### Step 6 — The private `.env`, with its own `DATABASE_URL` (owner act)
Create `.env` at the repository root. It is untracked, mode 0600, never committed. Start from
the checklist's block (`:87-101`) **verbatim**, replace the placeholder account with your real
paper id, and add **two lines the block does not carry**:

```dotenv
BROKER_MODE=ibkr
BROKER_ADAPTER=official_ibkr
IB_ENVIRONMENT=paper
IB_HOST=127.0.0.1
IB_PORT=7497            # or 4002 — the port step 3 showed
IB_CLIENT_ID=17
IB_ACCOUNT_ID=<your paper id; DU1234567 below is the checklist placeholder>
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
SYMBOL_ALLOWLIST=AAPL,MSFT,SPY
# AUTONOMY_MANDATE_FILE stays unset
DATABASE_URL=sqlite:////absolute/path/outside/the/repo/chronos-b1.db
CHRONOS_CAPTURE_PEPPER=<64 hex, generated below; never shown to anyone>
```
- **`DATABASE_URL` (why it is here, though the checklist omits it).** The first backend start
  binds the database file to `(broker mode, environment, account fingerprint)`
  (`src/chronos/runtime.py:337` → `src/chronos/ui/runtime_scope.py:161` →
  `src/chronos/persistence/database.py:218`). A file already bound to another scope refuses:
  "This Chronos database is already bound to a different broker scope; configure a separate
  DATABASE_URL." (`database.py:254-258`), and a file that holds account data but no scope refuses
  to bind (`:236-240`). The default is the relative `sqlite:///data/chronos.db`
  (`settings.py:320`; `.env.example:54`), which any earlier demo boot from the repo root has
  already bound to DEMO. Use a **dedicated absolute path** (four slashes). Pinned by
  `test_a_database_bound_to_the_demo_scope_refuses_the_b1_shape_until_it_has_its_own_url`.
- **`CHRONOS_CAPTURE_PEPPER`** is mandatory for any capture: the harness refuses without it,
  before anything is written and before any connection
  (`.claude/skills/chronos-real-gateway-campaign/SKILL.md:185-187`;
  `…/scripts/capture_readonly.py:68`, `:157`). It is **not** a product setting; `Settings`
  ignores the line (`settings.py:92`, `extra="ignore"`; pinned by
  `test_a_pepper_line_in_the_private_env_does_not_disturb_settings`). Generate it yourself;
  keep it only in this file (`.env.example:210-219`).
- `AUTONOMY_MANDATE_FILE` stays unset: a present, valid file auto-activates autonomy on every
  backend boot (ADR-0017; checklist `:62-64`).
```text
# NOT RUN — the pepper goes straight into the file: never to the screen, a log or a clipboard
umask 077 && touch .env && chmod 600 .env
PEP=$(python3 -c 'import secrets; print(secrets.token_hex(32))'); printf 'CHRONOS_CAPTURE_PEPPER=%s\n' "$PEP" >> .env; unset PEP
stat -c '%a %n' .env
test ! -e /absolute/path/outside/the/repo/chronos-b1.db && echo "database file absent (fresh)"
```
Run the pepper line **once** (do not append a second `CHRONOS_CAPTURE_PEPPER=` line), and add the
other lines with your editor. **Expected:** `600 .env`; `database file absent (fresh)` (a
pre-existing file must be unbound or already B1-scoped; if you are unsure, choose a new path).

### Step 7 — The configuration audit: both conjunctions must print `False`
This is checklist §3.3 step 2 (`:108-118`), unchanged.
```text
# NOT RUN
.venv/bin/python -c "
from chronos.config.settings import Settings
s = Settings()
print(s.broker_mode.value, s.broker_adapter.value, s.ib_environment.value, s.ib_port)
print('transmit', s.allow_order_transmit, 'live', s.allow_live_trading)
print('mandate', s.autonomy_mandate_file)
print('transmission_possible', s.transmission_possible)
print('live_transmission_possible', s.live_transmission_possible)"
```
**Expected** (derived by loading a dummy file of the same shape, not from your file):
```text
ibkr official_ibkr paper 7497
transmit False live False
mandate None
transmission_possible False
live_transmission_possible False
```
(`7497` or `4002`, whichever you set.) `ALLOW_LIVE_TRADING=true` on its own makes `Settings()`
raise a `ValidationError` ("ALLOW_LIVE_TRADING=true requires the full live conjunction …",
`settings.py:386-421`): that is the refusal, not a bug. Any `True`: stop.

### Step 8 — The non-secret preflight, on your own file (optional but recommended)
`tests/unit/test_b1_env_preflight.py` defines `preflight_b1_env(text, file_mode=, environ=)`
and runs it against **dummy** values in CI. The function opens no file of its own and reads no
`.env` (an audit-hook test with sentinel `.env` files in the working directory and in `$HOME`
proves it); you hand it text. You may hand it **your own** file's text, in your own shell. It
prints only condition ids and messages — never a value (the pepper's shape is checked, not
echoed). **Requirements:** run it from the repository root, in the Chronos venv with the dev
dependencies (`pytest` is imported by that module). A supported checker script under `scripts/`
is a follow-up: this change is docs and tests only, so the command imports the test module.
```text
# NOT RUN — Kevin's hands; reads his own private .env, prints condition ids only
.venv/bin/python -c "
import os
from pathlib import Path
from tests.unit.test_b1_env_preflight import preflight_b1_env
p = Path('.env')
found = preflight_b1_env(p.read_text(), file_mode=p.stat().st_mode, environ=os.environ)
for cid, msg in found:
    print(cid, msg)
print('conditions unmet:', len(found))"
```
**Expected:** exactly one line, `conditions unmet: 0`. Any `P<n> …` line above it names an unmet
condition (next section); fix the file and run it again.

### Step 9 — Offline rehearsal, and the safety-state inventory
```text
# NOT RUN
.venv/bin/pytest tests/unit/test_b1_swap_contract.py tests/unit/test_b1_env_preflight.py -q
.venv/bin/pytest tests/integration/test_ibkr_smoke.py -ra
.venv/bin/python -m chronos.cli status
ls data/live_kill_switch.json
```
**Expected:** the first line ends in `passed` with no failure (the module counts will have
changed since this file was written: read the summary, do not match a number). The smoke test
**must skip**, with a reason naming `CHRONOS_RUN_IBKR_SMOKE` (checklist `:124-127`): if it runs
or errors, stop (your environment already opts in, or collection is broken). `status` prints the
platform halt and audit-chain state (default paths `data/platform_halt.json`,
`data/platform_audit.jsonl`); a **missing** kill-switch file means *disengaged*, the opposite of
the platform halt, whose missing file reads *halted* (checklist `:119-123`). Record the output
in the session-1 evidence doc.

### Step 10 — Your word, then the sessions
Nothing above connects to the gateway. **Session 1 starts only on your word** (checklist `:14`,
VCP §11). From here the checklist owns the procedure: §4.1 (smoke test, session 1 only, then the
capture), §4.3 (per-session checks), §5 (the restart session), §6 (sanitization before anything
leaves the machine), §8 (the measured EXIT). Remember what the sessions do and do not exercise:
the capture harness builds the adapter **hookless**, by design
(`…/capture_readonly.py:295-297`), and never calls an order method
(`capture_readonly.py:10-11`); the three safety hooks of `runtime.py:321-326` matter only when
the backend runs.

## Preflight conditions
The ids below are the contract between this runbook and
`tests/unit/test_b1_env_preflight.py`; `test_the_runbook_and_the_preflight_list_the_same_conditions`
fails if they drift. "Step" names where the condition is created.

| id | condition | step |
|---|---|---|
| P1 | the file's settings load (Settings accepts them) with no process-env override | 6, 7 |
| P2 | BROKER_MODE=ibkr, BROKER_ADAPTER=official_ibkr, IB_ENVIRONMENT=paper | 6 |
| P3 | IB_HOST is 127.0.0.1 and IB_PORT is a paper port (7497 TWS or 4002 Gateway) | 3, 6 |
| P4 | IB_ACCOUNT_ID has the paper shape D[UF]+digits and is not the checklist placeholder | 2, 6 |
| P5 | ALLOW_ORDER_TRANSMIT and ALLOW_LIVE_TRADING are false and both conjunctions are false | 6, 7 |
| P6 | AUTONOMY_MANDATE_FILE is unset | 6 |
| P7 | DATABASE_URL is set to its own absolute sqlite file, not the shared default | 6 |
| P8 | IB_CLIENT_ID and IB_DATA_CLIENT_ID differ; SYMBOL_ALLOWLIST is non-empty | 6 |
| P9 | no IBKR credential-like key is in the file | 6 |
| P10 | CHRONOS_CAPTURE_PEPPER is present with the 64-hex shape (value never echoed) | 6 |
| P11 | the file mode is 0600 (when a mode is supplied) | 6 |
| P12 | no BROKER_*/IB_*/ALLOW_*/AUTONOMY_*/CHRONOS_* variable is exported (it outranks the file) | 1 |

What the preflight cannot see: the gateway, the entitlements, `ibapi`, whether the pepper is
the one you meant, whether the account id is *your* paper account, and whether an existing
database file is bound. Those are steps 2-6.

## What stays false (or unset), and until which gate
| setting | state | until | cite |
|---|---|---|---|
| `ALLOW_LIVE_TRADING` | **false, always, on this path** | never part of B1; live needs the full ADR-0009 conjunction (`IB_ENVIRONMENT=live`, a live `U…` account on `IB_ACCOUNT_ALLOWLIST`, arming, typed confirmation) | `settings.py:386-421`, `:502-523`; checklist `:50-56` |
| `ALLOW_ORDER_TRANSMIT` | **false** for the whole read-only campaign | the campaign's EXIT (≥5 sessions including a restart, no mutation, no leak, no drift, no unexplained callback or pacing failure, fixtures replay offline exactly; checklist `:310-322`) **and a separate Kevin decision** for paper order transmission (ADR-0025 fixes a paper floor of ≥20 order lifecycles across ≥10 paper sessions, `docs/adr/ADR-0025-owner-directed-live-experimentation.md:96-102`; this is the author's reading — no document found grants transmission) | `settings.py:490-499` |
| …and when it is turned on | also needs `IB_ACCOUNT_ALLOWLIST` containing the paper id (the mode lock denies "paper account allowlist is empty"), a non-blank `IB_ACCOUNT_ID`, a broker-reported paper environment, readiness RECONCILED | | `src/chronos/control/modes.py:126-143`; `settings.py:448-453` |
| `AUTONOMY_MANDATE_FILE` | **unset** for the whole campaign | D2 (you author the mandate) | checklist `:62-64`, `:100` |
| `CHRONOS_RUN_IBKR_SMOKE` | **unset** unless you opt in to the read-only smoke on session 1 | your word for a session | checklist `:124-127`; `scripts/smoke_test_ibkr.py` force-sets it itself |
| exported `BROKER_*`, `IB_*`, `ALLOW_*`, `AUTONOMY_*` | **none** | always | step 1 |

`IB_ACCOUNT_ALLOWLIST` is deliberately **not** in the read-only block (default empty,
`settings.py:134`); `ALLOW_ORDER_TRANSMIT=true` alone is *accepted* by `Settings` and flips
`transmission_possible` (`:490-499`) — the empty allowlist is refused later, by the paper mode
lock (`modes.py:128-129`). Do not rely on that layering: leave the flag false.

## Rollback — in this order, each step reversible
1. **Stop.** End any session; disconnect/log out of the gateway. Do not leave the API socket
   enabled and idle.
2. **Back to demo — two edits, not one.** In `.env` set `BROKER_MODE=demo` (keep both `ALLOW_*`
   lines at `false`) **and remove or comment out the `DATABASE_URL=…chronos-b1.db` line** (the
   B1 file itself stays untouched, step 3). With only the first edit, demo would boot against the
   B1-bound file and `Database.bind_scope` would refuse: "already bound to a different broker
   scope; configure a separate DATABASE_URL" (`database.py:254-258`). Or move the whole file
   aside, *not delete it*: `mv .env .env.b1-aside`. Verify the configuration:
   ```text
   # NOT RUN
   .venv/bin/python -c "from chronos.config.settings import Settings; s = Settings(); print(s.broker_mode.value, s.ib_environment.value, s.database_url); print(s.transmission_possible, s.live_transmission_possible)"
   ```
   **Expected** (either way; defaults at `settings.py:99`, `:102`, `:320`, `:127-128`):
   ```text
   demo paper sqlite:///data/chronos.db
   False False
   ```
   If the first line shows your `chronos-b1.db` path, the `DATABASE_URL` line is still active.
   **This proves the configuration, not the boot:** whether the demo database accepts the demo
   boot is shown by the next demo backend start (it binds `data/chronos.db` to the demo scope,
   or finds it already so). A `data/chronos.db` that an earlier mistaken B1 start with the
   default URL bound to B1 refuses it; then point demo at a fresh `DATABASE_URL`.
3. **Leave the B1 database file where it is.** It is bound to the B1 scope by design
   (`database.py:218`). Demo uses the default `sqlite:///data/chronos.db`, **a different file,
   only because step 2 removed the B1 `DATABASE_URL` line**; nothing needs migrating back. Do
   not delete either file (reversible mistakes only). If you want a clean B1 retry, choose a
   **new** `DATABASE_URL` path.
4. **Safety state.** Re-run `python -m chronos.cli status` and `ls data/live_kill_switch.json`
   (step 9). If the platform is *halted*, it stays halted across restarts by design
   (`docs/IBKR_RUNBOOK.md` §6); re-arm only with `python -m chronos.cli rearm --note "<why>"`
   after you understand the halt and reconciliation is clean. A read-only session runs no order
   plane, so note the halt state you observed.
5. **ibapi** may stay installed (no effect in demo mode) or be uninstalled
   (`pip uninstall ibapi`, step 5). **The pepper** stays in the untracked file; rotating it means
   new tokens, old fixtures remain valid but unlinkable to new ones, and losing it loses only
   linkability (`.env.example:215-218`).
6. **Evidence you already captured** stays off-repo until the gate-passage PR (checklist `:299-308`);
   a rollback is not a reason to delete it.

## Not proven offline — what this runbook cannot tell you
- **First contact.** That the real adapter reports the startup observations the runtime binds the
  scope with (one connected account, `PAPER`, quality `UNKNOWN`), that the connected session
  manages the configured account (`official_ibkr.py:982-986`), and that `ibapi` delivers the
  callbacks the bridge turns into hooks. Only a gateway shows it.
- **Gateway-side facts:** the entitlements granted, the Read-Only API checkbox actually taking
  effect, pacing, the daily restart, the host.
- **The adapter's behaviour once connected.** The rehearsal constructs the real class with only
  its `ibapi` import stubbed and never connects (`test_b1_swap_contract.py`).
- **Steps 2-6 and 10** are your acts and your word; the agent that wrote this ran none of them.
- **Python version.** The tests the preflight shares a module style with are CI-run on 3.12; the
  author ran them on 3.14.4 only.

## Sources
`docs/ops/m4-read-only-gate-session-checklist.md` · `docs/IBKR_RUNBOOK.md` · `docs/ibkr_setup.md`
· `docs/VISION_COMPLETION_PLAN.md` §7, §11 · `src/chronos/config/settings.py` ·
`src/chronos/runtime.py` · `src/chronos/broker/official_ibkr.py` · `src/chronos/control/modes.py` ·
`src/chronos/persistence/database.py` · `.env.example` ·
`.claude/skills/chronos-real-gateway-campaign/` · `tests/unit/test_b1_swap_contract.py` ·
`tests/unit/test_b1_env_preflight.py` · the delivery contract the rehearsal was built from
(`B1-DELIVERY-CONTRACT.md`, a Flow document, not in the repository).

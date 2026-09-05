# ADR-0049: A lost live-safety state file fails closed

- Status: Accepted (owner review required before merge)
- Date: 2026-09-03
- Deciders: opus seat (author), owner (merge gate)
- Related: R-66, D-63, ADR-0009 (the live gate stack), `docs/VISION_COMPLETION_PLAN.md`
  §6 finding 3, `docs/BACKUP_AND_RECOVERY.md`

## Context

The live order plane keeps two durable safety files beside each other:
`data/live_kill_switch.json` and the session-drawdown baseline. Both answered a missing
file with the permissive reading:

- `chronos.orders.kill_switch` returned `KillSwitchState(engaged=False)` on
  `FileNotFoundError` — documented as the fresh-deploy default, so a new backend trades
  subject to the other gates.
- `chronos.orders.session_drawdown` returned `None` on `FileNotFoundError`, which means
  "establish a fresh baseline" — so a breaker whose 100,000 baseline was deleted and then
  asked about 98,000 re-baselined at 98,000 and reported no breach.

On a genuinely fresh install both readings are correct: nothing has been written, so
nothing has been lost. The defect is that absence cannot distinguish that case from a
restore that omitted the file, a container that lost its sidecar volume, or an operator
who deleted a file to "clear" it. `docs/BACKUP_AND_RECOVERY.md` has carried the hazard as a
disclosed residual since 2026-08-02, with a manual operator step as the compensating
control, and `docs/VISION_COMPLETION_PLAN.md` §6 finding 3 lists the end state: recovery
boots kill-engaged, read-only, and unreconciled.

The 2026-09-03 team review re-derived both halves from the current commit, and the
drawdown half was reproduced as a probe before this change.

## Decision

Add an installation marker, `chronos.orders.state_generation`, written beside the state
files it describes. Each component records in the marker the first time it durably writes
its own state file, immediately after that write succeeds. A component's read path then
distinguishes two cases that used to look identical:

- **never materialised** — no marker, or the marker does not name this component: absence
  is a fresh install and keeps the permissive reading;
- **materialised, now missing** — the marker names this component and the file is gone:
  the kill switch reads ENGAGED with a reason naming the loss, and the drawdown breaker
  refuses (and engages the kill switch) rather than re-baselining.

A marker that is present but unreadable counts as materialised for every component: a
marker we cannot read is not evidence that a file was never written.

## Why not the alternatives

- **"Missing always reads ENGAGED."** Simplest, and it makes every fresh deploy boot
  killed with no operator anywhere near it. That is a different product decision, and one
  the owner would have to want.
- **"Write the state files at install time."** The kill switch would then have to write on
  a read path, and a read that writes is a worse contract than a marker that records.
- **"Detect restores in the backup tooling."** The tooling is not in the trading path; a
  deletion outside it — the case the review found — would still be invisible.

## Consequences

- A partial restore of the state directory now boots with the emergency stop engaged until
  an operator disengages with a note. That is the intended direction and it is disruptive
  by design; the reason string says exactly why.
- A restore that brings back **nothing** — marker included — still presents as a fresh
  install. The residual is disclosed in `docs/BACKUP_AND_RECOVERY.md`; the manual step
  remains the control for it.
- Only the *kill-engaged* half of §6 finding 3 is closed. Booting **read-only** and
  **unreconciled** after a restore is untouched here, as is the mandate file's
  auto-activation on boot (ADR-0017).
- The marker is not authority: it grants nothing, gates nothing, and names no account. It
  records that a file once existed.

## Verification

```bash
.venv/bin/python -m pytest -q tests/unit/test_live_safety_layer.py
```

Seven tests cover the loss of an engaged switch, the loss after a disengagement, the fresh
install that must stay permissive, the deleted baseline that must refuse, the fresh
baseline that must still be established, the unreadable marker, and the two components
sharing one marker without erasing each other. Each guard was mutation-proved: removing
the kill-switch marker check fails exactly three of them and removing the drawdown check
fails exactly one, with everything else green.

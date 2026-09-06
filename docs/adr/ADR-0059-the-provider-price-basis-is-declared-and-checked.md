# ADR-0059 — The provider price basis is declared, and a split-adjusted feed cannot be certified

Status: **accepted — ruled by the lead on 2026-09-06 under the owner's delegated steering, after a
design review HOLD was cleared. Slice 1 only (below); relaxable only by the owner, in writing.**
Index entry: DECISIONS.md D-75. Risk entry: RISK_REGISTER.md R-77. Amends ADR-0053 (the intake
contract) by adding a required key; loosens nothing in it.

## Context

`INTAKE.json` already carried `adjustment_policy`, fixed at `unadjusted_as_traded`. That single
field was doing two unrelated jobs:

- **A contract claim** — *Chronos requires as-traded levels.* Always true, never a choice.
- **A vendor fact** — *the delivered bytes are as-traded.* Sometimes false, and nothing checked it.

Because one key carried both, a delivery could satisfy the contract by restating it. The most
likely real export makes this concrete: IBKR's `TRADES` bar feed back-adjusts history for splits,
and its `ADJUSTED_LAST` feed adjusts for splits *and* dividends. Either one, exported and declared
`unadjusted_as_traded`, was accepted — the declaration described the requirement rather than the
data, and no code disagreed.

## The failure this prevents, and why the obvious guard does not catch it

A split **inside** the certified window is already caught: certification reconciles the
split-implied return against the bars and raises `UNRECONCILED_SPLIT`. The uncaught case is a
split **after** the delivered window.

When a vendor restates history, a 2-for-1 split occurring after the window halves every close in
the file — including bars years earlier. Three separate mechanisms then fail to notice:

- `research/certification.py` reconciles split-implied returns only for ex-dates falling inside a
  certified window. A later ex-date is not examined.
- `research/adjust.py` skips future-dated actions outright, so it applies no compensating factor.
- The same module computes its dividend factor as a ratio against the **delivered** close. Halved
  closes therefore double every `dividend / close` term — the error compounds rather than cancels.

The result is a self-consistent series that reconciles cleanly, certifies `CERTIFIED`, and is
wrong by a factor of two. No arrangement of in-window evidence detects it, because the evidence
that would is dated outside the window by construction.

**This is why an empty in-window split set is not an escape hatch.** The natural mitigation —
"accept a split-adjusted feed when no symbol split during the window" — is exactly backwards: it
grants permission on the strength of the one observation that carries no information about the
hazard.

## Decision

`INTAKE.json` moves to `schema_version: 2` and gains a **required**, closed-vocabulary
`provider_price_basis`, separate from `adjustment_policy`:

| Value | Verdict | Reason |
|---|---|---|
| `unadjusted_as_traded` | proceeds | as-traded levels, never restated — the only accepted basis |
| `ibkr_trades_split_adjusted` | `UNVERIFIED` (exit 2) | back-adjusted for splits; a post-window split rescales the file undetectably |
| `ibkr_adjusted_last_total_return` | `UNVERIFIED` (exit 2) | split *and* dividend adjusted; the dividend adjustment is not recoverable from the bars |
| absent, non-string, or unknown | `UNVERIFIED` (exit 2) | no default — a default would reintroduce the silent assumption this ADR removes |

Each symbol additionally declares `no_split_in_window` (a strict JSON boolean), checked in **both**
directions against the delivered action file: a `true` over a real in-window split refuses and
names the symbol and ex-date, and a `false` over an empty split set refuses too. It is
**corroborating evidence, never an acceptance path** — no value of it admits a non-raw basis.

The certification report and the frozen release both **record** the basis. The release takes it
from the certification rather than from a parameter of its own, so a release cannot disagree with
the verdict it froze.

## Sequencing

Deliberately three slices; only the first is accepted here.

- **Slice 1 (this ADR).** Declare, check, refuse, record. Fail-closed: it adds a required key and
  a refusal, and loosens nothing. A delivery that certified before either still certifies with one
  new line, or is refused for a reason it can read.
- **Slice 2 (not approved).** Consumer-boundary enforcement — making downstream adjustment code
  *read* the recorded basis and refuse to double-adjust. Slice 1 records the field and explicitly
  does **not** enforce its downstream use; the field's docstring says so in those words. Needs its
  own design and its own review.
- **Option B (not approved).** Admitting `ibkr_trades_split_adjusted` under compensating controls.
  That is a contract change, not an implementation detail, and requires the owner's written
  admission. It stays unapproved.

## What this does not prove

- It does not verify the declaration. A vendor fact stated by the owner is checked for internal
  consistency against the shipped action files, not against the market. An owner who declares
  `unadjusted_as_traded` over a split-adjusted export still gets `CERTIFIED`.
- It does not prevent double-adjustment downstream. That is slice 2.
- It does not detect dividend restatement in a file declared raw, nor any other vendor
  normalisation outside the closed vocabulary. Those must refuse rather than be coerced into
  `unadjusted_as_traded`, and the vocabulary being closed is what forces that.

## Consequences

Every existing `INTAKE.json` is now `schema_version: 1` and refuses; the runbook's §2 sample,
§3 lead-in note, and §4 evidence paragraph are updated so the next delivery carries the field.
No delivery has been certified to date, so nothing in the repository is invalidated. The
certification schema moves to `chronos-dataset-certification-v4` and the release schema to
`chronos-dataset-release-v3`, both because a recorded field that is not bound into the digest is
decoration.

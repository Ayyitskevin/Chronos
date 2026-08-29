---
name: chronos-docs-map
description: "Navigate or repair Chronos documentation. Use for deciding which document owns a claim, checking whether prose is current, resolving contradictions, finding stale instructions, validating links, and updating docs without erasing history. Differentiator: derive the inventory, authority, and evidence from the checked-out revision instead of trusting a cached document table or contradiction ledger; route permission changes through chronos-change-control."
---

# Chronos documentation map

Treat every document as a set of claims, not as repository state. A title, status
line, dateline, generated page, skill, handoff, or earlier review can identify where
to look; it cannot prove current behavior at the checked-out revision.

## Route the task

Use this skill to answer:

- which document is the canonical home for a claim;
- whether a procedure, status, count, capability, or limitation is current;
- which side of a documentation conflict has stronger authority and evidence;
- whether an old statement should be corrected, superseded, retained as history,
  regenerated, or proposed for owner approval;
- which related sites must change when a canonical statement changes; and
- whether a changed Markdown link or agent-facing pointer still reaches its target.

Route roadmap selection to `chronos-priorities-and-roadmap`, permission and review
classification to `chronos-change-control`, executable topology to
`chronos-architecture-contract`, runtime procedures to `chronos-run-and-operate`,
statistical claims to `chronos-research-methodology`, measured tests to
`chronos-validation-and-qa`, and read-only drift probes to `chronos-diagnostics`.

## Read the authority ladder from its source

Read `AGENTS.md` before judging any conflict. It owns the exact document-precedence
ladder and read-first set; this skill does not maintain a second copy. Also read
`docs/AGENT_PROTOCOL.md` for branch, evidence, review, and merge rules.

The practical categories are:

- explicit owner direction within safety and approval boundaries;
- current executable facts and unresolved defects, proven through executable source and exercising tests;
- governance and accepted intent in `DECISIONS.md` and relevant records under
  `docs/adr/`;
- safety and limitations in `RISK_REGISTER.md`, `docs/safety.md`, and
  `docs/limitations.md`;
- roadmap order and completion criteria in `docs/VISION_COMPLETION_PLAN.md`; and
- historical plans, task boards, build briefs, handoffs, and milestone narratives.

Current executable facts describe what the checkout does. Accepted decisions describe
what it is intended to do. If those differ, stop and surface the discrepancy; never average
incompatible statements or silently rewrite one to preserve the other. Load
`chronos-change-control` before proposing the resolution.

## Role is not truth

Classify each relevant document by what it is for before relying on a claim:

| Role | Starting sources | How to verify a claim |
|---|---|---|
| Governance and accepted intent | `AGENTS.md`, `DECISIONS.md`, `docs/adr/` | Read status, supersessions, owner direction, and current executable consequences |
| Current capability | `src/chronos/`, `tests/`, `docs/ARCHITECTURE.md`, `docs/generated/CURRENT_STATE.md`, `docs/generated/capability-matrix.json` | Trace exports, immediate callers, construction, defaults, effects, and exercising tests |
| Safety and limitations | `RISK_REGISTER.md`, `docs/safety.md`, `docs/limitations.md` | Reproduce the control, its failure direction, and its residual evidence gap |
| Operations | Runbooks, CLI help, service entry points, configuration sources | Compare each step with the actual command, parser, settings, state owner, and failure feedback |
| Dated evidence | `docs/TEST_RESULTS.md`, evaluations, reports, capture artifacts | Bind the claim to its exact revision, inputs, command, output, skips, and warnings |
| History and context | `CHANGELOG.md`, `HANDOFF.md`, `TASKS.md`, archived plans and briefs | Cite as history; rederive every present-tense implication before acting |

`README.md` is an entry point and mission narrative, not a substitute for the sources
above. `docs/TEST_PLAN.md` describes validation structure; it is not a fresh run.
Generated is not authorized: generated current-state artifacts report committed
defaults and mapped code paths, not environment, mandate, promotion, gateway, account,
market-data, or operating truth.

## 1. Establish the revision and competing work

Start read-only and record the identity being analyzed:

```bash
git fetch origin --prune
git status --short --branch
git rev-parse HEAD
git ls-remote --symref origin HEAD
git log --oneline --branches --tags --not --remotes
```

Check open work before treating a missing correction as unowned. A handoff or review
from another revision is a hypothesis until its critical claim is reproduced here.

## 2. Derive the documentation inventory

Build the inventory from tracked files rather than a maintained table:

```bash
git ls-files -- '*.md'
rg -n --glob '*.md' '^#{1,6} '
rg -n '^Status:' docs/adr
```

For a document in scope, inspect why and when it changed:

```bash
git log --follow -- <document>
git blame -- <document>
git log --format='%h %cs %s' -- <document>
```

Read the whole relevant section, including its banner, status, correction notes,
links, footnotes, and referenced ADRs. A sentence can be historically honest and still
be unsafe as current procedure. A status label can describe adoption while saying
nothing about implementation or operating evidence.

For every changed Markdown link, resolve its path relative to the containing document
and confirm the target exists. Discover links and pointers with:

```bash
rg -n --glob '*.md' '\[[^]]+\]\([^)]+\)|`[^`]+\.(md|json|yaml|py)`'
```

Ignore external URLs only after confirming they are intentionally external. Do not
infer that a relative link is valid merely because a same-named file exists elsewhere.

## 3. Find volatile and over-strong claims

Use broad searches to find candidates, then verify each one against its owning source:

```bash
rg -n -i --glob '*.md' '\b(current|only|always|never|not implemented|does not exist)\b'
rg -n -i --glob '*.md' '\b[0-9][0-9,]* (tests|passed|skipped|warnings|files)\b'
rg -n -i --glob '*.md' 'default branch|schema|migration|port|account|capital|live|paper'
.venv/bin/python scripts/build_current_state.py --check
```

For capability claims, trace `src/chronos/`, entry points, configuration defaults,
construction roots, immediate callers, and the tests that exercise the claimed path.
For validation claims, run the named command; a count in `docs/TEST_RESULTS.md` is dated
evidence only. For a procedure, compare every command and safety precondition with
current `--help`, parser code, and state semantics.

The read-only diagnostic can seed this search:

```bash
.venv/bin/python .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
```

Its output is candidate findings from a dated rule set. Read the matching rule and
reverify both sides against the checked-out revision. A nonzero finding exit is not
proof that the old diagnosis is still correct, and a clean exit is not proof that the
documentation set has no other contradiction. Never bulk-edit from diagnostic output.

## 4. Build a contradiction proof packet

Write one packet for every material conflict before choosing an edit:

```yaml
claim: <one falsifiable present-tense statement>
document: <path, heading, and quoted claim>
authority_class: <owner | executable | accepted-intent | safety-limit | roadmap | history>
live_evidence: <revision, source symbols, callers, tests, runtime artifact, or command output>
conflict: <the exact incompatible statement and why both cannot be true>
resolution_class: <factual correction | historical supersession | generated regeneration | governance proposal>
owner_gate: <required, satisfied, or not applicable>
correction_sites: <canonical home and every pointer that would otherwise misroute a reader>
verification: <commands and observed outcomes a cold reader can rerun>
```

Evidence must match the breadth of the claim. A text search proves only the spelling
and tree searched. A unit fixture proves handling of that fixture, not provider or
gateway behavior. A generated matrix proves its declared source mapping, not configured
authority. An accepted ADR proves intended design, not that a caller exists. If a
packet lacks live evidence, retain the uncertainty and report it.

## 5. Choose the correction class

### Factual status correction

Use for a present-tense statement disproved by current source, tests, or a named
artifact when the correction changes no authority. Update the canonical home first,
then repair pointers that would still send a reader to the false claim. A documentation-
only correction normally advances no project gate; use `gate_advanced: none`.

### Historical supersession

Preserve the old statement when it records what was once believed or built. Add a
dated in-place correction, scope banner, or explicit successor pointer using the
current date. Keep the historical claim readable while making the current reading
unambiguous. Do not refresh every archived document into a second current-state home.

### Governance or authority proposal

This is owner-gated work. Use it when resolving the contradiction would change product scope, accepted architecture,
money or risk limits, a safety mechanism, broker authority, promotion, security posture,
or an owner gate. Load `chronos-change-control`, preserve the conflict, and prepare the
required ADR or owner-reviewed proposal. A prose edit cannot silently make accidental
code behavior authoritative, and code cannot silently overwrite accepted intent.

### Generated artifact regeneration

Never hand-edit `docs/generated/CURRENT_STATE.md` or
`docs/generated/capability-matrix.json`. Change an authoritative input only when that
input belongs to the task, run `scripts/build_current_state.py`, inspect the generated
diff, and prove freshness with `scripts/build_current_state.py --check`.

## 6. Keep one canonical home per fact

- Current executable behavior lives in source and exercising tests; maintained prose
  points there without copying an exhaustive implementation inventory.
- Accepted decisions live in `DECISIONS.md` and the relevant ADR.
- Roadmap and completion criteria live in `docs/VISION_COMPLETION_PLAN.md`.
- Risk status and residuals live in `RISK_REGISTER.md`; `MITIGATED` is not `CLOSED`.
- Limitations live in `docs/limitations.md`; do not turn absence of evidence into a
  positive capability claim.
- Dated validation evidence lives in `docs/TEST_RESULTS.md`; fresh verification comes
  from the command run on the candidate.
- Milestone narrative lives in `CHANGELOG.md`; archived plans and handoffs remain
  context rather than a competing task board.

When several documents repeat a fact, keep the full claim in its canonical home and
replace other present-tense copies with scoped pointers. Duplication is a drift surface.

## Known pitfalls

- **Current-looking history:** a detailed old handoff can feel more authoritative than
  a concise current source. Detail is not precedence.
- **Code-versus-intent collapse:** executable behavior and accepted architecture answer
  different questions. A mismatch is a finding, not permission to choose silently.
- **Generated is not authorized:** code-path coverage does not reveal external state or
  grant permission.
- **Counts decay immediately:** test, file, ADR, branch, migration, and inventory counts
  are observations. Derive them when needed instead of storing them here.
- **Diagnostic completeness:** a rule ledger finds known spellings. It cannot prove the
  absence of an unmodeled contradiction.
- **Correction laundering:** a nearby note does not resolve a pending owner decision.
- **Case and relative paths:** similarly named documents and links can resolve differently
  across filesystems. Use exact tracked paths.
- **Agent instructions are documents:** a Chronos skill can become stale like any other
  prose. Treat its tables and examples as claims and prefer procedures that derive state.

## Close the loop

After the last edit:

```bash
.venv/bin/python -m pytest -q tests/unit/test_docs_map_skill_contract.py
.venv/bin/python scripts/build_current_state.py --check
git diff --check
git diff -- .claude/skills/chronos-docs-map tests/unit/test_docs_map_skill_contract.py
make gates
```

Read the entire output, including skips and warnings. A procedural skill and its
semantic contract are a moderate change, so obtain non-author review at the exact
candidate revision. After merge, prove candidate ancestry or changed-path equality,
verify exact-default CI, and spot-check that the stale inventory did not return.

Report the task contract from `docs/VISION_COMPLETION_PLAN.md`, every proof packet,
the fresh commands and outcomes, the review verdict, and all remaining conflicts or
owner gates. Documentation is repaired only when a cold reader can reach the current
authority without depending on this session's memory.

---
name: chronos-priorities-and-roadmap
description: >
  Open this FIRST at the start of any Chronos session. Load it whenever you ask
  "what should I work on", "what's next", "what are the priorities", "what's the
  current status", "where is this project", "what's the roadmap", "what's the game
  plan", "is X done yet", "what matters right now", or you are about to pick a task,
  scope a milestone, or judge whether a proposed feature advances the project. It is
  a live task-selection procedure grounded in docs/VISION_COMPLETION_PLAN.md,
  docs/TEST_RESULTS.md, docs/AGENT_PROTOCOL.md, and repository state. It deliberately
  does not cache a current queue or validation counters. NOT for how-to-run questions
  (chronos-run-and-operate), config (chronos-config-and-flags), doc trust
  (chronos-docs-map), or change classification (chronos-change-control).
---

# Chronos — priorities and roadmap

**Audience:** a session with zero prior context deciding what to do next.

**Authority:** this skill supplies a procedure, never project state. Apply the
document precedence in `AGENTS.md`; treat every summary, handoff, issue, and dated
test result as a claim until the live repository confirms it. Stop and surface a
contradiction instead of averaging two stories.

## 1. Preserve the two independent outcomes

`docs/VISION_COMPLETION_PLAN.md` defines two kinds of success that must never be
blurred:

| Outcome | Meaning |
|---|---|
| **Platform / safety 10/10** | One coherent, installable, observable, recoverable system whose declared capabilities match executable behavior. It may correctly remain `NO_TRADE`. |
| **Proven autonomous trader 10/10** | One exact strategy-policy configuration independently clears the evidence ladder for its asset family inside owner-frozen limits. |

An engineering session can materially advance the first outcome. The second also
requires prospective evidence, owner decisions, and calendar time. Code completion
is not gateway, operating, or economic proof. A correct `NO_TRADE` is success when
evidence is insufficient, and a failed gate is never weakened to manufacture
progress.

## 2. Acquire the live work queue

Run this sequence before selecting work. It is intentionally read-only until the
task and branch are chosen.

```bash
git fetch --all --prune
git ls-remote --symref origin HEAD
git status --short --branch
git log --oneline --branches --tags --not --remotes
gh pr list --state open
gh issue list --state open
```

Then read, in this order:

1. `AGENTS.md` for authority, safety, evidence, and repository constraints.
2. `docs/AGENT_PROTOCOL.md` for the current session/branch/PR protocol.
3. `docs/VISION_COMPLETION_PLAN.md` for the canonical current state, unresolved
   findings, dependency chain, promotion ladder, and owner gates.
4. `docs/TEST_RESULTS.md` for the latest **dated** evidence snapshot. It is a
   comparison point, not proof for the current checkout; rerun `make gates` before
   making a new completion claim.
5. `DECISIONS.md`, `RISK_REGISTER.md`, and the relevant ADRs for the candidate area.
6. The newest applicable handoff when the fleet handoff bus is mounted. Verify at
   least one critical claim before building on it.

Never reconstruct a current queue from this skill, `TASKS.md`, a historical game
plan, an old branch name, or remembered test counts. Before duplicating an apparent
open item, search the fetched refs and open PRs for completed or in-flight work.

## 3. Select one coherent item

Use the dependency chain in `docs/VISION_COMPLETION_PLAN.md` as the ranking rule:

```text
scope constitution
  -> authority correctness
  -> broker truth and recovery
  -> certified data
  -> strategy evidence
  -> promotion ladder
```

Choose the highest-leverage item that:

- is still open in live code/docs rather than only in an old narrative;
- advances that chain or removes a Critical/High integrity defect;
- has no active overlapping branch, PR, lease, or unpushed completed commit;
- can be expressed as one logical change with measurable pass/fail criteria;
- stays inside the current owner permissions and task contract.

When autonomous work is requested and the top item is owner-gated, record the gate
and select the next owner-independent item. Never infer that autonomy permits a
money, authority, credential, live-broker, schema, or irreversible decision. The
owner-decision queue lives only in `docs/VISION_COMPLETION_PLAN.md`; do not mirror it
here.

Good owner-independent work commonly includes stronger validation, removal of stale
or contradictory claims, deterministic evidence tooling, recovery documentation,
and test coverage around already-decided behavior. Classify even these through
`chronos-change-control` before editing when they touch a safety mechanism.

## 4. Define the task contract before editing

Record the fields required by `AGENTS.md` and `docs/AGENT_PROTOCOL.md`, including:

- plan phase and primary KPI;
- gate advanced, or `none` for integrity/tooling work;
- exact intended files and evidence artifact;
- owner gate, if any;
- explicit open risks and non-goals.

Derive the default branch live, branch from its fetched tip, and keep one logical
item per PR. Establish a clean baseline before editing. The repository-wide gate is:

```bash
make gates
```

Do not replace it with a remembered list of subcommands. Inspect `Makefile` and the
hosted workflow if the gate shape itself matters. The current validation snapshot is
parsed from the first explicitly current summary in `docs/TEST_RESULTS.md`; historical
sections are evidence history only.

## 5. Exit criteria

A selected item is ready to hand off or ship only when:

- focused tests demonstrate the intended behavior and the relevant regression;
- `make gates` passes after the last change;
- a non-author seat reviews a moderate change in its own clone/worktree;
- the repository's PR/ruleset requirements pass at the exact candidate SHA;
- after merge, hosted CI passes at the exact resulting default-branch SHA;
- the durable handoff states produced paths, rerunnable verification, assumptions,
  owner gates, and remaining work without inflating evidence.

If any requirement is skipped, say so and choose the honest outcome: full coherence,
pragmatic partial, hold and clarify, or explicit owner override.

## 6. Anti-goals

Decline or re-scope work that looks like:

- feature breadth outside the canonical dependency chain;
- asset-family vocabulary without a funded and evidenced lane;
- transferring promotion from one family to another;
- changing thresholds after observing the evidence they judge;
- treating fixture coverage as real-gateway conformance;
- treating paper operation as proof of alpha;
- using UI polish to mask an authority, provenance, reconciliation, or data gap;
- weakening fail-closed or deny-by-default behavior to make a milestone appear done.

The long-horizon schedule and per-family promotion thresholds live in
`docs/VISION_COMPLETION_PLAN.md`. Read them there so calendar estimates and evidence
requirements cannot silently fork inside a skill.

## 7. Route to the deeper skill

| Need | Use |
|---|---|
| Run/launch, halt/kill/arm/revoke, backup/restore | `chronos-run-and-operate` |
| Config and safety-class meanings | `chronos-config-and-flags` |
| Document authority and contradiction handling | `chronos-docs-map` |
| Owner gates, ADR discipline, task classification | `chronos-change-control` |
| Architecture invariants and weak points | `chronos-architecture-contract` |
| Authority, mandate, gateway, model discretion | `chronos-autonomy-and-mandates` |
| Wheel/options state and assignment | `chronos-wheel-and-options` |
| IBKR boundary and contract semantics | `chronos-ibkr-boundary` |
| Research statistics, registry, holdouts | `chronos-research-methodology` |
| Evidence and test design | `chronos-validation-and-qa` |
| Build, lockfile, environment | `chronos-build-and-env` |
| Diagnosis and state inventory | `chronos-diagnostics` |
| Real-gateway evidence collection | `chronos-real-gateway-campaign` |

## 8. Maintenance contract

This skill owns the **selection algorithm**, not a snapshot. Do not add test counts,
branch names, open-item tables, current balances, current strategy results, schema
versions, lockfile counts, or statements that a particular finding is open/closed.
Those facts belong in their canonical repository documents or live commands.

Update this skill only when the authority order, task-selection procedure,
dependency chain, repository protocol, or routing map changes. All commands here are
read-only until the normal branch workflow begins; nothing here authorizes a broker
connection, credential use, authority expansion, gate reduction, or owner decision.

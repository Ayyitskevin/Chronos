# ADR-0056 — An unsafe grant reports as itself, on both arms

- Status: Accepted (owner review required before merge)
- Date: 2026-09-05
- Deciders: opus seat (author), owner (merge gate)
- Related: D-72, R-74, ADR-0053 (the SECRET/GRANT modes), ADR-0051 (the sibling
  exception this mirrors), ADR-0017 (the mandate as the grant), ADR-0023 (the
  proposer registry)

## Context

ADR-0053 made both owner-authored grants — the autonomy mandate and the proposer
registry — read through the descriptor-bound `AuthorityMode.GRANT` contract, and wired the
typed `StartupFaultCode.AUTHORITY_FILE_UNSAFE` for the **registry** only. The mandate half
was filed as a region decision because its startup call site sits inside
`build_autonomy_runtime`, which another lane was holding at the time.

That left two defects, one on each arm.

**The mandate reported as absent.** `load_persistent_mandate` caught `UnsafeAuthorityFile`,
logged CRITICAL and returned `None` — which is also what "no file configured" and "does not
validate" return. So a symlinked, group-writable, foreign-owned or non-UTF-8 mandate booted
the backend inert with **no startup fault at all**, and the terminal rendered
`NO MANDATE LOADED`: precisely the screen an owner sees when they have never authored a
grant. The one thing the system did not say was the one thing that mattered — the grant
exists on disk and somebody else can write it.

**The registry reported three ways, one of them false.** `build_autonomy_runtime` reaches
the registry too, via `build_identity_resolver`. Its `UnsafeAuthorityFile` escaped assembly
and landed in the lifespan's generic handler, so `/health` carried `authority_file_unsafe`
(correct), `proposer_registry_invalid` (correct) **and** `autonomy_wiring_failed` — which
says assembly crashed when assembly had correctly refused, and sends an operator looking for
a bug instead of at their file's permissions. Measured, not inferred: the existing
lifespan test passed because it asserts membership, which is right for its purpose and is
also why the extra fault went unnoticed.

## Decision

**Each loader raises its own exception**, and the lifespan maps both to the fault that is
already true of the file.

- `chronos.api.autonomy_wiring.UnsafeMandateFile`
- `chronos.supervisor.proposers.UnsafeProposerRegistry`

Two types rather than one, because `build_autonomy_runtime` reaches both grants: with a
single shared exception the one handler that has to explain itself to an operator could not
say *which* file was unsafe, and would name the wrong one half the time.

A sibling exception rather than a typed result, mirroring ADR-0051's
`UnauthenticatedSubmittingMandate` — the same problem solved one branch over in the same
function, with the same reasoning: *the owner must be able to tell "the posture is wrong"
from "assembly crashed."* A result object would have changed `build_autonomy_runtime`'s
signature and put two dispatch mechanisms side by side for one class of outcome.

The lifespan gains one `except (UnsafeMandateFile, UnsafeProposerRegistry)` arm **above** the
generic handler, noting `AUTHORITY_FILE_UNSAFE`. Ordering is load-bearing and has its own
mutation row: below the generic arm, the defect silently returns.

The three states now differ:

| state | loader | fault | alert |
|---|---|---|---|
| absent | `None` | *none* — a fresh install must boot clean | none |
| invalid | `None` | *none* | `autonomy.mandate_invalid` |
| **unsafe** | **raises** | `AUTHORITY_FILE_UNSAFE` | **`autonomy.mandate_unsafe`** |

The new alert kind is not cosmetic: both are CRITICAL and both leave autonomy inert, so
severity and outcome cannot distinguish them. An alert consumer filtering on `kind` is the
only thing that can, and "you typed the mandate wrong" and "another account can rewrite your
mandate" deserve different pages.

**The terminal says which.** `_mandate_in_force` feeds three routes — the system panel, the
mandate panel, and `revoke_mandate`, the one an owner reaches for *during an incident*. It
catches the exception (a read route that starts failing is the worse outcome) and returns a
new `mandate_unavailable="unsafe"` alongside `mandate_known=False`, which the client renders
as a `MANDATE FILE UNSAFE` block instead of `NO MANDATE LOADED`.

That last part is this ADR taking the module's own rule seriously. `routes/terminal.py`
already documents why it prefers the runtime's mandate over the file: *"the failure that
ordering prevents is a panel that looks safer than the process it describes."* Mapping an
unsafe grant onto the absent screen is exactly that failure, so the panel gets a state of its
own rather than a follow-up.

## Consequences

An unsafe grant on either arm now produces: the backend still booting (nothing that can close
a position dies because a grant was malformed), autonomy inert, `AUTHORITY_FILE_UNSAFE` on
`/health`, a CRITICAL line naming the file and the failing property, an owner alert under its
own kind, and a terminal panel that says the grant is untrusted rather than missing. Nothing
is repaired: chmodding an owner-authored grant on the owner's behalf is the process editing
the grant.

`autonomy_wiring_failed` now means what it says — assembly crashed — because the one case
that was reaching it by refusing correctly no longer does.

**Contract change, deliberate and not backward compatible.** `load_persistent_mandate` and
`load_proposer_registry` raise where they previously returned `None`. Absent and invalid are
unchanged; only the unsafe path moved. Every caller is updated in this change, and the
distinguishability of the two exceptions has its own test, because an exception is a contract
change a type checker will not flag at a call site that only ever handled `None`.

## Rejected alternatives

**Let `UnsafeAuthorityFile` propagate unwrapped.** Fewer types, and the handler loses the one
thing it needs to say. Rejected — see Decision.

**A typed result (`AutonomyAssembly(runtime, fault)`).** Forces every caller to handle the
case, which an exception does not. Rejected because it changes the signature of the most
contested function in the file and duplicates ADR-0051's dispatch; the risk it mitigates is
answered instead by naming every consumer and testing the distinction.

**Give "invalid" a typed fault too.** Symmetrical-looking and wrong for now: a mandate with a
typo is an owner authoring error, already alerted, and promoting it to a health fault is a
change to the health contract that should not ride this one.

**Render unsafe as the absent panel, with `/health` carrying the truth.** This was the
author's first recommendation and it was wrong. The panel is where an owner looks during an
incident, and the absent screen tells them to write a mandate when the truth is that theirs
is on disk and untrusted.

**Fix the mandate arm only, and leave the registry's double-report to a follow-up.** R10
argues for the narrower change. Rejected on R4: the invariant this ADR establishes is *an
unsafe grant reports as itself*, and shipping it on one arm while the other still reports
"assembly crashed" would leave `/health` making a false statement for another cycle. The two
arms are symmetric and one file apart.

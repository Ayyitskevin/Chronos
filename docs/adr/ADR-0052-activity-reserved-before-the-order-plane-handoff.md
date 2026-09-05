# ADR-0052 — Mandate activity is spent before the order-plane handoff, not after it

Status: **accepted design direction — Kevin authorized Sprint 1 on 2026-09-03; implementation
remains owner-gated at merge.** Index entry: DECISIONS.md D-67. Risk entry: RISK_REGISTER.md
R-70.

## Context

`run_cycle` handed a compiled intent to the order plane and only then recorded the attempt
against the mandate's `ActivityLimits`, inside the tick's transaction. `AutonomyRuntime._drain`
commits that transaction after `run_cycle` returns. Everything between the broker answering and
that commit was therefore a window in which the venue could hold an order the supervisor was
about to forget.

The 2026-09-03 adversarial review (P1-02) measured it: a cycle returned a submitted handoff,
the supervisor transaction was rolled back to simulate process loss, and a new session read
`orders_submitted` back at zero. Order-plane idempotency protects a repeat of the *same*
intent; it does not stop a *different* decision after restart from spending an allowance the
mandate had already spent.

Two obvious repairs were measured and rejected before this one, and the measurements are the
reason this ADR exists rather than a smaller patch:

- **Reserving from a second, independently committed session** deadlocks. By the handoff the
  cycle's session already holds an open SQLite write transaction (`durable.record_outcome`
  writes during admission), and the production database is file-backed SQLite with
  `journal_mode=WAL` and `synchronous=FULL`, which permits exactly one writer. A second write
  transaction opened inside the cycle waits out `busy_timeout` and fails:
  `OperationalError: (sqlite3.OperationalError) database is locked`, observed through the real
  `run_tick`. The order plane escapes this only because it persists to its own store
  (`chronos.execution.sqlite_ledger`), not the main database.
- **A distinct `orders_reserved` column** does not escape it either. The contention is in the
  transaction topology, not the counter's shape; the reservation still needs an independent
  commit at the same instant.

**A finding that outlives this ADR:** on an in-memory database the deadlock does not appear,
and the rejected designs *look* correct. `Database` gives a `:memory:` URL a `StaticPool` — one
connection shared by every session — so a **second** session joins the first one's transaction
and its commit also commits the first's pending work. A probe on `:memory:` recorded
`orders=1 turnover=4000 cancellations=1`, where `cancellations=1` was the cycle's own
*uncommitted* write surviving a rollback. Every supervisor test runs on `:memory:`, so a fully
green suite would have certified a reserve-from-a-second-session design that deadlocks in
production.

Stated precisely, because the tempting stronger claim is false: this does **not** mean the
crash test below would have passed spuriously on `:memory:`. It would not. Measured four ways —
file and `:memory:` crossed with the commit present and absent — the verdict is identical on
both pools, and removing the commit kills the test on both. It has to: the decision adopted
here commits the cycle's *own* session, so no second connection exists for `StaticPool` to
collapse. The hazard is real, and it is a hazard for **multi-connection designs**, which is why
it disqualified two of them; it is not a hazard this test needed rescuing from. The file-backed
fixture is justified on its own terms — a durability test should run the durability
configuration — not by a defect it would have caught.

## Decision

0. **The split is not a new idea in this file.** `_persist_selection_receipt`
   (`loop.py:1294`) has committed the cycle's session mid-cycle since the option-selection
   receipt barrier landed, for the same reason: a receipt the handoff is about to rely on must
   not be rolled back by the handoff's own failure. Every session in
   `tests/safety/test_option_selection_cycle.py` is a plain one because of it. ADR-0052
   generalises that existing barrier to the activity counters rather than inventing a
   mechanism.
1. `run_cycle` records the attempt — `orders_submitted=1` and the same notional the
   post-handoff counter used — **before** calling the handoff, in the cycle's own session.
2. It then commits, through an injected `commit_before_handoff` callable. That commit is the
   mechanism: it ends the cycle's transaction so the reservation cannot be rolled back with it,
   and it releases the SQLite write lock rather than contending with it.
3. The callable is injected rather than a bare `session.commit()` because the caller owns its
   transaction scope. `_drain` holds a plain session and supplies `session.commit`; a caller
   whose session lives inside a `sessionmaker.begin()` block cannot commit mid-scope at all
   (SQLAlchemy raises `InvalidRequestError: Can't operate on closed transaction inside context
   manager`).
4. **A configured handoff with no seam refuses.** `submit` without `commit_before_handoff`
   journals `NO_DURABLE_RESERVATION` at the HANDOFF stage and never calls the handoff. Absence
   of the mechanism is not permission to fall back to the pre-ADR-0052 accounting: a caller
   that wired submission but not durability has not proven it can spend the budget across a
   crash. Omitting `submit` is still SHADOW and is untouched.
5. The reservation is given back only by `durable.release_activity_reservation`, and only when
   the disposition proves nothing left the process — `REFUSED_NOT_SENT`. It is driven by the
   same `counts_activity_attempt` flag the counting used, so reserve and release cannot
   disagree about what an attempt is.
6. `SENT_AMBIGUOUS`, `REJECTED_AFTER_SEND`, an unclassifiable result, and **an exception out of
   the handoff** all keep the reservation. This last one is a deliberate behaviour change:
   `ORDER_PLANE_REFUSED` previously counted nothing, which was only safe while a raise proved a
   quiet wire — and `loop.py` has always disclosed that it does not.
7. `release_activity_reservation` is the only decrement in `chronos.supervisor.durable`. It
   refuses negative arguments and clamps at zero, because a negative counter would widen
   headroom under a ceiling.
8. No schema change, no migration, no new authority, no threshold moved.

## Consequences and bounds

The counter now **outlives its journal entry** in exactly one window: a crash between the
handoff and the cycle's final commit keeps the reservation and loses the outcome record. That
inverts the rule `durable.py` states for every other write, and the inversion is the point —
an atomic cycle is the wrong guarantee where the venue holds state this process does not.
Over-counting narrows the mandate's own authority; under-counting hands back budget that may
already be spent.

It also strengthens R-31 replay protection as a side effect: the admission attempt record is
now committed before the wire instead of dying with the same crash.

Bounds. This does not reconcile a surviving reservation against what the venue actually did —
an owner reading the counters cannot yet distinguish "reserved, outcome unknown" from
"confirmed", because both are `orders_submitted`. Adding that distinction (an
`orders_reserved` column, or reconstruction from the order plane's ledger keyed by immutable
decision/intent) is real follow-up work and is deliberately not here. Nothing about a broker,
PAPER, LIVE, promotion, or operating campaign is proved by this change.

## Verification

- `tests/safety/test_autonomy_runtime.py::test_a_crash_between_the_handoff_and_the_commit_leaves_the_attempt_spent`
  runs the real `run_tick` against a **file-backed** database in `tmp_path`, injects process
  loss at `proposals.mark_processed` — after the handoff answers, before `session.commit()` —
  and reads the counter back from a fresh session. It fails against the pre-ADR-0052 tree with
  `orders_submitted == 0`.
- `tests/safety/test_autonomy_runtime.py::test_the_drain_supplies_the_durable_seam_to_every_cycle`
  pins the production call site: the drain passes its own session's bound `commit`, so a wiring
  change that dropped it reads as a failing test rather than a silently inert backend.
- `tests/safety/test_autonomy_cycle.py::test_a_configured_handoff_without_the_durable_seam_refuses_and_sends_nothing`
  proves the handoff is never called without the seam, and that the refusal spends nothing.
- `tests/safety/test_typed_handoff_outcomes_exercised.py::test_an_exception_out_of_the_handoff_refuses_and_keeps_the_reservation`
  pins decision 6.
- The existing counting suite is unchanged in its assertions: the net counter in every
  non-crash path is identical to pre-ADR-0052 behaviour.

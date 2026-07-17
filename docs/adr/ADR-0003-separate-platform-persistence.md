# ADR-0003 — The platform persists to its own files, separate from the wheel ledger

Status: Accepted (2026-07-17). Index entry: DECISIONS.md D-03.

## Context

The wheel dashboard's SQLite schema (v2, `src/chronos/persistence/`) binds a database file to one
broker mode, environment, and pseudonymous account fingerprint, with strict adoption rules: an
existing file with a different scope is refused, not migrated. Mixing platform order-intent state
into that schema would either weaken those invariants or force the platform through account-binding
rules designed for a different lifecycle.

## Decision

The platform persists to its own files under `data/`:

- `data/platform_ledger.db` — SQLite order ledger (`src/chronos/execution/sqlite_ledger.py`).
  Tables: `schema_info`, `intents`, `transitions`, `fills`. Append-oriented: intents insert once
  (duplicate intent ids violate the primary key), transitions and fills are insert-only history;
  nothing updates or deletes rows. WAL journaling with `synchronous=FULL`, so a returned write has
  reached disk. Schema version 1; an unknown version refuses to open rather than migrating.
- `data/platform_halt.json` — persistent halt state (`src/chronos/control/halt.py`). Atomic
  temp-file-plus-rename writes; a missing, unreadable, or corrupt file reads as HALTED.
- `data/platform_audit.jsonl` — hash-chained audit records (`src/chronos/auditlog/log.py`).
  Append-only JSONL, fsync per record.

The wheel dashboard keeps its own `data/chronos.db` (default `DATABASE_URL=sqlite:///data/chronos.db`
in `src/chronos/config/settings.py`). No shared mutable state exists between the two systems.

## Consequences

- The wheel schema's account-fingerprint invariants are untouched.
- Backup and restore are file-level and simple (docs/BACKUP_AND_RECOVERY.md); the halt file's
  fail-closed read means a restored deployment starts halted by design.
- Two databases must both be backed up; forgetting one loses either wheel evidence or platform
  order history, never both.
- The ledger schema is versioned but has no migration tooling yet; a schema change requires an
  explicit, reviewed migration path (not implemented — future work).

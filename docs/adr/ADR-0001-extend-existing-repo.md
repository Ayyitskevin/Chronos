# ADR-0001 — Extend the existing repository rather than rewrite it

Status: Accepted (2026-07-17). Index entry: DECISIONS.md D-01.

## Context

The repository already contained the wheel-strategy decision-support dashboard (milestones 1–10):
951 passing tests plus 1 credential-gated skip, a hard-disabled order boundary
(`src/chronos/broker/ibkr.py` raises `BrokerSafetyError` from every order method), fail-closed
reconciliation, Decimal money handling, and an account-fingerprint-bound SQLite schema. The new
deterministic strategy platform (research → backtest → replay → shadow → paper) needed a home.

Options considered:

1. New repository / full rewrite.
2. Extend this repository with new `chronos.*` subpackages, leaving the wheel dashboard intact.

## Decision

Extend the existing repository. The platform lives in new subpackages (`chronos.marketdata`,
`chronos.indicators`, `chronos.specs`, `chronos.strategies`, `chronos.portfolio`, `chronos.risk`,
`chronos.execution`, `chronos.control`, `chronos.backtest`, `chronos.research`, `chronos.auditlog`,
`chronos.notifications`, `chronos.cli`). Wheel-dashboard packages (`chronos.broker`,
`chronos.services`, `chronos.strategy`, `chronos.ui`, `chronos.persistence`) are unchanged.

## Consequences

- Tested safety invariants of the wheel dashboard are preserved, not re-implemented; its docs
  (`docs/architecture.md`, `docs/safety.md`, `docs/ibkr_setup.md`) remain valid for that subsystem.
- The two systems share vocabulary (`chronos.domain`, `chronos.config`) and one CI pipeline
  (`.github/workflows/ci.yml`), so a regression in either blocks merging.
- The platform must keep its state separate from the wheel ledger (see ADR-0003) because the wheel
  schema binds a database file to one account fingerprint.
- One repository means one dependency set (`pyproject.toml`); the platform reuses the already-pinned
  `ib_async` dependency (see ADR-0002).
- Slight naming friction: `docs/architecture.md` (wheel) and `docs/ARCHITECTURE.md` (platform)
  coexist; both are intentional.

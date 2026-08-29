# Security — Threat Model and Controls As Implemented

Scope: the whole repository (wheel dashboard + deterministic platform), operated by one person on
one local Linux machine, against one small IBKR account. This document describes only controls
that exist in code today, and says so plainly where something is aspirational.

## Threat model

Assets, in order of importance:

1. The brokerage account (money). Worst case: an unintended live order.
2. Broker credentials and account identity.
3. Integrity of the order ledger and audit trail (the evidence of what the system did).
4. Integrity of research inputs (data, Pine corpus) that future trading decisions rest on.

Adversaries considered: the operator's own mistakes (the dominant risk), a compromised or buggy
dependency, local malware with user-level file access, and accidental exposure of local services.
Explicitly out of scope: nation-state attackers, physical access, and multi-user threat models —
this is a single-operator local system (ASSUMPTIONS.md A-42).

## Controls as implemented

### No credentials anywhere in the repo

- Chronos never asks for, stores, or transmits IBKR usernames/passwords. Authentication happens in
  TWS/IB Gateway, owned by the operator, including 2FA. There is no headless auth automation and
  none should be added (it would defeat IBKR's session security and 2FA).
- `.env` is gitignored. `.env.example` contains placeholders and non-secret defaults only —
  verified: its only account field is `IB_ACCOUNT_ID=` (empty), and no keys, tokens, or passwords
  appear in it.
- The audit log forbids secrets by contract: callers pass sanitized payloads only
  (`src/chronos/auditlog/log.py` module docstring).

### No live-order capability

- Platform: `CANARY_LIVE`/`LIVE` resolve to `DENIED_LIVE_DISABLED` unconditionally
  (`src/chronos/control/modes.py`); the paper adapter is constructible only from a
  `PAPER_SUBMISSION` lock and only on paper ports {7497, 4002} with a `D[UF]\d{4,}` account.
- Wheel dashboard: every IBKR order method on the `ib_async` adapter raises `BrokerSafetyError`
  (`src/chronos/broker/ibkr.py`); ~~`ALLOW_LIVE_TRADING=true` makes settings validation raise~~
  *(Corrected 2026-08-02: true only before Milestone 7. Since ADR-0009 the flag is **honored**
  under the full nine-conjunct live configuration — official adapter, LIVE environment, a
  `U\d{4,}` account on a non-empty allowlist, the transmit switch, and the arming/typed-confirmation
  flags. Startup refuses and names every unmet conjunct otherwise; at run time an order still walks
  the ten-gate live stack. See `src/chronos/config/settings.py:165-199` and
  `docs/live_trading_runbook.md`. `docs/DEPLOYMENT.md` already carried this correction.)*
- There is no `--force` flag in the operator CLI (`src/chronos/cli/main.py`), and no command that
  changes trading capability.

### Localhost-only surfaces

- The IBKR API socket is expected to be bound to loopback (operator-configured in TWS/Gateway;
  see docs/IBKR_RUNBOOK.md).
- The wheel dashboard is run locally via Streamlit; the platform control surface is a local CLI.
  ~~Neither system implements remote access, authentication, or multi-user features~~
  *(Corrected 2026-08-02 — the reality is stronger than this text, but the text is wrong.)* Since
  Milestone 5 the order-management surface is a **FastAPI backend** bound to loopback
  (`backend_host` defaults to `127.0.0.1`, port `8765`, and a settings validator refuses any
  non-loopback host — `src/chronos/config/settings.py:106-107, 255`). It **does** authenticate:
  every mutating route requires the `X-Chronos-Token` header (`src/chronos/api/auth.py:21`), and
  since M8b the operator terminal may instead present an httpOnly session cookie scoped
  `path=/terminal`, so the browser never attaches it to `/orders/*`
  (`src/chronos/api/terminal_session.py`). Sessions are in-memory only, so a restart signs every
  terminal out, and a session grants no writer authority. Still no multi-user model and no remote
  access is intended: **do not put any of these behind a reverse proxy or expose ports.**

### Tamper-evident audit log

- `data/platform_audit.jsonl` is a hash chain: each record embeds the SHA-256 of the previous
  record; edits, deletions, and reordering break the chain (`src/chronos/auditlog/log.py`).
- Verify with `python -m chronos.cli verify-audit-log` (exit code 1 on failure). A verification
  failure is an incident (docs/INCIDENT_RESPONSE.md), not a nuisance.
- Appends are flushed and fsynced; a failed audit write is designed to halt trading, not be
  dropped.
- Limitation, honestly: a hash chain proves internal consistency, not authenticity — an attacker
  with file write access could rewrite the whole chain. There is no external anchor (no remote
  copy, no signing). Off-machine backups (docs/BACKUP_AND_RECOVERY.md) are the compensating
  control.
- A truncated final record (e.g. a process killed mid-append) is detected on the next
  construction: `AuditLog(...)` raises `AuditLogCorruptionError` and the CLI shadow-scan path
  halts with `AUDIT_LOG_FAILURE` rather than crashing with a raw traceback.

### Owner-only permissions on platform state files

- `data/platform_ledger.db` (+ its `-wal`/`-shm` sidecars), `data/platform_halt.json`, and
  `data/platform_audit.jsonl` are created with mode `0600` via
  `chronos.utils.secure_files.secure_owner_only`, which refuses to follow a symlink and refuses a
  file not owned by the current process (so a local attacker cannot redirect the chmod). This
  matches the hardening the wheel dashboard already applies to its own SQLite/log files. These
  files hold trade intents, symbols, and prices — not credentials — but should not be
  world-readable on a shared host. The halt file is additionally fsync-durable (temp file +
  directory fsync + atomic rename).

### Fail-closed halt and deny-by-default risk policy

- Halt state persists in `data/platform_halt.json`; missing/corrupt file reads as HALTED; rearm
  requires an operator note (`src/chronos/control/halt.py`).
- The risk policy schema defaults every allowance to zero/false and rejects unknown keys
  (`extra="forbid"`, `src/chronos/risk/policy.py`) — a typoed key is an error, not a silently
  ignored widening. An all-default policy approves nothing (tested).

### CI gates

`.github/workflows/ci.yml` runs on every push and PR: `ruff check`, `ruff format --check`,
`mypy src/chronos` (strict mode per `pyproject.toml`), `pytest -q`, the release security gate,
and the release-artifact gate, on Python 3.12 with safety env pins (`BROKER_MODE=demo`,
`ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`). `make gates` is the local equivalent.

### Dependency pinning — current status

- `pyproject.toml` uses bounded ranges (e.g. `ib_async>=2.0,<3`, `pydantic>=2.9,<3`), which
  express intent but do NOT by themselves give reproducible installs.
- **Hash-verified lockfiles are committed:** `requirements-dev.lock` pins every runtime and
  development dependency (the full transitive closure) to an exact version and SHA-256 hash. It is
  generated from `pyproject.toml` with
  `uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock`.
  The release gate's `requirements-runtime.lock` is generated from the same project without the
  `dev` extra, preserving the shared versions:
  `uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements-runtime.lock`.
  `requirements-build.lock` separately pins the exact `setuptools` backend declared in
  `[build-system]`, and `requirements-sbom.lock` pins the isolated generator declared in
  `requirements-sbom.in`; regenerate them with their exact header commands.
- **CI and the release gate install from locks, not ranges:** CI installs the build and full
  runtime/dev locks for editable quality checks. The release gate installs the build lock in a
  builder venv, the runtime-only lock plus the wheel in a separate runtime venv, and the SBOM tool
  lock back in the builder only after the wheel exists. It disables build isolation and asks pip
  to check the already-installed backend version against `pyproject.toml`. The preceding
  `--require-hashes` install provides integrity; `--check-build-dependencies` verifies requirement
  compatibility, not hashes. A tampered or substituted locked dependency therefore fails its hash
  check rather than installing silently. Modern setuptools implements
  `bdist_wheel` itself, so the `wheel` CLI package is not a Chronos build dependency.
- **Release security tools are exact dev dependencies:** `pip-audit==2.10.1`,
  `bandit==1.9.4`, and `detect-secrets==1.5.0` are installed from the hash-locked dev set.
  `scripts/verify_release_security.py` refuses any installed-version drift before scanning.
- **The release SBOM is a checked artifact, not a best-effort report.** The gate uses the official
  `cyclonedx-py environment` CLI against only the runtime venv, requests reproducible CycloneDX 1.6
  JSON with schema validation, then independently requires the Chronos application identity,
  every runtime-lock package and exact version, no component outside that lock except the venv's
  pip/setuptools bootstrap, a closed dependency graph, and an exact root edge matching the wheel's
  non-extra `Requires-Dist` metadata. The verified wheel and SBOM are published together under
  `dist/`; exact-main CI requests 90-day retention under a commit-addressed artifact name.
- **Wheel reproducibility is measured, not inferred from locked inputs.** The gate derives
  `SOURCE_DATE_EPOCH` from the exact Git `HEAD` commit, overriding any ambient caller value, then
  copies the same source set into two isolated source/output trees and invokes the exact locked
  backend twice with pip's cache disabled. It requires one identical filename and byte-for-byte
  identical wheel, and checks every ZIP member against the expected UTC source timestamp after the
  backend's pre-1980 clamp and ZIP's two-second precision. A Git timestamp outside the supported
  ZIP range, either build failure, byte drift, or member timestamp drift blocks publication. This
  is same-revision evidence in one pinned build environment, not a cross-platform or independently
  rebuilt attestation.
- **The security gate observes three different failure domains and never repairs them.** It asks
  `pip-audit` to audit the exact runtime lock with hashes required and dependency resolution
  disabled; any current known advisory or audit-service/tool failure blocks. Bandit recursively
  checks `src/chronos`, `worker`, and `scripts`, blocking findings at medium severity and medium
  confidence or stronger. detect-secrets checks every Git-tracked file except the fingerprint-only
  `.secrets.baseline`; every baseline entry is an explicitly reviewed false positive. The wrapper
  scans a temporary copy and refuses if detect-secrets would rewrite it, so stale line fingerprints
  fail without mutating the checkout. The baseline stores hashes, detector names, paths, and lines,
  never raw candidate values.
  **Residual:** advisory and heuristic results are current evidence, not proof that dependencies are
  non-malicious, every historical secret is absent, or lower-confidence static issues do not exist.
  The gate does not scan Git history or sign either artifact. pip remains the frontend that
  creates/installs into these environments and remains outside the hash lock (CI upgrades it before
  installation; the release verifier uses the interpreter's bundled pip). The measured wheel result
  does not prove reproducibility across different operating systems, Python/pip versions, archive
  implementations, or compromised builders. Note also the runtime lock's
  `aeventkit` entry is legitimate,
  not a typosquat: it is the
  dependency `ib_async` itself declares (the ib-api-reloaded republication of `eventkit`; same
  maintainer org, provides the `eventkit` module).
- Maintenance (owner action): regenerate the relevant lock with its header command when bumping a
  bound or tool, and review the diff before committing. Regenerate both application locks when a
  runtime requirement changes and require their shared package versions to remain equal. Keep
  `requirements-build.in` requirement-aligned with `[build-system].requires` and
  `requirements-sbom.in` exact; focused gate tests enforce these boundaries.
  `requirements.txt` remains `-e .` for a quick editable dev install; the locks are the
  reproducible, verified path used by CI and recommended for deployment.
- **Regenerate INTO the existing lock, never into a fresh file** *(added 2026-08-08)*. The
  commands above write to existing locks and `uv` treats their current pins as preferred, so an
  unrelated addition stays an addition. Compiling to a new path instead —
  `-o new.lock` — gives the resolver no prior pins to respect and it re-solves everything from
  the declared ranges.

  Measured on 2026-08-08 while evaluating one dev-only test tool: compiling to a fresh file
  produced **17 version changes** across the runtime stack, including `fastapi` 0.139.2 → 0.141.1,
  `streamlit` 1.59.2 → 1.61.1, `pydantic-settings` 2.14.2 → 2.15.0 (the package that validates
  the ADR-0009 live-transmission conjunction), plus `alembic`, `uvicorn`, `mako`, `greenlet` and
  `ruff` — and it silently *dropped* four packages a `streamlit` minor had stopped requiring.
  Compiling into the existing lock produced **zero** version changes and zero removals.

  The hazard is that both commands succeed, both produce a valid hash-pinned lock, and only the
  diff distinguishes "added a dev tool" from "upgraded the framework the order plane runs on".
  A reviewer skimming a 1600-line generated file for an expected addition will not see the
  seventeen. If a broad upgrade is actually intended, do it as its own reviewed change with the
  suite re-run against it — never as a side effect of adding something else.

### Log and notification redaction posture

- Platform notifications carry (kind, summary, detail) built by callers; the only implemented
  sink logs to the local `chronos.notifications` logger (`src/chronos/notifications/notifier.py`).
  No network notification channel exists, so nothing leaves the machine. Callers are responsible
  for sanitized summaries; the audit log's no-secrets rule applies equally.
- The wheel dashboard has its own, stricter documented redaction: masked account ids, pseudonymous
  fingerprints, aggregate-only diagnostics, raw broker error text kept out of UI and logs — see
  docs/safety.md ("Secrets and logs"). Those guarantees are that subsystem's own.

## Secrets handling rules for contributors

1. Never commit `.env`, credentials, tokens, account ids, or real account exports. `.env.example`
   changes must contain placeholders only.
2. Never log or write to the audit log: credentials, raw account identifiers, or raw broker error
   text. Pass sanitized payloads.
3. Never add headless IBKR authentication, credential storage, or session automation.
4. Never widen a default: new risk-policy fields must default to zero/false; new settings must
   fail closed on invalid input.
5. Never add a `--force` flag or a code path that bypasses the mode lock, halt store, risk
   engine, or reconciliation gate.
6. If a secret is ever committed: rotate it at the source first, then rewrite history; treat the
   secret as burned regardless.

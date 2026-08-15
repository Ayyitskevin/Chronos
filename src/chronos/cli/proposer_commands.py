"""``chronos proposer mint|check|revoke`` — credentials and registry (ADR-0023, A3).

``mint`` and ``check`` remain stdout-only, following ``mandate
template``/``check``'s doctrine exactly: **a tool that mints text is not a tool
that grants**. The owner pastes the printed registration into the registry file
themselves; neither command writes a file, and neither can enable or extend a
proposer. ``tests/safety/test_proposer_credentials_exercised.py`` pins the
no-write property the same way ``test_mandate_check`` pins the mandate
commands'.

``mint`` generates the credential with ``secrets.token_hex(32)`` and prints it
exactly once, beside the registration entry that carries only its SHA-256. The
registry file never holds a credential, so reading it yields nothing
presentable — the same reasoning as the terminal's hashed session ids (R-41).

## ``revoke`` is the first mutating command on this CLI, and it is narrow (A3)

It writes **one row in the database and nothing else**. It does not touch the
registry file: the grant document stays owner-authored, and revocation is
durable state the running backend consults — which is exactly why it takes
effect without a restart, unlike editing ``enabled: false`` into the file.

Two properties keep it from being an authority surface:

- **It only ever removes.** There is no un-revoke and no flag that grants. The
  strongest thing this command can do is stop a proposer, which is the direction
  a mutating operator tool is allowed to move in.
- **It revokes a credential, not a name.** The row is keyed by the
  registration's ``secret_sha256``, so re-minting a fresh credential for the
  same ``proposer_id`` works at the next restart while the leaked one stays dead
  forever.

An unregistered credential cannot be revoked, and that is not a gap: a
credential the registry does not contain already fails ``verify()``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Minted credentials are 32 random bytes, hex-encoded — the same strength as
#: the backend's own API token.
_CREDENTIAL_BYTES = 32

# `chronos.supervisor.proposers` is imported INSIDE the commands, not here:
# importing the supervisor package pulls the broker plane transitively, and
# the operator CLI is pinned broker-free at import time
# (tests/platform_unit/test_monitoring.py) — the same reason
# `mandate_check._resolve_ingress_pins` imports lazily.


def cmd_proposer_mint(args: argparse.Namespace) -> int:
    """Generate a credential and print its registration. Writes nothing."""

    from chronos.supervisor.proposers import (
        SCHEMA_VERSION,
        ProposerRegistration,
        credential_hash,
    )

    if args.expires_at:
        try:
            expires = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
        except ValueError:
            print(
                f"proposer mint: {args.expires_at!r} is not an ISO-8601 timestamp", file=sys.stderr
            )
            return 2
        if expires.tzinfo is None:
            print("proposer mint: --expires-at must carry a UTC offset", file=sys.stderr)
            return 2
    else:
        expires = datetime.now(tz=UTC) + timedelta(days=args.expires_days)

    credential = secrets.token_hex(_CREDENTIAL_BYTES)
    try:
        registration = ProposerRegistration(
            proposer_id=args.proposer_id,
            secret_sha256=credential_hash(credential),
            provider=args.provider,
            model_id=args.model_id,
            model_version=args.model_version,
            prompt_version=args.prompt_version,
            tool_schema_version=args.tool_schema_version,
            decision_schema_version=args.decision_schema_version,
            policy_version=args.policy_version,
            expires_at=expires,
        )
    except ValueError as error:
        print(f"proposer mint: {error}", file=sys.stderr)
        return 2

    entry = registration.model_dump(mode="json")
    print("# The credential, shown exactly once. Put it in the proposer process's")
    print("# environment (e.g. CHRONOS_WORKER_PROPOSER_TOKEN); it is stored nowhere else.")
    print(f"credential: {credential}")
    print()
    print('# The registration entry. Paste it into the registry file\'s "proposers"')
    print("# array (AUTONOMY_PROPOSERS_FILE); it carries only the credential's SHA-256.")
    print(json.dumps(entry, indent=2))
    print()
    print("# A complete registry document, if you are starting one:")
    print(json.dumps({"schema_version": SCHEMA_VERSION, "proposers": [entry]}, indent=2))
    return 0


def _read_revocations(database_url: str) -> tuple[dict[str, Any] | None, str]:
    """Every revocation keyed by credential hash, or ``None`` and why not (A3).

    Failure is reported rather than swallowed: a registration that looks CURRENT
    because the ledger could not be read is the exact shape of fabricated calm
    this repository's terminal tests exist to prevent.
    """

    try:
        from chronos.persistence.database import Database, _sqlite_database_path
        from chronos.supervisor.revocation import revoked_credentials
    except Exception as error:  # pragma: no cover - import failure is environmental
        return None, f"{type(error).__name__}"
    # `Database(...)` PREPARES its target: it mkdirs the parent and O_CREATs the
    # SQLite file. That is right for the backend and wrong here — `check` is a
    # reporting command that must leave the filesystem exactly as it found it, so
    # a ledger that does not exist yet has to be reported as unreadable rather
    # than brought into existence by the act of asking about it.
    try:
        sqlite_path = _sqlite_database_path(database_url)
    except ValueError as error:
        return None, f"{type(error).__name__}"
    if sqlite_path is not None and not sqlite_path.exists():
        return None, "NoLedgerFile"
    database = Database(database_url)
    try:
        with database.sessions.begin() as session:
            return dict(revoked_credentials(session)), ""
    except Exception as error:
        return None, f"{type(error).__name__}"
    finally:
        database.dispose()


def cmd_proposer_check(args: argparse.Namespace) -> int:
    """Validate a registry file and describe what it registers. Writes nothing."""

    from chronos.supervisor.proposers import load_proposer_registry

    path = Path(args.file)
    loaded = load_proposer_registry(path)
    if loaded is None:
        print(
            f"STATUS   INVALID — {path} is unreadable or does not validate; with this "
            "file configured, every proposal refuses"
        )
        return 1
    now = datetime.now(tz=UTC)
    print(
        f"STATUS   VALID — digest {loaded.digest[:16]}…, "
        f"{len(loaded.registry.proposers)} registration(s)"
    )
    if not loaded.registry.proposers:
        print(
            "NOTE     the registry is empty: proposals refuse until someone is registered "
            "(a valid lockdown state)"
        )
    revocations, failure = _read_revocations(_database_url(args))
    if revocations is None:
        print(
            f"NOTE     the revocation ledger could not be read ({failure}); an entry shown "
            "UNVERIFIED may have been revoked and this command cannot tell"
        )
    for entry in loaded.registry.proposers:
        state = _entry_state(entry, now=now, revocations=revocations)
        print(
            f"  {entry.proposer_id:<24} {state:<10} provider={entry.provider} "
            f"model={entry.model_id}@{entry.model_version} "
            f"prompt={entry.prompt_version} policy={entry.policy_version} "
            f"expires={entry.expires_at.isoformat()}"
        )
        if revocations is not None and entry.secret_sha256 in revocations:
            record = revocations[entry.secret_sha256]
            print(
                f"  {'':<24} revoked {record.revoked_at.isoformat()} — {record.reason}; "
                "mint a new credential for this proposer and restart to re-grant"
            )
    return 0


def _entry_state(entry: Any, *, now: datetime, revocations: dict[str, Any] | None) -> str:
    """The one word an operator reads. REVOKED outranks everything (A3).

    A revoked credential that has also expired is still, first and foremost,
    revoked: expiry would have let a re-mint of the same secret work, and
    revocation is what says it never will.
    """

    if revocations is not None and entry.secret_sha256 in revocations:
        return "REVOKED"
    if not entry.enabled:
        return "DISABLED"
    if not entry.is_current(now):
        return "EXPIRED"
    # Would read CURRENT — but only the ledger can rule out a revocation, so
    # without it the honest answer is that this was not verified.
    return "CURRENT" if revocations is not None else "UNVERIFIED"


def _database_url(args: argparse.Namespace) -> str:
    override = getattr(args, "database_url", "") or ""
    if override:
        return override
    from chronos.config.settings import get_settings

    return get_settings().database_url


def cmd_proposer_revoke(args: argparse.Namespace) -> int:
    """Kill one registered credential, now. Writes one database row, no files."""

    from chronos.persistence.database import Database
    from chronos.supervisor.proposers import load_proposer_registry
    from chronos.supervisor.revocation import revoke

    reason = args.reason.strip()
    if not reason:
        print(
            "proposer revoke: --reason must not be empty; a credential killed for no "
            "stated cause cannot be reviewed afterwards",
            file=sys.stderr,
        )
        return 2

    path = Path(args.file)
    loaded = load_proposer_registry(path)
    if loaded is None:
        print(
            f"proposer revoke: {path} is unreadable or does not validate, so the "
            "credential to revoke cannot be identified",
            file=sys.stderr,
        )
        return 2
    registration = loaded.registry.find(args.proposer_id)
    if registration is None:
        print(
            f"proposer revoke: {args.proposer_id!r} is not in {path}. A credential this "
            "registry does not contain already fails verification, so there is nothing "
            "to revoke",
            file=sys.stderr,
        )
        return 2

    database = Database(_database_url(args))
    try:
        with database.sessions.begin() as session:
            written = revoke(
                session,
                proposer_id=registration.proposer_id,
                secret_sha256=registration.secret_sha256,
                reason=reason,
                now=datetime.now(tz=UTC),
            )
    finally:
        database.dispose()

    if not written:
        print(f"ALREADY REVOKED  {registration.proposer_id} — no second record written")
        return 0
    print(f"REVOKED  {registration.proposer_id}")
    print(f"  credential {registration.secret_sha256[:16]}… is refused from now on:")
    print("  at the proposal route (401) and at drain time (STAMP: PROPOSER_REVOKED).")
    print("  No restart is needed, and none undoes this.")
    print(f"  The registry file {path} was NOT modified; revocation lives in the database.")
    print("  To re-grant: mint a NEW credential for this proposer, put it in the registry,")
    print("  and restart the backend — the revoked credential stays dead permanently.")
    return 0


def add_proposer_commands(sub: Any) -> None:
    """Register ``proposer mint|check`` on the operator CLI."""

    proposer = sub.add_parser(
        "proposer",
        help="mint proposer credentials and inspect the registry (ADR-0023; read-only)",
    )
    proposer_sub = proposer.add_subparsers(dest="proposer_command", required=True)

    mint = proposer_sub.add_parser(
        "mint",
        help="generate a credential and print its registration entry (writes nothing)",
    )
    mint.add_argument(
        "--proposer-id",
        required=True,
        help="stable name, [a-z0-9_-], appears in provenance and logs",
    )
    mint.add_argument(
        "--provider", required=True, help='who runs the model, e.g. "anthropic" or "tradingview"'
    )
    mint.add_argument(
        "--model-id", required=True, help='e.g. "claude-opus-5", or the bridge\'s translator name'
    )
    mint.add_argument("--model-version", default="1")
    mint.add_argument(
        "--prompt-version",
        default="1",
        help="label the policy/prompt revision, e.g. a git short hash",
    )
    mint.add_argument("--tool-schema-version", default="1")
    mint.add_argument("--decision-schema-version", default="1")
    mint.add_argument("--policy-version", default="1")
    expiry = mint.add_mutually_exclusive_group()
    expiry.add_argument(
        "--expires-at", default="", help="ISO-8601 expiry with offset (e.g. 2026-12-31T00:00:00Z)"
    )
    expiry.add_argument(
        "--expires-days", type=int, default=90, help="expiry as days from now (default 90)"
    )
    mint.set_defaults(func=cmd_proposer_mint)

    check = proposer_sub.add_parser(
        "check",
        help="validate a registry file and list what it registers (writes nothing)",
    )
    check.add_argument("--file", required=True, help="path to the registry JSON")
    check.add_argument(
        "--database-url",
        default="",
        help="where the revocation ledger lives (default: the configured database)",
    )
    check.set_defaults(func=cmd_proposer_check)

    revoke = proposer_sub.add_parser(
        "revoke",
        help=(
            "kill a registered credential immediately (A3; WRITES one database row — "
            "the only mutating proposer command, and it only ever removes authority)"
        ),
    )
    revoke.add_argument("--file", required=True, help="path to the registry JSON")
    revoke.add_argument(
        "--proposer-id",
        required=True,
        help="the registration whose credential is being killed",
    )
    revoke.add_argument(
        "--reason",
        required=True,
        help="why, for the audit trail — an act with no stated cause cannot be reviewed",
    )
    revoke.add_argument(
        "--database-url",
        default="",
        help="where the revocation ledger lives (default: the configured database)",
    )
    revoke.set_defaults(func=cmd_proposer_revoke)

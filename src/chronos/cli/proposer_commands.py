"""``chronos proposer mint|check`` — credentials and registry, read-only (ADR-0023).

Two commands, both stdout-only, following ``mandate template``/``check``'s
doctrine exactly: **a tool that mints text is not a tool that grants**. The
owner pastes the printed registration into the registry file themselves;
nothing here writes a file, and nothing here can enable, extend, or revoke a
proposer. ``tests/safety/test_proposer_credentials_exercised.py`` pins the
no-write property the same way ``test_mandate_check`` pins the mandate
commands'.

``mint`` generates the credential with ``secrets.token_hex(32)`` and prints it
exactly once, beside the registration entry that carries only its SHA-256. The
registry file never holds a credential, so reading it yields nothing
presentable — the same reasoning as the terminal's hashed session ids (R-41).
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
    for entry in loaded.registry.proposers:
        state = (
            "CURRENT" if entry.is_current(now) else ("DISABLED" if not entry.enabled else "EXPIRED")
        )
        print(
            f"  {entry.proposer_id:<24} {state:<9} provider={entry.provider} "
            f"model={entry.model_id}@{entry.model_version} "
            f"prompt={entry.prompt_version} policy={entry.policy_version} "
            f"expires={entry.expires_at.isoformat()}"
        )
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
    check.set_defaults(func=cmd_proposer_check)

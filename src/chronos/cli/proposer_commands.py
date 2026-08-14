"""``chronos proposer mint|check|fingerprint|revoke`` — credentials and registry.

ADR-0023, then A3 (revocation) and A4 (policy pinning).

``mint``, ``check`` and ``fingerprint`` are stdout-only, following ``mandate
template``/``check``'s doctrine exactly: **a tool that mints text is not a tool
that grants**. The owner pastes the printed registration into the registry file
themselves; none of the three writes a file, and none of them can enable or
extend a proposer. ``tests/safety/test_proposer_credentials_exercised.py`` pins the
no-write property the same way ``test_mandate_check`` pins the mandate
commands'.

``mint`` generates the credential with ``secrets.token_hex(32)`` and prints it
exactly once, beside the registration entry that carries only its SHA-256. The
registry file never holds a credential, so reading it yields nothing
presentable — the same reasoning as the terminal's hashed session ids (R-41).

## ``revoke`` is the only mutating command on this CLI, and it is narrow (A3)

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

## Policy-content pinning, and exactly what it proves (A4)

R-47 residual (b): ``prompt_version`` is an owner-typed label, so nothing binds
it to the policy the worker actually runs, and a policy edit is unattributable
unless the owner remembers to bump the label. ``--policy-file`` derives it
instead — ``sha256(policy bytes)[:16]`` — so an edited policy produces a
different registration, the mandate's version pin stops agreeing, and admission
refuses on ``VERSION_PIN_MISMATCH`` until the owner re-pins deliberately.

**What that does NOT prove, stated here because this is the file someone reads
before believing otherwise:** it does not prove which policy the worker *ran*.
The worker loads its policy at startup from ``CHRONOS_WORKER_POLICY_FILE``
(default ``worker/policy.md``) and nothing in Chronos observes that read. The
digest binds the file **as it was at mint time** to the label the registration
carries. ``check --policy-file`` narrows the gap by comparing the file on this
machine *now* against what was registered — drift detection, not attestation.
Closing the rest needs the worker to attest its own policy digest, which is a
worker change and is not this item.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Minted credentials are 32 random bytes, hex-encoded — the same strength as
#: the backend's own API token.
_CREDENTIAL_BYTES = 32

#: How much of the policy digest becomes ``prompt_version``. 16 hex characters
#: is 64 bits — far past accidental collision for a handful of hand-authored
#: policy revisions, and short enough to read in a registry file, a mandate pin,
#: and a log line without wrapping. The field is a label, not a security
#: boundary: nothing authenticates on it, so truncation costs nothing here.
POLICY_DIGEST_CHARS = 16

#: ``--prompt-version``'s default. Named so ``mint`` can tell "the owner typed a
#: label" from "the owner left it alone", which is what makes refusing both
#: flags possible without argparse's help.
_PROMPT_VERSION_DEFAULT = "1"


def policy_fingerprint(path: Path) -> str:
    """``sha256`` of the policy file's exact bytes, truncated to a label.

    Over the raw bytes, not the decoded-and-stripped text the worker uses: a
    trailing-whitespace edit is still an edit, and a fingerprint that forgave
    some byte changes but not others would be a rule nobody could apply from
    memory.

    Raises ``ValueError`` when the file cannot be read or is empty. Empty is
    refused rather than digested because the worker itself refuses to start on
    an empty policy ("an empty policy is not a strategy", ``worker/config.py``),
    and minting a credential that claims a policy the worker would reject would
    put a fiction in the registry.
    """

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"the policy file {path} is unreadable: {error}") from error
    if not raw.strip():
        raise ValueError(
            f"the policy file {path} is empty; an empty policy is not a strategy, and the "
            "worker refuses to start on one"
        )
    return hashlib.sha256(raw).hexdigest()[:POLICY_DIGEST_CHARS]


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

    prompt_version = args.prompt_version
    policy_file = getattr(args, "policy_file", "") or ""
    if policy_file:
        # argparse's mutually-exclusive group already refuses both flags, but
        # this path is also called directly (tests, scripts), so the ambiguity
        # is refused here too rather than resolved by precedence. A label whose
        # provenance depends on which flag won is worse than no label.
        if prompt_version != _PROMPT_VERSION_DEFAULT:
            print(
                "proposer mint: pass --policy-file or --prompt-version, not both; the "
                "digest and a typed label cannot both be the authority for one field",
                file=sys.stderr,
            )
            return 2
        try:
            prompt_version = policy_fingerprint(Path(policy_file))
        except ValueError as error:
            print(f"proposer mint: {error}", file=sys.stderr)
            return 2

    credential = secrets.token_hex(_CREDENTIAL_BYTES)
    try:
        registration = ProposerRegistration(
            proposer_id=args.proposer_id,
            secret_sha256=credential_hash(credential),
            provider=args.provider,
            model_id=args.model_id,
            model_version=args.model_version,
            prompt_version=prompt_version,
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
    if policy_file:
        print()
        print(f"# prompt_version was derived from {policy_file} (A4):")
        print(f"#   sha256(bytes)[:{POLICY_DIGEST_CHARS}] = {prompt_version}")
        print("# Editing that policy changes this value, so the mandate's version pin stops")
        print("# agreeing and admission refuses VERSION_PIN_MISMATCH until you re-pin. What")
        print("# this does NOT prove: which policy the worker actually ran — nothing in")
        print("# Chronos observes the worker's own read of its policy file.")
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
    policy_file = getattr(args, "policy_file", "") or ""
    expected_prompt = ""
    if policy_file:
        try:
            expected_prompt = policy_fingerprint(Path(policy_file))
        except ValueError as error:
            print(f"POLICY   UNREADABLE — {error}")
        else:
            print(f"POLICY   {policy_file} fingerprints to {expected_prompt} (A4)")
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
        if expected_prompt:
            # Drift detection, and the only part of A4 that keeps working after
            # mint time: does the policy on THIS machine still match what this
            # registration was minted against? A DIFFERS is not necessarily
            # wrong — the owner may have edited deliberately — it means the
            # label no longer describes the file, and the mandate pin that
            # trusts the label is now describing something that changed.
            verdict = "MATCHES" if entry.prompt_version == expected_prompt else "DIFFERS"
            print(
                f"  {'':<24} policy {verdict}: registered prompt_version="
                f"{entry.prompt_version}, file now {expected_prompt}"
                + (
                    ""
                    if verdict == "MATCHES"
                    else " — re-mint (or re-pin) if this edit was intended"
                )
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


def cmd_proposer_fingerprint(args: argparse.Namespace) -> int:
    """Print a policy file's derived ``prompt_version``. Writes nothing.

    Exists so re-pinning a mandate does not require re-minting a credential:
    after an intended policy edit the owner needs the new label, and minting a
    fresh secret to learn it would rotate a credential for no security reason.
    """

    try:
        derived = policy_fingerprint(Path(args.policy_file))
    except ValueError as error:
        print(f"proposer fingerprint: {error}", file=sys.stderr)
        return 2
    print(f"prompt_version: {derived}")
    print(f"# sha256({args.policy_file})[:{POLICY_DIGEST_CHARS}]")
    print("# Put this in the registration's prompt_version and in the mandate's")
    print("# versions.prompt_version. They must agree or admission refuses the")
    print("# proposal with VERSION_PIN_MISMATCH — which is the point.")
    print("# It does NOT prove which policy the worker ran: nothing in Chronos")
    print("# observes the worker's own read of its policy file.")
    return 0


def add_proposer_commands(sub: Any) -> None:
    """Register ``proposer mint|check|fingerprint|revoke`` on the operator CLI."""

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
    prompt = mint.add_mutually_exclusive_group()
    prompt.add_argument(
        "--prompt-version",
        default=_PROMPT_VERSION_DEFAULT,
        help="label the policy/prompt revision, e.g. a git short hash",
    )
    prompt.add_argument(
        "--policy-file",
        default="",
        help=(
            "derive prompt_version from this policy file's bytes instead of typing a "
            "label (A4). Binds the label to the file AS MINTED; it does not prove which "
            "policy the worker ran"
        ),
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
    check.add_argument(
        "--policy-file",
        default="",
        help=(
            "also report, per registration, whether its prompt_version still matches "
            "this policy file's current bytes (A4 drift check)"
        ),
    )
    check.set_defaults(func=cmd_proposer_check)

    fingerprint = proposer_sub.add_parser(
        "fingerprint",
        help=(
            "print the prompt_version a policy file derives to, so a mandate can be "
            "re-pinned without re-minting a credential (writes nothing)"
        ),
    )
    fingerprint.add_argument("--policy-file", required=True, help="path to the policy file")
    fingerprint.set_defaults(func=cmd_proposer_fingerprint)

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

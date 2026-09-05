"""The proposer registry: who may propose, and as whom (ADR-0023).

Until this module, `POST /autonomy/proposals` authenticated with the same local
API token every other mutating route accepts, and every proposal was stamped
with one hardcoded identity. Both halves of plan §6 finding 6 in one sentence:
the credential was not proposal-only, and provenance authenticated the ingress
rather than an author.

This module is the registry that fixes both. The owner registers each proposer
— the Claude worker, the TradingView bridge, anything else — as a record
binding a **credential hash** to the **identity facts the queue writer will
stamp**: provider, model, prompt/tool/schema/policy versions, and an expiry.
Identity then comes from *which credential authenticated*, which is the one
place a hostile payload cannot reach: the ingress already refuses payloads
carrying ``provenance``, and now there is finally something real to stamp
instead.

## The trust model, precisely

- **The file is a grant, authored by the owner**, exactly like the mandate
  file: validated on boot, digest-stamped in logs, and never writable by any
  CLI or route. `python -m chronos.cli proposer mint` prints a credential and
  its registration JSON to stdout — the owner pastes it in; nothing writes.
- **The file holds only hashes.** A registration stores the SHA-256 of the
  credential, so reading the registry does not yield a credential. The secret
  exists in exactly two places: the proposer process's environment, and
  nowhere.
- **Registration is identity, not authority.** A registered proposer can do
  precisely one thing an anonymous caller cannot: be *attributed*. Its
  proposals walk the same ingress, admission, sizing, compilation, and
  handoff; the mandate's version pins now bind to a real authorship — a
  mandate pinned to the worker's registration refuses the bridge's proposals
  at admission, which is the owner choosing which author a grant trusts.
- **Everything expires.** A registration without a future expiry never
  verifies. Renewal is a fresh owner edit, the mandate's own rule applied to
  authorship.

## Failure posture

An unreadable or invalid registry file loads as ``None`` and the caller alerts
— the same shape as a broken mandate: the backend boots, proposals refuse, and
the process that can still close positions never dies because a grant was
malformed. A configured-but-empty registry is a valid lockdown: proposals
refuse until the owner registers someone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from chronos.utils.secure_files import AuthorityMode, read_authority_file

_logger = logging.getLogger("chronos.supervisor.proposers")

#: The registry file's schema version. Bumping it is a reviewed act.
SCHEMA_VERSION = 1

#: Proposer ids appear in logs, the journal, and provenance — bound them to an
#: alphabet that is safe everywhere text is rendered.
_PROPOSER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def credential_hash(credential: str) -> str:
    """The only form of a credential the registry ever stores or compares."""

    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RegistrationBinding:
    """Immutable identifiers for the exact registration that authenticated.

    ``credential_epoch`` is the already non-secret credential hash used by the
    revocation ledger. ``registry_entry_digest`` additionally binds every
    owner-authored identity/version/expiry field, so changing a registration
    without rotating its credential still invalidates outstanding work.
    """

    credential_epoch: str
    registry_entry_digest: str


class ProposerRegistration(BaseModel):
    """One proposer the owner has registered: a credential hash bound to authorship.

    The seven version fields mirror :class:`~chronos.supervisor.queue`'s
    ``HarnessIdentity`` (and therefore ``DecisionProvenance``) on purpose:
    these ARE the values the writer will stamp, so their bounds match the
    contract's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposer_id: str = Field(min_length=1, max_length=64)
    #: SHA-256 of the credential, lowercase hex. Never the credential itself.
    secret_sha256: str
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    tool_schema_version: str = Field(min_length=1, max_length=64)
    decision_schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    #: Hard expiry. There is no "forever" credential, by construction.
    expires_at: AwareDatetime
    #: The owner's off switch that keeps the record (and its history) legible.
    enabled: bool = True

    @field_validator("proposer_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _PROPOSER_ID_PATTERN.match(value):
            raise ValueError(
                "proposer_id must be 1-64 characters of [a-z0-9_-], starting alphanumeric; "
                "it appears in logs and provenance"
            )
        return value

    @field_validator("secret_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError(
                "secret_sha256 must be the 64-character lowercase hex SHA-256 of the "
                "credential — never the credential itself"
            )
        return normalized

    def is_current(self, now: datetime) -> bool:
        """Enabled and unexpired. Everything else about authority lives elsewhere."""

        return self.enabled and now < self.expires_at


def registration_binding(registration: ProposerRegistration) -> RegistrationBinding:
    """Bind durable work to one complete validated registry entry.

    The domain and registry schema version make the encoding explicit and
    evolvable. Canonical JSON prevents key order or whitespace from changing
    the digest of semantically identical validated registrations.
    """

    registration_document = registration.model_dump(mode="json")
    registration_document["expires_at"] = registration.expires_at.astimezone(UTC).isoformat()
    canonical = json.dumps(
        {
            "domain": "chronos.proposer-registration.v1",
            "registry_schema_version": SCHEMA_VERSION,
            "registration": registration_document,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return RegistrationBinding(
        credential_epoch=registration.secret_sha256,
        registry_entry_digest=hashlib.sha256(canonical).hexdigest(),
    )


class ProposerRegistry(BaseModel):
    """The owner-authored registry document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    proposers: tuple[ProposerRegistration, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"registry schema_version {value} is not the supported {SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> ProposerRegistry:
        ids = [entry.proposer_id for entry in self.proposers]
        if len(ids) != len(set(ids)):
            raise ValueError("proposer_id values must be unique within the registry")
        hashes = [entry.secret_sha256 for entry in self.proposers]
        if len(hashes) != len(set(hashes)):
            raise ValueError(
                "two registrations share a credential hash; a shared credential would make "
                "attribution ambiguous, which is the defect this registry exists to fix"
            )
        return self

    def verify(self, presented: str, *, now: datetime) -> ProposerRegistration | None:
        """The registration a presented credential authenticates as, or None.

        Constant-time comparison per entry (``hmac.compare_digest`` over the
        hex digests), and every entry is visited even after a match so the
        response time does not narrow which position matched. Disabled and
        expired registrations never verify — a stale credential is a refusal,
        not a warning.
        """

        digest = credential_hash(presented)
        matched: ProposerRegistration | None = None
        for entry in self.proposers:
            if hmac.compare_digest(digest, entry.secret_sha256) and entry.is_current(now):
                matched = entry
        return matched

    def find(self, proposer_id: str) -> ProposerRegistration | None:
        for entry in self.proposers:
            if entry.proposer_id == proposer_id:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class LoadedRegistry:
    """A validated registry and the digest of the exact text that granted it."""

    registry: ProposerRegistry
    digest: str


def load_proposer_registry(path: Path) -> LoadedRegistry | None:
    """Read and validate the owner's proposer grants. Invalid is None, loudly.

    Mirrors ``load_persistent_mandate``: the digest is over the raw bytes, so
    logs record exactly which document defined who may propose, and an edited
    file is distinguishable from the one before it.

    Read through the descriptor-bound authority-file contract in
    ``AuthorityMode.GRANT``: no symlink, a regular file owned by this effective
    user, one link, not writable by group or other, and the digest taken over
    the same bytes every check ran against. GRANT rather than SECRET because a
    registry is an owner-authored decision, not a secret — anyone who can WRITE
    it can change who may propose, while reading it discloses no credential
    (entries hold only hashes). ``FileNotFoundError`` and
    ``UnsafeAuthorityFile`` both propagate: absence and unsafety are the
    caller's to distinguish, and neither is "invalid content".
    """

    try:
        contents = read_authority_file(path, label="proposer registry", mode=AuthorityMode.GRANT)
    except FileNotFoundError:
        # The previous shape caught OSError, so an absent registry returned
        # None. Keep that exactly: absence is a configuration state every
        # caller already handles, and widening it into an exception would
        # change them all for no safety gain.
        _logger.error("Proposer registry file %s is unreadable", path)
        return None
    try:
        registry = ProposerRegistry.model_validate_json(contents.data)
    except ValueError:
        _logger.exception(
            "Proposer registry file %s does not validate; proposals will refuse", path
        )
        return None
    return LoadedRegistry(registry=registry, digest=contents.sha256)

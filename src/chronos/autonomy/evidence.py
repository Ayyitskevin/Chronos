"""EvidenceBundles: the only thing a model is allowed to see (ADR-0016 §5, M4).

A model's view of the world is exactly one immutable, versioned, digest-pinned
bundle. It does not query a database, hold a broker handle, or read files. It is
*given* facts, and the bundle is the boundary.

Three properties, and each closes a specific way this goes wrong:

- **Immutable and digest-pinned.** The digest covers the bundle's contents, and
  the supervisor compares the decision's cited digest against the bundle it
  issued (``admission._check_evidence_bundle``). A model that reasoned from
  different facts than the ones on record is refused, so "the evidence it
  reasoned from" and "the evidence in the audit trail" cannot diverge.
- **Redacted by construction.** The bundle is built from typed facts, and there
  is no field for an account number, a credential, a file path, or a raw broker
  identifier. Redaction is not a filtering step that could be skipped — the
  shape simply has nowhere to put those things. Account identity appears only as
  a pseudonymous fingerprint.
- **Text is quoted, never trusted.** ``TextualEvidence`` exists because real
  evidence includes news and filings, and those are attacker-controlled. It is
  carried with its source and marked untrusted; nothing in the deterministic
  pipeline reads it, and the injection tests assert that adversarial text
  changes no order parameter.

## On prompt injection (R-30)

Chronos does not claim to prevent prompt injection. Nothing does, reliably. What
it claims is narrower and testable: **a successful injection cannot become a
trade that the mandate would not otherwise have permitted.** Every number that
reaches a venue is derived by deterministic code from supervisor-gathered facts;
the most an injected instruction can do is cause a *differently-shaped proposal*,
which then faces admission, sizing, and compilation exactly like any other. The
deterministic kernel is the control that holds when injection succeeds, and that
is why it is the thing that was built first.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, Field, field_validator

from chronos.autonomy.base import AutonomyModel

#: Substrings that must never appear in a bundle. This is a defense-in-depth
#: tripwire, not the redaction mechanism — the mechanism is that the types have
#: no field for these. A tripwire is still worth having: it catches a future
#: field added without thinking, which is exactly when redaction fails.
_FORBIDDEN_MARKERS = (
    "password",
    "api_key",
    "apikey",
    "secret",
    "token",
    "private_key",
    "BEGIN RSA",
    "BEGIN OPENSSH",
)


class QuoteFact(AutonomyModel):
    """One instrument's observed market state. Never a broker handle."""

    symbol: str = Field(min_length=1, max_length=32)
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    as_of: AwareDatetime
    quality: str = Field(min_length=1, max_length=32)


class PositionFact(AutonomyModel):
    """What the account holds, in the model's read-only view.

    ``quantity`` is signed. There is deliberately no account id, no broker
    position id, and no cost basis tied to an account number.
    """

    symbol: str = Field(min_length=1, max_length=32)
    quantity: Decimal
    average_cost: Decimal | None = None


class AccountFact(AutonomyModel):
    """Account state, pseudonymously.

    ``fingerprint`` is the hashed account identity used everywhere else in
    Chronos. There is no field for the broker account number, so a bundle cannot
    carry one even by accident.
    """

    fingerprint: str = Field(min_length=64, max_length=64)
    net_liquidation_usd: Decimal | None = None
    buying_power_usd: Decimal | None = None
    total_cash_usd: Decimal | None = None


#: Schema identity for bundles that carry Five-Tool / pairing advisory facts.
#: Empty advisory collections may still use ``"1"``; a non-empty advisory pack
#: must use this version so the digest is honest about what the model was shown.
ADVISORY_BUNDLE_VERSION = "1.1"

#: Names that would make an advisory fact look like a size, stop, or credential.
#: Advisory facts are closed-bar research evidence. They may not smuggle an
#: economic instruction the kernel would have to ignore (ADR-0021).
_FORBIDDEN_ADVISORY_NAME_MARKERS = (
    "account",
    "api_key",
    "credential",
    "equity",
    "notional",
    "password",
    "quantity",
    "risk_budget",
    "risk_multiplier",
    "risk_scale",
    "secret",
    "shares",
    "size",
    "stop",
    "token",
    "transmit",
)


def _reject_advisory_name(name: str) -> str:
    normalized = name.strip()
    if not normalized or not normalized.replace("_", "").isalnum():
        raise ValueError("advisory value names must be simple identifiers")
    lowered = normalized.lower()
    for marker in _FORBIDDEN_ADVISORY_NAME_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"advisory fact {normalized!r} looks economic or secret-bearing; "
                "advisory packs may carry closed-bar signal facts only"
            )
    return normalized


def _reject_advisory_number(
    value: bool | int | float | str | None,
) -> bool | int | float | str | None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError("advisory numeric values must be finite")
    if isinstance(value, str) and len(value) > 128:
        raise ValueError("advisory string values are limited to 128 characters")
    return value


class AdvisoryDatum(AutonomyModel):
    """One named closed-bar fact. Never a size, stop, or credential."""

    name: str = Field(min_length=1, max_length=64)
    value: bool | int | float | str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _reject_advisory_name(value)

    @field_validator("value")
    @classmethod
    def _validate_value(
        cls, value: bool | int | float | str | None
    ) -> bool | int | float | str | None:
        return _reject_advisory_number(value)


class AdvisoryFiveToolFact(AutonomyModel):
    """Closed-bar Five-Tool signal facts shown to a model. Explicitly advisory.

    This is not an order, a size, or a stop. The kernel never reads these
    fields to compute quantity or protection. Changing them changes the
    bundle digest, which is the point: the model and the audit trail must
    have seen the same closed-bar evidence.
    """

    advisory: bool = True
    symbol: str = Field(min_length=1, max_length=32)
    timestamp_utc: AwareDatetime
    primary_sequence_id: str = Field(min_length=1, max_length=256)
    bar_index: int = Field(ge=0)
    intent: str = Field(min_length=1, max_length=32)
    long_setup: str = Field(min_length=1, max_length=64)
    short_setup: str = Field(min_length=1, max_length=64)
    warmup_blockers: tuple[str, ...] = ()
    state_digest: str = Field(min_length=64, max_length=64)
    values: tuple[AdvisoryDatum, ...] = Field(default=(), max_length=64)

    @field_validator("advisory")
    @classmethod
    def _always_advisory(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Five-Tool bundle facts are advisory; they cannot be marked binding")
        return value

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("intent", "long_setup", "short_setup")
    @classmethod
    def _normalize_label(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("state_digest")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("state_digest must be a 64-character lowercase hex digest")
        return normalized


class AdvisoryFeatureSnapshotFact(AutonomyModel):
    """One pairing-family snapshot. Explicitly advisory; never a size."""

    advisory: bool = True
    family: str = Field(min_length=1, max_length=32)
    timestamp_utc: AwareDatetime
    primary_sequence_id: str = Field(min_length=1, max_length=256)
    warmup: bool
    missing_required: tuple[str, ...] = ()
    values: tuple[AdvisoryDatum, ...] = Field(default=(), max_length=64)

    @field_validator("advisory")
    @classmethod
    def _always_advisory(cls, value: bool) -> bool:
        if not value:
            raise ValueError("pairing snapshots are advisory; they cannot be marked binding")
        return value

    @field_validator("family")
    @classmethod
    def _normalize_family(cls, value: str) -> str:
        return value.strip().lower()


class AdvisoryVetoFact(AutonomyModel):
    """Pairing veto shown to a model. Explicitly advisory; not an order."""

    advisory: bool = True
    status: str = Field(min_length=1, max_length=32)
    original_intent: str = Field(min_length=1, max_length=32)
    filtered_intent: str = Field(min_length=1, max_length=32)
    reasons: tuple[str, ...] = ()

    @field_validator("advisory")
    @classmethod
    def _always_advisory(cls, value: bool) -> bool:
        if not value:
            raise ValueError("pairing vetoes are advisory; they cannot be marked binding")
        return value

    @field_validator("status", "original_intent", "filtered_intent")
    @classmethod
    def _normalize_label(cls, value: str) -> str:
        return value.strip().lower()


class TextualEvidence(AutonomyModel):
    """Attacker-controlled text, carried honestly as attacker-controlled.

    News, filings, transcripts. It is marked untrusted and attributed to a
    source so the audit trail shows where an idea came from. Nothing
    deterministic reads ``body``: it exists to inform a model, and a model's
    conclusions are then judged as any other decision would be.
    """

    evidence_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=200)
    as_of: AwareDatetime
    body: str = Field(max_length=20_000)
    #: Always True. A field rather than a constant so it appears in the record
    #: and in anything rendered from it — the reader should see the label.
    untrusted: bool = True

    @field_validator("untrusted")
    @classmethod
    def _always_untrusted(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "external text is untrusted by definition; marking it trusted would "
                "misrepresent the one thing a reader most needs to know about it"
            )
        return value


class EvidenceBundle(AutonomyModel):
    """One immutable, versioned view of the world, issued to a model.

    The digest is computed from the content, so two bundles with the same facts
    have the same digest and a bundle that was altered has a different one.
    """

    bundle_id: str = Field(min_length=1, max_length=128)
    #: The bundle schema version, pinned by the mandate like every other version.
    bundle_version: str = Field(min_length=1, max_length=32)
    issued_at: AwareDatetime
    account: AccountFact | None = None
    quotes: tuple[QuoteFact, ...] = Field(default=(), max_length=256)
    positions: tuple[PositionFact, ...] = Field(default=(), max_length=256)
    texts: tuple[TextualEvidence, ...] = Field(default=(), max_length=64)
    #: Closed-bar Five-Tool / pairing facts. Advisory only: the kernel does not
    #: size, stop, or transmit from these fields.
    five_tool_signals: tuple[AdvisoryFiveToolFact, ...] = Field(default=(), max_length=64)
    feature_snapshots: tuple[AdvisoryFeatureSnapshotFact, ...] = Field(default=(), max_length=64)
    pairing_vetoes: tuple[AdvisoryVetoFact, ...] = Field(default=(), max_length=64)

    def digest(self) -> str:
        """SHA-256 over the canonical content. Excludes nothing that was shown.

        Canonicalized the same way the hash chain canonicalizes payloads, so a
        key-order difference cannot produce a different digest for identical
        facts and break the supervisor's comparison for no reason.
        """

        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def redaction_violations(self) -> tuple[str, ...]:
        """Any forbidden marker found anywhere in the bundle.

        A tripwire, checked at issue time. The real guarantee is structural —
        there is no field for a credential — but a future field added without
        thought is precisely when that guarantee lapses, so this looks anyway.
        """

        rendered = json.dumps(self.model_dump(mode="json"), default=str).lower()
        return tuple(marker for marker in _FORBIDDEN_MARKERS if marker.lower() in rendered)


def issue(
    *,
    bundle_id: str,
    bundle_version: str,
    issued_at: datetime,
    account: AccountFact | None = None,
    quotes: tuple[QuoteFact, ...] = (),
    positions: tuple[PositionFact, ...] = (),
    texts: tuple[TextualEvidence, ...] = (),
    five_tool_signals: tuple[AdvisoryFiveToolFact, ...] = (),
    feature_snapshots: tuple[AdvisoryFeatureSnapshotFact, ...] = (),
    pairing_vetoes: tuple[AdvisoryVetoFact, ...] = (),
) -> tuple[EvidenceBundle, str]:
    """Build a bundle and return it with its digest, refusing on a redaction hit.

    Returning the digest alongside is deliberate: the caller must record it as
    the *expected* digest for this run, and having to receive it makes that hard
    to forget. A bundle whose digest nobody kept cannot be checked against
    anything, which would quietly restore the gap the check exists to close.

    A non-empty advisory pack requires :data:`ADVISORY_BUNDLE_VERSION` so the
    schema identity matches what the model was shown.
    """

    if (
        five_tool_signals or feature_snapshots or pairing_vetoes
    ) and bundle_version != ADVISORY_BUNDLE_VERSION:
        raise ValueError(
            "advisory Five-Tool / pairing facts require bundle_version "
            f"{ADVISORY_BUNDLE_VERSION}; an older version would hide a schema change "
            "inside an unchanged pin"
        )

    bundle = EvidenceBundle(
        bundle_id=bundle_id,
        bundle_version=bundle_version,
        issued_at=issued_at,
        account=account,
        quotes=quotes,
        positions=positions,
        texts=texts,
        five_tool_signals=five_tool_signals,
        feature_snapshots=feature_snapshots,
        pairing_vetoes=pairing_vetoes,
    )
    violations = bundle.redaction_violations()
    if violations:
        raise ValueError(
            "refusing to issue an evidence bundle containing forbidden material: "
            + ", ".join(violations)
        )
    return bundle, bundle.digest()


def as_model_view(bundle: EvidenceBundle) -> dict[str, Any]:
    """The bundle as plain data, ready to be rendered into a prompt.

    Returns a copy. A model-facing view that shared structure with the recorded
    bundle would let a rendering step mutate the thing the digest was taken over
    — and then the audit trail would describe evidence nobody was shown.
    """

    view: dict[str, Any] = bundle.model_dump(mode="json")
    return view

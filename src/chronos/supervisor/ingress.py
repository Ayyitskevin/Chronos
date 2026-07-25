"""The proposal ingress: where an external model worker hands Chronos a decision (M5).

ADR-0016 §3 requires the model worker to run **outside** the broker-writing
process. M4 delivered a *code* boundary — ``chronos.autonomy`` cannot import
anything that acts — and disclosed honestly (R-35) that an import graph does not
sandbox Python. This module is the other half: a **process** boundary.

Chronos does not call a model. A model worker, running wherever its owner runs
it, calls *in*. That inversion is the whole design:

- **Chronos makes no outbound call**, so there is no provider SDK in this
  process, no API key in this process, and no egress path a compromised model
  could ride back through. A worker that dies, hangs, or is never started
  produces no decisions, which is the correct failure mode.
- **The worker holds no Chronos capability.** It receives an EvidenceBundle and
  returns a proposal. It has no broker handle, no database session, no lease, no
  kill switch, and no submission path — not because it promises not to use them,
  but because they were never in its address space.
- **The ingress trusts nothing it receives.** Everything below is written as
  though the sender is hostile, because a separate process is exactly where an
  attacker who compromised the model worker would be standing.

## What "trusts nothing" means concretely

A payload arriving here is bytes from another process. It is not a
``ProposedDecision`` until this module says so, and this module says so only
after:

1. **A size bound before parsing.** A multi-megabyte payload is refused by
   length, not by exhausting memory proving it invalid.
2. **Strict JSON, one object.** No trailing data, no NaN/Infinity (which
   ``json`` accepts by default and which would sail into a ``Decimal`` as a
   non-finite quantity), no nesting deep enough to blow the parser's stack.
3. **The full contract.** ``ProposedDecision`` validation runs unchanged —
   every field validator, the kind/payload coherence rules, the control-character
   refusal. Nothing here re-implements or relaxes them.
4. **No provenance, no id.** Those belong to
   :mod:`chronos.supervisor.queue`, and a payload that tries to supply them is
   refused loudly rather than having them silently stripped: a sender who
   *tried* is a sender worth knowing about.

## Deliberately not here

There is no authentication of *which* worker is calling. That is the transport's
job — a Unix socket with filesystem permissions, or loopback plus the existing
API token — and inventing a second, weaker scheme here would give false
assurance. The ingress's guarantee is narrower and stated exactly: **whatever
arrives is either a well-formed proposal that authorizes nothing, or a refusal.**
Everything that decides whether it may become an order happens downstream, in
gates a caller cannot reach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from chronos.autonomy import ProposedDecision

#: Refuse before parsing. A proposal is a small structured object; anything this
#: large is either a bug or an attempt to make the parser the attack surface.
MAX_PAYLOAD_BYTES = 256 * 1024

#: Bound nesting so a deeply-nested payload cannot exhaust the parser's stack.
#: Real proposals nest about four levels; sixteen is generous and still finite.
MAX_NESTING_DEPTH = 16

#: Fields the sender may never supply. Refused rather than stripped: silently
#: dropping them would hide a worker that is trying to attribute itself.
_WRITER_OWNED_FIELDS = ("provenance", "decision_id")


class IngressRejected(ValueError):
    """A payload did not become a proposal, and the reason is safe to log.

    The message never echoes payload content. An error string that quoted the
    input would let a hostile worker write arbitrary text into an operator's
    terminal and logs through the one path guaranteed to be read — which is the
    same reasoning behind the decision contract's control-character refusal.
    """


@dataclass(frozen=True, slots=True)
class IngressOutcome:
    """A parsed proposal, or a refusal. ``proposal`` is never a default."""

    proposal: ProposedDecision | None
    refusal: str = ""
    #: Bytes accepted, for rate accounting by the caller.
    payload_bytes: int = 0

    @property
    def accepted(self) -> bool:
        return self.proposal is not None


def _depth(value: Any, current: int = 0) -> int:
    if current > MAX_NESTING_DEPTH:
        return current
    if isinstance(value, dict):
        return max((_depth(item, current + 1) for item in value.values()), default=current)
    if isinstance(value, list):
        return max((_depth(item, current + 1) for item in value), default=current)
    return current


def _reject_non_finite(value: Any) -> None:
    """Refuse NaN and Infinity anywhere in the payload.

    ``json.loads`` accepts them by default, and a non-finite float reaching a
    ``Decimal`` field produces a quantity that compares strangely against every
    ceiling — ``NaN > limit`` is False, so a NaN size would pass a naive check.
    The contract's own validators catch it too; refusing here means the error
    names the real problem instead of surfacing as a confusing type failure.
    """

    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise IngressRejected(
            "payload contains a non-finite number; NaN and Infinity are never valid "
            "quantities and compare falsely against every limit"
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def parse_proposal(payload: bytes | str) -> IngressOutcome:
    """Turn bytes from another process into a proposal, or refuse.

    Never raises for a bad payload — a hostile or buggy worker must not be able
    to crash the process that holds the broker connection. Returns an outcome
    the caller records. The one thing it does raise on is a programming error in
    Chronos itself, which should not be swallowed.
    """

    try:
        return _parse(payload)
    except IngressRejected as error:
        return IngressOutcome(proposal=None, refusal=str(error))


def _parse(payload: bytes | str) -> IngressOutcome:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    size = len(raw)
    if size == 0:
        raise IngressRejected("empty payload")
    if size > MAX_PAYLOAD_BYTES:
        raise IngressRejected(
            f"payload is {size} bytes, over the {MAX_PAYLOAD_BYTES}-byte ingress limit; "
            "a proposal is a small structured object"
        )

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IngressRejected("payload is not valid UTF-8") from error

    try:
        document = json.loads(decoded, parse_constant=_refuse_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise IngressRejected("payload is not a single well-formed JSON document") from error

    if not isinstance(document, dict):
        raise IngressRejected(
            f"payload must be a JSON object describing one proposal, got {type(document).__name__}"
        )
    if _depth(document) > MAX_NESTING_DEPTH:
        raise IngressRejected(
            f"payload nests deeper than {MAX_NESTING_DEPTH} levels; a proposal does not"
        )
    _reject_non_finite(document)

    present = [name for name in _WRITER_OWNED_FIELDS if name in document]
    if present:
        raise IngressRejected(
            f"payload supplies writer-owned field(s) {sorted(present)}: a model may not "
            "attribute or name its own decision. These are stamped by the deterministic "
            "queue writer from configuration this sender does not have."
        )

    try:
        proposal = ProposedDecision.model_validate(document)
    except ValidationError as error:
        # Report which fields failed, never what they contained: a hostile
        # worker must not be able to write chosen text into operator logs.
        locations = sorted(
            {
                ".".join(str(part) for part in item.get("loc", ())) or "(root)"
                for item in error.errors()
            }
        )
        raise IngressRejected(
            "payload is not a valid proposal; rejected field(s): " + ", ".join(locations)
        ) from error

    return IngressOutcome(proposal=proposal, payload_bytes=size)


def _refuse_constant(name: str) -> Any:
    raise IngressRejected(
        f"payload contains the JSON constant {name!r}; NaN and Infinity are never valid"
    )

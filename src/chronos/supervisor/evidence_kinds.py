"""What a bundle's origin claims, and which citation may back it (ADR-0028).

This vocabulary has its own module for one reason: **the admission kernel must
stay pure.** ``chronos.supervisor.admission`` documents itself as having "no
broker, no database, no clock of its own", which is what lets every refusal path
be exercised without any of them — and check 9's payload-side half needs this
rule. Importing :mod:`chronos.supervisor.evidence_bundles` to get it would drag
SQLAlchemy into the pure kernel's import graph for the sake of one lookup table.

So the rule lives here, with no dependencies at all, and both sides import it.
There is exactly one statement of which citation kinds a bundle kind admits, and
neither the durable writer nor the kernel can drift from the other.
"""

from __future__ import annotations

from enum import StrEnum


class BundleKind(StrEnum):
    """What the backend can honestly say about a bundle's origin.

    The distinction is the honest half of ADR-0028 and is never cosmetic:

    - ``BACKEND_SERVED`` — the backend composed a canonical document and took
      SHA-256 over the exact bytes it served. It is a **witness**: it can
      recompute what it sent, so "unissued", "issued to another proposer" and
      "expired" are facts it checks rather than claims it accepts.
    - ``ALERT_ATTESTED`` — a proposer asserted, under its own credential and at a
      recorded time, that it saw bytes with this digest. The backend never saw
      those bytes and does not claim to. **Attested is not witnessed:** the
      record is non-repudiation, not verification.

    ADR-0028's recommended rule for the promotion ladder travels with the label
    and is blunt: an attested bundle may back a proposal; it may **not** back a
    promotion rung. A source whose evidence originates outside Chronos cannot
    produce evidence Chronos witnessed, and calling it otherwise would be exactly
    the false-evidence class the ladder exists to prevent. (Whether and how a
    bundle reference appears in a rung is ADR-0024's decision, not this one's.)
    """

    BACKEND_SERVED = "backend_served"
    ALERT_ATTESTED = "alert_attested"


#: Which citation ``kind`` each bundle kind admits. The worker's digest machinery
#: emits ``worker_evidence_snapshot`` (``worker/evidence.py``); the bridge's emits
#: ``tradingview_alert`` (``chronos.bridge.translate``). This table is the rule
#: that they do not substitute for one another.
_CITATION_KINDS: dict[BundleKind, frozenset[str]] = {
    BundleKind.BACKEND_SERVED: frozenset({"worker_evidence_snapshot"}),
    BundleKind.ALERT_ATTESTED: frozenset({"tradingview_alert"}),
}


def kind_permits_citation(kind: str, citation_kind: str) -> bool:
    """Whether a citation of ``citation_kind`` may back a bundle of ``kind``.

    An unrecognized bundle kind admits nothing. A kind this build does not
    understand is a record it cannot judge, and deny-by-default means an
    unjudgeable record refuses rather than passing on the strength of the fields
    it happens to recognize.
    """

    try:
        bundle_kind = BundleKind(kind)
    except ValueError:
        return False
    return citation_kind in _CITATION_KINDS[bundle_kind]


def citation_kinds_for(kind: BundleKind) -> frozenset[str]:
    """The citation kinds admitted by ``kind``. Exposed for tests and rendering."""

    return _CITATION_KINDS[kind]

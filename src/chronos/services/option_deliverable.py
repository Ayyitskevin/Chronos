"""Is this option's deliverable the standard 100 shares? (M11, R-27).

``OptionContract.deliverable_verified`` gates every option order through
``standard_deliverable_verified``, and until M11 it was set by exactly one
thing: ``DemoBroker``, by fiat. Neither IBKR adapter populated it, so the check
FAILed every option order against a real gateway — the option path was
fail-closed but entirely unproven outside demo.

Why this matters more than it sounds. The assignment math treats a short put's
obligation as ``strike x multiplier x contracts``. That is only true for a
**standard** contract. When OCC adjusts a series — a split that is not whole-
share, a spinoff, a merger, a special cash dividend — the deliverable becomes
something else: 150 shares, or shares plus cash, or another issuer's stock. Sell
a put on an adjusted series while assuming 100 shares and the cash reserved is
simply the wrong number, in the direction that leaves the account short at
assignment.

**What follows is a non-standard detector, not a deliverable reader.** The TWS
API does not expose OCC's deliverable schedule; no field on ``ContractDetails``
says how many shares of what a contract delivers. What it does expose is the OCC
root, and OCC's convention is that *any* deliverable change produces a **new root
with a numeric suffix** (``AAPL1``, ``SPY7``) while the original root keeps the
standard 100-share deliverable. So a root that still equals the symbol is
evidence of the *absence of an adjustment*, which under that convention implies
the standard deliverable.

That is an inference from a naming convention, not a read of the schedule, and
R-27 keeps it as a disclosed residual rather than claiming more than it has.

Every condition below is **necessary**, and they are conjunctive: any one of them
missing or contradicted refuses. Refusals carry reasons, because "your option was
blocked" without a why is the kind of message that gets a safety control disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: The deliverable a standard US equity option carries. Options with a different
#: multiplier (the defunct mini series carried 10) are a different instrument and
#: are refused rather than reasoned about.
STANDARD_DELIVERABLE_SHARES = Decimal("100")

#: OSI symbology: 6-character padded root, 6-digit date, 1 right, 8-digit strike.
_OSI_LENGTH = 21
_OSI_ROOT_WIDTH = 6


@dataclass(frozen=True, slots=True)
class DeliverableAssessment:
    """Whether the deliverable screen passed, and if not, every reason it did not.

    ``reasons`` is deliberately exhaustive rather than short-circuiting. An
    operator looking at a refused option wants the whole story, and a contract
    that fails three conditions is a different situation from one that fails a
    single borderline check.
    """

    standard: bool
    reasons: tuple[str, ...]


def assess_standard_deliverable(
    *,
    symbol: str,
    trading_class: str,
    local_symbol: str,
    multiplier: Decimal,
    underlying_con_id: int | None,
    underlying_symbol: str,
    underlying_security_type: str,
) -> DeliverableAssessment:
    """Screen one qualified option for evidence of a non-standard deliverable.

    Returns ``standard=True`` only when every necessary condition holds. The
    asymmetry is the same one that shapes the session gate: a spurious ``True``
    here lets an adjusted contract be sized as if it delivered 100 shares, while
    a spurious ``False`` blocks an order that could have gone — visible, and
    safe.
    """

    root = symbol.strip().upper()
    occ_root = trading_class.strip().upper()
    under_symbol = underlying_symbol.strip().upper()
    under_type = underlying_security_type.strip().upper()

    reasons: list[str] = []

    if not root:
        reasons.append("the option carries no symbol")

    # The broker must name the contract being delivered. Without it the option
    # is a claim on something unidentified, and `has_verified_standard_
    # deliverable` requires the identifier anyway.
    if underlying_con_id is None or underlying_con_id <= 0:
        reasons.append("the broker did not name the underlying contract")

    # Stock, not an index, future or anything cash-settled. A cash-settled
    # contract has no share deliverable at all, so the share math is not merely
    # imprecise there — it is meaningless.
    if not under_type:
        reasons.append("the broker did not say what the underlying is")
    elif under_type != "STK":
        reasons.append(f"the underlying is {under_type}, not a stock")

    if not under_symbol:
        reasons.append("the broker did not name the underlying symbol")
    elif root and under_symbol != root:
        reasons.append(f"the underlying is {under_symbol}, not the option root {root}")

    # The load-bearing condition. A suffixed OCC root is how an adjusted series
    # is marked, and it is the only signal in the API that speaks to the
    # deliverable at all.
    if not occ_root:
        reasons.append("the broker did not give an OCC root")
    elif root and occ_root != root:
        reasons.append(
            f"the OCC root is {occ_root}, not {root} — a suffixed root is how OCC marks a "
            "series whose deliverable was adjusted"
        )

    if multiplier != STANDARD_DELIVERABLE_SHARES:
        reasons.append(f"the multiplier is {multiplier}, not the standard 100")

    # Corroboration, and only ever in the rejecting direction. The OSI root
    # carries no information the trading class does not already carry, so its
    # *absence* is not evidence about the deliverable and is not held against
    # the contract — IBKR's local-symbol format has not been verified against a
    # live gateway (R-04), and refusing every option over an unparsed cosmetic
    # field would make this control inert in exactly the way R-25 and R-26 were.
    # A local symbol that parses and *disagrees* is different: that is IBKR's own
    # fields contradicting each other, and it refuses.
    osi_root = _osi_root(local_symbol.strip().upper())
    if osi_root is not None and occ_root and osi_root != occ_root:
        reasons.append(f"the local symbol's root {osi_root} contradicts the OCC root {occ_root}")

    return DeliverableAssessment(standard=not reasons, reasons=tuple(reasons))


def _osi_root(local_symbol: str) -> str | None:
    """The OCC root of a 21-character OSI local symbol, or ``None`` if it is not one.

    ``None`` means "this is not an OSI symbol", never "this symbol is wrong" —
    the caller must not treat the two as the same thing.
    """

    if len(local_symbol) != _OSI_LENGTH:
        return None
    root = local_symbol[:_OSI_ROOT_WIDTH].strip()
    tail = local_symbol[_OSI_ROOT_WIDTH:]
    if not root:
        return None
    if not tail[:6].isdigit() or tail[6] not in {"C", "P"} or not tail[7:].isdigit():
        return None
    return root

"""What the order plane actually did, in the supervisor's own vocabulary (A1).

Until this module the cycle asked one question of the handoff — *did it raise?* —
and treated every other answer as success. That was wrong in both directions at
once. The submission boundary **returns** its refusals
(``SubmissionOutcome(submitted=False, ...)``): a read-only lease, a kill switch
engaged between pre-submit and transmit, a mode that forbids submitting, an
unready reconciliation, an adapter that refused before any network send. Each of
those journaled as ``COMPLETE`` and consumed an activity attempt, so the journal
said "an order was submitted" about a cycle in which nothing left the process,
and the activity ceiling — a limit on how much the system may *attempt* — was
spent on attempts that never happened.

The journal is the only thing that can answer "why did it not trade". It was
answering falsely for every non-exception refusal, which is finding 5 of
``docs/VISION_COMPLETION_PLAN.md`` §6.

## Why the type lives here and not in the order plane

``chronos.supervisor`` must not import the order plane's result types
(``CycleOutcome.handoff`` is deliberately untyped for exactly that reason). So
the vocabulary is **owned by the supervisor** and the translation happens at the
app-plane seam, ``order_plane_handoff`` in ``chronos.api.autonomy_wiring``, which
is the one module allowed to hold both planes. The supervisor learns what
happened without learning what a ``SubmissionOutcome`` is.

## The four classes, and the counting rule stated once

An activity attempt is consumed **exactly when the supervisor cannot prove that
nothing reached the wire.**

| Disposition | Wire truth | Counts an attempt |
|---|---|---|
| :attr:`~HandoffDisposition.SUBMITTED` | the venue holds a working or filled order | yes |
| :attr:`~HandoffDisposition.REFUSED_NOT_SENT` | provably nothing left the process | **no** |
| :attr:`~HandoffDisposition.SENT_AMBIGUOUS` | bytes may have left; the state is unknown | yes |
| :attr:`~HandoffDisposition.REJECTED_AFTER_SEND` | the venue saw it and answered non-active | yes |

Both halves of that rule matter, and the old behavior had both backwards:

- **Not counting a not-sent refusal** is what makes the ceiling mean what it
  says. A backend sitting read-only would otherwise exhaust the day's opening
  budget without ever asking the venue for anything.
- **Counting an ambiguous send** is the fail-closed direction. Over-counting
  narrows the system's own authority; under-counting hands back budget that may
  already have been spent at the venue. When the wire state is unknown, the
  arithmetic must assume the order exists.

``SENT_AMBIGUOUS`` additionally raises a CRITICAL owner alert. Manual broker
resolution of ambiguous sends is an owner gate
(``docs/VISION_COMPLETION_PLAN.md`` §11) — a class of event the system is
explicitly not allowed to resolve for itself, so the one thing it must do is say
so out loud.

## An unrecognizable answer is ambiguous, never success

``run_cycle`` takes a plain callable, so a caller can hand back anything. A
result this module cannot read is recorded as ``SENT_AMBIGUOUS`` with its own
refusal code, because "the handoff did not tell us what happened" and "the order
is confirmed" are the two answers a fail-closed system must never merge. That is
also why the disposition, not the truthiness of some attribute, is what the
journal records: duck-typing ``getattr(result, "submitted", None)`` would read
``None`` — absent evidence — as ``False``, i.e. as a refusal, which is the
direction that silently un-counts a real send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self


class HandoffDisposition(StrEnum):
    """What the order plane did with a compiled intent, as the journal records it."""

    #: The boundary confirmed a working, partially filled, or filled order.
    SUBMITTED = "SUBMITTED"
    #: A gate refused before the single ``transmit=True`` site. Nothing was sent.
    REFUSED_NOT_SENT = "REFUSED_NOT_SENT"
    #: Bytes may have reached the venue and the resulting state is unconfirmed.
    SENT_AMBIGUOUS = "SENT_AMBIGUOUS"
    #: The venue acknowledged the send with a non-active lifecycle.
    REJECTED_AFTER_SEND = "REJECTED_AFTER_SEND"


#: Journal refusal codes. Additive: no existing refusal code changes meaning, and
#: ``ORDER_PLANE_REFUSED`` still names an exception out of the handoff callable.
REFUSED_NOT_SENT_CODE = "ORDER_PLANE_REFUSED_NOT_SENT"
SEND_AMBIGUOUS_CODE = "ORDER_PLANE_SEND_AMBIGUOUS"
REJECTED_AFTER_SEND_CODE = "ORDER_PLANE_REJECTED_AFTER_SEND"
#: The handoff returned something this module cannot classify.
UNTYPED_RESULT_CODE = "HANDOFF_RESULT_UNTYPED"
#: The submission call itself raised, so the wire state cannot be established
#: from outside the boundary. Distinct from ``ORDER_PLANE_REFUSED``, which the
#: cycle records for a raise from anywhere in the handoff.
SUBMIT_RAISED_CODE = "ORDER_PLANE_SUBMIT_RAISED"

#: The refusal code each class defaults to, pinned so a test can assert the
#: vocabulary rather than infer it.
DEFAULT_REFUSAL_CODES: dict[HandoffDisposition, str] = {
    HandoffDisposition.SUBMITTED: "",
    HandoffDisposition.REFUSED_NOT_SENT: REFUSED_NOT_SENT_CODE,
    HandoffDisposition.SENT_AMBIGUOUS: SEND_AMBIGUOUS_CODE,
    HandoffDisposition.REJECTED_AFTER_SEND: REJECTED_AFTER_SEND_CODE,
}

#: Dispositions that consume an ``orders_submitted`` activity attempt. The
#: module docstring is the argument; this is the enforcement.
COUNTS_ACTIVITY_ATTEMPT: frozenset[HandoffDisposition] = frozenset(
    {
        HandoffDisposition.SUBMITTED,
        HandoffDisposition.SENT_AMBIGUOUS,
        HandoffDisposition.REJECTED_AFTER_SEND,
    }
)

#: Dispositions the owner must be told about without having to look.
REQUIRES_OWNER_ALERT: frozenset[HandoffDisposition] = frozenset(
    {
        HandoffDisposition.SENT_AMBIGUOUS,
        HandoffDisposition.REJECTED_AFTER_SEND,
    }
)


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """One handoff, classified — plus the order plane's own words, verbatim.

    ``order_plane_code`` and ``detail`` are carried rather than interpreted: the
    order plane's gates are the authority on *why*, and paraphrasing them here
    would make this module a second, drifting copy of that vocabulary.
    """

    disposition: HandoffDisposition
    #: The order plane's own refusal code, as text. Empty when there is none.
    order_plane_code: str = ""
    detail: str = ""
    #: The supervisor-side journal code. Defaults per disposition; overridden for
    #: the untyped and submit-raised cases so the journal keeps them distinct.
    refusal_code: str = ""
    #: The raw object the handoff returned, unread. Kept so an app-plane caller
    #: can still reach the order plane's own result without the supervisor
    #: acquiring its type.
    raw: Any = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.refusal_code:
            object.__setattr__(self, "refusal_code", DEFAULT_REFUSAL_CODES[self.disposition])

    @property
    def counts_activity_attempt(self) -> bool:
        """Whether this outcome consumed an attempt under the activity ceiling."""

        return self.disposition in COUNTS_ACTIVITY_ATTEMPT

    @property
    def requires_owner_alert(self) -> bool:
        return self.disposition in REQUIRES_OWNER_ALERT

    @property
    def journal_detail(self) -> str:
        """The detail text the cycle records, naming the order plane's code."""

        if self.order_plane_code and self.detail:
            return f"the order plane answered {self.order_plane_code}: {self.detail}"
        if self.order_plane_code:
            return f"the order plane answered {self.order_plane_code}"
        return self.detail

    # -- constructors, one per class ---------------------------------------

    @classmethod
    def submitted(cls, *, order_plane_code: str = "", detail: str = "", raw: Any = None) -> Self:
        return cls(
            disposition=HandoffDisposition.SUBMITTED,
            order_plane_code=order_plane_code,
            detail=detail,
            raw=raw,
        )

    @classmethod
    def refused_not_sent(
        cls, *, order_plane_code: str = "", detail: str = "", raw: Any = None
    ) -> Self:
        return cls(
            disposition=HandoffDisposition.REFUSED_NOT_SENT,
            order_plane_code=order_plane_code,
            detail=detail,
            raw=raw,
        )

    @classmethod
    def sent_ambiguous(
        cls,
        *,
        order_plane_code: str = "",
        detail: str = "",
        refusal_code: str = "",
        raw: Any = None,
    ) -> Self:
        return cls(
            disposition=HandoffDisposition.SENT_AMBIGUOUS,
            order_plane_code=order_plane_code,
            detail=detail,
            refusal_code=refusal_code,
            raw=raw,
        )

    @classmethod
    def rejected_after_send(
        cls, *, order_plane_code: str = "", detail: str = "", raw: Any = None
    ) -> Self:
        return cls(
            disposition=HandoffDisposition.REJECTED_AFTER_SEND,
            order_plane_code=order_plane_code,
            detail=detail,
            raw=raw,
        )


def classify(result: Any) -> HandoffResult:
    """Read a handoff return value, or refuse to guess at it.

    A :class:`HandoffResult` passes through. Anything else — including ``None``,
    a bare string, or an order-plane object this plane cannot import — becomes
    ``SENT_AMBIGUOUS`` under :data:`UNTYPED_RESULT_CODE`. That is deliberately
    inconvenient for callers: the translation belongs at the app-plane seam, and
    a caller who has not written one is telling the supervisor nothing about
    whether an order exists.
    """

    if isinstance(result, HandoffResult):
        return result
    return HandoffResult.sent_ambiguous(
        detail=(
            "the handoff returned "
            f"{type(result).__name__}, which is not a typed HandoffResult; whether "
            "anything reached the venue is unknown and is treated as possibly sent"
        ),
        refusal_code=UNTYPED_RESULT_CODE,
        raw=result,
    )

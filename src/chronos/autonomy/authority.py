"""Refuse-closed worker and paper/live authority. Chronos does not call a model.

A real worker is an owner-run process against the existing JSON contract.
Paper and live remain owner gates. This module records those refusals so a
later slice cannot treat the shadow journal as promotion.
"""

from __future__ import annotations

from chronos.autonomy.worker_protocol import REFERENCE_WORKER_PINS, WorkerIdentityPins

EXTERNAL_WORKER_STATUS = "pending_owner_pins"
PAPER_AUTHORITY = "none"
LIVE_AUTHORITY = "none"


class AuthorityError(ValueError):
    """An external worker, paper, or live path was requested before it is lawful."""


def require_external_worker(
    pins: WorkerIdentityPins | None = None,
) -> None:
    """Refuse every attempt to treat an unpinned worker as Chronos-called."""

    if pins is None or pins == REFERENCE_WORKER_PINS:
        raise AuthorityError(
            "external worker stays pending_owner_pins; Chronos does not call a "
            "model. Use the deterministic reference worker for SHADOW, or supply "
            "Chronos-owned pins from outside this process. Workers may not "
            "self-attest provenance or decision_id"
        )
    raise AuthorityError(
        "external worker pins are recognized but this slice does not invoke a "
        "model, open paper, or stamp a promotion artifact"
    )


def require_paper_authority() -> None:
    """Refuse paper. The shadow journal is not supervised paper."""

    raise AuthorityError(
        "paper authority is none; a shadow journal is not paper and does not "
        "admit, size, compile, or transmit"
    )


def require_live_authority() -> None:
    """Refuse live and canary. Live is not on the table in this slice."""

    raise AuthorityError(
        "live authority is none; certified overlapping bytes, an untouched "
        "holdout, and an owner gate are required before any live path"
    )

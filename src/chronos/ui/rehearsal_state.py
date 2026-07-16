"""Process-memory-only disposition for a DEMO approval-rehearsal receipt.

The display lease in this module is deliberately unrelated to market-evidence
freshness.  It only bounds how long Streamlit retains a historical, scalar M8
receipt.  No state here can authorize, persist, or submit an order.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from chronos.domain.models import ChronosModel
from chronos.services.short_put_demo_approval import ShortPutDemoApprovalReceipt

DEFAULT_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS = 15 * 60.0
MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS = 60 * 60.0

_APPROVAL_REFERENCE_PATTERN = re.compile(r"^CHR-APR-[A-F0-9]{32}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,32}$")

MonotonicSeconds = Annotated[
    float,
    Field(strict=True, ge=0, allow_inf_nan=False),
]


class DemoApprovalDisposition(StrEnum):
    """Presentation-only receipt state, intentionally outside OrderLifecycle."""

    RETAINED = "RETAINED"
    EXPIRED = "EXPIRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class DemoApprovalDispositionReason(StrEnum):
    """Bounded reasons that cannot carry broker or account text."""

    DISPLAY_LEASE_EXPIRED = "DISPLAY_LEASE_EXPIRED"
    MONOTONIC_CLOCK_REGRESSION = "MONOTONIC_CLOCK_REGRESSION"
    OPERATOR_ABANDONED = "OPERATOR_ABANDONED"
    NEW_EVIDENCE_ATTEMPT = "NEW_EVIDENCE_ATTEMPT"


class DemoApprovalSessionRecord(ChronosModel):
    """One active scalar receipt or a term-free terminal tombstone."""

    approval_reference: str
    symbol: str
    receipt: ShortPutDemoApprovalReceipt | None
    disposition: DemoApprovalDisposition
    retained_at_monotonic: MonotonicSeconds
    last_observed_at_monotonic: MonotonicSeconds
    display_expires_at_monotonic: MonotonicSeconds
    disposed_at_monotonic: MonotonicSeconds | None = None
    disposition_reason: DemoApprovalDispositionReason | None = None
    authority_created: Literal[False] = False
    persistence_recorded: Literal[False] = False
    actions_locked: Literal[True] = True

    @field_validator("approval_reference")
    @classmethod
    def validate_approval_reference(cls, value: str) -> str:
        if _APPROVAL_REFERENCE_PATTERN.fullmatch(value) is None:
            raise ValueError("approval_reference is not a canonical rehearsal reference")
        return value

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if _SYMBOL_PATTERN.fullmatch(value) is None:
            raise ValueError("symbol must be a canonical code")
        return value

    @field_validator("receipt")
    @classmethod
    def validate_receipt(
        cls,
        value: ShortPutDemoApprovalReceipt | None,
    ) -> ShortPutDemoApprovalReceipt | None:
        if value is None:
            return None
        return _validated_receipt(value)

    @model_validator(mode="after")
    def validate_disposition(self) -> DemoApprovalSessionRecord:
        if self.display_expires_at_monotonic <= self.retained_at_monotonic:
            raise ValueError("display expiry must follow receipt retention")
        if (
            self.display_expires_at_monotonic - self.retained_at_monotonic
            > MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS
        ):
            raise ValueError("display lease exceeds the supported maximum")
        if not (
            self.retained_at_monotonic
            <= self.last_observed_at_monotonic
            <= self.display_expires_at_monotonic
        ):
            raise ValueError("last observation must remain inside the display lease")
        if self.disposition is DemoApprovalDisposition.RETAINED:
            if (
                self.receipt is None
                or self.disposed_at_monotonic is not None
                or self.disposition_reason is not None
                or self.last_observed_at_monotonic >= self.display_expires_at_monotonic
                or self.receipt.approval_reference != self.approval_reference
                or self.receipt.symbol != self.symbol
            ):
                raise ValueError("a retained record requires one matching scalar receipt")
            return self

        if (
            self.receipt is not None
            or self.disposed_at_monotonic is None
            or self.disposition_reason is None
            or self.last_observed_at_monotonic != self.disposed_at_monotonic
        ):
            raise ValueError("a terminal record must be a term-free disposition tombstone")

        disposed_at = self.disposed_at_monotonic
        if self.disposition is DemoApprovalDisposition.EXPIRED:
            if self.disposition_reason is DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED:
                if disposed_at != self.display_expires_at_monotonic:
                    raise ValueError("lease expiry must occur at the exact display deadline")
            elif (
                self.disposition_reason is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
            ):
                if disposed_at >= self.display_expires_at_monotonic:
                    raise ValueError("clock regression must precede display expiry")
            else:
                raise ValueError("expired records require a fail-closed expiry reason")
        elif self.disposition is DemoApprovalDisposition.ABANDONED:
            if (
                self.disposition_reason is not DemoApprovalDispositionReason.OPERATOR_ABANDONED
                or not self.retained_at_monotonic <= disposed_at < self.display_expires_at_monotonic
            ):
                raise ValueError("abandonment must precede display expiry")
        elif self.disposition is DemoApprovalDisposition.SUPERSEDED:
            if (
                self.disposition_reason is not DemoApprovalDispositionReason.NEW_EVIDENCE_ATTEMPT
                or not self.retained_at_monotonic <= disposed_at < self.display_expires_at_monotonic
            ):
                raise ValueError("supersession must precede display expiry")
        else:  # pragma: no cover - exhaustive guard for future enum additions
            raise ValueError("unsupported DEMO approval disposition")
        return self


def retain_demo_approval_receipt(
    receipt: ShortPutDemoApprovalReceipt,
    *,
    now_monotonic: float,
    retention_seconds: float = DEFAULT_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
    previous: DemoApprovalSessionRecord | None = None,
) -> DemoApprovalSessionRecord:
    """Retain one fresh scalar receipt without extending an exact replay's lease."""

    receipt = _validated_receipt(receipt)
    now = _monotonic_seconds(now_monotonic, label="now_monotonic")
    retention = _retention_seconds(retention_seconds)

    if previous is not None:
        previous = refresh_demo_approval_record(previous, now_monotonic=now)
        previous_clock_floor = (
            previous.disposed_at_monotonic
            if previous.disposed_at_monotonic is not None
            else previous.last_observed_at_monotonic
        )
        if (
            now < previous_clock_floor
            or previous.disposition_reason
            is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
        ):
            return previous
        if previous.approval_reference == receipt.approval_reference:
            if previous.receipt is not None and previous.receipt != receipt:
                raise ValueError("approval reference was replayed with conflicting receipt terms")
            return previous
        if previous.disposition is DemoApprovalDisposition.RETAINED:
            raise ValueError("an active receipt must be terminalized before retaining a new one")

    expires_at = now + retention
    if not math.isfinite(expires_at):
        raise ValueError("display expiry is outside the supported monotonic range")
    return DemoApprovalSessionRecord(
        approval_reference=receipt.approval_reference,
        symbol=receipt.symbol,
        receipt=receipt,
        disposition=DemoApprovalDisposition.RETAINED,
        retained_at_monotonic=now,
        last_observed_at_monotonic=now,
        display_expires_at_monotonic=expires_at,
    )


def refresh_demo_approval_record(
    record: DemoApprovalSessionRecord,
    *,
    now_monotonic: float,
) -> DemoApprovalSessionRecord:
    """Apply display expiry; a regressed monotonic clock fails closed."""

    record = _validated_record(record)
    now = _monotonic_seconds(now_monotonic, label="now_monotonic")
    if record.disposition is not DemoApprovalDisposition.RETAINED:
        return record
    if now < record.last_observed_at_monotonic:
        return _terminal_record(
            record,
            disposition=DemoApprovalDisposition.EXPIRED,
            disposed_at=record.last_observed_at_monotonic,
            reason=DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION,
        )
    if now >= record.display_expires_at_monotonic:
        return _terminal_record(
            record,
            disposition=DemoApprovalDisposition.EXPIRED,
            disposed_at=record.display_expires_at_monotonic,
            reason=DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED,
        )
    if now == record.last_observed_at_monotonic:
        return record
    receipt = record.receipt
    assert receipt is not None  # guaranteed by the validated retained-record invariant
    return DemoApprovalSessionRecord(
        approval_reference=record.approval_reference,
        symbol=record.symbol,
        receipt=receipt,
        disposition=DemoApprovalDisposition.RETAINED,
        retained_at_monotonic=record.retained_at_monotonic,
        last_observed_at_monotonic=now,
        display_expires_at_monotonic=record.display_expires_at_monotonic,
    )


def abandon_demo_approval_record(
    record: DemoApprovalSessionRecord,
    *,
    now_monotonic: float,
) -> DemoApprovalSessionRecord:
    """Apply an explicit operator abandonment without broker or persistence work."""

    return _requested_terminal_record(
        record,
        now_monotonic=now_monotonic,
        disposition=DemoApprovalDisposition.ABANDONED,
        reason=DemoApprovalDispositionReason.OPERATOR_ABANDONED,
    )


def supersede_demo_approval_record(
    record: DemoApprovalSessionRecord,
    *,
    now_monotonic: float,
) -> DemoApprovalSessionRecord:
    """Invalidate retained terms before a newer evidence attempt begins."""

    return _requested_terminal_record(
        record,
        now_monotonic=now_monotonic,
        disposition=DemoApprovalDisposition.SUPERSEDED,
        reason=DemoApprovalDispositionReason.NEW_EVIDENCE_ATTEMPT,
    )


def _requested_terminal_record(
    record: DemoApprovalSessionRecord,
    *,
    now_monotonic: float,
    disposition: DemoApprovalDisposition,
    reason: DemoApprovalDispositionReason,
) -> DemoApprovalSessionRecord:
    now = _monotonic_seconds(now_monotonic, label="now_monotonic")
    refreshed = refresh_demo_approval_record(record, now_monotonic=now)
    if refreshed.disposition is not DemoApprovalDisposition.RETAINED:
        return refreshed
    return _terminal_record(
        refreshed,
        disposition=disposition,
        disposed_at=now,
        reason=reason,
    )


def _terminal_record(
    record: DemoApprovalSessionRecord,
    *,
    disposition: DemoApprovalDisposition,
    disposed_at: float,
    reason: DemoApprovalDispositionReason,
) -> DemoApprovalSessionRecord:
    return DemoApprovalSessionRecord(
        approval_reference=record.approval_reference,
        symbol=record.symbol,
        receipt=None,
        disposition=disposition,
        retained_at_monotonic=record.retained_at_monotonic,
        last_observed_at_monotonic=disposed_at,
        display_expires_at_monotonic=record.display_expires_at_monotonic,
        disposed_at_monotonic=disposed_at,
        disposition_reason=reason,
    )


def _monotonic_seconds(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite nonnegative monotonic seconds")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be finite nonnegative monotonic seconds") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{label} must be finite nonnegative monotonic seconds")
    return normalized


def _retention_seconds(value: object) -> float:
    retention = _monotonic_seconds(value, label="retention_seconds")
    if retention <= 0 or retention > MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS:
        raise ValueError(
            "retention_seconds must be positive and no greater than the supported maximum"
        )
    return retention


def _validated_receipt(value: object) -> ShortPutDemoApprovalReceipt:
    if type(value) is not ShortPutDemoApprovalReceipt:
        raise ValueError("receipt must be an exact scalar DEMO approval receipt")
    try:
        return ShortPutDemoApprovalReceipt.model_validate(
            value.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("receipt must be a valid scalar DEMO approval receipt") from error


def _validated_record(value: object) -> DemoApprovalSessionRecord:
    if type(value) is not DemoApprovalSessionRecord:
        raise ValueError("record must be an exact immutable DEMO approval session record")
    try:
        return DemoApprovalSessionRecord.model_validate(
            value.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("record must be a valid immutable DEMO approval session record") from error

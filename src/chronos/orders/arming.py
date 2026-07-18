"""Live-arming: a short-lived, operator-typed authorization to trade live.

Milestone 6 builds the live safety layer; it does NOT yet enable live
transmission (that is Milestone 7). Arming is deliberately **backend memory**:
the arm token lives only in the running backend process, so a restart clears
it and re-arming is required. It carries a TTL and can be explicitly revoked.

Security: the raw typed phrase is compared in constant time and is NEVER
logged, persisted, or echoed — only the audit *events* (armed / expired /
revoked) reach ``live_arm_events`` via the injected sink, with no phrase in
them. This mirrors the spec rule "never log raw live-arm phrases".
"""

from __future__ import annotations

import hmac
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from chronos.domain.models import ChronosModel

# The exact phrase the operator must type to arm live trading. It is a fixed
# constant (not a setting) so it never lands in a logged/serialized config.
REQUIRED_ARM_PHRASE = "I ACCEPT LIVE TRADING RISK"


class ArmState(ChronosModel):
    armed: bool
    expires_at: datetime | None = None
    reason: str = ""


class LiveArmAuditSink(Protocol):
    """Records arm lifecycle events for audit (never the raw phrase)."""

    def record(self, *, event: str, reason: str, occurred_at: datetime) -> None: ...


class NullArmAuditSink:
    def record(self, *, event: str, reason: str, occurred_at: datetime) -> None:
        del event, reason, occurred_at


@dataclass(slots=True)
class _Arm:
    expires_at: datetime
    reason: str


class LiveArmingError(RuntimeError):
    """The supplied arm phrase did not match the required phrase."""


class LiveArmingService:
    """In-process live-arming with TTL and explicit revocation."""

    def __init__(
        self,
        *,
        ttl_minutes: int,
        audit: LiveArmAuditSink | None = None,
    ) -> None:
        if ttl_minutes <= 0:
            raise ValueError("live arm TTL must be positive")
        self._ttl = timedelta(minutes=ttl_minutes)
        self._audit = audit or NullArmAuditSink()
        self._arm: _Arm | None = None
        # Sync FastAPI routes run in a threadpool; concurrent arm/is_armed/revoke
        # would otherwise race on _arm (e.g. double-recording a lazy expiry).
        self._lock = threading.Lock()

    @staticmethod
    def _require_aware(now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("live-arming timestamps must be timezone-aware")

    def arm(self, typed_phrase: str, *, now: datetime, reason: str = "operator arm") -> ArmState:
        self._require_aware(now)
        # Compare as bytes so a non-ASCII phrase is a clean mismatch rather than
        # a TypeError (hmac.compare_digest rejects non-ASCII str); the raw phrase
        # is never stored, logged, or echoed.
        if not hmac.compare_digest(
            typed_phrase.encode("utf-8"), REQUIRED_ARM_PHRASE.encode("utf-8")
        ):
            raise LiveArmingError("live arm phrase did not match the required phrase")
        with self._lock:
            expires_at = now + self._ttl
            self._arm = _Arm(expires_at=expires_at, reason=reason)
        self._audit.record(event="armed", reason=reason, occurred_at=now)
        return ArmState(armed=True, expires_at=expires_at, reason=reason)

    def is_armed(self, *, now: datetime) -> bool:
        self._require_aware(now)
        with self._lock:
            arm = self._arm
            if arm is None:
                return False
            if now >= arm.expires_at:
                # Lazily expire: clear the token and record the expiry once.
                self._arm = None
                expired_reason = arm.reason
            else:
                return True
        self._audit.record(event="expired", reason=expired_reason, occurred_at=now)
        return False

    def revoke(self, *, now: datetime, reason: str = "operator revoke") -> ArmState:
        self._require_aware(now)
        with self._lock:
            had_arm = self._arm is not None
            self._arm = None
        if had_arm:
            self._audit.record(event="revoked", reason=reason, occurred_at=now)
        return ArmState(armed=False, reason=reason)

    def state(self, *, now: datetime) -> ArmState:
        if self.is_armed(now=now):
            with self._lock:
                arm = self._arm
            if arm is not None:
                return ArmState(armed=True, expires_at=arm.expires_at, reason=arm.reason)
        return ArmState(armed=False)

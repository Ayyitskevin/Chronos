"""Durable audit sinks for the live safety layer.

These implement the arm/kill-switch audit-sink protocols by appending to the
``live_arm_events`` / ``kill_switch_events`` tables. They record only the
lifecycle *events* — never the raw arm phrase, never a raw broker account id
(the pseudonymous fingerprint is bound at construction).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from chronos.persistence.repositories import (
    _reject_raw_account_event_data,
    _require_scope,
)
from chronos.persistence.schema import KillSwitchEventRow, LiveArmEventRow


class LiveArmEventRepository:
    """Audit sink for live-arm lifecycle events (armed / expired / revoked)."""

    def __init__(self, sessions: sessionmaker[Session], *, account_fingerprint: str) -> None:
        self._sessions = sessions
        self._fingerprint = account_fingerprint

    def record(self, *, event: str, reason: str, occurred_at: datetime) -> None:
        # Defense-in-depth: the reason is free text; never let a raw broker
        # account id land in the audit trail (only the fingerprint may).
        _reject_raw_account_event_data(persisted_values=(event, reason))
        with self._sessions.begin() as session:
            _require_scope(session)
            session.add(
                LiveArmEventRow(
                    event=event,
                    account_fingerprint=self._fingerprint,
                    reason=reason,
                    occurred_at=occurred_at,
                )
            )


class KillSwitchEventRepository:
    """Audit sink for kill-switch engage/disengage actions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def record(
        self,
        *,
        action: str,
        initiated_by: str,
        detail: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        # Defense-in-depth: the operator-supplied reason/note in detail is free
        # text; never let a raw broker account id land in the audit trail.
        _reject_raw_account_event_data(persisted_values=(action, initiated_by, *detail.values()))
        with self._sessions.begin() as session:
            _require_scope(session)
            session.add(
                KillSwitchEventRow(
                    action=action,
                    initiated_by=initiated_by,
                    detail=dict(detail),
                    occurred_at=occurred_at,
                )
            )

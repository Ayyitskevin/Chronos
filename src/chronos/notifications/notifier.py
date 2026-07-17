"""Notifier implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger("chronos.notifications")


class NotificationKind(StrEnum):
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    MODE_CHANGE = "MODE_CHANGE"
    BROKER_CONNECTED = "BROKER_CONNECTED"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    SIGNAL = "SIGNAL"
    RISK_REJECTION = "RISK_REJECTION"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCELLATION = "CANCELLATION"
    REJECTION = "REJECTION"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    LOSS_LIMIT = "LOSS_LIMIT"
    KILL_SWITCH = "KILL_SWITCH"
    CRITICAL_EXCEPTION = "CRITICAL_EXCEPTION"


@dataclass(frozen=True, slots=True)
class Notification:
    kind: NotificationKind
    summary: str
    detail: str
    at_utc: datetime


@runtime_checkable
class Notifier(Protocol):
    def send(self, notification: Notification) -> bool:
        """Deliver one notification; return False on failure. Must not raise."""
        ...


class ConsoleNotifier:
    """Logs notifications; always 'succeeds' unless logging itself fails."""

    def send(self, notification: Notification) -> bool:
        try:
            logger.info(
                "[%s] %s — %s", notification.kind.value, notification.summary, notification.detail
            )
            return True
        except Exception:  # pragma: no cover - logging failure is best-effort
            return False


@dataclass
class SafeNotifierFanout:
    """Delivers to all notifiers, isolating every failure."""

    notifiers: tuple[Notifier, ...]
    failure_count: int = 0
    _log: list[Notification] = field(default_factory=list)

    def notify(self, kind: NotificationKind, summary: str, detail: str = "") -> None:
        notification = Notification(
            kind=kind, summary=summary, detail=detail, at_utc=datetime.now(tz=UTC)
        )
        self._log.append(notification)
        for notifier in self.notifiers:
            try:
                if not notifier.send(notification):
                    self.failure_count += 1
            except Exception:
                self.failure_count += 1

    @property
    def history(self) -> tuple[Notification, ...]:
        return tuple(self._log)

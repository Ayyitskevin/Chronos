"""Persistent halt (kill-switch) state (ADR-0007).

The halt state lives in a JSON file whose path is part of the platform
configuration. Design rules:

- Any component may raise a halt; only an explicit operator rearm clears it.
- Restarting the process must re-load, never reset, the halt file.
- A missing, unreadable, or corrupt halt file counts as HALTED (fail closed):
  the absence of provable "not halted" evidence is not permission to trade.
  A brand-new deployment therefore starts halted until the operator arms it
  once, which is intentional.
- Writes are atomic (temp file + rename) so a crash cannot leave a torn file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class HaltReason(StrEnum):
    OPERATOR_REQUEST = "OPERATOR_REQUEST"
    NEVER_ARMED = "NEVER_ARMED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    POSITION_LIMIT_BREACH = "POSITION_LIMIT_BREACH"
    EXPOSURE_LIMIT_BREACH = "EXPOSURE_LIMIT_BREACH"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    REPEATED_ORDER_REJECTION = "REPEATED_ORDER_REJECTION"
    REPEATED_BROKER_ERROR = "REPEATED_BROKER_ERROR"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    CONNECTION_UNSTABLE = "CONNECTION_UNSTABLE"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    AUDIT_LOG_FAILURE = "AUDIT_LOG_FAILURE"
    STRATEGY_EXCEPTION = "STRATEGY_EXCEPTION"
    RISK_ENGINE_EXCEPTION = "RISK_ENGINE_EXCEPTION"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    MISSING_ACCOUNT_STATE = "MISSING_ACCOUNT_STATE"
    UNEXPECTED_ACCOUNT = "UNEXPECTED_ACCOUNT"
    LIVE_ACCOUNT_DETECTED = "LIVE_ACCOUNT_DETECTED"
    DUPLICATE_ORDER_INTENT = "DUPLICATE_ORDER_INTENT"
    STATE_CORRUPTION = "STATE_CORRUPTION"


@dataclass(frozen=True, slots=True)
class HaltState:
    halted: bool
    reason: HaltReason | None
    detail: str
    raised_at_utc: datetime | None
    rearm_note: str

    @property
    def trading_blocked(self) -> bool:
        return self.halted


_SCHEMA_VERSION = 1


class HaltStore:
    """Durable halt state with fail-closed reads and atomic writes."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> HaltState:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if payload.get("schema") != _SCHEMA_VERSION:
                raise ValueError(f"unsupported halt schema {payload.get('schema')!r}")
            halted = bool(payload["halted"])
            reason_raw = payload.get("reason")
            reason = HaltReason(reason_raw) if reason_raw else None
            raised_raw = payload.get("raised_at_utc")
            raised = datetime.fromisoformat(raised_raw) if raised_raw else None
            if raised is not None and raised.tzinfo is None:
                raise ValueError("halt timestamp must be timezone-aware")
            if halted and reason is None:
                raise ValueError("halted state requires a reason")
            return HaltState(
                halted=halted,
                reason=reason,
                detail=str(payload.get("detail", "")),
                raised_at_utc=raised,
                rearm_note=str(payload.get("rearm_note", "")),
            )
        except FileNotFoundError:
            return HaltState(
                halted=True,
                reason=HaltReason.NEVER_ARMED,
                detail="no halt file exists; a new deployment starts halted until first rearm",
                raised_at_utc=None,
                rearm_note="",
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            return HaltState(
                halted=True,
                reason=HaltReason.STATE_CORRUPTION,
                detail=f"halt file unreadable; failing closed: {error}",
                raised_at_utc=None,
                rearm_note="",
            )

    def halt(self, reason: HaltReason, detail: str) -> HaltState:
        state = HaltState(
            halted=True,
            reason=reason,
            detail=detail,
            raised_at_utc=datetime.now(tz=UTC),
            rearm_note="",
        )
        self._write(state)
        return state

    def rearm(self, operator_note: str) -> HaltState:
        """Clear the halt. Requires a non-empty operator note for the audit trail."""

        note = operator_note.strip()
        if not note:
            raise ValueError("rearm requires a non-empty operator note")
        state = HaltState(
            halted=False,
            reason=None,
            detail="",
            raised_at_utc=None,
            rearm_note=note,
        )
        self._write(state)
        return state

    def _write(self, state: HaltState) -> None:
        payload = {
            "schema": _SCHEMA_VERSION,
            "halted": state.halted,
            "reason": state.reason.value if state.reason else None,
            "detail": state.detail,
            "raised_at_utc": (
                state.raised_at_utc.isoformat() if state.raised_at_utc is not None else None
            ),
            "rearm_note": state.rearm_note,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, self._path)

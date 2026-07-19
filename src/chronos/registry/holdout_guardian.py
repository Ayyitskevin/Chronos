"""The holdout guardian (ADR-0013 §3/§4, hardened by the C2 two-reviewer review).

Consuming a holdout requires an explicit, **owner-typed, single-use, logged** unlock,
and a consumed window is **burned** in the anchor-verified, hash-chained ledger — so the
M5 "burned holdout silently reused as fresh" failure is caught rather than silent.

The owner-typed guarantee follows the codebase's `orders.arming` doctrine: a
module-constant phrase (never a setting, never serialized), validated with
``hmac.compare_digest`` and never stored/logged/echoed. "No shipped automated path
invokes it" is guarded by ``tests/safety/test_registry_no_automated_unlock.py`` and
``test_single_unmask_site.py`` (accidental-wiring guards; a determined runtime-reflection
evasion is out of scope and disclosed in ADR-0013 §7 / limitations).

Review hardening:
- **verify-before-trust (F1/F2/safety-1):** every trust of burned/consumed state runs
  ``ledger.verify()`` (chain + head anchor) first and fails closed on a broken/truncated
  ledger.
- **concurrency (safety-2):** the read-verify-append critical section holds an exclusive
  OS file lock, so two processes cannot both consume one grant.
- **expiry (both reviewers):** ``now`` must be timezone-aware and expiry is compared as
  ``datetime`` objects, not ISO strings.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from chronos.auditlog.log import AuditRecord
from chronos.histdata.holdout import HoldoutWindow, embargoed_view, load_holdouts
from chronos.histdata.store import read_bars
from chronos.marketdata.bars import BarSeries
from chronos.registry.budget import available_budget
from chronos.registry.ledger import KIND_CONSUME, KIND_UNLOCK, RegistryLedger, registry_lock

# Module constant — never a setting, so it can never land in a serialized/logged config.
REQUIRED_HOLDOUT_UNLOCK_PHRASE = "I ACCEPT BURNING THIS HOLDOUT"


class HoldoutGuardianError(RuntimeError):
    """A holdout unlock or mediated read was refused (fails closed)."""


@dataclass(frozen=True, slots=True)
class UnlockGrant:
    unlock_id: str
    window: str
    expires_at: str  # ISO-8601


def _phrase_ok(typed_phrase: str) -> bool:
    return hmac.compare_digest(
        typed_phrase.encode("utf-8"), REQUIRED_HOLDOUT_UNLOCK_PHRASE.encode("utf-8")
    )


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise HoldoutGuardianError("`now` must be timezone-aware (UTC)")


def _require_verified(ledger: RegistryLedger) -> None:
    ok, detail = ledger.verify()
    if not ok:
        raise HoldoutGuardianError(f"registry ledger failed verification: {detail}")


def _expiry(record: AuditRecord) -> datetime | None:
    value = record.payload.get("expires_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def burned_windows(ledger: RegistryLedger) -> frozenset[str]:
    """Windows with a recorded consume — spent."""

    return frozenset(str(r.payload["window"]) for r in ledger.records_of(KIND_CONSUME))


def is_burned(ledger: RegistryLedger, window: str) -> bool:
    return window in burned_windows(ledger)


def _consumed_unlock_ids(ledger: RegistryLedger) -> frozenset[str]:
    return frozenset(str(r.payload["unlock_id"]) for r in ledger.records_of(KIND_CONSUME))


def _unlock_record(ledger: RegistryLedger, unlock_id: str) -> AuditRecord | None:
    for record in ledger.records_of(KIND_UNLOCK):
        if str(record.payload.get("unlock_id")) == unlock_id:
            return record
    return None


def _window_by_name(windows: tuple[HoldoutWindow, ...], name: str) -> HoldoutWindow | None:
    return next((w for w in windows if w.name == name), None)


def _has_outstanding_grant(ledger: RegistryLedger, window: str, now: datetime) -> bool:
    consumed = _consumed_unlock_ids(ledger)
    for record in ledger.records_of(KIND_UNLOCK):
        if str(record.payload.get("window")) != window:
            continue
        if str(record.payload.get("unlock_id")) in consumed:
            continue
        expiry = _expiry(record)
        if expiry is not None and now < expiry:
            return True
    return False


def request_unlock(
    ledger: RegistryLedger,
    history_root: Path,
    window_name: str,
    *,
    typed_phrase: str,
    reason: str,
    now: datetime,
    accrued_sessions: int,
    ttl_minutes: int,
    sessions_per_unlock: int,
    max_outstanding_unlocks: int,
) -> UnlockGrant:
    """Grant a single-use holdout unlock; fails closed on any precondition."""

    _require_aware(now)
    if not _phrase_ok(typed_phrase):
        raise HoldoutGuardianError("holdout unlock phrase mismatch")  # never echo the phrase
    if not reason.strip():
        raise HoldoutGuardianError("an unlock reason is required")
    with registry_lock(ledger.path):
        _require_verified(ledger)
        windows = load_holdouts(history_root)
        if _window_by_name(windows, window_name) is None:
            raise HoldoutGuardianError(f"holdout window {window_name!r} is not declared")
        if is_burned(ledger, window_name):
            raise HoldoutGuardianError(
                f"holdout window {window_name!r} is already burned; it cannot be re-unlocked"
            )
        if _has_outstanding_grant(ledger, window_name, now):
            raise HoldoutGuardianError(
                f"holdout window {window_name!r} already has an outstanding unlock grant"
            )
        if (
            available_budget(
                ledger,
                now=now,
                accrued_sessions=accrued_sessions,
                sessions_per_unlock=sessions_per_unlock,
                max_outstanding_unlocks=max_outstanding_unlocks,
            )
            <= 0
        ):
            raise HoldoutGuardianError("no holdout unlock budget; more data must accrue")

        unlock_id = secrets.token_hex(16)
        expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        ledger.append(
            KIND_UNLOCK,
            {
                "unlock_id": unlock_id,
                "window": window_name,
                "reason": reason,
                "expires_at": expires_at,
            },
        )
    return UnlockGrant(unlock_id=unlock_id, window=window_name, expires_at=expires_at)


def mediated_holdout_read(
    ledger: RegistryLedger,
    history_root: Path,
    symbol: str,
    *,
    grant: UnlockGrant,
    now: datetime,
) -> BarSeries:
    """The only sanctioned unmasking read; records the burn *before* returning data."""

    _require_aware(now)
    with registry_lock(ledger.path):
        _require_verified(ledger)
        record = _unlock_record(ledger, grant.unlock_id)
        if record is None:
            raise HoldoutGuardianError("unknown unlock grant")
        if grant.unlock_id in _consumed_unlock_ids(ledger):
            raise HoldoutGuardianError("unlock grant already consumed (single-use)")
        if is_burned(ledger, grant.window):
            raise HoldoutGuardianError(f"holdout window {grant.window!r} is already burned")
        expiry = _expiry(record)
        if expiry is None or now >= expiry:
            raise HoldoutGuardianError("unlock grant expired")
        windows = load_holdouts(history_root)
        window = _window_by_name(windows, grant.window)
        if window is None:
            raise HoldoutGuardianError(f"holdout window {grant.window!r} is no longer declared")
        if not window.applies_to(symbol):
            raise HoldoutGuardianError(f"holdout window {grant.window!r} does not cover {symbol}")

        # Record the consume (burning the window) BEFORE unmasking, so a burn is durable
        # even if the read is interrupted — the fail-safe direction.
        ledger.append(
            KIND_CONSUME,
            {"unlock_id": grant.unlock_id, "window": grant.window, "symbol": symbol},
        )
    series = read_bars(history_root, symbol)
    return embargoed_view(series, windows, symbol, unlocked=True)

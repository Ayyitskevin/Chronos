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
- **verify-before-trust (F1/F2/safety-1):** every trust of burned/consumed state derives
  from one exact chain-and-anchor-verified snapshot and fails closed on a broken or
  truncated ledger.
- **concurrency (safety-2):** the read-verify-append critical section holds an exclusive
  OS file lock, so two processes cannot both consume one grant.
- **expiry (both reviewers):** ``now`` must be timezone-aware and expiry is compared as
  ``datetime`` objects, not ISO strings.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from chronos.auditlog.log import AuditLogCorruptionError, AuditRecord
from chronos.histdata.holdout import (
    HoldoutWindow,
    _embargoed_view_with_window_unlocked,
    load_holdouts,
)
from chronos.histdata.store import bars_path
from chronos.marketdata.bars import BarInterval, BarSeries
from chronos.marketdata.csv_provider import load_daily_csv_bytes
from chronos.registry.budget import available_budget
from chronos.registry.ledger import (
    KIND_CONSUME,
    KIND_UNLOCK,
    RegistryIntegrityError,
    RegistryLedger,
    verified_registry_transaction,
)

# Module constant — never a setting, so it can never land in a serialized/logged config.
REQUIRED_HOLDOUT_UNLOCK_PHRASE = "I ACCEPT BURNING THIS HOLDOUT"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)


class HoldoutGuardianError(RuntimeError):
    """A holdout unlock or mediated read was refused (fails closed)."""


@dataclass(frozen=True, slots=True)
class UnlockGrant:
    unlock_id: str
    window: str
    expires_at: str  # ISO-8601


@dataclass(frozen=True, slots=True)
class _WindowBinding:
    window: HoldoutWindow
    definition: dict[str, object]
    definition_sha256: str
    scope_sha256: str
    data_identity_sha256: str
    holdout_set_sha256: str


def _phrase_ok(typed_phrase: str) -> bool:
    return hmac.compare_digest(
        typed_phrase.encode("utf-8"), REQUIRED_HOLDOUT_UNLOCK_PHRASE.encode("utf-8")
    )


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise HoldoutGuardianError("`now` must be timezone-aware (UTC)")


@contextmanager
def _fresh_verified_ledger(ledger: RegistryLedger) -> Iterator[RegistryLedger]:
    """Translate the shared registry transaction's integrity refusal to this API."""

    try:
        with verified_registry_transaction(ledger._path_capability) as fresh:
            yield fresh
    except (AuditLogCorruptionError, RegistryIntegrityError) as error:
        raise HoldoutGuardianError(str(error)) from error


def _expiry(record: AuditRecord) -> datetime | None:
    value = record.payload.get("expires_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _window_definition(window: HoldoutWindow) -> dict[str, object]:
    return {
        "name": window.name,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "symbols": sorted(set(window.symbols)),
    }


def _window_scope(window: HoldoutWindow) -> dict[str, object]:
    definition = _window_definition(window)
    return {key: definition[key] for key in ("start", "end", "symbols")}


def _holdout_set_digest(windows: tuple[HoldoutWindow, ...]) -> str:
    definitions = [{**_window_definition(window), "reason": window.reason} for window in windows]
    definitions.sort(key=lambda definition: json.dumps(definition, sort_keys=True))
    return _digest(definitions)


def _data_identity(
    history_root: Path,
    window: HoldoutWindow,
    *,
    snapshots: Mapping[str, bytes | None] | None = None,
) -> str:
    """Hash the exact stored bar bytes in scope at grant/consume time."""

    supplied = snapshots or {}
    identities: dict[str, str | None] = {}
    if window.symbols:
        for symbol in sorted(set(window.symbols)):
            path = bars_path(history_root, symbol)
            raw = supplied[symbol] if symbol in supplied else _safe_read_bar_bytes(path)
            identities[symbol] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    else:
        bars_root = history_root / "bars"
        for path in sorted(bars_root.glob("*.csv")):
            raw = supplied[path.name] if path.name in supplied else _safe_read_bar_bytes(path)
            identities[path.name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    return _digest(identities)


def _safe_read_bar_bytes(path: Path) -> bytes | None:
    """Read one owner-controlled regular file through held no-follow descriptors."""

    absolute = Path(os.path.abspath(path))
    parent_descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for depth, component in enumerate(absolute.parent.parts[1:], start=1):
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            except FileNotFoundError:
                return None
            except OSError as error:
                location = Path(os.sep, *absolute.parent.parts[1:depth])
                raise HoldoutGuardianError(
                    f"holdout bars path {location} contains a symlink or non-directory"
                ) from error
            os.close(parent_descriptor)
            parent_descriptor = child

        try:
            before = os.stat(absolute.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _require_safe_bar_file(before, absolute)
        try:
            descriptor = os.open(absolute.name, _FILE_FLAGS, dir_fd=parent_descriptor)
        except OSError as error:
            raise HoldoutGuardianError(
                f"holdout bars file {absolute} could not be opened safely"
            ) from error
        try:
            opened = os.fstat(descriptor)
            _require_safe_bar_file(opened, absolute)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise HoldoutGuardianError("holdout bars file was replaced during open")
            raw = _read_all(descriptor)
            after = os.fstat(descriptor)
            _require_safe_bar_file(after, absolute)
            try:
                current = os.stat(absolute.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError as error:
                raise HoldoutGuardianError(
                    "holdout bars file disappeared during its read"
                ) from error
            _require_safe_bar_file(current, absolute)
            opened_identity = _stable_file_identity(opened)
            if opened_identity != _stable_file_identity(after) or opened_identity != (
                _stable_file_identity(current)
            ):
                raise HoldoutGuardianError("holdout bars file changed during its read")
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _require_safe_bar_file(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise HoldoutGuardianError(f"holdout bars file {path} is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise HoldoutGuardianError(f"holdout bars file {path} is not owner-controlled")
    if metadata.st_nlink != 1:
        raise HoldoutGuardianError(f"holdout bars file {path} has multiple hard links")


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _current_binding(
    history_root: Path,
    window: HoldoutWindow,
    windows: tuple[HoldoutWindow, ...],
    *,
    snapshots: Mapping[str, bytes | None] | None = None,
) -> _WindowBinding:
    definition = _window_definition(window)
    return _WindowBinding(
        window=window,
        definition=definition,
        definition_sha256=_digest(definition),
        scope_sha256=_digest(_window_scope(window)),
        data_identity_sha256=_data_identity(history_root, window, snapshots=snapshots),
        holdout_set_sha256=_holdout_set_digest(windows),
    )


def _binding_fields(binding: _WindowBinding) -> dict[str, object]:
    return {
        "window_definition": binding.definition,
        "window_definition_sha256": binding.definition_sha256,
        "window_scope_sha256": binding.scope_sha256,
        "data_identity_sha256": binding.data_identity_sha256,
        "holdout_set_sha256": binding.holdout_set_sha256,
    }


def _recorded_binding(record: AuditRecord) -> _WindowBinding | None:
    """Validate a stored immutable binding; ``None`` denotes a legacy record."""

    payload = record.payload
    binding_keys = (
        "window_definition",
        "window_definition_sha256",
        "window_scope_sha256",
        "data_identity_sha256",
        "holdout_set_sha256",
    )
    present = [key in payload for key in binding_keys]
    if not any(present):
        return None
    if not all(present):
        raise HoldoutGuardianError("holdout record has an incomplete immutable window binding")
    definition = payload["window_definition"]
    if not isinstance(definition, dict) or set(definition) != {"name", "start", "end", "symbols"}:
        raise HoldoutGuardianError("holdout record window definition is invalid")
    raw_symbols = definition.get("symbols")
    if not isinstance(raw_symbols, list) or not all(isinstance(item, str) for item in raw_symbols):
        raise HoldoutGuardianError("holdout record window symbols are invalid")
    try:
        window = HoldoutWindow(
            name=str(definition["name"]),
            start=date.fromisoformat(str(definition["start"])),
            end=date.fromisoformat(str(definition["end"])),
            symbols=tuple(raw_symbols),
        )
    except (KeyError, ValueError) as error:
        raise HoldoutGuardianError("holdout record window definition is invalid") from error
    canonical_definition = _window_definition(window)
    if definition != canonical_definition or payload.get("window") != window.name:
        raise HoldoutGuardianError("holdout record window definition is not canonical")
    definition_sha256 = payload["window_definition_sha256"]
    scope_sha256 = payload["window_scope_sha256"]
    data_identity_sha256 = payload["data_identity_sha256"]
    holdout_set_sha256 = payload["holdout_set_sha256"]
    if (
        definition_sha256 != _digest(canonical_definition)
        or scope_sha256 != _digest(_window_scope(window))
        or not isinstance(data_identity_sha256, str)
        or len(data_identity_sha256) != 64
        or any(character not in "0123456789abcdef" for character in data_identity_sha256)
        or not isinstance(holdout_set_sha256, str)
        or len(holdout_set_sha256) != 64
        or any(character not in "0123456789abcdef" for character in holdout_set_sha256)
    ):
        raise HoldoutGuardianError("holdout record immutable window binding is invalid")
    assert isinstance(definition_sha256, str)
    assert isinstance(scope_sha256, str)
    return _WindowBinding(
        window=window,
        definition=canonical_definition,
        definition_sha256=definition_sha256,
        scope_sha256=scope_sha256,
        data_identity_sha256=data_identity_sha256,
        holdout_set_sha256=holdout_set_sha256,
    )


def _bindings_match(left: _WindowBinding, right: _WindowBinding) -> bool:
    return (
        left.definition == right.definition
        and left.definition_sha256 == right.definition_sha256
        and left.scope_sha256 == right.scope_sha256
        and left.data_identity_sha256 == right.data_identity_sha256
        and left.holdout_set_sha256 == right.holdout_set_sha256
    )


def _scopes_overlap(left: HoldoutWindow, right: HoldoutWindow) -> bool:
    dates_overlap = left.start <= right.end and right.start <= left.end
    if not dates_overlap:
        return False
    left_symbols = set(left.symbols)
    right_symbols = set(right.symbols)
    return not left_symbols or not right_symbols or bool(left_symbols & right_symbols)


def burned_windows(ledger: RegistryLedger) -> frozenset[str]:
    """Windows with a verified recorded consume — spent."""

    with _fresh_verified_ledger(ledger) as fresh:
        return _burned_windows_unchecked(fresh)


def is_burned(ledger: RegistryLedger, window: str) -> bool:
    """Whether ``window`` is spent, refusing an unverified ledger."""

    with _fresh_verified_ledger(ledger) as fresh:
        return _is_burned_unchecked(fresh, window)


def _burned_windows_unchecked(ledger: RegistryLedger) -> frozenset[str]:
    """Derive burns inside an already-verified registry transaction."""

    burned: set[str] = set()
    for record in ledger.records_of(KIND_CONSUME):
        window = record.payload.get("window")
        if not isinstance(window, str) or not window:
            raise HoldoutGuardianError("holdout consume record has an invalid window")
        _recorded_binding(record)
        burned.add(window)
    return frozenset(burned)


def _is_burned_unchecked(ledger: RegistryLedger, window: str) -> bool:
    return window in _burned_windows_unchecked(ledger)


def _consumed_unlock_ids(ledger: RegistryLedger) -> frozenset[str]:
    return frozenset(str(r.payload["unlock_id"]) for r in ledger.records_of(KIND_CONSUME))


def _unlock_record(ledger: RegistryLedger, unlock_id: str) -> AuditRecord | None:
    for record in ledger.records_of(KIND_UNLOCK):
        if str(record.payload.get("unlock_id")) == unlock_id:
            return record
    return None


def _window_by_name(windows: tuple[HoldoutWindow, ...], name: str) -> HoldoutWindow | None:
    return next((w for w in windows if w.name == name), None)


def _has_outstanding_grant(
    ledger: RegistryLedger,
    window: HoldoutWindow,
    now: datetime,
) -> bool:
    consumed = _consumed_unlock_ids(ledger)
    for record in ledger.records_of(KIND_UNLOCK):
        if str(record.payload.get("unlock_id")) in consumed:
            continue
        expiry = _expiry(record)
        if expiry is None or now >= expiry:
            continue
        binding = _recorded_binding(record)
        if binding is None:
            # A still-active legacy grant lacks scope evidence, so overlap cannot be
            # disproved.  Wait for expiry rather than fail open.
            return True
        if _scopes_overlap(binding.window, window):
            return True
    return False


def _require_scope_not_burned(ledger: RegistryLedger, window: HoldoutWindow) -> None:
    for record in ledger.records_of(KIND_CONSUME):
        binding = _recorded_binding(record)
        if binding is None:
            recorded_name = record.payload.get("window")
            if recorded_name == window.name:
                raise HoldoutGuardianError(
                    f"holdout window {window.name!r} is already burned; it cannot be re-unlocked"
                )
            raise HoldoutGuardianError(
                "a legacy burn lacks immutable scope evidence; an explicit owner "
                "contamination/reset record is required before another unlock"
            )
        if _scopes_overlap(binding.window, window):
            raise HoldoutGuardianError(
                f"holdout window {window.name!r} overlaps already-burned scope "
                f"{binding.window.name!r}"
            )


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
    with _fresh_verified_ledger(ledger) as fresh:
        windows = load_holdouts(history_root)
        window = _window_by_name(windows, window_name)
        if window is None:
            raise HoldoutGuardianError(f"holdout window {window_name!r} is not declared")
        binding = _current_binding(history_root, window, windows)
        _require_scope_not_burned(fresh, window)
        if _has_outstanding_grant(fresh, window, now):
            raise HoldoutGuardianError(
                f"holdout window {window_name!r} already has an outstanding unlock grant"
            )
        if (
            available_budget(
                fresh,
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
        fresh.append(
            KIND_UNLOCK,
            {
                "unlock_id": unlock_id,
                "window": window_name,
                "reason": reason,
                "expires_at": expires_at,
                **_binding_fields(binding),
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
    normalized_symbol = symbol.upper()
    with _fresh_verified_ledger(ledger) as fresh:
        record = _unlock_record(fresh, grant.unlock_id)
        if record is None:
            raise HoldoutGuardianError("unknown unlock grant")
        stored_window = record.payload.get("window")
        stored_expiry = record.payload.get("expires_at")
        if (
            not isinstance(stored_window, str)
            or not isinstance(stored_expiry, str)
            or grant.window != stored_window
            or grant.expires_at != stored_expiry
        ):
            raise HoldoutGuardianError("unlock grant disagrees with its durable record")
        stored_binding = _recorded_binding(record)
        if stored_binding is None:
            raise HoldoutGuardianError(
                "unlock grant predates immutable window binding and cannot be consumed"
            )
        if grant.unlock_id in _consumed_unlock_ids(fresh):
            raise HoldoutGuardianError("unlock grant already consumed (single-use)")
        if _is_burned_unchecked(fresh, stored_window):
            raise HoldoutGuardianError(f"holdout window {stored_window!r} is already burned")
        expiry = _expiry(record)
        if expiry is None or now >= expiry:
            raise HoldoutGuardianError("unlock grant expired")
        windows = load_holdouts(history_root)
        window = _window_by_name(windows, stored_window)
        if window is None:
            raise HoldoutGuardianError(f"holdout window {stored_window!r} is no longer declared")
        target_path = bars_path(history_root, normalized_symbol)
        target_raw = _safe_read_bar_bytes(target_path)
        snapshot_key = normalized_symbol if window.symbols else target_path.name
        current_binding = _current_binding(
            history_root,
            window,
            windows,
            snapshots={snapshot_key: target_raw},
        )
        if not _bindings_match(stored_binding, current_binding):
            raise HoldoutGuardianError(
                "holdout definition or stored data changed after the unlock grant"
            )
        if not window.applies_to(normalized_symbol):
            raise HoldoutGuardianError(f"holdout window {stored_window!r} does not cover {symbol}")

        # Record the consume (burning the window) BEFORE unmasking, so a burn is durable
        # even if the read is interrupted — the fail-safe direction.
        fresh.append(
            KIND_CONSUME,
            {
                "unlock_id": grant.unlock_id,
                "window": stored_window,
                "symbol": normalized_symbol,
                **_binding_fields(stored_binding),
            },
        )
        series = (
            BarSeries(symbol=normalized_symbol, interval=BarInterval.DAY_1, bars=())
            if target_raw is None
            else load_daily_csv_bytes(
                target_raw,
                path=target_path,
                symbol=normalized_symbol,
                source="ibkr",
                exchange="SMART",
            ).series
        )
    return _embargoed_view_with_window_unlocked(
        series,
        windows,
        normalized_symbol,
        window_name=stored_window,
    )

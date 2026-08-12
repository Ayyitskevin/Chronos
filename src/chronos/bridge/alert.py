"""Parsing a TradingView webhook body as though the sender were hostile (ADR-0026).

This module is the bridge's equivalent of :mod:`chronos.supervisor.ingress`, and
it is written from the same posture for the same reason: the bytes arriving here
came off the public internet, and the sender may be anyone who learned the URL.

It deliberately does not *reuse* the supervisor's ingress. That module lives in
the process that holds the broker connection and validates against the real
decision contract; importing it here would drag the contract — and the
single-consumer question — into a process whose whole value is that it holds
nothing. The duplication is small, bounded, and guarded by
``tests/safety/test_tradingview_bridge_exercised.py``, which proves the two
agree by pushing this module's output through the real ingress.

## The order of checks, and why it is this order

1. **Size, before parsing.** A large body is refused by length rather than by
   exhausting memory proving it invalid.
2. **Strict JSON, one object, bounded depth, no NaN/Infinity.** ``json`` accepts
   the non-finite constants by default, and a NaN quantity compares falsely
   against every ceiling it will later meet.
3. **The shared secret, before anything else is validated.** An unauthenticated
   caller should learn only that it is unauthenticated — not which of its other
   fields the bridge dislikes, which would make this endpoint a free schema
   oracle for someone probing it.
4. **The rest of the fields**, each normalized and bounded.

No refusal message ever echoes payload content. A hostile sender must not be
able to write chosen text into the owner's terminal or logs through the one path
guaranteed to be read — the same reasoning behind the decision contract's
control-character refusal and the supervisor ingress's location-only errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from chronos.bridge.vocabulary import (
    DECISION_KINDS,
    DIRECTIONS,
    STRATEGY_FORMS,
    SYMBOL_ALPHABET,
    TIME_HORIZONS,
)
from chronos.utils.time import as_utc

#: An alert is far smaller than a proposal; the bound is tighter than the
#: ingress's 256 KiB for the same reason the ingress's is tighter than nothing.
MAX_ALERT_BYTES: Final[int] = 16 * 1024

#: Real alerts nest two levels. Eight is generous and still finite.
MAX_NESTING_DEPTH: Final[int] = 8

#: Bounded so the derived ``evidence_id`` stays inside the contract's 128-char
#: limit, and restricted to an alphabet that is safe to write into a log line.
MAX_ALERT_ID_LENGTH: Final[int] = 80
_ALERT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]+$")

_MAX_NARRATIVE_LENGTH: Final[int] = 4000
_MAX_NARRATIVE_ENTRY_LENGTH: Final[int] = 500
_MAX_NARRATIVE_ENTRIES: Final[int] = 32
_MAX_SYMBOL_LENGTH: Final[int] = 32

#: Mirrors the decision contract's persistence envelope. Restated rather than
#: imported; the contract re-checks it downstream regardless.
_MAX_QUANTITY: Final[Decimal] = Decimal(10) ** 12
_MIN_QUANTITY_EXPONENT: Final[int] = -8

#: The field carrying the shared secret. Removed from the document before the
#: digest is taken and before any field is logged, so the secret never reaches
#: the audit chain, a log line, or an error message.
_SECRET_FIELD: Final[str] = "secret"

_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        _SECRET_FIELD,
        "alert_id",
        "sent_at",
        "action",
        "symbol",
        "direction",
        "quantity",
        "strategy",
        "time_horizon",
        "target_reference",
        "thesis",
        "rationale",
        "confidence",
        "invalidation",
    }
)


class AlertRejected(ValueError):
    """The body did not become an alert. The message is safe to log."""


class AlertUnauthorized(AlertRejected):
    """The body did not carry the shared secret.

    Separate from :class:`AlertRejected` only so the transport can answer 401
    rather than 422. The message is deliberately identical for a missing secret
    and a wrong one.
    """


@dataclass(frozen=True, slots=True)
class TradingViewAlert:
    """One authenticated, well-formed alert. Authorizes nothing by existing."""

    alert_id: str
    sent_at: datetime
    action: str
    symbol: str
    direction: str
    quantity: Decimal | None
    strategy: str
    time_horizon: str
    target_reference: str
    thesis: str
    rationale: str
    confidence: Decimal | None
    invalidation: tuple[str, ...]
    #: SHA-256 over the canonical JSON of this alert with the secret removed.
    #: Written into the proposal's evidence citation, so the audit record can
    #: prove which alert text produced which decision.
    digest: str


def parse_alert(payload: bytes | str, *, expected_secret: str) -> TradingViewAlert:
    """Turn a webhook body into an alert, or raise.

    Raises :class:`AlertUnauthorized` when the secret is absent or wrong, and
    :class:`AlertRejected` for everything else.
    """

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not raw:
        raise AlertRejected("empty body")
    if len(raw) > MAX_ALERT_BYTES:
        raise AlertRejected(
            f"body is {len(raw)} bytes, over the {MAX_ALERT_BYTES}-byte limit; "
            "an alert is a small structured object"
        )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AlertRejected("body is not valid UTF-8") from error

    try:
        document = json.loads(decoded, parse_constant=_refuse_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise AlertRejected(
            "body is not a single well-formed JSON document; the TradingView alert "
            "message must be JSON, not free text"
        ) from error
    if not isinstance(document, dict):
        raise AlertRejected(
            f"body must be a JSON object describing one alert, got {type(document).__name__}"
        )
    if _depth(document) > MAX_NESTING_DEPTH:
        raise AlertRejected(f"body nests deeper than {MAX_NESTING_DEPTH} levels; an alert does not")
    _reject_non_finite(document)

    _check_secret(document, expected_secret)

    unknown = sorted(set(document) - _KNOWN_FIELDS)
    if unknown:
        raise AlertRejected(
            f"body carries unknown field(s) {unknown}; refused rather than ignored, because a "
            "field the bridge silently drops is a field the author believes is doing something"
        )

    fields = {name: value for name, value in document.items() if name != _SECRET_FIELD}
    return TradingViewAlert(
        alert_id=_alert_id(fields),
        sent_at=_sent_at(fields),
        action=_enumerated(fields, "action", DECISION_KINDS, required=True),
        symbol=_symbol(fields),
        direction=_enumerated(fields, "direction", DIRECTIONS, required=False) or "NEUTRAL",
        quantity=_quantity(fields),
        strategy=_enumerated(fields, "strategy", STRATEGY_FORMS, required=False),
        time_horizon=_enumerated(fields, "time_horizon", TIME_HORIZONS, required=False),
        target_reference=_target_reference(fields),
        thesis=_narrative(fields, "thesis"),
        rationale=_narrative(fields, "rationale"),
        confidence=_confidence(fields),
        invalidation=_invalidation(fields),
        digest=canonical_digest(fields),
    )


def canonical_digest(fields: dict[str, Any]) -> str:
    """SHA-256 over the alert's canonical JSON, secret already removed.

    Deterministic — sorted keys, tight separators — so the owner can recompute it
    from the alert text TradingView sent and match it against what the audit
    chain recorded.
    """

    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_secret(document: dict[str, Any], expected: str) -> None:
    presented = document.get(_SECRET_FIELD)
    if not isinstance(presented, str) or not hmac.compare_digest(presented, expected):
        # One message for both cases on purpose: a caller probing this endpoint
        # learns whether it is authorized and nothing else.
        raise AlertUnauthorized("the alert did not carry the configured shared secret")


def _alert_id(fields: dict[str, Any]) -> str:
    value = fields.get("alert_id")
    if not isinstance(value, str) or not value.strip():
        raise AlertRejected(
            "alert_id is required: it is how a duplicate delivery is recognized and how the "
            "resulting decision is traced back to the alert that caused it"
        )
    normalized = value.strip()
    if len(normalized) > MAX_ALERT_ID_LENGTH:
        raise AlertRejected(f"alert_id is longer than {MAX_ALERT_ID_LENGTH} characters")
    if not _ALERT_ID_PATTERN.match(normalized):
        raise AlertRejected(
            "alert_id may contain only letters, digits, '.', '_', ':' and '-'; it is written "
            "into logs and into the decision's evidence citation"
        )
    return normalized


def _sent_at(fields: dict[str, Any]) -> datetime:
    value = fields.get("sent_at")
    if not isinstance(value, str) or not value.strip():
        raise AlertRejected(
            "sent_at is required: without the alert's own timestamp the bridge cannot refuse a "
            "stale replay, and TradingView supplies it as {{timenow}}"
        )
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise AlertRejected(
            "sent_at is not an ISO-8601 timestamp; use TradingView's {{timenow}} placeholder"
        ) from error
    if parsed.tzinfo is None:
        raise AlertRejected(
            "sent_at must carry a UTC offset; a naive timestamp cannot be aged reliably"
        )
    return as_utc(parsed)


def _symbol(fields: dict[str, Any]) -> str:
    value = fields.get("symbol")
    if not isinstance(value, str) or not value.strip():
        raise AlertRejected("symbol is required")
    normalized = value.strip().upper()
    if len(normalized) > _MAX_SYMBOL_LENGTH:
        raise AlertRejected(f"symbol is longer than {_MAX_SYMBOL_LENGTH} characters")
    if not set(normalized) <= SYMBOL_ALPHABET:
        raise AlertRejected("symbol contains characters the decision contract does not accept")
    return normalized


def _enumerated(
    fields: dict[str, Any], name: str, permitted: frozenset[str], *, required: bool
) -> str:
    value = fields.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise AlertRejected(f"{name} is required; one of {sorted(permitted)}")
        return ""
    if not isinstance(value, str):
        raise AlertRejected(f"{name} must be a string; one of {sorted(permitted)}")
    normalized = value.strip().upper()
    if normalized not in permitted:
        # The permitted set is Chronos's own vocabulary, so naming it echoes
        # nothing the sender wrote.
        raise AlertRejected(f"{name} is not a recognized value; one of {sorted(permitted)}")
    return normalized


def _quantity(fields: dict[str, Any]) -> Decimal | None:
    value = fields.get("quantity")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _bounded_amount(value, "quantity")


def _confidence(fields: dict[str, Any]) -> Decimal | None:
    value = fields.get("confidence")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    amount = _decimal(value, "confidence")
    if not Decimal(0) <= amount <= Decimal(1):
        raise AlertRejected("confidence must be between 0 and 1 inclusive")
    return amount


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise AlertRejected(f"{label} must be a number or a numeric string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AlertRejected(f"{label} is not a number") from error
    if not amount.is_finite():
        raise AlertRejected(f"{label} must be finite")
    return amount


def _bounded_amount(value: Any, label: str) -> Decimal:
    amount = _decimal(value, label)
    if amount <= 0:
        raise AlertRejected(f"{label} must be positive")
    exponent = amount.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < _MIN_QUANTITY_EXPONENT:
        raise AlertRejected(f"{label} is finer than the 1e-8 persistence scale")
    if amount >= _MAX_QUANTITY:
        raise AlertRejected(f"{label} exceeds the persistence magnitude the contract allows")
    return amount


def _target_reference(fields: dict[str, Any]) -> str:
    value = fields.get("target_reference")
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        raise AlertRejected("target_reference must be a string")
    normalized = value.strip().upper()
    if len(normalized) > 128:
        raise AlertRejected("target_reference is longer than 128 characters")
    return normalized


def _narrative(fields: dict[str, Any], name: str) -> str:
    value = fields.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AlertRejected(f"{name} must be a string")
    if len(value) > _MAX_NARRATIVE_LENGTH:
        raise AlertRejected(f"{name} is longer than {_MAX_NARRATIVE_LENGTH} characters")
    return _reject_control_characters(value, name)


def _invalidation(fields: dict[str, Any]) -> tuple[str, ...]:
    value = fields.get("invalidation")
    if value is None:
        return ()
    if isinstance(value, str):
        entries = [value]
    elif isinstance(value, list):
        entries = []
        for item in value:
            if not isinstance(item, str):
                raise AlertRejected("every invalidation entry must be a string")
            entries.append(item)
    else:
        raise AlertRejected("invalidation must be a string or a list of strings")

    normalized: list[str] = []
    for entry in entries:
        cleaned = _reject_control_characters(entry.strip(), "invalidation entry")
        if not cleaned:
            raise AlertRejected("invalidation entries must not be blank")
        if len(cleaned) > _MAX_NARRATIVE_ENTRY_LENGTH:
            raise AlertRejected(
                f"invalidation entries are limited to {_MAX_NARRATIVE_ENTRY_LENGTH} characters"
            )
        normalized.append(cleaned)
    if len(normalized) > _MAX_NARRATIVE_ENTRIES:
        raise AlertRejected(f"at most {_MAX_NARRATIVE_ENTRIES} invalidation entries are accepted")
    return tuple(normalized)


def _reject_control_characters(value: str, label: str) -> str:
    """Mirrors the decision contract's rule; newlines and tabs stay allowed."""

    forbidden = {
        character for character in value if ord(character) < 32 and character not in "\n\t"
    }
    if forbidden or "\x7f" in value or "\x1b" in value:
        raise AlertRejected(f"{label} may not contain control characters or escape sequences")
    return value


def _depth(value: Any, current: int = 0) -> int:
    if current > MAX_NESTING_DEPTH:
        return current
    if isinstance(value, dict):
        return max((_depth(item, current + 1) for item in value.values()), default=current)
    if isinstance(value, list):
        return max((_depth(item, current + 1) for item in value), default=current)
    return current


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise AlertRejected(
            "body contains a non-finite number; NaN and Infinity are never valid quantities "
            "and compare falsely against every limit"
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _refuse_constant(name: str) -> Any:
    raise AlertRejected(
        f"body contains the JSON constant {name!r}; NaN and Infinity are never valid"
    )

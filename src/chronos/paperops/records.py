"""Decision record model and secret-safe payload sanitization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from chronos.paperops.reasons import DecisionKind, DecisionOutcome, PaperReasonCode

# Substrings that must never appear in ledger payloads (case-insensitive).
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "client_secret",
    "bearer",
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"(?i)sk-[a-z0-9]{16,}"),
)


class RecordValidationError(ValueError):
    """Record is incomplete, corrupt, or contains forbidden material."""


def _is_secret_key(key: str) -> bool:
    lower = key.lower().replace("-", "_")
    return any(fragment in lower for fragment in _SECRET_KEY_FRAGMENTS)


def sanitize_payload(payload: Mapping[str, Any] | dict[str, Any]) -> dict[str, object]:
    """Return a JSON-safe payload with secret-like keys/values stripped.

    Fail closed: secret-bearing keys are dropped (not redacted in place as
    still-present values) so greps for tokens against the ledger stay clean.
    """

    clean: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise RecordValidationError(f"payload keys must be strings, got {type(key)}")
        if _is_secret_key(key):
            continue
        if isinstance(value, Mapping):
            nested = sanitize_payload(dict(value))
            clean[key] = nested
        elif isinstance(value, (list, tuple)):
            items: list[object] = []
            for item in value:
                if isinstance(item, Mapping):
                    items.append(sanitize_payload(dict(item)))
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    if isinstance(item, str) and any(
                        p.search(item) for p in _SECRET_VALUE_PATTERNS
                    ):
                        continue
                    items.append(item)
                else:
                    items.append(str(item))
            clean[key] = items
        elif isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and any(p.search(value) for p in _SECRET_VALUE_PATTERNS):
                continue
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


def inputs_fingerprint(material: Mapping[str, Any] | dict[str, Any]) -> str:
    """Deterministic SHA-256 of canonical JSON for replay binding."""

    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One append-only paper decision ledger row (pre-hash-chain fields)."""

    sequence: int
    at_utc: str
    kind: str
    reason_code: str
    outcome: str
    strategy_id: str
    strategy_version: str
    config_hash: str
    data_timestamp_utc: str | None
    data_source: str
    data_quality_label: str
    inputs_fingerprint: str
    payload: dict[str, object]
    previous_hash: str
    record_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DecisionRecord:
        required = (
            "sequence",
            "at_utc",
            "kind",
            "reason_code",
            "outcome",
            "strategy_id",
            "strategy_version",
            "config_hash",
            "data_source",
            "data_quality_label",
            "inputs_fingerprint",
            "payload",
            "previous_hash",
            "record_hash",
        )
        missing = [k for k in required if k not in raw]
        if missing:
            raise RecordValidationError(f"incomplete decision record; missing: {missing}")
        try:
            kind = DecisionKind(str(raw["kind"]))
            reason = PaperReasonCode(str(raw["reason_code"]))
            outcome = DecisionOutcome(str(raw["outcome"]))
        except ValueError as error:
            raise RecordValidationError(f"invalid enum in record: {error}") from error
        payload = raw["payload"]
        if not isinstance(payload, dict):
            raise RecordValidationError("payload must be an object")
        data_ts = raw.get("data_timestamp_utc")
        if data_ts is not None and not isinstance(data_ts, str):
            raise RecordValidationError("data_timestamp_utc must be a string or null")
        return cls(
            sequence=int(raw["sequence"]),
            at_utc=str(raw["at_utc"]),
            kind=kind.value,
            reason_code=reason.value,
            outcome=outcome.value,
            strategy_id=str(raw["strategy_id"]),
            strategy_version=str(raw["strategy_version"]),
            config_hash=str(raw["config_hash"]),
            data_timestamp_utc=data_ts,
            data_source=str(raw["data_source"]),
            data_quality_label=str(raw["data_quality_label"]),
            inputs_fingerprint=str(raw["inputs_fingerprint"]),
            payload=sanitize_payload(payload),
            previous_hash=str(raw["previous_hash"]),
            record_hash=str(raw["record_hash"]),
        )

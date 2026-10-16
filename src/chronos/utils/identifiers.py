"""Opaque Chronos correlation identifiers."""

import hmac
from hashlib import sha256
from typing import NoReturn
from uuid import uuid4

_PERSISTENCE_KEY_BYTES = 32
_LOWER_HEX = frozenset("0123456789abcdef")


class PersistenceKey:
    """The per-install secret that keys every persisted account fingerprint (N3).

    Built only from its 64-lowercase-hex form (``CHRONOS_PERSISTENCE_KEY``, owner-held, never
    in a tracked file). ``repr`` and ``str`` never render the bytes, so a key that reaches a log
    line or a traceback prints as a placeholder.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if type(secret) is not bytes or len(secret) != _PERSISTENCE_KEY_BYTES:
            raise ValueError("a persistence key is exactly 32 bytes")
        self._secret = secret

    @classmethod
    def from_hex(cls, value: str) -> "PersistenceKey":
        if (
            type(value) is not str
            or len(value) != 2 * _PERSISTENCE_KEY_BYTES
            or not set(value) <= _LOWER_HEX
        ):
            raise ValueError("a persistence key is 64 lowercase hex digits")
        return cls(bytes.fromhex(value))

    def __repr__(self) -> str:
        return "PersistenceKey(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("a persistence key is never serialized")


def normalize_account_fingerprint(value: str) -> str:
    """Validate and normalize a pseudonymous account scope."""

    normalized = value.strip().lower()
    invalid_character = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or invalid_character:
        raise ValueError("account_fingerprint must be a 64-character lowercase hex pseudonym")
    return normalized


def account_fingerprint(account_id: str, *, key: PersistenceKey) -> str:
    """Return the keyed pseudonym persisted in place of a raw broker account id.

    HMAC-SHA256 under the install's persistence key over the domain-tagged id. Without the key
    the value cannot be recomputed from a candidate id, so a copy of the database does not
    reveal the account (N3). There is no unkeyed variant and no default key.
    """

    if not isinstance(key, PersistenceKey):
        raise TypeError("account_fingerprint requires the install's PersistenceKey")
    normalized_account = account_id.strip()
    if not normalized_account:
        raise ValueError("account_id must not be blank")
    return hmac.new(
        key._secret, f"chronos-account:{normalized_account}".encode(), sha256
    ).hexdigest()


def new_correlation_id(prefix: str) -> str:
    normalized = "".join(character for character in prefix.upper() if character.isalnum())
    if not normalized:
        raise ValueError("Identifier prefix must contain an alphanumeric character")
    return f"CHR-{normalized}-{uuid4().hex.upper()}"

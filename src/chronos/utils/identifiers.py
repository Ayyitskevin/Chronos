"""Opaque Chronos correlation identifiers."""

from hashlib import sha256
from uuid import uuid4


def normalize_account_fingerprint(value: str) -> str:
    """Validate and normalize a pseudonymous account scope."""

    normalized = value.strip().lower()
    invalid_character = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or invalid_character:
        raise ValueError("account_fingerprint must be a 64-character lowercase hex pseudonym")
    return normalized


def account_fingerprint(account_id: str) -> str:
    """Return a stable pseudonym so raw broker account IDs are never persisted."""

    normalized_account = account_id.strip()
    if not normalized_account:
        raise ValueError("account_id must not be blank")
    return sha256(f"chronos-account:{normalized_account}".encode()).hexdigest()


def new_correlation_id(prefix: str) -> str:
    normalized = "".join(character for character in prefix.upper() if character.isalnum())
    if not normalized:
        raise ValueError("Identifier prefix must contain an alphanumeric character")
    return f"CHR-{normalized}-{uuid4().hex.upper()}"

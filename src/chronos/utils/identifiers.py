"""Opaque Chronos correlation identifiers."""

from uuid import uuid4


def new_correlation_id(prefix: str) -> str:
    normalized = "".join(character for character in prefix.upper() if character.isalnum())
    if not normalized:
        raise ValueError("Identifier prefix must contain an alphanumeric character")
    return f"CHR-{normalized}-{uuid4().hex.upper()}"

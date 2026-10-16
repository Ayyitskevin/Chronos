"""Test-only persistence keys (N3). Derived at run time from a label, never written as a literal.

The real ``CHRONOS_PERSISTENCE_KEY`` is generated and held by the owner alone; no test, fixture or
log ever carries it. Every key here is ``sha256(label)``: 64 lowercase hex, the shape the resolver
accepts, and different for every label, so a test that needs "another install's key" names
another label.
"""

from __future__ import annotations

from hashlib import sha256

from chronos.utils.identifiers import PersistenceKey


def derived_key_hex(label: str) -> str:
    """The 64-hex form of the test key for ``label`` (what an ``.env`` line would carry)."""

    return sha256(f"chronos-tests:persistence-key:{label}".encode()).hexdigest()


def derived_key(label: str) -> PersistenceKey:
    return PersistenceKey.from_hex(derived_key_hex(label))


#: The suite's default install key. ``tests/conftest.py`` exports the same value as
#: ``CHRONOS_PERSISTENCE_KEY`` for every test, so a value computed here equals the one a
#: runtime built from the environment computes.
TEST_PERSISTENCE_KEY_HEX = derived_key_hex("default")
TEST_PERSISTENCE_KEY = PersistenceKey.from_hex(TEST_PERSISTENCE_KEY_HEX)

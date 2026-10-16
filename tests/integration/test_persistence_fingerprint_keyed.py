"""N3: the persisted account fingerprint must not be enumerable from the account id.

Before N3-1 ``Database.bind_scope`` stored an unkeyed sha256 of ``chronos-account:<id>``, so anyone
holding the database recomputed it for each candidate account id with no secret and recovered the
id. Since N3-1 the stored value is HMAC-SHA256 under the install's persistence key: the attack
below recovers nothing, while the owner, who holds the key, still recomputes it.
"""

from hashlib import sha256

from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

from chronos.persistence.database import Database
from chronos.persistence.schema import DatabaseScopeRow
from chronos.utils.identifiers import account_fingerprint

_ACCOUNT_ID = "DU1234567"
_CANDIDATES = ("DU0000001", "DU0000002", _ACCOUNT_ID, "DU0000003")


def _stored_fingerprint(tmp_path) -> str:
    database = Database(f"sqlite:///{(tmp_path / 'chronos.db').resolve()}")
    try:
        database.initialize()
        database.bind_scope(
            broker_mode="demo",
            environment="paper",
            account_id=_ACCOUNT_ID,
            key=TEST_PERSISTENCE_KEY,
        )
        with database.sessions.begin() as session:
            return session.get(DatabaseScopeRow, 1).account_fingerprint
    finally:
        database.dispose()


def _enumerate(stored: str) -> list[str]:
    """What an attacker holding only the database can do: no key, just candidate ids."""

    return [
        candidate
        for candidate in _CANDIDATES
        if sha256(f"chronos-account:{candidate}".encode()).hexdigest() == stored
    ]


def test_n3_control_the_enumeration_recovers_an_unkeyed_value_and_the_owner_the_keyed_one(
    tmp_path,
) -> None:
    # Positive control (N3-1 conversion; before the fix it asserted the STORED value was
    # enumerable). The harness still recovers the id from an unkeyed value, so the assertion
    # below fails for the right reason if the store ever goes back to one; and the owner, who
    # holds the key, recomputes the stored value exactly.
    unkeyed = sha256(f"chronos-account:{_ACCOUNT_ID}".encode()).hexdigest()
    assert _enumerate(unkeyed) == [_ACCOUNT_ID]

    stored = _stored_fingerprint(tmp_path)
    assert stored == account_fingerprint(_ACCOUNT_ID, key=TEST_PERSISTENCE_KEY)


def test_n3_stored_fingerprint_is_not_reproducible_from_the_account_id(tmp_path) -> None:
    stored = _stored_fingerprint(tmp_path)

    assert _enumerate(stored) == []

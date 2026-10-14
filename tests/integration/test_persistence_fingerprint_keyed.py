"""N3: the persisted account fingerprint must not be enumerable from the account id.

Today ``Database.bind_scope`` stores ``account_fingerprint(account_id)``, an unkeyed sha256 of
``chronos-account:<id>``. Anyone holding the database recomputes that value for each candidate
account id with no secret and recovers the id. The strict xfail pins the property the keyed fix
must establish (Muse ruling, N3): the stored value is not reproducible from the account id alone.
"""

import pytest

from chronos.persistence.database import Database
from chronos.persistence.schema import DatabaseScopeRow
from chronos.utils.identifiers import account_fingerprint

_ACCOUNT_ID = "DU1234567"
_CANDIDATES = ("DU0000001", "DU0000002", _ACCOUNT_ID, "DU0000003")


def _stored_fingerprint(tmp_path) -> str:
    database = Database(f"sqlite:///{(tmp_path / 'chronos.db').resolve()}")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="paper", account_id=_ACCOUNT_ID)
        with database.sessions.begin() as session:
            return session.get(DatabaseScopeRow, 1).account_fingerprint
    finally:
        database.dispose()


def _enumerate(stored: str) -> list[str]:
    """What an attacker holding only the database can do: no key, just candidate ids."""

    return [candidate for candidate in _CANDIDATES if account_fingerprint(candidate) == stored]


def test_n3_control_the_unkeyed_stored_fingerprint_is_enumerable(tmp_path) -> None:
    # Positive control: passes today. It shows the enumeration really does recover the id,
    # so the xfail below is failing for the right reason and not for a broken harness.
    stored = _stored_fingerprint(tmp_path)

    assert _enumerate(stored) == [_ACCOUNT_ID]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "N3: the persisted account_fingerprint is the unkeyed sha256 of 'chronos-account:<id>', "
        "so a database holder recovers the account id by enumerating candidates with no secret. "
        "The keyed fix (ruling pending; see DESIGN.md) makes this assertion hold."
    ),
)
def test_n3_stored_fingerprint_is_not_reproducible_from_the_account_id(tmp_path) -> None:
    stored = _stored_fingerprint(tmp_path)

    assert _enumerate(stored) == []

"""Keyed account fingerprints (N3-1, schema v18): refuse a bound database, never rewrite it.

From schema 18 every persisted account fingerprint is HMAC-SHA256 under the install's
``CHRONOS_PERSISTENCE_KEY``. A schema-17 database that is already bound to a broker scope holds
the unkeyed value in its scope row and in every account-specific row; no table shape changes,
but none of those values can be upgraded without the raw account id, which the database
deliberately does not hold. Kevin's ruling (iii): such a database is archived and replaced by a
fresh keyed one, never re-keyed in place. So this revision:

* on a database with a ``database_scope`` row: refuses before executing anything, so the
  database, its ``schema_version`` and its ``alembic_version`` are left exactly as they were,
  and names the archive runbook;
* on a database with no scope row (nothing account-specific was ever bound): records schema 18.

No DDL. Revision ID: 0017. Revises: 0016 (AP-1b's position_acknowledgements, schema v17).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_SCHEMA_VERSION = 18
#: The archive and cutover procedure (N3-2).
_ARCHIVE_RUNBOOK = "docs/ops/N3-CUTOVER.md"


def upgrade() -> None:
    bind = op.get_bind()
    if "database_scope" in sa.inspect(bind).get_table_names():
        bound = bind.execute(sa.text("SELECT COUNT(*) FROM database_scope")).scalar()
        if bound:
            raise RuntimeError(
                "Refusing schema 18: this database has a database_scope row, so its account "
                "fingerprints are the pre-N3 unkeyed values and cannot be re-keyed in place. "
                "Nothing was changed. Archive it and start a fresh keyed database following "
                f"{_ARCHIVE_RUNBOOK} (the N3 cutover runbook)."
            )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(
            version=_SCHEMA_VERSION, applied_at=datetime.now(tz=UTC)
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"DELETE FROM schema_version WHERE version = {_SCHEMA_VERSION}"))

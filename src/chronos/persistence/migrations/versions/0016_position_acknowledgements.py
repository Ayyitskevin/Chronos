"""Operator acknowledgements of positions (AP-1b, schema v17).

``position_acknowledgements`` is the producer AP-1 left unbuilt: one immutable row per operator
acknowledgement of a position key; a withdrawal is a NEW row whose ``superseded_by`` points at the
old one. The BP-3 run recorder reads the CURRENT rows and carries them into the run's decisions,
where AP-1's classifier records MANUAL — record-only end to end (K3(a)); append-only (K1(a));
a 16-hex operator fingerprint, never a name (K2(a)). Table-only, like 0010 and 0015: idempotent
on a database whose metadata already carries the table.

Revision ID: 0016
Revises: 0015 (AP-1's position_provenance, schema v16, on the chain 0013 → 0014 → 0015).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_V17_TABLES = ("position_acknowledgements",)
_SCHEMA_VERSION = 17


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V17_TABLES if name not in existing]
    if missing:
        schema.Base.metadata.create_all(
            bind, tables=[schema.Base.metadata.tables[name] for name in missing]
        )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(
            version=_SCHEMA_VERSION, applied_at=datetime.now(tz=UTC)
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V17_TABLES):
        op.drop_table(name)
    bind.execute(sa.text(f"DELETE FROM schema_version WHERE version = {_SCHEMA_VERSION}"))

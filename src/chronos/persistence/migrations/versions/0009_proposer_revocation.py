"""Schema v10: durable proposer revocation (A3).

Revision ID: 0009
Revises: 0008

ADR-0023 shipped the proposer registry as a boot-time snapshot on both planes,
and disclosed the consequence as R-48 residual (c): disabling a leaked
credential means editing the file and restarting the backend. A restart is the
wrong latency for a leak, and it is a restart of the process holding the broker
connection — the one thing an operator handling an incident least wants to
bounce.

This adds the table the running process consults instead. Revocation is a
durable act, the shape mandate revocation already has: written once, honored
immediately at the route and at drain, and permanent for the credential it
names.

Creating a new table, so the DDL comes from the canonical metadata like
0002-0004 and 0006 — the fail-closed drift checker requires byte-level
agreement with the models, which makes the metadata the definition.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_V10_TABLES = ("autonomy_proposer_revocations",)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V10_TABLES if name not in existing]
    if missing:
        schema.Base.metadata.create_all(
            bind, tables=[schema.Base.metadata.tables[name] for name in missing]
        )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=10, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V10_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 10"))

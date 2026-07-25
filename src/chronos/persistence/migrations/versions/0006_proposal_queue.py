"""Schema v7: the durable proposal queue.

Revision ID: 0006
Revises: 0005

M6 gave an external worker a way to reach Chronos, and M6's route deliberately
did not run a cycle: doing that inside the request would let the caller's HTTP
schedule drive broker interaction. M7 adds the queue that separates *receiving*
a proposal from *judging* one, so the runtime's tick decides when work happens.

Creating a new table, so as with 0002-0004 the DDL comes from the canonical
metadata — the fail-closed drift checker requires byte-level agreement with the
models, so the metadata is the definition.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_V7_TABLES = ("autonomy_proposal_queue",)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V7_TABLES if name not in existing]
    if missing:
        schema.Base.metadata.create_all(
            bind, tables=[schema.Base.metadata.tables[name] for name in missing]
        )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=7, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V7_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 7"))

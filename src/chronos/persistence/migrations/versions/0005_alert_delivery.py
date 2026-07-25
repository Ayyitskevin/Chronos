"""Schema v6: alert delivery state.

Revision ID: 0005
Revises: 0004

M3 made owner alerts durable; M5 disclosed R-32 as the last blocking promotion
criterion, because durable is not the same as *delivered*. This adds the two
columns that distinguish them:

- ``delivered_at`` — when the alert was successfully handed to a delivery sink.
  NULL means the owner has not been **told**, which is a different fact from not
  having **acknowledged**. An alert nobody was told about is not an alert.
- ``delivery_attempts`` — a durable count, so a sink that is persistently
  failing is visible rather than silently retrying forever.

Unlike the v5 tables, this alters an existing one, so the DDL is explicit rather
than derived from metadata. Existing rows get ``delivered_at = NULL``, which is
the conservative reading: alerts raised before delivery existed were, in fact,
never delivered.

**Why the column add is idempotent.** Revision 0004 creates its tables from the
*current* SQLAlchemy metadata rather than frozen DDL — a deliberate choice there,
because the fail-closed drift checker demands byte-level agreement with the
models. The cost of that choice shows up here: a database upgraded through 0004
*today* already receives these two columns, because the metadata now has them,
while a database upgraded before this revision existed does not. Adding blindly
would fail on the first path. Checking first is correct on both, and it is the
reason this revision inspects before it alters.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_NEW_COLUMNS = (
    sa.Column("delivered_at", sa.DateTime(), nullable=True),
    sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("autonomy_owner_alerts")}
    for column in _NEW_COLUMNS:
        if column.name not in existing:
            op.add_column("autonomy_owner_alerts", column)
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=6, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("autonomy_owner_alerts")}
    for name in ("delivery_attempts", "delivered_at"):
        if name in existing:
            op.drop_column("autonomy_owner_alerts", name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 6"))

"""Schema v11: one opening order binds one managed QQQ position.

Revision ID: 0010
Revises: 0009

ADR-0035 requires a database-enforced identity between the canonical order
plane and each hash-chained position stream before PAPER activation.  The row
contains pseudonymous account/order evidence only; it grants no trading
authority and the migration intentionally backfills nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_V11_TABLES = ("managed_position_bindings",)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V11_TABLES if name not in existing]
    if missing:
        schema.Base.metadata.create_all(
            bind, tables=[schema.Base.metadata.tables[name] for name in missing]
        )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=11, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V11_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 11"))

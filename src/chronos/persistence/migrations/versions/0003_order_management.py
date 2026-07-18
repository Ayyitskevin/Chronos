"""Schema v4: order-management lifecycle tables.

Revision ID: 0003
Revises: 0002

Creates the order-management lifecycle tables (order_events, risk_decisions,
risk_check_results) and records schema version 4.

As with revision 0002, the tables are created from the canonical SQLAlchemy
metadata rather than frozen inline DDL: Chronos's fail-closed drift checker
(persistence.database._schema_drift) requires byte-level agreement with the
current metadata, so the metadata IS the v4 definition. A future revision that
alters these tables will carry explicit DDL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_V4_TABLES = (
    "order_events",
    "risk_decisions",
    "risk_check_results",
)


def upgrade() -> None:
    bind = op.get_bind()
    schema.Base.metadata.create_all(
        bind,
        tables=[schema.Base.metadata.tables[name] for name in _V4_TABLES],
    )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=4, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V4_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 4"))

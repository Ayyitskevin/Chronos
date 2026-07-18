"""Schema v3: live-wheel order pipeline tables + writer lease.

Revision ID: 0002
Revises: 0001

Creates the durable order-pipeline tables (order_intents,
order_confirmations, live_arm_events, kill_switch_events, cash_reservations,
share_reservations) and the single-writer lease table, then records schema
version 3.

The tables are created from the canonical SQLAlchemy metadata rather than
frozen inline DDL: Chronos's fail-closed drift checker
(persistence.database._schema_drift) requires byte-level agreement with the
current metadata anyway, so the metadata IS the v3 definition. If a future
revision alters these tables, that revision will carry explicit DDL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_V3_TABLES = (
    "order_intents",
    "order_confirmations",
    "live_arm_events",
    "kill_switch_events",
    "cash_reservations",
    "share_reservations",
    "writer_lease",
)


def upgrade() -> None:
    bind = op.get_bind()
    schema.Base.metadata.create_all(
        bind,
        tables=[schema.Base.metadata.tables[name] for name in _V3_TABLES],
    )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=3, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V3_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 3"))

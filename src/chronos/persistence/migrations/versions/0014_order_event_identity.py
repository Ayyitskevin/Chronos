"""Order identity on the order_events table (BP-2, schema v15).

``order_events`` gains the broker's own identity for an order — ``permanent_id`` (nullable,
indexed) and ``client_id`` (the API client that placed the order) — as first-class columns
written from orderStatus/openOrder evidence, so reconciliation can match a working order by
``permId`` from the persisted events instead of digging the evidence JSON. Column-only, like
0011 and 0013: idempotent on a database whose metadata already carries the columns.

Both columns are NULLABLE and carry no default: a row written before this revision reads
back ``(permanent_id=None, client_id=None)`` — unknown stays unknown, never a synthesized
client ``0`` (0 is a legitimate IB client id) — and the evidence JSON is never back-filled
into the columns. The event writer supplies the ids the callback reported, or None.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_TABLE = "order_events"
_COLUMNS = (
    ("permanent_id", sa.Column("permanent_id", sa.Integer(), nullable=True)),
    ("client_id", sa.Column("client_id", sa.Integer(), nullable=True)),
)
_INDEX = "ix_order_events_permanent_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, column)
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["permanent_id"])
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=15, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    for name, _column in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
    op.get_bind().execute(sa.text("DELETE FROM schema_version WHERE version = 15"))

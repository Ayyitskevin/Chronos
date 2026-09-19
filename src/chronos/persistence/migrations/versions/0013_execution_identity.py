"""Execution identity on the fills table (BP-1, schema v14).

``fills`` gains the broker's own identity for an execution — ``permanent_id`` (nullable,
indexed), ``client_id`` (the API client that placed the order) and ``order_ref`` (Chronos's
intent reference the broker echoed) — so a persisted execution can be matched to the
broker's ``execDetails`` and to the submitted order without a second lookup. Column-only,
like 0011: idempotent on a database whose metadata already carries the columns.

``client_id`` is NOT NULL with a server default of ``0`` for the ALTER (sqlite cannot add a
NOT NULL column without one); nothing wrote ``fills`` before this revision, so no row
receives that default. The repository always supplies the real value.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLE = "fills"
_COLUMNS = (
    ("permanent_id", sa.Column("permanent_id", sa.Integer(), nullable=True)),
    (
        "client_id",
        sa.Column("client_id", sa.Integer(), nullable=False, server_default=sa.text("0")),
    ),
    ("order_ref", sa.Column("order_ref", sa.String(length=120), nullable=True)),
)
_INDEX = "ix_fills_permanent_id"


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
        sa.insert(schema.SchemaVersionRow).values(version=14, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    for name, _column in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
    op.get_bind().execute(sa.text("DELETE FROM schema_version WHERE version = 14"))

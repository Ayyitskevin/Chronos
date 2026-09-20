"""Allocation provenance for every observed position (AP-1, schema v16).

``position_provenance`` records, per persisted reconciliation run (BP-3's row), the origin
class of every position the broker reported — MANAGED (an autonomy binding), WHEEL (a basis
cycle), MANUAL (an operator acknowledgement in the run's decisions) or FOREIGN — with its
evidence link and a stable ``<con_id>:<security_type>:<side>`` key. Append-only, never pruned
(K1(a)); the account fingerprint only, never the raw id (K2(a)); nothing reads it to decide
anything (K3(a)). Table-only, like 0010: idempotent on a database whose metadata already
carries the table.

Revision ID: 0015
Revises: 0014 (BP-2's ``0014_order_event_identity``, schema v15, on main since PR #249).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_V16_TABLES = ("position_provenance",)
_SCHEMA_VERSION = 16


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V16_TABLES if name not in existing]
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
    for name in reversed(_V16_TABLES):
        op.drop_table(name)
    bind.execute(sa.text(f"DELETE FROM schema_version WHERE version = {_SCHEMA_VERSION}"))

"""Schema v8: the proposal queue learns who proposed (ADR-0023).

Revision ID: 0007
Revises: 0006

ADR-0023 derives a proposal's identity from *which credential authenticated*,
which means the route must record that fact durably: identity is stamped at
drain time, on the runtime's tick, and the credential is long gone by then.
This adds a nullable ``proposer_id`` to the queue row — NULL is the
pre-registry posture (local API token, static identity), a value is the
verified registration the route matched.

Altering an existing table rather than creating one, so unlike 0002-0006 this
is explicit DDL: ``create_all`` cannot add a column, and the fail-closed drift
checker will verify the outcome matches the canonical metadata byte-for-byte.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TABLE = "autonomy_proposal_queue"
_COLUMN = "proposer_id"


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN not in existing:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(64), nullable=True))
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=8, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 8"))

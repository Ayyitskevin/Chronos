"""Schema v12: bind queued autonomy work to its credential epoch.

Revision ID: 0011
Revises: 0010

ADR-0048 closes the same-id replacement gap in ADR-0023/ADR-0028. Existing
proposal and bundle rows are preserved with NULL bindings deliberately: the
current registry cannot truthfully reconstruct which historical credential or
entry authenticated them, so drain refuses them rather than inventing provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_BINDING_COLUMNS = ("proposer_credential_epoch", "proposer_registry_entry_digest")
_BOUND_TABLES = ("autonomy_proposal_queue", "autonomy_evidence_bundles")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _BOUND_TABLES:
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in _BINDING_COLUMNS:
            if column not in existing:
                op.add_column(table, sa.Column(column, sa.String(length=64), nullable=True))
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=12, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    for table in reversed(_BOUND_TABLES):
        for column in reversed(_BINDING_COLUMNS):
            op.drop_column(table, column)
    op.get_bind().execute(sa.text("DELETE FROM schema_version WHERE version = 12"))

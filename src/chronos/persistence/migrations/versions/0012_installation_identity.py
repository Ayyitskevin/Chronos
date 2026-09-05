"""Installation identity and recovery acknowledgements (ADR-0054, schema v13).

The upgrade writes the **adoption sentinel**: one identity row whose
``installation_id`` is NULL. It is what tells
``chronos.orders.recovery_hold.resolve_installation`` that this database predates
the cross-store witness, so the next writer boot adopts whatever state-generation
marker is beside it instead of reading its own upgrade as a replaced database.

A database created fresh by ``create_all`` gets no row at all, and must not: a
marker beside a database that has never witnessed one is the replaced-database
case, which is a hold rather than an adoption.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installation_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "recovery_acknowledgements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("binding", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("marker_installation_id", sa.Text(), nullable=False),
        sa.Column("recorded_installation_id", sa.Text(), nullable=False),
        sa.Column("witness_token", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("acknowledged_by", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("binding"),
    )
    # The adoption sentinel. Conservative on purpose: an existing deployment must
    # not boot held merely because this migration ran.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO installation_identity (id, installation_id, first_seen_at) "
            "VALUES (1, NULL, NULL)"
        )
    )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=13, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    op.drop_table("recovery_acknowledgements")
    op.drop_table("installation_identity")
    op.get_bind().execute(sa.text("DELETE FROM schema_version WHERE version = 13"))

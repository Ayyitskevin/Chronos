"""Schema v2 baseline (pre-live-wheel).

Revision ID: 0001
Revises: None

Databases created by Chronos releases at SCHEMA_VERSION 2 are the starting
point of the migration history. This revision is deliberately a no-op: it
exists so an existing v2 database can be stamped here and upgraded forward.
Fresh databases never run alembic at all — ``Database.initialize()`` creates
the current schema directly and records the current version.
"""

from __future__ import annotations

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

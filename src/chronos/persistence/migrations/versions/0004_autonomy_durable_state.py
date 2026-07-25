"""Schema v5: the autonomy supervisor's durable state.

Revision ID: 0004
Revises: 0003

Creates the tables M3 needs so the deterministic supervisor has a memory:

- ``hash_chain_records`` — tamper-evident append-only streams. The JSONL audit
  log has been hash-chained since Phase 9; the database's append-only tables
  were append-only only by convention, which is a statement about the code
  rather than about the data.
- ``autonomy_mandate_activations`` — the owner events that put a mandate in
  force, and revoke it. M2 took activation as a caller-supplied value, so "is
  this mandate active" had no durable answer and could not survive the restart
  that ``RestartBehavior.REQUIRE_REACTIVATION`` exists to govern.
- ``autonomy_session_counters`` — per-session realized loss and activity counts.
  These are what turn ``LossLimits`` and ``ActivityLimits`` from contract-only
  fields into enforced ones.
- ``autonomy_decision_attempts`` — durable R-31 re-submission counters. Holding
  them in memory meant a restart reset the attempt budget, so a model could
  route around a refusal by waiting for one.

As with revisions 0002 and 0003, the tables are created from the canonical
SQLAlchemy metadata rather than frozen inline DDL: Chronos's fail-closed drift
checker (``persistence.database._schema_drift``) requires byte-level agreement
with the current metadata, so the metadata IS the v5 definition.

**This migration only creates tables; it never backfills.** A pre-existing
database upgrades to an empty supervisor memory, which is the correct and
conservative outcome — deny-by-default means an absent counter must read as
"no authority established", never as "no losses yet".
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_V5_TABLES = (
    "hash_chain_records",
    "autonomy_mandate_activations",
    "autonomy_session_counters",
    "autonomy_decision_attempts",
)


def upgrade() -> None:
    bind = op.get_bind()
    schema.Base.metadata.create_all(
        bind,
        tables=[schema.Base.metadata.tables[name] for name in _V5_TABLES],
    )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=5, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V5_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 5"))

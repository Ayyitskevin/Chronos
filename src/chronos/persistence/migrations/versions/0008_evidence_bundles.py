"""Schema v9: the per-job evidence bundle record (ADR-0028 Option C).

Revision ID: 0008
Revises: 0007

ADR-0023 made authorship real and deliberately left evidence uniform: every
proposal from every proposer cited the placeholder bundle ``owner-workspace``
with an honestly-absent digest. The consequence ADR-0028 found is sharper than
"uniform" — admission check 9 compared a constant against itself, because the
decision side and the expectation side were two reads of ``INGRESS_IDENTITY``.
The check could not refuse in any posture, for any proposer.

This table is the record that gives the comparison a side. Each row is one
bundle the backend issued to a specific registered proposer, with the digest of
the exact bytes it served (``backend_served``) or the digest that proposer
attested to having seen (``alert_attested``), and a hard expiry judged at the
drain's clock.

Creating a new table, so as with 0002-0006 the DDL comes from the canonical
metadata — the fail-closed drift checker requires byte-level agreement with the
models, so the metadata is the definition and there is no second spelling of it
here to drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from chronos.persistence import schema

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_V9_TABLES = ("autonomy_evidence_bundles",)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing = [name for name in _V9_TABLES if name not in existing]
    if missing:
        schema.Base.metadata.create_all(
            bind, tables=[schema.Base.metadata.tables[name] for name in missing]
        )
    bind.execute(
        sa.insert(schema.SchemaVersionRow).values(version=9, applied_at=datetime.now(tz=UTC))
    )


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_V9_TABLES):
        op.drop_table(name)
    bind.execute(sa.text("DELETE FROM schema_version WHERE version = 9"))

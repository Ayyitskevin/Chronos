"""Alembic upgrade path: a v2 database reaches exactly the current schema.

The fail-closed drift checker demands byte-level agreement with the live
metadata, so the strongest possible migration test is: build a v2-shaped
database, run ``alembic upgrade head``, then assert ``Database.initialize()``
accepts it (version current, zero drift).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.schema import Base

_V3_TABLES = {
    "order_intents",
    "order_confirmations",
    "live_arm_events",
    "kill_switch_events",
    "cash_reservations",
    "share_reservations",
    "writer_lease",
}

_V4_TABLES = {
    "order_events",
    "risk_decisions",
    "risk_check_results",
}

_V5_TABLES = {
    "hash_chain_records",
    "autonomy_mandate_activations",
    "autonomy_session_counters",
    "autonomy_decision_attempts",
    "autonomy_owner_alerts",
}

_V7_TABLES = {
    "autonomy_proposal_queue",
}

# Frozen v2 baseline (the pre-alembic create_all schema, migration 0001 no-op).
# This set is deliberately hardcoded, NOT derived from Base.metadata, so a table
# added to the models without a migration cannot hide inside it.
_V2_BASELINE_TABLES = {
    "application_events",
    "candidate_evaluations",
    "commissions",
    "database_scope",
    "fills",
    "guardrail_decisions",
    "order_drafts",
    "order_previews",
    "reconciliation_runs",
    "rejected_candidate_reasons",
    "schema_version",
    "strategy_basis_entries",
    "strategy_state",
    "submitted_orders",
    "wheel_cycles",
}

_V9_TABLES = {
    "autonomy_evidence_bundles",
}

# The complete table universe the migration chain accounts for through head.
# Added by revision 0009 (schema v10): durable proposer revocation (A3).
_V10_TABLES = {
    "autonomy_proposer_revocations",
}

# Added by revision 0010 (schema v11): one opening order to one managed position.
_V11_TABLES = {
    "managed_position_bindings",
}

_ALL_MIGRATED_TABLES = (
    _V2_BASELINE_TABLES
    | _V3_TABLES
    | _V4_TABLES
    | _V5_TABLES
    | _V7_TABLES
    | _V9_TABLES
    | _V10_TABLES
    | _V11_TABLES
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_v2_database(path: Path) -> None:
    """Create a database shaped like schema v2 (current metadata minus v3/v4)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    post_v2 = (
        _V3_TABLES | _V4_TABLES | _V5_TABLES | _V7_TABLES | _V9_TABLES | _V10_TABLES | _V11_TABLES
    )
    v2_tables = [table for name, table in Base.metadata.tables.items() if name not in post_v2]
    Base.metadata.create_all(engine, tables=v2_tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (2, '2026-01-01 00:00:00.000000')"
            )
        )
    engine.dispose()


def _make_v3_database(path: Path) -> None:
    """Create a database shaped like schema v3 (current metadata minus v4)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    post_v3 = _V4_TABLES | _V5_TABLES | _V7_TABLES | _V9_TABLES | _V10_TABLES | _V11_TABLES
    v3_tables = [table for name, table in Base.metadata.tables.items() if name not in post_v3]
    Base.metadata.create_all(engine, tables=v3_tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (3, '2026-01-01 00:00:00.000000')"
            )
        )
    engine.dispose()


def _make_v4_database(path: Path) -> None:
    """Create a database shaped like schema v4 (current metadata minus v5)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    post_v4 = _V5_TABLES | _V7_TABLES | _V9_TABLES | _V10_TABLES | _V11_TABLES
    v4_tables = [table for name, table in Base.metadata.tables.items() if name not in post_v4]
    Base.metadata.create_all(engine, tables=v4_tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (4, '2026-01-01 00:00:00.000000')"
            )
        )
    engine.dispose()


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_REPO_ROOT / "src" / "chronos" / "persistence" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_v2_database_upgrades_to_current_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "chronos.db"
    _make_v2_database(db_path)

    config = _alembic_config(db_path)
    command.stamp(config, "0001")  # v2 == baseline revision
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert tables >= _V3_TABLES | _V4_TABLES
    with engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert version == SCHEMA_VERSION

    # The decisive check: the fail-closed initializer accepts the migrated DB
    # (correct version AND zero drift against the live metadata). The
    # alembic_version bookkeeping table must not trip the drift checker.
    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_v3_database_upgrades_to_current_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "chronos.db"
    _make_v3_database(db_path)

    config = _alembic_config(db_path)
    command.stamp(config, "0002")  # v3 == revision 0002
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = set(sa.inspect(engine).get_table_names())
    assert tables >= _V4_TABLES
    with engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_v4_database_upgrades_to_current_schema(tmp_path: Path) -> None:
    """A pre-M3 database gains the supervisor's durable state, empty.

    Empty is the correct outcome: deny-by-default means an absent loss counter
    must read as "no authority established", never as "no losses yet". The
    migration deliberately does not backfill.
    """

    db_path = tmp_path / "chronos.db"
    _make_v4_database(db_path)

    config = _alembic_config(db_path)
    command.stamp(config, "0003")  # v4 == revision 0003
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = set(sa.inspect(engine).get_table_names())
    assert tables >= _V5_TABLES | _V7_TABLES | _V9_TABLES | _V10_TABLES | _V11_TABLES
    with engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
        for table in sorted(_V5_TABLES):
            populated = connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
            assert populated is None, f"{table} was backfilled; it must upgrade empty"
    engine.dispose()
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_v7_database_gains_the_proposer_column_and_keeps_its_rows(tmp_path: Path) -> None:
    """Migration 0007's DDL, exercised against the TRUE pre-0007 table shape.

    Every other fixture in this file builds tables from the *current* metadata,
    so once the model carries ``proposer_id`` the earlier upgrade paths never
    reach 0007's ``add_column`` — its column-existence guard skips it. A real
    production v7 database is the one shape those tests cannot represent, so
    this builds it explicitly: current schema, then the column dropped, then
    the upgrade — proving the column is added, a queued row survives with the
    honest NULL (pre-registry, ADR-0023), and the fail-closed initializer
    accepts the result byte-for-byte.
    """

    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("ALTER TABLE autonomy_proposal_queue DROP COLUMN proposer_id"))
        connection.execute(
            sa.text(
                "INSERT INTO autonomy_proposal_queue "
                "(account_fingerprint, payload, received_at, status, cycle_stage, refusal) "
                "VALUES ('f' , '{}', '2026-01-01 00:00:00.000000', 'PENDING', '', '')"
            )
        )
        for version in range(2, 8):
            connection.execute(
                sa.text(
                    "INSERT INTO schema_version (version, applied_at) "
                    f"VALUES ({version}, '2026-01-01 00:00:00.000000')"
                )
            )
    engine.dispose()

    config = _alembic_config(db_path)
    command.stamp(config, "0006")  # v7 == revision 0006
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    columns = {
        column["name"]: column
        for column in sa.inspect(engine).get_columns("autonomy_proposal_queue")
    }
    assert "proposer_id" in columns, "0007 must add the column to a genuine v7 table"
    assert columns["proposer_id"]["nullable"] is True
    with engine.connect() as connection:
        surviving = connection.execute(
            sa.text("SELECT proposer_id, status FROM autonomy_proposal_queue")
        ).all()
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert surviving == [(None, "PENDING")], (
        "the pre-upgrade row must survive with proposer_id NULL — the pre-registry "
        "posture, never a guessed identity"
    )
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_v8_database_gains_the_evidence_bundle_table_and_keeps_its_rows(tmp_path: Path) -> None:
    """Migration 0008's DDL, exercised against the TRUE pre-0008 database shape.

    The same reasoning as the v7 test above, one version on. Every other fixture
    in this file derives its tables from the *current* metadata minus a
    hardcoded later-set, so those paths reach 0008 with a database that never
    looked like a real production v8 — in particular they carry no v8 row
    history to lose. A real v8 database is the shape that matters: every earlier
    table present and populated, ``proposer_id`` already on the queue, schema
    versions 2..8 recorded, and only ``autonomy_evidence_bundles`` missing.

    So this builds exactly that, then upgrades — proving the table is created,
    that it arrives **empty** (deny-by-default: an absent bundle record must read
    as "nothing was issued", never as a bundle that happens to have no rows
    against it), that the pre-existing queue row survives untouched, and that the
    fail-closed initializer accepts the result byte-for-byte against the live
    metadata.
    """

    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE autonomy_evidence_bundles"))
        connection.execute(
            sa.text(
                "INSERT INTO autonomy_proposal_queue "
                "(account_fingerprint, payload, received_at, status, cycle_stage, refusal, "
                "proposer_id) "
                "VALUES ('f', '{}', '2026-01-01 00:00:00.000000', 'PENDING', '', '', "
                "'claude-worker')"
            )
        )
        for version in range(2, 9):
            connection.execute(
                sa.text(
                    "INSERT INTO schema_version (version, applied_at) "
                    f"VALUES ({version}, '2026-01-01 00:00:00.000000')"
                )
            )
    engine.dispose()

    config = _alembic_config(db_path)
    command.stamp(config, "0007")  # v8 == revision 0007
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "autonomy_evidence_bundles" in set(inspector.get_table_names()), (
        "0008 must create the table on a genuine v8 database"
    )
    columns = {column["name"] for column in inspector.get_columns("autonomy_evidence_bundles")}
    assert columns == {
        "id",
        "account_fingerprint",
        "bundle_id",
        "proposer_id",
        "kind",
        "digest",
        "bundle_version",
        "issued_at",
        "expires_at",
    }
    with engine.connect() as connection:
        issued = connection.execute(
            sa.text("SELECT 1 FROM autonomy_evidence_bundles LIMIT 1")
        ).first()
        surviving = connection.execute(
            sa.text("SELECT proposer_id, status FROM autonomy_proposal_queue")
        ).all()
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert issued is None, (
        "the evidence-bundle table must upgrade empty; a backfilled row would be a "
        "bundle nobody issued, which is the exact claim this protocol exists to refuse"
    )
    assert surviving == [("claude-worker", "PENDING")], (
        "the pre-upgrade queue row and its ADR-0023 authorship must survive 0008 intact"
    )
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_v9_database_gains_the_revocation_table_and_keeps_its_rows(tmp_path: Path) -> None:
    """A genuine pre-A3 database reaches head and can be revoked against (A3).

    Same reasoning as the v7 and v8 tests above, one version on: every other
    fixture derives its tables from the *current* metadata, so once the model
    carries ``autonomy_proposer_revocations`` those paths reach 0009 with the
    table already present and its existence guard skips the DDL. A real v9
    database is the shape they cannot represent — every earlier table present,
    versions 2..9 recorded, and only the revocation ledger missing.

    The surviving queue row matters as much as the new table. An operator
    upgrading mid-incident has proposals in flight, and a migration that
    silently discarded them would lose exactly the evidence of what the leaked
    credential had been doing.
    """

    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE autonomy_proposer_revocations"))
        connection.execute(
            sa.text(
                "INSERT INTO autonomy_proposal_queue "
                "(account_fingerprint, payload, received_at, status, cycle_stage, refusal, "
                "proposer_id) "
                "VALUES ('f', '{}', '2026-01-01 00:00:00.000000', 'PENDING', '', '', "
                "'claude-worker')"
            )
        )
        for version in range(2, 10):
            connection.execute(
                sa.text(
                    "INSERT INTO schema_version (version, applied_at) "
                    f"VALUES ({version}, '2026-01-01 00:00:00.000000')"
                )
            )
    engine.dispose()

    config = _alembic_config(db_path)
    command.stamp(config, "0008")  # v9 == revision 0008
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    assert "autonomy_proposer_revocations" in set(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        surviving = connection.execute(
            sa.text("SELECT proposer_id, status FROM autonomy_proposal_queue")
        ).all()
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert surviving == [("claude-worker", "PENDING")]
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
        # The upgraded database is immediately usable for the act it exists for.
        from chronos.supervisor.revocation import is_revoked, revoke

        with database.sessions.begin() as session:
            assert revoke(
                session,
                proposer_id="claude-worker",
                secret_sha256="a" * 64,
                reason="credential pasted into a public issue",
                now=datetime(2026, 8, 13, tzinfo=UTC),
            )
        with database.sessions.begin() as session:
            assert is_revoked(session, secret_sha256="a" * 64)
    finally:
        database.dispose()


def test_v10_database_gains_managed_position_bindings_empty(tmp_path: Path) -> None:
    """A real schema-v10 database gains only the empty admission binding table."""

    db_path = tmp_path / "chronos.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE managed_position_bindings"))
        for version in range(2, 11):
            connection.execute(
                sa.text(
                    "INSERT INTO schema_version (version, applied_at) "
                    f"VALUES ({version}, '2026-01-01 00:00:00.000000')"
                )
            )
    engine.dispose()

    config = _alembic_config(db_path)
    command.stamp(config, "0009")
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "managed_position_bindings" in set(inspector.get_table_names())
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("managed_position_bindings")
    }
    assert ("account_fingerprint", "permanent_id") in unique_columns
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT 1 FROM managed_position_bindings LIMIT 1")).first()
            is None
        )
        version = connection.execute(
            sa.text("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        ).scalar()
    engine.dispose()
    assert version == SCHEMA_VERSION

    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()


def test_fresh_database_needs_no_alembic(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        database.initialize()
        inspector = sa.inspect(database.engine)
        assert (
            set(inspector.get_table_names())
            >= _V3_TABLES
            | _V4_TABLES
            | _V5_TABLES
            | _V7_TABLES
            | _V9_TABLES
            | _V10_TABLES
            | _V11_TABLES
        )
    finally:
        database.dispose()


def test_models_have_no_untracked_tables() -> None:
    """The models must match the frozen migration manifest exactly (M8c).

    Adding or removing a table in the ORM models fails here until the frozen
    ``_ALL_MIGRATED_TABLES`` manifest is updated — the human prompt to write the
    matching migration. Pairs with the upgrade test below, which proves the
    chain actually builds that manifest.
    """

    assert set(Base.metadata.tables) == _ALL_MIGRATED_TABLES


def _make_frozen_v2_database(path: Path) -> None:
    """Create ONLY the hardcoded v2 baseline tables (no current-metadata deriv)."""

    engine = sa.create_engine(f"sqlite:///{path}")
    tables = [Base.metadata.tables[name] for name in _V2_BASELINE_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (2, '2026-01-01 00:00:00.000000')"
            )
        )
    engine.dispose()


def test_migration_chain_builds_exactly_the_current_models(tmp_path: Path) -> None:
    """The migration chain, applied to a FROZEN v2 baseline, must reproduce the
    current model TABLE SET exactly (M8c).

    Scope and honest limits (see docs/limitations.md):
    - Catches the naive missing-migration case: a table added to the models
      (and to the frozen ``_V2_BASELINE_TABLES``/manifest so the manifest test
      passes) without a matching migration is absent from the chain, so the
      result set diverges and this fails. If a dev instead adds the table ONLY
      to ``_V2_BASELINE_TABLES`` and not to a migration, the baseline fixture
      creates it and this cannot tell — the guard trusts the frozen baseline,
      which has no independent v2-schema source of truth (0001 is a no-op).
    - Table-name level only: a missing ADD COLUMN/index/constraint migration is
      NOT caught here (the baseline tables are built from current metadata, so
      they already carry the new column). Add such migrations explicitly.
    """

    db_path = tmp_path / "chronos.db"
    _make_frozen_v2_database(db_path)

    config = _alembic_config(db_path)
    command.stamp(config, "0001")
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = set(sa.inspect(engine).get_table_names())
    engine.dispose()
    tables.discard("alembic_version")  # alembic's own bookkeeping, not a model

    assert tables == set(Base.metadata.tables)

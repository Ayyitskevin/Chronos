"""Alembic upgrade path: a v2 database reaches exactly the current schema.

The fail-closed drift checker demands byte-level agreement with the live
metadata, so the strongest possible migration test is: build a v2-shaped
database, run ``alembic upgrade head``, then assert ``Database.initialize()``
accepts it (version current, zero drift).
"""

from __future__ import annotations

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

# The complete table universe the migration chain accounts for through head.
_ALL_MIGRATED_TABLES = _V2_BASELINE_TABLES | _V3_TABLES | _V4_TABLES | _V5_TABLES

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_v2_database(path: Path) -> None:
    """Create a database shaped like schema v2 (current metadata minus v3/v4)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    post_v2 = _V3_TABLES | _V4_TABLES | _V5_TABLES
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
    post_v3 = _V4_TABLES | _V5_TABLES
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
    v4_tables = [table for name, table in Base.metadata.tables.items() if name not in _V5_TABLES]
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
    assert tables >= _V5_TABLES
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


def test_fresh_database_needs_no_alembic(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        database.initialize()
        inspector = sa.inspect(database.engine)
        assert set(inspector.get_table_names()) >= _V3_TABLES | _V4_TABLES | _V5_TABLES
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

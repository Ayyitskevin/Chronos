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
_ALL_MIGRATED_TABLES = _V2_BASELINE_TABLES | _V3_TABLES | _V4_TABLES

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_v2_database(path: Path) -> None:
    """Create a database shaped like schema v2 (current metadata minus v3/v4)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    post_v2 = _V3_TABLES | _V4_TABLES
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
    v3_tables = [table for name, table in Base.metadata.tables.items() if name not in _V4_TABLES]
    Base.metadata.create_all(engine, tables=v3_tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (3, '2026-01-01 00:00:00.000000')"
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


def test_fresh_database_needs_no_alembic(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        database.initialize()
        inspector = sa.inspect(database.engine)
        assert set(inspector.get_table_names()) >= _V3_TABLES | _V4_TABLES
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
    current model table set exactly — the definitive 'no pending/missing
    migration' check (M8c).

    Unlike the v2/v3 upgrade tests (whose fixtures are derived from current
    metadata, so a new table hides in the fixture), this starts from a hardcoded
    baseline: a table added to the models without a migration is absent from both
    the baseline and the chain, so the result set diverges and this fails.
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

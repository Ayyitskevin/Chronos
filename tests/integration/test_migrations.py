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

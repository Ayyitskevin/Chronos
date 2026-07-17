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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_v2_database(path: Path) -> None:
    """Create a database shaped like schema v2 (current metadata minus v3)."""

    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    v2_tables = [table for name, table in Base.metadata.tables.items() if name not in _V3_TABLES]
    Base.metadata.create_all(engine, tables=v2_tables)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (2, '2026-01-01 00:00:00.000000')"
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
    assert tables >= _V3_TABLES
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


def test_fresh_database_needs_no_alembic(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        database.initialize()
        inspector = sa.inspect(database.engine)
        assert set(inspector.get_table_names()) >= _V3_TABLES
    finally:
        database.dispose()

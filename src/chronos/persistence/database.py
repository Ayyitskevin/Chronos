"""SQLite engine creation and explicit schema initialization."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from chronos.persistence.schema import Base, SchemaVersionRow

SCHEMA_VERSION = 1


class Database:
    """Own the SQLAlchemy engine and enforce the supported schema version."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._prepare_sqlite_parent(url)
        engine_kwargs: dict[str, object] = {}
        if url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        if url.endswith(":memory:"):
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as session:
            current = session.scalar(select(SchemaVersionRow).order_by(SchemaVersionRow.id.desc()))
            if current is None:
                session.add(SchemaVersionRow(version=SCHEMA_VERSION))
            elif current.version != SCHEMA_VERSION:
                raise RuntimeError(
                    "Unsupported Chronos schema version "
                    f"{current.version}; expected {SCHEMA_VERSION}"
                )

    def dispose(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _prepare_sqlite_parent(url: str) -> None:
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            return
        path_text = url.removeprefix(prefix)
        if not path_text or path_text == ":memory:":
            return
        Path(path_text).expanduser().parent.mkdir(parents=True, exist_ok=True)

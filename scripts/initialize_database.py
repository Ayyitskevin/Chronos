"""Create or verify the current Chronos SQLite schema."""

from __future__ import annotations

import argparse

from chronos.config.settings import get_settings
from chronos.persistence.database import SCHEMA_VERSION, Database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Override DATABASE_URL for this initialization")
    arguments = parser.parse_args()
    database = Database(arguments.url or get_settings().database_url)
    try:
        database.initialize()
    finally:
        database.dispose()
    print(f"Chronos schema version {SCHEMA_VERSION} is initialized.")


if __name__ == "__main__":
    main()

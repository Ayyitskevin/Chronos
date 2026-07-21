#!/usr/bin/env python
"""Read-only paper-validation soak report (Milestone 5).

Summarizes the persisted order-management activity of a bound Chronos database:
lifecycle status counts, submission/fill/cancel/modify tallies,
SUBMISSION_UNKNOWN resolutions, and a risk-check FAIL/UNKNOWN histogram. It
opens the database read-only and places NO orders and no order-writing path —
it is purely an after-the-fact view of what the paper session did.

Usage::

    python scripts/paper_soak_report.py            # uses DATABASE_URL / .env
    python scripts/paper_soak_report.py --database sqlite:///data/chronos.db
"""

from __future__ import annotations

import argparse
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from chronos.config.settings import get_settings
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.schema import (
    OrderEventRow,
    OrderIntentRow,
    RiskCheckResultRow,
)


@dataclass(frozen=True)
class SoakReport:
    total_intents: int
    status_counts: dict[str, int]
    event_source_counts: dict[str, int]
    submission_unknown_resolutions: int
    risk_check_failures: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            "Chronos paper soak report",
            "=========================",
            f"order intents: {self.total_intents}",
            "",
            "lifecycle status:",
        ]
        lines.extend(
            f"  {status:<20} {count}" for status, count in sorted(self.status_counts.items())
        )
        lines.append("")
        lines.append("order events by source:")
        lines.extend(
            f"  {source:<20} {count}" for source, count in sorted(self.event_source_counts.items())
        )
        lines.append("")
        lines.append(
            f"SUBMISSION_UNKNOWN reconciliation resolutions: {self.submission_unknown_resolutions}"
        )
        lines.append("")
        lines.append("risk-check FAIL/UNKNOWN histogram:")
        if self.risk_check_failures:
            lines.extend(
                f"  {name:<32} {count}"
                for name, count in sorted(
                    self.risk_check_failures.items(), key=lambda item: (-item[1], item[0])
                )
            )
        else:
            lines.append("  (none)")
        return "\n".join(lines)


class PaperSoakDatabaseUnavailable(RuntimeError):
    """Sanitized refusal for a database that cannot be audited read-only."""


def build_read_only_sqlite_soak_report(database_url: str) -> SoakReport:
    """Read an existing Chronos SQLite database without creating or migrating it.

    The audit surface intentionally supports only local SQLite. Remote database
    URLs are refused before any engine or socket can be constructed. SQLite is
    opened with mode=ro and query_only; schema/version/integrity checks and all
    report queries run on that same read-only connection.
    """

    path = _existing_sqlite_path(database_url)
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise PaperSoakDatabaseUnavailable(
            "existing SQLite audit target could not be opened read-only"
        ) from error
    try:
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise PaperSoakDatabaseUnavailable("SQLite query-only enforcement is unavailable")
        _validate_read_only_sqlite(connection)
        return _build_sqlite_soak_report(connection)
    except sqlite3.Error as error:
        raise PaperSoakDatabaseUnavailable(
            "existing SQLite audit target failed schema or report queries"
        ) from error
    finally:
        connection.close()


def _existing_sqlite_path(database_url: str) -> Path:
    configured = make_url(database_url)
    if configured.get_backend_name() != "sqlite":
        raise PaperSoakDatabaseUnavailable(
            "paper soak audit supports existing local SQLite databases only"
        )
    if configured.query:
        raise PaperSoakDatabaseUnavailable("paper soak audit refuses SQLite URL query parameters")
    database = configured.database
    if not database or database == ":memory:":
        raise PaperSoakDatabaseUnavailable(
            "paper soak audit requires an existing file-backed SQLite database"
        )
    path = Path(database).expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise PaperSoakDatabaseUnavailable("paper soak audit target does not exist") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise PaperSoakDatabaseUnavailable(
            "paper soak audit target must be a regular file, not a link or special file"
        )
    return path.resolve(strict=True)


def _validate_read_only_sqlite(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA quick_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        raise PaperSoakDatabaseUnavailable("SQLite integrity check did not report ok")
    try:
        version_row = connection.execute(
            "SELECT version FROM schema_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise PaperSoakDatabaseUnavailable("Chronos schema_version is unavailable") from error
    if version_row is None or int(version_row[0]) != SCHEMA_VERSION:
        raise PaperSoakDatabaseUnavailable("Chronos schema version is missing or unsupported")


def _build_sqlite_soak_report(connection: sqlite3.Connection) -> SoakReport:
    total_row = connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()
    total = int(total_row[0]) if total_row is not None else 0

    status_counts: Counter[str] = Counter()
    for status, count in connection.execute(
        "SELECT status, COUNT(*) FROM order_intents GROUP BY status"
    ):
        status_counts[str(status)] = int(count)

    source_counts: Counter[str] = Counter()
    for source, count in connection.execute(
        "SELECT source, COUNT(*) FROM order_events GROUP BY source"
    ):
        source_counts[str(source)] = int(count)

    resolution_row = connection.execute(
        "SELECT COUNT(*) FROM order_events WHERE source = ? AND from_status = ?",
        ("RECONCILE", "SUBMISSION_UNKNOWN"),
    ).fetchone()
    resolutions = int(resolution_row[0]) if resolution_row is not None else 0

    risk_failures: Counter[str] = Counter()
    for name, count in connection.execute(
        "SELECT check_name, COUNT(*) FROM risk_check_results "
        "WHERE status IN (?, ?) GROUP BY check_name",
        ("FAIL", "UNKNOWN"),
    ):
        risk_failures[str(name)] = int(count)

    return SoakReport(
        total_intents=total,
        status_counts=dict(status_counts),
        event_source_counts=dict(source_counts),
        submission_unknown_resolutions=resolutions,
        risk_check_failures=dict(risk_failures),
    )


def build_soak_report(database: Database) -> SoakReport:
    with database.sessions() as session:
        total = int(session.scalar(select(func.count()).select_from(OrderIntentRow)) or 0)
        status_counts: Counter[str] = Counter()
        for status, count in session.execute(
            select(OrderIntentRow.status, func.count()).group_by(OrderIntentRow.status)
        ):
            status_counts[str(status)] = int(count)

        source_counts: Counter[str] = Counter()
        for source, count in session.execute(
            select(OrderEventRow.source, func.count()).group_by(OrderEventRow.source)
        ):
            source_counts[str(source)] = int(count)

        resolutions = int(
            session.scalar(
                select(func.count())
                .select_from(OrderEventRow)
                .where(OrderEventRow.source == "RECONCILE")
                .where(OrderEventRow.from_status == "SUBMISSION_UNKNOWN")
            )
            or 0
        )

        risk_failures: Counter[str] = Counter()
        for name, count in session.execute(
            select(RiskCheckResultRow.check_name, func.count())
            .where(RiskCheckResultRow.status.in_(("FAIL", "UNKNOWN")))
            .group_by(RiskCheckResultRow.check_name)
        ):
            risk_failures[str(name)] = int(count)

    return SoakReport(
        total_intents=total,
        status_counts=dict(status_counts),
        event_source_counts=dict(source_counts),
        submission_unknown_resolutions=resolutions,
        risk_check_failures=dict(risk_failures),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Chronos paper soak report (read-only)")
    parser.add_argument(
        "--database",
        default=None,
        help="SQLAlchemy database URL (defaults to the configured DATABASE_URL)",
    )
    args = parser.parse_args()
    url = args.database or get_settings().database_url
    report = build_read_only_sqlite_soak_report(url)
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

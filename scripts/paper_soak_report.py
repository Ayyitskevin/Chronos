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
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select

from chronos.config.settings import get_settings
from chronos.persistence.database import Database
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
    database = Database(url)
    try:
        database.initialize()
        report = build_soak_report(database)
    finally:
        database.dispose()
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

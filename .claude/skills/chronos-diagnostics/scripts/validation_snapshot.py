"""Read the latest dated validation evidence from ``docs/TEST_RESULTS.md``.

The diagnostics scripts use this small parser instead of carrying their own
test-count snapshots. Missing, malformed, or internally inconsistent evidence
returns ``None`` so callers can fail visibly without inventing a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_CURRENT_HEADING = re.compile(
    r"^## Summary \(current [—-] re-measured (?P<measured_on>\d{4}-\d{2}-\d{2})\)\s*$",
    re.MULTILINE,
)
_NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)
_COMMIT = re.compile(r"Measured on exact `main` `(?P<sha>[0-9a-f]{40})`")
_PYTEST_RESULT = re.compile(
    r"\|\s*`pytest -q`\s*\|\s*\*\*"
    r"(?P<passed>[\d,]+) passed,\s*"
    r"(?P<skipped>[\d,]+) skipped"
    r"(?:,[^*]*)?\*\*\s*"
    r"\((?P<collected>[\d,]+) collected\)"
)


@dataclass(frozen=True)
class ValidationSnapshot:
    """One coherent current-summary measurement."""

    measured_on: date
    commit_sha: str
    passed: int
    skipped: int
    collected: int

    def describe(self) -> str:
        """Return a compact human-readable comparison label."""
        return (
            f"{self.collected} collected / {self.passed} passed, "
            f"{self.skipped} skipped ({self.measured_on.isoformat()} "
            f"at {self.commit_sha[:12]})"
        )


def read_validation_snapshot(path: Path) -> ValidationSnapshot | None:
    """Parse the first explicitly current summary, or return ``None``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    heading = _CURRENT_HEADING.search(text)
    if heading is None:
        return None
    next_heading = _NEXT_HEADING.search(text, heading.end())
    section = text[heading.end() : next_heading.start() if next_heading else None]

    commit = _COMMIT.search(section)
    pytest_result = _PYTEST_RESULT.search(section)
    if commit is None or pytest_result is None:
        return None

    try:
        measured_on = date.fromisoformat(heading.group("measured_on"))
        passed = int(pytest_result.group("passed").replace(",", ""))
        skipped = int(pytest_result.group("skipped").replace(",", ""))
        collected = int(pytest_result.group("collected").replace(",", ""))
    except ValueError:
        return None
    if passed + skipped != collected:
        return None

    return ValidationSnapshot(
        measured_on=measured_on,
        commit_sha=commit.group("sha"),
        passed=passed,
        skipped=skipped,
        collected=collected,
    )

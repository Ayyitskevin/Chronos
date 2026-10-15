"""N3 cutover archive — RED stub (N3-2). The fix commit replaces this file.

The public names exist so the RED tests import; every one of them refuses.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from chronos.operations.restore_drill import AgeKeys, Clock

MAX_RECONCILIATION_AGE = timedelta(hours=1)
RUNBOOK = "docs/ops/N3-CUTOVER.md"


class CutoverRefused(RuntimeError):
    """A typed refusal: ``code`` names the check, the message is the whole diagnosis."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def flatten_gate(db_path: Path, *, now: datetime) -> object:
    raise NotImplementedError("N3-2 red")


def archive(
    db_path: Path,
    out_dir: Path,
    restore_into: Path,
    *,
    apply: bool,
    clock: Clock | None = None,
    keys: AgeKeys | None = None,
) -> object:
    raise NotImplementedError("N3-2 red")


def main(argv: list[str] | None = None, *, clock: Clock | None = None) -> int:
    raise NotImplementedError("N3-2 red")


if __name__ == "__main__":
    sys.exit(main())

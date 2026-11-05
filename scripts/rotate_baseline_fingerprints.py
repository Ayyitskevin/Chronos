"""Rotate reviewed matrix fingerprints from committed parent baselines only."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Fail closed until the regression contract is implemented."""
    print("rotation refused: implementation pending", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

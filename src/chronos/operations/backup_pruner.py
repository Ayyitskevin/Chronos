"""Backup pruner stub (RED commit): the API the tests pin, with no behaviour."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

RETENTION_DAYS = 90
EXIT_NOTHING_TO_DO = 0
EXIT_REFUSED = 2
EXIT_WOULD_PRUNE = 10
EXIT_PRUNED = 11


def main(argv: list[str] | None = None, *, clock: Callable[[], datetime] | None = None) -> int:
    raise NotImplementedError


def build_parser() -> argparse.ArgumentParser:
    raise NotImplementedError


def prune(root: Path, *, now: datetime, apply: bool) -> dict[str, object]:
    raise NotImplementedError

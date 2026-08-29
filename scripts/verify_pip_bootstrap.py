"""Verify that the active interpreter uses the pip identity in the bootstrap lock."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]


class PipBootstrapError(RuntimeError):
    """The bootstrap lock or installed pip identity is invalid."""


VersionGetter = Callable[[str], str]


def locked_pip_version(lock: Path) -> str:
    """Return the sole exact pip version declared by an uv hash lock."""

    requirements: list[tuple[str, str]] = []
    for raw_line in lock.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ]+)", line)
        if match is None:
            raise PipBootstrapError(f"unsupported bootstrap-lock line: {raw_line!r}")
        requirements.append((match.group(1).lower(), match.group(2)))

    if len(requirements) != 1 or requirements[0][0] != "pip":
        raise PipBootstrapError(
            f"bootstrap lock must contain exactly one pip requirement, observed {requirements!r}"
        )
    return requirements[0][1]


def verify_pip_bootstrap(
    lock: Path,
    *,
    version_getter: VersionGetter = metadata.version,
) -> str:
    """Require the active environment's pip distribution to match the lock."""

    expected = locked_pip_version(lock)
    try:
        observed = version_getter("pip")
    except metadata.PackageNotFoundError as error:
        raise PipBootstrapError("pip is not installed in the active environment") from error
    if observed != expected:
        raise PipBootstrapError(f"pip bootstrap drift: expected {expected}, observed {observed}")
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=_REPO_ROOT / "requirements-bootstrap.lock",
        help="exact hash lock that owns the expected pip identity",
    )
    parser.add_argument(
        "--check-lock-only",
        action="store_true",
        help="validate the bootstrap lock before allowing pip to consume it",
    )
    arguments = parser.parse_args()
    try:
        if arguments.check_lock_only:
            version = locked_pip_version(arguments.lock)
        else:
            version = verify_pip_bootstrap(arguments.lock)
    except (OSError, PipBootstrapError) as error:
        print(f"pip bootstrap verification failed: {error}", file=sys.stderr)
        return 1
    if arguments.check_lock_only:
        print(f"pip bootstrap lock verified: pip {version}")
        return 0
    print(f"pip bootstrap verified: pip {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the opt-in IBKR smoke test with every order-transmission flag forced off.

Usage: smoke_test_ibkr.py [official_ibkr|ib_async]  (default: official_ibkr)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    adapter = sys.argv[1] if len(sys.argv) > 1 else "official_ibkr"
    if adapter not in ("official_ibkr", "ib_async"):
        raise SystemExit(f"unknown adapter {adapter!r}; use official_ibkr or ib_async")
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "CHRONOS_RUN_IBKR_SMOKE": "1",
            "BROKER_MODE": "ibkr",
            "BROKER_ADAPTER": adapter,
            "ALLOW_ORDER_TRANSMIT": "false",
            "ALLOW_LIVE_TRADING": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "ibkr",
        "tests/integration/test_ibkr_smoke.py",
        "--maxfail=1",
        "-ra",
    ]
    print("Running the bounded IBKR read-only smoke test; all Chronos order transmission is off.")
    result = subprocess.run(command, cwd=repository_root, env=environment, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

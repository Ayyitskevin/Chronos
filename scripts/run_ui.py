"""Run the Chronos Streamlit UI (the backend-driven client).

Start the backend first (scripts/run_backend.py); this UI talks to it over
loopback HTTP. The legacy in-process app remains at src/chronos/app.py until
Milestone 5 removes it.

Usage:

    .venv/bin/python scripts/run_ui.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "src" / "chronos" / "ui" / "backend_app.py"),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

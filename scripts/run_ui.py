"""Run the Chronos Streamlit UI.

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
            str(ROOT / "src" / "chronos" / "app.py"),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

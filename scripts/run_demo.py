"""Launch Chronos with deterministic demo data and transmission forced off."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "BROKER_MODE": "demo",
            "ALLOW_ORDER_TRANSMIT": "false",
            "ALLOW_LIVE_TRADING": "false",
        }
    )
    app_path = Path(__file__).resolve().parents[1] / "src" / "chronos" / "app.py"
    result = subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_path)],
        check=False,
        env=environment,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

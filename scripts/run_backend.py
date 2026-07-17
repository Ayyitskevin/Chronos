"""Run the Chronos backend (FastAPI) on loopback.

The backend owns the only broker connection and, from Milestone 5 on, the
only order-writing authority. Usage:

    .venv/bin/python scripts/run_backend.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn

from chronos.api.main import create_app
from chronos.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(),
        host=settings.backend_host,
        port=settings.backend_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

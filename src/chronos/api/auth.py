"""Local API token for the backend (loopback-only defense in depth).

The backend binds to 127.0.0.1, but every endpoint except ``/health`` still
requires a random per-installation token so another local process cannot
drive the order-writing service by accident. The token is generated on first
startup, stored owner-only on disk, never logged, and is deliberately
separate from any future live-arm token (which will live only in backend
memory — docs/LIVE_WHEEL_GAME_PLAN.md Milestone 6).
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status

from chronos.utils.secure_files import secure_owner_only

_TOKEN_HEADER = "X-Chronos-Token"
_TOKEN_BYTES = 32


def load_or_create_token(path: Path) -> str:
    """Return the local API token, generating and securing it on first use."""

    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(_TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    secure_owner_only(path)
    return token


def require_token(request: Request) -> None:
    """FastAPI dependency: constant-time check of the local API token."""

    expected: str | None = getattr(request.app.state, "api_token", None)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend API token is not initialized",
        )
    presented = request.headers.get(_TOKEN_HEADER, "")
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {_TOKEN_HEADER} header",
        )


TokenRequired = Depends(require_token)

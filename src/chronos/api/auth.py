"""Local API token for the backend (loopback-only defense in depth).

The backend binds to 127.0.0.1, but every endpoint except ``/health`` still
requires a random per-installation token so another local process cannot
drive the order-writing service by accident. The token is generated on first
startup, stored owner-only on disk, never logged, and is deliberately
separate from any future live-arm token (which will live only in backend
memory — docs/LIVE_WHEEL_GAME_PLAN.md Milestone 6).

From ADR-0023 this module also owns the **proposer credential** check for
``POST /autonomy/proposals``. The two credentials are deliberately disjoint:

- the local API token opens every route *except* proposals once a proposer
  registry is configured;
- a proposer credential opens proposals *only* — every other route checks the
  local token, which a proposer credential is not, so presenting it anywhere
  else is a 401.

That asymmetry is the point. A leaked proposer credential is worth exactly one
thing: submitting proposals that still face the entire gate stack under the
identity the owner registered for it. It cannot read positions, cancel orders,
arm live trading, or acknowledge alerts.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status

from chronos.supervisor.proposers import ProposerRegistry, load_proposer_registry
from chronos.utils.secure_files import secure_owner_only
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.auth")

_TOKEN_HEADER = "X-Chronos-Token"
_TOKEN_BYTES = 32

#: The header a registered proposer presents. Distinct from ``X-Chronos-Token``
#: so the two credentials cannot be confused for one another at any boundary.
_PROPOSER_HEADER = "X-Chronos-Proposer-Token"


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


@dataclass(frozen=True, slots=True)
class ProposerAuth:
    """What startup learned about the proposer registry (ADR-0023).

    Three states, each explicit because each refuses differently:

    - ``configured=False`` — no registry file named. Proposals authenticate
      exactly as every other route does (local API token): the pre-registry
      posture, unchanged.
    - ``configured=True`` with a registry — registry-on. Proposals require a
      registered credential, and the local token is refused there with a
      message that says why.
    - ``configured=True`` with ``registry=None`` — the owner named a file and
      the file is unreadable or invalid. Every proposal refuses until it is
      fixed. Deliberately **not** a fallback to the token posture: a file
      error must not silently reopen anonymous proposing.
    """

    configured: bool
    registry: ProposerRegistry | None = None


def load_proposer_auth(path: Path | None) -> ProposerAuth:
    """Resolve the proposal route's authentication posture from settings."""

    if path is None:
        return ProposerAuth(configured=False)
    loaded = load_proposer_registry(path)
    if loaded is None:
        return ProposerAuth(configured=True, registry=None)
    return ProposerAuth(configured=True, registry=loaded.registry)


def require_proposer(request: Request) -> str | None:
    """FastAPI dependency for the proposal route: who may propose, and as whom.

    Returns the verified ``proposer_id`` when a registry is configured, or
    ``None`` under the pre-registry posture — the route records exactly what
    was verified, and the drain-time resolver turns it back into identity.

    The refusal messages name headers and postures, never credentials or
    registry contents: which proposer ids exist is owner configuration, and a
    401 body is the last place to disclose configuration to an unauthenticated
    caller.
    """

    auth: ProposerAuth | None = getattr(request.app.state, "proposer_auth", None)
    if auth is None or not auth.configured:
        # Pre-registry posture: precisely the bar every other route sets,
        # by calling the same check rather than restating it.
        require_token(request)
        return None
    if auth.registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "the proposer registry is configured but unreadable or invalid; "
                "proposals refuse until the owner repairs the file"
            ),
        )
    presented = request.headers.get(_PROPOSER_HEADER, "")
    if not presented:
        if request.headers.get(_TOKEN_HEADER, ""):
            # A distinct message on purpose. A worker still sending the local
            # token after the owner configures a registry should learn what
            # changed, not retry a generic 401 blindly (R-31's lesson).
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    f"a proposer registry is configured, so the {_TOKEN_HEADER} "
                    f"credential no longer opens this route; present a registered "
                    f"proposer credential in {_PROPOSER_HEADER}"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {_PROPOSER_HEADER} header",
        )
    registration = auth.registry.verify(presented, now=utc_now())
    if registration is None:
        # Unknown, disabled, and expired credentials share one message: which
        # of the three it was is the owner's business (the registry file and
        # `chronos.cli proposer check` say), not the caller's.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"the {_PROPOSER_HEADER} credential is not a current registered proposer",
        )
    if _is_revoked(request, registration.secret_sha256):
        # A revoked credential joins the same message (A3), for the same
        # reason: a 401 body is the last place to tell an unauthenticated
        # caller which of four states it is in. The distinction the owner needs
        # is in the revocation ledger, in the server log below, and — for a
        # proposal already queued — in the journal's own PROPOSER_REVOKED
        # refusal at STAMP.
        _logger.warning(
            "Refused a revoked proposer credential at the ingress: %s",
            registration.proposer_id,
            extra={"event": "proposer_revoked_refused"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"the {_PROPOSER_HEADER} credential is not a current registered proposer",
        )
    return registration.proposer_id


def _is_revoked(request: Request, secret_sha256: str) -> bool:
    """Consult the durable revocation ledger, fail-closed (A3).

    Read per request rather than cached: a cached answer would make revocation
    as slow as the boot-time snapshot it exists to fix. An unreadable ledger
    refuses — an authorization check that cannot see its own ledger has not
    passed, it has failed to run.
    """

    state = getattr(request.app.state, "backend", None)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend runtime is not initialized",
        )
    from chronos.supervisor import revocation

    try:
        with state.runtime.database.sessions.begin() as session:
            return revocation.is_revoked(session, secret_sha256=secret_sha256)
    except Exception as error:  # the ledger is unreadable: refuse, never assume
        _logger.exception("The proposer revocation ledger could not be read")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "the proposer revocation ledger could not be read; proposals refuse until it can be"
            ),
        ) from error

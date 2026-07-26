"""Browser sessions for the operator terminal (M8b, ADR-0018 §6 amendment).

M8a shipped a terminal whose panels could not authenticate. A browser cannot put
a header on a document load, so the shell was served unauthenticated while every
data route required ``X-Chronos-Token`` — and the bundled client, holding no
token, got ``401`` on everything (R-41). This module closes that, by the route
the owner chose: a login endpoint exchanges the token the operator already has on
disk for an httpOnly session cookie.

## The property that makes this safe to do at all

**The cookie is scoped to ``/terminal`` and the browser therefore never attaches
it to anything else.** That is not a detail; it is the whole reason a cookie is
acceptable here.

An ambient credential is a real hazard in this process. The M8a injection review
put it plainly: the moment the terminal page holds a credential, same-origin
script execution in that page becomes able to act with it — and this process is
the one holding the broker connection. A cookie with ``path=/`` would mean
injected script could reach ``/orders/*`` with the browser attaching
authentication automatically, turning a display defect into order submission.

Scoping the cookie to ``/terminal`` makes that structurally impossible rather
than merely unlikely. The order plane keeps requiring the explicit header, which
the terminal client does not have and cannot obtain: the token is never sent to
the browser, only *checked* against what the browser sent. So the terminal's
ambient credential authenticates exactly the surface the terminal is allowed to
use, and no part of the surface that moves money.

The defenses stack in a deliberate order, because no single one of them is
sufficient:

- ``path=/terminal`` — the browser will not attach the cookie to the order plane.
- ``httpOnly`` — script cannot read the cookie, so it cannot be exfiltrated or
  replayed anywhere else. Note what this does *not* buy: the browser still sends
  it on same-origin ``/terminal/*`` requests, so httpOnly is not a defense
  against injected script *using* it in place. The CSP (ADR-0018, R-40) is.
- ``SameSite=strict`` — another origin cannot cause the browser to send it.
- **In-memory only** — sessions do not survive a restart. A durable session store
  would be a credential outliving the process it authenticates to, and the one
  thing this project has been consistent about is that restarting is how an
  operator regains a known state.

## What a session is not

A session is not authority. It proves the caller held the API token; it grants
nothing else. ``require_writer`` still gates every mutating route independently,
the kill switch and arming are untouched, and a session on a read-only backend
buys exactly what the token buys there: inspection. Logging in is not arming.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, status

_logger = logging.getLogger("chronos.api.terminal")

#: The cookie name. Prefixed so it cannot collide with anything an operator's own
#: tooling sets on localhost, which is a genuinely crowded origin.
SESSION_COOKIE = "chronos_terminal_session"

#: The path the browser will attach the cookie to, and the reason this is safe.
#: Everything outside it — crucially ``/orders/*`` — keeps requiring the header.
SESSION_PATH = "/terminal"

#: How long a login lasts. Long enough for a working session, short enough that a
#: browser left open on an unattended machine stops being a credential overnight.
SESSION_TTL = timedelta(hours=12)

#: A ceiling on live sessions, so a script POSTing valid logins cannot grow this
#: process's memory without bound. Reaching it refuses the *new* login rather
#: than evicting an existing one: evicting would let a caller who already knows
#: the token log the operator out, which is a worse failure than refusing.
MAX_SESSIONS = 32

_SESSION_BYTES = 32


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class TerminalSessions:
    """Live browser sessions, by digest.

    Only digests are held. A memory disclosure — a traceback rendered into a log,
    a debugger left attached — then leaks nothing a caller could present, which
    is the same reasoning that keeps the API token out of settings objects.
    """

    ttl: timedelta = SESSION_TTL
    max_sessions: int = MAX_SESSIONS
    _expiry: dict[str, datetime] = field(default_factory=dict)

    def create(self, *, now: datetime) -> str:
        """Mint a session and return the id to hand the browser. Never logged."""

        self.purge_expired(now=now)
        if len(self._expiry) >= self.max_sessions:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=("too many terminal sessions are open; close one or wait for it to expire"),
            )
        session_id = secrets.token_urlsafe(_SESSION_BYTES)
        self._expiry[_digest(session_id)] = now + self.ttl
        return session_id

    def validate(self, session_id: str, *, now: datetime) -> bool:
        """True when this id names a live session.

        Expiry is checked here rather than only in :meth:`purge_expired`, so a
        session cannot outlive its TTL merely because nothing swept recently.
        """

        if not session_id:
            return False
        expires = self._expiry.get(_digest(session_id))
        if expires is None:
            return False
        if expires <= now:
            self._expiry.pop(_digest(session_id), None)
            return False
        return True

    def revoke(self, session_id: str) -> bool:
        """Drop one session. Returns whether there was one to drop."""

        return self._expiry.pop(_digest(session_id), None) is not None

    def purge_expired(self, *, now: datetime) -> int:
        dead = [digest for digest, expires in self._expiry.items() if expires <= now]
        for digest in dead:
            del self._expiry[digest]
        return len(dead)

    @property
    def live(self) -> int:
        return len(self._expiry)


def verify_api_token(request: Request, presented: str) -> bool:
    """Constant-time check of a token presented in a request *body*.

    Separate from :func:`chronos.api.auth.require_token` because that one reads a
    header and raises; this one answers a question about a body so the login
    route can decide what to say. The comparison is the same.
    """

    expected: str | None = getattr(request.app.state, "api_token", None)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend API token is not initialized",
        )
    return bool(presented) and hmac.compare_digest(presented, expected)


def sessions_of(request: Request) -> TerminalSessions:
    """The process's session store, created on first use."""

    store: TerminalSessions | None = getattr(request.app.state, "terminal_sessions", None)
    if store is None:
        store = TerminalSessions()
        request.app.state.terminal_sessions = store
    return store


def require_terminal_credential(request: Request) -> None:
    """Accept either the API token header or a live terminal session cookie.

    Both are the same claim — "I hold the local API token" — presented by
    different callers. A script or ``curl`` sends the header it already has; a
    browser, which cannot set a header on a document load, sends the cookie it
    was given in exchange for that same token.

    The header is checked first and on its own, so every existing non-browser
    caller behaves exactly as it did before this module existed. A failure says
    which credentials are acceptable and nothing about which one was wrong,
    because "the cookie was almost right" is not a thing an operator needs to
    hear and is a thing a prober would like to.
    """

    from chronos.api.auth import require_token

    expected: str | None = getattr(request.app.state, "api_token", None)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend API token is not initialized",
        )

    presented = request.headers.get("X-Chronos-Token", "")
    if presented:
        require_token(request)
        return

    from chronos.utils.time import utc_now

    cookie = request.cookies.get(SESSION_COOKIE, "")
    if sessions_of(request).validate(cookie, now=utc_now()):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "terminal routes need the X-Chronos-Token header or a terminal session "
            "cookie; POST /terminal/session with the token to obtain one"
        ),
    )

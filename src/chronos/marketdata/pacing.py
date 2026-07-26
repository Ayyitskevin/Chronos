"""Pacing controller for historical requests (ADR-0011 §5; shared since M8c).

IBKR rate-limits ``reqHistoricalData`` (documented: no more than ~60 historical
requests in any rolling 10-minute window, and identical requests must not repeat
too quickly). This controller enforces a **conservative** budget — a rolling
per-window cap plus a per-key cooldown — and is a pure function of an injected
clock, so it is unit-testable without sleeping and deterministic.

Usage in the data process (the only place a real sleep happens)::

    delay = pacing.delay_before(key, clock.now())
    if delay > 0:
        time.sleep(delay)
    now = clock.now()
    pacing.record(key, now)
    # ... issue the request ...

Honesty (ADR-0011 §5): this self-paces **one process under its own client id**.
A shared budget across the trading backend and the data process is still not
wired, and the limits cannot be validated without a live gateway.

That residual is why this module lives in ``chronos.marketdata`` rather than in
``chronos.histdata`` where it started. Since M8c the operator terminal's chart
also asks IBKR for bars, from the backend process, under a *different* client id
— so there are now two self-pacing callers instead of one. Sharing the
implementation at least means they enforce the same rule and a fix lands in one
place; it does not make their budgets one budget, and pretending otherwise by
leaving a second copy in the API plane would have hidden the gap instead of
naming it. The move is deliberate: ``chronos.marketdata`` is the neutral
vocabulary both the research plane and the trading plane already share, so
neither has to import the other to pace itself.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_DEFAULT_MAX_PER_WINDOW = 6
_DEFAULT_WINDOW = timedelta(minutes=1)
_DEFAULT_COOLDOWN = timedelta(seconds=15)


@dataclass
class PacingController:
    """A rolling-window + per-key-cooldown gate over historical requests."""

    max_per_window: int = _DEFAULT_MAX_PER_WINDOW
    window: timedelta = _DEFAULT_WINDOW
    cooldown: timedelta = _DEFAULT_COOLDOWN
    _recent: deque[datetime] = field(default_factory=deque, init=False, repr=False)
    _last_by_key: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_per_window < 1:
            raise ValueError("max_per_window must be >= 1")

    def delay_before(self, key: str, now: datetime) -> float:
        """Seconds to wait before a request for ``key`` is allowed at ``now`` (0 if now)."""

        self._purge(now)
        window_wait = 0.0
        if len(self._recent) >= self.max_per_window:
            frees_at = self._recent[0] + self.window
            window_wait = max(0.0, (frees_at - now).total_seconds())
        key_wait = 0.0
        last = self._last_by_key.get(key)
        if last is not None:
            frees_at = last + self.cooldown
            key_wait = max(0.0, (frees_at - now).total_seconds())
        return max(window_wait, key_wait)

    def record(self, key: str, now: datetime) -> None:
        """Record that a request for ``key`` was issued at ``now``."""

        self._purge(now)
        self._recent.append(now)
        self._last_by_key[key] = now

    def _purge(self, now: datetime) -> None:
        cutoff = now - self.window
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()

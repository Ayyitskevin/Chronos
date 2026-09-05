"""Session-drawdown circuit breaker.

Establishes a per-session net-liquidation baseline (the first observation of the
trading day, persisted so a restart mid-session keeps the same baseline), and
trips when the intraday drawdown from that baseline breaches either the absolute
(``max_session_drawdown_usd``) or the relative (``max_session_drawdown_pct``)
limit. A breach engages the kill switch, so recovery is an explicit operator
action — the breaker never silently re-arms.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from chronos.domain.models import ChronosModel
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.state_generation import SESSION_DRAWDOWN, StateGenerationMarker


class _CorruptBaseline(Exception):
    """The session-baseline file is present but unreadable for this session."""


class DrawdownDecision(ChronosModel):
    breached: bool
    session_date: str
    baseline_nlv: Decimal
    current_nlv: Decimal
    drawdown_usd: Decimal
    drawdown_pct: Decimal
    reason: str = ""


class SessionDrawdownBreaker:
    def __init__(
        self,
        path: Path,
        *,
        max_drawdown_usd: Decimal,
        max_drawdown_pct: Decimal,
        market_timezone: str,
        kill_switch: LiveKillSwitch | None = None,
        generation: StateGenerationMarker | None = None,
    ) -> None:
        self._path = path
        self._max_usd = max_drawdown_usd
        self._max_pct = max_drawdown_pct
        self._tz = ZoneInfo(market_timezone)
        self._kill_switch = kill_switch
        # See `chronos.orders.state_generation`: absence means "establish a fresh
        # baseline" only while this installation has never established one (R-66).
        self._generation = generation or StateGenerationMarker.beside(path)

    def _session_date(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("drawdown checks require a timezone-aware timestamp")
        return now.astimezone(self._tz).date().isoformat()

    def _read_baseline(self, session_date: str) -> Decimal | None:
        """Return today's baseline, ``None`` to (re)establish, or raise on corrupt.

        A genuinely absent file (fresh deploy) or a different ``session_date``
        (new trading day) returns ``None`` so ``check`` establishes a fresh
        baseline. A file that is PRESENT but unreadable/unparseable — or whose
        baseline for TODAY is non-numeric/non-finite/non-positive — raises
        :class:`_CorruptBaseline`, so ``check`` fails CLOSED rather than
        silently re-baselining at a possibly drawn-down value.
        """

        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._generation.was_materialized(SESSION_DRAWDOWN):
                # This installation established a baseline and the file is gone.
                # Re-baselining here would anchor the session at the post-loss
                # net liquidation value and report no breach for a drawdown that
                # already happened -- the exact hazard the directory fsync above
                # was added for, arriving by deletion instead of power loss.
                raise _CorruptBaseline(
                    "baseline file is missing but this installation established one; "
                    "refusing to re-baseline at the current net liquidation value"
                ) from None
            return None  # absent, and none was ever established: fresh baseline
        except OSError as error:
            raise _CorruptBaseline(f"baseline file unreadable: {error}") from error
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise _CorruptBaseline(f"baseline file is not valid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise _CorruptBaseline("baseline file is not a JSON object")
        if payload.get("session_date") != session_date:
            return None  # new trading day: establish a fresh baseline
        try:
            value = Decimal(str(payload.get("baseline_nlv")))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise _CorruptBaseline(f"baseline_nlv is not a number: {error}") from error
        if not value.is_finite() or value <= 0:
            raise _CorruptBaseline(f"baseline_nlv is not a positive finite number: {value}")
        return value

    def _write_baseline(self, session_date: str, baseline: Decimal, now: datetime) -> None:
        payload = {
            "session_date": session_date,
            "baseline_nlv": format(baseline, "f"),
            "established_at": now.isoformat(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, json.dumps(payload, indent=2).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, self._path)
        # fsync the parent directory so a just-established baseline survives OS
        # power loss (parity with the kill switch); otherwise a lost baseline
        # reads as "absent" on restart and re-establishes at the current NLV.
        dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        # Only after the durable write: from here a missing baseline is a loss.
        self._generation.record_materialized(SESSION_DRAWDOWN, now=now)

    def _tripped(
        self, session_date: str, current_nlv: Decimal, reason: str, now: datetime
    ) -> DrawdownDecision:
        if self._kill_switch is not None and not self._kill_switch.is_engaged():
            self._kill_switch.engage(
                reason=f"session drawdown breaker: {reason}",
                initiated_by="session_drawdown_breaker",
                now=now,
            )
        return DrawdownDecision(
            breached=True,
            session_date=session_date,
            baseline_nlv=Decimal("0"),
            current_nlv=current_nlv,
            drawdown_usd=Decimal("0"),
            drawdown_pct=Decimal("0"),
            reason=reason,
        )

    def check(self, current_nlv: Decimal, *, now: datetime) -> DrawdownDecision:
        session_date = self._session_date(now)
        try:
            baseline = self._read_baseline(session_date)
        except _CorruptBaseline as error:
            # Fail CLOSED: a corrupt in-session baseline could hide a drawdown.
            return self._tripped(
                session_date,
                current_nlv,
                f"session baseline unreadable; failing closed: {error}",
                now,
            )
        if baseline is None:
            if not current_nlv.is_finite() or current_nlv <= 0:
                # ADR-0009 §4 / M7 review finding F3: a non-positive first
                # observation is an implausible NLV read, never a baseline.
                # Refuse THIS check without writing the file and without the
                # durable kill-switch side effect (cannot-evaluate != breached).
                return DrawdownDecision(
                    breached=True,
                    session_date=session_date,
                    baseline_nlv=Decimal("0"),
                    current_nlv=current_nlv,
                    drawdown_usd=Decimal("0"),
                    drawdown_pct=Decimal("0"),
                    reason=(
                        "refusing to establish a non-positive session baseline "
                        f"(NLV read implausible: {current_nlv})"
                    ),
                )
            # First observation this session establishes the baseline.
            baseline = current_nlv
            self._write_baseline(session_date, baseline, now)
        drawdown_usd = baseline - current_nlv
        drawdown_pct = (drawdown_usd / baseline) if baseline > 0 else Decimal("0")
        breached = drawdown_usd >= self._max_usd or (baseline > 0 and drawdown_pct >= self._max_pct)
        reason = ""
        if breached:
            reason = (
                f"session drawdown {drawdown_usd} ({drawdown_pct:.4f}) from baseline "
                f"{baseline} breaches limits usd>={self._max_usd} or pct>={self._max_pct}"
            )
            if self._kill_switch is not None and not self._kill_switch.is_engaged():
                self._kill_switch.engage(
                    reason=f"session drawdown breaker: {reason}",
                    initiated_by="session_drawdown_breaker",
                    now=now,
                )
        return DrawdownDecision(
            breached=breached,
            session_date=session_date,
            baseline_nlv=baseline,
            current_nlv=current_nlv,
            drawdown_usd=drawdown_usd,
            drawdown_pct=drawdown_pct,
            reason=reason,
        )

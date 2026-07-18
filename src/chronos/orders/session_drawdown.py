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
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from chronos.domain.models import ChronosModel
from chronos.orders.kill_switch import LiveKillSwitch


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
    ) -> None:
        self._path = path
        self._max_usd = max_drawdown_usd
        self._max_pct = max_drawdown_pct
        self._tz = ZoneInfo(market_timezone)
        self._kill_switch = kill_switch

    def _session_date(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("drawdown checks require a timezone-aware timestamp")
        return now.astimezone(self._tz).date().isoformat()

    def _read_baseline(self, session_date: str) -> Decimal | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("session_date") != session_date:
            return None
        raw = payload.get("baseline_nlv")
        try:
            return Decimal(str(raw))
        except (TypeError, ValueError):
            return None

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

    def check(self, current_nlv: Decimal, *, now: datetime) -> DrawdownDecision:
        session_date = self._session_date(now)
        baseline = self._read_baseline(session_date)
        if baseline is None:
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

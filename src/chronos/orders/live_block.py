"""Explicit LIVE TRADING BLOCKED gate (research-readiness hardening).

A pure, fail-closed surface that any plane can query to prove live capital
cannot be deployed. This is intentionally redundant with the ADR-0009
conjunction, mode lock, and submission boundary: defense in depth, not a
replacement.

Default settings always report blocked. A research-risk policy cannot flip
this outcome — risk YAML never participates in live-branch selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronos.config.settings import Settings

# Canonical operator- and test-facing outcome string. Do not rephrase at call
# sites: structural tests and operator docs pin this exact token.
LIVE_TRADING_BLOCKED = "LIVE TRADING BLOCKED"


@dataclass(frozen=True, slots=True)
class LiveBlockDecision:
    """Immutable answer to: may this process transmit a live broker order?"""

    blocked: bool
    outcome: str
    reasons: tuple[str, ...]

    @property
    def may_transmit_live(self) -> bool:
        return not self.blocked


def evaluate_live_trading_block(settings: Settings) -> LiveBlockDecision:
    """Return LIVE TRADING BLOCKED unless the full ADR-0009 conjunction holds.

    This re-derives capability from frozen settings only (no broker I/O). Even
    when the conjunction is true, runtime gates (arming, kill switch, typed
    confirmation, fresh broker evidence) still refuse independently — this
    function is the configuration-layer hard stop, not the full live path.
    """

    reasons: list[str] = []
    if not settings.allow_live_trading:
        reasons.append("ALLOW_LIVE_TRADING is false")
    if not settings.allow_order_transmit:
        reasons.append("ALLOW_ORDER_TRANSMIT is false")
    if not settings.live_transmission_possible:
        reasons.append("settings.live_transmission_possible is False (ADR-0009 conjunction unmet)")

    if reasons:
        return LiveBlockDecision(
            blocked=True,
            outcome=LIVE_TRADING_BLOCKED,
            reasons=tuple(reasons),
        )
    # Conjunction is configuration-true. Still surface the explicit outcome
    # vocabulary so operators never see a silent "ok" without runtime gates.
    return LiveBlockDecision(
        blocked=False,
        outcome="LIVE_TRANSMISSION_CONFIG_POSSIBLE",
        reasons=(),
    )


def assert_live_trading_blocked(settings: Settings) -> LiveBlockDecision:
    """Fail closed: raise if settings would allow live transmission config.

    Research, backtest, and default CI processes call this to prove they
    cannot enter the live order path. Raises ``RuntimeError`` with the
    canonical outcome token when the block is not in force.
    """

    decision = evaluate_live_trading_block(settings)
    if not decision.blocked:
        raise RuntimeError(
            f"{LIVE_TRADING_BLOCKED} assertion failed: configuration reports "
            "live transmission possible; refuse rather than proceed"
        )
    if decision.outcome != LIVE_TRADING_BLOCKED:
        raise RuntimeError(f"expected outcome {LIVE_TRADING_BLOCKED!r}, got {decision.outcome!r}")
    return decision

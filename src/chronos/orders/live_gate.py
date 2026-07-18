"""The live-order gate stack (Milestone 6).

A pure, fail-closed evaluator that composes every precondition for a LIVE order
into one typed decision. Each gate is an explicit named check with a reason;
the overall decision passes ONLY when every gate passes AND neither breaker
(kill switch, session drawdown) has tripped. Any unmet gate blocks — there is
no default-allow path.

Milestone 6 builds and tests this stack; it does NOT enable live transmission.
In this build the config gate is False (``allow_live_trading`` is refused by
settings validation), so the stack can never pass in production — which is the
point. Milestone 7 relaxes that config gate and wires the stack into the
submission boundary as the LIVE branch. The evaluator itself is pure, so each
gate's blocking behavior is verified in isolation.
"""

from __future__ import annotations

from chronos.domain.models import ChronosModel

# The ordered gate names. The first eight are the spec's eight-gate stack; the
# last two are the always-checked emergency breakers.
LIVE_GATE_NAMES = (
    "config",
    "connection",
    "reconciliation",
    "data",
    "risk",
    "preview",
    "session_arming",
    "per_order_confirmation",
    "kill_switch",
    "session_drawdown",
)


class LiveGateCheck(ChronosModel):
    name: str
    passed: bool
    reason: str


class LiveGateDecision(ChronosModel):
    checks: tuple[LiveGateCheck, ...]
    all_passed: bool

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(check.reason for check in self.checks if not check.passed)


class LiveGateInputs(ChronosModel):
    """Pre-gathered gate evidence. Missing/unknown evidence must arrive False."""

    config_live_enabled: bool = False
    connection_healthy: bool = False
    reconciled: bool = False
    data_quality_ok: bool = False
    risk_approved: bool = False
    preview_accepted: bool = False
    armed: bool = False
    confirmation_valid: bool = False
    kill_switch_engaged: bool = True  # fail-closed default: assume engaged
    drawdown_breached: bool = True  # fail-closed default: assume breached
    # Optional richer reasons the caller can supply per gate.
    config_reason: str = ""
    connection_reason: str = ""
    reconciliation_reason: str = ""
    data_reason: str = ""
    risk_reason: str = ""
    preview_reason: str = ""
    arming_reason: str = ""
    confirmation_reason: str = ""
    kill_switch_reason: str = ""
    drawdown_reason: str = ""


def _check(
    name: str, passed: bool, ok_reason: str, fail_reason: str, supplied: str
) -> LiveGateCheck:
    reason = supplied or (ok_reason if passed else fail_reason)
    return LiveGateCheck(name=name, passed=passed, reason=reason)


def evaluate_live_gates(inputs: LiveGateInputs) -> LiveGateDecision:
    """Compose the fail-closed live gate stack into one typed decision."""

    checks = (
        _check(
            "config",
            inputs.config_live_enabled,
            "live trading configuration is enabled and valid",
            "live trading is not enabled in configuration (awaits Milestone 7)",
            inputs.config_reason,
        ),
        _check(
            "connection",
            inputs.connection_healthy,
            "broker connection is healthy",
            "broker connection is not healthy",
            inputs.connection_reason,
        ),
        _check(
            "reconciliation",
            inputs.reconciled,
            "broker state is reconciled",
            "broker state is not reconciled",
            inputs.reconciliation_reason,
        ),
        _check(
            "data",
            inputs.data_quality_ok,
            "market data quality is acceptable",
            "market data quality is not acceptable (stale/frozen/unknown)",
            inputs.data_reason,
        ),
        _check(
            "risk",
            inputs.risk_approved,
            "structured risk decision is approved and unexpired",
            "structured risk decision is not approved or has expired",
            inputs.risk_reason,
        ),
        _check(
            "preview",
            inputs.preview_accepted,
            "broker what-if preview was accepted",
            "broker what-if preview was not accepted",
            inputs.preview_reason,
        ),
        _check(
            "session_arming",
            inputs.armed,
            "live trading is armed and unexpired",
            "live trading is not armed (or the arm expired/was revoked)",
            inputs.arming_reason,
        ),
        _check(
            "per_order_confirmation",
            inputs.confirmation_valid,
            "typed per-order confirmation matches and is fresh",
            "typed per-order confirmation is missing, stale, or mismatched",
            inputs.confirmation_reason,
        ),
        _check(
            "kill_switch",
            not inputs.kill_switch_engaged,
            "kill switch is disengaged",
            "kill switch is ENGAGED — all live trading is halted",
            inputs.kill_switch_reason,
        ),
        _check(
            "session_drawdown",
            not inputs.drawdown_breached,
            "session drawdown is within limits",
            "session drawdown circuit breaker has tripped",
            inputs.drawdown_reason,
        ),
    )
    return LiveGateDecision(checks=checks, all_passed=all(check.passed for check in checks))

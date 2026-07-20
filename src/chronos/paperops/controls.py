"""Paper-plane portfolio and session controls (pure, fail closed).

Composes halt, kill-switch, exposure, concentration, position count, daily loss,
duplicate-order, and cooldown checks. Live kill-switch engagement blocks paper
opens here as a shared emergency stop; it does not enable live trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from chronos.paperops.reasons import PaperReasonCode


@dataclass(frozen=True, slots=True)
class PaperControlState:
    """Snapshot of portfolio / session state for control evaluation."""

    halted: bool
    halt_detail: str
    kill_switch_engaged: bool
    kill_switch_detail: str
    account_equity_usd: float
    cash_usd: float
    position_notional_by_symbol: dict[str, float]
    open_position_count: int
    realized_pnl_today_usd: float
    # Duplicate / cooldown evidence
    recent_order_fingerprints: tuple[str, ...]
    last_order_at_utc: datetime | None
    proposed_order_fingerprint: str
    proposed_symbol: str
    proposed_notional_usd: float
    # Limits
    max_aggregate_exposure_usd: float
    max_symbol_exposure_fraction: float
    max_simultaneous_positions: int
    max_daily_loss_usd: float
    cooldown_seconds: float
    now_utc: datetime


@dataclass(frozen=True, slots=True)
class PaperControlDecision:
    allowed: bool
    reason_codes: tuple[PaperReasonCode, ...]
    explanations: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason_codes": [c.value for c in self.reason_codes],
            "explanations": list(self.explanations),
        }


def evaluate_paper_controls(state: PaperControlState) -> PaperControlDecision:
    """Deny-by-default portfolio controls for paper opens."""

    codes: list[PaperReasonCode] = []
    explanations: list[str] = []

    def deny(code: PaperReasonCode, detail: str) -> None:
        codes.append(code)
        explanations.append(detail)

    if state.halted:
        deny(PaperReasonCode.HALTED, f"trading halted: {state.halt_detail or 'no detail'}")

    if state.kill_switch_engaged:
        deny(
            PaperReasonCode.KILL_SWITCH_ENGAGED,
            f"kill switch engaged: {state.kill_switch_detail or 'engaged'}",
        )

    if state.proposed_order_fingerprint in state.recent_order_fingerprints:
        deny(
            PaperReasonCode.DUPLICATE_ORDER,
            "duplicate order fingerprint already seen (retry/replay blocked)",
        )

    if state.last_order_at_utc is not None and state.cooldown_seconds > 0:
        if state.last_order_at_utc.tzinfo is None or state.now_utc.tzinfo is None:
            deny(
                PaperReasonCode.DATA_CLOCK_ANOMALY,
                "cooldown timestamps must be timezone-aware; failing closed",
            )
        else:
            elapsed = (state.now_utc - state.last_order_at_utc).total_seconds()
            if elapsed < state.cooldown_seconds:
                deny(
                    PaperReasonCode.COOLDOWN_ACTIVE,
                    f"cooldown active: {elapsed:.0f}s < {state.cooldown_seconds:.0f}s",
                )

    if state.max_daily_loss_usd > 0 and state.realized_pnl_today_usd <= -abs(
        state.max_daily_loss_usd
    ):
        deny(
            PaperReasonCode.DAILY_LOSS_LIMIT,
            f"daily realized PnL {state.realized_pnl_today_usd:.2f} at/below "
            f"limit -{abs(state.max_daily_loss_usd):.2f}",
        )

    gross = sum(abs(v) for v in state.position_notional_by_symbol.values())
    if state.proposed_notional_usd < 0:
        deny(
            PaperReasonCode.EXPOSURE_LIMIT,
            f"proposed notional is negative: {state.proposed_notional_usd}",
        )
    elif gross + state.proposed_notional_usd > state.max_aggregate_exposure_usd:
        deny(
            PaperReasonCode.EXPOSURE_LIMIT,
            f"aggregate exposure {gross + state.proposed_notional_usd:.2f} would exceed "
            f"{state.max_aggregate_exposure_usd:.2f}",
        )

    equity = state.account_equity_usd
    symbol_exposure = abs(state.position_notional_by_symbol.get(state.proposed_symbol, 0.0))
    if equity <= 0:
        deny(PaperReasonCode.EXPOSURE_LIMIT, "account equity is not positive; failing closed")
    elif equity > 0 and state.max_symbol_exposure_fraction >= 0:
        frac = (symbol_exposure + state.proposed_notional_usd) / equity
        if frac > state.max_symbol_exposure_fraction:
            deny(
                PaperReasonCode.CONCENTRATION_LIMIT,
                f"symbol concentration {frac:.2%} would exceed "
                f"{state.max_symbol_exposure_fraction:.2%}",
            )

    entering_new = abs(state.position_notional_by_symbol.get(state.proposed_symbol, 0.0)) == 0
    if entering_new and state.open_position_count + 1 > state.max_simultaneous_positions:
        deny(
            PaperReasonCode.POSITION_LIMIT,
            f"{state.open_position_count} positions open; max {state.max_simultaneous_positions}",
        )

    if codes:
        return PaperControlDecision(
            allowed=False, reason_codes=tuple(codes), explanations=tuple(explanations)
        )
    return PaperControlDecision(
        allowed=True,
        reason_codes=(PaperReasonCode.CONTROLS_OK,),
        explanations=("all paper portfolio controls passed",),
    )


def cooldown_until(last_order_at_utc: datetime | None, cooldown_seconds: float) -> datetime | None:
    if last_order_at_utc is None or cooldown_seconds <= 0:
        return None
    return last_order_at_utc + timedelta(seconds=cooldown_seconds)

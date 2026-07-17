"""The service decision cycle.

``run_once`` evaluates the latest closed bar per symbol through the production
decision path (strategy -> portfolio -> risk -> execution), audits every step,
and notifies. Submission is attempted only when the mode permits it AND
reconciliation has passed; in SHADOW the mode lock is ``NO_ORDERS`` so the
execution engine refuses, which is the structural guarantee that a shadow run
cannot submit.

Determinism: given the same components, bars, and ``now_utc``, two runs produce
identical decisions and identical audit payloads. There is no wall-clock or
randomness in the core; ``now_utc`` is injected.

Account model: flat (configured equity, no positions) — exact for SHADOW.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from chronos.control.modes import ExecutionCapability
from chronos.execution.engine import SubmissionResult
from chronos.marketdata.bars import Bar
from chronos.notifications.notifier import NotificationKind
from chronos.portfolio.sizer import convert_proposal
from chronos.risk.engine import AccountView, MarketViewEntry
from chronos.service.runtime import ServiceComponents
from chronos.strategies.base import (
    PositionState,
    ProposalDirection,
    Strategy,
    StrategyContext,
)


@dataclass(frozen=True, slots=True)
class SymbolDecision:
    symbol: str
    strategy_id: str
    as_of_date: str | None
    proposal: str
    intent: str
    risk_approved: bool | None
    risk_codes: tuple[str, ...]
    submission: str


@dataclass(frozen=True, slots=True)
class CycleReport:
    ran: bool
    halted: bool
    reconciliation_passed: bool
    decisions: tuple[SymbolDecision, ...]
    detail: str


class ServiceCycle:
    """Runs one decision cycle over the configured symbols."""

    def __init__(self, components: ServiceComponents) -> None:
        self._c = components

    def run_once(self, *, now_utc: datetime, min_bars: int = 1) -> CycleReport:
        c = self._c
        halt = c.halt_store.read()
        if halt.trading_blocked:
            reason = halt.reason.value if halt.reason else "unknown"
            c.audit_log.append("service_cycle", {"outcome": "halted", "reason": reason})
            return CycleReport(
                ran=False,
                halted=True,
                reconciliation_passed=c.engine.reconciliation_passed,
                decisions=(),
                detail=f"halted ({reason}); no decisions",
            )

        may_submit = c.engine.reconciliation_passed and c.mode_lock.capability in (
            ExecutionCapability.SIMULATED_ONLY,
            ExecutionCapability.PAPER_SUBMISSION,
        )

        decisions: list[SymbolDecision] = []
        for symbol in c.symbols:
            series = c.data_source.closed_bars(symbol)
            if len(series) < min_bars:
                decisions.append(
                    SymbolDecision(
                        symbol=symbol,
                        strategy_id="-",
                        as_of_date=None,
                        proposal="no-data",
                        intent="-",
                        risk_approved=None,
                        risk_codes=(),
                        submission="-",
                    )
                )
                continue
            bar = series[-1]
            context = StrategyContext(bars=series.bars, position=PositionState.flat())
            for strategy_id, strategy in c.strategies.items():
                decisions.append(
                    self._evaluate(symbol, bar, context, strategy_id, strategy, may_submit, now_utc)
                )

        c.audit_log.append(
            "service_cycle",
            {
                "outcome": "ran",
                "reconciliation_passed": c.engine.reconciliation_passed,
                "capability": c.mode_lock.capability.value,
                "symbols": list(c.symbols),
                "decision_count": len(decisions),
            },
        )
        return CycleReport(
            ran=True,
            halted=False,
            reconciliation_passed=c.engine.reconciliation_passed,
            decisions=tuple(decisions),
            detail="cycle complete",
        )

    def _evaluate(
        self,
        symbol: str,
        bar: Bar,
        context: StrategyContext,
        strategy_id: str,
        strategy: Strategy,
        may_submit: bool,
        now_utc: datetime,
    ) -> SymbolDecision:
        c = self._c
        proposal = strategy.on_bar(context)
        as_of = bar.session_date.isoformat()
        if proposal is None or proposal.direction is ProposalDirection.HOLD:
            return SymbolDecision(
                symbol=symbol,
                strategy_id=strategy_id,
                as_of_date=as_of,
                proposal="hold",
                intent="-",
                risk_approved=None,
                risk_codes=(),
                submission="-",
            )

        c.notifier.notify(
            NotificationKind.SIGNAL,
            f"{strategy_id} {proposal.direction.value} {symbol}",
            proposal.reason,
        )
        conversion = convert_proposal(
            proposal,
            policy=c.portfolio_policy,
            equity_usd=Decimal(str(c.equity_usd)),
            cash_usd=Decimal(str(c.equity_usd)),
            held_shares=0,
            reference_price=Decimal(str(bar.close)),
        )
        if conversion.intent is None:
            return SymbolDecision(
                symbol=symbol,
                strategy_id=strategy_id,
                as_of_date=as_of,
                proposal=proposal.direction.value,
                intent=f"skipped: {conversion.skipped_reason}",
                risk_approved=None,
                risk_codes=(),
                submission="-",
            )

        intent = conversion.intent
        account = AccountView(
            account_equity_usd=c.equity_usd,
            cash_usd=c.equity_usd,
            position_shares={},
            position_notional_usd={},
            open_order_count=len(c.engine.working_intent_ids()),
            realized_pnl_today_usd=0.0,
            realized_pnl_week_usd=0.0,
            peak_equity_usd=c.equity_usd,
            consecutive_losses=0,
            as_of_utc=now_utc,
        )
        market = MarketViewEntry(
            last_price=bar.close, bar_close_utc=bar.timestamp_utc, quote_utc=bar.timestamp_utc
        )
        decision = c.risk_engine.validate(
            intent,
            account=account,
            market=market,
            halt=c.halt_store.read(),
            mode_lock=c.mode_lock,
            now_utc=now_utc,
        )
        submission = "not attempted"
        if not decision.approved:
            c.notifier.notify(
                NotificationKind.RISK_REJECTION,
                f"{strategy_id} {symbol} rejected",
                "; ".join(code.value for code in decision.codes),
            )
        elif may_submit:
            result: SubmissionResult = c.engine.submit_approved(
                intent, decision.approval, now_utc=now_utc
            )
            submission = f"{result.submitted}:{result.refusal.value}"
            if result.submitted:
                c.notifier.notify(
                    NotificationKind.ORDER_SUBMITTED, f"{symbol} submitted", intent.intent_id
                )
        else:
            submission = "blocked: mode/reconciliation"

        c.audit_log.append(
            "service_decision",
            {
                "symbol": symbol,
                "strategy": strategy_id,
                "as_of": as_of,
                "direction": proposal.direction.value,
                "intent_id": intent.intent_id,
                "quantity": intent.quantity,
                "limit_price": str(intent.limit_price),
                "risk_approved": decision.approved,
                "risk_codes": [code.value for code in decision.codes],
                "submission": submission,
            },
        )
        return SymbolDecision(
            symbol=symbol,
            strategy_id=strategy_id,
            as_of_date=as_of,
            proposal=proposal.direction.value,
            intent=f"{intent.side.value} {intent.quantity} @ {intent.limit_price}",
            risk_approved=decision.approved,
            risk_codes=tuple(code.value for code in decision.codes),
            submission=submission,
        )

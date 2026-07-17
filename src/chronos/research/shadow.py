"""Shadow-mode scan (Phase 11 SHADOW).

Runs the production decision path over the latest closed daily bars and
reports what the system WOULD do — proposal, sized intent, and the risk
engine's full decision — without any submission capability: the SHADOW mode
lock resolves to ``NO_ORDERS``, and this module never constructs a broker
adapter. Every scan is appended to the audit log for after-the-fact
reconstruction.

For daily-bar strategies the operational pattern is: run the scan after the
market close; review the intents; nothing is transmitted anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from chronos.auditlog.log import AuditLog
from chronos.control.halt import HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.marketdata.csv_provider import load_daily_csv
from chronos.marketdata.quality import validate_series
from chronos.portfolio.sizer import PortfolioPolicy, convert_proposal
from chronos.risk.engine import AccountView, MarketViewEntry, RiskEngine
from chronos.risk.policy import RiskPolicy
from chronos.strategies.base import PositionState, Strategy, StrategyContext


def shadow_scan(
    *,
    strategies: dict[str, Strategy],
    symbols: tuple[str, ...],
    data_dir: Path,
    risk_policy: RiskPolicy,
    halt_store: HaltStore,
    audit_log: AuditLog,
    equity_usd: float,
    now_utc: datetime | None = None,
) -> list[dict[str, object]]:
    """Evaluate the latest closed bar per (strategy, symbol); return reports."""

    mode_lock = resolve_mode_lock(
        requested_mode=TradingMode.SHADOW,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )
    assert not mode_lock.may_submit_paper  # structural: shadow cannot submit
    risk_engine = RiskEngine(risk_policy)
    portfolio_policy = PortfolioPolicy(max_fraction_per_position=0.95)
    now = now_utc or datetime.now(tz=UTC)
    halt = halt_store.read()

    reports: list[dict[str, object]] = []
    for symbol in symbols:
        path = data_dir / f"{symbol}.csv"
        if not path.exists():
            reports.append({"symbol": symbol, "skipped": f"no data file {path.name}"})
            continue
        loaded = load_daily_csv(path, symbol=symbol, source="research_raw")
        quality = validate_series(loaded.series)
        if quality.blocking:
            reports.append({"symbol": symbol, "skipped": "blocking data-quality issues"})
            continue
        for strategy_id, strategy in strategies.items():
            context = StrategyContext(bars=loaded.series.bars, position=PositionState.flat())
            proposal = strategy.on_bar(context)
            report: dict[str, object] = {
                "symbol": symbol,
                "strategy": strategy_id,
                "as_of_bar": loaded.series[-1].session_date.isoformat(),
                "mode": mode_lock.mode.value,
                "capability": mode_lock.capability.value,
                "proposal": None,
                "intent": None,
                "risk_decision": None,
            }
            if proposal is not None:
                report["proposal"] = {
                    "direction": proposal.direction.value,
                    "exposure": proposal.desired_exposure_fraction,
                    "stop_fraction": proposal.stop_fraction,
                    "reason": proposal.reason,
                }
                last_close = Decimal(str(loaded.series[-1].close))
                conversion = convert_proposal(
                    proposal,
                    policy=portfolio_policy,
                    equity_usd=Decimal(str(equity_usd)),
                    cash_usd=Decimal(str(equity_usd)),
                    held_shares=0,
                    reference_price=last_close,
                )
                if conversion.intent is not None:
                    intent = conversion.intent
                    report["intent"] = {
                        "intent_id": intent.intent_id,
                        "side": intent.side.value,
                        "quantity": intent.quantity,
                        "limit_price": str(intent.limit_price),
                        "stop_price": str(intent.stop_price)
                        if intent.stop_price is not None
                        else None,
                    }
                    decision = risk_engine.validate(
                        intent,
                        account=AccountView(
                            account_equity_usd=equity_usd,
                            cash_usd=equity_usd,
                            position_shares={},
                            position_notional_usd={},
                            open_order_count=0,
                            realized_pnl_today_usd=0.0,
                            realized_pnl_week_usd=0.0,
                            peak_equity_usd=equity_usd,
                            consecutive_losses=0,
                            as_of_utc=now,
                        ),
                        market=MarketViewEntry(
                            last_price=loaded.series[-1].close,
                            bar_close_utc=loaded.series[-1].timestamp_utc,
                            quote_utc=loaded.series[-1].timestamp_utc,
                        ),
                        halt=halt,
                        mode_lock=mode_lock,
                        now_utc=now,
                    )
                    report["risk_decision"] = {
                        "approved": decision.approved,
                        "codes": [code.value for code in decision.codes],
                        "explanations": list(decision.explanations),
                    }
                else:
                    report["intent"] = {"skipped": conversion.skipped_reason}
            reports.append(report)
            audit_log.append("shadow_scan", dict(report))
    return reports

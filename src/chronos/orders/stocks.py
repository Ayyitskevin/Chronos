"""Stock fold-in validation (docs/LIVE_WHEEL_GAME_PLAN.md §6b).

Long-only US equities through the same submission boundary as options: whole
shares, limit DAY, BUY to open a long position (cash-covered) or SELL up to the
settled long shares held (never short). Produces the stock-specific
:class:`~chronos.orders.risk.OrderRiskCheck` list the engine folds into its
decision.

``chronos.orders.risk`` imports this module lazily (inside a method), so the
module-level import of the risk types here does not create an import cycle.
"""

from __future__ import annotations

from decimal import Decimal

from chronos.config.settings import Settings
from chronos.domain.enums import OrderIntent, OrderSide, RiskCheckStatus
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.risk import OrderRiskCheck, RiskEvidence

STOCK_TIME_IN_FORCE = "DAY"


def _passed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.PASS, detail=detail)


def _failed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.FAIL, detail=detail)


def validate_stock_order(
    intent: WheelOrderIntent,
    *,
    evidence: RiskEvidence,
    settings: Settings,
) -> list[OrderRiskCheck]:
    checks: list[OrderRiskCheck] = []

    # ADR-0010 §1 (panel h-3): a GENUINE whole-share FAIL check — the model
    # validator is the first line, but this one cannot be bypassed by
    # model_copy(update=...) or a future builder regression.
    if intent.quantity > 0 and intent.quantity == intent.quantity.to_integral_value():
        checks.append(_passed("stock_whole_shares", f"{intent.quantity} whole shares"))
    else:
        checks.append(_failed("stock_whole_shares", "stock orders must be positive whole shares"))

    if intent.side is OrderSide.BUY:
        checks.append(_stock_buy_cash_check(intent, evidence, settings))
    elif intent.intent is OrderIntent.CLOSE_LONG_STOCK:
        checks.append(_stock_sell_holding_check(intent, evidence))
    else:  # pragma: no cover - a SELL that is not CLOSE_LONG_STOCK would be a short
        checks.append(_failed("stock_no_short", "selling stock is only permitted to close a long"))

    return checks


def _stock_buy_cash_check(
    intent: WheelOrderIntent,
    evidence: RiskEvidence,
    settings: Settings,
) -> OrderRiskCheck:
    cost = intent.limit_price * Decimal(intent.quantity)
    buffer = max(
        settings.min_cash_buffer_usd,
        evidence.account.net_liquidation * settings.min_cash_buffer_pct,
    )
    # A stock purchase must not consume the cash that secures open or pending
    # short puts, nor the cash already committed to resting crypto BUYs
    # (ADR-0010 ris-1): subtract those gross obligations before checking
    # availability, or the buy strands a cash-secured put or double-commits cash.
    put_obligations = evidence.existing_short_put_obligation + evidence.pending_open_put_obligation
    available = (
        evidence.account.total_cash
        - buffer
        - put_obligations
        - evidence.pending_crypto_buy_notional
    )
    if cost <= available:
        return _passed("stock_cash_sufficiency", f"cost {cost} within available {available}")
    return _failed(
        "stock_cash_sufficiency",
        f"cost {cost} exceeds available cash {available} "
        f"(after buffer {buffer} and put obligations {put_obligations})",
    )


def _stock_sell_holding_check(
    intent: WheelOrderIntent,
    evidence: RiskEvidence,
) -> OrderRiskCheck:
    if Decimal(intent.quantity) <= evidence.settled_long_shares:
        return _passed(
            "stock_no_short",
            f"selling {intent.quantity} <= {evidence.settled_long_shares} settled shares",
        )
    return _failed(
        "stock_no_short",
        f"selling {intent.quantity} exceeds {evidence.settled_long_shares} held; no shorting",
    )

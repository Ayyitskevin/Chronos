"""Crypto fold-in validation (ADR-0010, plan §6b).

Long-only spot crypto through the same submission boundary as options and
stocks: fractional Decimal quantities, limit orders only, BUY to open a long
(cash-covered, inside the crypto allocation cap) or SELL up to held crypto
(never short). Produces the crypto-specific
:class:`~chronos.orders.risk.OrderRiskCheck` list the engine folds into its
decision.

Venue metadata (``min_size``/``size_increment``) comes ONLY from the qualified
:class:`~chronos.domain.models.CryptoContract`; when a field is absent the
dependent check is UNKNOWN (fail closed), never a default (plan §6b: "from
qualified contract details, never assumed"). There is no min-notional check —
IBKR ContractDetails carries no such field (ADR-0010 §2); the venue's own
minimum-order rejection is the fail-safe guard, and Chronos's per-order bound
is the MAX notional cap.

``chronos.orders.risk`` imports this module lazily (inside a method), so the
module-level import of the risk types here does not create an import cycle.
"""

from __future__ import annotations

from decimal import Decimal

from chronos.config.settings import Settings
from chronos.domain.enums import OrderSide, RiskCheckStatus
from chronos.domain.models import CryptoContract
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.risk import OrderRiskCheck, RiskEvidence


def _passed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.PASS, detail=detail)


def _failed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.FAIL, detail=detail)


def _unknown(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.UNKNOWN, detail=detail)


def validate_crypto_order(
    intent: WheelOrderIntent,
    *,
    evidence: RiskEvidence,
    settings: Settings,
) -> list[OrderRiskCheck]:
    checks: list[OrderRiskCheck] = []
    contract = intent.contract
    if not isinstance(contract, CryptoContract):
        return [_failed("crypto_contract", "CRYPTO intent without a CryptoContract")]

    checks.append(_crypto_venue_conformance(intent, contract))
    checks.append(_crypto_notional_cap(intent, settings))

    if intent.side is OrderSide.BUY:
        checks.append(_crypto_allocation_cap(intent, evidence, settings))
        checks.append(_crypto_cash_sufficiency(intent, evidence, settings))
    else:  # SELL — CLOSE_LONG_CRYPTO
        checks.append(_crypto_sell_holding_check(intent, evidence))

    return checks


def _order_notional(intent: WheelOrderIntent) -> Decimal:
    return intent.limit_price * intent.quantity


def _crypto_venue_conformance(intent: WheelOrderIntent, contract: CryptoContract) -> OrderRiskCheck:
    # Absent venue metadata is UNKNOWN, never assumed (plan §6b).
    if contract.min_size is None or contract.size_increment is None:
        return _unknown(
            "crypto_venue_conformance",
            "qualified contract is missing venue min-size/size-increment metadata",
        )
    if intent.quantity < contract.min_size:
        return _failed(
            "crypto_venue_conformance",
            f"quantity {intent.quantity} is below the venue minimum {contract.min_size}",
        )
    if intent.quantity % contract.size_increment != 0:
        return _failed(
            "crypto_venue_conformance",
            f"quantity {intent.quantity} is not a multiple of the venue size "
            f"increment {contract.size_increment}",
        )
    return _passed(
        "crypto_venue_conformance",
        f"quantity {intent.quantity} conforms to min {contract.min_size} and "
        f"increment {contract.size_increment}",
    )


def _crypto_notional_cap(intent: WheelOrderIntent, settings: Settings) -> OrderRiskCheck:
    notional = _order_notional(intent)
    limit = settings.max_crypto_notional_per_order_usd
    if notional <= limit:
        return _passed("crypto_notional_cap", f"order notional {notional} <= {limit}")
    return _failed(
        "crypto_notional_cap",
        f"order notional {notional} exceeds the per-order cap {limit}",
    )


def _crypto_allocation_cap(
    intent: WheelOrderIntent, evidence: RiskEvidence, settings: Settings
) -> OrderRiskCheck:
    # BUY/OPEN-scoped (ADR-0010 §4): a SELL reduces exposure and is never blocked
    # by the allocation cap. The current allocation must be MARKED from a fresh
    # quote (ris-6); an unmarked (cost-based) valuation under-counts appreciated
    # crypto against a market-valued NLV, so it fails UNKNOWN.
    if not evidence.crypto_allocation_marked:
        return _unknown(
            "crypto_allocation_cap",
            "current crypto allocation could not be marked to a fresh quote",
        )
    notional = _order_notional(intent)
    projected = evidence.current_crypto_allocation + evidence.pending_crypto_buy_notional + notional
    ceiling = settings.max_crypto_allocation_pct * evidence.account.net_liquidation
    if projected <= ceiling:
        return _passed(
            "crypto_allocation_cap",
            f"projected crypto allocation {projected} <= ceiling {ceiling}",
        )
    return _failed(
        "crypto_allocation_cap",
        f"projected crypto allocation {projected} (current "
        f"{evidence.current_crypto_allocation} + pending "
        f"{evidence.pending_crypto_buy_notional} + order {notional}) exceeds "
        f"ceiling {ceiling}",
    )


def _crypto_cash_sufficiency(
    intent: WheelOrderIntent, evidence: RiskEvidence, settings: Settings
) -> OrderRiskCheck:
    cost = _order_notional(intent)
    buffer = max(
        settings.min_cash_buffer_usd,
        evidence.account.net_liquidation * settings.min_cash_buffer_pct,
    )
    # A crypto BUY must not consume the cash securing open/pending short puts,
    # NOR the cash already committed to resting crypto BUYs (ris-1): a resting
    # crypto BUY that later fills would otherwise double-commit the same cash.
    put_obligations = evidence.existing_short_put_obligation + evidence.pending_open_put_obligation
    available = (
        evidence.account.total_cash
        - buffer
        - put_obligations
        - evidence.pending_crypto_buy_notional
    )
    if cost <= available:
        return _passed("crypto_cash_sufficiency", f"cost {cost} within available {available}")
    return _failed(
        "crypto_cash_sufficiency",
        f"cost {cost} exceeds available cash {available} (after buffer {buffer}, "
        f"put obligations {put_obligations}, pending crypto buys "
        f"{evidence.pending_crypto_buy_notional})",
    )


def _crypto_sell_holding_check(intent: WheelOrderIntent, evidence: RiskEvidence) -> OrderRiskCheck:
    # No shorting: a SELL is bounded by held crypto MINUS quantity already
    # committed to resting SELL orders (ris-3), so two SELLs can never be
    # approved against the same coins. ``held_crypto_quantity`` is trade-date
    # broker truth (positions carry no settled dimension).
    sellable = evidence.held_crypto_quantity - evidence.crypto_sell_reserved_quantity
    if intent.quantity <= sellable:
        return _passed(
            "crypto_no_short",
            f"selling {intent.quantity} <= sellable {sellable} "
            f"(held {evidence.held_crypto_quantity} - reserved "
            f"{evidence.crypto_sell_reserved_quantity})",
        )
    return _failed(
        "crypto_no_short",
        f"selling {intent.quantity} exceeds sellable {sellable}; no shorting",
    )

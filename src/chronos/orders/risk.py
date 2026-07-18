"""Structured, tri-state order risk engine (docs/LIVE_WHEEL_GAME_PLAN.md M5).

Distinct from the autonomous ``chronos.risk`` package (never imported): this
engine produces an :class:`OrderRiskDecision` whose overall result is PASS only
when *every* check is PASS. Any FAIL — or any UNKNOWN, because unknown fails
closed — blocks submission. Decisions carry an expiry so stale evidence cannot
authorize a later submission.

The engine is a pure function of explicitly-supplied :class:`RiskEvidence`; the
service gathers that evidence (account, reconciliation, session, positions,
wheel state) and never lets a primitive's ``ValueError`` escape — a bad or
missing input becomes an UNKNOWN check, not an exception.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from chronos.config.settings import Settings
from chronos.domain.enums import (
    OrderIntent,
    ProductFamily,
    ReconciliationStatus,
    RiskCheckStatus,
)
from chronos.domain.models import AccountSummary, ChronosModel, OptionContract
from chronos.orders.intent import WheelOrderIntent
from chronos.services.trading_hours import SessionDecision
from chronos.strategy import capital, reservations

DEFAULT_RISK_DECISION_TTL_SECONDS = 60


class OrderRiskCheck(ChronosModel):
    name: str
    status: RiskCheckStatus
    detail: str


class OrderRiskDecision(ChronosModel):
    decision_id: str
    overall: RiskCheckStatus
    checks: tuple[OrderRiskCheck, ...]
    decided_at: datetime
    expires_at: datetime

    @property
    def approved(self) -> bool:
        return self.overall is RiskCheckStatus.PASS


def order_risk_decision_from_record(
    checks: tuple[tuple[str, RiskCheckStatus, str], ...],
    *,
    decision_id: str,
    overall: RiskCheckStatus,
    decided_at: datetime,
    expires_at: datetime,
) -> OrderRiskDecision:
    """Rebuild an :class:`OrderRiskDecision` from persisted check tuples."""

    return OrderRiskDecision(
        decision_id=decision_id,
        overall=overall,
        checks=tuple(
            OrderRiskCheck(name=name, status=status, detail=detail)
            for name, status, detail in checks
        ),
        decided_at=decided_at,
        expires_at=expires_at,
    )


class RiskEvidence(ChronosModel):
    """Everything the engine needs, gathered by the service from fresh reads."""

    account: AccountSummary
    reconciliation_status: ReconciliationStatus
    session: SessionDecision
    # Wheel-stage context for opening intents (None ⇒ unknown ⇒ fail).
    wheel_eligible_action: OrderIntent | None = None
    wheel_opening_locked: bool = False
    wheel_manual_review: bool = False
    # Position / allocation evidence (all gross, never premium-netted).
    existing_short_put_obligation: Decimal = Decimal("0")
    pending_open_put_obligation: Decimal = Decimal("0")
    current_symbol_allocation: Decimal = Decimal("0")
    current_total_wheel_allocation: Decimal = Decimal("0")
    active_short_option_contracts: int = 0
    opening_orders_today: int = 0
    settled_long_shares: Decimal = Decimal("0")
    shares_covering_short_calls: Decimal = Decimal("0")
    shares_reserved_for_pending_calls: Decimal = Decimal("0")
    shares_reserved_by_other_orders: Decimal = Decimal("0")
    held_short_option_contracts: int = 0


def _passed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.PASS, detail=detail)


def _failed(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.FAIL, detail=detail)


def _unknown(name: str, detail: str) -> OrderRiskCheck:
    return OrderRiskCheck(name=name, status=RiskCheckStatus.UNKNOWN, detail=detail)


class OrderRiskEngine:
    """Compose the strategy primitives into one tri-state risk decision."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(
        self,
        intent: WheelOrderIntent,
        evidence: RiskEvidence,
        *,
        now: datetime,
        decision_id: str | None = None,
        ttl_seconds: int = DEFAULT_RISK_DECISION_TTL_SECONDS,
    ) -> OrderRiskDecision:
        checks: list[OrderRiskCheck] = [
            self._check_eligibility(intent),
            self._check_reconciled(evidence),
            self._check_market_open(evidence),
            self._check_limit_only(intent),
            self._check_max_contracts(intent),
        ]
        if intent.open_close_effect == "OPEN":
            checks.append(self._check_opening_count(evidence))
        if intent.product_family is ProductFamily.OPTION:
            checks.extend(self._option_checks(intent, evidence))
        else:
            checks.extend(self._stock_checks(intent, evidence))

        overall = (
            RiskCheckStatus.PASS
            if all(check.status is RiskCheckStatus.PASS for check in checks)
            else RiskCheckStatus.FAIL
        )
        return OrderRiskDecision(
            decision_id=decision_id or uuid.uuid4().hex,
            overall=overall,
            checks=tuple(checks),
            decided_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    @staticmethod
    def is_expired(decision: OrderRiskDecision, *, now: datetime) -> bool:
        return now >= decision.expires_at

    # --- individual checks -------------------------------------------------

    def _check_eligibility(self, intent: WheelOrderIntent) -> OrderRiskCheck:
        from chronos.strategy.eligibility import evaluate as evaluate_eligibility

        result = evaluate_eligibility(self._settings, intent.symbol, intent.product_family)
        if result.allowed:
            return _passed("symbol_family_eligibility", f"{intent.symbol} is allowlisted")
        return _failed("symbol_family_eligibility", "; ".join(result.reasons))

    @staticmethod
    def _check_reconciled(evidence: RiskEvidence) -> OrderRiskCheck:
        if evidence.reconciliation_status is ReconciliationStatus.RECONCILED:
            return _passed("reconciled_proven_state", "broker state reconciled")
        return _failed(
            "reconciled_proven_state",
            f"reconciliation is {evidence.reconciliation_status.value}, not RECONCILED",
        )

    @staticmethod
    def _check_market_open(evidence: RiskEvidence) -> OrderRiskCheck:
        session = evidence.session
        if session.may_submit:
            return _passed("market_open", session.reason)
        if session.session.value == "AMBIGUOUS":
            return _unknown("market_open", session.reason)
        return _failed("market_open", session.reason)

    @staticmethod
    def _check_limit_only(intent: WheelOrderIntent) -> OrderRiskCheck:
        if intent.order_type == "LMT" and intent.time_in_force == "DAY" and intent.limit_price > 0:
            return _passed("limit_only", "limit DAY order with a positive price")
        return _failed("limit_only", "only positive-price limit DAY orders are permitted")

    def _check_max_contracts(self, intent: WheelOrderIntent) -> OrderRiskCheck:
        if intent.product_family is not ProductFamily.OPTION:
            return _passed("max_contracts_per_order", "not an options order")
        if intent.quantity <= self._settings.max_contracts_per_order:
            return _passed(
                "max_contracts_per_order",
                f"{intent.quantity} <= {self._settings.max_contracts_per_order}",
            )
        return _failed(
            "max_contracts_per_order",
            f"{intent.quantity} exceeds the {self._settings.max_contracts_per_order} cap",
        )

    def _check_opening_count(self, evidence: RiskEvidence) -> OrderRiskCheck:
        limit = self._settings.max_opening_orders_per_day
        if evidence.opening_orders_today + 1 <= limit:
            return _passed(
                "max_opening_orders_per_day",
                f"{evidence.opening_orders_today + 1} <= {limit}",
            )
        return _failed(
            "max_opening_orders_per_day",
            f"{evidence.opening_orders_today + 1} would exceed the {limit} daily cap",
        )

    def _option_checks(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> list[OrderRiskCheck]:
        contract = intent.contract
        if not isinstance(contract, OptionContract):
            return [_failed("option_contract", "OPTION intent without an OptionContract")]
        checks = [self._check_deliverable_verified(contract)]
        if intent.intent is OrderIntent.OPEN_SHORT_PUT:
            checks.extend(self._short_put_checks(intent, contract, evidence))
        elif intent.intent is OrderIntent.OPEN_COVERED_CALL:
            checks.extend(self._covered_call_checks(intent, contract, evidence))
        else:  # CLOSE_SHORT_OPTION (buy-to-close)
            checks.append(self._check_close_holds_short(intent, evidence))
        return checks

    @staticmethod
    def _check_deliverable_verified(contract: OptionContract) -> OrderRiskCheck:
        if contract.has_verified_standard_deliverable:
            return _passed("standard_deliverable_verified", "verified 1:1 standard deliverable")
        return _failed(
            "standard_deliverable_verified",
            "option deliverable is not a verified standard 100-share deliverable",
        )

    def _capital_context(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> capital.CapitalContext:
        from chronos.utils.identifiers import account_fingerprint

        return capital.CapitalContext(
            account_fingerprint=account_fingerprint(intent.account_id),
            net_liquidation=evidence.account.net_liquidation,
            uncommitted_cash=evidence.account.total_cash,
            current_symbol_allocation=evidence.current_symbol_allocation,
            current_total_wheel_allocation=evidence.current_total_wheel_allocation,
            max_symbol_allocation_pct=self._settings.max_symbol_allocation_pct,
            max_total_wheel_allocation_pct=self._settings.max_total_wheel_allocation_pct,
        )

    def _short_put_checks(
        self,
        intent: WheelOrderIntent,
        contract: OptionContract,
        evidence: RiskEvidence,
    ) -> list[OrderRiskCheck]:
        checks: list[OrderRiskCheck] = []
        deliverable = contract.deliverable_shares or contract.multiplier
        try:
            capital_check = capital.evaluate_short_put_capital(
                context=self._capital_context(intent, evidence),
                strike=contract.strike,
                multiplier=contract.multiplier,
                verified_deliverable_shares=deliverable,
                contracts=intent.quantity,
            )
        except ValueError as error:
            return [_unknown("cash_secured_put", f"capital inputs rejected: {error}")]
        obligation = capital.short_put_notional(
            strike=contract.strike, multiplier=deliverable, contracts=intent.quantity
        )
        cash_buffer = max(
            self._settings.min_cash_buffer_usd,
            evidence.account.net_liquidation * self._settings.min_cash_buffer_pct,
        )
        summary = reservations.reserve_cash(
            reservations.CashReservationInput(
                existing_short_put_obligation=evidence.existing_short_put_obligation,
                pending_put_order_obligation=evidence.pending_open_put_obligation,
                proposed_order_obligation=obligation,
                cash_buffer=cash_buffer,
            ),
            available_cash=evidence.account.total_cash,
        )
        if capital_check.passed and summary.sufficient:
            checks.append(
                _passed(
                    "cash_secured_put",
                    f"gross obligation {obligation} covered with buffer {cash_buffer}",
                )
            )
        else:
            reasons = list(capital_check.reasons) + list(summary.reasons)
            checks.append(_failed("cash_secured_put", "; ".join(reasons) or "insufficient cash"))
        checks.append(self._concentration_check(capital_check))
        checks.append(self._max_open_short_check(intent, evidence))
        checks.append(self._max_gross_assignment_check(obligation))
        return checks

    def _covered_call_checks(
        self,
        intent: WheelOrderIntent,
        contract: OptionContract,
        evidence: RiskEvidence,
    ) -> list[OrderRiskCheck]:
        deliverable = contract.deliverable_shares or contract.multiplier
        share_summary = reservations.reserve_shares(
            reservations.ShareReservationInput(
                settled_long_shares=evidence.settled_long_shares,
                shares_covering_short_calls=evidence.shares_covering_short_calls,
                shares_reserved_for_pending_calls=evidence.shares_reserved_for_pending_calls,
                shares_reserved_by_other_orders=evidence.shares_reserved_by_other_orders,
                contract_multiplier=contract.multiplier,
            )
        )
        try:
            capital_check = capital.evaluate_covered_call_capital(
                context=self._capital_context(intent, evidence),
                multiplier=contract.multiplier,
                verified_deliverable_shares=deliverable,
                contracts=intent.quantity,
                unencumbered_shares=max(share_summary.unencumbered_shares, Decimal("0")),
            )
        except ValueError as error:
            return [_unknown("covered_call_coverage", f"capital inputs rejected: {error}")]
        covered = (
            capital_check.passed
            and not share_summary.reasons
            and intent.quantity <= share_summary.max_new_call_contracts
        )
        if covered:
            check = _passed(
                "covered_call_coverage",
                f"{intent.quantity} calls fully covered "
                f"(capacity {share_summary.max_new_call_contracts})",
            )
        else:
            reasons = list(capital_check.reasons) + list(share_summary.reasons)
            check = _failed(
                "covered_call_coverage",
                "; ".join(reasons) or "calls are not fully covered by unencumbered shares",
            )
        return [check, self._concentration_check(capital_check)]

    def _check_close_holds_short(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> OrderRiskCheck:
        if intent.quantity <= evidence.held_short_option_contracts:
            return _passed(
                "held_position_sufficiency",
                f"closing {intent.quantity} <= {evidence.held_short_option_contracts} held short",
            )
        return _failed(
            "held_position_sufficiency",
            f"closing {intent.quantity} exceeds {evidence.held_short_option_contracts} held short",
        )

    def _stock_checks(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> list[OrderRiskCheck]:
        from chronos.orders.stocks import validate_stock_order

        return validate_stock_order(intent, evidence=evidence, settings=self._settings)

    def _concentration_check(self, capital_check: capital.CapitalCheck) -> OrderRiskCheck:
        breach = any("allocation exceeds" in reason for reason in capital_check.reasons)
        net_liq_bad = any("Net liquidation" in reason for reason in capital_check.reasons)
        if net_liq_bad:
            return _unknown("concentration", "; ".join(capital_check.reasons))
        if breach:
            return _failed("concentration", "; ".join(capital_check.reasons))
        return _passed(
            "concentration",
            f"symbol {capital_check.resulting_symbol_allocation_pct}, "
            f"portfolio {capital_check.resulting_total_wheel_allocation_pct}",
        )

    def _max_open_short_check(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> OrderRiskCheck:
        limit = self._settings.max_open_short_option_contracts
        resulting = evidence.active_short_option_contracts + intent.quantity
        if resulting <= limit:
            return _passed("max_open_short_option_contracts", f"{resulting} <= {limit}")
        return _failed(
            "max_open_short_option_contracts", f"{resulting} would exceed the {limit} cap"
        )

    def _max_gross_assignment_check(self, obligation: Decimal) -> OrderRiskCheck:
        limit = self._settings.max_gross_assignment_usd
        if obligation <= limit:
            return _passed("max_gross_assignment_usd", f"{obligation} <= {limit}")
        return _failed("max_gross_assignment_usd", f"{obligation} exceeds the {limit} cap")

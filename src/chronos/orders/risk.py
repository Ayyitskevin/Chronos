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

from pydantic import AwareDatetime, Field, model_validator

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
_QQQ_FIVE_TOOL_CANDIDATE_SHA256 = "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
_QQQ_CAPITAL_BASE_USD_MAX = Decimal(3000)
_QQQ_NATIVE_STOP_RISK_FRACTION = Decimal("0.01")
_QQQ_NATIVE_STOP_RISK_USD_MAX = Decimal(30)
_QQQ_CVAR_RISK_FRACTION = Decimal("0.015")
_QQQ_CVAR_RISK_USD_MAX = Decimal(45)


class QQQPositionManagementRiskProjection(ChronosModel):
    """Deterministic protected-entry projection under the frozen QQQ limits."""

    handoff_entry_usd: Decimal = Field(gt=0)
    handoff_risk_distance_usd: Decimal = Field(gt=0)
    applicable_capital_base_usd: Decimal = Field(gt=0)
    native_stop_risk_usd: Decimal = Field(gt=0)
    native_stop_budget_usd: Decimal = Field(gt=0)
    cvar_risk_usd: Decimal = Field(gt=0)
    cvar_budget_usd: Decimal = Field(gt=0)
    gross_notional_usd: Decimal = Field(gt=0)

    @property
    def failures(self) -> tuple[str, ...]:
        failures: list[str] = []
        if self.native_stop_risk_usd > self.native_stop_budget_usd:
            failures.append(
                f"native stop {self.native_stop_risk_usd} exceeds {self.native_stop_budget_usd}"
            )
        if self.cvar_risk_usd > self.cvar_budget_usd:
            failures.append(f"CVaR {self.cvar_risk_usd} exceeds {self.cvar_budget_usd}")
        if self.gross_notional_usd > self.applicable_capital_base_usd:
            failures.append(
                f"gross {self.gross_notional_usd} exceeds {self.applicable_capital_base_usd}"
            )
        return tuple(failures)

    @property
    def passed(self) -> bool:
        return not self.failures


class QQQPositionManagementRiskEvidence(ChronosModel):
    """Signal-time facts required to manage an eventual QQQ opening fill.

    The evidence provider, not the order client, supplies this value.  It is
    persisted with the authorizing risk decision so post-fill admission can
    reconstruct the exact frozen risk geometry without accepting new economic
    inputs from its caller.
    """

    schema_version: str = "chronos-qqq-position-risk-v1"
    candidate_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: AwareDatetime
    signal_time_entry_basis_usd: Decimal = Field(gt=0)
    signal_time_initial_stop_price_usd: Decimal = Field(gt=0)
    signal_time_risk_distance_usd: Decimal = Field(gt=0)
    marked_strategy_nav_usd: Decimal = Field(gt=0)
    unit_exposure_cvar_loss_fraction: Decimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _exact_signal_geometry(self) -> QQQPositionManagementRiskEvidence:
        if self.schema_version != "chronos-qqq-position-risk-v1":
            raise ValueError("unsupported QQQ position-risk evidence schema")
        if (
            self.signal_time_entry_basis_usd - self.signal_time_initial_stop_price_usd
            != self.signal_time_risk_distance_usd
        ):
            raise ValueError("signal-time entry, stop, and risk distance disagree")
        return self

    def project(
        self,
        *,
        quantity: Decimal,
        protected_entry_price: Decimal,
    ) -> QQQPositionManagementRiskProjection:
        """Project exact risk at the worst executable protected entry price."""

        handoff_entry = max(self.signal_time_entry_basis_usd, protected_entry_price)
        handoff_distance = max(
            self.signal_time_risk_distance_usd,
            handoff_entry - self.signal_time_initial_stop_price_usd,
        )
        applicable_base = min(self.marked_strategy_nav_usd, _QQQ_CAPITAL_BASE_USD_MAX)
        return QQQPositionManagementRiskProjection(
            handoff_entry_usd=handoff_entry,
            handoff_risk_distance_usd=handoff_distance,
            applicable_capital_base_usd=applicable_base,
            native_stop_risk_usd=handoff_distance * quantity,
            native_stop_budget_usd=min(
                applicable_base * _QQQ_NATIVE_STOP_RISK_FRACTION,
                _QQQ_NATIVE_STOP_RISK_USD_MAX,
            ),
            cvar_risk_usd=(handoff_entry * quantity * self.unit_exposure_cvar_loss_fraction),
            cvar_budget_usd=min(
                applicable_base * _QQQ_CVAR_RISK_FRACTION,
                _QQQ_CVAR_RISK_USD_MAX,
            ),
            gross_notional_usd=handoff_entry * quantity,
        )


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
    reconciliation_generation: int | None = Field(default=None, ge=0)
    reconciliation_session_id: str | None = Field(default=None, min_length=1)

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
    reconciliation_generation: int | None = None,
    reconciliation_session_id: str | None = None,
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
        reconciliation_generation=reconciliation_generation,
        reconciliation_session_id=reconciliation_session_id,
    )


class RiskEvidence(ChronosModel):
    """Everything the engine needs, gathered by the service from fresh reads."""

    account: AccountSummary
    reconciliation_status: ReconciliationStatus
    reconciliation_generation: int | None = Field(default=None, ge=0)
    reconciliation_session_id: str | None = Field(default=None, min_length=1)
    session: SessionDecision
    # Wheel-stage context for opening intents (None ⇒ unknown ⇒ fail).
    wheel_eligible_action: OrderIntent | None = None
    wheel_opening_locked: bool = False
    wheel_manual_review: bool = False
    # Position / allocation evidence (all gross, never premium-netted). The
    # ge=0 constraints keep a malformed negative obligation from reaching the
    # reservation primitives as a hard ValueError.
    existing_short_put_obligation: Decimal = Field(default=Decimal("0"), ge=0)
    pending_open_put_obligation: Decimal = Field(default=Decimal("0"), ge=0)
    current_symbol_allocation: Decimal = Field(default=Decimal("0"), ge=0)
    current_total_wheel_allocation: Decimal = Field(default=Decimal("0"), ge=0)
    active_short_option_contracts: int = Field(default=0, ge=0)
    # None ⇒ the count could not be taken ⇒ UNKNOWN ⇒ opening blocked (R-25).
    # Deliberately NOT `int = 0`: a cap whose evidence defaults to "none used"
    # is a cap that reports full headroom precisely when it cannot see.
    opening_orders_today: int | None = Field(default=None, ge=0)
    settled_long_shares: Decimal = Field(default=Decimal("0"), ge=0)
    shares_covering_short_calls: Decimal = Field(default=Decimal("0"), ge=0)
    shares_reserved_for_pending_calls: Decimal = Field(default=Decimal("0"), ge=0)
    shares_reserved_by_other_orders: Decimal = Field(default=Decimal("0"), ge=0)
    held_short_option_contracts: int = Field(default=0, ge=0)
    # Crypto evidence (ADR-0010 §4), kept SEPARATE from the wheel aggregates so a
    # crypto holding never distorts stock concentration or cash math.
    held_crypto_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    crypto_sell_reserved_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    current_crypto_allocation: Decimal = Field(default=Decimal("0"), ge=0)
    crypto_allocation_marked: bool = True
    pending_crypto_buy_notional: Decimal = Field(default=Decimal("0"), ge=0)
    # Optional because every non-QQQ order and the currently manual Wheel path
    # has no reason to carry it.  Managed-position admission later requires the
    # exact typed record and refuses when it is absent.
    qqq_position_management: QQQPositionManagementRiskEvidence | None = None


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
        elif intent.product_family is ProductFamily.CRYPTO:
            checks.extend(self._crypto_checks(intent, evidence))
        else:
            checks.extend(self._stock_checks(intent, evidence))
        if evidence.qqq_position_management is not None:
            checks.append(self._check_qqq_position_management_risk(intent, evidence, now=now))

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
            reconciliation_generation=evidence.reconciliation_generation,
            reconciliation_session_id=evidence.reconciliation_session_id,
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
        if (
            evidence.reconciliation_status is ReconciliationStatus.RECONCILED
            and evidence.reconciliation_generation is not None
            and evidence.reconciliation_session_id is not None
        ):
            return _passed("reconciled_proven_state", "broker state reconciled")
        if evidence.reconciliation_status is ReconciliationStatus.RECONCILED:
            return _unknown(
                "reconciled_proven_state",
                "reconciliation provenance is incomplete; evidence cannot authorize submission",
            )
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

    def _check_limit_only(self, intent: WheelOrderIntent) -> OrderRiskCheck:
        # Family-conditional TIF (ADR-0010 §3): OPTION/STOCK are DAY-only. CRYPTO
        # always permits DAY (the safe default) and additively permits the
        # owner-verified crypto TIF, so enabling IOC never blocks a plain DAY
        # crypto order (adversarial-review F3). LMT and a positive price are
        # required on every family (market orders are impossible).
        if intent.product_family is ProductFamily.CRYPTO:
            allowed_tif = {"DAY", self._settings.crypto_time_in_force}
        else:
            allowed_tif = {"DAY"}
        if (
            intent.order_type == "LMT"
            and intent.time_in_force in allowed_tif
            and intent.limit_price > 0
        ):
            return _passed(
                "limit_only", f"limit {intent.time_in_force} order with a positive price"
            )
        return _failed(
            "limit_only",
            f"only positive-price limit orders with TIF in {sorted(allowed_tif)} are permitted",
        )

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
        used = evidence.opening_orders_today
        if used is None:
            # "We could not ask" is not "no". Until M10 this check could not
            # reach this branch at all, because the evidence was never gathered
            # and the field's 0 default made every call read as full headroom —
            # the cap had never refused anything in its life (R-25).
            return _unknown(
                "max_opening_orders_per_day",
                f"today's opening count is unavailable, so the {limit} daily cap cannot be applied",
            )
        if used + 1 <= limit:
            return _passed("max_opening_orders_per_day", f"{used + 1} <= {limit}")
        return _failed(
            "max_opening_orders_per_day",
            f"{used + 1} would exceed the {limit} daily cap",
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
        # Every primitive that can raise ValueError (bad deliverable, negative
        # bucket, ...) is inside this guard so a malformed input becomes an
        # UNKNOWN check, never an exception escaping the engine (fail-closed).
        try:
            capital_check = capital.evaluate_short_put_capital(
                context=self._capital_context(intent, evidence),
                strike=contract.strike,
                multiplier=contract.multiplier,
                verified_deliverable_shares=deliverable,
                contracts=int(intent.quantity),
            )
            obligation = capital.short_put_notional(
                strike=contract.strike, multiplier=deliverable, contracts=int(intent.quantity)
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
                # Cash committed to resting crypto BUYs is not available to
                # secure a new put (ADR-0010 ris-1); 0 until crypto is enabled.
                available_cash=evidence.account.total_cash - evidence.pending_crypto_buy_notional,
            )
        except ValueError as error:
            return [_unknown("cash_secured_put", f"capital inputs rejected: {error}")]
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
                contracts=int(intent.quantity),
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

    @staticmethod
    def _check_qqq_position_management_risk(
        intent: WheelOrderIntent,
        evidence: RiskEvidence,
        *,
        now: datetime,
    ) -> OrderRiskCheck:
        """Apply the frozen Five-Tool budgets to the protected entry terms."""

        managed = evidence.qqq_position_management
        if managed is None:  # pragma: no cover - caller invokes only when present
            return _unknown(
                "qqq_position_management_risk",
                "QQQ position-management risk evidence is unavailable",
            )
        if (
            intent.product_family is not ProductFamily.STOCK
            or intent.symbol != "QQQ"
            or intent.intent is not OrderIntent.OPEN_LONG_STOCK
            or managed.candidate_spec_sha256 != _QQQ_FIVE_TOOL_CANDIDATE_SHA256
            or managed.as_of > now
        ):
            return _failed(
                "qqq_position_management_risk",
                "management evidence does not identify the current long QQQ candidate",
            )

        # The protected buy limit is the worst executable entry price.  Its
        # distance from the frozen structural stop can exceed the signal-close
        # distance after a gap, so entry authorization uses the larger distance
        # and price exactly as the QQQ preregistration requires.
        projection = managed.project(
            quantity=intent.quantity,
            protected_entry_price=intent.limit_price,
        )
        if projection.failures:
            return _failed("qqq_position_management_risk", "; ".join(projection.failures))
        return _passed(
            "qqq_position_management_risk",
            f"native stop {projection.native_stop_risk_usd} <= "
            f"{projection.native_stop_budget_usd}; CVaR {projection.cvar_risk_usd} <= "
            f"{projection.cvar_budget_usd}; gross {projection.gross_notional_usd} <= "
            f"{projection.applicable_capital_base_usd}",
        )

    def _crypto_checks(
        self, intent: WheelOrderIntent, evidence: RiskEvidence
    ) -> list[OrderRiskCheck]:
        from chronos.orders.crypto import validate_crypto_order

        return validate_crypto_order(intent, evidence=evidence, settings=self._settings)

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

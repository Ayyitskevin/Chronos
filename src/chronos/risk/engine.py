"""The independent risk engine (Phase 8).

Every order intent passes through ``RiskEngine.validate``. The engine:

- is deny-by-default: an all-zero policy rejects everything;
- evaluates *all* checks and reports every failure, machine- and
  human-readable;
- fails closed: any internal exception becomes a denial, never an approval;
- cannot be modified at runtime: the policy object is frozen and there are no
  setters;
- mints ``RiskApproval`` tokens bound to the exact intent id and its own
  instance token. The execution engine refuses intents whose approval was not
  minted by the engine instance it was wired with, so strategy code cannot
  fabricate an approval or replay one across engines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chronos.control.halt import HaltState
from chronos.control.modes import ExecutionCapability, ModeLock
from chronos.domain.enums import OrderSide
from chronos.execution.intents import OrderIntent
from chronos.risk.policy import RiskPolicy


class RiskRejectionCode(StrEnum):
    HALTED = "HALTED"
    MODE_FORBIDS_ORDERS = "MODE_FORBIDS_ORDERS"
    ACCOUNT_STATE_MISSING = "ACCOUNT_STATE_MISSING"
    MARKET_STATE_MISSING = "MARKET_STATE_MISSING"
    STRATEGY_NOT_ALLOWED = "STRATEGY_NOT_ALLOWED"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    DIRECTION_NOT_ALLOWED = "DIRECTION_NOT_ALLOWED"
    STOP_REQUIRED = "STOP_REQUIRED"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    ZERO_CAPITAL_AUTHORIZED = "ZERO_CAPITAL_AUTHORIZED"
    NOTIONAL_LIMIT = "NOTIONAL_LIMIT"
    AGGREGATE_EXPOSURE_LIMIT = "AGGREGATE_EXPOSURE_LIMIT"
    SYMBOL_EXPOSURE_LIMIT = "SYMBOL_EXPOSURE_LIMIT"
    RISK_PER_TRADE_LIMIT = "RISK_PER_TRADE_LIMIT"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_OPEN_ORDERS = "MAX_OPEN_ORDERS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    PYRAMIDING_FORBIDDEN = "PYRAMIDING_FORBIDDEN"
    SELL_WITHOUT_POSITION = "SELL_WITHOUT_POSITION"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    INTERNAL_ERROR_FAIL_CLOSED = "INTERNAL_ERROR_FAIL_CLOSED"


@dataclass(frozen=True, slots=True)
class AccountView:
    """Broker-derived account evidence supplied by the execution engine."""

    account_equity_usd: float
    cash_usd: float
    position_shares: dict[str, int]
    position_notional_usd: dict[str, float]
    open_order_count: int
    realized_pnl_today_usd: float
    realized_pnl_week_usd: float
    peak_equity_usd: float
    consecutive_losses: int
    as_of_utc: datetime


@dataclass(frozen=True, slots=True)
class MarketViewEntry:
    last_price: float
    bar_close_utc: datetime
    quote_utc: datetime


@dataclass(frozen=True, slots=True)
class RiskApproval:
    """Proof that a specific intent passed this engine. Not user-constructible
    in practice: the execution engine checks ``minted_by`` identity."""

    intent_id: str
    policy_version: str
    policy_hash: str
    minted_by: object


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    intent_id: str
    codes: tuple[RiskRejectionCode, ...]
    explanations: tuple[str, ...]
    policy_version: str
    approval: RiskApproval | None

    @property
    def human_summary(self) -> str:
        if self.approved:
            return f"intent {self.intent_id} approved under policy {self.policy_version}"
        joined = "; ".join(self.explanations)
        return f"intent {self.intent_id} rejected: {joined}"


class RiskEngine:
    """Deny-by-default validator for order intents."""

    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy
        self._token: object = object()
        self._seen_intent_ids: set[str] = set()

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    @property
    def token(self) -> object:
        return self._token

    def validate(
        self,
        intent: OrderIntent,
        *,
        account: AccountView | None,
        market: MarketViewEntry | None,
        halt: HaltState,
        mode_lock: ModeLock,
        now_utc: datetime,
    ) -> RiskDecision:
        try:
            return self._validate(
                intent,
                account=account,
                market=market,
                halt=halt,
                mode_lock=mode_lock,
                now_utc=now_utc,
            )
        except Exception as error:
            return RiskDecision(
                approved=False,
                intent_id=intent.intent_id,
                codes=(RiskRejectionCode.INTERNAL_ERROR_FAIL_CLOSED,),
                explanations=(f"risk engine error; failing closed: {error!r}",),
                policy_version=self._policy.policy_version,
                approval=None,
            )

    def _validate(
        self,
        intent: OrderIntent,
        *,
        account: AccountView | None,
        market: MarketViewEntry | None,
        halt: HaltState,
        mode_lock: ModeLock,
        now_utc: datetime,
    ) -> RiskDecision:
        policy = self._policy
        failures: list[tuple[RiskRejectionCode, str]] = []

        def deny(code: RiskRejectionCode, explanation: str) -> None:
            failures.append((code, explanation))

        if halt.trading_blocked:
            reason = halt.reason.value if halt.reason else "unknown"
            deny(RiskRejectionCode.HALTED, f"trading is halted ({reason}): {halt.detail}")

        if mode_lock.capability not in (
            ExecutionCapability.SIMULATED_ONLY,
            ExecutionCapability.PAPER_SUBMISSION,
        ):
            deny(
                RiskRejectionCode.MODE_FORBIDS_ORDERS,
                f"mode {mode_lock.mode} with capability {mode_lock.capability} forbids orders",
            )

        if intent.intent_id in self._seen_intent_ids:
            deny(
                RiskRejectionCode.DUPLICATE_INTENT,
                "an identical intent id was already evaluated by this engine instance",
            )

        if account is None:
            deny(RiskRejectionCode.ACCOUNT_STATE_MISSING, "no account state evidence supplied")
        if market is None:
            deny(RiskRejectionCode.MARKET_STATE_MISSING, "no market snapshot supplied")

        if intent.strategy_id not in policy.allowed_strategy_ids:
            deny(
                RiskRejectionCode.STRATEGY_NOT_ALLOWED,
                f"strategy {intent.strategy_id!r} is not on the policy allowlist",
            )
        if intent.symbol not in policy.allowed_symbols:
            deny(
                RiskRejectionCode.SYMBOL_NOT_ALLOWED,
                f"symbol {intent.symbol!r} is not on the policy allowlist",
            )

        is_entry = intent.side is OrderSide.BUY
        if is_entry and not policy.allow_long_entries:
            deny(RiskRejectionCode.DIRECTION_NOT_ALLOWED, "long entries are not enabled")
        if is_entry and intent.stop_price is None:
            deny(RiskRejectionCode.STOP_REQUIRED, "entries must carry a protective stop price")

        if market is not None:
            quote_age = (now_utc - market.quote_utc).total_seconds()
            bar_age = (now_utc - market.bar_close_utc).total_seconds()
            if policy.max_quote_age_seconds <= 0 or quote_age > policy.max_quote_age_seconds:
                deny(
                    RiskRejectionCode.STALE_MARKET_DATA,
                    f"quote age {quote_age:.0f}s exceeds limit {policy.max_quote_age_seconds:.0f}s",
                )
            if policy.max_bar_age_seconds <= 0 or bar_age > policy.max_bar_age_seconds:
                deny(
                    RiskRejectionCode.STALE_MARKET_DATA,
                    f"bar age {bar_age:.0f}s exceeds limit {policy.max_bar_age_seconds:.0f}s",
                )
            if market.last_price > 0:
                deviation = abs(float(intent.limit_price) / market.last_price - 1.0)
                if deviation > policy.max_price_deviation_fraction:
                    deny(
                        RiskRejectionCode.PRICE_DEVIATION,
                        f"limit price deviates {deviation:.2%} from last trade; "
                        f"allowed {policy.max_price_deviation_fraction:.2%}",
                    )
            else:
                deny(RiskRejectionCode.MARKET_STATE_MISSING, "market last price is not positive")

        if account is not None:
            equity = account.account_equity_usd
            notional = float(intent.notional)
            gross_exposure = sum(abs(v) for v in account.position_notional_usd.values())
            symbol_exposure = abs(account.position_notional_usd.get(intent.symbol, 0.0))
            held_shares = account.position_shares.get(intent.symbol, 0)

            if policy.max_bot_capital_usd <= 0:
                deny(
                    RiskRejectionCode.ZERO_CAPITAL_AUTHORIZED,
                    "authorized bot capital is zero; no order can be approved",
                )
            if is_entry:
                if notional > policy.max_position_notional_usd:
                    deny(
                        RiskRejectionCode.NOTIONAL_LIMIT,
                        f"order notional ${notional:,.2f} exceeds "
                        f"${policy.max_position_notional_usd:,.2f}",
                    )
                if gross_exposure + notional > policy.max_aggregate_exposure_usd:
                    deny(
                        RiskRejectionCode.AGGREGATE_EXPOSURE_LIMIT,
                        f"gross exposure ${gross_exposure + notional:,.2f} would exceed "
                        f"${policy.max_aggregate_exposure_usd:,.2f}",
                    )
                if equity > 0 and (symbol_exposure + notional) / equity > (
                    policy.max_symbol_exposure_fraction
                ):
                    deny(
                        RiskRejectionCode.SYMBOL_EXPOSURE_LIMIT,
                        f"symbol exposure would be "
                        f"{(symbol_exposure + notional) / equity:.2%} of equity; allowed "
                        f"{policy.max_symbol_exposure_fraction:.2%}",
                    )
                if intent.stop_price is not None and equity > 0:
                    per_share_risk = float(intent.limit_price - intent.stop_price)
                    trade_risk = max(per_share_risk, 0.0) * intent.quantity
                    if per_share_risk <= 0:
                        deny(
                            RiskRejectionCode.STOP_REQUIRED,
                            "stop price must be below the limit price for a long entry",
                        )
                    elif trade_risk / equity > policy.max_risk_per_trade_fraction:
                        deny(
                            RiskRejectionCode.RISK_PER_TRADE_LIMIT,
                            f"trade risk ${trade_risk:,.2f} is "
                            f"{trade_risk / equity:.2%} of equity; allowed "
                            f"{policy.max_risk_per_trade_fraction:.2%}",
                        )
                open_positions = sum(1 for shares in account.position_shares.values() if shares)
                entering_new_symbol = held_shares == 0
                if entering_new_symbol and open_positions + 1 > policy.max_simultaneous_positions:
                    deny(
                        RiskRejectionCode.MAX_POSITIONS,
                        f"{open_positions} positions open; policy allows "
                        f"{policy.max_simultaneous_positions}",
                    )
                if held_shares > 0 and not policy.allow_pyramiding:
                    deny(
                        RiskRejectionCode.PYRAMIDING_FORBIDDEN,
                        "adding to an existing position is not enabled",
                    )
            else:
                if held_shares < intent.quantity:
                    deny(
                        RiskRejectionCode.SELL_WITHOUT_POSITION,
                        f"selling {intent.quantity} shares but only {held_shares} held; "
                        "short selling is not enabled",
                    )
            if account.open_order_count + 1 > policy.max_open_orders:
                deny(
                    RiskRejectionCode.MAX_OPEN_ORDERS,
                    f"{account.open_order_count} orders open; policy allows "
                    f"{policy.max_open_orders}",
                )
            if is_entry and -account.realized_pnl_today_usd >= policy.max_daily_loss_usd:
                deny(
                    RiskRejectionCode.DAILY_LOSS_LIMIT,
                    f"realized loss today ${-account.realized_pnl_today_usd:,.2f} reached "
                    f"the ${policy.max_daily_loss_usd:,.2f} limit",
                )
            if is_entry and -account.realized_pnl_week_usd >= policy.max_weekly_loss_usd:
                deny(
                    RiskRejectionCode.WEEKLY_LOSS_LIMIT,
                    f"realized loss this week ${-account.realized_pnl_week_usd:,.2f} reached "
                    f"the ${policy.max_weekly_loss_usd:,.2f} limit",
                )
            if is_entry and account.peak_equity_usd > 0:
                drawdown = 1.0 - equity / account.peak_equity_usd
                if drawdown >= policy.max_drawdown_fraction:
                    deny(
                        RiskRejectionCode.DRAWDOWN_LIMIT,
                        f"drawdown {drawdown:.2%} reached the "
                        f"{policy.max_drawdown_fraction:.2%} limit",
                    )
            if is_entry and account.consecutive_losses >= policy.max_consecutive_losses:
                deny(
                    RiskRejectionCode.CONSECUTIVE_LOSSES,
                    f"{account.consecutive_losses} consecutive losses reached the "
                    f"limit of {policy.max_consecutive_losses}",
                )

        self._seen_intent_ids.add(intent.intent_id)

        if failures:
            return RiskDecision(
                approved=False,
                intent_id=intent.intent_id,
                codes=tuple(code for code, _ in failures),
                explanations=tuple(text for _, text in failures),
                policy_version=self._policy.policy_version,
                approval=None,
            )
        return RiskDecision(
            approved=True,
            intent_id=intent.intent_id,
            codes=(),
            explanations=(),
            policy_version=self._policy.policy_version,
            approval=RiskApproval(
                intent_id=intent.intent_id,
                policy_version=self._policy.policy_version,
                policy_hash=self._policy.config_hash,
                minted_by=self._token,
            ),
        )

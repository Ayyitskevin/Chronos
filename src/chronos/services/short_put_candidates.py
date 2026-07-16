"""Read-only short-put candidate assembly behind strict reconciliation gates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isfinite
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from chronos.broker.market_data import (
    ManagedQuote,
    MarketDataManager,
    OptionChainSnapshot,
)
from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
)
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    ChronosModel,
    ConnectionStatus,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    UnderlyingContract,
)
from chronos.services.reconciliation import (
    ConnectionRunner,
    ReconciliationResult,
    ReconciliationSnapshotView,
)
from chronos.strategy.capital import CapitalContext
from chronos.strategy.strike_resolver import (
    ResolverContext,
    ResolverPolicy,
    StrikeResolution,
    StrikeResolver,
    spot_price,
)
from chronos.utils.identifiers import account_fingerprint, normalize_account_fingerprint
from chronos.utils.logging import mask_account_id

_LOGGER = logging.getLogger("chronos.services.short_put_candidates")
_RANKABLE_CONNECTION_QUALITIES = frozenset(
    {
        DataQuality.LIVE,
        DataQuality.FROZEN,
        DataQuality.DEMO,
    }
)


class ReconciliationReader(Protocol):
    """Fresh reconciliation surface needed by candidate evaluation."""

    def reconcile(self) -> ReconciliationResult: ...


class ShortPutCandidateEvaluation(ChronosModel):
    """Presentation-safe candidate evidence; this slice never unlocks an action."""

    symbol: str
    status: EligibilityStatus
    reconciliation: ReconciliationResult | None
    evaluated_at: AwareDatetime
    underlying_quote: MarketQuote | None = None
    chain: OptionChainParameters | None = None
    resolution: StrikeResolution | None = None
    opening_actions_locked: Literal[True] = True
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _MarketObservation:
    status_start: ConnectionStatus
    account_first: AccountSummary
    positions_first: tuple[BrokerPosition, ...]
    orders_first: tuple[BrokerOrder, ...]
    executions_first: tuple[BrokerExecution, ...]
    underlying: UnderlyingContract
    underlying_quote: ManagedQuote
    chain_snapshot: OptionChainSnapshot
    chain: OptionChainParameters
    option_contracts: tuple[OptionContract, ...]
    option_quotes: tuple[ManagedQuote, ...]
    positions_second: tuple[BrokerPosition, ...]
    orders_second: tuple[BrokerOrder, ...]
    executions_second: tuple[BrokerExecution, ...]
    account_second: AccountSummary
    status_end: ConnectionStatus
    evaluated_at: datetime


class _CandidateEvidenceError(RuntimeError):
    """Internal control flow carrying only a presentation-safe reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShortPutCandidateService:
    """Rank bounded short-put quotes only for a proven empty, freshly reconciled account."""

    def __init__(
        self,
        connection: ConnectionRunner,
        market_data: MarketDataManager,
        reconciliation: ReconciliationReader,
        settings: Settings,
        expected_account_fingerprint: str,
        *,
        resolver: StrikeResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        max_reconciliation_age: timedelta | None = None,
        observation_timeout_seconds: float = 30.0,
    ) -> None:
        maximum_age = (
            max_reconciliation_age
            if max_reconciliation_age is not None
            else timedelta(seconds=settings.max_quote_age_seconds)
        )
        if maximum_age <= timedelta(0):
            raise ValueError("max_reconciliation_age must be positive")
        if not isfinite(observation_timeout_seconds) or observation_timeout_seconds <= 0:
            raise ValueError("observation_timeout_seconds must be finite and positive")
        maximum_specs = settings.max_expirations * settings.max_strikes_per_expiration
        if (
            settings.max_expirations > MAX_CANDIDATE_EXPIRATIONS
            or settings.max_strikes_per_expiration > MAX_CANDIDATE_STRIKES_PER_EXPIRATION
            or maximum_specs > MAX_CANDIDATE_REQUEST_CONTRACTS
        ):
            raise ValueError("Candidate request bounds exceed the hard safety limits")

        self._connection = connection
        self._broker = connection.broker
        self._market_data = market_data
        self._reconciliation = reconciliation
        self._settings = settings
        self._expected_account_fingerprint = normalize_account_fingerprint(
            expected_account_fingerprint
        )
        self._resolver = resolver or StrikeResolver(ResolverPolicy.from_settings(settings))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_reconciliation_age = maximum_age
        self._observation_timeout_seconds = observation_timeout_seconds
        self._maximum_specs = maximum_specs
        self._allowlisted_symbols = frozenset(settings.symbol_allowlist)

    def evaluate(self, symbol: str) -> ShortPutCandidateEvaluation:
        """Return transparent ranking evidence without writing or invoking an order method."""

        evaluated_at = self._now()
        normalized_symbol = symbol.strip().upper()
        if (
            not normalized_symbol
            or not normalized_symbol.isalnum()
            or normalized_symbol not in self._allowlisted_symbols
        ):
            return self._no_trade(
                normalized_symbol or "INVALID",
                evaluated_at=evaluated_at,
                reason="The requested symbol is not in the configured allowlist.",
            )

        try:
            reconciliation = self._reconciliation.reconcile()
        except Exception:
            _LOGGER.warning(
                "Candidate reconciliation failed; evaluation remains locked",
                extra={"event": "candidate_reconciliation_failed", "symbol": normalized_symbol},
            )
            return self._no_trade(
                normalized_symbol,
                evaluated_at=evaluated_at,
                reason="Fresh reconciliation evidence could not be obtained.",
            )

        evaluated_at = self._now()

        preflight_reasons = self._preflight_reasons(
            normalized_symbol,
            reconciliation,
            evaluated_at=evaluated_at,
        )
        if preflight_reasons:
            return self._no_trade(
                normalized_symbol,
                evaluated_at=evaluated_at,
                reconciliation=reconciliation,
                reasons=preflight_reasons,
            )
        assert reconciliation.snapshot is not None

        try:
            observation = self._connection.run(
                self._observe_market(normalized_symbol),
                timeout=self._observation_timeout_seconds,
            )
            evidence_reasons = self._market_evidence_reasons(
                observation,
                reconciliation.snapshot,
            )
            evidence_reasons.extend(
                self._freshness_reasons(
                    reconciliation.snapshot,
                    evaluated_at=observation.evaluated_at,
                )
            )
            if evidence_reasons:
                return self._no_trade(
                    normalized_symbol,
                    evaluated_at=observation.evaluated_at,
                    reconciliation=reconciliation,
                    underlying_quote=observation.underlying_quote.quote,
                    chain=observation.chain,
                    reasons=tuple(dict.fromkeys(evidence_reasons)),
                )

            capital = CapitalContext(
                account_fingerprint=self._expected_account_fingerprint,
                net_liquidation=observation.account_second.net_liquidation,
                uncommitted_cash=observation.account_second.total_cash,
                current_symbol_allocation=Decimal("0"),
                current_total_wheel_allocation=Decimal("0"),
                max_symbol_allocation_pct=self._settings.max_symbol_allocation_pct,
                max_total_wheel_allocation_pct=self._settings.max_total_wheel_allocation_pct,
                currency=observation.account_second.currency,
            )
            resolution = self._resolver.resolve(
                ResolverContext(
                    wheel_cycle_id=(
                        f"READONLY-FLAT-{normalized_symbol}-"
                        f"{reconciliation.snapshot.captured_at.isoformat()}"
                    ),
                    right=OptionRight.PUT,
                    contracts=1,
                    as_of=observation.evaluated_at,
                    underlying_quote=observation.underlying_quote.quote,
                    chain=observation.chain,
                    option_quotes=tuple(item.quote for item in observation.option_quotes),
                    capital=capital,
                    conflicting_position_or_order=False,
                    reconciliation_status=ReconciliationStatus.RECONCILED,
                )
            )
        except _CandidateEvidenceError as error:
            return self._no_trade(
                normalized_symbol,
                evaluated_at=self._now(),
                reconciliation=reconciliation,
                reason=error.reason,
            )
        except Exception:
            _LOGGER.warning(
                "Candidate market observation failed; evaluation remains locked",
                extra={"event": "candidate_market_observation_failed", "symbol": normalized_symbol},
            )
            return self._no_trade(
                normalized_symbol,
                evaluated_at=self._now(),
                reconciliation=reconciliation,
                reason="Candidate market evidence could not be captured safely.",
            )

        reasons = resolution.no_trade_reasons
        _LOGGER.info(
            "Read-only short-put candidates evaluated",
            extra={
                "event": "short_put_candidates_evaluated",
                "symbol": normalized_symbol,
                "candidate_count": len(resolution.candidates),
                "rejected_count": len(resolution.rejected),
                "outcome": resolution.status.value,
            },
        )
        return ShortPutCandidateEvaluation(
            symbol=normalized_symbol,
            status=resolution.status,
            reconciliation=reconciliation,
            evaluated_at=observation.evaluated_at,
            underlying_quote=observation.underlying_quote.quote,
            chain=observation.chain,
            resolution=resolution,
            reasons=reasons,
        )

    async def _observe_market(self, symbol: str) -> _MarketObservation:
        status_start = await self._broker.connection_status()
        account_first = await self._broker.account_summary()
        self._require_connected_scope(status_start, account_first)
        positions_first = await self._broker.positions()
        orders_first = await self._broker.open_orders()
        executions_first = await self._broker.executions()
        if positions_first or orders_first or executions_first:
            raise _CandidateEvidenceError(
                "Broker exposure changed after reconciliation; capital remains unproven."
            )

        underlying = await self._broker.qualify_underlying(symbol)
        if underlying.symbol != symbol:
            raise _CandidateEvidenceError(
                "The broker qualified an underlying outside the requested symbol scope."
            )
        underlying_quote = await self._market_data.underlying_quote(
            underlying,
            force_refresh=True,
        )
        chain_snapshot = await self._market_data.option_chain_parameters(
            underlying,
            force_refresh=True,
        )
        if not chain_snapshot.fresh:
            raise _CandidateEvidenceError("Option-chain metadata is not fresh.")
        chain = self._select_chain(underlying, chain_snapshot)
        spot = spot_price(underlying_quote.quote)
        if spot is None:
            raise _CandidateEvidenceError(
                "The underlying quote has no valid positive spot reference."
            )
        narrowing_time = self._now()
        specs = self._market_data.narrow_option_specs(
            chain,
            symbol=symbol,
            spot=spot,
            right=OptionRight.PUT,
            as_of=narrowing_time.astimezone(ZoneInfo(self._settings.market_timezone)).date(),
            min_dte=self._settings.min_dte,
            target_dte=self._settings.target_dte,
            max_dte=self._settings.max_dte,
            max_expirations=self._settings.max_expirations,
            strikes_per_expiration=self._settings.max_strikes_per_expiration,
            currency=underlying.currency,
        )
        if len(specs) > self._maximum_specs:
            raise _CandidateEvidenceError("The narrowed option request exceeded configured bounds.")
        qualified_option_contracts = await self._market_data.qualify_option_contracts(
            specs,
            force_refresh=True,
        )
        option_contracts = tuple(
            contract
            for contract in qualified_option_contracts
            if contract.has_verified_standard_deliverable
        )
        if qualified_option_contracts and not option_contracts:
            raise _CandidateEvidenceError(
                "No qualified option contract has a verified standard deliverable."
            )
        option_quotes = await self._market_data.option_quotes(
            option_contracts,
            force_refresh=True,
        )

        positions_second = await self._broker.positions()
        orders_second = await self._broker.open_orders()
        executions_second = await self._broker.executions()
        account_second = await self._broker.account_summary()
        status_end = await self._broker.connection_status()
        evaluated_at = self._now()
        return _MarketObservation(
            status_start=status_start,
            account_first=account_first,
            positions_first=positions_first,
            orders_first=orders_first,
            executions_first=executions_first,
            underlying=underlying,
            underlying_quote=underlying_quote,
            chain_snapshot=chain_snapshot,
            chain=chain,
            option_contracts=option_contracts,
            option_quotes=option_quotes,
            positions_second=positions_second,
            orders_second=orders_second,
            executions_second=executions_second,
            account_second=account_second,
            status_end=status_end,
            evaluated_at=evaluated_at,
        )

    def _select_chain(
        self,
        underlying: UnderlyingContract,
        snapshot: OptionChainSnapshot,
    ) -> OptionChainParameters:
        exact = tuple(
            chain
            for chain in snapshot.parameters
            if chain.underlying_con_id == underlying.con_id
            and chain.exchange == underlying.exchange
            and chain.trading_class == underlying.symbol
        )
        if len(exact) != 1:
            raise _CandidateEvidenceError("Option-chain routing identity was missing or ambiguous.")
        return exact[0]

    def _preflight_reasons(
        self,
        symbol: str,
        result: ReconciliationResult,
        *,
        evaluated_at: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        snapshot = result.snapshot
        if result.status is not ReconciliationStatus.RECONCILED:
            reasons.append("The portfolio does not have fresh reconciled status.")
        if snapshot is None:
            reasons.append("No stable reconciliation snapshot is available.")
        else:
            reasons.extend(self._freshness_reasons(snapshot, evaluated_at=evaluated_at))
            if snapshot.positions or snapshot.open_orders or snapshot.execution_count:
                reasons.append(
                    "Whole-account allocation is not proven empty; "
                    "candidate capital remains locked."
                )

        matching = tuple(item for item in result.symbols if item.symbol == symbol)
        if len(matching) != 1:
            reasons.append("The requested symbol has no unique reconciliation result.")
        else:
            target = matching[0]
            if (
                target.status is not ReconciliationStatus.RECONCILED
                or target.stage is not WheelStage.FLAT
                or target.manual_review_required
            ):
                reasons.append("The requested symbol is not freshly reconciled and flat.")
            quantities = (
                target.stock_shares,
                target.unencumbered_shares,
                target.short_put_contracts,
                target.short_call_contracts,
                target.pending_put_contracts,
                target.pending_call_contracts,
            )
            if any(quantity != 0 for quantity in quantities):
                reasons.append("The requested symbol still has reconciled exposure.")
        return tuple(dict.fromkeys(reasons))

    def _freshness_reasons(
        self,
        snapshot: ReconciliationSnapshotView,
        *,
        evaluated_at: datetime,
    ) -> list[str]:
        age = evaluated_at.astimezone(UTC) - snapshot.captured_at.astimezone(UTC)
        if age < timedelta(0):
            return ["The reconciliation timestamp is in the future."]
        if age > self._max_reconciliation_age:
            return ["The reconciliation snapshot is too old for candidate evaluation."]
        return []

    def _market_evidence_reasons(
        self,
        observation: _MarketObservation,
        snapshot: ReconciliationSnapshotView,
    ) -> list[str]:
        reasons: list[str] = []
        try:
            self._require_connected_scope(observation.status_start, observation.account_first)
            self._require_connected_scope(observation.status_end, observation.account_second)
        except _CandidateEvidenceError as error:
            reasons.append(error.reason)
        if (
            observation.status_start.environment is not observation.status_end.environment
            or observation.status_end.environment is not snapshot.environment
        ):
            reasons.append("Broker environment changed after reconciliation.")
        if observation.status_end.data_quality not in _RANKABLE_CONNECTION_QUALITIES:
            reasons.append("Broker market data quality did not become rankable.")
        if self._account_signature(observation.account_first) != self._account_signature(
            observation.account_second
        ):
            reasons.append("Account capital values changed during candidate evaluation.")
        if (
            not self._is_aware(observation.account_first.as_of)
            or not self._is_aware(observation.account_second.as_of)
            or observation.account_second.as_of < observation.account_first.as_of
        ):
            reasons.append("Account observation timestamps were invalid or moved backward.")
        if self._account_signature(observation.account_second) != (
            snapshot.account.net_liquidation,
            snapshot.account.total_cash,
            snapshot.account.buying_power,
            snapshot.account.currency,
        ):
            reasons.append("Account capital values no longer match reconciliation.")
        if (
            mask_account_id(observation.account_second.account_id)
            != snapshot.account.masked_account_id
        ):
            reasons.append("Broker account scope no longer matches reconciliation.")
        if (
            observation.positions_first
            or observation.orders_first
            or observation.executions_first
            or observation.positions_second
            or observation.orders_second
            or observation.executions_second
        ):
            reasons.append(
                "Broker exposure changed after reconciliation; capital remains unproven."
            )
        if observation.underlying_quote.quote.contract != observation.underlying:
            reasons.append("The underlying quote does not match the qualified contract.")
        if len(observation.option_contracts) != len(observation.option_quotes):
            reasons.append("Option quote evidence is incomplete.")
        elif any(
            quote.quote.contract != contract
            for contract, quote in zip(
                observation.option_contracts,
                observation.option_quotes,
                strict=True,
            )
        ):
            reasons.append("Option quote identity does not match its qualified contract.")
        return reasons

    def _require_connected_scope(
        self,
        status: ConnectionStatus,
        account: AccountSummary,
    ) -> None:
        if status.state is not ConnectionState.CONNECTED or not status.connected:
            raise _CandidateEvidenceError(
                "The broker connection is not healthy enough for candidate evaluation."
            )
        raw_account_id = account.account_id
        if (
            not raw_account_id
            or raw_account_id != raw_account_id.strip()
            or status.account_id != raw_account_id
        ):
            raise _CandidateEvidenceError("The broker account scope is missing or inconsistent.")
        try:
            observed_fingerprint = account_fingerprint(raw_account_id)
        except ValueError:
            raise _CandidateEvidenceError(
                "The broker account scope is missing or inconsistent."
            ) from None
        if observed_fingerprint != self._expected_account_fingerprint:
            raise _CandidateEvidenceError(
                "The broker account does not match the bound candidate scope."
            )

    @staticmethod
    def _account_signature(
        account: AccountSummary,
    ) -> tuple[Decimal, Decimal, Decimal, str]:
        return (
            account.net_liquidation,
            account.total_cash,
            account.buying_power,
            account.currency,
        )

    @staticmethod
    def _is_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    def _no_trade(
        self,
        symbol: str,
        *,
        evaluated_at: datetime,
        reason: str | None = None,
        reasons: tuple[str, ...] = (),
        reconciliation: ReconciliationResult | None = None,
        underlying_quote: MarketQuote | None = None,
        chain: OptionChainParameters | None = None,
    ) -> ShortPutCandidateEvaluation:
        values = tuple(dict.fromkeys((*reasons, *((reason,) if reason is not None else ()))))
        return ShortPutCandidateEvaluation(
            symbol=symbol,
            status=EligibilityStatus.NO_TRADE,
            reconciliation=reconciliation,
            evaluated_at=evaluated_at,
            underlying_quote=underlying_quote,
            chain=chain,
            reasons=values,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ShortPutCandidateService clock must return an aware datetime")
        return value.astimezone(UTC)

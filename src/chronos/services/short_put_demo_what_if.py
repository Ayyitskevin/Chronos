"""Deterministic, non-transmitting short-put what-if rehearsals.

This module deliberately returns only the rehearsal-specific
``WHAT_IF_PREVIEWED`` status. It exercises a deterministic preview boundary
against :class:`~chronos.broker.demo.DemoBroker` while the real IBKR order
channel remains hard-locked.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from math import isfinite
from typing import Annotated, Any, Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from chronos.broker.base import Broker
from chronos.broker.demo import DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    EligibilityStatus,
    OptionRight,
    OrderIntent,
    OrderSide,
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
    OptionContract,
    OrderPreview,
    OrderRequest,
    UnderlyingContract,
)
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.services.short_put_risk_preview import (
    MAX_COMMISSION_DECIMAL_PLACES,
    MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
    MAX_TOTAL_COMMISSION_ESTIMATE,
    RiskPreviewStatus,
    RiskScenarioLabel,
    ShortPutRiskPreview,
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewResult,
    ShortPutRiskScenarioPoint,
    commission_estimate_decimal_places,
    commission_estimate_has_bounded_representation,
)
from chronos.strategy.capital import CapitalContext, evaluate_short_put_capital
from chronos.strategy.scenarios import ShortPutScenario, ShortPutScenarioInput, short_put_scenario
from chronos.strategy.strike_resolver import RankedCandidate, ResolverPolicy, spot_price
from chronos.utils.identifiers import account_fingerprint, normalize_account_fingerprint
from chronos.utils.logging import mask_account_id

_T = TypeVar("_T")
_LOGGER = logging.getLogger("chronos.services.short_put_demo_what_if")
_CORRELATION_PATTERN = re.compile(r"^CHR-WIF-[A-F0-9]{32}$")
_DEMO_RANKABLE_QUALITY = DataQuality.DEMO

MAX_LIMIT_PRICE = Decimal("1000000")
MAX_LIMIT_DECIMAL_PLACES = 4
MAX_LIMIT_DECIMAL_DIGITS = 16
MAX_LIMIT_INPUT_CHARACTERS = 32
MAX_WHAT_IF_OBSERVATION_TIMEOUT_SECONDS = 30.0


def limit_price_has_bounded_representation(value: Decimal) -> bool:
    """Reject pathological Decimal coefficients and exponents at ingress."""

    if not value.is_finite():
        return False
    parts = value.as_tuple()
    exponent = parts.exponent
    return (
        isinstance(exponent, int)
        and len(parts.digits) <= MAX_LIMIT_DECIMAL_DIGITS
        and abs(exponent) <= MAX_LIMIT_DECIMAL_DIGITS
    )


def limit_price_decimal_places(value: Decimal) -> int:
    """Count normalized fractional places without mutable Decimal context."""

    if not value.is_finite():
        raise ValueError("limit price must be finite")
    if value.is_zero():
        return 0
    parts = value.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError("limit price must have a finite decimal exponent")
    trailing_zeros = 0
    for digit in reversed(parts.digits):
        if digit != 0:
            break
        trailing_zeros += 1
    return max(-(exponent + trailing_zeros), 0)


class ShortPutRiskPreviewer(Protocol):
    """Fresh M6 boundary used by the DEMO what-if rehearsal."""

    def preview(self, request: ShortPutRiskPreviewRequest) -> ShortPutRiskPreviewResult: ...


class DemoWhatIfConnectionRunner(Protocol):
    """Narrow serialized broker runner required by the rehearsal."""

    broker: Broker

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T: ...


class DemoWhatIfStatus(StrEnum):
    WHAT_IF_PREVIEWED = "WHAT_IF_PREVIEWED"
    WITHHELD = "WITHHELD"


class ShortPutDemoWhatIfRequest(ChronosModel):
    """Untrusted UI terms; account, quantity, side, and order reference are internal."""

    symbol: str
    selected_contract_id: Annotated[int, Field(gt=0, strict=True)]
    total_commission_estimate: Decimal = Field(ge=0)
    limit_price: Decimal = Field(gt=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized or not normalized.isalnum():
            raise ValueError("symbol must be a nonblank alphanumeric code")
        return normalized

    @field_validator("total_commission_estimate")
    @classmethod
    def require_bounded_commission(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("total_commission_estimate must be finite")
        if value > MAX_TOTAL_COMMISSION_ESTIMATE:
            raise ValueError("total_commission_estimate exceeds the supported one-contract bound")
        if not commission_estimate_has_bounded_representation(value):
            raise ValueError("total_commission_estimate has an unsupported representation")
        if commission_estimate_decimal_places(value) > MAX_COMMISSION_DECIMAL_PLACES:
            raise ValueError("total_commission_estimate has too many fractional decimal places")
        return value

    @field_validator("limit_price")
    @classmethod
    def require_bounded_limit(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("limit_price must be finite")
        if value > MAX_LIMIT_PRICE:
            raise ValueError("limit_price exceeds the supported one-contract bound")
        if not limit_price_has_bounded_representation(value):
            raise ValueError("limit_price has an unsupported decimal representation")
        if limit_price_decimal_places(value) > MAX_LIMIT_DECIMAL_PLACES:
            raise ValueError("limit_price has too many fractional decimal places")
        return value


class ShortPutDemoWhatIfPreview(ChronosModel):
    """Presentation-safe what-if evidence with no raw account or broker request."""

    correlation_id: str
    symbol: str
    selected_contract: OptionContract
    candidate_rank: Annotated[int, Field(gt=0, strict=True)]
    quantity: Literal[1] = 1
    intent: Literal[OrderIntent.OPEN_SHORT_PUT] = OrderIntent.OPEN_SHORT_PUT
    side: Literal[OrderSide.SELL] = OrderSide.SELL
    limit_price: Decimal = Field(gt=0)
    fresh_bid: Decimal = Field(gt=0)
    fresh_ask: Decimal = Field(gt=0)
    currency: str
    evidence_evaluated_at: AwareDatetime
    broker_previewed_at: AwareDatetime
    operator_commission_estimate: Decimal = Field(ge=0)
    broker_estimated_commission: Decimal = Field(ge=0)
    commission_variance: Decimal
    initial_margin_change: Decimal
    maintenance_margin_change: Decimal
    equity_with_loan_change: Decimal
    gross_premium: Decimal = Field(ge=0)
    net_premium: Decimal
    assigned_shares: Decimal = Field(gt=0)
    gross_assignment_obligation: Decimal = Field(gt=0)
    net_cash_deployed_if_assigned: Decimal
    effective_entry_price: Decimal = Field(ge=0)
    cash_secured_notional_pct: Decimal = Field(ge=0)
    scenario_points: tuple[ShortPutRiskScenarioPoint, ...] = Field(min_length=1)
    broker_warning_count: int = Field(ge=0)
    broker_warning_notice: str | None = None
    transmit: Literal[False] = False
    outside_rth: Literal[False] = False
    demo_rehearsal_only: Literal[True] = True
    broker_authorized: Literal[False] = False
    user_confirmation_available: Literal[False] = False
    submission_authorized: Literal[False] = False
    opening_actions_locked: Literal[True] = True

    @field_validator("correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: str) -> str:
        if _CORRELATION_PATTERN.fullmatch(value) is None:
            raise ValueError("correlation_id is not a canonical DEMO what-if reference")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("currency must not be blank")
        return normalized

    @field_validator(
        "limit_price",
        "fresh_bid",
        "fresh_ask",
        "operator_commission_estimate",
        "broker_estimated_commission",
        "commission_variance",
        "initial_margin_change",
        "maintenance_margin_change",
        "equity_with_loan_change",
        "gross_premium",
        "net_premium",
        "assigned_shares",
        "gross_assignment_obligation",
        "net_cash_deployed_if_assigned",
        "effective_entry_price",
        "cash_secured_notional_pct",
    )
    @classmethod
    def require_finite_amounts(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("what-if numeric evidence must be finite")
        return value

    @model_validator(mode="after")
    def validate_coherence(self) -> ShortPutDemoWhatIfPreview:
        contract = self.selected_contract
        if (
            contract.symbol != self.symbol
            or contract.currency != self.currency
            or contract.right is not OptionRight.PUT
            or not contract.has_verified_standard_deliverable
        ):
            raise ValueError("what-if contract identity is inconsistent")
        if self.fresh_bid > self.limit_price or self.limit_price > self.fresh_ask:
            raise ValueError("what-if limit must stay inside the fresh quoted spread")
        if (self.broker_warning_notice is None) != (self.broker_warning_count == 0):
            raise ValueError("what-if warning notice must reflect the broker warning count")
        return self


class ShortPutDemoWhatIfResult(ChronosModel):
    """One explicit rehearsal attempt; success can never authorize submission."""

    request: ShortPutDemoWhatIfRequest
    status: DemoWhatIfStatus
    evaluated_at: AwareDatetime
    risk_refresh: ShortPutRiskPreviewResult | None = None
    preview: ShortPutDemoWhatIfPreview | None = None
    reasons: tuple[str, ...] = ()
    opening_actions_locked: Literal[True] = True

    @model_validator(mode="after")
    def validate_outcome(self) -> ShortPutDemoWhatIfResult:
        if self.status is DemoWhatIfStatus.WHAT_IF_PREVIEWED:
            if self.preview is None or self.risk_refresh is None or self.reasons:
                raise ValueError("A completed rehearsal requires fresh evidence and no reasons")
        elif self.preview is not None:
            raise ValueError("A withheld rehearsal cannot contain broker what-if evidence")
        return self


@dataclass(frozen=True, slots=True)
class _FreshSelection:
    risk: ShortPutRiskPreviewResult
    risk_preview: ShortPutRiskPreview
    candidate: RankedCandidate
    candidate_rank: int
    snapshot: ReconciliationSnapshotView


@dataclass(frozen=True, slots=True)
class _BrokerWhatIfWindow:
    status_first: ConnectionStatus
    account_first: AccountSummary
    positions_first: tuple[BrokerPosition, ...]
    orders_first: tuple[BrokerOrder, ...]
    executions_first: tuple[BrokerExecution, ...]
    server_time_first: datetime
    sent_at: datetime
    broker_preview: OrderPreview
    received_at: datetime
    server_time_second: datetime
    positions_second: tuple[BrokerPosition, ...]
    orders_second: tuple[BrokerOrder, ...]
    executions_second: tuple[BrokerExecution, ...]
    account_second: AccountSummary
    status_second: ConnectionStatus


class _DemoWhatIfEvidenceError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShortPutDemoWhatIfService:
    """Refresh risk evidence and run exactly one non-transmitting DEMO preview."""

    def __init__(
        self,
        risk_preview: ShortPutRiskPreviewer,
        connection: DemoWhatIfConnectionRunner,
        settings: Settings,
        expected_account_fingerprint: str,
        *,
        clock: Callable[[], datetime] | None = None,
        correlation_factory: Callable[[], str] | None = None,
        observation_timeout_seconds: float = 30.0,
    ) -> None:
        if (
            isinstance(observation_timeout_seconds, bool)
            or not isinstance(observation_timeout_seconds, (int, float))
            or not isfinite(float(observation_timeout_seconds))
            or observation_timeout_seconds <= 0
            or observation_timeout_seconds > MAX_WHAT_IF_OBSERVATION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "observation_timeout_seconds must be finite, positive, and no greater than "
                f"{MAX_WHAT_IF_OBSERVATION_TIMEOUT_SECONDS}"
            )
        self._risk_preview = risk_preview
        self._connection = connection
        self._broker = connection.broker
        self._settings = settings
        self._expected_account_fingerprint = normalize_account_fingerprint(
            expected_account_fingerprint
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._correlation_factory = correlation_factory or (
            lambda: f"CHR-WIF-{uuid4().hex.upper()}"
        )
        self._observation_timeout_seconds = observation_timeout_seconds

    def is_bound_to_demo_boundary(self, connection: object, settings: object) -> bool:
        """Attest that callers share this service's validated DEMO boundary.

        The check is intentionally observational: it performs no broker calls and
        fails closed for malformed collaborators or settings.
        """

        if connection is not self._connection:
            return False
        try:
            current_broker = connection.broker
            if (
                current_broker is not self._broker
                or not isinstance(self._broker, DemoBroker)
                or not isinstance(current_broker, DemoBroker)
                or not isinstance(settings, Settings)
            ):
                return False
            captured_settings = Settings.model_validate(self._settings.model_dump(mode="python"))
            supplied_settings = Settings.model_validate(settings.model_dump(mode="python"))
        except Exception:
            return False
        return (
            captured_settings.broker_mode is BrokerMode.DEMO
            and supplied_settings.broker_mode is BrokerMode.DEMO
            and supplied_settings.model_dump(mode="python")
            == captured_settings.model_dump(mode="python")
        )

    def preview(self, request: ShortPutDemoWhatIfRequest) -> ShortPutDemoWhatIfResult:
        """Run a fresh DEMO rehearsal; never accept UI evidence or an account identifier."""

        request = ShortPutDemoWhatIfRequest.model_validate(request.model_dump(mode="python"))
        attempted_at = self._now()
        try:
            settings = Settings.model_validate(self._settings.model_dump(mode="python"))
            if settings.broker_mode is not BrokerMode.DEMO or not isinstance(
                self._broker, DemoBroker
            ):
                raise _DemoWhatIfEvidenceError(
                    "Deterministic order what-if rehearsal is available only in DEMO mode."
                )
        except _DemoWhatIfEvidenceError as error:
            return self._withheld(request, evaluated_at=attempted_at, reason=error.reason)
        except Exception:
            return self._withheld(
                request,
                evaluated_at=attempted_at,
                reason="The configured DEMO rehearsal boundary is invalid.",
            )

        risk_request = ShortPutRiskPreviewRequest(
            symbol=request.symbol,
            selected_contract_id=request.selected_contract_id,
            total_commission_estimate=request.total_commission_estimate,
        )
        try:
            risk_refresh = self._risk_preview.preview(risk_request)
        except Exception as error:
            _LOGGER.warning(
                "DEMO what-if risk refresh failed; rehearsal remains locked",
                extra={
                    "event": "short_put_demo_what_if_risk_refresh_failed",
                    "symbol": request.symbol,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=self._now(),
                reason="Fresh risk evidence could not be obtained safely.",
            )

        attempted_at = self._now()
        try:
            selection = self._require_fresh_selection(
                request,
                risk_refresh,
                attempted_at=attempted_at,
                settings=settings,
            )
            correlation_id = self._correlation_factory()
            if (
                not isinstance(correlation_id, str)
                or _CORRELATION_PATTERN.fullmatch(correlation_id) is None
            ):
                raise _DemoWhatIfEvidenceError(
                    "A safe internal what-if reference could not be generated."
                )
            window = self._connection.run(
                self._observe_broker(
                    request,
                    selection,
                    correlation_id=correlation_id,
                    settings=settings,
                ),
                timeout=self._observation_timeout_seconds,
            )
            completed_at = self._now()
            safe_preview = self._build_safe_preview(
                request,
                selection,
                window,
                correlation_id=correlation_id,
                completed_at=completed_at,
                settings=settings,
            )
        except _DemoWhatIfEvidenceError as error:
            _LOGGER.info(
                "Short-put DEMO what-if withheld by fresh evidence",
                extra={
                    "event": "short_put_demo_what_if_withheld",
                    "symbol": request.symbol,
                    "contract_id": request.selected_contract_id,
                    "outcome": DemoWhatIfStatus.WITHHELD.value,
                },
            )
            return self._withheld(
                request,
                evaluated_at=self._now(),
                risk_refresh=risk_refresh,
                reason=error.reason,
            )
        except Exception as error:
            _LOGGER.warning(
                "Short-put DEMO what-if failed; rehearsal remains locked",
                extra={
                    "event": "short_put_demo_what_if_failed",
                    "symbol": request.symbol,
                    "contract_id": request.selected_contract_id,
                    "error_type": type(error).__name__,
                },
            )
            return self._withheld(
                request,
                evaluated_at=self._now(),
                risk_refresh=risk_refresh,
                reason="Broker what-if evidence could not be obtained safely.",
            )

        _LOGGER.info(
            "Short-put DEMO what-if rehearsal completed",
            extra={
                "event": "short_put_demo_what_if_completed",
                "symbol": request.symbol,
                "contract_id": request.selected_contract_id,
                "outcome": DemoWhatIfStatus.WHAT_IF_PREVIEWED.value,
            },
        )
        return ShortPutDemoWhatIfResult(
            request=request,
            status=DemoWhatIfStatus.WHAT_IF_PREVIEWED,
            evaluated_at=completed_at,
            risk_refresh=risk_refresh,
            preview=safe_preview,
        )

    async def _observe_broker(
        self,
        request: ShortPutDemoWhatIfRequest,
        selection: _FreshSelection,
        *,
        correlation_id: str,
        settings: Settings,
    ) -> _BrokerWhatIfWindow:
        status_first = await self._broker.connection_status()
        account_first = await self._broker.account_summary()
        positions_first = await self._broker.positions()
        orders_first = await self._broker.open_orders()
        executions_first = await self._broker.executions()
        server_time_first = await self._broker.server_time()
        sent_at = self._now()
        self._require_broker_preflight(
            selection,
            status=status_first,
            account=account_first,
            positions=positions_first,
            orders=orders_first,
            executions=executions_first,
            server_time=server_time_first,
            observed_at=sent_at,
            settings=settings,
        )
        order_request = OrderRequest(
            correlation_id=correlation_id,
            account_id=account_first.account_id,
            contract=selection.risk_preview.selected_contract,
            intent=OrderIntent.OPEN_SHORT_PUT,
            side=OrderSide.SELL,
            quantity=Decimal(1),
            limit_price=request.limit_price,
            order_ref=correlation_id,
            transmit=False,
            outside_rth=False,
        )
        broker_preview = await self._broker.preview_order(order_request)
        received_at = self._now()
        server_time_second = await self._broker.server_time()
        positions_second = await self._broker.positions()
        orders_second = await self._broker.open_orders()
        executions_second = await self._broker.executions()
        account_second = await self._broker.account_summary()
        status_second = await self._broker.connection_status()
        return _BrokerWhatIfWindow(
            status_first=status_first,
            account_first=account_first,
            positions_first=positions_first,
            orders_first=orders_first,
            executions_first=executions_first,
            server_time_first=server_time_first,
            sent_at=sent_at,
            broker_preview=broker_preview,
            received_at=received_at,
            server_time_second=server_time_second,
            positions_second=positions_second,
            orders_second=orders_second,
            executions_second=executions_second,
            account_second=account_second,
            status_second=status_second,
        )

    def _require_broker_preflight(
        self,
        selection: _FreshSelection,
        *,
        status: ConnectionStatus,
        account: AccountSummary,
        positions: tuple[BrokerPosition, ...],
        orders: tuple[BrokerOrder, ...],
        executions: tuple[BrokerExecution, ...],
        server_time: datetime,
        observed_at: datetime,
        settings: Settings,
    ) -> None:
        snapshot_account = selection.snapshot.account
        if (
            status.state is not ConnectionState.CONNECTED
            or not status.connected
            or status.environment is not DisplayEnvironment.DEMO
            or status.data_quality is not DataQuality.DEMO
            or status.account_id != account.account_id
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker is not in a connected, account-bound preview state."
            )
        try:
            observed_fingerprint = account_fingerprint(account.account_id)
        except ValueError:
            observed_fingerprint = ""
        if observed_fingerprint != self._expected_account_fingerprint:
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker account does not match the bound local scope."
            )
        if (
            self._account_signature(account) != self._account_signature(snapshot_account)
            or mask_account_id(account.account_id) != snapshot_account.masked_account_id
            or positions
            or orders
            or executions
        ):
            raise _DemoWhatIfEvidenceError(
                "Broker exposure or capital changed before the DEMO what-if request."
            )
        max_age = min(
            Decimal(settings.max_quote_age_seconds),
            MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
        )
        if not all(
            self._timestamp_is_fresh(value, now=observed_at, max_age_seconds=max_age)
            for value in (server_time, account.as_of)
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker preflight evidence is stale or future-dated."
            )

    def _require_fresh_selection(
        self,
        request: ShortPutDemoWhatIfRequest,
        risk_refresh: ShortPutRiskPreviewResult,
        *,
        attempted_at: datetime,
        settings: Settings,
    ) -> _FreshSelection:
        risk_refresh = ShortPutRiskPreviewResult.model_validate(
            risk_refresh.model_dump(mode="python")
        )
        if (
            risk_refresh.request
            != ShortPutRiskPreviewRequest(
                symbol=request.symbol,
                selected_contract_id=request.selected_contract_id,
                total_commission_estimate=request.total_commission_estimate,
            )
            or risk_refresh.status is not RiskPreviewStatus.READY
            or risk_refresh.preview is None
            or risk_refresh.candidate_refresh is None
            or risk_refresh.reasons
        ):
            raise _DemoWhatIfEvidenceError(
                "Fresh risk evidence did not produce the selected ready contract."
            )
        preview = risk_refresh.preview
        refresh = risk_refresh.candidate_refresh
        reconciliation = refresh.reconciliation
        if (
            preview.symbol != request.symbol
            or preview.selected_contract.con_id != request.selected_contract_id
            or preview.total_commission_estimate != request.total_commission_estimate
            or refresh.symbol != request.symbol
            or refresh.status is not EligibilityStatus.ELIGIBLE
            or refresh.resolution is None
            or refresh.underlying_quote is None
            or refresh.resolution.status is not EligibilityStatus.ELIGIBLE
            or refresh.resolution.policy != ResolverPolicy.from_settings(settings)
            or reconciliation is None
            or reconciliation.snapshot is None
            or reconciliation.status is not ReconciliationStatus.RECONCILED
            or reconciliation.reasons
        ):
            raise _DemoWhatIfEvidenceError("Fresh risk evidence is incomplete or inconsistent.")
        snapshot = reconciliation.snapshot
        underlying_quote = refresh.underlying_quote
        matches = tuple(
            (rank, candidate)
            for rank, candidate in enumerate(refresh.resolution.candidates, start=1)
            if candidate.contract.con_id == request.selected_contract_id
        )
        if len(matches) != 1:
            raise _DemoWhatIfEvidenceError(
                "The selected contract is not uniquely eligible in fresh evidence."
            )
        candidate_rank, candidate = matches[0]
        self._require_identity_and_capital(
            request,
            preview,
            candidate,
            snapshot,
            reconciliation.symbols,
            refresh=refresh,
            settings=settings,
        )
        self._require_limit(request.limit_price, candidate)
        max_age = min(
            Decimal(settings.max_quote_age_seconds),
            MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
        )
        timestamps = (
            risk_refresh.evaluated_at,
            preview.evidence_evaluated_at,
            preview.underlying_observed_at,
            refresh.evaluated_at,
            underlying_quote.timestamp,
            snapshot.captured_at,
            snapshot.account.as_of,
        )
        if (
            underlying_quote.timestamp > refresh.evaluated_at
            or snapshot.captured_at > refresh.evaluated_at
            or snapshot.account.as_of > refresh.evaluated_at
            or risk_refresh.evaluated_at < refresh.evaluated_at
        ):
            raise _DemoWhatIfEvidenceError(
                "Fresh risk evidence has an impossible observation chronology."
            )
        if any(
            not self._timestamp_is_fresh(value, now=attempted_at, max_age_seconds=max_age)
            for value in timestamps
        ):
            raise _DemoWhatIfEvidenceError(
                "Fresh risk evidence expired before the DEMO what-if request."
            )
        elapsed = self._timestamp_age_seconds(refresh.evaluated_at, now=attempted_at)
        if (
            elapsed is None
            or not candidate.data_age_seconds.is_finite()
            or candidate.data_age_seconds < 0
            or candidate.data_age_seconds + elapsed > max_age
        ):
            raise _DemoWhatIfEvidenceError(
                "The selected option quote expired before the DEMO what-if request."
            )
        return _FreshSelection(
            risk=risk_refresh,
            risk_preview=preview,
            candidate=candidate,
            candidate_rank=candidate_rank,
            snapshot=snapshot,
        )

    def _require_identity_and_capital(
        self,
        request: ShortPutDemoWhatIfRequest,
        preview: ShortPutRiskPreview,
        candidate: RankedCandidate,
        snapshot: ReconciliationSnapshotView,
        symbols: tuple[SymbolReconciliation, ...],
        *,
        refresh: ShortPutCandidateEvaluation,
        settings: Settings,
    ) -> None:
        contract = candidate.contract
        chain = refresh.chain
        underlying_quote = refresh.underlying_quote
        resolution = refresh.resolution
        matching_symbols = tuple(item for item in symbols if item.symbol == request.symbol)
        if (
            snapshot.environment is not DisplayEnvironment.DEMO
            or snapshot.data_quality is not _DEMO_RANKABLE_QUALITY
            or snapshot.positions
            or snapshot.open_orders
            or snapshot.execution_count != 0
            or len(matching_symbols) != 1
        ):
            raise _DemoWhatIfEvidenceError(
                "Whole-account DEMO reconciliation no longer proves an empty scope."
            )
        target = matching_symbols[0]
        quantities = (
            target.stock_shares,
            target.unencumbered_shares,
            target.short_put_contracts,
            target.short_call_contracts,
            target.pending_put_contracts,
            target.pending_call_contracts,
        )
        if (
            target.status is not ReconciliationStatus.RECONCILED
            or target.stage is not WheelStage.FLAT
            or target.manual_review_required
            or any(quantity != 0 for quantity in quantities)
        ):
            raise _DemoWhatIfEvidenceError(
                "The selected symbol is no longer uniquely reconciled and flat."
            )
        if (
            contract != preview.selected_contract
            or contract.symbol != request.symbol
            or contract.con_id != request.selected_contract_id
            or contract.right is not OptionRight.PUT
            or not contract.has_verified_standard_deliverable
            or candidate.symbol != request.symbol
            or candidate.right is not OptionRight.PUT
            or candidate.strike != contract.strike
            or candidate.expiration != contract.expiration
            or candidate.eligibility_status is not EligibilityStatus.ELIGIBLE
            or candidate.rejection_reasons
            or candidate.data_quality is not DataQuality.DEMO
            or preview.option_data_quality is not DataQuality.DEMO
            or chain is None
            or underlying_quote is None
            or resolution is None
            or underlying_quote.data_quality is not DataQuality.DEMO
            or not isinstance(underlying_quote.contract, UnderlyingContract)
            or underlying_quote.contract.symbol != request.symbol
            or contract.underlying_con_id != underlying_quote.contract.con_id
            or chain.underlying_con_id != contract.underlying_con_id
            or chain.exchange != underlying_quote.contract.exchange
            or chain.trading_class != underlying_quote.contract.symbol
            or chain.exchange != contract.exchange
            or chain.trading_class != contract.trading_class
            or chain.multiplier != contract.multiplier
            or candidate.expiration not in chain.expirations
            or candidate.strike not in chain.strikes
            or contract.currency != snapshot.account.currency
            or contract.currency != underlying_quote.contract.currency
            or preview.candidate_rank
            != next(
                (
                    rank
                    for rank, item in enumerate(resolution.candidates, start=1)
                    if item.contract.con_id == request.selected_contract_id
                ),
                0,
            )
            or preview.evidence_evaluated_at != refresh.evaluated_at
            or preview.underlying_observed_at != underlying_quote.timestamp
            or preview.option_quote_age_seconds != candidate.data_age_seconds
            or preview.hypothetical_credit_per_share != candidate.bid
            or preview.observed_spot != spot_price(underlying_quote)
            or any(
                rejected.contract.con_id == request.selected_contract_id
                for rejected in resolution.rejected
            )
        ):
            raise _DemoWhatIfEvidenceError(
                "The selected option identity no longer matches fresh DEMO evidence."
            )
        deliverable = contract.deliverable_shares
        capital = candidate.capital_check
        if deliverable is None:
            raise _DemoWhatIfEvidenceError(
                "The selected option has no verified standard deliverable."
            )
        obligation = contract.strike * deliverable
        expected_capital = evaluate_short_put_capital(
            context=CapitalContext(
                account_fingerprint=self._expected_account_fingerprint,
                net_liquidation=snapshot.account.net_liquidation,
                uncommitted_cash=snapshot.account.total_cash,
                current_symbol_allocation=Decimal("0"),
                current_total_wheel_allocation=Decimal("0"),
                max_symbol_allocation_pct=settings.max_symbol_allocation_pct,
                max_total_wheel_allocation_pct=settings.max_total_wheel_allocation_pct,
                currency=snapshot.account.currency,
            ),
            strike=contract.strike,
            multiplier=contract.multiplier,
            verified_deliverable_shares=deliverable,
            contracts=1,
        )
        if (
            not all(
                value.is_finite()
                for value in (
                    candidate.bid,
                    candidate.ask,
                    obligation,
                    snapshot.account.net_liquidation,
                    snapshot.account.total_cash,
                    capital.proposal_amount,
                    capital.resulting_symbol_allocation,
                    capital.resulting_total_wheel_allocation,
                    capital.shares_required,
                )
            )
            or candidate.bid <= 0
            or candidate.ask < candidate.bid
            or candidate.midpoint != (candidate.bid + candidate.ask) / Decimal("2")
            or snapshot.account.net_liquidation <= 0
            or snapshot.account.total_cash < obligation
            or not capital.passed
            or capital.reasons
            or capital != expected_capital
            or capital.proposal_amount != obligation
            or capital.resulting_symbol_allocation != obligation
            or capital.resulting_total_wheel_allocation != obligation
            or capital.shares_required != deliverable
        ):
            raise _DemoWhatIfEvidenceError(
                "Fresh capital or quote evidence is internally inconsistent."
            )

    @staticmethod
    def _require_limit(limit_price: Decimal, candidate: RankedCandidate) -> None:
        tick = candidate.contract.min_tick
        if (
            not tick.is_finite()
            or tick <= 0
            or limit_price < candidate.bid
            or limit_price > candidate.ask
        ):
            raise _DemoWhatIfEvidenceError(
                "The explicit limit must stay inside the fresh quoted spread."
            )
        precision = max(
            50,
            len(limit_price.as_tuple().digits) + len(tick.as_tuple().digits) + 16,
        )
        try:
            with localcontext(Context(prec=precision)):
                aligned = limit_price % tick == 0
        except InvalidOperation:
            aligned = False
        if not aligned:
            raise _DemoWhatIfEvidenceError(
                "The explicit limit is not aligned to the contract minimum tick."
            )

    def _build_safe_preview(
        self,
        request: ShortPutDemoWhatIfRequest,
        selection: _FreshSelection,
        window: _BrokerWhatIfWindow,
        *,
        correlation_id: str,
        completed_at: datetime,
        settings: Settings,
    ) -> ShortPutDemoWhatIfPreview:
        self._require_broker_window(
            request,
            selection,
            window,
            correlation_id=correlation_id,
            completed_at=completed_at,
            settings=settings,
        )
        broker_preview = OrderPreview.model_validate(
            window.broker_preview.model_dump(mode="python")
        )
        commission = broker_preview.estimated_commission
        if commission is None:
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker did not provide a complete commission estimate."
            )
        initial_margin_change = broker_preview.initial_margin_change
        maintenance_margin_change = broker_preview.maintenance_margin_change
        equity_with_loan_change = broker_preview.equity_with_loan_change
        if (
            initial_margin_change is None
            or maintenance_margin_change is None
            or equity_with_loan_change is None
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker did not provide complete margin evidence."
            )
        scenario_summary, points = self._exact_limit_scenarios(
            request,
            selection,
            broker_commission=commission,
        )
        warning_count = len(broker_preview.warnings)
        warning_notice = (
            None
            if warning_count == 0
            else (
                f"The deterministic broker returned {warning_count} rehearsal warning(s); "
                "no broker text or authority is carried forward."
            )
        )
        return ShortPutDemoWhatIfPreview(
            correlation_id=correlation_id,
            symbol=request.symbol,
            selected_contract=selection.candidate.contract,
            candidate_rank=selection.candidate_rank,
            limit_price=request.limit_price,
            fresh_bid=selection.candidate.bid,
            fresh_ask=selection.candidate.ask,
            currency=selection.candidate.contract.currency,
            evidence_evaluated_at=selection.risk.evaluated_at,
            broker_previewed_at=broker_preview.previewed_at,
            operator_commission_estimate=request.total_commission_estimate,
            broker_estimated_commission=commission,
            commission_variance=commission - request.total_commission_estimate,
            initial_margin_change=initial_margin_change,
            maintenance_margin_change=maintenance_margin_change,
            equity_with_loan_change=equity_with_loan_change,
            gross_premium=scenario_summary.gross_premium,
            net_premium=scenario_summary.net_premium,
            assigned_shares=scenario_summary.assigned_shares,
            gross_assignment_obligation=scenario_summary.gross_assignment_obligation,
            net_cash_deployed_if_assigned=scenario_summary.net_cash_deployed_if_assigned,
            effective_entry_price=scenario_summary.effective_entry_price,
            cash_secured_notional_pct=scenario_summary.cash_secured_notional_pct,
            scenario_points=points,
            broker_warning_count=warning_count,
            broker_warning_notice=warning_notice,
        )

    def _require_broker_window(
        self,
        request: ShortPutDemoWhatIfRequest,
        selection: _FreshSelection,
        window: _BrokerWhatIfWindow,
        *,
        correlation_id: str,
        completed_at: datetime,
        settings: Settings,
    ) -> None:
        first_status = window.status_first
        second_status = window.status_second
        first_account = window.account_first
        second_account = window.account_second
        snapshot_account = selection.snapshot.account
        if (
            first_status.state is not ConnectionState.CONNECTED
            or second_status.state is not ConnectionState.CONNECTED
            or not first_status.connected
            or not second_status.connected
            or first_status.environment is not DisplayEnvironment.DEMO
            or second_status.environment is not DisplayEnvironment.DEMO
            or first_status.data_quality is not DataQuality.DEMO
            or second_status.data_quality is not DataQuality.DEMO
            or first_status.account_id != first_account.account_id
            or second_status.account_id != second_account.account_id
            or first_account.account_id != second_account.account_id
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker connection or account scope changed during what-if."
            )
        try:
            observed_fingerprint = account_fingerprint(first_account.account_id)
        except ValueError:
            observed_fingerprint = ""
        if observed_fingerprint != self._expected_account_fingerprint:
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker account does not match the bound local scope."
            )

        if (
            self._account_signature(first_account) != self._account_signature(second_account)
            or self._account_signature(second_account) != self._account_signature(snapshot_account)
            or mask_account_id(second_account.account_id) != snapshot_account.masked_account_id
            or window.positions_first
            or window.orders_first
            or window.executions_first
            or window.positions_second
            or window.orders_second
            or window.executions_second
        ):
            raise _DemoWhatIfEvidenceError(
                "Broker exposure or capital changed during the DEMO what-if window."
            )
        if (
            window.server_time_second < window.server_time_first
            or window.received_at < window.sent_at
            or completed_at < window.received_at
            or second_account.as_of < first_account.as_of
            or window.broker_preview.previewed_at < window.sent_at
            or window.broker_preview.previewed_at > window.received_at
        ):
            raise _DemoWhatIfEvidenceError("The DEMO what-if observation clock moved backward.")
        max_age = min(
            Decimal(settings.max_quote_age_seconds),
            MAX_RISK_PREVIEW_EVIDENCE_AGE_SECONDS,
        )
        freshness_timestamps = (
            window.server_time_first,
            window.server_time_second,
            first_account.as_of,
            second_account.as_of,
            window.sent_at,
            window.received_at,
            window.broker_preview.previewed_at,
        )
        if any(
            not self._timestamp_is_fresh(value, now=completed_at, max_age_seconds=max_age)
            for value in freshness_timestamps
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker what-if evidence is stale or future-dated."
            )
        expected_request = OrderRequest(
            correlation_id=correlation_id,
            account_id=first_account.account_id,
            contract=selection.candidate.contract,
            intent=OrderIntent.OPEN_SHORT_PUT,
            side=OrderSide.SELL,
            quantity=Decimal(1),
            limit_price=request.limit_price,
            order_ref=correlation_id,
            transmit=False,
            outside_rth=False,
        )
        broker_preview = OrderPreview.model_validate(
            window.broker_preview.model_dump(mode="python")
        )
        decimals = (
            broker_preview.estimated_commission,
            broker_preview.initial_margin_change,
            broker_preview.maintenance_margin_change,
            broker_preview.equity_with_loan_change,
        )
        if (
            broker_preview.request != expected_request
            or not broker_preview.accepted
            or any(value is None or not value.is_finite() for value in decimals)
            or broker_preview.estimated_commission is None
            or broker_preview.estimated_commission < 0
        ):
            raise _DemoWhatIfEvidenceError(
                "The DEMO broker what-if response was rejected, incomplete, or inconsistent."
            )

    @staticmethod
    def _account_signature(
        value: AccountSummary | ReconciliationAccountView,
    ) -> tuple[Decimal, Decimal, Decimal, str]:
        return (
            value.net_liquidation,
            value.total_cash,
            value.buying_power,
            value.currency,
        )

    @staticmethod
    def _exact_limit_scenarios(
        request: ShortPutDemoWhatIfRequest,
        selection: _FreshSelection,
        *,
        broker_commission: Decimal,
    ) -> tuple[ShortPutScenario, tuple[ShortPutRiskScenarioPoint, ...]]:
        contract = selection.candidate.contract
        deliverable = contract.deliverable_shares
        if deliverable is None:
            raise _DemoWhatIfEvidenceError(
                "The selected option has no verified standard deliverable."
            )

        def scenario(price: Decimal) -> ShortPutScenario:
            return short_put_scenario(
                ShortPutScenarioInput(
                    strike=contract.strike,
                    premium_per_share=request.limit_price,
                    multiplier=contract.multiplier,
                    deliverable_shares=deliverable,
                    deliverable_verified=True,
                    contracts=1,
                    commission=broker_commission,
                    commission_is_estimate=True,
                    scenario_price=price,
                    net_liquidation=selection.snapshot.account.net_liquidation,
                )
            )

        observed_spot = selection.risk_preview.observed_spot
        summary = scenario(observed_spot)
        if summary.effective_entry_price < 0:
            raise _DemoWhatIfEvidenceError(
                "The exact-limit effective entry is outside the supported range."
            )
        labeled_prices = (
            (RiskScenarioLabel.OBSERVED_SPOT, observed_spot),
            (RiskScenarioLabel.STRIKE, contract.strike),
            (RiskScenarioLabel.EFFECTIVE_ENTRY, summary.effective_entry_price),
            (RiskScenarioLabel.UNDERLYING_ZERO, Decimal("0")),
        )
        labels_by_price: dict[Decimal, list[RiskScenarioLabel]] = {}
        ordered_prices: list[Decimal] = []
        for label, price in labeled_prices:
            if price not in labels_by_price:
                labels_by_price[price] = []
                ordered_prices.append(price)
            labels_by_price[price].append(label)
        points = tuple(
            ShortPutRiskScenarioPoint(
                labels=tuple(labels_by_price[price]),
                underlying_price=price,
                put_intrinsic_value_per_share=max(contract.strike - price, Decimal("0")),
                expiration_pnl=scenario(price).expiration_pnl,
            )
            for price in ordered_prices
        )
        return summary, points

    @staticmethod
    def _timestamp_is_fresh(
        value: datetime,
        *,
        now: datetime,
        max_age_seconds: Decimal,
    ) -> bool:
        age = ShortPutDemoWhatIfService._timestamp_age_seconds(value, now=now)
        return age is not None and age <= max_age_seconds

    @staticmethod
    def _timestamp_age_seconds(value: datetime, *, now: datetime) -> Decimal | None:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return None
        age = now.astimezone(UTC) - value.astimezone(UTC)
        if age < timedelta(0):
            return None
        return Decimal(age.days * 86_400 + age.seconds) + (
            Decimal(age.microseconds) / Decimal("1000000")
        )

    def _withheld(
        self,
        request: ShortPutDemoWhatIfRequest,
        *,
        evaluated_at: datetime,
        reason: str,
        risk_refresh: ShortPutRiskPreviewResult | None = None,
    ) -> ShortPutDemoWhatIfResult:
        return ShortPutDemoWhatIfResult(
            request=request,
            status=DemoWhatIfStatus.WITHHELD,
            evaluated_at=evaluated_at,
            risk_refresh=risk_refresh,
            reasons=(reason,),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ShortPutDemoWhatIfService clock must return an aware datetime")
        return value.astimezone(UTC)

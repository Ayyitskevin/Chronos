from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from chronos.broker.base import Broker, BrokerDataError
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker, demo_now
from chronos.broker.market_data import MarketDataManager
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode, DemoProfile, OptionRight
from chronos.domain.models import (
    BrokerPosition,
    CancellationResult,
    LocalReconciliationEvidence,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
)
from chronos.services.reconciliation import ReconciliationCoordinator
from chronos.services.short_put_candidates import ShortPutCandidateService
from chronos.services.short_put_demo_what_if import (
    DemoWhatIfStatus,
    ShortPutDemoWhatIfRequest,
    ShortPutDemoWhatIfService,
)
from chronos.services.short_put_risk_preview import (
    RiskPreviewStatus,
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewResult,
    ShortPutRiskPreviewService,
)
from chronos.utils.identifiers import account_fingerprint

CORRELATION_ID = "CHR-WIF-0123456789ABCDEF0123456789ABCDEF"


class _EmptyLocalReader:
    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        assert current_account_id == DEMO_ACCOUNT_ID
        return LocalReconciliationEvidence(complete=True)


class _RiskPreviewer:
    def __init__(
        self,
        result: ShortPutRiskPreviewResult,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[ShortPutRiskPreviewRequest] = []

    def preview(self, request: ShortPutRiskPreviewRequest) -> ShortPutRiskPreviewResult:
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        return self.result


class _TrackingDemoBroker(DemoBroker):
    def __init__(self) -> None:
        super().__init__(clock=demo_now, profile=DemoProfile.EMPTY_ACCOUNT)
        self.preview_calls: list[OrderRequest] = []
        self.submit_calls = 0
        self.modify_calls = 0
        self.cancel_calls = 0
        self.preview_failure: Exception | None = None
        self.preview_mutator: Callable[[OrderPreview], OrderPreview] | None = None
        self.drift_after_preview = False

    def restore_one_position(self) -> None:
        self._positions = (self._drift_fixture_position(),)

    def _drift_fixture_position(self) -> BrokerPosition:
        return BrokerPosition(
            account_id=DEMO_ACCOUNT_ID,
            contract=self._underlyings["AAPL"],
            quantity=Decimal("1"),
            average_cost=Decimal("190"),
            market_price=Decimal("190"),
            unrealized_pnl=Decimal("0"),
        )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        self.preview_calls.append(request)
        if self.preview_failure is not None:
            raise self.preview_failure
        result = await super().preview_order(request)
        if self.drift_after_preview:
            self._positions = (self._drift_fixture_position(),)
        if self.preview_mutator is not None:
            result = self.preview_mutator(result)
        return result

    async def submit_order(self, request: OrderRequest) -> OrderSubmission:
        self.submit_calls += 1
        return await super().submit_order(request)

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        self.modify_calls += 1
        return await super().modify_order(request)

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self.cancel_calls += 1
        return await super().cancel_order(broker_order_id)


@dataclass
class _Harness:
    settings: Settings
    broker: _TrackingDemoBroker
    connection: BrokerConnectionManager
    risk_result: ShortPutRiskPreviewResult
    contract_id: int
    limit_price: Decimal

    def close(self) -> None:
        self.connection.close()


def _aligned_limit(bid: Decimal, ask: Decimal, tick: Decimal) -> Decimal:
    units = (bid / tick).to_integral_value(rounding=ROUND_CEILING)
    result = units * tick
    assert bid <= result <= ask
    return result


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        broker_mode=BrokerMode.DEMO,
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        max_expirations=1,
        max_strikes_per_expiration=1,
    )
    broker = _TrackingDemoBroker()
    connection = BrokerConnectionManager(broker)
    connection.connect()
    reconciliation = ReconciliationCoordinator(
        connection,
        _EmptyLocalReader(),
        settings.symbol_allowlist,
    )
    market_data = MarketDataManager(
        broker,
        max_quote_age=timedelta(seconds=settings.max_quote_age_seconds),
        clock=demo_now,
    )
    candidates = ShortPutCandidateService(
        connection=connection,
        market_data=market_data,
        reconciliation=reconciliation,
        settings=settings,
        expected_account_fingerprint=account_fingerprint(DEMO_ACCOUNT_ID),
        clock=demo_now,
    )
    evaluation = candidates.evaluate("AAPL")
    assert evaluation.resolution is not None
    assert evaluation.resolution.candidates
    selected = evaluation.resolution.candidates[0]
    limit_price = _aligned_limit(
        selected.bid,
        selected.ask,
        selected.contract.min_tick,
    )
    risk_result = ShortPutRiskPreviewService(candidates, clock=demo_now).preview(
        ShortPutRiskPreviewRequest(
            symbol="AAPL",
            selected_contract_id=selected.contract.con_id,
            total_commission_estimate=Decimal("0.65"),
        )
    )
    assert risk_result.status is RiskPreviewStatus.READY
    return _Harness(
        settings=settings,
        broker=broker,
        connection=connection,
        risk_result=risk_result,
        contract_id=selected.contract.con_id,
        limit_price=limit_price,
    )


def _request(harness: _Harness, **updates: object) -> ShortPutDemoWhatIfRequest:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "selected_contract_id": harness.contract_id,
        "total_commission_estimate": Decimal("0.65"),
        "limit_price": harness.limit_price,
    }
    values.update(updates)
    return ShortPutDemoWhatIfRequest.model_validate(values)


def _service(
    harness: _Harness,
    risk: _RiskPreviewer,
    *,
    settings: Settings | None = None,
) -> ShortPutDemoWhatIfService:
    return ShortPutDemoWhatIfService(
        risk,
        harness.connection,
        settings or harness.settings,
        account_fingerprint(DEMO_ACCOUNT_ID),
        clock=demo_now,
        correlation_factory=lambda: CORRELATION_ID,
    )


def test_demo_boundary_attestation_accepts_same_objects_and_equal_settings_clone(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    service = _service(harness, _RiskPreviewer(harness.risk_result))
    try:
        assert service.is_bound_to_demo_boundary(harness.connection, harness.settings)
        assert service.is_bound_to_demo_boundary(
            harness.connection,
            harness.settings.model_copy(deep=True),
        )
    finally:
        harness.close()


def test_demo_boundary_attestation_rejects_changed_or_malformed_settings(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    service = _service(harness, _RiskPreviewer(harness.risk_result))
    changed = harness.settings.model_copy(update={"max_quote_age_seconds": 4})
    non_demo = harness.settings.model_copy(update={"broker_mode": BrokerMode.IBKR})
    try:
        assert not service.is_bound_to_demo_boundary(harness.connection, changed)
        assert not service.is_bound_to_demo_boundary(harness.connection, non_demo)
        assert not service.is_bound_to_demo_boundary(harness.connection, object())
        assert not service.is_bound_to_demo_boundary(object(), harness.settings)
    finally:
        harness.close()


def test_demo_boundary_attestation_rejects_invalid_captured_settings(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    invalid = harness.settings.model_copy(update={"max_quote_age_seconds": 0})
    service = _service(
        harness,
        _RiskPreviewer(harness.risk_result),
        settings=invalid,
    )
    try:
        assert not service.is_bound_to_demo_boundary(harness.connection, invalid)
    finally:
        harness.close()


def test_demo_boundary_attestation_rejects_connection_or_broker_identity_drift(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    service = _service(harness, _RiskPreviewer(harness.risk_result))
    other_connection = BrokerConnectionManager(harness.broker)
    replacement = DemoBroker(clock=demo_now, profile=DemoProfile.EMPTY_ACCOUNT)
    try:
        assert not service.is_bound_to_demo_boundary(other_connection, harness.settings)

        harness.connection.broker = replacement
        assert not service.is_bound_to_demo_boundary(harness.connection, harness.settings)
    finally:
        harness.connection.broker = harness.broker
        other_connection.close()
        harness.close()


def test_demo_boundary_attestation_accepts_demo_broker_subclass(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    service = _service(harness, _RiskPreviewer(harness.risk_result))
    try:
        assert isinstance(harness.broker, DemoBroker)
        assert service.is_bound_to_demo_boundary(harness.connection, harness.settings)
    finally:
        harness.close()


@pytest.mark.parametrize(
    "timeout",
    [False, 0, -1, float("nan"), float("inf"), 30.01, 1_000_000],
)
def test_observation_timeout_is_finite_and_hard_bounded(
    tmp_path: Path,
    timeout: float,
) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    try:
        with pytest.raises(ValueError, match="finite, positive"):
            ShortPutDemoWhatIfService(
                risk,
                harness.connection,
                harness.settings,
                account_fingerprint(DEMO_ACCOUNT_ID),
                observation_timeout_seconds=timeout,
            )
    finally:
        harness.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"symbol": ""},
        {"symbol": "AAPL!"},
        {"selected_contract_id": 0},
        {"selected_contract_id": True},
        {"selected_contract_id": "2002"},
        {"total_commission_estimate": Decimal("NaN")},
        {"total_commission_estimate": Decimal("-0.01")},
        {"limit_price": Decimal("NaN")},
        {"limit_price": Decimal("Infinity")},
        {"limit_price": Decimal("0")},
        {"limit_price": Decimal("1000000.0001")},
        {"limit_price": Decimal("1.23456")},
        {"limit_price": Decimal("1e-999999999999999999")},
    ],
)
def test_request_rejects_invalid_or_unbounded_terms(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "selected_contract_id": 2002,
        "total_commission_estimate": Decimal("0.65"),
        "limit_price": Decimal("3.20"),
    }

    with pytest.raises(ValidationError):
        ShortPutDemoWhatIfRequest.model_validate(values | updates)


def test_ready_rehearsal_uses_one_exact_nontransmitting_request_and_safe_output(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    try:
        request = _request(harness)
        result = _service(harness, risk).preview(request)

        assert result.status is DemoWhatIfStatus.WHAT_IF_PREVIEWED
        assert result.request == request
        assert result.risk_refresh == harness.risk_result
        assert result.reasons == ()
        assert result.opening_actions_locked is True
        assert risk.calls == [
            ShortPutRiskPreviewRequest(
                symbol="AAPL",
                selected_contract_id=harness.contract_id,
                total_commission_estimate=Decimal("0.65"),
            )
        ]
        assert len(harness.broker.preview_calls) == 1
        broker_request = harness.broker.preview_calls[0]
        assert broker_request.contract.con_id == harness.contract_id
        assert broker_request.quantity == 1
        assert broker_request.limit_price == harness.limit_price
        assert broker_request.transmit is False
        assert broker_request.outside_rth is False
        assert broker_request.account_id == DEMO_ACCOUNT_ID
        assert harness.broker.submit_calls == 0
        assert harness.broker.modify_calls == 0
        assert harness.broker.cancel_calls == 0

        preview = result.preview
        assert preview is not None
        assert preview.correlation_id == CORRELATION_ID
        assert preview.selected_contract.con_id == harness.contract_id
        assert preview.quantity == 1
        assert preview.limit_price == harness.limit_price
        assert preview.broker_estimated_commission == Decimal("0.65")
        assert preview.operator_commission_estimate == Decimal("0.65")
        assert preview.commission_variance == 0
        assert preview.initial_margin_change == 0
        assert preview.maintenance_margin_change == 0
        assert preview.equity_with_loan_change == 0
        assert preview.gross_premium == harness.limit_price * Decimal("100")
        assert preview.net_premium == preview.gross_premium - Decimal("0.65")
        assert preview.transmit is False
        assert preview.demo_rehearsal_only is True
        assert preview.broker_authorized is False
        assert preview.user_confirmation_available is False
        assert preview.submission_authorized is False
        assert preview.opening_actions_locked is True
        serialized = result.model_dump_json()
        assert DEMO_ACCOUNT_ID not in serialized
        assert "Demo preview only" not in serialized
    finally:
        harness.close()


def test_service_revalidates_model_copy_before_refresh(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    corrupted = _request(harness).model_copy(update={"limit_price": Decimal("NaN")})
    try:
        with pytest.raises(ValidationError):
            _service(harness, risk).preview(corrupted)

        assert risk.calls == []
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_non_demo_configuration_is_withheld_before_fresh_or_order_calls(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    settings = harness.settings.model_copy(update={"broker_mode": BrokerMode.IBKR})
    try:
        result = _service(harness, risk, settings=settings).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert "only in DEMO mode" in result.reasons[0]
        assert risk.calls == []
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_non_demo_adapter_is_withheld_before_fresh_or_order_calls(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    original_broker = harness.connection.broker
    harness.connection.broker = cast(Broker, object())
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert "only in DEMO mode" in result.reasons[0]
        assert risk.calls == []
        assert harness.broker.preview_calls == []
    finally:
        harness.connection.broker = original_broker
        harness.close()


@pytest.mark.parametrize("offset", [Decimal("-0.01"), Decimal("0.01")])
def test_limit_outside_fresh_spread_is_withheld_before_broker_preview(
    tmp_path: Path,
    offset: Decimal,
) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    assert harness.risk_result.candidate_refresh is not None
    assert harness.risk_result.candidate_refresh.resolution is not None
    candidate = next(
        item
        for item in harness.risk_result.candidate_refresh.resolution.candidates
        if item.contract.con_id == harness.contract_id
    )
    limit = candidate.bid + offset if offset < 0 else candidate.ask + offset
    try:
        result = _service(harness, risk).preview(_request(harness, limit_price=limit))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert "fresh quoted spread" in result.reasons[0]
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_misaligned_limit_is_withheld_before_broker_preview(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    assert harness.risk_result.candidate_refresh is not None
    assert harness.risk_result.candidate_refresh.resolution is not None
    candidate = next(
        item
        for item in harness.risk_result.candidate_refresh.resolution.candidates
        if item.contract.con_id == harness.contract_id
    )
    limit = candidate.bid + Decimal("0.001")
    assert candidate.bid <= limit <= candidate.ask
    assert limit % candidate.contract.min_tick != 0
    try:
        result = _service(harness, risk).preview(_request(harness, limit_price=limit))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert "minimum tick" in result.reasons[0]
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_stale_risk_result_is_withheld_before_broker_preview(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    stale = harness.risk_result.model_copy(
        update={"evaluated_at": demo_now() - timedelta(seconds=6)}
    )
    risk = _RiskPreviewer(stale)
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert "chronology" in result.reasons[0]
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_stale_underlying_inside_ready_risk_is_withheld_before_broker_preview(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    refresh = harness.risk_result.candidate_refresh
    assert refresh is not None
    assert refresh.underlying_quote is not None
    stale_refresh = refresh.model_copy(
        update={
            "underlying_quote": refresh.underlying_quote.model_copy(
                update={"timestamp": demo_now() - timedelta(seconds=60)}
            )
        }
    )
    risk = _RiskPreviewer(
        harness.risk_result.model_copy(update={"candidate_refresh": stale_refresh})
    )
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_future_underlying_relative_to_candidate_refresh_is_withheld_before_broker_preview(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    refresh = harness.risk_result.candidate_refresh
    preview = harness.risk_result.preview
    assert refresh is not None
    assert preview is not None
    earlier = demo_now() - timedelta(seconds=1)
    risk = _RiskPreviewer(
        harness.risk_result.model_copy(
            update={
                "candidate_refresh": refresh.model_copy(update={"evaluated_at": earlier}),
                "preview": preview.model_copy(update={"evidence_evaluated_at": earlier}),
            }
        )
    )
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert "chronology" in result.reasons[0]
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_wrong_underlying_symbol_inside_ready_risk_is_withheld_before_broker_preview(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    refresh = harness.risk_result.candidate_refresh
    assert refresh is not None
    assert refresh.underlying_quote is not None
    wrong_underlying = refresh.underlying_quote.contract.model_copy(update={"symbol": "MSFT"})
    tampered_refresh = refresh.model_copy(
        update={
            "underlying_quote": refresh.underlying_quote.model_copy(
                update={"contract": wrong_underlying}
            )
        }
    )
    risk = _RiskPreviewer(
        harness.risk_result.model_copy(update={"candidate_refresh": tampered_refresh})
    )
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    "candidate_update",
    [
        {"symbol": "MSFT"},
        {"right": OptionRight.CALL},
        {"strike": Decimal("999")},
        {"expiration": demo_now().date() + timedelta(days=99)},
    ],
)
def test_tampered_ranked_candidate_identity_is_withheld_before_broker_preview(
    tmp_path: Path,
    candidate_update: dict[str, object],
) -> None:
    harness = _harness(tmp_path)
    refresh = harness.risk_result.candidate_refresh
    assert refresh is not None
    assert refresh.resolution is not None
    resolution = refresh.resolution
    selected = next(
        candidate
        for candidate in resolution.candidates
        if candidate.contract.con_id == harness.contract_id
    )
    tampered_candidates = tuple(
        candidate.model_copy(update=candidate_update)
        if candidate.contract.con_id == harness.contract_id
        else candidate
        for candidate in resolution.candidates
    )
    tampered_refresh = refresh.model_copy(
        update={"resolution": resolution.model_copy(update={"candidates": tampered_candidates})}
    )
    risk = _RiskPreviewer(
        harness.risk_result.model_copy(update={"candidate_refresh": tampered_refresh})
    )
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert selected.contract.con_id == harness.contract_id
        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_m7_recomputes_capital_against_its_current_settings(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(harness.risk_result)
    stricter_settings = harness.settings.model_copy(
        update={
            "max_symbol_allocation_pct": Decimal("0.01"),
            "max_total_wheel_allocation_pct": Decimal("0.01"),
        }
    )
    try:
        result = _service(harness, risk, settings=stricter_settings).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    "preview_update",
    [
        {"candidate_rank": 2},
        {"observed_spot": Decimal("999.00")},
        {"hypothetical_credit_per_share": Decimal("0.01")},
        {"option_quote_age_seconds": Decimal("1")},
    ],
)
def test_tampered_ready_risk_scalars_are_withheld_before_broker_preview(
    tmp_path: Path,
    preview_update: dict[str, object],
) -> None:
    harness = _harness(tmp_path)
    preview = harness.risk_result.preview
    assert preview is not None
    risk = _RiskPreviewer(
        harness.risk_result.model_copy(
            update={"preview": preview.model_copy(update=preview_update)}
        )
    )
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda preview: preview.model_copy(update={"accepted": False}),
        lambda preview: preview.model_copy(update={"estimated_commission": None}),
        lambda preview: preview.model_copy(update={"estimated_commission": Decimal("NaN")}),
        lambda preview: preview.model_copy(update={"initial_margin_change": None}),
        lambda preview: preview.model_copy(
            update={"previewed_at": demo_now() + timedelta(seconds=1)}
        ),
        lambda preview: preview.model_copy(
            update={"previewed_at": demo_now() - timedelta(seconds=1)}
        ),
        lambda preview: preview.model_copy(
            update={
                "request": preview.request.model_copy(
                    update={"limit_price": preview.request.limit_price + Decimal("0.05")}
                )
            }
        ),
    ],
)
def test_untrusted_broker_response_is_withheld(
    tmp_path: Path,
    mutator: Callable[[OrderPreview], OrderPreview],
) -> None:
    harness = _harness(tmp_path)
    harness.broker.preview_mutator = mutator
    risk = _RiskPreviewer(harness.risk_result)
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert len(harness.broker.preview_calls) == 1
        assert harness.broker.submit_calls == 0
        assert harness.broker.modify_calls == 0
        assert harness.broker.cancel_calls == 0
    finally:
        harness.close()


def test_post_preview_exposure_drift_withholds_result(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.broker.drift_after_preview = True
    risk = _RiskPreviewer(harness.risk_result)
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert (
            "Exposure or capital" in result.reasons[0] or "exposure or capital" in result.reasons[0]
        )
        assert len(harness.broker.preview_calls) == 1
    finally:
        harness.close()


def test_pre_preview_exposure_drift_withholds_before_broker_preview(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.broker.restore_one_position()
    risk = _RiskPreviewer(harness.risk_result)
    try:
        result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert "before" in result.reasons[0]
        assert harness.broker.preview_calls == []
    finally:
        harness.close()


def test_broker_failure_is_sanitized_and_never_submits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _harness(tmp_path)
    harness.broker.preview_failure = BrokerDataError(
        f"{DEMO_ACCOUNT_ID} sensitive broker preview detail"
    )
    risk = _RiskPreviewer(harness.risk_result)
    service_logger = logging.getLogger("chronos.services.short_put_demo_what_if")
    service_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=service_logger.name):
            result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert result.reasons == ("Broker what-if evidence could not be obtained safely.",)
        assert harness.broker.submit_calls == 0
        assert harness.broker.modify_calls == 0
        assert harness.broker.cancel_calls == 0
        text = caplog.text
        assert DEMO_ACCOUNT_ID not in text
        assert "sensitive broker preview detail" not in text
        assert any(
            getattr(record, "error_type", None) == "BrokerDataError" for record in caplog.records
        )
    finally:
        service_logger.removeHandler(caplog.handler)
        harness.close()


def test_risk_refresh_failure_is_sanitized_before_any_order_call(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _harness(tmp_path)
    risk = _RiskPreviewer(
        harness.risk_result,
        failure=BrokerDataError(f"{DEMO_ACCOUNT_ID} sensitive risk detail"),
    )
    service_logger = logging.getLogger("chronos.services.short_put_demo_what_if")
    service_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=service_logger.name):
            result = _service(harness, risk).preview(_request(harness))

        assert result.status is DemoWhatIfStatus.WITHHELD
        assert result.preview is None
        assert harness.broker.preview_calls == []
        assert DEMO_ACCOUNT_ID not in caplog.text
        assert "sensitive risk detail" not in caplog.text
        assert any(
            getattr(record, "error_type", None) == "BrokerDataError" for record in caplog.records
        )
    finally:
        service_logger.removeHandler(caplog.handler)
        harness.close()

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from tests.unit.test_short_put_demo_what_if import (
    _Harness as _WhatIfHarness,
)
from tests.unit.test_short_put_demo_what_if import (
    _harness,
    _RiskPreviewer,
)
from tests.unit.test_short_put_demo_what_if import (
    _request as _what_if_request,
)
from tests.unit.test_short_put_demo_what_if import (
    _service as _what_if_service,
)

from chronos.broker.base import Broker, BrokerDataError
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker, demo_now
from chronos.config.settings import Settings
from chronos.domain.enums import (
    BrokerMode,
    DemoProfile,
    OptionRight,
    OrderLifecycle,
    ReconciliationStatus,
)
from chronos.services.short_put_demo_approval import (
    DemoApprovalStatus,
    ShortPutDemoApprovalContract,
    ShortPutDemoApprovalReceipt,
    ShortPutDemoApprovalRequest,
    ShortPutDemoApprovalResult,
    ShortPutDemoApprovalService,
)
from chronos.services.short_put_demo_what_if import (
    DemoWhatIfStatus,
    ShortPutDemoWhatIfRequest,
    ShortPutDemoWhatIfResult,
    ShortPutDemoWhatIfService,
)
from chronos.services.short_put_risk_preview import (
    RiskPreviewStatus,
    ShortPutRiskPreviewRequest,
)

APPROVAL_REFERENCE = "CHR-APR-0123456789ABCDEF0123456789ABCDEF"


class _WhatIfPreviewer:
    def __init__(
        self,
        result: ShortPutDemoWhatIfResult,
        *,
        failure: Exception | None = None,
        before_return: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.before_return = before_return
        self.calls: list[ShortPutDemoWhatIfRequest] = []

    def preview(self, request: ShortPutDemoWhatIfRequest) -> ShortPutDemoWhatIfResult:
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        if self.before_return is not None:
            self.before_return()
        return self.result


@dataclass
class _ApprovalHarness:
    base: _WhatIfHarness
    risk: _RiskPreviewer
    what_if_service: ShortPutDemoWhatIfService
    what_if: ShortPutDemoWhatIfResult

    def close(self) -> None:
        self.base.close()


def _approval_harness(tmp_path: Path) -> _ApprovalHarness:
    base = _harness(tmp_path)
    risk = _RiskPreviewer(base.risk_result)
    what_if_service = _what_if_service(base, risk)
    what_if = what_if_service.preview(_what_if_request(base))
    assert what_if.status is DemoWhatIfStatus.WHAT_IF_PREVIEWED
    assert what_if.preview is not None
    base.broker.preview_calls.clear()
    risk.calls.clear()
    return _ApprovalHarness(
        base=base,
        risk=risk,
        what_if_service=what_if_service,
        what_if=what_if,
    )


def _request(
    harness: _ApprovalHarness,
    **updates: object,
) -> ShortPutDemoApprovalRequest:
    preview = harness.what_if.preview
    assert preview is not None
    values: dict[str, object] = {
        "symbol": preview.symbol,
        "selected_contract_id": preview.selected_contract.con_id,
        "total_commission_estimate": preview.operator_commission_estimate,
        "limit_price": preview.limit_price,
        "gross_assignment_obligation": preview.gross_assignment_obligation,
        "typed_symbol": preview.symbol,
        "acknowledged_quantity": 1,
        "acknowledged_contract_id": preview.selected_contract.con_id,
        "acknowledged_limit_price": preview.limit_price,
        "acknowledged_gross_assignment_obligation": preview.gross_assignment_obligation,
        "risk_terms_acknowledged": True,
    }
    values.update(updates)
    return ShortPutDemoApprovalRequest.model_validate(values)


def _service(
    harness: _ApprovalHarness,
    previewer: object | None = None,
    *,
    settings: Settings | None = None,
    reference_factory: Callable[[], str] | None = None,
) -> ShortPutDemoApprovalService:
    return ShortPutDemoApprovalService(
        cast(
            ShortPutDemoWhatIfService,
            previewer if previewer is not None else harness.what_if_service,
        ),
        harness.base.connection,
        settings or harness.base.settings,
        clock=demo_now,
        reference_factory=reference_factory or (lambda: APPROVAL_REFERENCE),
    )


def _assert_no_order_methods(harness: _ApprovalHarness) -> None:
    assert harness.base.broker.submit_calls == 0
    assert harness.base.broker.modify_calls == 0
    assert harness.base.broker.cancel_calls == 0


def test_success_rehearses_exact_terms_once_without_order_or_account_authority(
    tmp_path: Path,
) -> None:
    harness = _approval_harness(tmp_path)
    try:
        request = _request(harness)
        result = _service(harness).rehearse(request)

        assert result.status is DemoApprovalStatus.APPROVAL_REHEARSED
        assert result.status.value != OrderLifecycle.USER_CONFIRMED.value
        assert result.request == request
        assert result.reasons == ()
        assert result.opening_actions_locked is True
        assert harness.risk.calls == [
            ShortPutRiskPreviewRequest(
                symbol=request.symbol,
                selected_contract_id=request.selected_contract_id,
                total_commission_estimate=request.total_commission_estimate,
            )
        ]
        assert len(harness.base.broker.preview_calls) == 1
        assert harness.base.broker.preview_calls[0].transmit is False

        receipt = result.receipt
        assert receipt is not None
        assert receipt.approval_reference == APPROVAL_REFERENCE
        assert receipt.symbol == request.symbol
        assert receipt.selected_contract.con_id == request.selected_contract_id
        assert not hasattr(receipt.selected_contract, "local_symbol")
        assert not hasattr(receipt.selected_contract, "exchange")
        assert not hasattr(receipt.selected_contract, "trading_class")
        assert receipt.quantity == 1
        assert receipt.limit_price == request.limit_price
        assert receipt.gross_assignment_obligation == request.gross_assignment_obligation
        assert receipt.risk_terms_acknowledged is True
        assert receipt.demo_rehearsal_only is True
        assert receipt.lifecycle_transition_recorded is False
        assert receipt.persistence_recorded is False
        assert receipt.approval_authority_created is False
        assert receipt.broker_request_included is False
        assert receipt.broker_text_included is False
        assert receipt.submission_authorized is False
        assert receipt.submission_locked is True
        assert receipt.opening_actions_locked is True
        with pytest.raises(ValidationError):
            receipt.limit_price = Decimal("1")

        serialized = result.model_dump_json()
        assert DEMO_ACCOUNT_ID not in serialized
        assert "Demo preview only" not in serialized
        assert '"account_id":' not in serialized
        assert "OrderRequest" not in serialized
        assert '"what_if_refresh":' not in serialized
        assert '"local_symbol":' not in serialized
        assert '"trading_class":' not in serialized
        _assert_no_order_methods(harness)
    finally:
        harness.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"symbol": ""},
        {"symbol": "A" * 33},
        {"selected_contract_id": 0},
        {"selected_contract_id": True},
        {"selected_contract_id": "2002"},
        {"selected_contract_id": 9_223_372_036_854_775_808},
        {"total_commission_estimate": True},
        {"total_commission_estimate": Decimal("NaN")},
        {"total_commission_estimate": Decimal("Infinity")},
        {"total_commission_estimate": Decimal("10000.0001")},
        {"total_commission_estimate": "1" * 33},
        {"limit_price": True},
        {"limit_price": Decimal("NaN")},
        {"limit_price": Decimal("Infinity")},
        {"limit_price": Decimal("0")},
        {"limit_price": Decimal("1000000.0001")},
        {"limit_price": Decimal("1.23456")},
        {"limit_price": Decimal("1e-999999999999999999")},
        {"gross_assignment_obligation": True},
        {"gross_assignment_obligation": Decimal("NaN")},
        {"gross_assignment_obligation": Decimal("Infinity")},
        {"gross_assignment_obligation": Decimal("0")},
        {"gross_assignment_obligation": Decimal("100000000.0001")},
        {"typed_symbol": ""},
        {"typed_symbol": "A" * 33},
        {"typed_symbol": "AAPL\n"},
        {"acknowledged_quantity": True},
        {"acknowledged_quantity": "1"},
        {"acknowledged_quantity": 0},
        {"acknowledged_contract_id": True},
        {"acknowledged_contract_id": "2002"},
        {"acknowledged_contract_id": 0},
        {"acknowledged_limit_price": True},
        {"acknowledged_limit_price": Decimal("NaN")},
        {"acknowledged_gross_assignment_obligation": True},
        {"acknowledged_gross_assignment_obligation": Decimal("Infinity")},
        {"risk_terms_acknowledged": 1},
        {"risk_terms_acknowledged": "true"},
    ],
)
def test_request_rejects_malformed_or_unbounded_values(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "selected_contract_id": 2002,
        "total_commission_estimate": Decimal("0.65"),
        "limit_price": Decimal("3.20"),
        "gross_assignment_obligation": Decimal("18500"),
        "typed_symbol": "AAPL",
        "acknowledged_quantity": 1,
        "acknowledged_contract_id": 2002,
        "acknowledged_limit_price": Decimal("3.20"),
        "acknowledged_gross_assignment_obligation": Decimal("18500"),
        "risk_terms_acknowledged": True,
    }

    with pytest.raises(ValidationError):
        ShortPutDemoApprovalRequest.model_validate(values | updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"typed_symbol": "aapl"},
        {"typed_symbol": " AAPL "},
        {"typed_symbol": "MSFT"},
        {"acknowledged_quantity": 2},
        {"acknowledged_contract_id": 9999},
        {"acknowledged_limit_price": Decimal("3.21")},
        {"acknowledged_gross_assignment_obligation": Decimal("18499")},
        {"risk_terms_acknowledged": False},
    ],
)
def test_preflight_mismatch_withholds_before_m7(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    harness = _approval_harness(tmp_path)
    try:
        result = _service(harness).rehearse(_request(harness, **updates))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert result.reasons
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_service_revalidates_request_copy_before_m7(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    corrupted = _request(harness).model_copy(update={"limit_price": Decimal("NaN")})
    try:
        with pytest.raises(ValidationError):
            _service(harness).rehearse(corrupted)

        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_non_demo_mode_withholds_before_m7(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    settings = harness.base.settings.model_copy(update={"broker_mode": BrokerMode.IBKR})
    try:
        result = _service(harness, settings=settings).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert "only" in result.reasons[0]
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_different_capital_settings_withhold_before_m7(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    settings = harness.base.settings.model_copy(
        update={"max_symbol_allocation_pct": Decimal("0.01")}
    )
    try:
        result = _service(harness, settings=settings).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_cached_m7_capital_policy_replay_is_recomputed_and_withheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    cached = harness.what_if
    calls: list[ShortPutDemoWhatIfRequest] = []
    original_allocation = harness.base.settings.max_symbol_allocation_pct

    def cached_preview(
        _service: ShortPutDemoWhatIfService,
        request: ShortPutDemoWhatIfRequest,
    ) -> ShortPutDemoWhatIfResult:
        calls.append(request)
        return cached

    # Settings is frozen (ADR-0009); this test deliberately simulates policy
    # drift between the cached what-if and the recompute, so it bypasses the
    # freeze explicitly — a tamper no production code path can perform.
    object.__setattr__(harness.base.settings, "max_symbol_allocation_pct", Decimal("0.01"))
    monkeypatch.setattr(ShortPutDemoWhatIfService, "preview", cached_preview)
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(calls) == 1
        _assert_no_order_methods(harness)
    finally:
        object.__setattr__(harness.base.settings, "max_symbol_allocation_pct", original_allocation)
        harness.close()


def test_non_concrete_m7_provider_withholds_before_call(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    previewer = _WhatIfPreviewer(harness.what_if)
    try:
        result = _service(harness, previewer).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert previewer.calls == []
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_non_demo_adapter_withholds_before_m7(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    original = harness.base.connection.broker
    harness.base.connection.broker = cast(Broker, object())
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.base.connection.broker = original
        harness.close()


def test_different_demo_adapter_withholds_before_m7(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    original = harness.base.connection.broker
    replacement = DemoBroker(clock=demo_now, profile=DemoProfile.SAFETY_CASES)
    harness.base.connection.broker = replacement
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert harness.risk.calls == []
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.base.connection.broker = original
        harness.close()


def test_adapter_swap_during_m7_withholds_after_one_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    original = harness.base.connection.broker
    replacement = DemoBroker(clock=demo_now, profile=DemoProfile.EMPTY_ACCOUNT)
    original_preview = harness.risk.preview

    def swap_after_risk(request: ShortPutRiskPreviewRequest):
        result = original_preview(request)
        harness.base.connection.broker = replacement
        return result

    monkeypatch.setattr(harness.risk, "preview", swap_after_risk)
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert len(harness.risk.calls) == 1
        assert len(harness.base.broker.preview_calls) == 1
        _assert_no_order_methods(harness)
    finally:
        harness.base.connection.broker = original
        harness.close()


def test_m7_withheld_never_becomes_approval(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    withheld = harness.base.risk_result.model_copy(
        update={
            "status": RiskPreviewStatus.WITHHELD,
            "preview": None,
            "reasons": ("Fresh evidence withheld.",),
        }
    )
    harness.risk.result = withheld
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(harness.risk.calls) == 1
        assert harness.base.broker.preview_calls == []
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_real_m7_broker_descriptive_text_is_not_carried_into_m8(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    risk = harness.base.risk_result
    risk_preview = risk.preview
    refresh = risk.candidate_refresh
    assert risk_preview is not None
    assert refresh is not None
    resolution = refresh.resolution
    assert resolution is not None
    candidate = resolution.candidates[0]
    raw_contract = candidate.contract.model_copy(
        update={"local_symbol": f"{DEMO_ACCOUNT_ID} RAW BROKER TEXT"}
    )
    raw_candidate = candidate.model_copy(
        update={
            "contract": raw_contract,
            "selection_rationale": f"{DEMO_ACCOUNT_ID} RAW BROKER TEXT",
        }
    )
    harness.risk.result = risk.model_copy(
        update={
            "candidate_refresh": refresh.model_copy(
                update={
                    "resolution": resolution.model_copy(update={"candidates": (raw_candidate,)})
                }
            ),
            "preview": risk_preview.model_copy(update={"selected_contract": raw_contract}),
        }
    )
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.APPROVAL_REHEARSED
        assert result.receipt is not None
        serialized = result.model_dump_json()
        assert DEMO_ACCOUNT_ID not in serialized
        assert "RAW BROKER TEXT" not in serialized
        assert "local_symbol" not in serialized
        assert "selection_rationale" not in serialized
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def _tampered_what_if(
    original: ShortPutDemoWhatIfResult,
    case: str,
) -> ShortPutDemoWhatIfResult:
    preview = original.preview
    risk = original.risk_refresh
    assert preview is not None
    assert risk is not None
    assert risk.preview is not None
    if case == "request":
        return original.model_copy(
            update={
                "request": original.request.model_copy(
                    update={"selected_contract_id": original.request.selected_contract_id + 1}
                )
            }
        )
    if case == "commission":
        return original.model_copy(
            update={
                "preview": preview.model_copy(
                    update={
                        "operator_commission_estimate": (
                            preview.operator_commission_estimate + Decimal("0.01")
                        )
                    }
                )
            }
        )
    if case == "obligation":
        return original.model_copy(
            update={
                "preview": preview.model_copy(
                    update={
                        "gross_assignment_obligation": (
                            preview.gross_assignment_obligation + Decimal("1")
                        )
                    }
                )
            }
        )
    if case == "submission":
        return original.model_copy(
            update={"preview": preview.model_copy(update={"submission_authorized": True})}
        )
    if case == "broker_text":
        return original.model_copy(
            update={
                "preview": preview.model_copy(
                    update={
                        "broker_warning_count": 1,
                        "broker_warning_notice": f"{DEMO_ACCOUNT_ID} raw broker message",
                    }
                )
            }
        )
    if case == "risk_request":
        return original.model_copy(
            update={
                "risk_refresh": risk.model_copy(
                    update={
                        "request": risk.request.model_copy(
                            update={"selected_contract_id": risk.request.selected_contract_id + 1}
                        )
                    }
                )
            }
        )
    if case == "risk_contract":
        call_contract = risk.preview.selected_contract.model_copy(
            update={"right": OptionRight.CALL}
        )
        return original.model_copy(
            update={
                "risk_refresh": risk.model_copy(
                    update={
                        "preview": risk.preview.model_copy(
                            update={"selected_contract": call_contract}
                        )
                    }
                )
            }
        )
    if case == "huge_text":
        return original.model_copy(
            update={
                "risk_refresh": risk.model_copy(
                    update={
                        "preview": risk.preview.model_copy(update={"assumptions": ("X" * 513,)})
                    }
                )
            }
        )
    if case == "huge_decimal":
        return original.model_copy(
            update={"preview": preview.model_copy(update={"net_premium": Decimal("1e129")})}
        )
    if case == "derived_net_premium":
        return original.model_copy(
            update={
                "preview": preview.model_copy(
                    update={"net_premium": preview.net_premium + Decimal("1")}
                )
            }
        )
    if case == "coordinated_obligation":
        return original.model_copy(
            update={
                "preview": preview.model_copy(update={"gross_assignment_obligation": Decimal("1")}),
                "risk_refresh": risk.model_copy(
                    update={
                        "preview": risk.preview.model_copy(
                            update={"gross_assignment_obligation": Decimal("1")}
                        )
                    }
                ),
            }
        )
    if case == "off_tick":
        off_tick = Decimal("3.21")
        return original.model_copy(
            update={
                "request": original.request.model_copy(update={"limit_price": off_tick}),
                "preview": preview.model_copy(update={"limit_price": off_tick}),
            }
        )
    if case == "detached_contract":
        detached = preview.selected_contract.model_copy(update={"strike": Decimal("1")})
        return original.model_copy(
            update={
                "preview": preview.model_copy(update={"selected_contract": detached}),
                "risk_refresh": risk.model_copy(
                    update={
                        "preview": risk.preview.model_copy(update={"selected_contract": detached})
                    }
                ),
            }
        )
    if case == "detached_rank":
        return original.model_copy(
            update={
                "preview": preview.model_copy(update={"candidate_rank": 999}),
                "risk_refresh": risk.model_copy(
                    update={"preview": risk.preview.model_copy(update={"candidate_rank": 999})}
                ),
            }
        )
    if case == "invalid_reconciliation":
        refresh = risk.candidate_refresh
        assert refresh is not None
        reconciliation = refresh.reconciliation
        assert reconciliation is not None
        invalid_refresh = refresh.model_copy(
            update={
                "reconciliation": reconciliation.model_copy(
                    update={"status": ReconciliationStatus.MANUAL_REVIEW}
                )
            }
        )
        return original.model_copy(
            update={"risk_refresh": risk.model_copy(update={"candidate_refresh": invalid_refresh})}
        )
    if case == "raw_assumption":
        return original.model_copy(
            update={
                "risk_refresh": risk.model_copy(
                    update={
                        "preview": risk.preview.model_copy(
                            update={
                                "assumptions": (
                                    *risk.preview.assumptions,
                                    f"{DEMO_ACCOUNT_ID} raw broker text",
                                )
                            }
                        )
                    }
                )
            }
        )
    raise AssertionError(f"unknown tamper case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "request",
        "commission",
        "obligation",
        "submission",
        "broker_text",
        "risk_request",
        "risk_contract",
        "huge_text",
        "huge_decimal",
        "derived_net_premium",
        "coordinated_obligation",
        "off_tick",
        "detached_contract",
        "detached_rank",
        "invalid_reconciliation",
        "raw_assumption",
    ],
)
def test_tampered_m7_output_is_withheld(
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    tampered = _tampered_what_if(harness.what_if, case)
    calls: list[ShortPutDemoWhatIfRequest] = []

    def forged_preview(
        _service: ShortPutDemoWhatIfService,
        request: ShortPutDemoWhatIfRequest,
    ) -> ShortPutDemoWhatIfResult:
        calls.append(request)
        return tampered

    monkeypatch.setattr(ShortPutDemoWhatIfService, "preview", forged_preview)
    request_updates: dict[str, object] = {}
    if case == "coordinated_obligation":
        request_updates = {
            "gross_assignment_obligation": Decimal("1"),
            "acknowledged_gross_assignment_obligation": Decimal("1"),
        }
    elif case == "off_tick":
        request_updates = {
            "limit_price": Decimal("3.21"),
            "acknowledged_limit_price": Decimal("3.21"),
        }
    try:
        result = _service(harness).rehearse(_request(harness, **request_updates))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(calls) == 1
        assert DEMO_ACCOUNT_ID not in result.model_dump_json()
        _assert_no_order_methods(harness)
    finally:
        harness.close()


@pytest.mark.parametrize("case", ["stale", "future", "backward", "nested_stale"])
def test_stale_or_impossible_m7_time_is_withheld(
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    preview = harness.what_if.preview
    assert preview is not None
    if case == "stale":
        tampered = harness.what_if.model_copy(
            update={"evaluated_at": demo_now() - timedelta(seconds=31)}
        )
    elif case == "future":
        tampered = harness.what_if.model_copy(
            update={"evaluated_at": demo_now() + timedelta(seconds=1)}
        )
    elif case == "backward":
        tampered = harness.what_if.model_copy(
            update={
                "preview": preview.model_copy(
                    update={
                        "evidence_evaluated_at": (
                            preview.broker_previewed_at + timedelta(microseconds=1)
                        )
                    }
                )
            }
        )
    else:
        risk = harness.what_if.risk_refresh
        assert risk is not None
        refresh = risk.candidate_refresh
        assert refresh is not None
        tampered = harness.what_if.model_copy(
            update={
                "risk_refresh": risk.model_copy(
                    update={
                        "candidate_refresh": refresh.model_copy(
                            update={"evaluated_at": demo_now() - timedelta(seconds=31)}
                        )
                    }
                )
            }
        )
    calls: list[ShortPutDemoWhatIfRequest] = []

    def forged_preview(
        _service: ShortPutDemoWhatIfService,
        request: ShortPutDemoWhatIfRequest,
    ) -> ShortPutDemoWhatIfResult:
        calls.append(request)
        return tampered

    monkeypatch.setattr(ShortPutDemoWhatIfService, "preview", forged_preview)
    try:
        result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(calls) == 1
        _assert_no_order_methods(harness)
    finally:
        harness.close()


def test_refresh_failure_is_sanitized_and_never_calls_order_methods(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _approval_harness(tmp_path)
    calls: list[ShortPutDemoWhatIfRequest] = []

    def failed_preview(
        _service: ShortPutDemoWhatIfService,
        request: ShortPutDemoWhatIfRequest,
    ) -> ShortPutDemoWhatIfResult:
        calls.append(request)
        raise BrokerDataError(f"{DEMO_ACCOUNT_ID} sensitive broker text")

    monkeypatch.setattr(ShortPutDemoWhatIfService, "preview", failed_preview)
    logger = logging.getLogger("chronos.services.short_put_demo_approval")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _service(harness).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.reasons == ("Fresh DEMO what-if evidence could not be obtained safely.",)
        assert DEMO_ACCOUNT_ID not in caplog.text
        assert "sensitive broker text" not in caplog.text
        assert len(calls) == 1
        _assert_no_order_methods(harness)
    finally:
        logger.removeHandler(caplog.handler)
        harness.close()


def test_invalid_approval_reference_withholds_after_one_refresh(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    try:
        result = _service(
            harness,
            reference_factory=lambda: "CHR-APR-not-canonical",
        ).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(harness.risk.calls) == 1
        assert len(harness.base.broker.preview_calls) == 1
        _assert_no_order_methods(harness)
    finally:
        harness.close()


@pytest.mark.parametrize("mutation", ["broker", "settings"])
def test_reference_factory_boundary_mutation_is_rechecked_before_success(
    tmp_path: Path,
    mutation: str,
) -> None:
    harness = _approval_harness(tmp_path)
    original_broker = harness.base.connection.broker
    original_allocation = harness.base.settings.max_symbol_allocation_pct

    def mutate_boundary() -> str:
        if mutation == "broker":
            harness.base.connection.broker = DemoBroker(
                clock=demo_now,
                profile=DemoProfile.SAFETY_CASES,
            )
        else:
            # Frozen Settings (ADR-0009): deliberate mid-flight tamper simulation.
            object.__setattr__(harness.base.settings, "max_symbol_allocation_pct", Decimal("0.01"))
        return APPROVAL_REFERENCE

    try:
        result = _service(harness, reference_factory=mutate_boundary).rehearse(_request(harness))

        assert result.status is DemoApprovalStatus.WITHHELD
        assert result.receipt is None
        assert len(harness.risk.calls) == 1
        assert len(harness.base.broker.preview_calls) == 1
        _assert_no_order_methods(harness)
    finally:
        harness.base.connection.broker = original_broker
        object.__setattr__(harness.base.settings, "max_symbol_allocation_pct", original_allocation)
        harness.close()


def test_receipt_model_rejects_call_or_unverified_deliverable(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    try:
        result = _service(harness).rehearse(_request(harness))
        receipt = result.receipt
        assert receipt is not None

        for updates in (
            {"right": OptionRight.CALL},
            {"deliverable_verified": False, "deliverable_shares": None},
        ):
            corrupted = receipt.model_copy(
                update={"selected_contract": receipt.selected_contract.model_copy(update=updates)}
            )
            with pytest.raises(ValidationError):
                ShortPutDemoApprovalReceipt.model_validate(corrupted.model_dump(mode="python"))
    finally:
        harness.close()


def test_result_model_rejects_directly_constructed_mismatched_success(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    try:
        result = _service(harness).rehearse(_request(harness))
        corrupted_request = result.request.model_copy(update={"acknowledged_quantity": 2})

        with pytest.raises(ValidationError):
            ShortPutDemoApprovalResult(
                request=corrupted_request,
                status=DemoApprovalStatus.APPROVAL_REHEARSED,
                evaluated_at=result.evaluated_at,
                receipt=result.receipt,
            )
    finally:
        harness.close()


def test_result_schema_rejects_parent_lineage(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    try:
        result = _service(harness).rehearse(_request(harness))
        assert result.status is DemoApprovalStatus.APPROVAL_REHEARSED
        assert result.receipt is not None

        with pytest.raises(ValidationError):
            ShortPutDemoApprovalResult.model_validate(
                {
                    **result.model_dump(mode="python"),
                    "what_if_refresh": harness.what_if,
                }
            )
    finally:
        harness.close()


def test_contract_summary_omits_broker_descriptive_text(tmp_path: Path) -> None:
    harness = _approval_harness(tmp_path)
    preview = harness.what_if.preview
    assert preview is not None
    raw_contract = preview.selected_contract.model_copy(
        update={"local_symbol": f"{DEMO_ACCOUNT_ID} RAW BROKER TEXT"}
    )
    try:
        summary = ShortPutDemoApprovalContract.from_option_contract(raw_contract)

        serialized = summary.model_dump_json()
        assert DEMO_ACCOUNT_ID not in serialized
        assert "RAW BROKER TEXT" not in serialized
        assert "local_symbol" not in serialized
        assert "exchange" not in serialized
        assert "trading_class" not in serialized
    finally:
        harness.close()

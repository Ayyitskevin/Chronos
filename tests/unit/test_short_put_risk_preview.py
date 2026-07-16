from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.broker.base import BrokerDataError
from chronos.domain.enums import (
    DataQuality,
    DisplayEnvironment,
    EligibilityStatus,
    OptionRight,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    UnderlyingContract,
)
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationOrderView,
    ReconciliationPositionView,
    ReconciliationResult,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.services.short_put_risk_preview import (
    RiskPreviewStatus,
    RiskScenarioLabel,
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewService,
)
from chronos.strategy.capital import CapitalCheck
from chronos.strategy.strike_resolver import (
    RankedCandidate,
    ResolverPolicy,
    StrikeResolution,
)
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.logging import mask_account_id

NOW = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 6)
ACCOUNT_ID = "DU1234567"
ACCOUNT_FINGERPRINT = account_fingerprint(ACCOUNT_ID)


class _Evaluator:
    def __init__(
        self,
        result: ShortPutCandidateEvaluation | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.result = result or _refresh()
        self.failure = failure
        self.calls: list[str] = []

    def evaluate(self, symbol: str) -> ShortPutCandidateEvaluation:
        self.calls.append(symbol)
        if self.failure is not None:
            raise self.failure
        return self.result


def _policy() -> ResolverPolicy:
    return ResolverPolicy(
        target_abs_delta=Decimal("0.30"),
        min_abs_delta=Decimal("0.20"),
        max_abs_delta=Decimal("0.35"),
        min_dte=7,
        target_dte=21,
        max_dte=45,
        max_expirations=6,
        min_option_volume=10,
        min_open_interest=100,
        max_relative_spread=Decimal("0.20"),
        max_quote_age_seconds=Decimal("5"),
        max_contracts_per_order=1,
        delta_weight=Decimal("0.45"),
        spread_weight=Decimal("0.30"),
        dte_weight=Decimal("0.15"),
        liquidity_weight=Decimal("0.10"),
    )


def _contract(*, con_id: int = 2002, strike: str = "180") -> OptionContract:
    strike_value = Decimal(strike)
    return OptionContract(
        con_id=con_id,
        underlying_con_id=1001,
        symbol="AAPL",
        expiration=EXPIRATION,
        strike=strike_value,
        right=OptionRight.PUT,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol=f"AAPL 260806P{int(strike_value * 1000):08d}",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )


def _candidate(
    *,
    con_id: int = 2002,
    strike: str = "180",
    bid: str = "2.00",
) -> RankedCandidate:
    strike_value = Decimal(strike)
    obligation = strike_value * Decimal("100")
    return RankedCandidate(
        contract=_contract(con_id=con_id, strike=strike),
        symbol="AAPL",
        expiration=EXPIRATION,
        dte=21,
        strike=strike_value,
        right=OptionRight.PUT,
        bid=Decimal(bid),
        ask=Decimal("2.10"),
        midpoint=Decimal("2.05"),
        last=Decimal("2.05"),
        delta=Decimal("-0.30"),
        gamma=Decimal("0.02"),
        theta=Decimal("-0.08"),
        implied_volatility=Decimal("0.24"),
        volume=500,
        open_interest=2_000,
        relative_spread=Decimal("0.05"),
        score=Decimal("0.10"),
        data_age_seconds=Decimal("1.25"),
        data_quality=DataQuality.DEMO,
        capital_check=CapitalCheck(
            passed=True,
            proposal_amount=obligation,
            resulting_symbol_allocation=obligation,
            resulting_total_wheel_allocation=obligation,
            resulting_symbol_allocation_pct=obligation / Decimal("250000"),
            resulting_total_wheel_allocation_pct=obligation / Decimal("250000"),
            shares_required=Decimal("100"),
            reasons=(),
        ),
        selection_rationale="Fresh eligible short-put evidence.",
    )


def _reconciliation() -> ReconciliationResult:
    return ReconciliationResult(
        status=ReconciliationStatus.RECONCILED,
        snapshot=ReconciliationSnapshotView(
            environment=DisplayEnvironment.DEMO,
            data_quality=DataQuality.DEMO,
            account=ReconciliationAccountView(
                masked_account_id=mask_account_id(ACCOUNT_ID),
                net_liquidation=Decimal("250000"),
                total_cash=Decimal("125000"),
                buying_power=Decimal("240000"),
                currency="USD",
                as_of=NOW,
            ),
            positions=(),
            open_orders=(),
            execution_count=0,
            server_time_start=NOW,
            server_time_end=NOW,
            captured_at=NOW,
            window_seconds=0,
            server_window_seconds=0,
        ),
        symbols=(
            SymbolReconciliation(
                symbol="AAPL",
                status=ReconciliationStatus.RECONCILED,
                stage=WheelStage.FLAT,
                stock_shares=Decimal("0"),
                unencumbered_shares=Decimal("0"),
                short_put_contracts=Decimal("0"),
                short_call_contracts=Decimal("0"),
                pending_put_contracts=Decimal("0"),
                pending_call_contracts=Decimal("0"),
                manual_review_required=False,
                reasons=(),
            ),
        ),
        reasons=(),
    )


def _underlying_quote(*, last: str = "190.25") -> MarketQuote:
    return MarketQuote(
        contract=UnderlyingContract(
            con_id=1001,
            symbol="AAPL",
            exchange="SMART",
            primary_exchange="NASDAQ",
            currency="USD",
        ),
        timestamp=NOW - timedelta(seconds=1),
        data_quality=DataQuality.DEMO,
        bid=Decimal("190.20"),
        ask=Decimal("190.30"),
        last=Decimal(last),
        volume=1_000_000,
    )


def _chain() -> OptionChainParameters:
    return OptionChainParameters(
        exchange="SMART",
        underlying_con_id=1001,
        trading_class="AAPL",
        multiplier=Decimal("100"),
        expirations=(EXPIRATION,),
        strikes=(Decimal("175"), Decimal("180")),
    )


def _resolution(
    candidates: tuple[RankedCandidate, ...] | None = None,
) -> StrikeResolution:
    return StrikeResolution(
        policy=_policy(),
        status=EligibilityStatus.ELIGIBLE,
        selected_expirations=(EXPIRATION,),
        candidates=candidates
        or (
            _candidate(con_id=2001, strike="175"),
            _candidate(),
        ),
        rejected=(),
    )


def _refresh(
    *,
    candidates: tuple[RankedCandidate, ...] | None = None,
) -> ShortPutCandidateEvaluation:
    return ShortPutCandidateEvaluation(
        symbol="AAPL",
        status=EligibilityStatus.ELIGIBLE,
        reconciliation=_reconciliation(),
        evaluated_at=NOW,
        underlying_quote=_underlying_quote(),
        chain=_chain(),
        resolution=_resolution(candidates),
    )


def _shift_refresh(
    refresh: ShortPutCandidateEvaluation,
    *,
    delta: timedelta,
) -> ShortPutCandidateEvaluation:
    assert refresh.underlying_quote is not None
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    shifted_snapshot = snapshot.model_copy(
        update={
            "account": snapshot.account.model_copy(
                update={"as_of": snapshot.account.as_of + delta}
            ),
            "server_time_start": (
                snapshot.server_time_start + delta
                if snapshot.server_time_start is not None
                else None
            ),
            "server_time_end": (
                snapshot.server_time_end + delta if snapshot.server_time_end is not None else None
            ),
            "captured_at": snapshot.captured_at + delta,
        }
    )
    return refresh.model_copy(
        update={
            "evaluated_at": refresh.evaluated_at + delta,
            "underlying_quote": refresh.underlying_quote.model_copy(
                update={"timestamp": refresh.underlying_quote.timestamp + delta}
            ),
            "reconciliation": refresh.reconciliation.model_copy(
                update={"snapshot": shifted_snapshot}
            ),
        }
    )


def _request(*, commission: str = "5.00", contract_id: int = 2002) -> ShortPutRiskPreviewRequest:
    return ShortPutRiskPreviewRequest(
        symbol=" aapl ",
        selected_contract_id=contract_id,
        total_commission_estimate=Decimal(commission),
    )


def _service(evaluator: _Evaluator) -> ShortPutRiskPreviewService:
    return ShortPutRiskPreviewService(evaluator, clock=lambda: NOW)


def test_ready_preview_uses_exact_fresh_identity_and_decimal_payoff_grid() -> None:
    evaluator = _Evaluator()
    request = _request()

    result = _service(evaluator).preview(request)

    assert evaluator.calls == ["AAPL"]
    assert result.status is RiskPreviewStatus.READY
    assert result.request == request
    assert result.candidate_refresh == evaluator.result
    assert result.reasons == ()
    assert result.opening_actions_locked is True
    preview = result.preview
    assert preview is not None
    assert preview.symbol == "AAPL"
    assert evaluator.result.resolution is not None
    assert preview.selected_contract == evaluator.result.resolution.candidates[1].contract
    assert preview.selected_contract.con_id == 2002
    assert preview.candidate_rank == 2
    assert preview.contracts == 1
    assert preview.evidence_evaluated_at == NOW
    assert preview.underlying_observed_at == NOW - timedelta(seconds=1)
    assert preview.option_data_quality is DataQuality.DEMO
    assert preview.option_quote_age_seconds == Decimal("1.25")
    assert preview.currency == "USD"
    assert preview.observed_spot == Decimal("190.25")
    assert preview.hypothetical_credit_per_share == Decimal("2.00")
    assert preview.hypothetical_credit_source == "FRESH_BID"
    assert preview.total_commission_estimate == Decimal("5.00")
    assert preview.commission_estimate_source == "OPERATOR_ESTIMATE"
    assert preview.fees_excluded is False
    assert preview.gross_premium == Decimal("200")
    assert preview.net_premium == Decimal("195")
    assert preview.assigned_shares == Decimal("100")
    assert preview.gross_assignment_obligation == Decimal("18000")
    assert preview.net_cash_deployed_if_assigned == Decimal("17805")
    assert preview.effective_entry_price == Decimal("178.05")
    assert preview.cash_secured_notional_pct == Decimal("0.072")
    assert preview.broker_margin_impact is None
    assert preview.opening_actions_locked is True
    assert [point.labels for point in preview.scenario_points] == [
        (RiskScenarioLabel.OBSERVED_SPOT,),
        (RiskScenarioLabel.STRIKE,),
        (RiskScenarioLabel.EFFECTIVE_ENTRY,),
        (RiskScenarioLabel.UNDERLYING_ZERO,),
    ]
    assert [point.underlying_price for point in preview.scenario_points] == [
        Decimal("190.25"),
        Decimal("180"),
        Decimal("178.05"),
        Decimal("0"),
    ]
    assert [point.put_intrinsic_value_per_share for point in preview.scenario_points] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("1.95"),
        Decimal("180"),
    ]
    assert [point.expiration_pnl for point in preview.scenario_points] == [
        Decimal("195"),
        Decimal("195"),
        Decimal("0"),
        Decimal("-17805"),
    ]


def test_explicit_zero_commission_is_preserved_and_labeled_as_fees_excluded() -> None:
    result = _service(_Evaluator()).preview(_request(commission="0"))

    assert result.status is RiskPreviewStatus.READY
    assert result.preview is not None
    assert result.preview.total_commission_estimate == Decimal("0")
    assert result.preview.fees_excluded is True
    assert result.preview.gross_premium == Decimal("200")
    assert result.preview.net_premium == Decimal("200")
    assert result.preview.net_cash_deployed_if_assigned == Decimal("17800")
    assert result.preview.effective_entry_price == Decimal("178")


@pytest.mark.parametrize(
    "values",
    [
        {"symbol": ""},
        {"symbol": "AAPL!"},
        {"selected_contract_id": 0},
        {"selected_contract_id": -1},
        {"selected_contract_id": True},
        {"selected_contract_id": "2002"},
        {"total_commission_estimate": Decimal("-0.01")},
        {"total_commission_estimate": Decimal("NaN")},
        {"total_commission_estimate": Decimal("Infinity")},
    ],
)
def test_request_rejects_invalid_untrusted_selection_values(values: dict[str, object]) -> None:
    defaults: dict[str, object] = {
        "symbol": "AAPL",
        "selected_contract_id": 2002,
        "total_commission_estimate": Decimal("0"),
    }

    with pytest.raises(ValidationError):
        ShortPutRiskPreviewRequest(**(defaults | values))


@pytest.mark.parametrize(
    "commission",
    [
        "10000.0001",
        "10001",
        "0.00001",
        "1e-999999999999999999",
        "0e999999999999999999",
        "1.23000000000000000000",
        "1.23456",
    ],
)
def test_request_rejects_excessive_or_overprecise_commission(commission: str) -> None:
    with pytest.raises(ValidationError):
        _request(commission=commission)


@pytest.mark.parametrize(
    "commission",
    [
        "10000",
        "10000.0000",
        "1.2345",
        "1.23000",
        "0.0000",
    ],
)
def test_request_accepts_bounded_commission_with_at_most_four_normalized_places(
    commission: str,
) -> None:
    request = _request(commission=commission)

    assert request.total_commission_estimate == Decimal(commission)


def test_preview_revalidates_model_copy_corrupted_request_before_refresh() -> None:
    evaluator = _Evaluator()
    corrupted = _request().model_copy(update={"total_commission_estimate": Decimal("10000.0001")})

    with pytest.raises(ValidationError):
        _service(evaluator).preview(corrupted)

    assert evaluator.calls == []


def test_option_contract_cannot_masquerade_as_underlying_quote() -> None:
    refresh = _refresh()
    assert refresh.underlying_quote is not None
    replacement = refresh.underlying_quote.model_copy(
        update={"contract": _contract(con_id=1001, strike="180")}
    )
    corrupted = refresh.model_copy(update={"underlying_quote": replacement})

    result = _service(_Evaluator(corrupted)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "exact requested stock contract" in result.reasons[-1]


def test_infinite_account_cash_cannot_prove_cash_security() -> None:
    refresh = _refresh()
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    account = snapshot.account.model_copy(update={"total_cash": Decimal("Infinity")})
    reconciliation = refresh.reconciliation.model_copy(
        update={"snapshot": snapshot.model_copy(update={"account": account})}
    )
    corrupted = refresh.model_copy(update={"reconciliation": reconciliation})

    result = _service(_Evaluator(corrupted)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "non-finite numeric values" in result.reasons[-1]


def test_returned_freshness_policy_cannot_exceed_hard_risk_ceiling() -> None:
    refresh = _shift_refresh(_refresh(), delta=timedelta(minutes=-1))
    assert refresh.resolution is not None
    inflated_policy = refresh.resolution.policy.model_copy(
        update={"max_quote_age_seconds": Decimal("100000")}
    )
    resolution = refresh.resolution.model_copy(update={"policy": inflated_policy})
    corrupted = refresh.model_copy(update={"resolution": resolution})

    result = _service(_Evaluator(corrupted)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "outside the risk-preview window" in result.reasons[-1]


def test_option_age_at_preview_includes_elapsed_time_since_candidate_evaluation() -> None:
    refresh = _shift_refresh(_refresh(), delta=timedelta(seconds=-20))
    assert refresh.resolution is not None
    candidates = tuple(
        candidate.model_copy(update={"data_age_seconds": Decimal("20")})
        for candidate in refresh.resolution.candidates
    )
    policy = refresh.resolution.policy.model_copy(update={"max_quote_age_seconds": Decimal("30")})
    resolution = refresh.resolution.model_copy(update={"candidates": candidates, "policy": policy})
    corrupted = refresh.model_copy(update={"resolution": resolution})

    result = _service(_Evaluator(corrupted)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "too old at the risk-preview decision point" in result.reasons[-1]


def test_each_preview_attempt_performs_a_new_candidate_evaluation() -> None:
    evaluator = _Evaluator()
    service = _service(evaluator)
    request = _request()

    first = service.preview(request)
    second = service.preview(request)

    assert first.status is RiskPreviewStatus.READY
    assert second.status is RiskPreviewStatus.READY
    assert evaluator.calls == ["AAPL", "AAPL"]


def test_service_clock_rejects_an_internally_coherent_day_old_refresh() -> None:
    refresh = _refresh()
    assert refresh.underlying_quote is not None
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    assert snapshot.server_time_start is not None
    assert snapshot.server_time_end is not None
    age = timedelta(days=1)
    stale_snapshot = snapshot.model_copy(
        update={
            "account": snapshot.account.model_copy(update={"as_of": snapshot.account.as_of - age}),
            "server_time_start": snapshot.server_time_start - age,
            "server_time_end": snapshot.server_time_end - age,
            "captured_at": snapshot.captured_at - age,
        }
    )
    stale_refresh = refresh.model_copy(
        update={
            "evaluated_at": refresh.evaluated_at - age,
            "underlying_quote": refresh.underlying_quote.model_copy(
                update={"timestamp": refresh.underlying_quote.timestamp - age}
            ),
            "reconciliation": refresh.reconciliation.model_copy(
                update={"snapshot": stale_snapshot}
            ),
        }
    )

    result = ShortPutRiskPreviewService(
        _Evaluator(stale_refresh),
        clock=lambda: NOW,
    ).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert result.opening_actions_locked is True


def test_underlying_timestamp_older_than_freshness_policy_is_withheld() -> None:
    refresh = _refresh()
    assert refresh.underlying_quote is not None
    stale_refresh = refresh.model_copy(
        update={
            "underlying_quote": refresh.underlying_quote.model_copy(
                update={"timestamp": NOW - timedelta(hours=1)}
            )
        }
    )

    result = ShortPutRiskPreviewService(
        _Evaluator(stale_refresh),
        clock=lambda: NOW,
    ).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


@pytest.mark.parametrize("timestamp", ["snapshot_captured_at", "account_as_of"])
def test_stale_reconciliation_or_account_timestamp_is_withheld(timestamp: str) -> None:
    refresh = _refresh()
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    if timestamp == "snapshot_captured_at":
        stale_snapshot = snapshot.model_copy(update={"captured_at": NOW - timedelta(hours=1)})
    else:
        stale_snapshot = snapshot.model_copy(
            update={
                "account": snapshot.account.model_copy(update={"as_of": NOW - timedelta(hours=1)})
            }
        )
    stale_refresh = refresh.model_copy(
        update={
            "reconciliation": refresh.reconciliation.model_copy(update={"snapshot": stale_snapshot})
        }
    )

    result = ShortPutRiskPreviewService(
        _Evaluator(stale_refresh),
        clock=lambda: NOW,
    ).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


@pytest.mark.parametrize("case", ["disappeared", "duplicate", "ineligible"])
def test_disappeared_duplicate_or_ineligible_selection_is_withheld(case: str) -> None:
    request = _request()
    if case == "disappeared":
        request = _request(contract_id=9999)
        refresh = _refresh()
        expected_reason = "The selected contract is no longer uniquely eligible in fresh evidence."
    elif case == "duplicate":
        selected = _candidate()
        refresh = _refresh(candidates=(selected, selected.model_copy()))
        expected_reason = "The selected contract is no longer uniquely eligible in fresh evidence."
    else:
        refresh = _refresh().model_copy(
            update={"status": EligibilityStatus.NO_TRADE, "resolution": None}
        )
        expected_reason = "Fresh candidate evaluation did not produce an eligible selection."

    result = _service(_Evaluator(refresh)).preview(request)

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert result.candidate_refresh == refresh
    assert expected_reason in result.reasons
    assert result.opening_actions_locked is True


@pytest.mark.parametrize("case", ["underlying", "reconciliation", "snapshot"])
def test_missing_quote_or_account_snapshot_is_withheld(case: str) -> None:
    refresh = _refresh()
    if case == "underlying":
        refresh = refresh.model_copy(update={"underlying_quote": None})
        expected = "Fresh candidate evidence omitted its underlying or reconciliation snapshot."
    elif case == "reconciliation":
        refresh = refresh.model_copy(update={"reconciliation": None})
        expected = "Fresh candidate evidence omitted its underlying or reconciliation snapshot."
    else:
        assert refresh.reconciliation is not None
        refresh = refresh.model_copy(
            update={"reconciliation": refresh.reconciliation.model_copy(update={"snapshot": None})}
        )
        expected = "Fresh candidate evidence omitted its reconciled account snapshot."

    result = _service(_Evaluator(refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert expected in result.reasons


def _invalid_candidate(case: str) -> RankedCandidate:
    candidate = _candidate()
    contract = candidate.contract
    capital = candidate.capital_check
    if case == "candidate_symbol":
        return candidate.model_copy(update={"symbol": "MSFT"})
    if case == "contract_symbol":
        return candidate.model_copy(
            update={"contract": contract.model_copy(update={"symbol": "MSFT"})}
        )
    if case == "candidate_right":
        return candidate.model_copy(update={"right": OptionRight.CALL})
    if case == "contract_right":
        return candidate.model_copy(
            update={"contract": contract.model_copy(update={"right": OptionRight.CALL})}
        )
    if case == "rejected":
        return candidate.model_copy(update={"eligibility_status": EligibilityStatus.REJECTED})
    if case == "capital_failed":
        return candidate.model_copy(
            update={"capital_check": capital.model_copy(update={"passed": False})}
        )
    if case == "unverified_deliverable":
        return candidate.model_copy(
            update={
                "contract": contract.model_copy(
                    update={"deliverable_verified": False, "deliverable_shares": None}
                )
            }
        )
    if case == "nonstandard_deliverable":
        return candidate.model_copy(
            update={"contract": contract.model_copy(update={"deliverable_shares": Decimal("50")})}
        )
    if case == "zero_bid":
        return candidate.model_copy(update={"bid": Decimal("0")})
    if case == "delayed":
        return candidate.model_copy(update={"data_quality": DataQuality.DELAYED})
    if case == "stale":
        return candidate.model_copy(update={"data_age_seconds": Decimal("5.01")})
    if case == "strike_mismatch":
        return candidate.model_copy(update={"strike": Decimal("179")})
    if case == "expiration_mismatch":
        return candidate.model_copy(update={"expiration": EXPIRATION + timedelta(days=1)})
    raise AssertionError(f"unknown invalid-candidate case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "candidate_symbol",
        "contract_symbol",
        "candidate_right",
        "contract_right",
        "rejected",
        "capital_failed",
        "unverified_deliverable",
        "nonstandard_deliverable",
        "zero_bid",
        "delayed",
        "stale",
        "strike_mismatch",
        "expiration_mismatch",
    ],
)
def test_model_copy_corruption_cannot_bypass_candidate_evidence_checks(case: str) -> None:
    invalid = _invalid_candidate(case)
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), invalid))

    result = _service(_Evaluator(refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "The selected candidate failed the fresh risk-preview evidence checks." in result.reasons


@pytest.mark.parametrize("field", ["shares_required", "proposal_amount"])
def test_internally_inconsistent_capital_evidence_is_withheld(field: str) -> None:
    candidate = _candidate()
    capital = candidate.capital_check.model_copy(update={field: Decimal("99")})
    invalid = candidate.model_copy(update={"capital_check": capital})
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), invalid))

    result = _service(_Evaluator(refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert "The selected candidate's capital evidence is internally inconsistent." in result.reasons


def test_cash_below_gross_assignment_obligation_is_withheld() -> None:
    refresh = _refresh()
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    low_cash_snapshot = snapshot.model_copy(
        update={"account": snapshot.account.model_copy(update={"total_cash": Decimal("17999.99")})}
    )
    invalid_refresh = refresh.model_copy(
        update={
            "reconciliation": refresh.reconciliation.model_copy(
                update={"snapshot": low_cash_snapshot}
            )
        }
    )

    result = _service(_Evaluator(invalid_refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resulting_symbol_allocation", Decimal("18001")),
        ("resulting_total_wheel_allocation", Decimal("18001")),
        ("resulting_symbol_allocation_pct", Decimal("0.0721")),
        ("resulting_total_wheel_allocation_pct", Decimal("0.0721")),
        ("reasons", ("Capital check reported a conflicting reason.",)),
    ],
)
def test_passed_capital_check_with_inconsistent_derived_evidence_is_withheld(
    field: str,
    value: object,
) -> None:
    candidate = _candidate()
    capital = candidate.capital_check.model_copy(update={field: value})
    invalid = candidate.model_copy(update={"capital_check": capital})
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), invalid))

    result = _service(_Evaluator(refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


def _portfolio_position() -> ReconciliationPositionView:
    return ReconciliationPositionView(
        contract=UnderlyingContract(
            con_id=9001,
            symbol="MSFT",
            exchange="SMART",
            primary_exchange="NASDAQ",
            currency="USD",
        ),
        quantity=Decimal("1"),
        average_cost=Decimal("450"),
        market_price=Decimal("455"),
        unrealized_pnl=Decimal("5"),
    )


def _portfolio_order() -> ReconciliationOrderView:
    return ReconciliationOrderView(
        broker_order_id=501,
        permanent_id=601,
        client_id=7,
        contract=UnderlyingContract(
            con_id=9001,
            symbol="MSFT",
            exchange="SMART",
            primary_exchange="NASDAQ",
            currency="USD",
        ),
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        limit_price=Decimal("450"),
        lifecycle=OrderLifecycle.SUBMITTED,
        transmit=True,
        outside_rth=False,
    )


@pytest.mark.parametrize("portfolio_evidence", ["positions", "orders", "executions"])
def test_nonempty_whole_account_exposure_evidence_is_withheld(
    portfolio_evidence: str,
) -> None:
    refresh = _refresh()
    assert refresh.reconciliation is not None
    assert refresh.reconciliation.snapshot is not None
    snapshot = refresh.reconciliation.snapshot
    if portfolio_evidence == "positions":
        update: dict[str, object] = {"positions": (_portfolio_position(),)}
    elif portfolio_evidence == "orders":
        update = {"open_orders": (_portfolio_order(),)}
    else:
        update = {"execution_count": 1}
    exposed_snapshot = snapshot.model_copy(update=update)
    exposed_refresh = refresh.model_copy(
        update={
            "reconciliation": refresh.reconciliation.model_copy(
                update={"snapshot": exposed_snapshot}
            )
        }
    )

    result = _service(_Evaluator(exposed_refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


@pytest.mark.parametrize(
    "symbol_evidence",
    ["duplicate", "not_flat", "nonzero", "manual_review"],
)
def test_target_symbol_must_be_unique_flat_zero_and_without_manual_review(
    symbol_evidence: str,
) -> None:
    refresh = _refresh()
    assert refresh.reconciliation is not None
    target = refresh.reconciliation.symbols[0]
    symbols: tuple[SymbolReconciliation, ...]
    if symbol_evidence == "duplicate":
        symbols = (target, target.model_copy())
    elif symbol_evidence == "not_flat":
        symbols = (target.model_copy(update={"stage": WheelStage.LONG_STOCK}),)
    elif symbol_evidence == "nonzero":
        symbols = (target.model_copy(update={"stock_shares": Decimal("1")}),)
    else:
        symbols = (target.model_copy(update={"manual_review_required": True}),)
    invalid_refresh = refresh.model_copy(
        update={"reconciliation": refresh.reconciliation.model_copy(update={"symbols": symbols})}
    )

    result = _service(_Evaluator(invalid_refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


@pytest.mark.parametrize("identity_field", ["exchange", "trading_class"])
def test_contract_and_chain_market_identity_mismatch_is_withheld(
    identity_field: str,
) -> None:
    candidate = _candidate()
    contract_update = {identity_field: "CBOE" if identity_field == "exchange" else "AAPL1"}
    mismatched = candidate.model_copy(
        update={
            "contract": candidate.contract.model_copy(update=contract_update),
        }
    )
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), mismatched))

    result = _service(_Evaluator(refresh)).preview(_request())

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None


def test_evidence_withheld_outcome_emits_a_sanitized_log_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_detail = f"{ACCOUNT_ID} {ACCOUNT_FINGERPRINT} must never reach logs"
    candidate = _candidate().model_copy(
        update={
            "data_age_seconds": Decimal("5.01"),
            "selection_rationale": secret_detail,
        }
    )
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), candidate))

    logger = logging.getLogger("chronos.services.short_put_risk_preview")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            result = _service(_Evaluator(refresh)).preview(_request())
    finally:
        logger.removeHandler(caplog.handler)

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    record = next(
        item
        for item in caplog.records
        if getattr(item, "event", None) == "short_put_risk_preview_withheld"
    )
    assert record.symbol == "AAPL"  # type: ignore[attr-defined]
    assert ACCOUNT_ID not in caplog.text
    assert ACCOUNT_FINGERPRINT not in caplog.text


def test_model_construct_corruption_is_safely_withheld_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = _candidate()
    values = {name: getattr(candidate, name) for name in RankedCandidate.model_fields}
    values["data_age_seconds"] = Decimal("-1")
    malformed = RankedCandidate.model_construct(**values)
    refresh = _refresh(candidates=(_candidate(con_id=2001, strike="175"), malformed))

    logger = logging.getLogger("chronos.services.short_put_risk_preview")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            result = _service(_Evaluator(refresh)).preview(_request())
    finally:
        logger.removeHandler(caplog.handler)

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.preview is None
    assert result.reasons == (
        "The selected candidate failed the fresh risk-preview evidence checks.",
    )
    record = next(
        item
        for item in caplog.records
        if getattr(item, "event", None) == "short_put_risk_preview_withheld"
    )
    assert record.symbol == "AAPL"  # type: ignore[attr-defined]


def test_evaluator_failure_is_sanitized_with_safe_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_detail = f"{ACCOUNT_ID} {ACCOUNT_FINGERPRINT} unavailable"
    evaluator = _Evaluator(failure=BrokerDataError(secret_detail))
    eastern = timezone(timedelta(hours=-4))
    service = ShortPutRiskPreviewService(
        evaluator,
        clock=lambda: datetime(2026, 7, 16, 11, 30, tzinfo=eastern),
    )

    logger = logging.getLogger("chronos.services.short_put_risk_preview")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = service.preview(_request())
    finally:
        logger.removeHandler(caplog.handler)

    assert result.status is RiskPreviewStatus.WITHHELD
    assert result.evaluated_at == NOW
    assert result.candidate_refresh is None
    assert result.preview is None
    assert result.reasons == ("Fresh candidate evidence could not be obtained safely.",)
    record = next(
        item
        for item in caplog.records
        if getattr(item, "event", None) == "short_put_risk_preview_refresh_failed"
    )
    assert record.error_type == "BrokerDataError"  # type: ignore[attr-defined]
    assert ACCOUNT_ID not in caplog.text
    assert ACCOUNT_FINGERPRINT not in caplog.text
    serialized = result.model_dump_json()
    assert ACCOUNT_ID not in serialized
    assert ACCOUNT_FINGERPRINT not in serialized


def test_naive_service_clock_is_rejected() -> None:
    service = ShortPutRiskPreviewService(
        _Evaluator(failure=BrokerDataError("offline")),
        clock=lambda: NOW.replace(tzinfo=None),
    )

    with pytest.raises(ValueError, match="clock must return an aware datetime"):
        service.preview(_request())


def test_ready_serialization_excludes_raw_account_identity_and_fingerprint() -> None:
    result = _service(_Evaluator()).preview(_request())

    assert result.status is RiskPreviewStatus.READY
    serialized = result.model_dump_json()
    assert ACCOUNT_ID not in serialized
    assert ACCOUNT_FINGERPRINT not in serialized
    assert mask_account_id(ACCOUNT_ID) in serialized

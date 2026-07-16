from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from chronos.config.settings import Settings
from chronos.domain.enums import (
    DataQuality,
    EligibilityStatus,
    OptionRight,
    ReconciliationStatus,
)
from chronos.domain.models import (
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    StockAvailability,
    UnderlyingContract,
)
from chronos.strategy.basis import BasisProjection
from chronos.strategy.capital import CapitalContext
from chronos.strategy.strike_resolver import (
    CandidateRejectionCode,
    ResolverContext,
    ResolverPolicy,
    StrikeResolver,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
UNDERLYING = UnderlyingContract(con_id=1, symbol="AAPL", primary_exchange="NASDAQ")
ACCOUNT_FINGERPRINT = account_fingerprint("DU1234567")
OTHER_ACCOUNT_FINGERPRINT = account_fingerprint("DU7654321")
WHEEL_CYCLE_ID = "wheel-aapl-1"


def _policy(**changes: object) -> ResolverPolicy:
    values: dict[str, object] = {
        "target_abs_delta": Decimal("0.30"),
        "min_abs_delta": Decimal("0.20"),
        "max_abs_delta": Decimal("0.35"),
        "min_dte": 7,
        "target_dte": 21,
        "max_dte": 45,
        "max_expirations": 6,
        "min_option_volume": 10,
        "min_open_interest": 100,
        "max_relative_spread": Decimal("0.20"),
        "max_quote_age_seconds": Decimal("5"),
        "max_contracts_per_order": 1,
        "delta_weight": Decimal("0.45"),
        "spread_weight": Decimal("0.30"),
        "dte_weight": Decimal("0.15"),
        "liquidity_weight": Decimal("0.10"),
    }
    values.update(changes)
    return ResolverPolicy(**values)


def _capital(**changes: object) -> CapitalContext:
    values: dict[str, object] = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "net_liquidation": Decimal("100000"),
        "uncommitted_cash": Decimal("100000"),
        "current_symbol_allocation": Decimal("0"),
        "current_total_wheel_allocation": Decimal("0"),
        "max_symbol_allocation_pct": Decimal("0.25"),
        "max_total_wheel_allocation_pct": Decimal("0.60"),
    }
    values.update(changes)
    return CapitalContext(**values)


def _underlying_quote(
    *,
    quality: DataQuality = DataQuality.LIVE,
    age_seconds: Decimal = Decimal("0"),
) -> MarketQuote:
    return MarketQuote(
        contract=UNDERLYING,
        timestamp=NOW - timedelta(seconds=float(age_seconds)),
        data_quality=quality,
        bid=Decimal("99.90"),
        ask=Decimal("100.10"),
        last=Decimal("100"),
    )


def _contract(
    con_id: int,
    *,
    strike: str = "95",
    right: OptionRight = OptionRight.PUT,
    dte: int = 21,
    multiplier: str = "100",
    trading_class: str = "AAPL",
    exchange: str = "SMART",
    currency: str = "USD",
    underlying_con_id: int | None = UNDERLYING.con_id,
    deliverable_verified: bool = True,
    deliverable_shares: str | None = None,
) -> OptionContract:
    return OptionContract(
        con_id=con_id,
        symbol="AAPL",
        underlying_con_id=underlying_con_id,
        expiration=(NOW + timedelta(days=dte)).date(),
        strike=Decimal(strike),
        right=right,
        multiplier=Decimal(multiplier),
        trading_class=trading_class,
        local_symbol=f"AAPL-{con_id}",
        exchange=exchange,
        currency=currency,
        deliverable_shares=(
            Decimal(deliverable_shares)
            if deliverable_shares is not None
            else Decimal(multiplier)
            if deliverable_verified
            else None
        ),
        deliverable_verified=deliverable_verified,
    )


def _basis(
    *,
    adjusted_basis: Decimal | None = Decimal("105"),
    wheel_cycle_id: str = WHEEL_CYCLE_ID,
    symbol: str = UNDERLYING.symbol,
    underlying_con_id: int = UNDERLYING.con_id,
    account: str = ACCOUNT_FINGERPRINT,
    currency: str = UNDERLYING.currency,
    eligible_shares: Decimal = Decimal("100"),
    reconciliation: ReconciliationStatus = ReconciliationStatus.RECONCILED,
) -> BasisProjection:
    broker_basis = Decimal("100")
    return BasisProjection(
        wheel_cycle_id=wheel_cycle_id,
        symbol=symbol,
        underlying_con_id=underlying_con_id,
        account_fingerprint=account,
        currency=currency,
        broker_average_cost=broker_basis,
        strategy_adjusted_basis=adjusted_basis,
        total_adjustment=(
            Decimal("0")
            if adjusted_basis is None
            else (adjusted_basis - broker_basis) * eligible_shares
        ),
        eligible_shares=eligible_shares,
        provisional_commission=False,
        reconciliation_status=reconciliation,
        reasons=(),
    )


def _stock(
    shares: Decimal = Decimal("100"),
    *,
    wheel_cycle_id: str = WHEEL_CYCLE_ID,
    symbol: str = UNDERLYING.symbol,
    underlying_con_id: int = UNDERLYING.con_id,
    account: str = ACCOUNT_FINGERPRINT,
    currency: str = UNDERLYING.currency,
) -> StockAvailability:
    return StockAvailability(
        wheel_cycle_id=wheel_cycle_id,
        symbol=symbol,
        account_fingerprint=account,
        underlying_con_id=underlying_con_id,
        currency=currency,
        shares=shares,
    )


def _option_quote(
    contract: OptionContract,
    *,
    bid: Decimal | None = Decimal("0.90"),
    ask: Decimal | None = Decimal("1.10"),
    delta: Decimal | None = Decimal("-0.30"),
    volume: int | None = 10,
    open_interest: int | None = 100,
    age_seconds: Decimal = Decimal("0"),
    quality: DataQuality = DataQuality.LIVE,
) -> MarketQuote:
    return MarketQuote(
        contract=contract,
        timestamp=NOW - timedelta(seconds=float(age_seconds)),
        data_quality=quality,
        bid=bid,
        ask=ask,
        last=Decimal("1.00"),
        volume=volume,
        open_interest=open_interest,
        greeks=(
            ModelGreeks(
                delta=delta,
                gamma=Decimal("0.02"),
                theta=Decimal("-0.08"),
                implied_volatility=Decimal("0.25"),
            )
            if delta is not None
            else None
        ),
    )


def _context(
    quotes: tuple[MarketQuote, ...],
    *,
    right: OptionRight = OptionRight.PUT,
    chain_multiplier: str = "100",
    chain_trading_class: str = "AAPL",
    capital: CapitalContext | None = None,
    strategy_basis: BasisProjection | None = None,
    stock_availability: StockAvailability | None = None,
    wheel_cycle_id: str = WHEEL_CYCLE_ID,
    conflict: bool = False,
    reconciliation: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    underlying_quote: MarketQuote | None = None,
    contracts: int = 1,
    as_of: datetime = NOW,
) -> ResolverContext:
    option_contracts = [
        quote.contract for quote in quotes if isinstance(quote.contract, OptionContract)
    ]
    expirations = tuple(sorted({contract.expiration for contract in option_contracts}))
    strikes = tuple(sorted({contract.strike for contract in option_contracts}))
    if not expirations:
        expirations = ((NOW + timedelta(days=21)).date(),)
    if not strikes:
        strikes = (Decimal("95"),)
    return ResolverContext(
        wheel_cycle_id=wheel_cycle_id,
        right=right,
        contracts=contracts,
        as_of=as_of,
        underlying_quote=underlying_quote or _underlying_quote(),
        chain=OptionChainParameters(
            exchange="SMART",
            underlying_con_id=UNDERLYING.con_id,
            trading_class=chain_trading_class,
            multiplier=Decimal(chain_multiplier),
            expirations=expirations,
            strikes=strikes,
        ),
        option_quotes=quotes,
        capital=capital or _capital(),
        strategy_basis=strategy_basis,
        stock_availability=stock_availability,
        conflicting_position_or_order=conflict,
        reconciliation_status=reconciliation,
    )


def _codes(resolution: object) -> set[CandidateRejectionCode]:
    assert hasattr(resolution, "rejected")
    return {
        reason.code
        for rejected in resolution.rejected  # type: ignore[attr-defined]
        for reason in rejected.rejection_reasons
    }


def test_absolute_delta_distance_ranks_across_expirations() -> None:
    near = _contract(10, dte=30, strike="94")
    far = _contract(11, dte=21, strike="95")
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (
                _option_quote(far, delta=Decimal("-0.34")),
                _option_quote(near, delta=Decimal("-0.29")),
            )
        )
    )

    assert resolution.status is EligibilityStatus.ELIGIBLE
    assert [candidate.contract.con_id for candidate in resolution.candidates] == [10, 11]
    assert resolution.selected is not None
    assert resolution.selected.delta == Decimal("-0.29")
    assert resolution.selected.capital_check.passed is True
    assert "abs-delta distance=" in resolution.selected.selection_rationale
    assert resolution.policy == _policy()
    assert resolution.algorithm_version == "strike-resolver-v1"


def test_exact_filter_boundaries_are_inclusive() -> None:
    contract = _contract(20, dte=7)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (
                _option_quote(
                    contract,
                    bid=Decimal("0.90"),
                    ask=Decimal("1.10"),
                    delta=Decimal("-0.20"),
                    volume=10,
                    open_interest=0,
                    age_seconds=Decimal("5"),
                    quality=DataQuality.FROZEN,
                ),
            )
        )
    )

    assert resolution.status is EligibilityStatus.ELIGIBLE
    assert resolution.selected is not None
    assert resolution.selected.relative_spread == Decimal("0.20")
    assert resolution.selected.data_age_seconds == Decimal("5.0")


def test_liquidity_uses_specified_or_rule_without_fabricating_missing_field() -> None:
    volume_only = _contract(30, strike="94")
    oi_only = _contract(31, strike="95")
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (
                _option_quote(volume_only, volume=10, open_interest=None),
                _option_quote(oi_only, volume=None, open_interest=100),
            )
        )
    )

    assert len(resolution.candidates) == 2
    by_id = {candidate.contract.con_id: candidate for candidate in resolution.candidates}
    assert by_id[30].open_interest is None
    assert by_id[31].volume is None


def test_candidate_accumulates_all_material_rejection_codes() -> None:
    contract = _contract(40, strike="100")
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (
                _option_quote(
                    contract,
                    bid=Decimal("0"),
                    ask=Decimal("0.50"),
                    delta=Decimal("0.40"),
                    volume=9,
                    open_interest=99,
                    age_seconds=Decimal("5.001"),
                    quality=DataQuality.DELAYED,
                ),
            )
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert resolution.selected is None
    assert {
        CandidateRejectionCode.NOT_OTM,
        CandidateRejectionCode.ZERO_BID,
        CandidateRejectionCode.WRONG_DELTA_SIGN,
        CandidateRejectionCode.DELTA_OUTSIDE_RANGE,
        CandidateRejectionCode.INSUFFICIENT_LIQUIDITY,
        CandidateRejectionCode.STALE_QUOTE,
        CandidateRejectionCode.DATA_QUALITY,
    } <= _codes(resolution)
    assert resolution.no_trade_reasons


def test_missing_delta_returns_typed_no_trade_without_fallback() -> None:
    resolution = StrikeResolver(_policy()).resolve(
        _context((_option_quote(_contract(50), delta=None),))
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.MISSING_DELTA in _codes(resolution)


def test_call_requires_otm_strike_reconciled_basis_and_full_coverage() -> None:
    valid = _contract(60, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(valid, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=_basis(adjusted_basis=Decimal("105")),
            stock_availability=_stock(),
            capital=_capital(
                current_symbol_allocation=Decimal("10000"),
                current_total_wheel_allocation=Decimal("10000"),
            ),
        )
    )

    assert resolution.status is EligibilityStatus.ELIGIBLE

    below_basis = _contract(61, strike="104", right=OptionRight.CALL)
    rejected = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(below_basis, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=_basis(adjusted_basis=Decimal("105")),
            stock_availability=_stock(Decimal("99")),
        )
    )
    assert CandidateRejectionCode.BELOW_STRATEGY_BASIS in _codes(rejected)
    assert CandidateRejectionCode.UNCOVERED_CALL in _codes(rejected)


def test_call_without_strategy_basis_is_rejected() -> None:
    contract = _contract(70, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            stock_availability=_stock(),
        )
    )

    assert CandidateRejectionCode.MISSING_STRATEGY_BASIS in _codes(resolution)


def test_capital_conflict_and_reconciliation_failures_are_explicit() -> None:
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(_contract(80)),),
            capital=_capital(
                uncommitted_cash=Decimal("1"),
                current_symbol_allocation=Decimal("25000"),
                current_total_wheel_allocation=Decimal("60000"),
            ),
            conflict=True,
            reconciliation=ReconciliationStatus.PENDING,
        )
    )

    assert {
        CandidateRejectionCode.CAPITAL_LIMIT,
        CandidateRejectionCode.CONFLICTING_EXPOSURE,
        CandidateRejectionCode.RECONCILIATION_NOT_READY,
    } <= _codes(resolution)


def test_wrong_contract_metadata_and_duplicate_quotes_fail_closed() -> None:
    contract = _contract(90, trading_class="WRONG")
    quote = _option_quote(contract)
    resolution = StrikeResolver(_policy()).resolve(
        _context((quote, quote), chain_trading_class="AAPL")
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.CONTRACT_METADATA in _codes(resolution)
    assert CandidateRejectionCode.DUPLICATE_QUOTE in _codes(resolution)


def test_max_expirations_is_deterministic_and_rejects_unselected_expiry() -> None:
    target = _contract(100, dte=21, strike="95")
    other = _contract(101, dte=22, strike="94")
    resolution = StrikeResolver(_policy(max_expirations=1)).resolve(
        _context((_option_quote(other), _option_quote(target)))
    )

    assert resolution.selected_expirations == (target.expiration,)
    assert [candidate.contract.con_id for candidate in resolution.candidates] == [100]
    assert CandidateRejectionCode.EXPIRATION_NOT_SELECTED in _codes(resolution)


def test_equal_scores_have_total_order_independent_of_input_order() -> None:
    lower_strike = _contract(110, strike="94")
    higher_strike = _contract(111, strike="95")
    forward = StrikeResolver(_policy()).resolve(
        _context((_option_quote(higher_strike), _option_quote(lower_strike)))
    )
    reverse = StrikeResolver(_policy()).resolve(
        _context((_option_quote(lower_strike), _option_quote(higher_strike)))
    )

    assert [item.contract.con_id for item in forward.candidates] == [110, 111]
    assert forward.candidates == reverse.candidates


def test_underlying_and_option_future_or_unrankable_data_are_rejected() -> None:
    contract = _contract(120)
    future_quote = _option_quote(contract, age_seconds=Decimal("-0.001"))
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (future_quote,),
            underlying_quote=_underlying_quote(quality=DataQuality.UNKNOWN),
        )
    )

    assert CandidateRejectionCode.FUTURE_QUOTE in _codes(resolution)
    assert CandidateRejectionCode.UNDERLYING_DATA in _codes(resolution)


def test_empty_quote_set_is_no_trade() -> None:
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (),
            conflict=True,
            reconciliation=ReconciliationStatus.MANUAL_REVIEW,
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert resolution.candidates == ()
    assert resolution.rejected == ()
    assert resolution.no_trade_reasons[0] == (
        "No qualified option quotes were supplied for evaluation."
    )
    assert any("CONFLICTING_EXPOSURE" in reason for reason in resolution.no_trade_reasons)
    assert any("RECONCILIATION_NOT_READY" in reason for reason in resolution.no_trade_reasons)


def test_policy_is_constructed_from_validated_settings() -> None:
    settings = Settings(_env_file=None)
    policy = ResolverPolicy.from_settings(settings)

    assert policy.target_abs_delta == settings.target_abs_delta
    assert policy.max_contracts_per_order == settings.max_contracts_per_order
    assert policy.expiration_timezone == settings.market_timezone


@pytest.mark.parametrize(
    ("right", "strike", "delta"),
    [
        pytest.param(OptionRight.PUT, "95", "-0.30", id="put"),
        pytest.param(OptionRight.CALL, "105", "0.30", id="call"),
    ],
)
@pytest.mark.parametrize(
    ("deliverable_verified", "deliverable_shares"),
    [
        pytest.param(False, None, id="unverified"),
        pytest.param(True, "50", id="adjusted"),
    ],
)
def test_wheel_candidates_require_verified_standard_deliverable(
    right: OptionRight,
    strike: str,
    delta: str,
    deliverable_verified: bool,
    deliverable_shares: str | None,
) -> None:
    contract = _contract(
        130,
        strike=strike,
        right=right,
        deliverable_verified=deliverable_verified,
        deliverable_shares=deliverable_shares,
    )
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal(delta)),),
            right=right,
            strategy_basis=_basis(adjusted_basis=Decimal("100")),
            stock_availability=_stock(),
        )
    )

    assert CandidateRejectionCode.UNVERIFIED_DELIVERABLE in _codes(resolution)


@pytest.mark.parametrize(
    ("basis", "expected_code"),
    [
        pytest.param(
            _basis(wheel_cycle_id="other-cycle"),
            CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
            id="cycle",
        ),
        pytest.param(
            _basis(symbol="MSFT"),
            CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
            id="symbol",
        ),
        pytest.param(
            _basis(underlying_con_id=999),
            CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
            id="underlying",
        ),
        pytest.param(
            _basis(currency="EUR"),
            CandidateRejectionCode.STRATEGY_BASIS_MISMATCH,
            id="currency",
        ),
        pytest.param(
            _basis(account=OTHER_ACCOUNT_FINGERPRINT),
            CandidateRejectionCode.ACCOUNT_SCOPE_MISMATCH,
            id="account",
        ),
    ],
)
def test_call_rejects_basis_outside_exact_cycle_scope(
    basis: BasisProjection,
    expected_code: CandidateRejectionCode,
) -> None:
    contract = _contract(142, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=basis,
            stock_availability=_stock(),
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert expected_code in _codes(resolution)


@pytest.mark.parametrize(
    "basis",
    [
        pytest.param(
            _basis(reconciliation=ReconciliationStatus.PENDING),
            id="pending",
        ),
        pytest.param(_basis(adjusted_basis=None), id="missing-projection"),
    ],
)
def test_call_requires_complete_reconciled_basis_projection(
    basis: BasisProjection,
) -> None:
    contract = _contract(146, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=basis,
            stock_availability=_stock(),
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.STRATEGY_BASIS_MISMATCH in _codes(resolution)


@pytest.mark.parametrize(
    ("stock", "expected_code"),
    [
        pytest.param(
            _stock(wheel_cycle_id="other-cycle"),
            CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
            id="cycle",
        ),
        pytest.param(
            _stock(symbol="MSFT"),
            CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
            id="symbol",
        ),
        pytest.param(
            _stock(underlying_con_id=999),
            CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
            id="underlying",
        ),
        pytest.param(
            _stock(currency="EUR"),
            CandidateRejectionCode.STOCK_AVAILABILITY_MISMATCH,
            id="currency",
        ),
        pytest.param(
            _stock(account=OTHER_ACCOUNT_FINGERPRINT),
            CandidateRejectionCode.ACCOUNT_SCOPE_MISMATCH,
            id="account",
        ),
    ],
)
def test_call_rejects_stock_availability_outside_exact_instrument_scope(
    stock: StockAvailability,
    expected_code: CandidateRejectionCode,
) -> None:
    contract = _contract(143, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=_basis(),
            stock_availability=stock,
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert expected_code in _codes(resolution)


def test_stock_availability_normalizes_required_cycle_and_symbol_scope() -> None:
    stock = _stock(
        wheel_cycle_id=f"  {WHEEL_CYCLE_ID}  ",
        symbol="  aapl  ",
    )

    assert stock.wheel_cycle_id == WHEEL_CYCLE_ID
    assert stock.symbol == "AAPL"

    with pytest.raises(ValidationError, match="wheel_cycle_id must not be blank"):
        _stock(wheel_cycle_id="  ")
    with pytest.raises(ValidationError, match="symbol and currency must not be blank"):
        _stock(symbol="  ")


def test_call_accepts_legitimate_negative_reconciled_strategy_basis() -> None:
    contract = _contract(144, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=_basis(adjusted_basis=Decimal("-5")),
            stock_availability=_stock(),
        )
    )

    assert resolution.status is EligibilityStatus.ELIGIBLE


def test_call_requires_typed_stock_availability() -> None:
    contract = _contract(145, strike="105", right=OptionRight.CALL)
    resolution = StrikeResolver(_policy()).resolve(
        _context(
            (_option_quote(contract, delta=Decimal("0.30")),),
            right=OptionRight.CALL,
            strategy_basis=_basis(),
        )
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.MISSING_STOCK_AVAILABILITY in _codes(resolution)


def test_contract_cannot_claim_verified_deliverable_without_complete_evidence() -> None:
    with pytest.raises(ValidationError, match="Verified option deliverables"):
        OptionContract(
            con_id=140,
            symbol="AAPL",
            expiration=(NOW + timedelta(days=21)).date(),
            strike=Decimal("105"),
            right=OptionRight.CALL,
            multiplier=Decimal("100"),
            trading_class="AAPL",
            local_symbol="AAPL-140",
            deliverable_verified=True,
        )


@pytest.mark.parametrize(
    "contract",
    [
        _contract(131, currency="EUR"),
        _contract(132, exchange="CBOE"),
        _contract(133, underlying_con_id=999),
    ],
)
def test_currency_exchange_and_underlying_identity_must_match_chain(
    contract: OptionContract,
) -> None:
    resolution = StrikeResolver(_policy()).resolve(_context((_option_quote(contract),)))

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert {
        CandidateRejectionCode.CURRENCY_MISMATCH,
        CandidateRejectionCode.CONTRACT_METADATA,
    } & _codes(resolution)


def test_option_quote_cannot_supply_underlying_spot() -> None:
    false_underlying = _contract(UNDERLYING.con_id)
    underlying_quote = _option_quote(false_underlying)
    candidate = _contract(134)

    resolution = StrikeResolver(_policy()).resolve(
        _context((_option_quote(candidate),), underlying_quote=underlying_quote)
    )

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.UNDERLYING_DATA in _codes(resolution)


def test_exchange_local_date_controls_dte() -> None:
    as_of = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
    contract = _contract(135, dte=6).model_copy(
        update={"expiration": datetime(2026, 1, 21, tzinfo=UTC).date()}
    )
    underlying_quote = _underlying_quote().model_copy(update={"timestamp": as_of})
    option_quote = _option_quote(contract).model_copy(update={"timestamp": as_of})

    resolution = StrikeResolver(_policy(min_dte=7)).resolve(
        _context((option_quote,), as_of=as_of, underlying_quote=underlying_quote)
    )

    assert resolution.status is EligibilityStatus.ELIGIBLE
    assert resolution.selected is not None
    assert resolution.selected.dte == 7


def test_hard_spread_filter_is_independent_of_ambient_decimal_precision() -> None:
    contract = _contract(136)
    quote = _option_quote(
        contract,
        bid=Decimal("0.89999995"),
        ask=Decimal("1.10000005"),
    )

    with localcontext() as decimal_context:
        decimal_context.prec = 6
        resolution = StrikeResolver(_policy()).resolve(_context((quote,)))

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.SPREAD_TOO_WIDE in _codes(resolution)


def test_hard_delta_filter_is_independent_of_ambient_decimal_precision() -> None:
    quote = _option_quote(_contract(141), delta=Decimal("-0.3500004"))

    with localcontext() as decimal_context:
        decimal_context.prec = 6
        resolution = StrikeResolver(_policy()).resolve(_context((quote,)))

    assert resolution.status is EligibilityStatus.NO_TRADE
    assert CandidateRejectionCode.DELTA_OUTSIDE_RANGE in _codes(resolution)


def test_missing_liquidity_is_not_rewarded_over_known_zero() -> None:
    missing = _contract(137, strike="94")
    known_zero = _contract(138, strike="95")
    policy = _policy(
        delta_weight=Decimal("0"),
        spread_weight=Decimal("0"),
        dte_weight=Decimal("0"),
        liquidity_weight=Decimal("1"),
    )

    resolution = StrikeResolver(policy).resolve(
        _context(
            (
                _option_quote(missing, volume=None, open_interest=100),
                _option_quote(known_zero, volume=0, open_interest=100),
            )
        )
    )

    scores = {candidate.contract.con_id: candidate.score for candidate in resolution.candidates}
    assert scores[missing.con_id] == scores[known_zero.con_id]


def test_resolver_context_defaults_reconciliation_to_pending() -> None:
    values = _context((_option_quote(_contract(139)),)).model_dump()
    values.pop("reconciliation_status")

    context = ResolverContext.model_validate(values)
    resolution = StrikeResolver(_policy()).resolve(context)

    assert context.reconciliation_status is ReconciliationStatus.PENDING
    assert CandidateRejectionCode.RECONCILIATION_NOT_READY in _codes(resolution)


def test_invalid_policy_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="expiration_timezone"):
        _policy(expiration_timezone="Mars/Olympus_Mons")

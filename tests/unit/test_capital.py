from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from chronos.strategy.capital import (
    CapitalContext,
    evaluate_covered_call_capital,
    evaluate_short_put_capital,
    short_put_notional,
    stock_capital,
)
from chronos.utils.identifiers import account_fingerprint

ACCOUNT_FINGERPRINT = account_fingerprint("DU1234567")


def context(
    *,
    net_liquidation: str = "100000",
    uncommitted_cash: str = "10000",
    symbol_allocation: str = "15000",
    total_allocation: str = "50000",
) -> CapitalContext:
    return CapitalContext(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        net_liquidation=Decimal(net_liquidation),
        uncommitted_cash=Decimal(uncommitted_cash),
        current_symbol_allocation=Decimal(symbol_allocation),
        current_total_wheel_allocation=Decimal(total_allocation),
        max_symbol_allocation_pct=Decimal("0.25"),
        max_total_wheel_allocation_pct=Decimal("0.60"),
    )


def test_capital_context_defaults_and_normalizes_currency() -> None:
    assert context().currency == "USD"

    normalized = CapitalContext(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        net_liquidation=Decimal("100000"),
        uncommitted_cash=Decimal("10000"),
        current_symbol_allocation=Decimal("15000"),
        current_total_wheel_allocation=Decimal("50000"),
        max_symbol_allocation_pct=Decimal("0.25"),
        max_total_wheel_allocation_pct=Decimal("0.60"),
        currency=" eur ",
    )

    assert normalized.currency == "EUR"


def test_capital_context_rejects_blank_currency() -> None:
    with pytest.raises(ValidationError, match="currency must not be blank"):
        CapitalContext.model_validate({**context().model_dump(), "currency": " "})


def test_capital_context_rejects_raw_or_malformed_account_scope() -> None:
    with pytest.raises(ValidationError, match="account_fingerprint"):
        CapitalContext.model_validate(
            {**context().model_dump(), "account_fingerprint": "DU1234567"}
        )


def test_capital_context_rejects_symbol_allocation_above_total_allocation() -> None:
    with pytest.raises(ValidationError, match="symbol allocation must not exceed"):
        context(symbol_allocation="50000.01", total_allocation="50000")


@pytest.mark.parametrize(
    ("strike", "multiplier", "contracts", "expected"),
    [
        ("50", "100", 2, "10000"),
        ("80", "50", 3, "12000"),
        ("12.345", "10", 4, "493.800"),
    ],
)
def test_short_put_notional_uses_qualified_multiplier_and_decimal_math(
    strike: str,
    multiplier: str,
    contracts: int,
    expected: str,
) -> None:
    assert short_put_notional(
        strike=Decimal(strike),
        multiplier=Decimal(multiplier),
        contracts=contracts,
    ) == Decimal(expected)


@pytest.mark.parametrize(
    ("strike", "multiplier", "contracts", "message"),
    [
        ("0", "100", 1, "strike"),
        ("-1", "100", 1, "strike"),
        ("50", "0", 1, "multiplier"),
        ("50", "-100", 1, "multiplier"),
        ("50", "100", 0, "contracts"),
        ("50", "100", -1, "contracts"),
    ],
)
def test_short_put_notional_rejects_nonpositive_contract_inputs(
    strike: str,
    multiplier: str,
    contracts: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        short_put_notional(
            strike=Decimal(strike),
            multiplier=Decimal(multiplier),
            contracts=contracts,
        )


@pytest.mark.parametrize(
    "contracts",
    [True, 1.0, 1.5, Decimal("1"), Decimal("1.5")],
)
def test_capital_helpers_reject_non_integer_contract_counts(contracts: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        short_put_notional(
            strike=Decimal("50"),
            multiplier=Decimal("100"),
            contracts=contracts,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="positive integer"):
        evaluate_short_put_capital(
            context=context(),
            strike=Decimal("50"),
            multiplier=Decimal("100"),
            verified_deliverable_shares=Decimal("100"),
            contracts=contracts,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="positive integer"):
        evaluate_covered_call_capital(
            context=context(),
            multiplier=Decimal("100"),
            verified_deliverable_shares=Decimal("100"),
            contracts=contracts,  # type: ignore[arg-type]
            unencumbered_shares=Decimal("100"),
        )


def test_stock_capital_uses_an_explicit_reference_price() -> None:
    assert stock_capital(
        shares=Decimal("123.5"),
        reference_price=Decimal("40.25"),
    ) == Decimal("4970.875")


@pytest.mark.parametrize(
    ("shares", "price", "message"),
    [("-1", "100", "shares"), ("100", "-0.01", "reference_price")],
)
def test_stock_capital_rejects_negative_inputs(
    shares: str,
    price: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        stock_capital(shares=Decimal(shares), reference_price=Decimal(price))


def test_short_put_passes_exact_cash_and_allocation_boundaries() -> None:
    result = evaluate_short_put_capital(
        context=context(),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is True
    assert result.proposal_amount == Decimal("10000")
    assert result.resulting_symbol_allocation == Decimal("25000")
    assert result.resulting_total_wheel_allocation == Decimal("60000")
    assert result.resulting_symbol_allocation_pct == Decimal("0.25")
    assert result.resulting_total_wheel_allocation_pct == Decimal("0.60")
    assert result.shares_required == Decimal("200")
    assert result.reasons == ()


@pytest.mark.parametrize("deliverable_shares", ["0", "99", "101"])
def test_short_put_rejects_unverified_or_nonstandard_deliverable_values(
    deliverable_shares: str,
) -> None:
    message = (
        "verified_deliverable_shares"
        if deliverable_shares == "0"
        else "verified standard share deliverables"
    )
    with pytest.raises(ValueError, match=message):
        evaluate_short_put_capital(
            context=context(),
            strike=Decimal("50"),
            multiplier=Decimal("100"),
            verified_deliverable_shares=Decimal(deliverable_shares),
            contracts=1,
        )


def test_one_cent_cash_deficit_fails_even_when_allocations_pass() -> None:
    result = evaluate_short_put_capital(
        context=context(
            uncommitted_cash="9999.99",
            symbol_allocation="0",
            total_allocation="0",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct == Decimal("0.10")
    assert result.resulting_total_wheel_allocation_pct == Decimal("0.10")
    assert result.reasons == ("Uncommitted cash is below the gross assignment obligation.",)


def test_symbol_limit_is_not_rounded_before_comparison() -> None:
    result = evaluate_short_put_capital(
        context=context(
            symbol_allocation="15000.01",
            total_allocation="40000",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct == Decimal("0.2500001")
    assert result.resulting_total_wheel_allocation_pct == Decimal("0.50")
    assert result.reasons == ("The resulting symbol allocation exceeds its configured limit.",)


def test_low_ambient_precision_cannot_hide_a_one_cent_symbol_limit_breach() -> None:
    with localcontext() as ambient:
        ambient.prec = 6
        result = evaluate_covered_call_capital(
            context=context(
                symbol_allocation="25000.01",
                total_allocation="50000",
            ),
            multiplier=Decimal("100"),
            verified_deliverable_shares=Decimal("100"),
            contracts=1,
            unencumbered_shares=Decimal("100"),
        )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct == Decimal("0.2500001")
    assert result.reasons == ("The resulting symbol allocation exceeds its configured limit.",)


def test_more_than_fifty_digit_one_unit_symbol_limit_breach_is_not_rounded_away() -> None:
    symbol_limit = "5" + "0" * 60
    one_unit_over = "5" + "0" * 59 + "1"
    with localcontext() as ambient:
        ambient.prec = 6
        result = evaluate_short_put_capital(
            context=context(
                net_liquidation="1" + "0" * 61,
                uncommitted_cash="1",
                symbol_allocation=symbol_limit,
                total_allocation=symbol_limit,
            ),
            strike=Decimal("1"),
            multiplier=Decimal("1"),
            verified_deliverable_shares=Decimal("1"),
            contracts=1,
        )

    assert result.resulting_symbol_allocation == Decimal(one_unit_over)
    assert result.passed is False
    assert result.reasons == ("The resulting symbol allocation exceeds its configured limit.",)


def test_total_limit_is_not_rounded_before_comparison() -> None:
    result = evaluate_short_put_capital(
        context=context(
            symbol_allocation="0",
            total_allocation="50000.01",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct == Decimal("0.10")
    assert result.resulting_total_wheel_allocation_pct == Decimal("0.6000001")
    assert result.reasons == ("The resulting total Wheel allocation exceeds its configured limit.",)


def test_all_capital_failures_are_reported_together() -> None:
    result = evaluate_short_put_capital(
        context=context(
            uncommitted_cash="9999.99",
            symbol_allocation="15000.01",
            total_allocation="50000.01",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is False
    assert result.reasons == (
        "The resulting symbol allocation exceeds its configured limit.",
        "The resulting total Wheel allocation exceeds its configured limit.",
        "Uncommitted cash is below the gross assignment obligation.",
    )


def test_zero_net_liquidation_fails_closed_without_division() -> None:
    result = evaluate_short_put_capital(
        context=context(
            net_liquidation="0",
            symbol_allocation="0",
            total_allocation="0",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct is None
    assert result.resulting_total_wheel_allocation_pct is None
    assert result.reasons == ("Net liquidation value must be positive for allocation checks.",)


def test_negative_uncommitted_cash_fails_the_cash_guard() -> None:
    result = evaluate_short_put_capital(
        context=context(
            uncommitted_cash="-1",
            symbol_allocation="0",
            total_allocation="0",
        ),
        strike=Decimal("1"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=1,
    )

    assert result.passed is False
    assert any("Uncommitted cash" in reason for reason in result.reasons)


def test_existing_commitments_are_included_in_post_trade_allocations() -> None:
    result = evaluate_short_put_capital(
        context=context(
            uncommitted_cash="20000",
            symbol_allocation="10000",
            total_allocation="45000",
        ),
        strike=Decimal("50"),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
    )

    assert result.proposal_amount == Decimal("10000")
    assert result.resulting_symbol_allocation == Decimal("20000")
    assert result.resulting_total_wheel_allocation == Decimal("55000")
    assert result.passed is True


def test_covered_call_passes_at_exact_coverage_and_allocation_limits() -> None:
    result = evaluate_covered_call_capital(
        context=context(),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
        unencumbered_shares=Decimal("200"),
    )

    assert result.passed is True
    assert result.proposal_amount == Decimal("0")
    assert result.shares_required == Decimal("200")
    assert result.remaining_unencumbered_shares == Decimal("0")
    assert result.resulting_symbol_allocation_pct == Decimal("0.15")
    assert result.resulting_total_wheel_allocation_pct == Decimal("0.50")
    assert result.reasons == ()


def test_covered_call_fails_when_one_share_short() -> None:
    result = evaluate_covered_call_capital(
        context=context(),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=2,
        unencumbered_shares=Decimal("199"),
    )

    assert result.passed is False
    assert result.shares_required == Decimal("200")
    assert result.remaining_unencumbered_shares == Decimal("0")
    assert result.reasons == ("The proposed calls are not fully covered by unencumbered shares.",)


@pytest.mark.parametrize("deliverable_shares", ["50", "101"])
def test_covered_call_rejects_nonstandard_deliverables(deliverable_shares: str) -> None:
    with pytest.raises(ValueError, match="verified standard share deliverables"):
        evaluate_covered_call_capital(
            context=context(),
            multiplier=Decimal("100"),
            verified_deliverable_shares=Decimal(deliverable_shares),
            contracts=1,
            unencumbered_shares=Decimal("200"),
        )


def test_covered_call_does_not_bypass_preexisting_allocation_failures() -> None:
    result = evaluate_covered_call_capital(
        context=context(
            uncommitted_cash="0",
            symbol_allocation="25000.01",
            total_allocation="60000.01",
        ),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=1,
        unencumbered_shares=Decimal("100"),
    )

    assert result.passed is False
    assert result.proposal_amount == Decimal("0")
    assert result.reasons == (
        "The resulting symbol allocation exceeds its configured limit.",
        "The resulting total Wheel allocation exceeds its configured limit.",
    )


def test_covered_call_zero_net_liquidation_fails_closed() -> None:
    result = evaluate_covered_call_capital(
        context=context(
            net_liquidation="0",
            uncommitted_cash="0",
            symbol_allocation="0",
            total_allocation="0",
        ),
        multiplier=Decimal("100"),
        verified_deliverable_shares=Decimal("100"),
        contracts=1,
        unencumbered_shares=Decimal("100"),
    )

    assert result.passed is False
    assert result.resulting_symbol_allocation_pct is None
    assert result.resulting_total_wheel_allocation_pct is None


@pytest.mark.parametrize(
    ("multiplier", "deliverable_shares", "contracts", "shares", "message"),
    [
        ("0", "100", 1, "100", "multiplier"),
        ("100", "0", 1, "100", "verified_deliverable_shares"),
        ("100", "100", 0, "100", "contracts"),
        ("100", "100", 1, "-1", "unencumbered_shares"),
    ],
)
def test_covered_call_rejects_invalid_inputs(
    multiplier: str,
    deliverable_shares: str,
    contracts: int,
    shares: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        evaluate_covered_call_capital(
            context=context(),
            multiplier=Decimal(multiplier),
            verified_deliverable_shares=Decimal(deliverable_shares),
            contracts=contracts,
            unencumbered_shares=Decimal(shares),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"net_liquidation": Decimal("-1")},
        {"current_symbol_allocation": Decimal("-1")},
        {"current_total_wheel_allocation": Decimal("-1")},
        {"max_symbol_allocation_pct": Decimal("0")},
        {"max_total_wheel_allocation_pct": Decimal("1.01")},
    ],
)
def test_capital_context_rejects_invalid_account_constraints(
    overrides: dict[str, Decimal],
) -> None:
    values = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "net_liquidation": Decimal("100000"),
        "uncommitted_cash": Decimal("10000"),
        "current_symbol_allocation": Decimal("0"),
        "current_total_wheel_allocation": Decimal("0"),
        "max_symbol_allocation_pct": Decimal("0.25"),
        "max_total_wheel_allocation_pct": Decimal("0.60"),
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        CapitalContext.model_validate(values)

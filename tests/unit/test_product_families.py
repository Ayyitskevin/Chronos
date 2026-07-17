"""Milestone 3: product-family eligibility, reservations, trading hours."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from chronos.config.settings import Settings
from chronos.domain.enums import ProductFamily
from chronos.services.trading_hours import MarketSession, session_for
from chronos.strategy.eligibility import classify_symbol, evaluate
from chronos.strategy.reservations import (
    CashReservationInput,
    ShareReservationInput,
    reserve_cash,
    reserve_shares,
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestEligibility:
    def test_crypto_disabled_by_default(self) -> None:
        settings = _settings()
        decision = evaluate(settings, "BTC", ProductFamily.CRYPTO)
        assert decision.allowed is False
        assert any("disabled" in reason for reason in decision.reasons)

    def test_crypto_enabled_by_allowlist(self) -> None:
        settings = _settings(crypto_allowlist="BTC,ETH")
        assert evaluate(settings, "BTC", ProductFamily.CRYPTO).allowed is True
        assert evaluate(settings, "DOGE", ProductFamily.CRYPTO).allowed is False

    def test_stock_and_option_families_use_symbol_allowlist(self) -> None:
        settings = _settings()
        assert evaluate(settings, "AAPL", ProductFamily.STOCK).allowed is True
        assert evaluate(settings, "AAPL", ProductFamily.OPTION).allowed is True
        assert evaluate(settings, "TSLA", ProductFamily.STOCK).allowed is False

    def test_symbol_on_both_lists_is_refused(self) -> None:
        settings = _settings(crypto_allowlist="AAPL")
        decision = evaluate(settings, "AAPL", ProductFamily.STOCK)
        assert decision.allowed is False
        assert any("both" in reason for reason in decision.reasons)

    def test_classify(self) -> None:
        settings = _settings(crypto_allowlist="BTC")
        assert classify_symbol(settings, "aapl") is ProductFamily.STOCK
        assert classify_symbol(settings, "BTC") is ProductFamily.CRYPTO
        assert classify_symbol(settings, "TSLA") is None


class TestCashReservations:
    def test_sufficient(self) -> None:
        summary = reserve_cash(
            CashReservationInput(
                existing_short_put_obligation=Decimal("19000"),
                pending_put_order_obligation=Decimal("0"),
                proposed_order_obligation=Decimal("18500"),
                cash_buffer=Decimal("5000"),
            ),
            available_cash=Decimal("50000"),
        )
        assert summary.sufficient is True
        assert summary.total_reserved == Decimal("42500")
        assert summary.remaining_after_reservations == Decimal("7500")

    def test_shortfall_reports_reason(self) -> None:
        summary = reserve_cash(
            CashReservationInput(
                proposed_order_obligation=Decimal("19000"),
                cash_buffer=Decimal("5000"),
            ),
            available_cash=Decimal("20000"),
        )
        assert summary.sufficient is False
        assert "shortfall 4000" in summary.reasons[0]

    def test_negative_bucket_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            reserve_cash(
                CashReservationInput(cash_buffer=Decimal("-1")),
                available_cash=Decimal("0"),
            )


class TestShareReservations:
    def test_capacity_floors_to_whole_contracts(self) -> None:
        summary = reserve_shares(
            ShareReservationInput(
                settled_long_shares=Decimal("250"),
                shares_covering_short_calls=Decimal("100"),
            )
        )
        assert summary.unencumbered_shares == Decimal("150")
        assert summary.max_new_call_contracts == 1  # 150 // 100

    def test_over_encumbrance_flags_and_zeroes_capacity(self) -> None:
        summary = reserve_shares(
            ShareReservationInput(
                settled_long_shares=Decimal("100"),
                shares_covering_short_calls=Decimal("100"),
                shares_reserved_for_pending_calls=Decimal("100"),
            )
        )
        assert summary.unencumbered_shares == Decimal("-100")
        assert summary.max_new_call_contracts == 0
        assert summary.reasons  # disagreement surfaced, not hidden


class TestTradingHours:
    def test_crypto_always_open(self) -> None:
        sunday_3am = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
        decision = session_for(ProductFamily.CRYPTO, now=sunday_3am)
        assert decision.session is MarketSession.OPEN
        assert decision.may_submit is True

    def test_equity_weekend_closed(self) -> None:
        saturday = datetime(2026, 7, 18, 15, 0, tzinfo=UTC)
        decision = session_for(ProductFamily.STOCK, now=saturday)
        assert decision.session is MarketSession.CLOSED

    def test_equity_outside_rth_closed(self) -> None:
        # 21:00 ET on a Friday.
        friday_evening = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
        decision = session_for(ProductFamily.OPTION, now=friday_evening)
        assert decision.session is MarketSession.CLOSED

    def test_in_rth_without_broker_evidence_is_ambiguous_and_blocked(self) -> None:
        tuesday_noon_et = datetime(2026, 7, 14, 16, 0, tzinfo=UTC)  # 12:00 ET
        decision = session_for(ProductFamily.OPTION, now=tuesday_noon_et)
        assert decision.session is MarketSession.AMBIGUOUS
        assert decision.may_submit is False  # holidays can't be ruled out

    def test_in_rth_with_broker_confirmation_opens(self) -> None:
        tuesday_noon_et = datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        decision = session_for(ProductFamily.OPTION, now=tuesday_noon_et, broker_confirms_open=True)
        assert decision.session is MarketSession.OPEN
        assert decision.may_submit is True

    def test_broker_reported_holiday_closes_in_rth(self) -> None:
        tuesday_noon_et = datetime(2026, 7, 14, 16, 0, tzinfo=UTC)
        decision = session_for(ProductFamily.STOCK, now=tuesday_noon_et, broker_confirms_open=False)
        assert decision.session is MarketSession.CLOSED

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            session_for(ProductFamily.STOCK, now=datetime(2026, 7, 14, 12, 0))  # noqa: DTZ001

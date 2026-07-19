"""M7C commit 3: crypto risk branch, calendar restructure, family-aware TIF.

The intent-validator refusal on CRYPTO is still in force (ADR-0010 §7: it is
deleted in the LAST M7C commit), so these tests build crypto intents via
``model_construct`` to exercise the risk function directly — the standard way
to unit-test a validator independently of the model guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from chronos.config.settings import Settings
from chronos.domain.enums import OrderIntent, ProductFamily, ReconciliationStatus
from chronos.domain.models import AccountSummary, CryptoContract
from chronos.orders.crypto import validate_crypto_order
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.services.trading_hours import MarketSession, session_for

_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
_ACCOUNT = "U7654321"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "crypto_allowlist": ("BTC", "ETH"),
        "max_crypto_allocation_pct": Decimal("0.10"),
        "max_crypto_notional_per_order_usd": Decimal("1000"),
        "min_cash_buffer_usd": Decimal("0"),
        "min_cash_buffer_pct": Decimal("0"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _crypto_contract(**overrides: object) -> CryptoContract:
    base: dict[str, object] = {
        "con_id": 479624278,
        "symbol": "BTC",
        "min_size": Decimal("0.0001"),
        "size_increment": Decimal("0.0001"),
    }
    base.update(overrides)
    return CryptoContract(**base)  # type: ignore[arg-type]


def _crypto_intent(
    *,
    intent: OrderIntent = OrderIntent.OPEN_LONG_CRYPTO,
    quantity: Decimal = Decimal("0.001"),
    limit_price: Decimal = Decimal("64000"),
    contract: CryptoContract | None = None,
    time_in_force: str = "DAY",
) -> WheelOrderIntent:
    return WheelOrderIntent.model_construct(
        intent_id="crypto-1",
        correlation_id="CHR-ORD-" + "C" * 32,
        account_id=_ACCOUNT,
        product_family=ProductFamily.CRYPTO,
        intent=intent,
        contract=contract or _crypto_contract(),
        quantity=quantity,
        limit_price=limit_price,
        order_type="LMT",
        time_in_force=time_in_force,
        outside_rth=False,
    )


def _evidence(**overrides: object) -> RiskEvidence:
    base: dict[str, object] = {
        "account": AccountSummary(
            account_id=_ACCOUNT,
            net_liquidation=Decimal("100000"),
            total_cash=Decimal("50000"),
            buying_power=Decimal("100000"),
            as_of=_NOW,
        ),
        "reconciliation_status": ReconciliationStatus.RECONCILED,
        "session": session_for(ProductFamily.CRYPTO, now=_NOW),
    }
    base.update(overrides)
    return RiskEvidence(**base)  # type: ignore[arg-type]


def _status(checks: list, name: str) -> str:
    return next(c.status.value for c in checks if c.name == name)


# --- venue conformance ----------------------------------------------------


def test_conforming_buy_passes_all_crypto_checks() -> None:
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001")), evidence=_evidence(), settings=_settings()
    )
    assert all(c.status.value == "PASS" for c in checks), [
        (c.name, c.status.value, c.detail) for c in checks
    ]


def test_absent_venue_metadata_is_unknown_not_assumed() -> None:
    contract = _crypto_contract(min_size=None, size_increment=None)
    checks = validate_crypto_order(
        _crypto_intent(contract=contract), evidence=_evidence(), settings=_settings()
    )
    assert _status(checks, "crypto_venue_conformance") == "UNKNOWN"


def test_below_min_size_fails() -> None:
    contract = _crypto_contract(min_size=Decimal("0.01"))
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001"), contract=contract),
        evidence=_evidence(),
        settings=_settings(),
    )
    assert _status(checks, "crypto_venue_conformance") == "FAIL"


def test_non_increment_multiple_fails() -> None:
    contract = _crypto_contract(size_increment=Decimal("0.001"))
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.0015"), contract=contract),
        evidence=_evidence(),
        settings=_settings(),
    )
    assert _status(checks, "crypto_venue_conformance") == "FAIL"


# --- notional cap ---------------------------------------------------------


def test_notional_over_cap_fails() -> None:
    # 0.02 BTC * 64000 = 1280 > 1000 cap
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.02")),
        evidence=_evidence(),
        settings=_settings(),
    )
    assert _status(checks, "crypto_notional_cap") == "FAIL"


# --- allocation cap (BUY-scoped, mark-required) ---------------------------


def test_allocation_unmarked_is_unknown() -> None:
    checks = validate_crypto_order(
        _crypto_intent(),
        evidence=_evidence(crypto_allocation_marked=False),
        settings=_settings(),
    )
    assert _status(checks, "crypto_allocation_cap") == "UNKNOWN"


def test_allocation_cap_breach_fails() -> None:
    # ceiling = 0.10 * 100000 = 10000; current 9800 + order (0.001*64000=64) ok,
    # but current 9990 + 64 = 10054 > 10000 fails.
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001")),
        evidence=_evidence(current_crypto_allocation=Decimal("9990")),
        settings=_settings(),
    )
    assert _status(checks, "crypto_allocation_cap") == "FAIL"


def test_allocation_cap_counts_pending_buys() -> None:
    # current 9900 + pending 64 + order 64 = 10028 > 10000 ceiling.
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001")),
        evidence=_evidence(
            current_crypto_allocation=Decimal("9900"),
            pending_crypto_buy_notional=Decimal("64"),
        ),
        settings=_settings(),
    )
    assert _status(checks, "crypto_allocation_cap") == "FAIL"


def test_sell_is_not_blocked_by_allocation_cap() -> None:
    # A CLOSE reduces exposure: no allocation-cap check is even produced.
    checks = validate_crypto_order(
        _crypto_intent(intent=OrderIntent.CLOSE_LONG_CRYPTO, quantity=Decimal("0.001")),
        evidence=_evidence(
            current_crypto_allocation=Decimal("99999"),
            held_crypto_quantity=Decimal("1"),
        ),
        settings=_settings(),
    )
    assert not any(c.name == "crypto_allocation_cap" for c in checks)


# --- cash sufficiency (pending-buy encumbrance) ---------------------------


def test_cash_sufficiency_subtracts_pending_crypto_buys() -> None:
    # total_cash 50000, buffer 0, put_obl 0, pending crypto buys 49950 =>
    # available 50; order cost 64 > 50 => FAIL.
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001")),
        evidence=_evidence(pending_crypto_buy_notional=Decimal("49950")),
        settings=_settings(),
    )
    assert _status(checks, "crypto_cash_sufficiency") == "FAIL"


def test_cash_sufficiency_subtracts_put_obligations() -> None:
    checks = validate_crypto_order(
        _crypto_intent(quantity=Decimal("0.001")),
        evidence=_evidence(existing_short_put_obligation=Decimal("49990")),
        settings=_settings(),
    )
    assert _status(checks, "crypto_cash_sufficiency") == "FAIL"


# --- SELL holding (resting-order encumbrance) -----------------------------


def test_sell_within_holdings_passes() -> None:
    checks = validate_crypto_order(
        _crypto_intent(intent=OrderIntent.CLOSE_LONG_CRYPTO, quantity=Decimal("0.5")),
        evidence=_evidence(held_crypto_quantity=Decimal("1.0")),
        settings=_settings(),
    )
    assert _status(checks, "crypto_no_short") == "PASS"


def test_sell_exceeding_holdings_minus_reserved_fails() -> None:
    # held 1.0, reserved 0.8 => sellable 0.2; selling 0.5 fails (no double-sell).
    checks = validate_crypto_order(
        _crypto_intent(intent=OrderIntent.CLOSE_LONG_CRYPTO, quantity=Decimal("0.5")),
        evidence=_evidence(
            held_crypto_quantity=Decimal("1.0"),
            crypto_sell_reserved_quantity=Decimal("0.8"),
        ),
        settings=_settings(),
    )
    assert _status(checks, "crypto_no_short") == "FAIL"


# --- engine dispatch + eligibility ----------------------------------------


def test_engine_dispatches_crypto_and_disabled_allowlist_fails_eligibility() -> None:
    engine = OrderRiskEngine(_settings(crypto_allowlist=()))
    decision = engine.evaluate(_crypto_intent(), _evidence(), now=_NOW)
    # Empty allowlist disables the family: eligibility FAILs, overall FAIL.
    assert not decision.approved
    assert any(
        c.name == "symbol_family_eligibility" and c.status.value == "FAIL" for c in decision.checks
    )


def test_engine_full_crypto_buy_approves() -> None:
    engine = OrderRiskEngine(_settings())
    decision = engine.evaluate(_crypto_intent(quantity=Decimal("0.001")), _evidence(), now=_NOW)
    assert decision.approved, [(c.name, c.status.value, c.detail) for c in decision.checks]


# --- calendar restructure -------------------------------------------------


def test_crypto_default_is_open() -> None:
    decision = session_for(ProductFamily.CRYPTO, now=_NOW)
    assert decision.session is MarketSession.OPEN
    assert decision.may_submit


def test_crypto_broker_confirmed_closed_wins() -> None:
    decision = session_for(ProductFamily.CRYPTO, now=_NOW, broker_confirms_open=False)
    assert decision.session is MarketSession.CLOSED
    assert not decision.may_submit


def test_crypto_broker_none_stays_open() -> None:
    decision = session_for(ProductFamily.CRYPTO, now=_NOW, broker_confirms_open=None)
    assert decision.session is MarketSession.OPEN


# --- family-aware TIF -----------------------------------------------------


def test_crypto_ioc_permitted_when_configured() -> None:
    engine = OrderRiskEngine(_settings(crypto_time_in_force="IOC"))
    decision = engine.evaluate(
        _crypto_intent(quantity=Decimal("0.001"), time_in_force="IOC"), _evidence(), now=_NOW
    )
    assert any(c.name == "limit_only" and c.status.value == "PASS" for c in decision.checks)


def test_crypto_ioc_rejected_when_setting_is_day() -> None:
    engine = OrderRiskEngine(_settings(crypto_time_in_force="DAY"))
    decision = engine.evaluate(
        _crypto_intent(quantity=Decimal("0.001"), time_in_force="IOC"), _evidence(), now=_NOW
    )
    assert any(c.name == "limit_only" and c.status.value == "FAIL" for c in decision.checks)

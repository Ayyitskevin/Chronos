"""The option deliverable screen, and the check it finally feeds (M11, closes R-27).

R-27 was the last of the three kernel defects, and the shape was the same as the
other two: a control that was configured, documented, and structurally incapable
of passing. ``standard_deliverable_verified`` gates every option order on
``OptionContract.deliverable_verified``, and exactly one thing in the codebase
set that flag — ``DemoBroker``, by fiat. Neither IBKR adapter populated it, so
every option order against a real gateway FAILed the check and the entire option
path was unproven outside demo.

The stake is the assignment math. A short put's obligation is computed as
``strike x multiplier x contracts``, which is true only for a **standard**
contract. OCC-adjusted series deliver something else — 150 shares, shares plus
cash, another issuer's stock — and sizing one of those as if it delivered 100
shares reserves the wrong amount of cash, in the direction that leaves the
account short at assignment.

What this file pins is the honest scope of the fix. The TWS API does not expose
OCC's deliverable schedule; the screen infers *absence of adjustment* from the
OCC root, which is a naming convention rather than a read of the schedule. So
the tests are weighted toward the rejecting direction, and the one test that
matters most is the last: the check must actually pass now, or M11 built another
inert control.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from chronos.broker.official_ibkr import _with_deliverable_evidence
from chronos.config.settings import Settings
from chronos.domain.enums import OptionRight, RiskCheckStatus
from chronos.domain.models import OptionContract
from chronos.orders.risk import OrderRiskEngine
from chronos.services.option_deliverable import assess_standard_deliverable

_LOCAL = "AAPL  260821P00185000"  # OSI: 6-char padded root, 6 date, 1 right, 8 strike


def _assess(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "trading_class": "AAPL",
        "local_symbol": _LOCAL,
        "multiplier": Decimal("100"),
        "underlying_con_id": 101,
        "underlying_symbol": "AAPL",
        "underlying_security_type": "STK",
    }
    base.update(overrides)
    return assess_standard_deliverable(**base)


def _contract(**overrides: Any) -> OptionContract:
    base: dict[str, Any] = {
        "con_id": 202,
        "symbol": "AAPL",
        "expiration": date(2026, 8, 21),
        "strike": Decimal("185"),
        "right": OptionRight.PUT,
        "multiplier": Decimal("100"),
        "trading_class": "AAPL",
        "local_symbol": _LOCAL,
    }
    base.update(overrides)
    return OptionContract(**base)


def _details(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "underConId": 101,
        "underSymbol": "AAPL",
        "underSecType": "STK",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- the screen ---------------------------------------------------------------


def test_a_plain_standard_option_passes() -> None:
    """If this ever stops passing, the milestone is inert and R-27 is not closed."""

    assessment = _assess()
    assert assessment.standard is True
    assert assessment.reasons == ()


def test_a_suffixed_occ_root_is_refused() -> None:
    """The load-bearing case.

    ``AAPL1`` is how OCC marks a series whose deliverable was adjusted. It is the
    only signal in the TWS API that speaks to the deliverable at all, and it is
    the entire reason this screen can say anything.

    The local symbol is moved to match, deliberately: a stale ``AAPL`` local
    symbol would also trip the contradiction check, and then this test would pass
    even if the root condition were deleted. Here only the root can refuse.
    """

    assessment = _assess(trading_class="AAPL1", local_symbol="AAPL1 260821P00185000")
    assert assessment.standard is False
    assert assessment.reasons == (
        "the OCC root is AAPL1, not AAPL — a suffixed root is how OCC marks a series "
        "whose deliverable was adjusted",
    )


def test_a_non_hundred_multiplier_is_refused() -> None:
    """A different multiplier is a different instrument, not a rounding question."""

    assert _assess(multiplier=Decimal("10")).standard is False


@pytest.mark.parametrize(
    "override",
    [
        {"underlying_con_id": None},
        {"underlying_con_id": 0},
        {"underlying_con_id": -1},
        {"underlying_symbol": ""},
        {"underlying_security_type": ""},
        {"symbol": ""},
        {"trading_class": ""},
    ],
)
def test_every_missing_fact_refuses(override: dict[str, Any]) -> None:
    """Absence is refusal, one field at a time.

    A gateway that answers without these must not look like a gateway that
    answered "standard". This is the direction the whole screen has to fail in,
    so it is asserted field by field rather than in aggregate.
    """

    assert _assess(**override).standard is False


def test_a_cash_settled_underlying_is_refused() -> None:
    """An index option has no share deliverable at all.

    The share math there is not merely imprecise — there are no shares to
    deliver, so ``strike x 100`` describes nothing.
    """

    assessment = _assess(underlying_security_type="IND")
    assert assessment.standard is False
    assert any("not a stock" in reason for reason in assessment.reasons)


def test_an_option_on_a_different_underlying_is_refused() -> None:
    """Post-merger series can point at an acquirer's stock while keeping a root."""

    assert _assess(underlying_symbol="MSFT").standard is False


def test_a_contradicting_local_symbol_is_refused() -> None:
    """IBKR's own fields disagreeing is evidence, and it rejects."""

    assessment = _assess(local_symbol="AAPL1 260821P00185000")
    assert assessment.standard is False
    assert any("contradicts" in reason for reason in assessment.reasons)


@pytest.mark.parametrize(
    "local_symbol",
    ["", "AAPL-20260821", "AAPL  260821X00185000", "AAPL  2608XXP00185000", "not a symbol"],
)
def test_an_unparseable_local_symbol_is_not_held_against_the_contract(
    local_symbol: str,
) -> None:
    """The deliberate asymmetry, and the one place this screen does *not* fail closed.

    The OSI root carries no information the trading class does not already carry,
    so its **absence** is not evidence about the deliverable — only a parsed root
    that *disagrees* is. Refusing every option over an unparsed cosmetic field
    would make this control inert in exactly the way R-25 and R-26 were, and
    IBKR's local-symbol format has not been verified against a live gateway.
    """

    assert _assess(local_symbol=local_symbol).standard is True


def test_reasons_are_exhaustive_not_first_failure() -> None:
    """A contract failing four conditions is a different situation from one failing a
    borderline check, and an operator reading a refusal needs the whole story."""

    assessment = _assess(
        trading_class="AAPL1",
        multiplier=Decimal("10"),
        underlying_con_id=None,
        underlying_security_type="IND",
    )
    # Five, not four: leaving the local symbol standard while the OCC root moves
    # to AAPL1 is itself a contradiction, and the screen says so.
    assert len(assessment.reasons) == 5
    for fragment in ("underlying contract", "not a stock", "AAPL1", "multiplier", "contradicts"):
        assert any(fragment in reason for reason in assessment.reasons), fragment


def test_case_and_padding_do_not_decide_anything() -> None:
    assert _assess(trading_class=" aapl ", underlying_symbol="aapl").standard is True


# --- the wire from ContractDetails --------------------------------------------


def test_qualification_records_the_evidence_when_the_screen_passes() -> None:
    """The defect was here: the facts arrive on `ContractDetails` and nothing read them.

    Same failure mode as R-26 — `underConId`, `underSymbol` and `underSecType`
    live on the *details*, not on the `Contract` inside it, so
    `instrument_from_contract` never saw them.
    """

    verified = _with_deliverable_evidence(_contract(), _details())
    assert verified.deliverable_verified is True
    assert verified.deliverable_shares == Decimal("100")
    assert verified.underlying_con_id == 101
    assert verified.has_verified_standard_deliverable is True


def test_the_recorded_contract_still_satisfies_its_own_invariants() -> None:
    """`model_copy` skips validators, so the invariant is asserted rather than assumed.

    `OptionContract` refuses to be built with `deliverable_verified` set but no
    underlying id or share count. Writing the three fields through `model_copy`
    bypasses that check, so this re-validates the result to prove the update
    could have been constructed legitimately.
    """

    verified = _with_deliverable_evidence(_contract(), _details())
    assert OptionContract.model_validate(verified.model_dump()) == verified


@pytest.mark.parametrize(
    "details",
    [
        _details(underConId=0),
        _details(underSymbol=""),
        _details(underSecType="IND"),
        _details(underSymbol="MSFT"),
        SimpleNamespace(),
    ],
)
def test_a_refused_contract_comes_back_exactly_as_it_went_in(details: Any) -> None:
    """Failing the screen must never be worse than not having run it.

    An unverified contract is precisely the pre-M11 state — blocked by the risk
    engine, carrying no deliverable claim — so a failed screen is a no-op, not a
    mutation.
    """

    original = _contract()
    assert _with_deliverable_evidence(original, details) == original


def test_an_adjusted_series_survives_qualification_unverified() -> None:
    """The case the whole milestone exists to catch, end to end."""

    adjusted = _contract(trading_class="AAPL1", local_symbol="AAPL1 260821P00185000")
    assert _with_deliverable_evidence(adjusted, _details()).deliverable_verified is False


# --- the check it feeds -------------------------------------------------------


def _deliverable_check(contract: OptionContract) -> Any:
    engine = OrderRiskEngine(Settings(_env_file=None))  # type: ignore[call-arg]
    return engine._check_deliverable_verified(contract)


def test_the_gate_finally_opens_for_a_qualified_option() -> None:
    """The assertion R-27 was actually about.

    Every option order against a real gateway FAILed this check for six
    milestones. A qualified, screened contract now passes it — which is what
    "the option path is unproven outside demo" was blocking.
    """

    check = _deliverable_check(_with_deliverable_evidence(_contract(), _details()))
    assert check.status is RiskCheckStatus.PASS


def test_the_gate_still_holds_for_everything_else() -> None:
    """M11 must not have quietly turned "unscreened" into "fine"."""

    assert _deliverable_check(_contract()).status is RiskCheckStatus.FAIL
    adjusted = _contract(trading_class="AAPL1")
    assert (
        _deliverable_check(_with_deliverable_evidence(adjusted, _details())).status
        is RiskCheckStatus.FAIL
    )

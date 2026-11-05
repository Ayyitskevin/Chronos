"""Adapter-mapped execution identity and ruled planner comparison precision."""

from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tests.support.settle_rig import (
    ACCOUNT,
    BROKER_ID,
    CLIENT_ID,
    CON_ID,
    PERM_ID,
    PRIOR,
    SYMBOL,
    Rig,
    make_rig,
    venue_execution,
    venue_order,
)

from chronos.broker.official_ibkr import execution_from_pair, instrument_from_contract
from chronos.domain.enums import OrderLifecycle as L


@pytest.fixture
def rig() -> Iterator[Rig]:
    instance = make_rig()
    try:
        yield instance
    finally:
        instance.close()


def _contract(**changes: object) -> SimpleNamespace:
    fields = dict(
        secType="STK", symbol=SYMBOL, conId=CON_ID, currency="USD", exchange="SMART",
        primaryExchange="", lastTradeDateOrContractMonth="20261120", right="C", strike=150.0,
        multiplier="100", localSymbol="AAPL  261120C00150000", tradingClass="AAPL",
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("route", ["legacy", "planner-listed", "planner-completed"])
@pytest.mark.parametrize("shape", ["stock-exchange", "blank-stock-detail", "option-exchange", "option-blank-detail"])
def test_adapter_contract_detail_cannot_hide_an_own_blank_reference_execution(
    rig: Rig, route: str, shape: str
) -> None:
    row_fields: dict[str, object] = {}
    execution_fields: dict[str, object] = {}
    if shape == "stock-exchange":
        row_fields = {"primaryExchange": "NASDAQ"}
        execution_fields = {"exchange": "ISLAND"}
    elif shape == "blank-stock-detail":
        row_fields = {"exchange": "", "primaryExchange": ""}
        execution_fields = {"exchange": "NYSE"}
    elif shape == "option-exchange":
        row_fields = {"secType": "OPT"}
        execution_fields = {"secType": "OPT", "exchange": "CBOE"}
    else:
        row_fields = {"secType": "OPT", "localSymbol": "", "tradingClass": ""}
        execution_fields = {"secType": "OPT"}
    row_contract = instrument_from_contract(_contract(**row_fields))
    execution = execution_from_pair(
        _contract(**execution_fields),
        SimpleNamespace(
            execId="mapped-fill", acctNumber=ACCOUNT, orderId=BROKER_ID, permId=0,
            clientId=CLIENT_ID, orderRef="", side="BOT", shares=5.0, price=150.0,
            time="20261009 11:00:00",
        ),
        commission=None, commission_currency=None,
    )
    assert execution.contract != row_contract  # prove the venue-detail difference reaches the test
    assert execution.contract.con_id == row_contract.con_id
    rig.seed()
    if route != "legacy":
        rig.pending()
    before = rig.events()
    row = venue_order(L.CANCELLED, "0", broker_id=0 if route == "planner-completed" else BROKER_ID)
    row = row.model_copy(update={"contract": row_contract})
    if route == "planner-completed":
        rig.venue.completed = [row]
    else:
        rig.venue.open = [row]
    rig.venue.execs = [execution]

    report = rig.reconcile()

    assert rig.events() == before
    assert rig.status() is (L.SUBMITTED if route == "legacy" else L.CANCEL_PENDING)
    assert report.applied_updates == () and not report.permits_new_opening_order
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


@pytest.mark.parametrize("status", [L.SUBMITTED, L.SUBMISSION_UNKNOWN])
def test_first_seen_execution_permid_is_a_witness_when_the_owned_row_has_none(
    rig: Rig, status: L
) -> None:
    rig.seed(permanent_id=None)
    if status is L.SUBMISSION_UNKNOWN:
        rig.event(status, at=PRIOR)
    before = rig.events()
    rig.venue.open = [venue_order(L.CANCELLED, "0", perm=None)]
    rig.venue.execs = [venue_execution("first-permid", "5", PRIOR, ref=None)]

    report = rig.reconcile()

    assert rig.status() is status and rig.events() == before
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


@pytest.mark.parametrize("known", ["row", "persisted"])
def test_a_blank_execution_with_a_contradicting_known_permid_stays_excluded(
    rig: Rig, known: str
) -> None:
    rig.seed(permanent_id=PERM_ID if known == "persisted" else None)
    rig.venue.open = [venue_order(L.CANCELLED, "0", perm=PERM_ID if known == "row" else None)]
    rig.venue.execs = [venue_execution("other-permid", "5", PRIOR, ref=None, perm=PERM_ID + 1)]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and report.unresolved == ()


@pytest.mark.parametrize("figure,bound", [("5.000000001", "5.000000001"), ("4.999999995", "5")])
def test_planner_compares_both_snapshot_figures_at_the_same_eight_place_scale(
    rig: Rig, figure: str, bound: str
) -> None:
    rig.seed()
    rig.pending()
    rig.venue.completed = [venue_order(L.CANCELLED, figure, broker_id=0)]
    rig.venue.execs = [venue_execution("precision-fill", bound, PRIOR, ref=None, perm=None)]

    report = rig.reconcile()

    assert Decimal(figure).quantize(Decimal("0.00000001")) == Decimal(bound).quantize(Decimal("0.00000001"))
    assert rig.status() is L.CANCELLED and report.unresolved == ()
    assert len(report.applied_updates) == 1


@pytest.mark.parametrize("persisted_id", [BROKER_ID, None])
def test_completed_zero_id_counts_blank_zero_id_executions_with_or_without_persisted_id(
    rig: Rig, persisted_id: int | None
) -> None:
    rig.seed(broker_id=persisted_id)
    rig.pending()
    before = rig.events()
    rig.venue.completed = [venue_order(L.CANCELLED, "0", broker_id=0)]
    rig.venue.execs = [venue_execution("zero-id-fill", "5", PRIOR, ref=None, perm=None).model_copy(
        update={"broker_order_id": 0}
    )]

    report = rig.reconcile()

    assert rig.status() is L.CANCEL_PENDING and rig.events() == before
    assert report.unresolved[0].reason.startswith("snapshot fill conflict:")


@pytest.mark.parametrize("route", ["legacy", "planner-listed", "planner-completed"])
def test_blank_execution_on_another_broker_id_does_not_hold_this_intent(rig: Rig, route: str) -> None:
    rig.seed()
    if route != "legacy":
        rig.pending()
    row = venue_order(L.CANCELLED, "0", broker_id=0 if route == "planner-completed" else BROKER_ID)
    if route == "planner-completed":
        rig.venue.completed = [row]
    else:
        rig.venue.open = [row]
    rig.venue.execs = [venue_execution("other-id", "5", PRIOR, ref=None, perm=None).model_copy(
        update={"broker_order_id": BROKER_ID + 77}
    )]

    report = rig.reconcile()

    assert rig.status() is L.CANCELLED and report.unresolved == ()

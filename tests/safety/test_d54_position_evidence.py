"""D-54 (a): an autonomous CLOSE/REDUCE sizes only from broker position evidence, or refuses typed.

Owner rulings (D-55 (a), Fable's Q1-Q7 adopted 2026-10-28) pinned here, end to end through the
production gatherers (``BackendGatherers.cycle_facts`` per tick, ``instrument_facts`` per
decision) and ``run_cycle``:

- Q1: the figure is the broker's ``positions()``, read twice under one readiness bracket, cut by
  the decision's own ``con_id`` + security type. Never the reconciled store, fills, or the model.
- Q3: only a contract a FILLED Chronos opening intent of this account holds may be exited.
- Q4: the exit predicate is weaker than READY (a held position is never READY): same session,
  same generation, two agreeing reads, connection not closed. A per-decision failure is a typed
  refusal on that decision; connection-closed or out-of-scope at tick start is a blocking
  ``DegradedReason`` for the whole tick.
- Q7: a figure worth more than twice net liquidation is corrupt evidence, refused, not clamped.
- Unknown is never zero.

Nothing here has a submit path: every cycle runs in SHADOW and the broker double is read-only.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from chronos.api.autonomy_wiring import BackendGatherers
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    ProposedDecision,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    DataQuality,
    OrderIntent,
    ProductFamily,
    ReconciliationStatus,
    RiskCheckStatus,
)
from chronos.domain.models import AccountSummary, BrokerPosition
from chronos.orders.intent import build_stock_intent
from chronos.orders.reconciliation_readiness import (
    ReconciliationReadiness,
    ReconciliationReadinessSnapshot,
)
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.persistence.database import Database
from chronos.persistence.schema import HashChainRow
from chronos.services.trading_hours import session_for
from chronos.supervisor import durable, queue
from chronos.supervisor.loop import CycleFacts, CycleStage, run_cycle
from tests.support import position_evidence as pe
from tests.support.order_fakes import PAPER_ACCOUNT, paper_settings, stock_contract
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

_TARGET = "CHR-ORD-" + "A" * 32


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite+pysqlite:///:memory:")
    db.initialize()
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    db_session = database.sessions()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.close()


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-d54",
        "mandate_version": 1,
        "account_fingerprint": pe.FINGERPRINT,
        # A submitting mode, with no submit callable: the cycle's own SHADOW (nothing is sent).
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": pe.NOW - timedelta(hours=1),
        "expires_at": pe.NOW + timedelta(days=1),
        "versions": VersionPins(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        "scope": InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY", "QQQ"),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        "concentration": ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        "owner_authorization_ref": "owner-1",
        "authored_at": pe.NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _exit(
    kind: DecisionKind = DecisionKind.CLOSE,
    *,
    symbol: str = "SPY",
    quantity: Decimal | None = None,
    target: str = _TARGET,
) -> ProposedDecision:
    return ProposedDecision(
        kind=kind,
        asset_class=TradableAssetClass.EQUITY,
        symbol=symbol,
        requested_strategy=None,
        requested_quantity=quantity,
        target_client_reference=target,
        evidence=(
            EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=pe.NOW, digest="c" * 64),
        ),
        invalidation_conditions=("closes below 400",),
    )


class _Tick:
    """One production tick: ``cycle_facts`` once, then ``run_cycle`` per proposal."""

    def __init__(self, session: Session, gatherers: BackendGatherers, mandate: AutonomyMandate):
        self.session = session
        self.gatherers = gatherers
        self.mandate = mandate
        facts = gatherers.cycle_facts(mandate)(pe.NOW)
        assert facts is not None, "a position problem must never cost the whole tick its facts"
        self.facts: CycleFacts = facts

    def run(self, payload: ProposedDecision) -> Any:
        identity = queue.HarnessIdentity(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
            evidence_bundle_id=self.facts.evidence_bundle_id,
            evidence_bundle_digest=self.facts.evidence_bundle_digest,
        )
        return run_cycle(
            payload,
            session=self.session,
            mandate=self.mandate,
            identity=identity,
            facts=self.facts,
            gather_instrument=self.gatherers.instrument_facts,
            commit_before_handoff=self.session.commit,
        )


def _tick(
    session: Session,
    database: Database,
    broker: pe.Broker,
    *,
    mandate: AutonomyMandate | None = None,
    **runtime_overrides: Any,
) -> _Tick:
    mandate = mandate or _mandate()
    durable.activate(
        session,
        account_fingerprint=pe.FINGERPRINT,
        mandate=mandate,
        owner_event_id="owner-event-1",
        now=pe.NOW,
        process_generation=7,
    )
    # Committed, as production's startup activation is: the tick-start bindings read opens its
    # own session, and on an in-memory database that session shares the one connection.
    session.commit()
    runtime = pe.runtime(broker, database, **runtime_overrides)
    gatherers = BackendGatherers(
        runtime, pe.FINGERPRINT, 7, mandate=mandate, mandate_digest="d" * 64
    )
    return _Tick(session, gatherers, mandate)


def _cycle_records(session: Session) -> list[dict[str, Any]]:
    stream = durable.stream_for("autonomy.cycles", pe.FINGERPRINT)
    rows = session.query(HashChainRow).filter(HashChainRow.stream == stream).all()
    return [json.loads(row.payload_json) for row in rows]


def _spy_held(session: Session, quantity: str = "10") -> pe.Broker:
    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    row = (pe.held("SPY", quantity),)
    return pe.Broker(reads=[row, row])


# ---------------------------------------------------------------- the positive control


def test_a_held_managed_position_sizes_the_close_and_reaches_handoff_in_shadow(
    session: Session, database: Database
) -> None:
    """Kimi's D-17 probe shape, with a broker double that returns the row: HANDOFF, nothing sent."""

    broker = _spy_held(session)
    outcome = _tick(session, database, broker).run(_exit())

    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "NO_SUBMISSION_CONFIGURED"
    assert outcome.handoff is None  # nothing was handed to any order plane
    assert outcome.intent is not None
    assert outcome.intent.intent is OrderIntent.CLOSE_LONG_STOCK
    assert outcome.intent.contract.con_id == 111
    assert outcome.intent.quantity == Decimal(10)
    assert broker.position_calls == 2  # two reads, one bracket
    binding = outcome.sizing.binding_constraint
    assert binding.startswith("position held 10 (")
    assert "con_id 111" in binding and "STK" in binding
    assert f"account {pe.FINGERPRINT[:12]}" in binding
    assert pe.ACCOUNT not in " ".join(outcome.sizing.constraints)


def test_the_chained_cycle_record_carries_the_position_provenance(
    session: Session, database: Database
) -> None:
    _tick(session, database, _spy_held(session)).run(_exit())
    (record,) = _cycle_records(session)
    evidence = dict(record["position_evidence"])
    observed_at = evidence.pop("observed_at")
    assert datetime.fromisoformat(observed_at).tzinfo is not None  # read time, UTC
    assert evidence == {
        "source": "broker.positions",
        "account_fingerprint": pe.FINGERPRINT,
        "con_id": 111,
        "security_type": "STK",
        "quantity": "10",
        "read_session_id": "sess-d54",
        "read_generation": 0,
        "reads_agree": True,
        "refusal": None,
        "detail": "",
    }
    assert pe.ACCOUNT not in json.dumps(record)


@pytest.mark.parametrize(
    ("reads", "refusal"),
    [
        ([(pe.held("SPY", "10"),), (pe.held("SPY", "6"),)], "POSITION_READS_DISAGREE"),
        ([(), ()], "POSITION_FLAT"),
    ],
    ids=["gather-refusal", "loop-refusal"],
)
def test_a_refused_exits_chained_record_carries_its_position_evidence(
    session: Session, database: Database, reads: list[Any], refusal: str
) -> None:
    """D54-FIX1 P2-4 (threat map §3.5, "the reason when unknown"): kills MF29.

    Both refusal sources reach the record: one typed by the gather, one by the loop.
    """

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    _tick(session, database, pe.Broker(reads=reads)).run(_exit())
    (record,) = _cycle_records(session)
    assert record["refusal"] == refusal
    evidence = record["position_evidence"]
    assert evidence["refusal"] == refusal
    assert evidence["detail"]
    assert evidence["con_id"] == 111
    assert evidence["read_generation"] == 0
    assert evidence["account_fingerprint"] == pe.FINGERPRINT


def test_an_opening_cycle_record_is_unchanged_by_d54(session: Session, database: Database) -> None:
    """Negative control: a decision that gathered no position evidence writes no new key."""

    from chronos.autonomy import ProposedDecision as Proposal

    broker = pe.Broker()
    tick = _tick(session, database, broker)
    opening = tick.run(
        Proposal(
            kind=DecisionKind.OPEN,
            asset_class=TradableAssetClass.EQUITY,
            symbol="SPY",
            requested_strategy=StrategyForm.LONG_EQUITY,
            requested_quantity=Decimal(5),
            evidence=(
                EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=pe.NOW, digest="c" * 64),
            ),
            invalidation_conditions=("closes below 400",),
        )
    )
    # Pre-existing and unchanged: production gathers no exposure evidence, so this mandate's
    # exposure ceilings refuse an OPEN at sizing. What D-54 must not do is read positions for it.
    assert opening.stage is CycleStage.SIZING
    assert "no evidence for" in opening.detail
    assert broker.position_calls == 0
    (record,) = _cycle_records(session)
    assert "position_evidence" not in record


# ---------------------------------------------------------------- Q2: sign


def test_a_short_held_against_an_equity_close_is_refused_not_sold(
    session: Session, database: Database
) -> None:
    """Threat map probe 2a through production: -10 held, EQUITY CLOSE, must not compile SELL 10."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    row = (pe.held("SPY", "-10"),)
    outcome = _tick(session, database, pe.Broker(reads=[row, row])).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_SIGN_MISMATCH"
    assert "position held is short (-10)" in outcome.detail
    assert outcome.intent is None


# ---------------------------------------------------------------- Q1: identity per decision


def test_two_symbols_in_one_batch_each_size_from_their_own_contract(
    session: Session, database: Database
) -> None:
    """Probe 2c: the tick's probe symbol is SPY; a QQQ CLOSE must size from QQQ's row, not SPY's."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    pe.bind_managed(session, con_id=222, symbol="QQQ", tag="C")
    rows = (pe.held("SPY", "10"), pe.held("QQQ", "3"))
    tick = _tick(session, database, pe.Broker(reads=[rows]))
    spy = tick.run(_exit(symbol="SPY", target="CHR-ORD-" + "1" * 32))
    qqq = tick.run(_exit(symbol="QQQ", target="CHR-ORD-" + "2" * 32))
    assert (spy.intent.contract.con_id, spy.intent.quantity) == (111, Decimal(10))
    assert (qqq.intent.contract.con_id, qqq.intent.quantity) == (222, Decimal(3))


def test_a_position_matched_by_symbol_but_not_by_con_id_is_not_the_position(
    session: Session, database: Database
) -> None:
    """Threat map (h): the SPY a row names must be the qualified SPY contract, by con_id."""

    from chronos.domain.models import UnderlyingContract

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    other_listing = BrokerPosition(
        account_id=pe.ACCOUNT,
        contract=UnderlyingContract(con_id=999, symbol="SPY"),
        quantity=Decimal(10),
        average_cost=Decimal(390),
    )
    outcome = _tick(session, database, pe.Broker(reads=[(other_listing,), (other_listing,)])).run(
        _exit()
    )
    assert outcome.refusal == "POSITION_FLAT"


def test_a_row_for_another_account_is_not_this_accounts_position(
    session: Session, database: Database
) -> None:
    """D54-FIX1 P2-1 (review probe X1): the identity is (account, con_id, security type).

    The official adapter filters rows to the configured account, but the gatherer must not rely on
    the layer below: a row for another account with the same con_id is refused, typed, and never
    attributed to this install's fingerprint.
    """

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    foreign = BrokerPosition(
        account_id="DU9999999",
        contract=pe.CONTRACTS["SPY"],
        quantity=Decimal(10),
        average_cost=Decimal(390),
    )
    outcome = _tick(session, database, pe.Broker(reads=[(foreign,), (foreign,)])).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_ACCOUNT_MISMATCH"
    assert outcome.intent is None
    assert "DU9999999" not in outcome.detail and pe.ACCOUNT not in outcome.detail
    (record,) = _cycle_records(session)
    assert record["position_evidence"]["refusal"] == "POSITION_ACCOUNT_MISMATCH"
    assert record["position_evidence"]["quantity"] is None
    assert "DU9999999" not in json.dumps(record)


def test_a_blank_tick_account_is_no_account_even_against_a_blank_row(
    session: Session, database: Database
) -> None:
    """D54-FIX2 (delta review P3-2): the evidence layer never attributes a figure to a blank identity.

    Unreachable through typed rows (``BrokerPosition`` rejects a blank account), so the row is built
    with ``model_construct``. Without the check, blank == blank passed the account comparison and
    the cycle was refused only later, at COMPILATION, by the intent model.
    """

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    blank = BrokerPosition.model_construct(
        account_id="",
        contract=pe.CONTRACTS["SPY"],
        quantity=Decimal(10),
        average_cost=Decimal(390),
        market_price=None,
        unrealized_pnl=None,
    )
    broker = pe.Broker(reads=[(blank,), (blank,)], summary_account="")
    outcome = _tick(session, database, broker).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_ACCOUNT_MISMATCH"
    assert outcome.intent is None


# ---------------------------------------------------------------- Q3: managed only


@pytest.mark.parametrize(
    "binding",
    [
        None,
        {"status": "CANCELLED"},
        {"fingerprint": "f" * 64},
        {"open_close_effect": "CLOSE"},
        {"con_id": 333},
    ],
    ids=["no-binding", "intent-not-filled", "other-account", "not-an-opening", "other-contract"],
)
def test_an_exit_on_a_contract_no_filled_chronos_opening_holds_is_refused(
    session: Session, database: Database, binding: dict[str, Any] | None
) -> None:
    if binding is not None:
        pe.bind_managed(session, **{"con_id": 111, "symbol": "SPY", "tag": "B", **binding})
    row = (pe.held("SPY", "10"),)
    broker = pe.Broker(reads=[row, row])
    outcome = _tick(session, database, broker).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_NOT_MANAGED"
    assert outcome.intent is None


# ---------------------------------------------------------------- Q4: the bracket


def test_a_read_that_raises_is_a_typed_refusal_for_that_decision_only(
    session: Session, database: Database
) -> None:
    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")

    def boom(call: int) -> None:
        raise ConnectionError("synthetic: positions request did not complete")

    outcome = _tick(session, database, pe.Broker(on_read=boom)).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_READ_FAILED"
    assert "ConnectionError" in outcome.detail
    assert "synthetic" not in outcome.detail  # the type, never the message


def test_a_connection_close_between_the_reads_reads_as_unknown_not_flat(
    session: Session, database: Database
) -> None:
    """Probe 3 at the gather: the close invalidates readiness; the truncated read is not used."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    readiness = ReconciliationReadiness(session_id="sess-d54")

    def close(call: int) -> None:
        if call == 2:
            readiness.invalidate("official IBKR connection closed")

    row = (pe.held("SPY", "10"),)
    broker = pe.Broker(reads=[row, ()], on_read=close)
    outcome = _tick(session, database, broker, readiness=readiness).run(_exit())
    assert outcome.refusal == "POSITION_READINESS_MOVED"
    assert outcome.refusal != "POSITION_FLAT"


def test_a_reconciliation_pass_starting_between_the_reads_refuses(
    session: Session, database: Database
) -> None:
    """(i)/(j): the generation moves without an uncertainty (a routine pass begins)."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    readiness = ReconciliationReadiness(session_id="sess-d54")

    def begin(call: int) -> None:
        if call == 2:
            readiness.begin_reconciliation("periodic reconciliation")

    row = (pe.held("SPY", "10"),)
    outcome = _tick(
        session, database, pe.Broker(reads=[row, row], on_read=begin), readiness=readiness
    ).run(_exit())
    assert outcome.refusal == "POSITION_READINESS_MOVED"
    assert "generation" in outcome.detail


def test_a_reconciliation_pass_starting_during_the_first_read_refuses(
    session: Session, database: Database
) -> None:
    """D54-FIX1 P2-2: the bracket's LEFT edge; the snapshot is taken before read 1 (kills MF14)."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    readiness = ReconciliationReadiness(session_id="sess-d54")

    def begin(call: int) -> None:
        if call == 1:
            readiness.begin_reconciliation("periodic reconciliation")

    row = (pe.held("SPY", "10"),)
    outcome = _tick(
        session, database, pe.Broker(reads=[row, row], on_read=begin), readiness=readiness
    ).run(_exit())
    assert outcome.refusal == "POSITION_READINESS_MOVED"
    assert "generation moved from 0 to 1" in outcome.detail


class _SessionSwap:
    """A latch whose session id changes between the two snapshots (a process restart)."""

    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> ReconciliationReadinessSnapshot:
        self.calls += 1
        return ReconciliationReadinessSnapshot(
            status=ReconciliationStatus.MANUAL_REVIEW,
            session_id="sess-a" if self.calls == 1 else "sess-b",
            generation=4,
            reason="a nonzero broker position",
        )


def test_a_readiness_session_change_between_the_reads_refuses(
    session: Session, database: Database
) -> None:
    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    row = (pe.held("SPY", "10"),)
    outcome = _tick(session, database, pe.Broker(reads=[row, row]), readiness=_SessionSwap()).run(
        _exit()
    )
    assert outcome.refusal == "POSITION_READINESS_MOVED"
    assert "session" in outcome.detail


def test_a_manual_review_latch_does_not_refuse_the_exit(
    session: Session, database: Database
) -> None:
    """Q4 positive: a held position is MANUAL_REVIEW by construction; that alone never refuses."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    readiness = ReconciliationReadiness(session_id="sess-d54")
    generation = readiness.begin_reconciliation("pass")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.MANUAL_REVIEW,
        reason="A nonzero broker position lacks complete cycle and fill-allocation provenance.",
    )
    assert readiness.snapshot().ready is False
    row = (pe.held("SPY", "10"),)
    outcome = _tick(session, database, pe.Broker(reads=[row, row]), readiness=readiness).run(
        _exit()
    )
    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.intent.quantity == Decimal(10)


def test_two_reads_that_disagree_refuse(session: Session, database: Database) -> None:
    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    broker = pe.Broker(reads=[(pe.held("SPY", "10"),), (pe.held("SPY", "6"),)])
    outcome = _tick(session, database, broker).run(_exit())
    assert outcome.refusal == "POSITION_READS_DISAGREE"


def test_the_read_only_cache_adapter_is_not_position_evidence(
    session: Session, database: Database
) -> None:
    """ib_async's ``positions()`` is a client cache an assignment never updates (threat map (r))."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    row = (pe.held("SPY", "10"),)
    broker = pe.Broker(reads=[row, row])
    outcome = _tick(session, database, broker, broker_adapter=BrokerAdapter.IB_ASYNC).run(_exit())
    assert outcome.refusal == "POSITION_SOURCE_CACHED"
    assert broker.position_calls == 0


def test_the_demo_adapter_is_read_as_its_fixture(session: Session, database: Database) -> None:
    """Control for the cache refusal: in DEMO mode the IB_ASYNC adapter setting is not consulted."""

    broker = _spy_held(session)
    outcome = _tick(
        session,
        database,
        broker,
        broker_mode=BrokerMode.DEMO,
        broker_adapter=BrokerAdapter.IB_ASYNC,
    ).run(_exit())
    assert outcome.stage is CycleStage.HANDOFF


@pytest.mark.parametrize(
    ("connected", "in_scope", "expected"),
    [
        (False, True, "the broker connection is closed"),
        (True, False, "outside the broker's managed accounts"),
    ],
)
def test_a_tick_that_starts_disconnected_or_out_of_scope_blocks_even_a_close(
    session: Session, database: Database, connected: bool, in_scope: bool, expected: str
) -> None:
    """Q4 tick-wide: a blocking DegradedReason, so admission refuses DEGRADED_STATE."""

    broker = _spy_held(session)
    broker.connected = connected
    broker.account_in_scope = in_scope
    tick = _tick(session, database, broker)
    (reason,) = tick.facts.degraded_reasons
    assert reason.blocks_risk_reduction is True
    assert expected in str(reason)
    outcome = tick.run(_exit())
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.refusal == "DEGRADED_STATE"
    assert broker.position_calls == 0


def test_a_tick_that_starts_connected_and_in_scope_is_not_degraded(
    session: Session, database: Database
) -> None:
    tick = _tick(session, database, _spy_held(session))
    assert tick.facts.degraded_reasons == ()


# ---------------------------------------------------------------- figures


def test_a_non_finite_figure_is_refused_typed_at_the_gather(
    session: Session, database: Database
) -> None:
    """(o): NaN never reaches sizing's arithmetic catch, and the refusal says why."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    nan = BrokerPosition.model_construct(
        account_id=pe.ACCOUNT,
        contract=pe.CONTRACTS["SPY"],
        quantity=Decimal("NaN"),
        average_cost=Decimal(390),
        market_price=None,
        unrealized_pnl=None,
    )
    outcome = _tick(session, database, pe.Broker(reads=[(nan,), (nan,)])).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_NOT_FINITE"
    assert outcome.sizing is None


def test_an_implausibly_large_figure_is_refused_not_clamped(
    session: Session, database: Database
) -> None:
    """Q7: 1E+30 shares would be clamped to max_shares_per_order 100 and look legitimate."""

    outcome = _tick(session, database, _spy_held(session, "1E+30")).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_IMPLAUSIBLE"
    assert outcome.intent is None


def test_a_figure_just_under_twice_net_liquidation_still_sizes(
    session: Session, database: Database
) -> None:
    """Q7 boundary control: 499 x 400.00 = 199,600 <= 2 x 100,000."""

    outcome = _tick(session, database, _spy_held(session, "499")).run(_exit())
    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.intent.quantity == Decimal(25)  # max_order_notional_usd 10,000 / 400


def test_a_figure_just_over_twice_net_liquidation_is_refused(
    session: Session, database: Database
) -> None:
    """D54-FIX1 P2-3, Q7's upper edge: 501 x 400.00 = 200,400 > 2 x 100,000 (kills MF31)."""

    outcome = _tick(session, database, _spy_held(session, "501")).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_IMPLAUSIBLE"
    assert "worth 200400" in outcome.detail


def test_absent_after_a_complete_read_is_flat_and_refused(
    session: Session, database: Database
) -> None:
    """(q): no row after a complete, agreeing read is flat — nothing to close."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    outcome = _tick(session, database, pe.Broker(reads=[(), ()])).run(_exit())
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "POSITION_FLAT"


def test_a_row_of_another_security_type_on_the_contracts_con_id_is_ambiguous(
    session: Session, database: Database
) -> None:
    """D54-FIX1 P3-1: the security-type half of the identity check (kills MF1).

    IB con_ids are unique across types, so no adapter is known to deliver this; the pin keeps
    the check from being dropped as dead code.
    """

    from datetime import date

    from chronos.domain.enums import OptionRight
    from chronos.domain.models import OptionContract

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    option = BrokerPosition(
        account_id=pe.ACCOUNT,
        contract=OptionContract(
            con_id=111,
            symbol="SPY",
            expiration=date(2026, 8, 21),
            strike=Decimal(400),
            right=OptionRight.PUT,
            multiplier=Decimal(100),
            trading_class="SPY",
            local_symbol="SPY 260821P00400000",
        ),
        quantity=Decimal(-1),
        average_cost=Decimal(390),
    )
    outcome = _tick(session, database, pe.Broker(reads=[(option,), (option,)])).run(_exit())
    assert outcome.refusal == "POSITION_IDENTITY_AMBIGUOUS"


def test_a_managed_opening_of_another_product_family_does_not_cover_the_contract(
    session: Session, database: Database
) -> None:
    """D54-FIX1 P3-1: the product-family half of the Q3 key (kills MF34)."""

    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B", product_family="OPTION")
    outcome = _tick(session, database, pe.Broker(reads=[(pe.held("SPY", "10"),)])).run(_exit())
    assert outcome.refusal == "POSITION_NOT_MANAGED"


def test_two_rows_for_one_contract_are_ambiguous(session: Session, database: Database) -> None:
    pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
    rows = (pe.held("SPY", "10"), pe.held("SPY", "4"))
    outcome = _tick(session, database, pe.Broker(reads=[rows, rows])).run(_exit())
    assert outcome.refusal == "POSITION_IDENTITY_AMBIGUOUS"


# ---------------------------------------------------------------- Q5, (f)


def test_a_reduce_with_no_quantity_is_refused_typed_in_the_cycle(
    session: Session, database: Database
) -> None:
    outcome = _tick(session, database, _spy_held(session)).run(_exit(DecisionKind.REDUCE))
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "REDUCE_QUANTITY_REQUIRED"
    assert outcome.detail == "a REDUCE must say how much; use CLOSE"


def test_a_reduce_with_a_quantity_sizes_to_it(session: Session, database: Database) -> None:
    outcome = _tick(session, database, _spy_held(session)).run(
        _exit(DecisionKind.REDUCE, quantity=Decimal(4))
    )
    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.intent.quantity == Decimal(4)


def test_a_second_exit_on_the_same_contract_in_one_tick_is_refused(
    session: Session, database: Database
) -> None:
    """(f): two CLOSEs with distinct target refs fork the decision id; netting is owner-gated.

    The first sizes from the tick's read. The second cannot be bounded by what remains without
    netting the first, so it refuses, typed. A later tick reads the broker afresh.
    """

    broker = _spy_held(session)
    tick = _tick(session, database, broker)
    first = tick.run(_exit(target="CHR-ORD-" + "1" * 32))
    second = tick.run(_exit(target="CHR-ORD-" + "2" * 32))
    assert first.intent.quantity == Decimal(10)
    assert second.stage is CycleStage.SIZING
    assert second.refusal == "POSITION_EXIT_ALREADY_SIZED_THIS_TICK"
    assert second.intent is None

    next_tick = _Tick(session, tick.gatherers, tick.mandate)
    third = next_tick.run(_exit(target="CHR-ORD-" + "3" * 32))
    assert third.intent is not None


# ---------------------------------------------------------------- sources it must not read


def test_the_gatherer_reads_no_record_only_store_and_no_model_fact() -> None:
    """Q1 / K3(a): the reconciled store, fills, order events and PositionFact are never inputs."""

    source = inspect.getsource(BackendGatherers)
    for name in (
        "PositionFact",
        "ReconciliationRunRow",
        "PositionProvenanceRow",
        "FillRow",
        "OrderEventRow",
        "position_provenance",
        "broker_snapshot",
    ):
        assert name not in source, name


# ---------------------------------------------------------------- the submitting-mode answer


def test_a_submitting_mode_still_refuses_a_held_position_at_reconciled_proven_state() -> None:
    """Owner residual (Q4), stated not changed: D-54 does not make an exit executable.

    A held position keeps the wheel reconciler at MANUAL_REVIEW (``services/reconciliation.py``:
    "A nonzero broker position lacks complete cycle and fill-allocation provenance."), and the order
    plane fails ``reconciled_proven_state`` on anything that is not RECONCILED. So in PAPER/LIVE the
    exit D-54 sizes is refused by the order plane (``RiskRefusedByOrderPlane``); only SHADOW reaches
    HANDOFF.
    """

    engine = OrderRiskEngine(paper_settings(), persistence_key=TEST_PERSISTENCE_KEY)
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)  # a Friday, regular session
    intent = build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_LONG_STOCK,
        contract=stock_contract(symbol="SPY", con_id=111),
        quantity=10,
        limit_price=Decimal("399.98"),
        correlation_id="CHR-ORD-" + "D" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=AccountSummary(
                account_id=PAPER_ACCOUNT,
                net_liquidation=Decimal(100_000),
                total_cash=Decimal(60_000),
                buying_power=Decimal(60_000),
                as_of=now,
            ),
            reconciliation_status=ReconciliationStatus.MANUAL_REVIEW,
            session=session_for(ProductFamily.STOCK, now=now, broker_confirms_open=True),
        ),
        now=now,
    )
    check = next(item for item in decision.checks if item.name == "reconciled_proven_state")
    assert check.status is RiskCheckStatus.FAIL
    assert decision.overall is RiskCheckStatus.FAIL

"""End-to-end order lifecycle on simulated flow (SIM-HARNESS U3).

Drives the REAL order plane -- ``OrderManagementService``, ``OrderSubmissionBoundary``,
``OrderRiskEngine`` fed by the real ``BrokerRiskEvidenceProvider``, the modification and
cancellation services, ``OrderRestartReconciler`` and ``ReconciliationCoordinator`` --
against ``tests.support.sim_broker.SimBroker`` with ``paper_settings()``.

What ``paper_settings()`` is, stated once (survey review P3-4): a ``Settings`` object
built in process with ``allow_order_transmit=True`` and ``_env_file=None``. Nothing
here reads or writes the environment or any ``.env``; every real configuration keeps
``ALLOW_ORDER_TRANSMIT`` and ``ALLOW_LIVE_TRADING`` false, and the demo-shaped control
at the end of this module (the sibling of ``test_order_pipeline.py``'s
``test_non_paper_settings_refuse_transmission``) proves a default ``Settings`` refuses
to transmit even against a broker that would accept. No gateway, credential or venue
exists anywhere in these tests.

How this differs from ``test_order_pipeline.py``: fills are never fed to the tracker by
hand. ``SimBroker`` fills on advanced bars, and the real ``OrderRestartReconciler``
proves the result from the broker's open orders and executions. Risk evidence comes
from the real provider reading the simulator's account and positions, so the next
proposal sees the position the last fill created.

Every case names, in a comment, a change that makes it fail; none can pass vacuously.
SimBroker fidelity gaps found while writing these cases are recorded in the unit's
RESULT.md, never patched here (U2 r1 owns SimBroker).

S2 (approved round 3, Kevin ~01:28 EDT 2026-10-12): every case now runs over TWO venues,
parametrized by the ``venue`` fixture:

* ``sim`` -- SimBroker driven directly, exactly as before;
* ``official_fake`` -- the REAL, unmodified ``OfficialIBKRBroker`` (its ``_App``, its
  ``CallbackBridge``, its normalizers) over S1b's in-process FakeGateway, whose venue is
  a SimBroker shaped like the ``sim`` one, so both venues are judged on the same book.
  ``ALLOW_ORDER_TRANSMIT=true`` exists ONLY in the ``Settings`` object built in process
  by :func:`official_transmit_settings` for this venue -- never in the environment, a
  ``.env`` or any runtime configuration (``test_transmit_true_lives_only_in_the_test_
  settings_object`` proves it before, during and after a run) -- and the only wire it
  reaches is the FakeGateway: the installer forbids every socket.

Where the real adapter differs from SimBroker BY DESIGN, the difference is a strict
xfail with an ``S2-D`` id in :data:`KNOWN_DIFFERENCES`; a real-adapter FAILURE would be
an ``S2-F`` finding, never a ``src`` fix. The official venue's order ids are the
adapter's own (a what-if preview consumes one too), so the cases read them from the
submission rather than assuming SimBroker's 1001.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.support import fake_ibapi
from tests.support.fake_ibapi import FakeContract, FakeEClient, FakeGateway, FakeOrder
from tests.support.order_fakes import PAPER_ACCOUNT, paper_settings
from tests.support.sim_broker import FillPlan, SimBroker, SimClock, _SeedGuard

from chronos.broker import official_ibkr
from chronos.broker.base import Broker, BrokerRefusedBeforeSend
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import (
    BrokerAdapter,
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    ReconciliationStatus,
)
from chronos.domain.models import UnderlyingContract
from chronos.marketdata.bars import Bar, BarInterval
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import WheelOrderIntent, build_stock_intent
from chronos.orders.kill_switch import LiveKillSwitch
from chronos.orders.mutations import OrderCancellationService, OrderModificationService
from chronos.orders.preview import OrderPreviewService
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import OrderRestartReconciler
from chronos.orders.risk import OrderRiskEngine
from chronos.orders.service import OrderManagementService, ProposeResult
from chronos.orders.submission import OrderSubmissionBoundary, SubmissionRefusalCode
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import ExecutionRepository
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskDecisionRepository,
)
from chronos.persistence.repositories import LocalReconciliationRepository
from chronos.persistence.schema import FillRow
from chronos.services.reconciliation import ReconciliationCoordinator

#: Monday 2026-10-12, 10:30 New York: inside regular trading hours.
T0 = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
#: The venue's own session evidence for that day, in IBKR's ``liquidHours`` shape; the
#: real evidence provider reads it off the qualified contract (R-26).
LIQUID_HOURS = ("20261012:0930-20261012:1600", "America/New_York")
SYMBOL = "AAPL"
CON_ID = 265598
#: The FakeGateway's registry seeds AAPL and MSFT by name, so the venue knows both.
SYMBOLS = {SYMBOL: CON_ID, "MSFT": 272093}
ONE_MINUTE = timedelta(minutes=1)

VENUES = ("sim", "official_fake")
#: The real adapter waits this long for an acknowledgement; the fake answers synchronously,
#: so only the deliberately silent (ambiguous) send ever waits it out. A test-only patch.
OFFICIAL_ACK_TIMEOUT_S = 0.3


@dataclass(frozen=True)
class _Venue:
    kind: str
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path

    @property
    def account(self) -> str:
        # The FakeGateway reports its own seed account to the adapter (managedAccounts,
        # account_summary, positions), so the official venue's book is kept under it.
        return PAPER_ACCOUNT if self.kind == "sim" else fake_ibapi.ACCOUNT


@pytest.fixture(params=VENUES)
def venue(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> _Venue:
    return _Venue(kind=str(request.param), monkeypatch=monkeypatch, tmp_path=tmp_path)


def official_transmit_settings(**overrides: object) -> Settings:
    """The ONLY place ``allow_order_transmit=True`` exists for the real adapter (S2, Kevin
    round 3): an in-process ``Settings`` for the FakeGateway's seed account and client id,
    paper environment on the paper port, ``ALLOW_LIVE_TRADING`` false. Never written
    anywhere; the socket tripwire installed with the gateway makes a real wire impossible.
    """

    base: dict[str, object] = {
        "broker_adapter": BrokerAdapter.OFFICIAL_IBKR,
        "ib_host": "127.0.0.1",
        "ib_port": 7497,
        "ib_client_id": fake_ibapi.CLIENT_ID,
        "ib_account_id": fake_ibapi.ACCOUNT,
        "ib_account_allowlist": (fake_ibapi.ACCOUNT,),
    }
    base.update(overrides)
    return paper_settings(**base)


#: (venue, case) -> (id, exception the venue raises instead, why). Strict xfails: the day
#: the difference disappears, the case XPASSes and fails the suite until the row is removed.
KNOWN_DIFFERENCES: dict[tuple[str, str], tuple[str, type[BaseException], str]] = {
    (
        "official_fake",
        "test_modify_when_the_sim_allows_it_reprices_the_working_order_and_reconciles",
    ): (
        "S2-D1",
        BrokerRefusedBeforeSend,
        "OfficialIBKRBroker.modify_order refuses modify-in-place before send BY DESIGN "
        "(official_ibkr.py: 'not implemented for the official adapter in M7; cancel and "
        "re-submit'); U1's contract pins that refusal. SimBroker's opt-in replace has no "
        "counterpart in the adapter, so this sim-only capability case cannot pass there.",
    ),
}


@pytest.fixture(autouse=True)
def _known_difference(request: pytest.FixtureRequest) -> None:
    """Turn a registered (venue, case) pair into a strict xfail (the contract suite's pattern)."""

    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return
    key = (str(callspec.params.get("venue")), request.node.originalname)
    entry = KNOWN_DIFFERENCES.get(key)
    if entry is None:
        return
    finding, raises, why = entry
    request.node.add_marker(
        pytest.mark.xfail(strict=True, raises=(raises, AssertionError), reason=f"{finding}: {why}")
    )


class _LifecycleGateway(FakeGateway):
    """S1b's FakeGateway over a U3-shaped SimBroker, plus the two controls these cases need.

    * **Silent acknowledgements** (``silent_refs``): the venue records the order but the
      client hears nothing -- the ambiguous send, at the wire where it really happens, so
      the adapter times out and reports SUBMISSION_UNKNOWN with its allocated id.
    * **Bar-driven fills streamed live**: :meth:`advance_bar` lets SimBroker match a bar
      exactly as the ``sim`` venue does, then streams every resulting execution to each
      attached client in IB's order (``orderStatus``, ``execDetails(-1)``,
      ``commissionReport``) and returns only after the last callback was dispatched -- the
      same shape as ``FakeGateway.fill_sim_order``, driven by a bar instead of a quantity.
    """

    def __init__(self, sim: SimBroker, *, silent_refs: set[str]) -> None:
        super().__init__(sim, client_of={})
        self.silent_refs = set(silent_refs)

    def place_order(
        self, client: FakeEClient, adapter_id: int, contract: FakeContract, order: FakeOrder
    ) -> None:
        if order.whatIf or str(order.orderRef) not in self.silent_refs:
            super().place_order(client, adapter_id, contract, order)
            return
        with self._lock:
            self._next_valid_id = max(self._next_valid_id, adapter_id + 1)
            submission = self._run(
                self.sim.submit_order(
                    self._request_of(contract, order, transmit=True), send_guard=_SeedGuard()
                )
            )
            sim_id: int = submission.broker_order_id
            self._client_of[sim_id] = int(client.client_id or 0)
            self._adapter_of[sim_id] = adapter_id
            self._sim_of[adapter_id] = sim_id
            # ... and nothing is emitted: the acknowledgement never reaches the client.

    def advance_bar(self, bar: Bar) -> None:
        with self._lock:
            before = {e.execution_id for e in self._run(self.sim.executions())}
            self.sim.advance_bar(bar)
            new = [e for e in self._run(self.sim.executions()) if e.execution_id not in before]
            working = {o.broker_order_id: o for o in self._run(self.sim.open_orders())}
            clients = list(self._clients)
            for sim_id in sorted({e.broker_order_id for e in new}):
                adapter_id = self._adapter_of.get(sim_id, sim_id)
                order = working.get(sim_id)
                fills = [e for e in new if e.broker_order_id == sim_id]
                for client in clients:
                    if order is not None:
                        self._emit_status(client, order, adapter_id)
                    else:
                        filled = sum((e.quantity for e in fills), Decimal(0))
                        client.emit(
                            "orderStatus",
                            adapter_id,
                            "Filled",
                            float(filled),
                            0.0,
                            0.0,
                            0,
                            0,
                            0.0,
                            self._client_of.get(sim_id, fake_ibapi.MANUAL_CLIENT_ID),
                            "",
                            0.0,
                        )
                    for execution in fills:
                        client.emit(
                            "execDetails",
                            -1,
                            self.contract_of(execution.contract),
                            self._execution_of(execution),
                        )
                        client.emit("commissionReport", self._commission_of(execution))
        for client in clients:
            client.wait_dispatched()


def _ref(letter: str) -> str:
    return "CHR-ORD-" + letter * 32


class _KillSwitch:
    """A last-line send gate, like the live kill switch the official adapter nests."""

    def __init__(self, *, engaged: bool) -> None:
        self.engaged = engaged
        self.entered = 0

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        self.entered += 1
        yield not self.engaged


class _SimHarness:
    """The real services wired to one venue, exactly as the pipeline tests wire FakeBroker.

    ``venue.kind == "sim"``: the broker IS the SimBroker. ``"official_fake"``: the broker is
    the real ``OfficialIBKRBroker`` and the SimBroker is the FakeGateway's venue behind it;
    the kill switch is then the adapter's own ``LiveKillSwitch``, not SimBroker's gate.
    """

    def __init__(
        self,
        venue: _Venue,
        *,
        settings: Settings | None = None,
        plans: dict[str, FillPlan] | None = None,
        allow_modify: bool = False,
        kill_switch_engaged: bool = False,
        silent_refs: tuple[str, ...] = (),
    ) -> None:
        self.venue = venue
        self.kind = venue.kind
        self.account = venue.account
        self.clock = SimClock(T0)
        self.readiness = ReconciliationReadiness()
        self.submitted_ids: list[int] = []
        self.sim_kill_switch: _KillSwitch | None = None
        self.kill_switch: LiveKillSwitch | None = None
        self.gateway: _LifecycleGateway | None = None
        if self.kind == "sim":
            self.settings = settings or paper_settings()
            self.sim_kill_switch = _KillSwitch(engaged=True) if kill_switch_engaged else None
            self.sim = SimBroker(
                account_id=self.account,
                clock=self.clock,
                symbols=SYMBOLS,
                starting_cash=Decimal("100000"),
                client_id=self.settings.ib_client_id,
                plans=plans,
                allow_modify=allow_modify,
                kill_switch=self.sim_kill_switch,
                session_hours={SYMBOL: LIQUID_HOURS},
            )
            self.broker: Broker = self.sim
        else:
            self.settings = settings or official_transmit_settings()
            self.sim = SimBroker(
                account_id=self.account,
                clock=self.clock,
                symbols=SYMBOLS,
                starting_cash=Decimal("100000"),
                client_id=self.settings.ib_client_id,
                plans=plans,
                allow_modify=allow_modify,
                session_hours={SYMBOL: LIQUID_HOURS},
            )
            self.gateway = _LifecycleGateway(self.sim, silent_refs=set(silent_refs))
            # The loaders and the socket tripwire, for this test only (S1b's seam).
            fake_ibapi.install(venue.monkeypatch, self.gateway)
            venue.monkeypatch.setattr(official_ibkr, "_REQUEST_TIMEOUT_S", OFFICIAL_ACK_TIMEOUT_S)
            if kill_switch_engaged:
                self.kill_switch = LiveKillSwitch(venue.tmp_path / "live_kill_switch.json")
                self.kill_switch.engage(
                    reason="S2 case: engaged before the send", initiated_by="test", now=T0
                )
            self.broker = OfficialIBKRBroker(
                self.settings,
                live_kill_switch=self.kill_switch,
                on_connection_uncertain=self.readiness.invalidate,
            )
        self.db = Database("sqlite:///:memory:")
        self.db.initialize()
        self.db.bind_scope(broker_mode="ibkr", environment="paper", account_id=self.account)
        self.connection = BrokerConnectionManager(self.broker, readiness=self.readiness)
        self.connection.start()
        self.connection.run(self.broker.connect())
        assert self.readiness.complete(
            expected_generation=self.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="U3 harness: broker and local state start empty and equal",
            reconciled_at=T0,
        )
        self.intents = OrderIntentRepository(self.db.sessions)
        self.tracker_repo = OrderTrackerRepository(self.db.sessions)
        self.confirmations = OrderConfirmationRepository(self.db.sessions)
        self.risk_decisions = RiskDecisionRepository(self.db.sessions)
        self.tracker = OrderTracker(self.intents, self.tracker_repo)
        boundary = OrderSubmissionBoundary(
            settings=self.settings,
            connection=self.connection,
            intents=self.intents,
            confirmations=self.confirmations,
            tracker=self.tracker_repo,
            reconciliation_readiness=self.readiness,
        )
        self.service = OrderManagementService(
            settings=self.settings,
            environment=IBEnvironment.PAPER,
            account_id=self.account,
            # The REAL provider: account, positions and open orders come from the sim.
            evidence_provider=BrokerRiskEvidenceProvider(
                connection=self.connection,
                settings=self.settings,
                reconciliation_readiness=self.readiness,
                intents=self.intents,
            ),
            risk_engine=OrderRiskEngine(self.settings),
            preview_service=OrderPreviewService(self.connection),
            submission_boundary=boundary,
            modification=OrderModificationService(
                connection=self.connection,
                intents=self.intents,
                tracker=self.tracker,
                tracker_repo=self.tracker_repo,
            ),
            cancellation=OrderCancellationService(
                connection=self.connection,
                intents=self.intents,
                tracker=self.tracker,
                tracker_repo=self.tracker_repo,
            ),
            tracker=self.tracker,
            tracker_repo=self.tracker_repo,
            reconciler=OrderRestartReconciler(
                connection=self.connection,
                intents=self.intents,
                tracker=self.tracker,
                reconciliation_readiness=self.readiness,
                expected_broker_client_id=self.settings.ib_client_id,
            ),
            intents=self.intents,
            confirmations=self.confirmations,
            risk_decisions=self.risk_decisions,
            broker_environment_is_paper=True,
        )
        self.coordinator = ReconciliationCoordinator(
            self.connection,
            LocalReconciliationRepository(self.db.sessions),
            self.settings.symbol_allowlist,
            execution_repository=ExecutionRepository(self.db.sessions),
        )

    # --- driving helpers ----------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.clock()

    def contract(self) -> UnderlyingContract:
        """The sim-qualified contract, carrying the session evidence the risk gate reads.

        Qualification is outside the FakeGateway's surface (S1a: no ``reqContractDetails``),
        so the official venue takes the contract from the venue SimBroker directly.
        """

        if self.gateway is None:
            return self.connection.run(self.broker.qualify_underlying(SYMBOL))
        contract: UnderlyingContract = self.gateway._run(self.sim.qualify_underlying(SYMBOL))
        return contract

    @property
    def sent_order_count(self) -> int:
        """Orders that crossed the venue's wire, whichever broker object sent them."""

        return self.sim.sent_order_count

    @property
    def first_id(self) -> int:
        """The broker order id of the first submission that reached the venue."""

        return self.submitted_ids[0]

    def venue_lifecycle(self, broker_order_id: int) -> OrderLifecycle:
        """The venue's own view of an order, by the id the broker object reported."""

        sim_id = broker_order_id
        if self.gateway is not None:
            sim_id = self.gateway._sim_of.get(broker_order_id, broker_order_id)
        return self.sim.order_lifecycle(sim_id)

    def transmit_disabled_settings(self) -> Settings:
        """This venue's shape of ``transmission_possible == False``."""

        if self.kind == "sim":
            return Settings(_env_file=None)  # type: ignore[call-arg]
        return official_transmit_settings(allow_order_transmit=False)

    def buy(
        self, *, quantity: int = 10, limit: str = "100.00", ref: str, intent_id: str
    ) -> WheelOrderIntent:
        return build_stock_intent(
            account_id=self.account,
            intent=OrderIntent.OPEN_LONG_STOCK,
            contract=self.contract(),
            quantity=quantity,
            limit_price=Decimal(limit),
            correlation_id=ref,
            intent_id=intent_id,
        )

    def sell(
        self, *, quantity: int, limit: str = "100.00", ref: str, intent_id: str
    ) -> WheelOrderIntent:
        return build_stock_intent(
            account_id=self.account,
            intent=OrderIntent.CLOSE_LONG_STOCK,
            contract=self.contract(),
            quantity=quantity,
            limit_price=Decimal(limit),
            correlation_id=ref,
            intent_id=intent_id,
        )

    def propose(self, intent: WheelOrderIntent) -> ProposeResult:
        return self.service.propose(intent, now=self.now)

    def confirm(self, intent: WheelOrderIntent) -> ProposeResult:
        proposal = self.propose(intent)
        assert proposal.risk.approved, [c for c in proposal.risk.checks if c.status.value != "PASS"]
        self.service.preview(intent, now=self.now)
        self.service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=self.now)
        return proposal

    def submit(self, intent: WheelOrderIntent):  # type: ignore[no-untyped-def]
        outcome = self.service.submit(intent, writer_lease_held=True, now=self.now)
        if outcome.submission is not None:
            self.submitted_ids.append(outcome.submission.broker_order_id)
        return outcome

    def bar(self, *, open_: str, high: str, low: str, close: str) -> None:
        """Advance the clock one minute and let the sim match a bar stamped now."""

        self.clock.advance(ONE_MINUTE)
        stamp = self.now
        venue = self.sim if self.gateway is None else self.gateway
        venue.advance_bar(
            Bar(
                symbol=SYMBOL,
                source="sim",
                exchange="SMART",
                interval=BarInterval.MIN_1,
                session_date=stamp.date(),
                timestamp_utc=stamp,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=1000.0,
            )
        )

    def rearm_readiness(self, reason: str) -> None:
        """Re-assert broker/local parity after a send consumed the last reconciliation."""

        assert self.readiness.complete(
            expected_generation=self.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason=reason,
            reconciled_at=self.now,
        )

    def status(self, intent_id: str) -> OrderLifecycle:
        stored = self.service.get(intent_id)
        assert stored is not None
        return stored.status

    def sim_open_order_ids(self) -> set[int]:
        return {o.broker_order_id for o in self.connection.run(self.broker.open_orders())}

    def sim_position(self) -> Decimal:
        positions = self.connection.run(self.broker.positions())
        return sum((p.quantity for p in positions if p.contract.symbol == SYMBOL), Decimal(0))

    def sim_cash(self) -> Decimal:
        return self.connection.run(self.broker.account_summary()).total_cash

    def persisted_fill_quantity(self) -> Decimal:
        with self.db.sessions() as session:
            rows = session.scalars(select(FillRow).where(FillRow.symbol == SYMBOL)).all()
            return sum((row.quantity for row in rows), Decimal(0))

    def close(self) -> None:
        self.connection.close()
        self.db.dispose()


@contextmanager
def harness(venue: _Venue, **kwargs: object) -> Iterator[_SimHarness]:
    h = _SimHarness(venue, **kwargs)  # type: ignore[arg-type]
    try:
        yield h
    finally:
        h.close()


def _cap_of_one(venue: _Venue) -> Settings:
    if venue.kind == "sim":
        return paper_settings(max_opening_orders_per_day=1)
    return official_transmit_settings(max_opening_orders_per_day=1)


#: A bar that crosses a BUY at 100.00: fills at min(open, limit) = 99.90.
CROSSING = {"open_": "99.90", "high": "100.10", "low": "99.50", "close": "99.80"}
#: A bar that stays above a 100.00 BUY: nothing fills.
ABOVE = {"open_": "100.40", "high": "100.80", "low": "100.20", "close": "100.60"}


def _fill_ten(
    h: _SimHarness, *, ref: str = _ref("A"), intent_id: str = "intent-1"
) -> WheelOrderIntent:
    """Propose, confirm, submit and fill a 10-share BUY at 100.00 (99.90 on the bar)."""

    intent = h.buy(ref=ref, intent_id=intent_id)
    h.confirm(intent)
    outcome = h.submit(intent)
    assert outcome.submitted is True, outcome.detail
    h.bar(**CROSSING)
    report = h.service.reconcile_on_restart_report(now=h.now)
    assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.FILLED]
    return intent


# --------------------------------------------------------------------------------------
# place -> fill
# --------------------------------------------------------------------------------------


def test_place_then_fill_reaches_filled_through_restart_reconciliation(venue: _Venue) -> None:
    # Fails if the boundary stops transmitting, if SimBroker stops filling on a crossing
    # bar, or if the reconciler stops proving FILLED from an execution-only snapshot.
    with harness(venue) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)

        outcome = h.submit(intent)

        assert outcome.submitted is True
        assert outcome.refusal is SubmissionRefusalCode.NOT_REFUSED
        assert h.status("intent-1") is OrderLifecycle.SUBMITTED
        assert h.sent_order_count == 1
        assert h.sim_open_order_ids() == {h.first_id}
        assert h.sim_position() == 0

        h.bar(**ABOVE)  # the limit is not crossed: still working
        assert h.venue_lifecycle(h.first_id) is OrderLifecycle.SUBMITTED
        h.bar(**CROSSING)
        assert h.venue_lifecycle(h.first_id) is OrderLifecycle.FILLED
        assert h.sim_open_order_ids() == set()
        # The services learn of the fill only by reconciling against the broker.
        assert h.status("intent-1") is OrderLifecycle.SUBMITTED

        report = h.service.reconcile_on_restart_report(now=h.now)

        assert report.unresolved == ()
        assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.FILLED]
        assert report.applied_updates[0].filled_quantity == Decimal("10")
        assert h.status("intent-1") is OrderLifecycle.FILLED
        assert h.sim_position() == Decimal("10")
        # 10 x 99.90 plus the 1.00 minimum commission, by hand.
        assert h.sim_cash() == Decimal("100000") - Decimal("999.00") - Decimal("1.00")
        assert h.sent_order_count == 1  # reconciliation never re-sends


def test_the_next_proposal_sees_the_position_the_fill_created(venue: _Venue) -> None:
    # Fails if the real evidence provider stops reading the sim's positions, or if the
    # stock close check stops comparing against settled shares.
    with harness(venue) as h:
        _fill_ten(h)
        # r1 (T-1): a successful send CONSUMES readiness (one reconciliation authorizes
        # one opening submission), so the next proposal needs a fresh reconciliation.
        # The coordinator cannot re-arm it after a fill (case 14, G4), so the harness
        # re-asserts broker/local parity the way an operator would.
        consumed = h.readiness.snapshot()
        assert consumed.ready is False
        assert "consumed by an opening-order submission" in consumed.reason
        h.rearm_readiness("U3: operator re-reconciled after the fill")

        closing_all = h.sell(quantity=10, ref=_ref("B"), intent_id="intent-close-10")
        closing_more = h.sell(quantity=11, ref=_ref("C"), intent_id="intent-close-11")

        assert h.propose(closing_all).risk.approved is True
        refused = h.propose(closing_more)

        assert refused.risk.approved is False
        failed = {c.name for c in refused.risk.checks if c.status.value == "FAIL"}
        assert failed == {"stock_no_short"}
        assert h.status("intent-close-11") is OrderLifecycle.REJECTED
        assert h.sent_order_count == 1  # risk refusals never reach the broker


# --------------------------------------------------------------------------------------
# partial fill
# --------------------------------------------------------------------------------------


def test_a_partial_fill_then_completion_is_monotonic_and_never_resends(venue: _Venue) -> None:
    # Fails if the FillPlan partial stops working, if the reconciler stops refining
    # PARTIALLY_FILLED by quantity, or if a second pass re-applies an old update.
    with harness(venue, plans={_ref("A"): FillPlan(partial_fraction=(1, 2))}) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True

        h.bar(**CROSSING)  # 5 of 10 at 99.90
        first = h.service.reconcile_on_restart_report(now=h.now)

        assert [u.lifecycle for u in first.applied_updates] == [OrderLifecycle.PARTIALLY_FILLED]
        assert first.applied_updates[0].filled_quantity == Decimal("5")
        assert first.applied_updates[0].remaining_quantity == Decimal("5")
        assert h.status("intent-1") is OrderLifecycle.PARTIALLY_FILLED
        assert h.sim_position() == Decimal("5")
        assert h.sim_open_order_ids() == {h.first_id}

        again = h.service.reconcile_on_restart_report(now=h.now)
        assert again.applied_updates == ()  # nothing new: no lifecycle change
        assert h.status("intent-1") is OrderLifecycle.PARTIALLY_FILLED

        h.bar(**CROSSING)  # the remaining 5
        second = h.service.reconcile_on_restart_report(now=h.now)

        assert [u.lifecycle for u in second.applied_updates] == [OrderLifecycle.FILLED]
        assert second.applied_updates[0].filled_quantity == Decimal("10")
        assert h.status("intent-1") is OrderLifecycle.FILLED
        assert h.sim_position() == Decimal("10")
        assert h.sent_order_count == 1


# --------------------------------------------------------------------------------------
# modify
# --------------------------------------------------------------------------------------


def test_modify_is_refused_before_send_by_the_faithful_sim_and_changes_nothing(
    venue: _Venue,
) -> None:
    # The B1 target refuses modify-in-place; SimBroker mirrors it by default. Fails if the
    # sim starts accepting modifies, or if the modification service records a MODIFIED
    # event (or any transition) when the adapter refused before send.
    with harness(venue) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True
        events_before = len(h.tracker_repo.events("intent-1", current_account_id=h.account))
        readiness_before = h.readiness.snapshot()  # already consumed by the send (T-1)

        with pytest.raises(BrokerRefusedBeforeSend):
            h.service.modify("intent-1", Decimal("101.00"), now=h.now)

        assert h.status("intent-1") is OrderLifecycle.SUBMITTED
        events_after = len(h.tracker_repo.events("intent-1", current_account_id=h.account))
        assert events_after == events_before
        assert h.tracker.effective_limit_price(
            "intent-1", original_limit_price=Decimal("100.00"), current_account_id=h.account
        ) == Decimal("100.00")
        (working,) = h.connection.run(h.broker.open_orders())
        assert working.limit_price == Decimal("100.00")
        # The refusal said nothing about connection health, so readiness is exactly as
        # it was (an invalidation would have bumped the generation), and the order still
        # works and fills at its ORIGINAL limit.
        readiness_after = h.readiness.snapshot()
        assert readiness_after == readiness_before
        assert readiness_after.generation == readiness_before.generation
        h.bar(**CROSSING)
        report = h.service.reconcile_on_restart_report(now=h.now)
        assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.FILLED]


def test_modify_when_the_sim_allows_it_reprices_the_working_order_and_reconciles(
    venue: _Venue,
) -> None:
    # The opt-in replace. Fails if the MODIFIED evidence stops carrying the new price (the
    # reconciler would then see a limit mismatch and leave the intent unresolved), or if
    # the sim's fill rule stops using the replaced limit.
    with harness(venue, allow_modify=True) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True

        submission = h.service.modify("intent-1", Decimal("101.00"), now=h.now)

        assert submission.broker_order_id == h.first_id
        assert h.status("intent-1") is OrderLifecycle.SUBMITTED
        assert h.tracker.effective_limit_price(
            "intent-1", original_limit_price=Decimal("100.00"), current_account_id=h.account
        ) == Decimal("101.00")
        (working,) = h.connection.run(h.broker.open_orders())
        assert working.limit_price == Decimal("101.00")

        report = h.service.reconcile_on_restart_report(now=h.now)
        assert report.unresolved == ()
        assert [item.intent_id for item in report.proven] == ["intent-1"]
        assert report.applied_updates == ()

        # A bar that crosses 101.00 but not 100.00 fills only because of the modify:
        # min(open 100.80, limit 101.00) = 100.80.
        h.bar(open_="100.80", high="101.10", low="100.50", close="100.90")
        assert h.venue_lifecycle(h.first_id) is OrderLifecycle.FILLED
        (execution,) = h.connection.run(h.broker.executions())
        assert execution.price == Decimal("100.80")
        report = h.service.reconcile_on_restart_report(now=h.now)
        assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.FILLED]


# --------------------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------------------


def test_cancel_runs_through_cancel_pending_and_the_sim_drops_the_order(venue: _Venue) -> None:
    # Fails if the cancellation service stops recording CANCEL_PENDING before the broker
    # call, if the sim keeps a cancelled order in open_orders, or if a later crossing bar
    # fills a cancelled order.
    with harness(venue) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True

        result = h.service.cancel("intent-1", now=h.now)

        assert result.requested is True
        assert result.lifecycle is OrderLifecycle.CANCELLED
        assert h.status("intent-1") is OrderLifecycle.CANCELLED
        statuses = [
            e.to_status for e in h.tracker_repo.events("intent-1", current_account_id=h.account)
        ]
        assert statuses[-2:] == [OrderLifecycle.CANCEL_PENDING, OrderLifecycle.CANCELLED]
        assert h.sim_open_order_ids() == set()
        assert h.venue_lifecycle(h.first_id) is OrderLifecycle.CANCELLED

        h.bar(**CROSSING)
        assert h.connection.run(h.broker.executions()) == ()
        assert h.sim_position() == 0
        assert h.service.reconcile_on_restart_report(now=h.now).remaining_active == ()


def test_cancel_after_a_partial_fill_keeps_the_fill_and_ends_terminal(venue: _Venue) -> None:
    # Fails if the sim forgets a partial fill on cancel, or if CANCELLED after
    # PARTIALLY_FILLED stops being terminal for the tracker.
    with harness(venue, plans={_ref("A"): FillPlan(partial_fraction=(1, 2))}) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True
        h.bar(**CROSSING)  # 5 of 10
        h.service.reconcile_on_restart_report(now=h.now)
        assert h.status("intent-1") is OrderLifecycle.PARTIALLY_FILLED

        result = h.service.cancel("intent-1", now=h.now)

        assert result.lifecycle is OrderLifecycle.CANCELLED
        assert h.status("intent-1") is OrderLifecycle.CANCELLED
        assert h.sim_open_order_ids() == set()
        assert h.sim_position() == Decimal("5")
        assert len(h.connection.run(h.broker.executions())) == 1
        h.bar(**CROSSING)  # nothing more fills
        assert h.sim_position() == Decimal("5")


# --------------------------------------------------------------------------------------
# reject, ambiguous post-send
# --------------------------------------------------------------------------------------


def test_a_venue_rejection_persists_rejected_and_leaves_no_working_order(venue: _Venue) -> None:
    # Fails if the boundary stops persisting a non-working acknowledgement as REJECTED,
    # or if the sim keeps a rejected order working.
    with harness(venue, plans={_ref("A"): FillPlan(reject=True)}) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)

        outcome = h.submit(intent)

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED
        assert outcome.submission is not None
        assert outcome.submission.lifecycle is OrderLifecycle.REJECTED
        assert h.status("intent-1") is OrderLifecycle.REJECTED
        assert h.sent_order_count == 1  # it crossed the wire; the venue said no
        assert h.sim_open_order_ids() == set()
        h.bar(**CROSSING)
        assert h.connection.run(h.broker.executions()) == ()
        assert h.service.reconcile_on_restart_report(now=h.now).remaining_active == ()


def test_an_ambiguous_send_persists_submission_unknown_then_recovers_without_resending(
    venue: _Venue,
) -> None:
    # SURVEY's SUBMISSION_UNKNOWN case. Fails if the boundary stops persisting the broker
    # identity on an unknown acknowledgement, if the reconciler re-submits, or if it fails
    # to prove the venue's working order against the persisted id.
    with harness(venue, plans={_ref("A"): FillPlan(ambiguous=True)}, silent_refs=(_ref("A"),)) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)

        outcome = h.submit(intent)

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.BROKER_SUBMIT_FAILED
        assert outcome.submission is not None
        assert outcome.submission.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN
        assert h.status("intent-1") is OrderLifecycle.SUBMISSION_UNKNOWN
        assert h.tracker.broker_order_id("intent-1", current_account_id=h.account) == h.first_id
        assert h.sim_open_order_ids() == {h.first_id}  # the venue holds it
        assert h.sent_order_count == 1

        report = h.service.reconcile_on_restart_report(now=h.now)

        assert report.unresolved == ()
        assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.SUBMITTED]
        assert h.status("intent-1") is OrderLifecycle.SUBMITTED
        assert report.permits_new_opening_order is False
        assert h.sent_order_count == 1  # recovered, never re-sent

        h.bar(**CROSSING)
        report = h.service.reconcile_on_restart_report(now=h.now)
        assert [u.lifecycle for u in report.applied_updates] == [OrderLifecycle.FILLED]
        assert h.sim_position() == Decimal("10")


# --------------------------------------------------------------------------------------
# risk refusal before send
# --------------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "U3-F1: daily opening cap counts the intent under evaluation (finding, RED, awaiting Kevin)"
    ),
)
def test_the_daily_opening_cap_refuses_the_next_opening_after_a_fill(venue: _Venue) -> None:
    # SURVEY: "the opening cap after N fills". Fails if the real evidence provider stops
    # counting today's opening intents from the repository (R-25), or if the cap stops
    # refusing at the limit. At 306efd5 it fails EARLIER, inside _fill_ten: the cap counts
    # the intent under evaluation, so the FIRST opening of the day is refused (U3-F1) —
    # the strict xfail above absorbs exactly that AssertionError. Against the fix it must
    # XPASS, so (r2) the second opening is economically distinct (an identical order would
    # be refused as a duplicate idempotency key, a ValueError, not the cap) and readiness
    # is re-armed after the fill (a send consumes it; see case 2), leaving the cap as the
    # only check that can fail.
    with harness(venue, settings=_cap_of_one(venue)) as h:
        _fill_ten(h)
        h.rearm_readiness("U3: operator re-reconciled after the fill")

        second = h.propose(h.buy(quantity=11, ref=_ref("B"), intent_id="intent-2"))

        assert second.risk.approved is False
        failed = {c.name: c.detail for c in second.risk.checks if c.status.value == "FAIL"}
        assert set(failed) == {"max_opening_orders_per_day"}
        assert "2 would exceed the 1 daily cap" in failed["max_opening_orders_per_day"]
        assert h.status("intent-2") is OrderLifecycle.REJECTED
        assert h.sent_order_count == 1


def test_a_buy_the_sim_account_cannot_fund_is_refused_by_risk(venue: _Venue) -> None:
    # Fails if the cash check stops reading the sim's account summary. The positive
    # control proves the same check passes just under the line (100000 cash, 10% buffer).
    with harness(venue) as h:
        affordable = h.propose(h.buy(quantity=800, ref=_ref("A"), intent_id="intent-ok"))
        assert affordable.risk.approved is True

        refused = h.propose(h.buy(quantity=1000, ref=_ref("B"), intent_id="intent-too-big"))

        assert refused.risk.approved is False
        failed = {c.name for c in refused.risk.checks if c.status.value == "FAIL"}
        assert failed == {"stock_cash_sufficiency"}
        assert h.status("intent-too-big") is OrderLifecycle.REJECTED
        assert h.sent_order_count == 0


def test_a_kill_switch_engaged_at_the_send_is_refused_before_send(venue: _Venue) -> None:
    # SURVEY: "the kill switch engaged mid-flow". The paper branch has no earlier kill
    # check, so the adapter's last-line gate is the one that fires. Fails if the sim stops
    # honouring the gate, or if the boundary stops resolving a before-send refusal to
    # REJECTED (it would strand SUBMISSION_UNKNOWN).
    with harness(venue, kill_switch_engaged=True) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)

        outcome = h.submit(intent)

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
        if h.sim_kill_switch is not None:  # sim: SimBroker's last-line gate was consulted once
            assert h.sim_kill_switch.entered == 1
        else:  # official: the adapter's own LiveKillSwitch refused before any order call
            assert h.kill_switch is not None and h.kill_switch.is_engaged()
        assert h.status("intent-1") is OrderLifecycle.REJECTED
        assert h.sent_order_count == 0
        assert h.sim_open_order_ids() == set()


def test_a_disconnect_before_submit_invalidates_readiness_and_refuses(venue: _Venue) -> None:
    # Survey review P2-1's U3 case: a dropped connection must drop the system out of
    # RECONCILED and refuse to submit, and a bare reconnect must not restore authority.
    # Fails if the boundary stops re-reading connection status, or if readiness stops
    # being invalidated by it.
    with harness(venue) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        h.connection.run(h.broker.disconnect())

        outcome = h.submit(intent)

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
        assert h.readiness.snapshot().ready is False
        assert h.status("intent-1") is OrderLifecycle.USER_CONFIRMED
        assert h.sent_order_count == 0

        h.connection.run(h.broker.connect())
        assert h.submit(intent).refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
        assert h.readiness.complete(
            expected_generation=h.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="U3: operator re-reconciled after the drop",
            reconciled_at=h.now,
        )
        # Re-reconciled, but the risk evidence belongs to the OLD generation.
        assert h.submit(intent).refusal is SubmissionRefusalCode.RISK_EVIDENCE_STALE
        assert h.sent_order_count == 0


# --------------------------------------------------------------------------------------
# reconciliation after fills
# --------------------------------------------------------------------------------------


def test_reconciliation_after_fills_reports_the_sim_position_and_persists_the_executions(
    venue: _Venue,
) -> None:
    # The coordinator reads the position the fills created and persists the executions
    # (BP-1b); the test cross-checks the two, which is what U4 would move into readiness
    # (SURVEY G4). Fails if the coordinator stops observing positions or executions, if the
    # execution writer stops persisting, or if the fail-closed provenance reasons vanish.
    with harness(venue) as h:
        _fill_ten(h)

        result = h.coordinator.reconcile()

        assert result.snapshot is not None
        (position,) = result.snapshot.positions
        assert position.contract.symbol == SYMBOL
        assert position.quantity == Decimal("10")
        assert position.average_cost == Decimal("100.00")  # (999.00 + 1.00) / 10
        assert result.snapshot.execution_count == 1
        (symbol,) = [s for s in result.symbols if s.symbol == SYMBOL]
        assert symbol.stock_shares == Decimal("10")
        # Today's coordinator cannot prove a filled position's provenance, so it fails
        # closed rather than reporting RECONCILED (G4: no fill-to-position cross-check).
        assert result.status is ReconciliationStatus.MANUAL_REVIEW
        assert symbol.manual_review_required is True
        assert any("fill-allocation provenance" in reason for reason in symbol.reasons)
        # The executions were persisted as broker-observed facts ...
        assert h.persisted_fill_quantity() == Decimal("10")
        # ... and they agree with the broker position: the cross-check U4 would make.
        assert h.persisted_fill_quantity() == h.sim_position()
        # Idempotent: a second pass writes nothing new.
        h.coordinator.reconcile()
        assert h.persisted_fill_quantity() == Decimal("10")


# --------------------------------------------------------------------------------------
# the demo-mode control (survey review P3-4)
# --------------------------------------------------------------------------------------


def test_demo_shaped_settings_refuse_transmission_against_a_broker_that_would_accept(
    venue: _Venue,
) -> None:
    # Default settings (demo mode, ALLOW_ORDER_TRANSMIT false) must refuse before the
    # broker is asked, even though SimBroker would accept the order. Fails if the
    # TRANSMISSION_NOT_POSSIBLE gate is bypassed. Also pins that paper_settings() is an
    # in-process object, never a live-capable environment.
    assert paper_settings().allow_live_trading is False
    assert official_transmit_settings().allow_live_trading is False
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.allow_order_transmit is False
    assert defaults.allow_live_trading is False
    assert defaults.transmission_possible is False
    # The official venue cannot even construct the adapter from demo defaults (no account),
    # so its control is the same IBKR/paper shape with transmit withheld: still refused
    # before the broker is asked.
    with harness(venue) as probe:
        settings = probe.transmit_disabled_settings()
    assert settings.transmission_possible is False
    with harness(venue, settings=settings) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)

        outcome = h.submit(intent)

        assert outcome.submitted is False
        assert outcome.refusal is SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE
        assert h.sent_order_count == 0
        assert h.sim_open_order_ids() == set()


# --------------------------------------------------------------------------------------
# the S2 scope proof (Kevin, round 3)
# --------------------------------------------------------------------------------------


#: Spellings pydantic reads as False; the slice's run env pins ``ALLOW_*=false`` explicitly
#: (the standing stop), so the proof is "never true", not "absent".
_FALSE = {"", "0", "false", "f", "no", "n", "off"}


def _no_transmit_outside_the_test_object() -> None:
    assert os.environ.get("ALLOW_ORDER_TRANSMIT", "").strip().lower() in _FALSE
    assert os.environ.get("ALLOW_LIVE_TRADING", "").strip().lower() in _FALSE
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.allow_order_transmit is False and defaults.transmission_possible is False
    get_settings.cache_clear()
    try:
        live = get_settings()
        assert live.allow_order_transmit is False
        assert live.allow_live_trading is False
        assert live.transmission_possible is False
    finally:
        get_settings.cache_clear()


def test_transmit_true_lives_only_in_the_test_settings_object(venue: _Venue) -> None:
    # Fails if any venue's harness reaches transmit=true through the environment, a .env
    # or the cached global Settings instead of the in-process object, or if the object
    # leaks into the global accessor during or after a run.
    _no_transmit_outside_the_test_object()
    with harness(venue) as h:
        assert h.settings.allow_order_transmit is True  # the in-process object, nothing else
        assert h.settings.allow_live_trading is False
        _no_transmit_outside_the_test_object()
        get_settings.cache_clear()
        try:
            assert get_settings() is not h.settings
        finally:
            get_settings.cache_clear()
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        assert h.submit(intent).submitted is True  # the send reached the venue ...
        assert h.sent_order_count == 1  # ... the in-process venue, behind a socket tripwire
        _no_transmit_outside_the_test_object()
    _no_transmit_outside_the_test_object()

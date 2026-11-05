"""XFIX-D r1: actual demo refusals and the two downstream typed-error handlers.

Public demo configuration cannot enter transmission. The post-admission test
explicitly exercises the existing internal CAS seam to isolate the error's
durable effect, without weakening or claiming to pass the upstream demo gate.
"""

from __future__ import annotations

from collections.abc import Coroutine, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support import fake_ibapi
from tests.support.order_fakes import FIXED_NOW, option_contract, paper_settings
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

from chronos.broker.base import BrokerRefusedBeforeSend
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings
from chronos.domain.enums import IBEnvironment, OrderIntent, OrderLifecycle, ReconciliationStatus
from chronos.domain.models import OrderModification, OrderRequest, OrderSubmission
from chronos.orders.intent import WheelOrderIntent, build_option_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskDecision
from chronos.orders.submission import (
    OrderSubmissionBoundary,
    SubmissionOutcome,
    SubmissionRefusalCode,
)
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderConfirmationRepository,
    OrderIntentRepository,
    OrderTrackerRepository,
)


def _intent(name: str = "demo-refusal-1", price: str = "1.20") -> WheelOrderIntent:
    return build_option_intent(
        account_id=DEMO_ACCOUNT_ID,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal(price),
        correlation_id="CHR-ORD-" + name.upper(),
        intent_id=name,
    )


def _entry_call(
    broker: DemoBroker | OfficialIBKRBroker, method: str
) -> Coroutine[Any, Any, OrderSubmission]:
    if method == "submit":
        return broker.submit_order(_intent().to_order_request(transmit=False))
    return broker.modify_order(
        OrderModification(
            correlation_id="CHR-ORD-DEMO-MODIFY", broker_order_id=1, new_limit_price=1
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "modify"])
@pytest.mark.parametrize("state", ["never_connected", "connected", "disconnected_again"])
async def test_demo_order_entry_is_provably_not_sent_in_every_connection_state(
    method: str, state: str
) -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    if state != "never_connected":
        await broker.connect()
    if state == "disconnected_again":
        await broker.disconnect()
    before = await broker.connection_status()
    with pytest.raises(BrokerRefusedBeforeSend):
        await _entry_call(broker, method)
    assert await broker.connection_status() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "modify"])
async def test_official_adapter_disconnected_order_entry_uses_the_same_proof_type(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)
    broker = OfficialIBKRBroker(paper_settings(allow_order_transmit=False))
    try:
        with pytest.raises(BrokerRefusedBeforeSend, match="not connected"):
            await _entry_call(broker, method)
    finally:
        gateway.release_readers()
    assert gateway.callback_errors == []


@pytest.mark.parametrize("method", ["submit", "modify"])
def test_real_demo_refusal_preserves_connection_observation_and_readiness(method: str) -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW)
    readiness = ReconciliationReadiness(clock=lambda: FIXED_NOW)
    connection = BrokerConnectionManager(
        broker, readiness=readiness, observation_clock=lambda: FIXED_NOW
    )
    try:
        connection.connect()
        connection.connection_status()
        assert readiness.complete(
            expected_generation=readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="synthetic demo parity",
            reconciled_at=FIXED_NOW,
        )
        before_ready = readiness.snapshot()
        before_observation = connection.connection_observation()
        before_book = connection.run(broker.snapshot())
        with pytest.raises(BrokerRefusedBeforeSend):
            connection.run(_entry_call(broker, method))
        assert readiness.snapshot() == before_ready
        assert connection.connection_observation() == before_observation
        assert connection.run(broker.snapshot()) == before_book
        assert connection.connection_status().connected
    finally:
        connection.close()


class _DemoBoundary:
    def __init__(self, tmp_path: Path) -> None:
        self.db = Database(f"sqlite:///{tmp_path / 'demo-refusal.db'}")
        self.db.initialize()
        self.db.bind_scope(
            broker_mode="demo",
            environment="paper",
            account_id=DEMO_ACCOUNT_ID,
            key=TEST_PERSISTENCE_KEY,
        )
        self.broker = DemoBroker(clock=lambda: FIXED_NOW)
        self.readiness = ReconciliationReadiness(clock=lambda: FIXED_NOW)
        self.connection = BrokerConnectionManager(self.broker, readiness=self.readiness)
        self.connection.connect()
        self.connection.connection_status()
        self.mark_ready()
        self.intents = OrderIntentRepository(self.db.sessions)
        self.tracker = OrderTrackerRepository(self.db.sessions)
        self.boundary = OrderSubmissionBoundary(
            settings=Settings(
                _env_file=None,
                broker_mode="demo",
                ib_environment="paper",
                allow_order_transmit=False,
                allow_live_trading=False,
            ),
            connection=self.connection,
            intents=self.intents,
            confirmations=OrderConfirmationRepository(self.db.sessions),
            tracker=self.tracker,
            reconciliation_readiness=self.readiness,
            clock=lambda: FIXED_NOW,
        )

    def mark_ready(self) -> None:
        assert self.readiness.complete(
            expected_generation=self.readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="synthetic pre-admission parity",
            reconciled_at=FIXED_NOW,
        )

    def seed_confirmed(self, intent: WheelOrderIntent) -> None:
        assert self.intents.create(
            intent.to_intent_record(
                environment=IBEnvironment.PAPER,
                status=OrderLifecycle.USER_CONFIRMED,
                created_at=FIXED_NOW,
                expires_at=FIXED_NOW + timedelta(minutes=1),
                day_bucket=FIXED_NOW.date().isoformat(),
                key=TEST_PERSISTENCE_KEY,
            ),
            current_account_id=DEMO_ACCOUNT_ID,
        )

    def post_admission(self, intent: WheelOrderIntent) -> SubmissionOutcome:
        return self.boundary._cas_and_transmit(
            intent=intent,
            account_id=DEMO_ACCOUNT_ID,
            now=FIXED_NOW,
            final_kill_check=False,
            branch_detail="synthetic admitted seam",
            expected_reconciliation_generation=self.readiness.snapshot().generation,
        )


@pytest.fixture
def demo_boundary(tmp_path: Path) -> Iterator[_DemoBoundary]:
    rig = _DemoBoundary(tmp_path)
    try:
        yield rig
    finally:
        rig.connection.close()
        rig.db.dispose()


def test_demo_refusal_at_the_post_admission_boundary_rejects_without_stranding(
    demo_boundary: _DemoBoundary,
) -> None:
    rig = demo_boundary
    intent = _intent()
    rig.seed_confirmed(intent)
    ready = rig.readiness.snapshot()
    observation = rig.connection.connection_observation()
    book = rig.connection.run(rig.broker.snapshot())
    outcome = rig.post_admission(intent)
    assert not outcome.submitted and outcome.submission is None
    assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
    stored = rig.intents.get(intent.intent_id, current_account_id=DEMO_ACCOUNT_ID)
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
    assert stored.submitted_at is None
    events = rig.tracker.events(intent.intent_id, current_account_id=DEMO_ACCOUNT_ID)
    assert [(e.from_status, e.to_status) for e in events] == [
        (OrderLifecycle.USER_CONFIRMED, OrderLifecycle.SUBMISSION_UNKNOWN),
        (OrderLifecycle.SUBMISSION_UNKNOWN, OrderLifecycle.REJECTED),
    ]
    assert [event.evidence["phase"] for event in events] == ["pre_submit", "refused_before_send"]
    assert all(event.broker_order_id is None for event in events)
    assert (
        rig.intents.ids_in_status(
            OrderLifecycle.SUBMISSION_UNKNOWN, current_account_id=DEMO_ACCOUNT_ID
        )
        == ()
    )
    assert rig.intents.active(current_account_id=DEMO_ACCOUNT_ID) == ()
    assert rig.connection.connection_observation() == observation
    assert rig.connection.run(rig.broker.snapshot()) == book
    # A submission claim still consumes readiness. The typed refusal must not
    # add a connection-uncertainty generation or authorize a second claim.
    after = rig.readiness.snapshot()
    assert after.status is ReconciliationStatus.PENDING
    assert (after.generation, after.session_id) == (ready.generation, ready.session_id)
    follow_up = _intent("demo-refusal-2", "1.25")
    rig.seed_confirmed(follow_up)
    assert rig.post_admission(follow_up).refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    rig.mark_ready()
    assert rig.post_admission(follow_up).refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
    assert (
        rig.intents.ids_in_status(
            OrderLifecycle.SUBMISSION_UNKNOWN, current_account_id=DEMO_ACCOUNT_ID
        )
        == ()
    )
    assert rig.intents.active(current_account_id=DEMO_ACCOUNT_ID) == ()


def test_public_demo_boundary_still_refuses_before_reaching_the_broker(
    demo_boundary: _DemoBoundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = demo_boundary
    intent = _intent()
    rig.seed_confirmed(intent)

    async def forbidden(_request: OrderRequest, **_kwargs: object) -> None:
        raise AssertionError("public demo admission must never call submit_order")

    monkeypatch.setattr(rig.broker, "submit_order", forbidden)
    outcome = rig.boundary.submit(
        intent=intent,
        risk_decision=OrderRiskDecision.model_construct(),
        connected_account_id=DEMO_ACCOUNT_ID,
        broker_environment_is_paper=False,
        writer_lease_held=True,
        now=FIXED_NOW,
    )
    assert outcome.refusal is SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE
    assert rig.tracker.events(intent.intent_id, current_account_id=DEMO_ACCOUNT_ID) == ()

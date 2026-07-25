"""The tick drives autonomy, and events can only make it earlier (R-36, M7).

The cadence decision, pinned: a time-driven tick is the only thing that runs
cycles, and events are wake-up *hints* that coalesce. Under pure event-driven, an
event storm is an order storm — the rate would be set by the market or the
caller rather than by us, and the activity limits would be catching a problem
the design created. Under a pure timer, a fill waits. Coalescing gives the
responsiveness without the unboundedness: any number of events collapse into
"wake early once", bounded by the minimum-interval floor.

Also pinned here: the queue that separates *receiving* a proposal from *judging*
one, and the supervision rule that a failing tick alerts and keeps cadence
rather than dying silently — because a runtime that stopped without saying so
looks exactly like a runtime with nothing to do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.domain.models import UnderlyingContract
from chronos.persistence.database import Database
from chronos.supervisor import alerts, durable, proposals, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts
from chronos.supervisor.runtime import AutonomyRuntime, RuntimeConfig
from chronos.supervisor.sizing import AccountEvidence

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def database() -> Iterator[Database]:
    instance = Database("sqlite+pysqlite:///:memory:")
    instance.initialize()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def sessions(database: Database) -> sessionmaker[Session]:
    return database.sessions


def _identity() -> queue.HarnessIdentity:
    return queue.HarnessIdentity(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
    )


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": _NOW - timedelta(hours=1),
        "expires_at": _NOW + timedelta(days=1),
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
            symbols=("SPY",),
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
        "authored_at": _NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _facts(now: datetime) -> CycleFacts:
    return CycleFacts(
        account_fingerprint=_FINGERPRINT,
        account_id="DU1234567",
        now=now,
        process_generation=7,
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
        market_data=MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        account=AccountEvidence(
            net_liquidation_usd=Decimal(100_000),
            total_cash_usd=Decimal(60_000),
            buying_power_usd=Decimal(60_000),
            symbol_exposure_usd=Decimal(0),
            gross_exposure_usd=Decimal(0),
            net_exposure_usd=Decimal(0),
            position_notional_usd=Decimal(0),
            maintenance_margin_usd=Decimal(0),
            deployed_capital_usd=Decimal(0),
        ),
        quote=QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02")),
        contract=UnderlyingContract(con_id=111, symbol="SPY"),
        reference_price=Decimal(400),
    )


def _payload() -> str:
    return json.dumps(
        {
            "kind": "OPEN",
            "asset_class": "EQUITY",
            "symbol": "SPY",
            "requested_strategy": "LONG_EQUITY",
            "requested_quantity": "10",
            "evidence": [
                {
                    "evidence_id": "ev-1",
                    "kind": "quote",
                    "as_of": _NOW.isoformat(),
                    "digest": "c" * 64,
                }
            ],
            "invalidation_conditions": ["closes below 400"],
        }
    )


class _NullSink:
    name = "null"

    def __init__(self) -> None:
        self.seen: list[alerts.OwnerAlert] = []

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        self.seen.append(alert)
        return True


def _runtime(
    sessions: sessionmaker[Session],
    *,
    mandate: AutonomyMandate | None = None,
    gather: Any = None,
    submit: Any = None,
    config: RuntimeConfig | None = None,
    sink: _NullSink | None = None,
) -> AutonomyRuntime:
    return AutonomyRuntime(
        sessions=sessions,
        config=config or RuntimeConfig(account_fingerprint=_FINGERPRINT),
        identity=_identity(),
        mandate_source=lambda: mandate,
        gather_facts=gather if gather is not None else _facts,
        sinks=(sink or _NullSink(),),
        submit=submit,
    )


def _activate(sessions: sessionmaker[Session], mandate: AutonomyMandate) -> None:
    with sessions.begin() as session:
        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-event-1",
            now=_NOW,
            process_generation=7,
        )


def _enqueue(sessions: sessionmaker[Session], payload: str | None = None) -> None:
    with sessions.begin() as session:
        outcome = proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=payload or _payload(),
            now=_NOW,
        )
        assert outcome.queued


# --------------------------------------------------------------- the cadence


def test_a_zero_minimum_interval_is_refused_at_construction() -> None:
    """A zero floor makes the tick event-driven — the unbounded shape rejected."""

    with pytest.raises(ValueError, match="event-driven"):
        RuntimeConfig(account_fingerprint=_FINGERPRINT, minimum_interval_seconds=0)


def test_idle_cadence_may_not_undercut_the_floor() -> None:
    with pytest.raises(ValueError, match="floor"):
        RuntimeConfig(
            account_fingerprint=_FINGERPRINT,
            minimum_interval_seconds=10,
            idle_interval_seconds=5,
        )


def test_an_event_pulls_the_next_tick_to_the_floor_and_no_further(
    sessions: sessionmaker[Session],
) -> None:
    config = RuntimeConfig(
        account_fingerprint=_FINGERPRINT,
        minimum_interval_seconds=5,
        idle_interval_seconds=60,
    )
    runtime = _runtime(sessions, config=config)
    runtime.run_tick(_NOW)

    quiet = runtime.seconds_until_next_tick(_NOW + timedelta(seconds=1))
    assert quiet == pytest.approx(59.0)

    runtime.note_event("fill observed")
    hinted = runtime.seconds_until_next_tick(_NOW + timedelta(seconds=1))
    assert hinted == pytest.approx(4.0)  # to the 5s floor, not to zero


def test_a_thousand_events_coalesce_into_one_early_wake(
    sessions: sessionmaker[Session],
) -> None:
    """An event storm must not become a cycle storm."""

    runtime = _runtime(sessions)
    runtime.run_tick(_NOW)
    for index in range(1000):
        runtime.note_event(f"event {index}")
    # Still exactly one hint: the next tick comes at the floor, once.
    assert runtime.seconds_until_next_tick(_NOW) == pytest.approx(5.0)
    # And after that tick runs, the hint is consumed.
    runtime.run_tick(_NOW + timedelta(seconds=5))
    assert runtime.seconds_until_next_tick(_NOW + timedelta(seconds=5)) == pytest.approx(60.0)


def test_there_is_no_way_to_run_a_cycle_from_an_event() -> None:
    """The event surface is one flag. Structurally, nothing more."""

    surface = [name for name in dir(AutonomyRuntime) if not name.startswith("_")]
    assert "note_event" in surface
    # No "run_now", "force_cycle", "trigger" or similar exists to bypass the tick.
    for name in surface:
        assert "force" not in name and "now" not in name and "trigger" not in name, name


# ------------------------------------------------------------------ the queue


def test_a_queued_proposal_is_judged_on_the_next_tick(
    sessions: sessionmaker[Session],
) -> None:
    mandate = _mandate()
    _activate(sessions, mandate)
    _enqueue(sessions)

    runtime = _runtime(sessions, mandate=mandate)
    report = runtime.run_tick(_NOW)
    assert report.proposals_judged == 1
    # Shadow: no submit callable was supplied, so nothing was handed off.
    assert report.orders_handed_off == 0

    with sessions.begin() as session:
        assert proposals.pending_depth(session, account_fingerprint=_FINGERPRINT) == 0


def test_a_judged_refusal_is_marked_processed_not_retried(
    sessions: sessionmaker[Session],
) -> None:
    """One malformed payload must not block every proposal behind it forever."""

    _enqueue(sessions, payload="{ not json")
    runtime = _runtime(sessions)
    runtime.run_tick(_NOW)
    second = runtime.run_tick(_NOW + timedelta(seconds=60))
    assert second.proposals_judged == 0  # judged once, refused, done


def test_the_queue_is_bounded_and_refuses_the_newest(
    sessions: sessionmaker[Session],
) -> None:
    """Dropping the oldest would let a flood erase a legitimate earlier proposal."""

    with sessions.begin() as session:
        for _ in range(proposals.MAX_PENDING):
            assert proposals.enqueue(
                session,
                account_fingerprint=_FINGERPRINT,
                payload=_payload(),
                now=_NOW,
            ).queued
        overflow = proposals.enqueue(
            session, account_fingerprint=_FINGERPRINT, payload=_payload(), now=_NOW
        )
    assert overflow.queued is False
    assert "cap" in overflow.refusal


def test_a_tick_drains_boundedly_so_a_flood_cannot_starve_alerting(
    sessions: sessionmaker[Session],
) -> None:
    mandate = _mandate()
    _activate(sessions, mandate)
    for _ in range(25):
        _enqueue(sessions)
    config = RuntimeConfig(account_fingerprint=_FINGERPRINT, proposals_per_tick=10)
    runtime = _runtime(sessions, mandate=mandate, config=config)
    report = runtime.run_tick(_NOW)
    assert report.proposals_judged == 10
    with sessions.begin() as session:
        assert proposals.pending_depth(session, account_fingerprint=_FINGERPRINT) == 15


def test_the_raw_payload_is_what_gets_judged(sessions: sessionmaker[Session]) -> None:
    """The bytes that arrived are the bytes that are judged — no re-serialization."""

    raw = _payload()
    _enqueue(sessions, payload=raw)
    with sessions.begin() as session:
        batch = proposals.claim_batch(session, account_fingerprint=_FINGERPRINT, limit=1)
        assert batch[0].payload == raw


# ------------------------------------------------------------- supervision


def test_a_failing_gatherer_alerts_and_keeps_cadence(
    sessions: sessionmaker[Session],
) -> None:
    """No facts is a refusal to run, not a crash — and the owner hears about it."""

    sink = _NullSink()
    runtime = _runtime(sessions, gather=lambda now: None, sink=sink)
    _enqueue(sessions)

    report = runtime.run_tick(_NOW)
    assert report.ok  # the tick itself succeeded; it declined to run cycles
    assert report.proposals_judged == 0
    assert any(alert.kind == "runtime.no_facts" for alert in sink.seen)
    with sessions.begin() as session:
        # The queue is intact: the work happens once facts return.
        assert proposals.pending_depth(session, account_fingerprint=_FINGERPRINT) == 1


def test_a_raising_tick_records_the_type_never_the_message(
    sessions: sessionmaker[Session],
) -> None:
    """A hostile payload must not write chosen text into operator logs."""

    def _explode(now: datetime) -> CycleFacts:
        raise RuntimeError("SECRET-TEXT-THE-OPERATOR-MUST-NOT-SEE")

    runtime = _runtime(sessions, gather=_explode)
    report = runtime.run_tick(_NOW)
    assert report.ok is False
    assert "RuntimeError" in report.failure
    assert "SECRET-TEXT" not in report.failure


def test_consecutive_failures_stop_the_runtime_with_a_critical_alert(
    sessions: sessionmaker[Session],
) -> None:
    """Ticking into a broken dependency generates noise, not progress."""

    def _explode(now: datetime) -> CycleFacts:
        raise RuntimeError("down")

    sink = _NullSink()
    config = RuntimeConfig(account_fingerprint=_FINGERPRINT, max_consecutive_failures=3)
    runtime = _runtime(sessions, gather=_explode, config=config, sink=sink)

    for index in range(3):
        runtime.run_tick(_NOW + timedelta(seconds=index * 60))
    assert runtime.stopped is True
    assert any(
        alert.severity is alerts.AlertSeverity.CRITICAL and "stopped" in alert.summary
        for alert in sink.seen
    )
    # A stopped runtime refuses further ticks rather than pretending.
    after = runtime.run_tick(_NOW + timedelta(hours=1))
    assert after.failure == "runtime stopped"


def test_one_success_resets_the_failure_count(sessions: sessionmaker[Session]) -> None:
    calls = {"n": 0}

    def _flaky(now: datetime) -> CycleFacts | None:
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise RuntimeError("intermittent")
        return _facts(now)

    config = RuntimeConfig(account_fingerprint=_FINGERPRINT, max_consecutive_failures=3)
    runtime = _runtime(sessions, gather=_flaky, config=config)
    for index in range(8):  # alternating fail/succeed never reaches 3 consecutive
        runtime.run_tick(_NOW + timedelta(seconds=index * 60))
    assert runtime.stopped is False


def test_alerts_are_delivered_each_tick(sessions: sessionmaker[Session]) -> None:
    """The tick is also the delivery heartbeat, so telling the owner needs no
    separate process to have survived."""

    sink = _NullSink()
    with sessions.begin() as session:
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.WARNING,
            kind="degraded.broker",
            summary="unreachable",
            now=_NOW,
        )
    runtime = _runtime(sessions, sink=sink)
    report = runtime.run_tick(_NOW)
    assert report.alerts_delivered == 1
    assert sink.seen[0].kind == "degraded.broker"


def test_a_stopped_runtime_stays_stopped(sessions: sessionmaker[Session]) -> None:
    """Restarting is an operator act; self-restart would obscure why it stopped."""

    runtime = _runtime(sessions)
    runtime.stop("operator requested")
    assert runtime.stopped is True
    assert runtime.seconds_until_next_tick(_NOW) == float("inf")
    assert not hasattr(runtime, "restart")
    assert not hasattr(runtime, "resume")


# ---------------------------------------------------------------- the wiring


def test_two_deliberate_acts_are_needed_for_an_order(
    sessions: sessionmaker[Session],
) -> None:
    """The mandate must permit submission AND the operator must wire the handoff.

    Neither alone is enough, and neither is a default.
    """

    received: list[Any] = []

    # Act only 1: submitting mandate, no handoff -> shadow.
    mandate = _mandate()
    _activate(sessions, mandate)
    _enqueue(sessions)
    shadow = _runtime(sessions, mandate=mandate)
    report = shadow.run_tick(_NOW)
    assert report.proposals_judged == 1
    assert report.orders_handed_off == 0

    # Both acts: submitting mandate AND a handoff -> the order plane hears.
    # A DIFFERENT trade: re-enqueueing the same one would collide on its
    # economic fingerprint and be refused as a replay — the R-31 guard doing
    # its job, and worth this comment because it looks like test setup noise.
    different = json.loads(_payload())
    different["requested_quantity"] = "7"
    _enqueue(sessions, payload=json.dumps(different))
    live = _runtime(
        sessions, mandate=mandate, submit=lambda intent: received.append(intent) or "ok"
    )
    report = live.run_tick(_NOW + timedelta(seconds=60))
    assert report.orders_handed_off == 1
    assert len(received) == 1


def test_the_runtime_imports_nothing_that_can_transmit() -> None:
    """Same guarantee as the loop: the tick cannot reach a venue."""

    import ast
    import inspect

    from chronos.supervisor import runtime as runtime_module

    tree = ast.parse(inspect.getsource(runtime_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for name in imported:
        assert not name.startswith(("chronos.broker", "chronos.execution", "chronos.orders")), name

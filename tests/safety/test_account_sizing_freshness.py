"""Account evidence must remain usable through the last per-decision read."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from chronos.api.autonomy_wiring import BackendGatherers
from chronos.autonomy import DecisionKind, TradableAssetClass
from chronos.persistence.database import Database
from chronos.supervisor import durable, loop
from chronos.supervisor.sizing import AccountEvidence, SizingOutcome, size_order
from tests.safety import test_d54_position_evidence as d54
from tests.support import position_evidence as pe


@pytest.mark.parametrize("delay", [0, 2])
@pytest.mark.parametrize("phase", ["quote", "positions"])
def test_later_instrument_reads_cannot_size_stale_account(
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
    phase: str,
) -> None:
    moment = [pe.NOW + timedelta(seconds=29)]
    sized = []
    plausibility_reads = []

    class Delayed(pe.Broker):
        quote_calls = 0

        async def request_underlying_quote(self, contract):
            self.quote_calls += 1
            await asyncio.sleep(0)
            if phase == "quote" and self.quote_calls == 2:
                moment[0] += timedelta(seconds=delay)
            quote = await super().request_underlying_quote(contract)
            return quote.model_copy(update={"timestamp": moment[0]})

        async def positions(self):
            rows = await super().positions()
            await asyncio.sleep(0)
            if phase == "positions" and self.position_calls == 2:
                moment[0] += timedelta(seconds=delay)
            return rows

    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions() as session:
            pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
            mandate = d54._mandate()
            durable.activate(
                session,
                account_fingerprint=pe.FINGERPRINT,
                mandate=mandate,
                owner_event_id="synthetic-sizing-proof",
                now=moment[0],
                process_generation=7,
            )
            session.commit()
            rows = (pe.held("SPY", "10"),)
            broker = Delayed(reads=[rows, rows])
            gatherers = BackendGatherers(
                pe.runtime(broker, database),
                pe.FINGERPRINT,
                7,
                mandate=mandate,
                mandate_digest="d" * 64,
                clock=lambda: moment[0],
            )
            facts = gatherers.cycle_facts(mandate)(moment[0])
            assert facts is not None
            real_size = loop.size_order

            def record_size(**kwargs):
                result = real_size(**kwargs)
                sized.append(((moment[0] - broker.summary_as_of).total_seconds(), result.quantity))
                return result

            monkeypatch.setattr(loop, "size_order", record_size)
            real_position_refusal = loop._position_refusal

            def record_plausibility(*args, **kwargs):
                plausibility_reads.append(
                    ((moment[0] - broker.summary_as_of).total_seconds(), kwargs["net_liquidation"])
                )
                return real_position_refusal(*args, **kwargs)

            monkeypatch.setattr(loop, "_position_refusal", record_plausibility)
            tick = d54._Tick.__new__(d54._Tick)
            tick.session, tick.gatherers, tick.mandate, tick.facts = (
                session,
                gatherers,
                mandate,
                facts,
            )
            outcome = tick.run(d54._exit())
            if delay:
                assert not sized, f"STALE ACCOUNT REACHED SIZING: {sized!r}"
                assert not plausibility_reads
                assert outcome.stage is loop.CycleStage.SIZING
                assert outcome.refusal == "STALE"
                assert "account summary freshness refused: stale" in outcome.detail
            else:
                assert sized == [(29, 10)]
                assert plausibility_reads == [(29, Decimal("100000"))]
                assert outcome.stage is loop.CycleStage.HANDOFF
    finally:
        database.dispose()


def _evidence(**updates: Any) -> AccountEvidence:
    base = {
        "net_liquidation_usd": Decimal("100000"),
        "total_cash_usd": Decimal("60000"),
        "buying_power_usd": Decimal("60000"),
        "position_quantity": Decimal("10"),
        "as_of": pe.NOW,
        "clock": lambda: pe.NOW,
    }
    base.update(updates)
    return AccountEvidence(**base)


def _size(evidence: AccountEvidence) -> SizingOutcome:
    return size_order(
        mandate=d54._mandate(),
        decision_kind=DecisionKind.CLOSE,
        asset_class=TradableAssetClass.EQUITY,
        reference_price=Decimal("400"),
        multiplier=Decimal(1),
        evidence=evidence,
        requested_quantity=None,
    )


@pytest.mark.parametrize(
    "age,max_age,reason",
    [
        (29, 30, None),
        (30, 30, None),
        (31, 30, "STALE"),
        (1, 1, None),
        (2, 1, "STALE"),
        (-1, 30, "FUTURE_DATED"),
    ],
)
def test_size_order_checks_account_age_at_direct_use(age, max_age, reason) -> None:
    evidence = _evidence(
        clock=lambda: pe.NOW + timedelta(seconds=age),
        max_age_seconds=max_age,
    )
    result = _size(evidence)
    if reason is None:
        assert result.quantity == Decimal("10")
        assert result.refusal_code == ""
    else:
        assert result.quantity is None
        assert result.refusal_code == reason
        assert result.constraints == ()


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"as_of": None}, "MISSING_TIMESTAMP"),
        ({"as_of": pe.NOW.replace(tzinfo=None)}, "INVALID_TIMESTAMP"),
        ({"clock": lambda: pe.NOW.replace(tzinfo=None)}, "INVALID_CLOCK"),
        ({"max_age_seconds": 0}, "INVALID_MAX_AGE"),
        ({"max_age_seconds": 31}, "INVALID_MAX_AGE"),
    ],
)
def test_size_order_refuses_missing_or_invalid_account_metadata(updates, reason) -> None:
    result = _size(_evidence(**updates))
    assert result.quantity is None
    assert result.refusal_code == reason


def test_clock_failure_is_a_typed_sanitized_sizing_refusal() -> None:
    def broken_clock():
        raise RuntimeError("unexpected clock failure")

    result = _size(_evidence(clock=broken_clock))
    assert result.quantity is None
    assert result.refusal_code == "INVALID_CLOCK"
    assert "unexpected clock failure" not in result.refusal


def test_backend_carries_stamp_maximum_and_dynamic_clock() -> None:
    moment = [pe.NOW + timedelta(seconds=1)]

    def clock():
        return moment[0]

    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        runtime = pe.runtime(pe.Broker(), database)
        runtime.settings.account_summary_max_age_seconds = 1
        mandate = d54._mandate()
        gatherers = BackendGatherers(
            runtime,
            pe.FINGERPRINT,
            7,
            mandate=mandate,
            mandate_digest="d" * 64,
            clock=clock,
        )
        facts = gatherers.cycle_facts(mandate)(moment[0])
        assert facts is not None
        assert facts.account.as_of == pe.NOW
        assert facts.account.max_age_seconds == 1
        assert facts.account.clock is clock
        assert _size(replace(facts.account, position_quantity=Decimal("10"))).quantity == 10
        moment[0] += timedelta(seconds=1)
        result = _size(facts.account)
        assert result.quantity is None
        assert result.refusal_code == "STALE"
    finally:
        database.dispose()


@pytest.mark.parametrize("elapsed", [0, 2])
def test_later_decision_rechecks_the_same_tick_account(elapsed: int) -> None:
    moment = [pe.NOW + timedelta(seconds=29)]

    class Broker(pe.Broker):
        async def request_underlying_quote(self, contract):
            await asyncio.sleep(0)
            quote = await super().request_underlying_quote(contract)
            return quote.model_copy(update={"timestamp": moment[0]})

    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions() as session:
            pe.bind_managed(session, con_id=111, symbol="SPY", tag="B")
            pe.bind_managed(session, con_id=222, symbol="QQQ", tag="C")
            mandate = d54._mandate()
            durable.activate(
                session,
                account_fingerprint=pe.FINGERPRINT,
                mandate=mandate,
                owner_event_id="synthetic-reused-tick",
                now=moment[0],
                process_generation=7,
            )
            session.commit()
            rows = (pe.held("SPY", "10"), pe.held("QQQ", "10"))
            gatherers = BackendGatherers(
                pe.runtime(Broker(reads=[rows, rows]), database),
                pe.FINGERPRINT,
                7,
                mandate=mandate,
                mandate_digest="d" * 64,
                clock=lambda: moment[0],
            )
            facts = gatherers.cycle_facts(mandate)(moment[0])
            assert facts is not None
            tick = d54._Tick.__new__(d54._Tick)
            tick.session, tick.gatherers, tick.mandate, tick.facts = (
                session,
                gatherers,
                mandate,
                facts,
            )
            first = tick.run(d54._exit())
            assert first.stage is loop.CycleStage.HANDOFF
            assert first.sizing is not None and first.sizing.quantity == 10
            moment[0] += timedelta(seconds=elapsed)
            second = tick.run(d54._exit(symbol="QQQ", target="CHR-ORD-" + "C" * 32))
            if elapsed:
                assert second.stage is loop.CycleStage.SIZING
                assert second.refusal == "STALE"
                assert second.sizing is None
            else:
                assert second.stage is loop.CycleStage.HANDOFF
                assert second.sizing is not None and second.sizing.quantity == 10
    finally:
        database.dispose()

"""Account evidence must remain usable through the last per-decision read."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from chronos.api.autonomy_wiring import BackendGatherers
from chronos.persistence.database import Database
from chronos.supervisor import durable, loop
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
                assert outcome.stage is loop.CycleStage.SIZING
                assert outcome.refusal == "STALE"
                assert "account summary freshness refused: stale" in outcome.detail
            else:
                assert sized == [(29, 10)]
                assert outcome.stage is loop.CycleStage.HANDOFF
    finally:
        database.dispose()

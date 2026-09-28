"""Storage write-path fault pins (VCP §6 EXIT "disk failure" class, write-path
depth — flow-team TG2, from READ-EXIT6 P-G2). tmp_path fixtures only; no fault
is ever pointed at a real state dir.

Three failure shapes, each with a mutation that turns it red:

1. An audit-log append into a read-only directory raises and creates nothing
   (fresh shape), and on a pre-existing good pair it raises having written
   exactly one COMPLETE record — the designed, documented crash window
   (``src/chronos/auditlog/log.py:33-37,49-56``: the record is fsynced before
   the anchor publish, so the pair reads BROKEN and every later writer refuses
   BEFORE writing). Mutation: making the append path's refusal (``_refuse``)
   swallow instead of raise.
2. ENOSPC at the append's write — injected by proxying ``os.fdopen`` so the
   write call on the append's descriptor raises before any byte is accepted
   (the C-level ``os.write`` inside buffered IO is not interceptable from
   Python; the effect point is identical). Zero bytes land, the pair stays
   VALID, and after remediation the next append re-chains from the last good
   record. Mutation: swallowing ``OSError`` around the log write/flush/fsync
   so append reports success with nothing durable.
3. The supervised cycle's decision write (``durable.record_outcome`` — the
   attempt counter and the decision journal commit in one transaction, so a
   counter can never claim a refusal the journal cannot account for): a write
   failure raises, and afterwards no attempt row and no chain row exist.
   Mutation: suppressing the journal failure inside ``record_outcome``.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.auditlog.log import AuditLog, AuditLogCorruptionError, ChainState, verify_chain
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
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.supervisor import alerts, proposals, queue
from chronos.supervisor import durable as dur
from chronos.supervisor.admission import AdmissionCheck, AdmissionOutcome, MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import HandoffResult
from chronos.supervisor.loop import CycleFacts
from chronos.supervisor.runtime import AutonomyRuntime, RuntimeConfig, TickReport
from chronos.supervisor.sizing import AccountEvidence

requires_nonroot = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory write permission bits"
)

_NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
_FINGERPRINT = "b" * 64
_STATIC_POSTURE = dur.DecisionPosture(
    registry_configured=False, evidence_binding=False, credential_epoch_bound=False
)


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@requires_nonroot
def test_1_append_into_read_only_directory_raises_and_creates_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    try:
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(state / "audit.jsonl").append("cycle", {"index": 0})
        # Byte length and last record are unchanged: there is no log at all,
        # and no sibling (lock, anchor, temp) was left behind either.
        assert sorted(entry.name for entry in state.iterdir()) == []
    finally:
        state.chmod(0o755)


@requires_nonroot
def test_1b_append_failure_on_a_good_pair_is_the_documented_crash_window(tmp_path: Path) -> None:
    """A pre-existing pair: the refusal lands AFTER the record's fsync (anchor
    publish is what the read-only dir refuses), so the log gains one COMPLETE
    record, the pair reads BROKEN, and every later writer refuses before
    writing — fail closed, never a truncated tail."""
    state = tmp_path / "state"
    state.mkdir()
    path = state / "audit.jsonl"
    log = AuditLog(path)
    log.append("cycle", {"index": 0})
    second = log.append("cycle", {"index": 1})
    head_before = second.record_hash
    assert verify_chain(path).state is ChainState.VALID

    state.chmod(0o555)
    try:
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(path).append("cycle", {"index": 2})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3, "the durable record is complete; nothing truncated"
        appended = json.loads(lines[-1])
        assert appended["previous_hash"] == head_before
        assert appended["record_hash"], "a full record, not a partial tail"
        assert verify_chain(path).state is ChainState.BROKEN, "crash window: log ahead of anchor"
        # Every later writer refuses BEFORE writing: the byte length is now frozen.
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(path).append("cycle", {"index": 3})
        assert path.read_text(encoding="utf-8").splitlines() == lines
    finally:
        state.chmod(0o755)


def test_2_enospc_at_the_append_write_leaves_zero_bytes_and_rechains_after_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("cycle", {"index": 0})
    second = log.append("cycle", {"index": 1})
    head_before = second.record_hash
    bytes_before = path.read_bytes()

    real_fdopen = os.fdopen

    class _EnospcWrite:
        """The append's descriptor, reading normally, refusing every write the
        way a full disk does — before any byte is accepted."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._wrapped.__exit__(*exc_info)

        def write(self, data):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def fdopen_with_enospc(descriptor, mode="r", *args, **kwargs):
        handle = real_fdopen(descriptor, mode, *args, **kwargs)
        if "+" in mode:
            return _EnospcWrite(handle)
        return handle

    monkeypatch.setattr("os.fdopen", fdopen_with_enospc)
    with pytest.raises(OSError) as raised:
        AuditLog(path).append("cycle", {"index": 2})
    assert raised.value.errno == errno.ENOSPC
    assert path.read_bytes() == bytes_before, "a refused write leaves zero bytes behind"
    assert verify_chain(path).state is ChainState.VALID, "the pair is untouched"

    monkeypatch.undo()
    third = AuditLog(path).append("cycle", {"index": 2})
    assert third.sequence == 2
    assert third.previous_hash == head_before, "re-chains from the last good record — no gap"
    verification = verify_chain(path)
    assert verification.state is ChainState.VALID, verification.detail
    assert "3 records" in verification.detail


def test_3_a_failed_decision_write_journals_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions() as session:
            outcome = AdmissionOutcome(
                admitted=True,
                checks=(AdmissionCheck(name="mandate", passed=True, detail="in scope"),),
                detail="admitted",
            )

            def fail_append(*args, **kwargs):
                raise OSError("private filesystem detail")

            monkeypatch.setattr(hash_chain, "append", fail_append)
            with pytest.raises(OSError):
                dur.record_outcome(
                    session,
                    account_fingerprint=_FINGERPRINT,
                    decision_id="d-faulted",
                    outcome=outcome,
                    now=_NOW,
                    posture=_STATIC_POSTURE,
                )
            # The caller's own transaction duty in this runtime-free unit
            # harness (record_outcome: "the caller's transaction") — NOT a
            # stand-in for the drain's exception branch, which the drain-level
            # pin below exercises for real.
            session.rollback()
            admitted, refusals = dur.load_attempts(session, account_fingerprint=_FINGERPRINT)
            assert "d-faulted" not in admitted, "no counter may claim what the journal cannot"
            assert "d-faulted" not in refusals
            stream = dur.stream_for(dur.DECISION_STREAM, _FINGERPRINT)
            assert hash_chain.head(session, stream) is None, "no journaled success"
    finally:
        database.dispose()


# --------------------------------------------------------------------------- #
# Contract 3 at the supervised-cycle boundary (TG2r2, Daybreak's round-2
# repair criterion): the same fault driven through the REAL drain —
# AutonomyRuntime._drain owns the exception branch (runtime.py:479-481) this
# pin observes. Harness mirrors tests/safety/test_autonomy_runtime.py.
# --------------------------------------------------------------------------- #

_ACCOUNT = "DU1234567"


class _NullSink:
    name = "null"

    def __init__(self) -> None:
        self.seen: list[alerts.OwnerAlert] = []

    def deliver(self, alert: alerts.OwnerAlert) -> bool:
        self.seen.append(alert)
        return True


def _cycle_identity() -> queue.HarnessIdentity:
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


def _cycle_mandate() -> AutonomyMandate:
    return AutonomyMandate(
        mandate_id="m-1",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=AutonomyMode.PAPER_AUTONOMOUS,
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        effective_from=_NOW - timedelta(hours=1),
        expires_at=_NOW + timedelta(days=1),
        versions=VersionPins(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        capital=CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        owner_authorization_ref="owner-1",
        authored_at=_NOW,
    )


def _cycle_facts() -> CycleFacts:
    return CycleFacts(
        account_fingerprint=_FINGERPRINT,
        account_id=_ACCOUNT,
        now=_NOW,
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
        multiplier=Decimal(1),
    )


def _cycle_payload() -> str:
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


def test_3_cycle_level_a_failed_decision_write_aborts_the_drain_before_any_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak's round-2 criterion: the decision-stream hash_chain.append
    fault is driven through AutonomyRuntime._drain — the production exception
    owner (runtime.py:479-481), whose rollback is what leaves neither the
    attempt row nor the decision-chain row. Mutations that turn this red:
    runtime.py:480 rollback→commit (the admitted attempt row PERSISTS without
    its chain row, observed from a fresh session), and try/except OSError:
    pass around durable.record_outcome at loop.py:568-575 (the cycle walks on
    to the handoff)."""
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        sessions = database.sessions
        mandate = _cycle_mandate()
        with sessions.begin() as session:
            dur.activate(
                session,
                account_fingerprint=_FINGERPRINT,
                mandate=mandate,
                owner_event_id="owner-event-1",
                now=_NOW,
                process_generation=7,
            )
        with sessions.begin() as session:
            enqueued = proposals.enqueue(
                session,
                account_fingerprint=_FINGERPRINT,
                payload=_cycle_payload(),
                now=_NOW,
            )
            assert enqueued.queued
        with sessions.begin() as session:
            batch = proposals.claim_batch(session, account_fingerprint=_FINGERPRINT, limit=10)
        assert len(batch) == 1, "the drain receives a genuine QueuedProposal"

        facts = _cycle_facts()
        handoff_calls: list[object] = []

        def spy_handoff(intent: object) -> HandoffResult:
            handoff_calls.append(intent)
            return HandoffResult.refused_not_sent(
                order_plane_code="READ_ONLY_LEASE", detail="spy handoff"
            )

        runtime = AutonomyRuntime(
            sessions=sessions,
            config=RuntimeConfig(account_fingerprint=_FINGERPRINT),
            identity=_cycle_identity(),
            mandate_source=lambda: mandate,
            gather_facts=lambda now: facts,
            sinks=(_NullSink(),),
            submit=spy_handoff,
        )

        real_append = hash_chain.append
        decision_stream = dur.stream_for(dur.DECISION_STREAM, _FINGERPRINT)

        def fail_append(*args, **kwargs):
            # The DECISION-journal write fails; every other chained write in
            # the process behaves (stream scoping KEPT — Daybreak ruled it
            # sound and load-bearing): a blanket fault lets the cycle die
            # later at the attempt reserve even when record_outcome's failure
            # is suppressed, and this pin passes for the wrong reason.
            if kwargs.get("stream") == decision_stream:
                raise OSError("private filesystem detail")
            return real_append(*args, **kwargs)

        monkeypatch.setattr(hash_chain, "append", fail_append)
        with pytest.raises(OSError):
            runtime._drain(batch, mandate, facts, _NOW, TickReport(at=_NOW))
        assert handoff_calls == [], "no submission/handoff once the decision write has failed"
        # The drain's own exception branch owns the rollback; the inspection
        # opens a NEW session from the sessionmaker, never the drain session.
        with sessions() as fresh:
            admitted, refusals = dur.load_attempts(fresh, account_fingerprint=_FINGERPRINT)
            assert admitted == frozenset() and refusals == {}, (
                "the drain's rollback left no attempt row behind"
            )
            assert hash_chain.head(fresh, decision_stream) is None, "no journaled success"
    finally:
        database.dispose()

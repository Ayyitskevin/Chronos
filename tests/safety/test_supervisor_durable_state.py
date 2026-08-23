"""The supervisor remembers, and its memory is tamper-evident (ADR-0016, M3).

M2's gateway was a pure function over state the caller assembled from nowhere.
Three guarantees were consequently unenforceable, and this module pins all
three now that they are real:

- **A loss or activity limit that is set is a limit that binds.** They were
  contract-only in M2 because nothing counted. The counters are durable, and a
  breach produces a `DegradedReason`, which stops new exposure while leaving
  risk reduction available — what a loss limit is *for*.
- **Authority survives a restart, or it does not survive at all.** Activation is
  a durable owner event, so `RestartBehavior.REQUIRE_REACTIVATION` means
  something. An in-memory activation could not have.
- **A refusal cannot be outwaited.** R-31's attempt counters used to reset on
  restart, so patience defeated the bound. They are durable now.

Everything append-only is hash-chained (`chronos.persistence.hash_chain`), so a
silently edited or deleted record is detectable. That is tamper-*evident*, not
tamper-proof; the honest bound is in the module docstring there.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from chronos.autonomy import (
    ActivityLimits,
    AITradeDecision,
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    DecisionProvenance,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    LossLimits,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    RestartBehavior,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyOwnerAlertRow, HashChainRow
from chronos.supervisor import (
    AdmissionRefusal,
    DegradedReason,
    MarketDataEvidence,
    admit,
    alerts,
)
from chronos.supervisor import durable as dur

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_TARGET_REF = "CHR-ORD-" + "A" * 32


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as db_session:
            yield db_session
    finally:
        database.dispose()


def _pins() -> VersionPins:
    return VersionPins(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
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
        "versions": _pins(),
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


def _decision(**overrides: Any) -> AITradeDecision:
    base: dict[str, Any] = {
        "decision_id": "d-1",
        "kind": DecisionKind.OPEN,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "requested_strategy": StrategyForm.LONG_EQUITY,
        "evidence": (
            EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_NOW, digest="c" * 64),
        ),
        "invalidation_conditions": ("closes below 400",),
        "provenance": DecisionProvenance(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
            evidence_bundle_id="eb-1",
            evidence_bundle_digest="b" * 64,
            produced_at=_NOW,
        ),
    }
    base.update(overrides)
    return AITradeDecision(**base)


def _state(session: Session, mandate: AutonomyMandate, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "account_fingerprint": _FINGERPRINT,
        "mandate": mandate,
        "now": _NOW,
        "process_generation": 7,
        "expected_evidence_bundle_id": "eb-1",
        "expected_evidence_bundle_digest": "b" * 64,
        "market_data": MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
    }
    base.update(overrides)
    return dur.build_state(session, **base)


def _activate(session: Session, mandate: AutonomyMandate, *, generation: int = 7) -> None:
    dur.activate(
        session,
        account_fingerprint=_FINGERPRINT,
        mandate=mandate,
        owner_event_id="owner-event-1",
        now=_NOW,
        process_generation=generation,
    )


# ------------------------------------------------------------------ authority


def test_a_database_with_no_activation_authorizes_nothing(session: Session) -> None:
    """Deny-by-default for state: absent is never permissive.

    A pre-M3 database upgrades to an empty supervisor memory. That must read as
    "no authority established", not "nothing has gone wrong yet".
    """

    mandate = _mandate()
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MANDATE_NOT_ACTIVATED


def test_activation_is_durable_and_admits(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is True


def test_revocation_is_recorded_in_place_rather_than_deleted(session: Session) -> None:
    """An audit trail that forgets a revocation cannot show when authority ended."""

    mandate = _mandate()
    _activate(session, mandate)
    assert (
        dur.revoke(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate_id="m-1",
            reason="owner stood the system down",
            now=_NOW,
        )
        is True
    )
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.MANDATE_REVOKED

    activation = dur.load_activation(session, account_fingerprint=_FINGERPRINT, mandate=mandate)
    assert activation is not None and activation.revoked is True


def test_revoking_nothing_reports_it_rather_than_appearing_to_succeed(session: Session) -> None:
    assert (
        dur.revoke(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate_id="never-activated",
            reason="x",
            now=_NOW,
        )
        is False
    )


def test_an_activation_for_another_account_does_not_carry_over(session: Session) -> None:
    mandate = _mandate()
    dur.activate(
        session,
        account_fingerprint="b" * 64,
        mandate=mandate,
        owner_event_id="owner-event-1",
        now=_NOW,
        process_generation=7,
    )
    assert dur.load_activation(session, account_fingerprint=_FINGERPRINT, mandate=mandate) is None


def test_reactivation_after_restart_is_enforceable_now_that_it_is_durable(
    session: Session,
) -> None:
    """The whole point of REQUIRE_REACTIVATION is surviving a restart.

    An in-memory activation could not express this: it vanished on restart, so
    the check could never distinguish "reactivated" from "never activated".
    """

    mandate = _mandate(restart_behavior=RestartBehavior.REQUIRE_REACTIVATION)
    _activate(session, mandate, generation=7)

    # Same process generation: the activation still applies.
    assert admit(_decision(), mandate, _state(session, mandate)).admitted is True

    # After a restart the generation advances and the old activation is stale.
    restarted = admit(_decision(), mandate, _state(session, mandate, process_generation=8))
    assert restarted.admitted is False
    assert restarted.refusal is AdmissionRefusal.MANDATE_NOT_ACTIVATED


def test_an_activation_must_cite_the_owner_event_that_authorized_it(session: Session) -> None:
    with pytest.raises(ValueError, match="owner event"):
        dur.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=_mandate(),
            owner_event_id="   ",
            now=_NOW,
            process_generation=7,
        )


# ------------------------------------------------------------- loss limits


def test_a_breached_session_loss_limit_stops_new_exposure(session: Session) -> None:
    """The M2 gap, closed: LossLimits was read by no code at all."""

    mandate = _mandate(loss=LossLimits(max_session_loss_usd=Decimal(1_000)))
    _activate(session, mandate)
    dur.record_activity(
        session,
        account_fingerprint=_FINGERPRINT,
        now=_NOW,
        realized_loss_usd=Decimal(1_000),
    )
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.DEGRADED_RISK_REDUCTION_ONLY
    assert "loss" in outcome.detail


def test_a_breached_loss_limit_still_permits_closing_the_position(session: Session) -> None:
    """Being at a loss limit is exactly when closing must remain possible.

    A limit that refused the close would trap the position it exists to cap --
    the same inversion the M2 review found in the degraded-state rule.
    """

    mandate = _mandate(loss=LossLimits(max_session_loss_usd=Decimal(1_000)))
    _activate(session, mandate)
    dur.record_activity(
        session,
        account_fingerprint=_FINGERPRINT,
        now=_NOW,
        realized_loss_usd=Decimal(5_000),
    )
    closing = _decision(
        kind=DecisionKind.CLOSE, requested_strategy=None, target_client_reference=_TARGET_REF
    )
    assert admit(closing, mandate, _state(session, mandate)).admitted is True


def test_a_loss_below_the_limit_does_not_stop_anything(session: Session) -> None:
    """Guard the guard: a limit that always fires is not a limit."""

    mandate = _mandate(loss=LossLimits(max_session_loss_usd=Decimal(1_000)))
    _activate(session, mandate)
    dur.record_activity(
        session, account_fingerprint=_FINGERPRINT, now=_NOW, realized_loss_usd=Decimal(999)
    )
    assert admit(_decision(), mandate, _state(session, mandate)).admitted is True


def test_peak_to_trough_drawdown_binds(session: Session) -> None:
    mandate = _mandate(loss=LossLimits(max_peak_to_trough_drawdown_usd=Decimal(2_000)))
    _activate(session, mandate)
    for equity in (Decimal(100_000), Decimal(101_000), Decimal(98_500)):
        dur.record_equity(session, account_fingerprint=_FINGERPRINT, equity_usd=equity, now=_NOW)
    counters = dur.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert counters.drawdown_usd == Decimal(2_500)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False
    assert "drawdown" in outcome.detail


def test_drawdown_is_never_negative_even_if_recorded_out_of_order() -> None:
    """A negative drawdown would WIDEN headroom -- bad bookkeeping as authority."""

    counters = dur.SessionCounters(
        session_date="2026-07-25",
        peak_equity_usd=Decimal(100),
        trough_equity_usd=Decimal(150),
    )
    assert counters.drawdown_usd == Decimal(0)
    assert counters.drawdown_pct == Decimal(0)


def test_drawdown_of_an_unseeded_session_is_zero_not_a_division_error() -> None:
    counters = dur.SessionCounters(session_date="2026-07-25")
    assert counters.drawdown_usd == Decimal(0)
    assert counters.drawdown_pct == Decimal(0)


# --------------------------------------------------------- activity limits


@pytest.mark.parametrize(
    ("limits", "kwargs"),
    [
        (ActivityLimits(max_orders_per_session=2), {"orders_submitted": 2}),
        (ActivityLimits(max_cancellations_per_session=3), {"cancellations": 3}),
        (ActivityLimits(max_replacements_per_session=1), {"replacements": 1}),
        (
            ActivityLimits(max_turnover_usd_per_session=Decimal(10_000)),
            {"turnover_usd": Decimal(10_000)},
        ),
    ],
)
def test_each_activity_ceiling_binds(
    session: Session, limits: ActivityLimits, kwargs: dict[str, Any]
) -> None:
    mandate = _mandate(activity=limits)
    _activate(session, mandate)
    dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, **kwargs)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False
    assert outcome.refusal is AdmissionRefusal.DEGRADED_RISK_REDUCTION_ONLY


def test_counters_accumulate_across_calls(session: Session) -> None:
    for _ in range(3):
        dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, orders_submitted=1)
    counters = dur.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert counters.orders_submitted == 3


def test_counters_are_per_session_not_cumulative_forever(session: Session) -> None:
    dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, orders_submitted=5)
    tomorrow = _NOW + timedelta(days=1)
    assert (
        dur.load_counters(session, account_fingerprint=_FINGERPRINT, now=tomorrow).orders_submitted
        == 0
    )


def test_counters_are_per_account(session: Session) -> None:
    dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, orders_submitted=5)
    other = dur.load_counters(session, account_fingerprint="b" * 64, now=_NOW)
    assert other.orders_submitted == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"orders_submitted": -1},
        {"cancellations": -1},
        {"replacements": -1},
        {"turnover_usd": Decimal(-1)},
        {"realized_loss_usd": Decimal(-1)},
    ],
)
def test_a_counter_may_never_be_decremented(session: Session, kwargs: dict[str, Any]) -> None:
    """A caller that could decrease a count could restore headroom under a ceiling."""

    with pytest.raises(ValueError, match="must not be negative"):
        dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, **kwargs)


def test_a_zero_ceiling_does_not_halt_on_the_first_tick(session: Session) -> None:
    """Documented asymmetry: for breach comparisons, zero reads as unset.

    Unlike a CapitalLimits ceiling -- where zero authorizes nothing -- a zero
    loss ceiling read as binding would refuse before any loss existed, since
    `0 >= 0`. The mandate validator is what requires a submitting mandate to
    state the limits it wants enforced.
    """

    mandate = _mandate(loss=LossLimits(), activity=ActivityLimits())
    _activate(session, mandate)
    assert admit(_decision(), mandate, _state(session, mandate)).admitted is True


def test_an_unrelated_degraded_reason_cannot_clear_a_breach(session: Session) -> None:
    """`extra_degraded_reasons` is appended, never used to override."""

    mandate = _mandate(activity=ActivityLimits(max_orders_per_session=1))
    _activate(session, mandate)
    dur.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW, orders_submitted=1)
    state = _state(
        session,
        mandate,
        extra_degraded_reasons=(
            DegradedReason(subsystem="unrelated", detail="", blocks_risk_reduction=False),
        ),
    )
    assert len(state.degraded_reasons) == 2
    assert admit(_decision(), mandate, state).admitted is False


# ------------------------------------------------- R-31: durable re-submission


def test_a_refusal_cannot_be_outwaited_by_restarting(session: Session) -> None:
    """The M2 residual, closed: attempt counters used to live in memory.

    A model that wanted to route around a refusal only had to wait for a
    restart. Rebuilding the state from the database -- which is what a restart
    does -- must not reset the budget.
    """

    mandate = _mandate()
    _activate(session, mandate)
    decision = _decision(decision_id="d-loop")
    for _ in range(3):
        state = _state(session, mandate)
        outcome = admit(
            decision,
            mandate,
            dur.build_state(
                session,
                account_fingerprint=_FINGERPRINT,
                mandate=mandate,
                now=_NOW,
                process_generation=7,
                expected_evidence_bundle_id="eb-2",  # forces a refusal
                expected_evidence_bundle_digest="b" * 64,
                market_data=state.market_data,
            ),
        )
        assert outcome.admitted is False
        dur.record_outcome(
            session,
            account_fingerprint=_FINGERPRINT,
            decision_id="d-loop",
            outcome=outcome,
            now=_NOW,
        )

    # A fresh state -- exactly what a restart produces -- still knows.
    exhausted = admit(decision, mandate, _state(session, mandate))
    assert exhausted.refusal is AdmissionRefusal.RESUBMISSION_EXHAUSTED


def test_an_admitted_decision_cannot_be_replayed_after_a_restart(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    decision = _decision(decision_id="d-once")
    outcome = admit(decision, mandate, _state(session, mandate))
    assert outcome.admitted is True
    dur.record_outcome(
        session,
        account_fingerprint=_FINGERPRINT,
        decision_id="d-once",
        outcome=outcome,
        now=_NOW,
    )
    replayed = admit(decision, mandate, _state(session, mandate))
    assert replayed.refusal is AdmissionRefusal.DECISION_REPLAY


# ------------------------------------------------------------- the hash chain


def test_every_judged_decision_lands_in_the_chain(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    dur.record_outcome(
        session,
        account_fingerprint=_FINGERPRINT,
        decision_id="d-1",
        outcome=outcome,
        now=_NOW,
    )
    stream = dur.stream_for(dur.DECISION_STREAM, _FINGERPRINT)
    verification = hash_chain.verify(session, stream)
    assert verification.ok is True
    assert verification.records == 1


def test_an_edited_record_breaks_the_chain(session: Session) -> None:
    """The property the database's append-only tables did not have.

    They were append-only *by convention* -- a statement about the code, not
    about the data. Anyone with the file could edit a row and nothing noticed.
    """

    stream = "test.stream"
    for index in range(3):
        hash_chain.append(session, stream=stream, kind="k", payload={"n": index}, recorded_at=_NOW)
    assert hash_chain.verify(session, stream).ok is True

    target = session.query(HashChainRow).filter_by(stream=stream, sequence=2).one()
    target.payload_json = '{"n":99}'
    session.flush()

    verification = hash_chain.verify(session, stream)
    assert verification.ok is False
    assert verification.broken_at == 2
    assert "modified after it was written" in verification.detail


def test_a_deleted_record_breaks_the_chain(session: Session) -> None:
    stream = "test.deleted"
    for index in range(3):
        hash_chain.append(session, stream=stream, kind="k", payload={"n": index}, recorded_at=_NOW)
    session.query(HashChainRow).filter_by(stream=stream, sequence=2).delete()
    session.flush()

    verification = hash_chain.verify(session, stream)
    assert verification.ok is False
    assert "deleted or renumbered" in verification.detail


def test_a_reordered_record_breaks_the_chain(session: Session) -> None:
    """Swapping two records' contents keeps every sequence number intact."""

    stream = "test.reorder"
    for index in range(3):
        hash_chain.append(session, stream=stream, kind="k", payload={"n": index}, recorded_at=_NOW)
    first = session.query(HashChainRow).filter_by(stream=stream, sequence=1).one()
    second = session.query(HashChainRow).filter_by(stream=stream, sequence=2).one()
    first.payload_json, second.payload_json = second.payload_json, first.payload_json
    session.flush()

    assert hash_chain.verify(session, stream).ok is False


def test_streams_are_independent(session: Session) -> None:
    """A break in one account's history must not invalidate another's."""

    hash_chain.append(session, stream="a", kind="k", payload={}, recorded_at=_NOW)
    hash_chain.append(session, stream="b", kind="k", payload={}, recorded_at=_NOW)
    session.query(HashChainRow).filter_by(stream="a").delete()
    session.flush()
    assert hash_chain.verify(session, "b").ok is True


def test_an_empty_chain_verifies(session: Session) -> None:
    verification = hash_chain.verify(session, "never.written")
    assert verification.ok is True
    assert verification.records == 0


def test_a_naive_timestamp_is_refused(session: Session) -> None:
    """A naive timestamp hashes differently depending on the reader's assumptions."""

    with pytest.raises(hash_chain.HashChainError, match="timezone-aware"):
        hash_chain.append(
            session,
            stream="s",
            kind="k",
            payload={},
            recorded_at=datetime(2026, 7, 25, 14, 0),  # noqa: DTZ001 - the point of the test
        )


def test_append_projects_only_a_bounded_head_link(session: Session) -> None:
    stream = "test.bounded-head"
    oversized_prior_payload = "x" * 100_000
    hash_chain.append(
        session,
        stream=stream,
        kind="k",
        payload={"padding": oversized_prior_payload},
        recorded_at=_NOW,
    )
    session.flush()
    executed_selects: list[str] = []
    assert session.bind is not None

    def _capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            executed_selects.append(statement.lower())

    event.listen(session.bind, "before_cursor_execute", _capture_statement)
    try:
        appended = hash_chain.append(
            session,
            stream=stream,
            kind="k",
            payload={"n": 2},
            recorded_at=_NOW,
        )
    finally:
        event.remove(session.bind, "before_cursor_execute", _capture_statement)

    assert appended.sequence == 2
    head_select = next(statement for statement in executed_selects if "order by" in statement)
    projection = head_select.partition("from hash_chain_records")[0]
    assert "payload_json" not in projection
    assert "length(cast(hash_chain_records.record_hash as text))" in projection
    assert "substr(cast(hash_chain_records.record_hash as text)" in projection


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("sequence", "9" * 100_000),
        ("record_hash", "x" * 100_000),
        ("sequence", b"\x80" * 100_000),
        ("record_hash", b"\x80" * 100_000),
    ],
)
def test_append_refuses_a_corrupt_dynamic_sqlite_head_field(
    session: Session,
    column: str,
    corrupt_value: object,
) -> None:
    stream = "test.corrupt-head"
    hash_chain.append(session, stream=stream, kind="k", payload={}, recorded_at=_NOW)
    session.flush()
    assert column in {"sequence", "record_hash"}
    session.connection().exec_driver_sql(
        f"UPDATE hash_chain_records SET {column} = ? WHERE stream = ?",
        (corrupt_value, stream),
    )

    with pytest.raises(hash_chain.HashChainError) as raised:
        hash_chain.append(session, stream=stream, kind="k", payload={}, recorded_at=_NOW)

    assert len(str(raised.value)) < 100


def test_payload_hashing_is_key_order_independent() -> None:
    """Verification must not depend on how an encoder happened to order keys."""

    assert hash_chain.canonical_payload({"b": 1, "a": 2}) == hash_chain.canonical_payload(
        {"a": 2, "b": 1}
    )


def test_a_record_cannot_be_moved_between_streams(session: Session) -> None:
    """The digest covers identity, not only payload."""

    hash_chain.append(session, stream="origin", kind="k", payload={"x": 1}, recorded_at=_NOW)
    row = session.query(HashChainRow).filter_by(stream="origin").one()
    row.stream = "elsewhere"
    session.flush()
    assert hash_chain.verify(session, "elsewhere").ok is False


def test_authority_events_are_chained_too(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    dur.revoke(
        session,
        account_fingerprint=_FINGERPRINT,
        mandate_id="m-1",
        reason="stand down",
        now=_NOW,
    )
    stream = dur.stream_for(dur.AUTHORITY_STREAM, _FINGERPRINT)
    verification = hash_chain.verify(session, stream)
    assert verification.ok is True
    assert verification.records == 2


def test_the_counter_and_its_journal_entry_commit_together(session: Session) -> None:
    """A counter that outlives its explanation is a counter nobody can audit."""

    mandate = _mandate()
    _activate(session, mandate)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    dur.record_outcome(
        session,
        account_fingerprint=_FINGERPRINT,
        decision_id="d-1",
        outcome=outcome,
        now=_NOW,
    )
    admitted, refusals = dur.load_attempts(session, account_fingerprint=_FINGERPRINT)
    chain = hash_chain.verify(session, dur.stream_for(dur.DECISION_STREAM, _FINGERPRINT))
    assert admitted == frozenset({"d-1"})
    assert refusals == {}
    assert chain.records == 1


# ------------------------------------------------------------- owner alerting
#
# ADR-0016 section 8's fourth clause: "record the denial, AND alert the owner."
# M2 did the first three. These pin the fourth, including its honest boundary:
# the channel is pull, not push, and an owner who is not looking is not told.


def test_a_degraded_refusal_alerts_the_owner(session: Session) -> None:
    mandate = _mandate(loss=LossLimits(max_session_loss_usd=Decimal(100)))
    _activate(session, mandate)
    dur.record_activity(
        session, account_fingerprint=_FINGERPRINT, now=_NOW, realized_loss_usd=Decimal(500)
    )
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is False

    alerts.alert_for_refusal(
        session,
        account_fingerprint=_FINGERPRINT,
        decision_id="d-1",
        outcome=outcome,
        now=_NOW,
    )
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert len(pending) == 1
    assert pending[0].severity is alerts.AlertSeverity.WARNING


def test_an_ordinary_scope_refusal_does_not_alert(session: Session) -> None:
    """A gateway that alerts on every disagreement trains the owner to ignore it.

    Then the alert that matters is ignored too. Alerting is deliberately narrow:
    it fires when the SYSTEM is wrong, not when the model is.
    """

    mandate = _mandate()
    _activate(session, mandate)
    outcome = admit(_decision(symbol="TSLA"), mandate, _state(session, mandate))
    assert outcome.refusal is AdmissionRefusal.INSTRUMENT_NOT_PERMITTED

    assert (
        alerts.alert_for_refusal(
            session,
            account_fingerprint=_FINGERPRINT,
            decision_id="d-1",
            outcome=outcome,
            now=_NOW,
        )
        is None
    )
    assert alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT) == ()


def test_an_admitted_decision_raises_no_alert(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = admit(_decision(), mandate, _state(session, mandate))
    assert outcome.admitted is True
    assert (
        alerts.alert_for_refusal(
            session,
            account_fingerprint=_FINGERPRINT,
            decision_id="d-1",
            outcome=outcome,
            now=_NOW,
        )
        is None
    )


def test_repeat_conditions_fold_into_one_row(session: Session) -> None:
    """A degraded loop must not bury every other alert under identical entries."""

    for index in range(500):
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.WARNING,
            kind="degraded.market_data",
            summary="stale quotes",
            now=_NOW + timedelta(seconds=index),
        )
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert len(pending) == 1
    assert pending[0].occurrences == 500
    assert pending[0].is_recurring is True


def test_severity_escalates_but_never_downgrades(session: Session) -> None:
    """A later INFO must not quietly downgrade a CRITICAL nobody has seen."""

    alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.CRITICAL,
        kind="degraded.broker",
        summary="broker unreachable",
        now=_NOW,
    )
    alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.INFO,
        kind="degraded.broker",
        summary="broker flapped",
        now=_NOW,
    )
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert pending[0].severity is alerts.AlertSeverity.CRITICAL


def test_acknowledgement_requires_a_note(session: Session) -> None:
    """ "Acknowledged" with no reason is indistinguishable from a stray click."""

    row = alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.WARNING,
        kind="k",
        summary="s",
        now=_NOW,
    )
    with pytest.raises(ValueError, match="requires a note"):
        alerts.acknowledge(
            session, account_fingerprint=_FINGERPRINT, alert_id=row.id, note="  ", now=_NOW
        )


def test_an_acknowledged_alert_is_kept_not_deleted(session: Session) -> None:
    """An alert that can vanish cannot prove the owner was told."""

    row = alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.WARNING,
        kind="k",
        summary="s",
        now=_NOW,
    )
    assert (
        alerts.acknowledge(
            session,
            account_fingerprint=_FINGERPRINT,
            alert_id=row.id,
            note="investigated; stale feed, restarted",
            now=_NOW,
        )
        is True
    )
    assert alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT) == ()
    persisted = session.get(AutonomyOwnerAlertRow, row.id)
    assert persisted is not None
    assert persisted.acknowledged_note.startswith("investigated")


def test_acknowledging_the_same_alert_twice_reports_it(session: Session) -> None:
    row = alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.INFO,
        kind="k",
        summary="s",
        now=_NOW,
    )
    assert alerts.acknowledge(
        session, account_fingerprint=_FINGERPRINT, alert_id=row.id, note="seen", now=_NOW
    )
    assert not alerts.acknowledge(
        session, account_fingerprint=_FINGERPRINT, alert_id=row.id, note="again", now=_NOW
    )


def test_a_new_occurrence_after_acknowledgement_opens_a_fresh_alert(session: Session) -> None:
    """Otherwise a resolved-then-recurring condition would stay silent."""

    first = alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.WARNING,
        kind="degraded.broker",
        summary="unreachable",
        now=_NOW,
    )
    alerts.acknowledge(
        session, account_fingerprint=_FINGERPRINT, alert_id=first.id, note="fixed", now=_NOW
    )
    alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.WARNING,
        kind="degraded.broker",
        summary="unreachable again",
        now=_NOW + timedelta(hours=1),
    )
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert len(pending) == 1
    assert pending[0].occurrences == 1


def test_alerts_are_per_account(session: Session) -> None:
    alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.CRITICAL,
        kind="k",
        summary="s",
        now=_NOW,
    )
    assert alerts.unacknowledged(session, account_fingerprint="b" * 64) == ()


def test_alerts_sort_most_severe_first(session: Session) -> None:
    for severity, kind in (
        (alerts.AlertSeverity.INFO, "a"),
        (alerts.AlertSeverity.CRITICAL, "b"),
        (alerts.AlertSeverity.WARNING, "c"),
    ):
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=severity,
            kind=kind,
            summary=kind,
            now=_NOW,
        )
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert [alert.severity for alert in pending] == [
        alerts.AlertSeverity.CRITICAL,
        alerts.AlertSeverity.WARNING,
        alerts.AlertSeverity.INFO,
    ]


def test_a_severity_threshold_filters(session: Session) -> None:
    alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.INFO,
        kind="k",
        summary="s",
        now=_NOW,
    )
    assert (
        alerts.unacknowledged(
            session,
            account_fingerprint=_FINGERPRINT,
            minimum_severity=alerts.AlertSeverity.WARNING,
        )
        == ()
    )


def test_degradation_alerts_even_when_no_decision_arrived(session: Session) -> None:
    """A system that only reports problems when a model asks stays silent
    exactly when the model plane is the thing that failed."""

    raised = alerts.alert_for_degradation(
        session,
        account_fingerprint=_FINGERPRINT,
        reasons=(
            DegradedReason(subsystem="model", detail="worker down", blocks_risk_reduction=False),
            DegradedReason(subsystem="reconciliation", detail="positions unproven"),
        ),
        now=_NOW,
    )
    assert len(raised) == 2
    pending = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    # The one that blocks risk reduction is the more serious of the two.
    assert pending[0].severity is alerts.AlertSeverity.CRITICAL
    assert pending[0].kind == "degraded.reconciliation"


def test_alerts_are_chained(session: Session) -> None:
    row = alerts.raise_alert(
        session,
        account_fingerprint=_FINGERPRINT,
        severity=alerts.AlertSeverity.WARNING,
        kind="k",
        summary="s",
        now=_NOW,
    )
    alerts.acknowledge(
        session, account_fingerprint=_FINGERPRINT, alert_id=row.id, note="seen", now=_NOW
    )
    verification = hash_chain.verify(session, f"{alerts.ALERT_STREAM}:{_FINGERPRINT}")
    assert verification.ok is True
    assert verification.records == 2


def test_the_alerting_module_opens_no_network_connection() -> None:
    """The honest boundary, enforced structurally rather than promised in prose.

    The docstring says this is a pull channel with no outbound delivery. If
    someone later adds an SMTP or webhook sender here, that claim becomes false
    silently -- and a half-working push channel is worse than none, because
    "no alerts" stops meaning "unknown" and starts meaning "all clear".
    """

    import ast
    import inspect as inspect_module

    from chronos.supervisor import alerts as alerts_module

    tree = ast.parse(inspect_module.getsource(alerts_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = {
        "smtplib",
        "socket",
        "http",
        "http.client",
        "urllib",
        "urllib.request",
        "requests",
        "httpx",
        "email",
        "subprocess",
    }
    offenders = [
        name for name in imported if name.split(".")[0] in {f.split(".")[0] for f in forbidden}
    ]
    assert offenders == [], (
        f"the alerting module gained an egress dependency: {offenders}. Out-of-band "
        "delivery is a disclosed gap and a promotion criterion, not something to add "
        "quietly next to a trading system."
    )

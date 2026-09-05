"""One proposal, all the way through, or refused (ADR-0016, M5).

Every milestone before this built a stage and disclosed that nothing called it.
`run_cycle` is the caller, and these tests pin what the assembled pipeline
guarantees.

- **No second submission path.** The cycle hands a compiled intent to a callable
  and learns the result. It has no broker, no transmit, and no way to acquire
  one -- `chronos.orders` stays the single canonical execution plane.
- **Non-live by default, structurally.** Omitting the handoff is SHADOW. A
  caller who has not thought about the last step gets no order.
- **The ingress trusts nothing.** It is a process boundary, so its inputs are
  treated as hostile: oversized, malformed, non-finite, over-nested, and
  self-attributing payloads are each refused without raising.
- **Every cycle is journalled, especially the refusals.** "Why did it not trade"
  is asked far more often than its opposite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    LossLimits,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    ProposedDecision,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.domain.models import UnderlyingContract
from chronos.orders.intent import WheelOrderIntent
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import HashChainRow
from chronos.supervisor import durable, ingress, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import HandoffResult
from chronos.supervisor.loop import CycleFacts, CycleStage, run_cycle
from chronos.supervisor.sizing import AccountEvidence

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_ACCOUNT = "DU1234567"


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    # A plain session, not ``sessionmaker.begin()``: since ADR-0052 a cycle with a
    # handoff commits before the wire, and SQLAlchemy refuses to operate on a
    # transaction closed inside a context manager. Production's drain holds a
    # plain session for the same reason, so this is also the truer harness.
    db_session = database.sessions()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.close()
        database.dispose()


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


def _facts(**overrides: Any) -> CycleFacts:
    base: dict[str, Any] = {
        "account_fingerprint": _FINGERPRINT,
        "account_id": _ACCOUNT,
        "now": _NOW,
        "process_generation": 7,
        "evidence_bundle_id": "eb-1",
        "evidence_bundle_digest": "b" * 64,
        "market_data": MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        "account": AccountEvidence(
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
        "quote": QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02")),
        "contract": UnderlyingContract(con_id=111, symbol="SPY"),
        "reference_price": Decimal(400),
        "multiplier": Decimal(1),
    }
    base.update(overrides)
    return CycleFacts(**base)


def _proposal(**overrides: Any) -> ProposedDecision:
    base: dict[str, Any] = {
        "kind": DecisionKind.OPEN,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "requested_strategy": StrategyForm.LONG_EQUITY,
        "requested_quantity": Decimal(10),
        "evidence": (
            EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_NOW, digest="c" * 64),
        ),
        "invalidation_conditions": ("closes below 400",),
    }
    base.update(overrides)
    return ProposedDecision(**base)


def _activate(session: Session, mandate: AutonomyMandate) -> None:
    durable.activate(
        session,
        account_fingerprint=_FINGERPRINT,
        mandate=mandate,
        owner_event_id="owner-event-1",
        now=_NOW,
        process_generation=7,
    )


def _run(session: Session, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "payload": _proposal(),
        "session": session,
        "mandate": _mandate(),
        "identity": _identity(),
        "facts": _facts(),
        # ADR-0052: a configured handoff without this seam refuses. The helper
        # supplies what production supplies; the shadow cases pass no submit and
        # return before it is consulted.
        "commit_before_handoff": session.commit,
    }
    base.update(overrides)
    payload = base.pop("payload")
    return run_cycle(payload, **base)


# ------------------------------------------------------------- the full walk


def test_a_good_proposal_walks_every_stage_and_compiles(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate)
    # No handoff supplied: the walk completed and nothing was sent.
    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "NO_SUBMISSION_CONFIGURED"
    assert outcome.intent is not None
    assert outcome.intent.quantity == Decimal(10)
    assert outcome.admission is not None and outcome.admission.admitted is True


def test_omitting_the_handoff_is_shadow_and_is_the_default(session: Session) -> None:
    """A caller who has not thought about the last step gets no order."""

    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate)
    assert outcome.intent is not None  # it compiled
    assert outcome.handoff is None  # and went nowhere


def test_a_supplied_handoff_receives_the_compiled_intent(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    received: list[WheelOrderIntent] = []

    def _submit(intent: WheelOrderIntent) -> HandoffResult:
        received.append(intent)
        return HandoffResult.submitted(detail="accepted-by-order-plane")

    outcome = _run(session, mandate=mandate, submit=_submit)
    # COMPLETE means a CONFIRMED send and nothing weaker (A1): the handoff has to
    # say so in the supervisor's own vocabulary, not merely fail to raise.
    assert outcome.stage is CycleStage.COMPLETE
    assert outcome.handoff == HandoffResult.submitted(detail="accepted-by-order-plane")
    assert len(received) == 1
    # What the order plane received is a plain intent, not a live order.
    assert received[0].to_order_request().transmit is False


def test_the_cycle_has_no_way_to_transmit() -> None:
    """Structural: the loop must not import anything that can reach a venue."""

    import ast
    import inspect

    from chronos.supervisor import loop as loop_module

    tree = ast.parse(inspect.getsource(loop_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    for name in imported:
        assert not name.startswith(("chronos.broker", "chronos.execution")), name
    # It may name the intent type; it may not reach the submission boundary.
    assert "chronos.orders.submission" not in imported
    assert "chronos.orders.service" not in imported


# --------------------------------------------------------- refusals in order


def test_no_mandate_stops_at_admission(session: Session) -> None:
    outcome = _run(session, mandate=None)
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.refusal == "NO_ACTIVE_MANDATE"
    assert outcome.intent is None


def test_no_activation_stops_at_admission(session: Session) -> None:
    """Deny-by-default: a mandate in storage authorizes nothing on its own."""

    outcome = _run(session)
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.refusal == "MANDATE_NOT_ACTIVATED"


def test_a_non_submitting_mode_stops_before_compilation(session: Session) -> None:
    mandate = _mandate(mode=AutonomyMode.SHADOW)
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate)
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.refusal == "MODE_CANNOT_SUBMIT"
    assert outcome.compilation is None


def test_an_out_of_scope_symbol_stops_at_admission(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate, payload=_proposal(symbol="TSLA"))
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.refusal == "INSTRUMENT_NOT_PERMITTED"


def test_an_unsizeable_proposal_stops_at_sizing(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    facts = _facts(
        account=AccountEvidence(
            net_liquidation_usd=Decimal(100_000),
            total_cash_usd=Decimal(1_000),  # exactly the cash floor: nothing spendable
            buying_power_usd=Decimal(500),
            symbol_exposure_usd=Decimal(0),
            gross_exposure_usd=Decimal(0),
            net_exposure_usd=Decimal(0),
            position_notional_usd=Decimal(0),
            maintenance_margin_usd=Decimal(0),
            deployed_capital_usd=Decimal(0),
        )
    )
    outcome = _run(session, mandate=mandate, facts=facts)
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "NO_EXECUTABLE_SIZE"
    assert outcome.intent is None


def test_an_unresolvable_contract_stops_at_compilation(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate, facts=_facts(contract=None))
    assert outcome.stage is CycleStage.COMPILATION
    assert outcome.intent is None


def test_an_order_plane_refusal_is_recorded_not_raised(session: Session) -> None:
    """The order plane's gates are the authority; the cycle records that it ran."""

    mandate = _mandate()
    _activate(session, mandate)

    def _refusing(intent: WheelOrderIntent) -> None:
        raise RuntimeError("kill switch engaged")

    outcome = _run(session, mandate=mandate, submit=_refusing)
    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "ORDER_PLANE_REFUSED"
    # The exception's TYPE is recorded, never its message: a hostile proposal
    # must not be able to write chosen text into an operator's logs.
    assert "RuntimeError" in outcome.detail
    assert "kill switch" not in outcome.detail


# --------------------------------------------------- counters and journalling


def test_a_completed_cycle_advances_the_session_counters(session: Session) -> None:
    """M3 built the counters; M5 is what finally feeds them."""

    mandate = _mandate()
    _activate(session, mandate)
    _run(session, mandate=mandate, submit=lambda intent: HandoffResult.submitted())
    counters = durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert counters.orders_submitted == 1
    assert counters.turnover_usd == Decimal(4_000)  # 10 shares x 400


def test_a_refused_cycle_does_not_advance_the_order_counter(session: Session) -> None:
    _run(session)  # refused at admission (no activation)
    counters = durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert counters.orders_submitted == 0


def test_counting_happens_at_handoff_because_limits_bound_attempts(
    session: Session,
) -> None:
    """An order that was sent and then rejected still consumed an attempt.

    Counting at fill instead would let a system that is being rejected by the
    venue retry without limit, which is exactly when a rate limit matters.

    **Rewritten 2026-08-12 (A1).** This test used to pass
    ``{"submitted": False}`` — a dict whose only readable content said the order
    was NOT submitted — and assert that the counter advanced anyway. It read as
    proof of the "attempts, not fills" rule while actually pinning the defect
    beneath it: the cycle could not tell a venue rejection from a refusal before
    the wire, so it counted both. The rule survives; what proves it is now a
    typed rejection *after* the send. The refusal-before-the-wire case, which
    must NOT count, is proven in
    ``tests/safety/test_typed_handoff_outcomes_exercised.py``.
    """

    mandate = _mandate()
    _activate(session, mandate)
    _run(
        session,
        mandate=mandate,
        submit=lambda intent: HandoffResult.rejected_after_send(
            order_plane_code="BROKER_SUBMIT_FAILED",
            detail="the venue answered REJECTED",
        ),
    )
    assert (
        durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW).orders_submitted
        == 1
    )


def test_the_activity_limit_stops_the_next_cycle(session: Session) -> None:
    """End to end: M3's counters, fed by M5, actually bind."""

    from chronos.autonomy import ActivityLimits

    mandate = _mandate(activity=ActivityLimits(max_orders_per_session=1))
    _activate(session, mandate)
    first = _run(session, mandate=mandate, submit=lambda intent: HandoffResult.submitted())
    assert first.stage is CycleStage.COMPLETE

    second = _run(
        session,
        mandate=mandate,
        submit=lambda intent: HandoffResult.submitted(),
        payload=_proposal(requested_quantity=Decimal(5)),
    )
    assert second.stage is CycleStage.ADMISSION
    assert second.refusal == "DEGRADED_RISK_REDUCTION_ONLY"


def test_a_loss_limit_breach_stops_the_next_cycle_but_permits_closing(
    session: Session,
) -> None:
    mandate = _mandate(loss=LossLimits(max_session_loss_usd=Decimal(500)))
    _activate(session, mandate)
    durable.record_activity(
        session,
        account_fingerprint=_FINGERPRINT,
        now=_NOW,
        realized_loss_usd=Decimal(600),
    )
    opening = _run(session, mandate=mandate)
    assert opening.refusal == "DEGRADED_RISK_REDUCTION_ONLY"

    closing = _run(
        session,
        mandate=mandate,
        payload=_proposal(
            kind=DecisionKind.CLOSE,
            requested_strategy=None,
            requested_quantity=None,
            target_client_reference="CHR-ORD-" + "A" * 32,
        ),
        facts=_facts(
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
                position_quantity=Decimal(10),
            )
        ),
    )
    assert closing.intent is not None, closing.refusal


def test_every_cycle_is_journalled_including_refusals(session: Session) -> None:
    _run(session)  # refused
    mandate = _mandate()
    _activate(session, mandate)
    _run(session, mandate=mandate)  # shadow

    verification = hash_chain.verify(session, durable.stream_for("autonomy.cycles", _FINGERPRINT))
    assert verification.ok is True
    assert verification.records == 2


def test_a_refusal_alerts_only_when_the_system_is_wrong(session: Session) -> None:
    from chronos.supervisor import alerts

    mandate = _mandate()
    _activate(session, mandate)
    _run(session, mandate=mandate, payload=_proposal(symbol="TSLA"))
    assert alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT) == ()


# ------------------------------------------------------- the hostile ingress


def _payload(**overrides: Any) -> str:
    body: dict[str, Any] = {
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
    body.update(overrides)
    return json.dumps(body)


@st.composite
def _structured_ingress_cases(draw: Any) -> tuple[bytes | str, bool]:
    """Bounded hostile shapes plus honest proposals, generated deterministically."""

    shape = draw(st.sampled_from(("valid", "text", "bytes", "nested", "integer")))
    if shape == "valid":
        quantity = draw(st.integers(min_value=1, max_value=1_000))
        return _payload(requested_quantity=str(quantity)), True
    if shape == "text":
        suffix = draw(st.text(alphabet='abcXYZ0123 {}[],:"', max_size=128))
        return "not-json:" + suffix, False
    if shape == "bytes":
        suffix = draw(st.binary(max_size=128))
        return b"\xff" + suffix, False
    if shape == "nested":
        depth = draw(st.integers(min_value=0, max_value=ingress.MAX_NESTING_DEPTH + 5))
        value: Any = "leaf"
        for _ in range(depth):
            value = {"n": value}
        return json.dumps({"nested": value}), False

    digit_count = draw(st.integers(min_value=4_298, max_value=4_310))
    sign = draw(st.sampled_from(("", "-")))
    template = draw(
        st.sampled_from(
            (
                '{"requested_quantity":%s}',
                '{"nested":{"value":%s}}',
                "[%s]",
            )
        )
    )
    return template % (sign + ("1" * digit_count)), False


def test_a_well_formed_payload_parses() -> None:
    outcome = ingress.parse_proposal(_payload())
    assert outcome.accepted is True
    assert outcome.proposal is not None
    assert outcome.proposal.symbol == "SPY"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", "empty payload"),
        (b"\xff\xfe", "not valid UTF-8"),
        ("not json", "well-formed JSON"),
        ('{"a": 1} {"b": 2}', "well-formed JSON"),
        ("[1,2,3]", "must be a JSON object"),
        ('{"kind": NaN}', "NaN"),
    ],
)
def test_malformed_payloads_refuse_without_raising(payload: Any, expected: str) -> None:
    """A hostile worker must not be able to crash the broker-holding process."""

    outcome = ingress.parse_proposal(payload)
    assert outcome.accepted is False
    assert expected.lower() in outcome.refusal.lower()


@pytest.mark.parametrize(
    ("digit_count", "expected"),
    [
        (4_300, "not a valid proposal"),
        (4_301, "not a single well-formed JSON document"),
    ],
)
def test_json_integer_conversion_boundary_refuses_without_raising(
    digit_count: int, expected: str
) -> None:
    payload = '{"requested_quantity":' + ("1" * digit_count) + "}"

    outcome = ingress.parse_proposal(payload)

    assert outcome.accepted is False
    assert f"payload is {expected}" in outcome.refusal
    assert "1" * 128 not in outcome.refusal


@settings(max_examples=64, derandomize=True, deadline=None)
@given(case=_structured_ingress_cases())
def test_structured_hostile_ingress_never_raises_and_valid_proposals_still_parse(
    case: tuple[bytes | str, bool],
) -> None:
    payload, should_accept = case

    outcome = ingress.parse_proposal(payload)

    assert outcome.accepted is should_accept


def test_an_oversized_payload_is_refused_by_length_before_parsing() -> None:
    """Refused by size, not by exhausting memory proving it invalid."""

    outcome = ingress.parse_proposal("x" * (ingress.MAX_PAYLOAD_BYTES + 1))
    assert outcome.accepted is False
    assert "ingress limit" in outcome.refusal


def test_a_deeply_nested_payload_is_refused() -> None:
    nested: Any = "leaf"
    for _ in range(ingress.MAX_NESTING_DEPTH + 5):
        nested = {"n": nested}
    outcome = ingress.parse_proposal(json.dumps(nested))
    assert outcome.accepted is False


@pytest.mark.parametrize("field", ["provenance", "decision_id"])
def test_a_self_attributing_payload_is_refused_loudly(field: str) -> None:
    """Refused rather than stripped: a sender who TRIED is worth knowing about."""

    outcome = ingress.parse_proposal(_payload(**{field: "forged"}))
    assert outcome.accepted is False
    assert "writer-owned" in outcome.refusal
    assert field in outcome.refusal


def test_a_refusal_never_echoes_payload_content() -> None:
    """Otherwise a hostile worker writes chosen text into operator logs."""

    marker = "PWNED-BY-THE-PAYLOAD"
    outcome = ingress.parse_proposal(_payload(symbol=marker + "!!!"))
    assert outcome.accepted is False
    assert marker not in outcome.refusal


def test_an_infinite_quantity_is_refused() -> None:
    """NaN > limit is False, so a naive ceiling check would pass it."""

    outcome = ingress.parse_proposal('{"kind": "OPEN", "requested_quantity": Infinity}')
    assert outcome.accepted is False


def test_a_hostile_payload_flows_through_the_cycle_as_a_refusal(session: Session) -> None:
    outcome = _run(session, payload=b"{ not json")
    assert outcome.stage is CycleStage.INGRESS
    assert outcome.intent is None


# ------------------------------------------------ R-34: the session boundary


def test_the_session_boundary_defaults_to_the_utc_day() -> None:
    """Unchanged behaviour when no market is named -- right for a UTC audit."""

    assert durable.session_key(_NOW) == "2026-07-25"


def test_naming_a_market_moves_the_boundary_to_its_local_day() -> None:
    """The R-34 defect: a US session in UTC rolls mid-afternoon local.

    20:00 UTC is 16:00 in New York -- the same trading afternoon. 02:00 UTC the
    next day is 22:00 the previous evening in New York, still the same session
    date locally, but a UTC-keyed counter would already have rolled.
    """

    afternoon = datetime(2026, 7, 25, 20, 0, tzinfo=UTC)
    evening = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
    assert durable.session_key(afternoon, market_timezone="America/New_York") == "2026-07-25"
    assert durable.session_key(evening, market_timezone="America/New_York") == "2026-07-25"
    # ...whereas the UTC day has already rolled, which is the bug.
    assert durable.session_key(evening) == "2026-07-26"


def test_an_unknown_timezone_raises_rather_than_falling_back_to_utc() -> None:
    """A silent fallback is wrong in exactly the way nobody notices."""

    with pytest.raises(ValueError, match="unknown market timezone"):
        durable.session_key(_NOW, market_timezone="Mars/Olympus_Mons")


def test_counters_roll_on_the_market_day_when_one_is_named(session: Session) -> None:
    durable.record_activity(
        session,
        account_fingerprint=_FINGERPRINT,
        now=datetime(2026, 7, 25, 20, 0, tzinfo=UTC),
        orders_submitted=1,
        market_timezone="America/New_York",
    )
    # Later the same New York session, but the next UTC day.
    still_same = durable.load_counters(
        session,
        account_fingerprint=_FINGERPRINT,
        now=datetime(2026, 7, 26, 2, 0, tzinfo=UTC),
        market_timezone="America/New_York",
    )
    assert still_same.orders_submitted == 1


def test_the_cycle_honours_the_configured_market_timezone(session: Session) -> None:
    mandate = _mandate()
    _activate(session, mandate)
    facts = _facts(now=datetime(2026, 7, 25, 20, 0, tzinfo=UTC), market_timezone="America/New_York")
    _run(session, mandate=mandate, facts=facts, submit=lambda intent: HandoffResult.submitted())
    counters = durable.load_counters(
        session,
        account_fingerprint=_FINGERPRINT,
        now=datetime(2026, 7, 26, 2, 0, tzinfo=UTC),
        market_timezone="America/New_York",
    )
    assert counters.orders_submitted == 1


# ------------------------------ ADR-0052: no durable seam, no handoff


def test_a_configured_handoff_without_the_durable_seam_refuses_and_sends_nothing(
    session: Session,
) -> None:
    """Deny-by-default applies to a missing mechanism, not only a missing fact.

    ``commit_before_handoff`` is what makes the pre-wire reservation survive a
    crash. A caller that wired submission but not the seam has not proven it can
    spend the mandate's budget durably, and the tempting reading — "then just
    behave the way it did before ADR-0052" — is precisely the one that lets an
    unattended backend transmit on accounting a power cut can undo. So the cycle
    refuses at the door, journals which mechanism was absent, and the handoff is
    never called.
    """

    mandate = _mandate()
    _activate(session, mandate)
    called: list[Any] = []
    outcome = _run(
        session,
        mandate=mandate,
        submit=lambda intent: called.append(intent) or HandoffResult.submitted(),
        commit_before_handoff=None,
    )

    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "NO_DURABLE_RESERVATION"
    assert called == [], "the handoff must not be reached without the durable seam"
    with_counters = durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert with_counters.orders_submitted == 0, "a refusal at the door spends nothing"


def test_shadow_is_unaffected_by_the_seam_requirement(session: Session) -> None:
    """No submit configured is still SHADOW, seam or not — the walk runs, nothing is sent."""

    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(session, mandate=mandate, commit_before_handoff=None)

    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "NO_SUBMISSION_CONFIGURED"


# ------------------------------- ADR-0055: a half-bound row is not a bound row


def test_a_row_with_an_epoch_but_no_entry_digest_reads_unbound(session: Session) -> None:
    """ADR-0048 binds a row with BOTH values; the posture must not call one of them enough.

    The drain refuses a half-bound row before admission, so this state never
    reaches the journal through the runtime — which is exactly why the rule needs
    a direct pin: nothing else would notice if the posture started judging on the
    epoch alone.
    """

    mandate = _mandate()
    _activate(session, mandate)
    outcome = _run(
        session,
        mandate=mandate,
        registry_configured=True,
        proposer_credential_epoch="e" * 64,
        proposer_registry_entry_digest=None,
    )
    assert outcome.admission is not None and outcome.admission.admitted
    stream = durable.stream_for(durable.DECISION_STREAM, _FINGERPRINT)
    rows = session.query(HashChainRow).filter(HashChainRow.stream == stream).all()
    assert [row.kind for row in rows] == ["admitted"]
    posture = json.loads(rows[0].payload_json)["posture"]
    assert posture["credential_epoch_bound"] is False
    assert posture["identity"] == "static", (
        "a registry alone does not authenticate a half-bound row"
    )
    assert posture["registry"] == "configured"

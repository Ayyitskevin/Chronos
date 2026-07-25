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
from chronos.supervisor import durable, ingress, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts, CycleStage, run_cycle
from chronos.supervisor.sizing import AccountEvidence

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_ACCOUNT = "DU1234567"


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as db_session:
            yield db_session
    finally:
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

    def _submit(intent: WheelOrderIntent) -> str:
        received.append(intent)
        return "accepted-by-order-plane"

    outcome = _run(session, mandate=mandate, submit=_submit)
    assert outcome.stage is CycleStage.COMPLETE
    assert outcome.handoff == "accepted-by-order-plane"
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
    _run(session, mandate=mandate, submit=lambda intent: "ok")
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
    """

    mandate = _mandate()
    _activate(session, mandate)
    _run(session, mandate=mandate, submit=lambda intent: {"submitted": False})
    assert (
        durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW).orders_submitted
        == 1
    )


def test_the_activity_limit_stops_the_next_cycle(session: Session) -> None:
    """End to end: M3's counters, fed by M5, actually bind."""

    from chronos.autonomy import ActivityLimits

    mandate = _mandate(activity=ActivityLimits(max_orders_per_session=1))
    _activate(session, mandate)
    first = _run(session, mandate=mandate, submit=lambda intent: "ok")
    assert first.stage is CycleStage.COMPLETE

    second = _run(
        session,
        mandate=mandate,
        submit=lambda intent: "ok",
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
    _run(session, mandate=mandate, facts=facts, submit=lambda intent: "ok")
    counters = durable.load_counters(
        session,
        account_fingerprint=_FINGERPRINT,
        now=datetime(2026, 7, 26, 2, 0, tzinfo=UTC),
        market_timezone="America/New_York",
    )
    assert counters.orders_submitted == 1

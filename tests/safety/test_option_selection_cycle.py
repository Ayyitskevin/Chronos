"""The option resolver is a mandatory, persisted gate before sizing or handoff."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    ProposedDecision,
    SessionPolicy,
    StrategyForm,
    TradableAssetClass,
    TradingSession,
)
from chronos.config.limits import (
    MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
)
from chronos.domain.enums import DataQuality
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import HashChainRow
from chronos.supervisor import alerts, durable
from chronos.supervisor import loop as supervisor_loop
from chronos.supervisor.loop import CycleStage, InstrumentFacts, run_cycle
from chronos.supervisor.option_selection import (
    OPTION_SELECTION_STREAM,
    SelectionRejectionCode,
    SelectionStatus,
    build_candidate_acquisition_plan,
    canonical_digest,
    failure_evidence,
    select_option,
)
from chronos.supervisor.sizing import AccountEvidence
from tests.safety.test_autonomy_cycle import _facts, _identity, _mandate
from tests.unit.test_option_selection import _select, _semantically_equivalent, _valid_inputs

_NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions() as db_session:
            yield db_session
    finally:
        database.dispose()


@pytest.fixture
def file_database(tmp_path: Path) -> Iterator[Database]:
    """A real SQLite file so another connection cannot see uncommitted rows."""

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'option-selection.db'}")
    database.initialize()
    try:
        yield database
    finally:
        database.dispose()


def _option_mandate(
    *, order_forms: tuple[OrderForm, ...] = (OrderForm.MARKETABLE_LIMIT,)
) -> AutonomyMandate:
    template = _mandate()
    return _mandate(
        effective_from=_NOW - timedelta(hours=1),
        expires_at=_NOW + timedelta(days=1),
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY_OPTION,
                level=PromotionLevel.PAPER_AUTONOMOUS,
            ),
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY_OPTION,),
            symbols=("AAPL",),
            exchanges=("SMART",),
            contract_families=("AAPL",),
            strategies=(StrategyForm.CASH_SECURED_PUT,),
            order_forms=order_forms,
        ),
        capital=template.capital.model_copy(
            update={
                "allocated_capital_usd": Decimal("100000"),
                "max_order_notional_usd": Decimal("50000"),
                "max_contracts_per_order": 5,
            }
        ),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal("30"),
            permitted_data_qualities=(DataQuality.LIVE,),
            min_option_volume=100,
            min_open_interest=500,
            max_relative_spread=Decimal("0.10"),
        ),
        sessions=SessionPolicy(permitted_sessions=(TradingSession.REGULAR,)),
    )


def _proposal() -> ProposedDecision:
    return ProposedDecision(
        kind="OPEN",
        asset_class=TradableAssetClass.EQUITY_OPTION,
        symbol="AAPL",
        requested_strategy=StrategyForm.CASH_SECURED_PUT,
        requested_quantity=Decimal(1),
        evidence=(
            EvidenceCitation(
                evidence_id="ev-option-1",
                kind="option-chain",
                as_of=_NOW,
                digest="c" * 64,
            ),
        ),
        invalidation_conditions=("underlying thesis invalidates",),
    )


def _activate(session: Session, mandate: AutonomyMandate) -> None:
    durable.activate(
        session,
        account_fingerprint=_FINGERPRINT,
        mandate=mandate,
        owner_event_id="owner-option-cycle",
        now=_NOW,
        process_generation=7,
    )


def _receipt_for(
    decision: Any,
    mandate: AutonomyMandate,
    *,
    no_trade: bool = False,
    policy_updates: dict[str, object] | None = None,
    mandate_digest: str | None = None,
    promotion_digest: str | None = None,
):
    inputs = _valid_inputs()
    request = inputs.request.model_copy(
        update={
            "decision_id": decision.decision_id,
            "requested_contracts": decision.requested_quantity,
        }
    )
    policy = inputs.policy.model_copy(update=policy_updates or {})
    evidence = inputs.evidence
    if policy_updates:
        chain = evidence.chain_parameters[0]
        evidence = evidence.model_copy(
            update={
                "acquisition_plan": build_candidate_acquisition_plan(
                    request=request,
                    policy=policy,
                    chain=chain,
                    spot=Decimal("200"),
                    observed_at=evidence.observed_at,
                )
            }
        )
    if no_trade:
        evidence = failure_evidence(
            observed_at=_NOW,
            source="synthetic-cycle",
            code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
            detail="synthetic evidence outage",
        )
    return select_option(
        request=request,
        policy=policy,
        evidence=evidence,
        mandate_digest=mandate_digest or canonical_digest(mandate),
        resolver_compatibility_digest="2" * 64,
        promotion_digest=promotion_digest,
    )


def _gatherer(mandate: AutonomyMandate, *, no_trade: bool = False):
    def _gather(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(decision, mandate, no_trade=no_trade)
        selected = receipt.selected
        if selected is None:
            return InstrumentFacts(None, None, Decimal(0), selection_receipt=receipt)
        return InstrumentFacts(
            selected.contract,
            None,
            selected.sizing_reference_price,
            selected.deliverable_shares,
            receipt,
        )

    return _gather


def _run(
    session: Session,
    mandate: AutonomyMandate,
    *,
    gather_instrument: Any,
    submit: Any = None,
    facts: Any = None,
):
    return run_cycle(
        _proposal(),
        session=session,
        mandate=mandate,
        identity=_identity(),
        facts=facts or _facts(now=_NOW),
        gather_instrument=gather_instrument,
        submit=submit,
    )


def _selection_payloads(session: Session) -> list[dict[str, Any]]:
    stream = _selection_stream()
    rows = hash_chain.verify(session, stream)
    assert rows.ok
    stored = _selection_rows(session)
    return [hash_chain.payload_of(row) for row in stored]


def _selection_stream() -> str:
    return durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)


def _selection_rows(session: Session) -> list[HashChainRow]:
    return list(
        session.scalars(
            select(HashChainRow)
            .where(HashChainRow.stream == _selection_stream())
            .order_by(HashChainRow.sequence)
        )
    )


def _assert_selection_system_alert(
    session: Session,
    outcome: Any,
    failure: str,
) -> None:
    matching = [
        alert
        for alert in alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
        if alert.kind == "option_selection.system_failure"
    ]
    assert len(matching) == 1
    assert matching[0].severity == alerts.AlertSeverity.CRITICAL.value
    assert matching[0].detail == {"failure": failure}
    assert "option_selection.system_failure" in outcome.alerts_raised


def _named_receipt(
    mandate: AutonomyMandate,
    decision_id: str,
    *,
    no_trade: bool = False,
):
    decision = SimpleNamespace(decision_id=decision_id, requested_quantity=Decimal(1))
    return _receipt_for(decision, mandate, no_trade=no_trade)


def _persist_named_receipt(
    database: Database,
    mandate: AutonomyMandate,
    decision_id: str,
) -> None:
    with database.sessions() as receipt_session:
        supervisor_loop._persist_selection_receipt(
            receipt_session,
            account_fingerprint=_FINGERPRINT,
            receipt=_named_receipt(mandate, decision_id),
        )


def _activate_database(database: Database, mandate: AutonomyMandate) -> None:
    with database.sessions.begin() as setup:
        _activate(setup, mandate)


def test_selected_receipt_is_persisted_before_the_existing_handoff(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)
    received = []

    outcome = _run(
        session,
        mandate,
        gather_instrument=_gatherer(mandate),
        submit=lambda intent: received.append(intent) or "existing-order-plane",
    )

    assert outcome.stage is CycleStage.COMPLETE
    assert outcome.selection_receipt is not None
    assert outcome.selection_receipt.status is SelectionStatus.SELECTED
    assert len(received) == 1
    intent = received[0]
    assert intent.contract.con_id == 2001
    assert intent.quantity == Decimal(1)
    assert intent.limit_price == Decimal("2.03")
    assert intent.to_order_request().transmit is False
    payloads = _selection_payloads(session)
    assert len(payloads) == 1
    assert payloads[0]["output_digest"] == outcome.selection_receipt.output_digest
    assert (
        hashlib.sha256(payloads[0]["canonical_receipt"].encode("utf-8")).hexdigest()
        == hashlib.sha256(outcome.selection_receipt.canonical_bytes()).hexdigest()
    )


def test_selected_receipt_is_durable_and_valid_in_an_independent_session_before_handoff(
    file_database: Database,
) -> None:
    mandate = _option_mandate()
    _activate_database(file_database, mandate)

    gathered_receipts = []
    observed: dict[str, Any] = {}
    gather = _gatherer(mandate)

    def _gather(decision: Any) -> InstrumentFacts:
        facts = gather(decision)
        gathered_receipts.append(facts.selection_receipt)
        return facts

    def _handoff(intent: Any) -> str:
        del intent
        with file_database.sessions() as independent:
            verification = hash_chain.verify(independent, _selection_stream())
            rows = _selection_rows(independent)
            observed.update(
                chain_ok=verification.ok,
                count=len(rows),
                kind=rows[0].kind if rows else None,
                recorded_at=rows[0].recorded_at if rows else None,
                payload=hash_chain.payload_of(rows[0]) if rows else None,
            )
        return "existing-order-plane"

    with file_database.sessions() as cycle_session:
        outcome = _run(
            cycle_session,
            mandate,
            gather_instrument=_gather,
            submit=_handoff,
        )

    receipt = gathered_receipts[0]
    assert receipt is not None
    assert outcome.stage is CycleStage.COMPLETE
    assert observed == {
        "chain_ok": True,
        "count": 1,
        "kind": receipt.status.value,
        "recorded_at": receipt.evaluated_at,
        "payload": {
            "decision_id": receipt.request.decision_id,
            "status": receipt.status.value,
            "output_digest": receipt.output_digest,
            "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
        },
    }


def test_no_trade_receipt_is_persisted_and_never_reaches_handoff(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)
    received = []

    outcome = _run(
        session,
        mandate,
        gather_instrument=_gatherer(mandate, no_trade=True),
        submit=received.append,
    )

    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_NO_TRADE"
    assert received == []
    assert _selection_payloads(session)[0]["status"] == SelectionStatus.NO_TRADE.value
    raised = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    selection_alert = next(
        alert for alert in raised if alert.kind == "option_selection.system_failure"
    )
    assert selection_alert.detail == {
        "rejection_codes": [
            SelectionRejectionCode.CHAIN_MISSING.value,
            SelectionRejectionCode.EVIDENCE_PARTIAL.value,
            SelectionRejectionCode.EVIDENCE_UNAVAILABLE.value,
            SelectionRejectionCode.UNDERLYING_MISSING.value,
            SelectionRejectionCode.UNDERLYING_QUOTE_MISSING.value,
        ]
    }


def test_candidate_economics_no_trade_does_not_raise_a_system_alert(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)

    def _economics_refusal(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(
            decision,
            mandate,
            policy_updates={"min_option_volume": 1_000_000},
        )
        assert receipt.status is SelectionStatus.NO_TRADE
        return InstrumentFacts(None, None, Decimal(0), selection_receipt=receipt)

    outcome = _run(session, mandate, gather_instrument=_economics_refusal)

    assert outcome.refusal == "OPTION_SELECTION_NO_TRADE"
    assert all(
        alert.kind != "option_selection.system_failure"
        for alert in alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    )


@pytest.mark.parametrize(
    "code",
    (
        SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
        SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH,
        SelectionRejectionCode.MARKET_RULE_MISSING,
        SelectionRejectionCode.DELTA_MISSING,
        SelectionRejectionCode.VOLUME_UNKNOWN,
        SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,
        SelectionRejectionCode.QUOTE_DATA_QUALITY,
    ),
)
def test_missing_conflicting_or_unknown_selection_evidence_raises_system_alert(
    session: Session,
    code: SelectionRejectionCode,
) -> None:
    receipt = SimpleNamespace(
        evaluated_at=_NOW,
        no_trade_reasons=(SimpleNamespace(code=code),),
    )

    raised = supervisor_loop._alert_for_selection_failure(
        session,
        account_fingerprint=_FINGERPRINT,
        receipt=receipt,
    )

    assert raised is not None
    assert raised.kind == "option_selection.system_failure"
    assert raised.detail == {"rejection_codes": [code.value]}


@pytest.mark.parametrize(
    "code",
    (
        SelectionRejectionCode.EXPIRATION_OUTSIDE_RANGE,
        SelectionRejectionCode.MONEYNESS_MISMATCH,
        SelectionRejectionCode.DELTA_OUTSIDE_RANGE,
        SelectionRejectionCode.SPREAD_TOO_WIDE,
        SelectionRejectionCode.VOLUME_TOO_LOW,
        SelectionRejectionCode.OPEN_INTEREST_TOO_LOW,
    ),
)
def test_numeric_candidate_filter_misses_do_not_raise_system_alert(
    session: Session,
    code: SelectionRejectionCode,
) -> None:
    receipt = SimpleNamespace(
        evaluated_at=_NOW,
        no_trade_reasons=(SimpleNamespace(code=code),),
    )

    raised = supervisor_loop._alert_for_selection_failure(
        session,
        account_fingerprint=_FINGERPRINT,
        receipt=receipt,
    )

    assert raised is None
    assert alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT) == ()


def test_legacy_option_contract_without_a_receipt_cannot_bypass_selection(
    session: Session,
) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)
    selected = _valid_inputs().evidence.candidates[0].contract
    received = []

    outcome = _run(
        session,
        mandate,
        gather_instrument=lambda decision: InstrumentFacts(
            selected,
            None,
            Decimal("190"),
            Decimal("100"),
        ),
        submit=received.append,
    )

    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_MISSING"
    assert received == []
    assert _selection_payloads(session) == []
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_RECEIPT_MISSING")


def test_receipt_must_bind_the_exact_canonical_mandate(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)

    def _wrong(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(decision, mandate, mandate_digest="f" * 64)
        assert receipt.selected is not None
        return InstrumentFacts(
            receipt.selected.contract,
            None,
            receipt.selected.sizing_reference_price,
            receipt.selected.deliverable_shares,
            receipt,
        )

    outcome = _run(session, mandate, gather_instrument=_wrong)
    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_INVALID"
    assert "owner mandate" in outcome.detail
    assert _selection_payloads(session) == []
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_RECEIPT_INVALID")


def test_receipt_policy_cannot_weaken_the_owner_grant(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)

    def _weak(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(
            decision,
            mandate,
            policy_updates={"max_quote_age_seconds": Decimal("31")},
        )
        return InstrumentFacts(None, None, Decimal(0), selection_receipt=receipt)

    outcome = _run(session, mandate, gather_instrument=_weak)
    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_INVALID"
    assert "older" in outcome.detail
    assert _selection_payloads(session) == []
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_RECEIPT_INVALID")


def test_compiler_must_reproduce_the_receipt_bound_price(session: Session) -> None:
    mandate = _option_mandate(order_forms=(OrderForm.MARKETABLE_LIMIT, OrderForm.LIMIT))
    _activate(session, mandate)
    received = []

    def _limit_receipt(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(
            decision,
            mandate,
            policy_updates={"order_form": OrderForm.LIMIT},
        )
        assert receipt.selected is not None
        assert receipt.selected.limit_price == Decimal("2.07")
        return InstrumentFacts(
            receipt.selected.contract,
            None,
            receipt.selected.sizing_reference_price,
            receipt.selected.deliverable_shares,
            receipt,
        )

    outcome = _run(
        session,
        mandate,
        gather_instrument=_limit_receipt,
        submit=received.append,
    )
    assert outcome.stage is CycleStage.COMPILATION
    assert outcome.refusal == "OPTION_SELECTION_COMPILATION_MISMATCH"
    assert received == []
    assert len(_selection_payloads(session)) == 1
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_COMPILATION_MISMATCH")


def test_receipt_is_persisted_even_when_later_sizing_refuses(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)
    facts = _facts(
        now=_NOW,
        account=AccountEvidence(
            net_liquidation_usd=Decimal("100000"),
            total_cash_usd=mandate.capital.min_cash_floor_usd,
            buying_power_usd=mandate.capital.min_buying_power_usd,
            symbol_exposure_usd=Decimal(0),
            gross_exposure_usd=Decimal(0),
            net_exposure_usd=Decimal(0),
            position_notional_usd=Decimal(0),
            maintenance_margin_usd=Decimal(0),
            deployed_capital_usd=Decimal(0),
        ),
    )

    outcome = _run(
        session,
        mandate,
        gather_instrument=_gatherer(mandate),
        facts=facts,
    )
    assert outcome.stage is CycleStage.SIZING
    assert outcome.refusal == "NO_EXECUTABLE_SIZE"
    assert len(_selection_payloads(session)) == 1


def test_receipt_persistence_failure_prevents_handoff(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)
    received = []

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr("chronos.supervisor.loop._persist_selection_receipt", _fail)
    outcome = _run(
        session,
        mandate,
        gather_instrument=_gatherer(mandate),
        submit=received.append,
    )
    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED"
    assert received == []
    _assert_selection_system_alert(
        session,
        outcome,
        "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
    )


def test_live_promotion_is_revalidated_immediately_before_handoff(session: Session) -> None:
    mandate = _option_mandate().model_copy(
        update={
            "mode": AutonomyMode.CANARY_LIVE_AUTONOMOUS,
            "promotions": (
                FamilyPromotion(
                    asset_class=TradableAssetClass.EQUITY_OPTION,
                    level=PromotionLevel.CANARY_LIVE_AUTONOMOUS,
                ),
            ),
        }
    )
    _activate(session, mandate)
    received: list[Any] = []

    def _expired(decision: Any) -> InstrumentFacts:
        receipt = _receipt_for(decision, mandate, promotion_digest="4" * 64)
        assert receipt.selected is not None
        return InstrumentFacts(
            contract=receipt.selected.contract,
            quote=None,
            reference_price=receipt.selected.sizing_reference_price,
            multiplier=receipt.selected.deliverable_shares,
            selection_receipt=receipt,
            selection_activation_validator=lambda _: "effective_window expired",
        )

    outcome = _run(
        session,
        mandate,
        gather_instrument=_expired,
        submit=received.append,
    )

    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_PROMOTION_INVALID"
    assert "effective_window" in outcome.detail
    assert received == []
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_PROMOTION_INVALID")


def test_corrupt_receipt_chain_prefix_prevents_handoff(file_database: Database) -> None:
    mandate = _option_mandate()
    _persist_named_receipt(file_database, mandate, "historical-decision")
    with file_database.sessions.begin() as tamper:
        row = _selection_rows(tamper)[0]
        row.payload_json = "{}"
    _activate_database(file_database, mandate)
    received = []

    with file_database.sessions() as cycle_session:
        outcome = _run(
            cycle_session,
            mandate,
            gather_instrument=_gatherer(mandate),
            submit=received.append,
        )
        _assert_selection_system_alert(
            cycle_session,
            outcome,
            "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
        )
        cycle_session.commit()

    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED"
    assert received == []
    with file_database.sessions() as independent:
        verification = hash_chain.verify(independent, _selection_stream())
        assert verification.ok is False
        assert verification.broken_at == 1
        assert len(_selection_rows(independent)) == 1
        _assert_selection_system_alert(
            independent,
            outcome,
            "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
        )


def test_pre_handoff_verifier_projects_a_bounded_corrupt_payload(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = "x" * (MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS + 1)
    session.add(
        HashChainRow(
            stream=_selection_stream(),
            sequence=1,
            kind=SelectionStatus.NO_TRADE.value,
            payload_json=oversized,
            recorded_at=_NOW,
            previous_hash=hash_chain.GENESIS_HASH,
            record_hash="0" * 64,
        )
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

    def _unexpected_hash(**_kwargs: Any) -> str:
        raise AssertionError("an oversized projected payload must not be hashed")

    event.listen(session.bind, "before_cursor_execute", _capture_statement)
    monkeypatch.setattr(hash_chain, "compute_hash", _unexpected_hash)
    try:
        with pytest.raises(hash_chain.HashChainError, match="invalid semantic envelope"):
            supervisor_loop._verified_selection_rows(
                session,
                _selection_stream(),
                decision_id="oversized",
            )
    finally:
        event.remove(session.bind, "before_cursor_execute", _capture_statement)

    assert len(executed_selects) == 1
    statement = executed_selects[0]
    assert "length(cast(hash_chain_records.payload_json as text))" in statement
    assert "substr(cast(hash_chain_records.payload_json as text)" in statement


def test_pre_handoff_verifier_bounds_receipt_before_model_parsing(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_receipt = "x" * (MAX_OPTION_SELECTION_RECEIPT_BYTES + 1)
    payload_json = hash_chain.canonical_payload(
        {
            "decision_id": "oversized-receipt",
            "status": SelectionStatus.NO_TRADE.value,
            "output_digest": "0" * 64,
            "canonical_receipt": canonical_receipt,
        }
    )
    session.add(
        HashChainRow(
            stream=_selection_stream(),
            sequence=1,
            kind=SelectionStatus.NO_TRADE.value,
            payload_json=payload_json,
            recorded_at=_NOW,
            previous_hash=hash_chain.GENESIS_HASH,
            record_hash=hash_chain.compute_hash(
                stream=_selection_stream(),
                sequence=1,
                recorded_at=_NOW,
                payload_json=payload_json,
                previous_hash=hash_chain.GENESIS_HASH,
            ),
        )
    )
    session.flush()
    parse_calls = 0

    def _unexpected_parse(*_args: object, **_kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        raise AssertionError("oversized canonical receipt must not reach Pydantic")

    monkeypatch.setattr(
        supervisor_loop.OptionSelectionReceipt,
        "model_validate_json",
        _unexpected_parse,
    )
    with pytest.raises(hash_chain.HashChainError, match="invalid semantic envelope"):
        supervisor_loop._verified_selection_rows(
            session,
            _selection_stream(),
            decision_id="oversized-receipt",
        )

    assert parse_calls == 0


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("sequence", "9" * 100_000),
        ("recorded_at", "x" * 100_000),
    ],
)
def test_pre_handoff_verifier_bounds_dynamic_sqlite_identity_fields(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    corrupt_value: str,
) -> None:
    mandate = _option_mandate()
    receipt = _named_receipt(mandate, "historical-decision")
    hash_chain.append(
        session,
        stream=_selection_stream(),
        kind=receipt.status.value,
        payload=supervisor_loop._selection_receipt_payload(receipt),
        recorded_at=receipt.evaluated_at,
    )
    session.flush()
    assert column in {"sequence", "recorded_at"}
    session.connection().exec_driver_sql(
        f"UPDATE hash_chain_records SET {column} = ? WHERE stream = ?",
        (corrupt_value, _selection_stream()),
    )

    def _unexpected_hash(**_kwargs: Any) -> str:
        raise AssertionError("a malformed bounded identity field must not be hashed")

    monkeypatch.setattr(hash_chain, "compute_hash", _unexpected_hash)
    with pytest.raises(hash_chain.HashChainError) as raised:
        supervisor_loop._verified_selection_rows(
            session,
            _selection_stream(),
            decision_id=receipt.request.decision_id,
        )

    assert str(raised.value) == (
        "option-selection receipt stream contains an invalid semantic envelope"
    )
    assert corrupt_value not in str(raised.value)


@pytest.mark.parametrize(
    "column",
    ("sequence", "kind", "payload_json", "recorded_at", "previous_hash", "record_hash"),
)
def test_pre_handoff_verifier_refuses_invalid_utf8_sqlite_storage(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
) -> None:
    mandate = _option_mandate()
    receipt = _named_receipt(mandate, "invalid-storage")
    hash_chain.append(
        session,
        stream=_selection_stream(),
        kind=receipt.status.value,
        payload=supervisor_loop._selection_receipt_payload(receipt),
        recorded_at=receipt.evaluated_at,
    )
    session.flush()
    corrupt_value = b"\x80" * 100_000
    assert column in {
        "sequence",
        "kind",
        "payload_json",
        "recorded_at",
        "previous_hash",
        "record_hash",
    }
    session.connection().exec_driver_sql(
        f"UPDATE hash_chain_records SET {column} = ? WHERE stream = ?",
        (corrupt_value, _selection_stream()),
    )

    def _unexpected_hash(**_kwargs: Any) -> str:
        raise AssertionError("an invalid SQLite storage class must not be hashed")

    monkeypatch.setattr(hash_chain, "compute_hash", _unexpected_hash)
    with pytest.raises(hash_chain.HashChainError) as raised:
        supervisor_loop._verified_selection_rows(
            session,
            _selection_stream(),
            decision_id=receipt.request.decision_id,
        )

    assert str(raised.value) == (
        "option-selection receipt stream contains an invalid semantic envelope"
    )


def test_pre_handoff_verifier_rejects_rehashed_noncanonical_envelope(
    session: Session,
) -> None:
    mandate = _option_mandate()
    receipt = _named_receipt(mandate, "noncanonical-envelope")
    payload = supervisor_loop._selection_receipt_payload(receipt)
    payload_json = json.dumps(payload, indent=2)
    session.add(
        HashChainRow(
            stream=_selection_stream(),
            sequence=1,
            kind=receipt.status.value,
            payload_json=payload_json,
            recorded_at=receipt.evaluated_at,
            previous_hash=hash_chain.GENESIS_HASH,
            record_hash=hash_chain.compute_hash(
                stream=_selection_stream(),
                sequence=1,
                recorded_at=receipt.evaluated_at,
                payload_json=payload_json,
                previous_hash=hash_chain.GENESIS_HASH,
            ),
        )
    )
    session.flush()

    assert hash_chain.verify(session, _selection_stream()).ok is True
    with pytest.raises(hash_chain.HashChainError, match="invalid semantic envelope"):
        supervisor_loop._verified_selection_rows(
            session,
            _selection_stream(),
            decision_id=receipt.request.decision_id,
        )


def test_semantic_kind_tamper_prevents_handoff_even_when_hash_chain_is_valid(
    file_database: Database,
) -> None:
    mandate = _option_mandate()
    _persist_named_receipt(file_database, mandate, "historical-decision")
    with file_database.sessions.begin() as tamper:
        row = _selection_rows(tamper)[0]
        row.kind = SelectionStatus.NO_TRADE.value
    _activate_database(file_database, mandate)
    received = []

    with file_database.sessions() as preflight:
        assert hash_chain.verify(preflight, _selection_stream()).ok is True
    with file_database.sessions() as cycle_session:
        outcome = _run(
            cycle_session,
            mandate,
            gather_instrument=_gatherer(mandate),
            submit=received.append,
        )
        _assert_selection_system_alert(
            cycle_session,
            outcome,
            "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
        )
        cycle_session.commit()

    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED"
    assert received == []
    with file_database.sessions() as independent:
        assert hash_chain.verify(independent, _selection_stream()).ok is True
        assert len(_selection_rows(independent)) == 1


def test_receipt_persistence_is_idempotent_but_conflicting_reuse_fails_closed(
    file_database: Database,
) -> None:
    mandate = _option_mandate()
    receipt = _named_receipt(mandate, "same-decision")

    for _ in range(2):
        with file_database.sessions() as receipt_session:
            supervisor_loop._persist_selection_receipt(
                receipt_session,
                account_fingerprint=_FINGERPRINT,
                receipt=receipt,
            )

    with file_database.sessions() as independent:
        assert hash_chain.verify(independent, _selection_stream()).ok is True
        rows = _selection_rows(independent)
        assert len(rows) == 1
        assert hash_chain.payload_of(rows[0])["output_digest"] == receipt.output_digest

    conflicting = _named_receipt(mandate, "same-decision", no_trade=True)
    assert conflicting.output_digest != receipt.output_digest
    with file_database.sessions() as conflict_session:
        with pytest.raises(hash_chain.HashChainError, match="conflicting"):
            supervisor_loop._persist_selection_receipt(
                conflict_session,
                account_fingerprint=_FINGERPRINT,
                receipt=conflicting,
            )
        conflict_session.rollback()

    with file_database.sessions() as independent:
        assert hash_chain.verify(independent, _selection_stream()).ok is True
        assert len(_selection_rows(independent)) == 1


def test_receipt_persistence_normalizes_equivalent_offset_timestamp(
    file_database: Database,
) -> None:
    receipt = _select(_semantically_equivalent(_valid_inputs()))
    assert receipt.evaluated_at.utcoffset() == timedelta(hours=-4)
    assert receipt.verifies() is True

    with file_database.sessions() as receipt_session:
        supervisor_loop._persist_selection_receipt(
            receipt_session,
            account_fingerprint=_FINGERPRINT,
            receipt=receipt,
        )

    with file_database.sessions() as independent:
        matches = supervisor_loop._verified_selection_rows(
            independent,
            _selection_stream(),
            decision_id=receipt.request.decision_id,
        )
        assert len(matches) == 1
        assert matches[0][0].recorded_at == receipt.evaluated_at.astimezone(UTC)


def test_tamper_after_commit_is_rechecked_immediately_before_handoff(
    file_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate = _option_mandate()
    _activate_database(file_database, mandate)
    received = []
    tampered = []
    real_compile = supervisor_loop.compile_order

    def _compile_and_tamper(*args: Any, **kwargs: Any):
        result = real_compile(*args, **kwargs)
        with file_database.sessions.begin() as tamper:
            row = _selection_rows(tamper)[0]
            row.kind = SelectionStatus.NO_TRADE.value
            tampered.append(row.sequence)
        return result

    monkeypatch.setattr(supervisor_loop, "compile_order", _compile_and_tamper)
    with file_database.sessions() as cycle_session:
        outcome = _run(
            cycle_session,
            mandate,
            gather_instrument=_gatherer(mandate),
            submit=received.append,
        )
        _assert_selection_system_alert(
            cycle_session,
            outcome,
            "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
        )
        cycle_session.commit()

    assert tampered == [1]
    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED"
    assert "pre-handoff" in outcome.detail
    assert received == []
    with file_database.sessions() as independent:
        # ``kind`` is not part of the generic record hash, so the semantic replay
        # check — not merely the byte-chain check — is what blocked the handoff.
        assert hash_chain.verify(independent, _selection_stream()).ok is True
        _assert_selection_system_alert(
            independent,
            outcome,
            "OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
        )


def test_crash_after_durability_barrier_cannot_roll_back_the_receipt(
    file_database: Database,
) -> None:
    class _SyntheticCrash(BaseException):
        pass

    mandate = _option_mandate()
    _activate_database(file_database, mandate)
    gathered_receipts = []
    handoff_calls = []
    gather = _gatherer(mandate)

    def _gather(decision: Any) -> InstrumentFacts:
        facts = gather(decision)
        gathered_receipts.append(facts.selection_receipt)
        return facts

    def _crash(intent: Any) -> None:
        handoff_calls.append(intent)
        raise _SyntheticCrash

    with file_database.sessions() as cycle_session:
        with pytest.raises(_SyntheticCrash):
            _run(
                cycle_session,
                mandate,
                gather_instrument=_gather,
                submit=_crash,
            )
        cycle_session.rollback()

    receipt = gathered_receipts[0]
    assert receipt is not None
    assert len(handoff_calls) == 1
    with file_database.sessions() as independent:
        assert hash_chain.verify(independent, _selection_stream()).ok is True
        rows = _selection_rows(independent)
        assert len(rows) == 1
        assert hash_chain.payload_of(rows[0]) == {
            "decision_id": receipt.request.decision_id,
            "status": receipt.status.value,
            "output_digest": receipt.output_digest,
            "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
        }


def test_instrument_gatherer_exception_is_a_selection_refusal(session: Session) -> None:
    mandate = _option_mandate()
    _activate(session, mandate)

    def _raise(decision: Any) -> InstrumentFacts:
        raise LookupError("synthetic")

    outcome = _run(session, mandate, gather_instrument=_raise)
    assert outcome.stage is CycleStage.SELECTION
    assert outcome.refusal == "OPTION_SELECTION_FAILED"
    assert "LookupError" in outcome.detail
    _assert_selection_system_alert(session, outcome, "OPTION_SELECTION_FAILED")

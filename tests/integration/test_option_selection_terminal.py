"""Read-only operator inspection of canonical option-selection receipts."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from chronos.api.main import create_app
from chronos.autonomy import OrderForm, StrategyForm, TradingSession
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.config.limits import (
    MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
    MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
)
from chronos.config.settings import get_settings
from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import (
    MarketQuote,
    ModelGreeks,
    OptionChainParameters,
    OptionContract,
    OptionDeliverableFacts,
    OptionMarketRule,
    OptionPriceIncrement,
    UnderlyingContract,
)
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import HashChainRow
from chronos.supervisor import durable
from chronos.supervisor.option_selection import (
    OPTION_SELECTION_STREAM,
    CandidateEvidence,
    ObservedDeliverableEvidence,
    ObservedMarketRuleEvidence,
    ObservedOptionQuoteEvidence,
    OptionSelectionEvidence,
    OptionSelectionPolicy,
    OptionSelectionReceipt,
    OptionSelectionRequest,
    SelectionRejectionCode,
    SelectionStatus,
    build_candidate_acquisition_plan,
    failure_evidence,
    select_option,
)
from chronos.terminal import views
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.locking import WriterLease

REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 9, 4)
_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def client(demo_env: Path) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def headers(client: TestClient, demo_env: Path) -> dict[str, str]:
    del client  # Starting the app creates the credential file.
    token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()
    return {"X-Chronos-Token": token}


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


class _ProjectionCaptureSession:
    def __init__(self, dialect_name: str) -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.statement: object | None = None

    def get_bind(self) -> SimpleNamespace:
        return self._bind

    def execute(self, statement: object) -> tuple[()]:
        self.statement = statement
        return ()


def _request(decision_id: str) -> OptionSelectionRequest:
    return OptionSelectionRequest(
        decision_id=decision_id,
        symbol="AAPL",
        strategy=StrategyForm.CASH_SECURED_PUT,
        right=OptionRight.PUT,
        requested_contracts=Decimal("1"),
    )


def _policy() -> OptionSelectionPolicy:
    return OptionSelectionPolicy(
        max_quote_age_seconds=Decimal("30"),
        max_chain_age_seconds=Decimal("300"),
        max_candidate_expirations=4,
        max_strikes_per_expiration=12,
        min_dte=25,
        target_dte=32,
        max_dte=45,
        min_abs_delta=Decimal("0.20"),
        target_abs_delta=Decimal("0.30"),
        max_abs_delta=Decimal("0.40"),
        permitted_data_qualities=(DataQuality.LIVE,),
        min_option_volume=100,
        min_open_interest=500,
        max_relative_spread=Decimal("0.10"),
        permitted_exchanges=("SMART",),
        permitted_trading_classes=("AAPL",),
        permitted_sessions=(TradingSession.REGULAR,),
        required_multiplier=Decimal("100"),
        order_form=OrderForm.MARKETABLE_LIMIT,
        delta_weight=Decimal("4"),
        spread_weight=Decimal("2"),
        dte_weight=Decimal("1"),
        liquidity_weight=Decimal("1"),
    )


def _receipt(decision_id: str, *, selected: bool = False) -> OptionSelectionReceipt:
    request = _request(decision_id)
    policy = _policy()
    evidence = (
        _selected_evidence(request, policy)
        if selected
        else failure_evidence(
            observed_at=_NOW,
            source="terminal-test",
            code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
            detail="the synthetic acquisition returned no evidence",
        )
    )
    return select_option(
        request=request,
        policy=policy,
        evidence=evidence,
        mandate_digest="1" * 64,
        resolver_compatibility_digest="2" * 64,
        promotion_digest="3" * 64,
    )


def _selected_evidence(
    request: OptionSelectionRequest,
    policy: OptionSelectionPolicy,
) -> OptionSelectionEvidence:
    underlying = UnderlyingContract(
        con_id=1001,
        symbol="AAPL",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )
    contract = OptionContract(
        con_id=2001,
        symbol="AAPL",
        underlying_con_id=underlying.con_id,
        expiration=_EXPIRATION,
        strike=Decimal("190"),
        right=OptionRight.PUT,
        exchange="SMART",
        currency="USD",
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol="AAPL  260904P00190000",
        liquid_hours="20260803:0930-20260803:1600",
        time_zone_id="US/Eastern",
    )
    underlying_quote = MarketQuote(
        contract=underlying,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("199.90"),
        ask=Decimal("200.10"),
        last=Decimal("200"),
    )
    quote = MarketQuote(
        contract=contract,
        timestamp=_NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("2.03"),
        ask=Decimal("2.07"),
        last=Decimal("2.05"),
        volume=1000,
        open_interest=2000,
        greeks=ModelGreeks(delta=Decimal("-0.30")),
    )
    chain = OptionChainParameters(
        exchange="SMART",
        underlying_con_id=underlying.con_id,
        trading_class="AAPL",
        multiplier=Decimal("100"),
        expirations=(_EXPIRATION,),
        strikes=(contract.strike,),
    )
    market_rule = OptionMarketRule(
        con_id=contract.con_id,
        exchange="SMART",
        market_rule_id=26,
        price_increments=(
            OptionPriceIncrement(low_edge=Decimal("0"), increment=Decimal("0.01")),
            OptionPriceIncrement(low_edge=Decimal("3"), increment=Decimal("0.05")),
        ),
        source="terminal-test",
    )
    deliverable = OptionDeliverableFacts(
        con_id=contract.con_id,
        authoritative=True,
        source="terminal-test",
        underlying_con_id=underlying.con_id,
        share_quantity=Decimal("100"),
        cash_amount=Decimal("0"),
        other_assets=(),
    )
    return OptionSelectionEvidence(
        source="terminal-test",
        observed_at=_NOW,
        complete=True,
        completion_marker="END",
        underlying=underlying,
        underlying_qualified_at=_NOW,
        underlying_quote=underlying_quote,
        underlying_quote_fetched_at=_NOW,
        underlying_quote_observed_at=_NOW,
        chain_parameters=(chain,),
        chain_fetched_at=_NOW,
        chain_observed_at=_NOW,
        chain_completion_observed_at=_NOW,
        chain_completion_source="terminal-test-end-callback",
        acquisition_plan=build_candidate_acquisition_plan(
            request=request,
            policy=policy,
            chain=chain,
            spot=Decimal("200"),
            observed_at=_NOW,
        ),
        observed_market_rules=(
            ObservedMarketRuleEvidence(market_rule=market_rule, observed_at=_NOW),
        ),
        observed_deliverables=(
            ObservedDeliverableEvidence(deliverable=deliverable, observed_at=_NOW),
        ),
        observed_option_quotes=(
            ObservedOptionQuoteEvidence(
                quote=quote,
                fetched_at=_NOW,
                observed_at=_NOW,
            ),
        ),
        candidates=(
            CandidateEvidence(
                contract=contract,
                qualified_at=_NOW,
                quote=quote,
                quote_fetched_at=_NOW,
                quote_observed_at=_NOW,
                market_rule=market_rule,
                market_rule_observed_at=_NOW,
                deliverable=deliverable,
                deliverable_observed_at=_NOW,
            ),
        ),
    )


def _append_receipt(client: TestClient, receipt: OptionSelectionReceipt) -> None:
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind=receipt.status.value,
            payload={
                "decision_id": receipt.request.decision_id,
                "status": receipt.status.value,
                "output_digest": receipt.output_digest,
                "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
            },
            recorded_at=receipt.evaluated_at,
        )


def test_empty_stream_is_explicit_and_authenticated(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.get("/terminal/option-selections").status_code == 401

    response = client.get("/terminal/option-selections", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "entries": [],
        "stream_present": False,
        "chain_valid": True,
        "chain_records": 0,
        "chain_broken_at": None,
        "chain_detail": "chain is empty",
        "semantic_valid": True,
        "semantic_detail": "",
        "limit": views.DEFAULT_OPTION_SELECTION_LIMIT,
        "account_fingerprint": _FINGERPRINT,
        "generated_at": response.json()["generated_at"],
    }


@pytest.mark.parametrize(
    ("dialect_name", "dialect"),
    [
        ("sqlite", sqlite.dialect()),
        ("postgresql", postgresql.dialect()),  # type: ignore[no-untyped-call]
    ],
)
def test_bounded_projection_compiles_for_supported_database_dialects(
    dialect_name: str,
    dialect: object,
) -> None:
    capture = _ProjectionCaptureSession(dialect_name)

    result = views.option_selections_view(
        cast(Session, capture),
        account_fingerprint=_FINGERPRINT,
        now=_NOW,
    )

    assert result.stream_present is False
    statement = capture.statement
    assert isinstance(statement, Select)
    compiled = str(statement.compile(dialect=dialect))  # type: ignore[arg-type]
    normalized = compiled.lower()
    assert "length(" in normalized
    assert "substr(" in normalized
    assert "cast(" in normalized
    assert "order by hash_chain_records.sequence asc" in normalized
    assert statement.get_execution_options()["yield_per"] == 1
    assert ("typeof(" in normalized) is (dialect_name == "sqlite")


def test_valid_selected_and_no_trade_receipts_return_exact_and_parsed_payloads(
    client: TestClient, headers: dict[str, str]
) -> None:
    selected = _receipt("selected", selected=True)
    no_trade = _receipt("no-trade")
    _append_receipt(client, selected)
    _append_receipt(client, no_trade)

    body = client.get("/terminal/option-selections", headers=headers).json()

    assert body["chain_valid"] is True
    assert body["semantic_valid"] is True
    assert body["semantic_detail"] == ""
    assert body["chain_records"] == 2
    assert [entry["status"] for entry in body["entries"]] == ["NO_TRADE", "SELECTED"]
    expected = {receipt.request.decision_id: receipt for receipt in (selected, no_trade)}
    for entry in body["entries"]:
        receipt = expected[entry["decision_id"]]
        assert entry["canonical_receipt"] == receipt.canonical_bytes().decode("utf-8")
        assert entry["output_digest"] == receipt.output_digest
        assert entry["receipt"]["output_digest"] == receipt.output_digest
        assert entry["receipt_valid"] is True
        assert entry["record_hash_valid"] is True
        assert entry["record_valid"] is True
        assert entry["chain_valid"] is True
        assert entry["invalid_detail"] == ""


def test_duplicate_decision_ids_fail_whole_stream_semantic_validation(
    client: TestClient, headers: dict[str, str]
) -> None:
    receipt = _receipt("duplicate-decision")
    _append_receipt(client, receipt)
    _append_receipt(client, receipt)

    body = client.get("/terminal/option-selections?limit=1", headers=headers).json()

    assert body["chain_valid"] is True
    assert body["chain_records"] == 2
    assert body["semantic_valid"] is False
    assert "duplicate option decision receipt" in body["semantic_detail"]
    assert len(body["entries"]) == 1


def test_corrupt_canonical_payload_is_shown_but_not_parsed_as_valid(
    client: TestClient, headers: dict[str, str]
) -> None:
    receipt = _receipt("corrupt-envelope")
    canonical = receipt.canonical_bytes().decode("utf-8")
    corrupted = canonical.replace('"decision_id":"corrupt-envelope"', '"decision_id":"changed"')
    assert corrupted != canonical
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind=receipt.status.value,
            payload={
                "decision_id": receipt.request.decision_id,
                "status": receipt.status.value,
                "output_digest": receipt.output_digest,
                "canonical_receipt": corrupted,
            },
            recorded_at=receipt.evaluated_at,
        )

    body = client.get("/terminal/option-selections", headers=headers).json()
    entry = body["entries"][0]
    assert body["chain_valid"] is True
    assert body["semantic_valid"] is False
    assert entry["canonical_receipt"] == corrupted
    assert entry["receipt"] is None
    assert entry["receipt_valid"] is False
    assert entry["record_hash_valid"] is True
    assert entry["record_valid"] is False
    assert "replay verification failed" in entry["invalid_detail"]
    assert "decision_id does not match" in entry["invalid_detail"]


def test_rehashed_noncanonical_outer_envelope_is_semantically_invalid(
    client: TestClient, headers: dict[str, str]
) -> None:
    _append_receipt(client, _receipt("noncanonical-envelope"))
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        row = session.scalar(
            select(HashChainRow).where(
                HashChainRow.stream == durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)
            )
        )
        assert row is not None
        row.payload_json = f" {row.payload_json}"
        row.record_hash = hash_chain.compute_hash(
            stream=row.stream,
            sequence=row.sequence,
            recorded_at=row.recorded_at,
            payload_json=row.payload_json,
            previous_hash=row.previous_hash,
        )

    body = client.get("/terminal/option-selections", headers=headers).json()
    entry = body["entries"][0]

    assert body["chain_valid"] is True
    assert body["semantic_valid"] is False
    assert entry["record_hash_valid"] is True
    assert entry["receipt_valid"] is False
    assert entry["record_valid"] is False
    assert "record payload is not its canonical JSON representation" in entry["invalid_detail"]


def test_deeply_nested_valid_hash_payload_fails_closed_without_recursion_error(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    payload_json = "[" * 10_000 + "0" + "]" * 10_000
    stream = durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        session.add(
            HashChainRow(
                stream=stream,
                sequence=1,
                kind=SelectionStatus.NO_TRADE.value,
                payload_json=payload_json,
                recorded_at=_NOW,
                previous_hash=hash_chain.GENESIS_HASH,
                record_hash=hash_chain.compute_hash(
                    stream=stream,
                    sequence=1,
                    recorded_at=_NOW,
                    payload_json=payload_json,
                    previous_hash=hash_chain.GENESIS_HASH,
                ),
            )
        )

    response = client.get("/terminal/option-selections", headers=headers)
    body = response.json()
    entry = body["entries"][0]

    assert response.status_code == 200
    assert body["chain_valid"] is True
    assert body["semantic_valid"] is False
    assert entry["record_hash_valid"] is True
    assert entry["receipt_valid"] is False
    assert entry["record_valid"] is False
    # Which refusal fires depends on the interpreter's recursion behaviour, not on
    # Chronos: CPython 3.12 raises RecursionError inside json.loads at this depth
    # ("not valid JSON"), while 3.14 parses the 10,000-deep array and the object
    # check refuses it ("not a JSON object"). Both are the fail-closed outcome the
    # test exists to prove; the record is invalid either way, and nothing recursed
    # out of the view. Pinning one string pinned the interpreter, not the property.
    assert entry["invalid_detail"] in {
        "record payload is not valid JSON",
        "record payload is not a JSON object",
    }


def test_oversized_invalid_receipt_is_not_echoed_by_the_bounded_view(
    client: TestClient, headers: dict[str, str]
) -> None:
    oversized = "x" * (MAX_OPTION_SELECTION_RECEIPT_BYTES + 1)
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind="NO_TRADE",
            payload={
                "decision_id": "oversized",
                "status": "NO_TRADE",
                "output_digest": "0" * 64,
                "canonical_receipt": oversized,
            },
            recorded_at=_NOW,
        )

    response = client.get("/terminal/option-selections", headers=headers)
    body = response.json()
    entry = body["entries"][0]

    assert response.status_code == 200
    assert len(response.content) < 100_000
    assert body["chain_valid"] is True
    assert body["semantic_valid"] is False
    assert entry["canonical_receipt"] is None
    assert entry["receipt_valid"] is False
    assert "exceeds the inspection size limit" in entry["invalid_detail"]


def test_oversized_payload_envelope_is_rejected_before_decoding(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = "x" * (MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS + 1)
    database = _app(client).state.backend.runtime.database
    sessions = database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind="NO_TRADE",
            payload={"unexpected_padding": oversized},
            recorded_at=_NOW,
        )

    def unexpected_decode(_: str) -> object:
        raise AssertionError("oversized payload must be refused before json.loads")

    def unexpected_hash(**_: object) -> str:
        raise AssertionError("oversized payload must be refused before compute_hash")

    projected_queries: list[tuple[str, object]] = []

    def capture_projection(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, context, executemany
        if "FROM hash_chain_records" in statement:
            projected_queries.append((statement, parameters))

    monkeypatch.setattr(views, "json", SimpleNamespace(loads=unexpected_decode))
    monkeypatch.setattr(hash_chain, "compute_hash", unexpected_hash)
    event.listen(database.engine, "before_cursor_execute", capture_projection)
    try:
        response = client.get("/terminal/option-selections", headers=headers)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_projection)
    body = response.json()
    entry = body["entries"][0]

    assert len(projected_queries) == 1
    projected_sql, projected_parameters = projected_queries[0]
    projection = projected_sql.partition("FROM hash_chain_records")[0].lower()
    assert "typeof(hash_chain_records.payload_json)" in projection
    assert "cast(hash_chain_records.payload_json as text)" in projection
    assert " as payload_length" in projection
    assert " as payload_json" in projection
    assert isinstance(projected_parameters, tuple)
    assert MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS + 1 in projected_parameters
    assert response.status_code == 200
    assert len(response.content) < 100_000
    assert oversized not in response.text
    assert body["chain_valid"] is False
    assert body["chain_broken_at"] == 1
    assert (
        f"record payload exceeds the {MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS}-character "
        "inspection limit"
    ) in body["chain_detail"]
    assert body["semantic_valid"] is False
    assert entry["decision_id"] is None
    assert entry["status"] is None
    assert entry["output_digest"] is None
    assert entry["canonical_receipt"] is None
    assert entry["record_hash_valid"] is False
    assert entry["receipt_valid"] is False
    assert "digest was not verified" in entry["invalid_detail"]


@pytest.mark.parametrize(
    ("field", "expected_detail"),
    [
        ("sequence", "record sequence has an invalid storage type"),
        (
            "recorded_at",
            f"record timestamp exceeds the {MAX_DURABLE_TIMESTAMP_TEXT_CHARS}-character "
            "inspection limit",
        ),
    ],
)
def test_dynamic_identity_fields_are_sql_bounded_and_rejected_without_echo(
    client: TestClient,
    headers: dict[str, str],
    field: str,
    expected_detail: str,
) -> None:
    receipt = _receipt(f"corrupt-{field}")
    payload_json = hash_chain.canonical_payload(
        {
            "decision_id": receipt.request.decision_id,
            "status": receipt.status.value,
            "output_digest": receipt.output_digest,
            "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
        }
    )
    oversized = "x" * 100_000
    values: dict[str, object] = {
        "stream": durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
        "sequence": 1,
        "kind": receipt.status.value,
        "payload_json": payload_json,
        "recorded_at": "2026-08-03 14:00:00.000000",
        "previous_hash": hash_chain.GENESIS_HASH,
        "record_hash": "0" * 64,
    }
    values[field] = oversized
    database = _app(client).state.backend.runtime.database
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hash_chain_records "
                "(stream, sequence, kind, payload_json, recorded_at, previous_hash, record_hash) "
                "VALUES (:stream, :sequence, :kind, :payload_json, :recorded_at, "
                ":previous_hash, :record_hash)"
            ),
            values,
        )

    projected_sql: list[str] = []

    def capture_projection(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "FROM hash_chain_records" in statement:
            projected_sql.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_projection)
    try:
        response = client.get("/terminal/option-selections", headers=headers)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_projection)
    body = response.json()
    entry = body["entries"][0]

    assert len(projected_sql) == 1
    projection = projected_sql[0].partition("FROM hash_chain_records")[0].lower()
    for column in ("sequence", "recorded_at"):
        assert f"typeof(hash_chain_records.{column})" in projection
        assert f"cast(hash_chain_records.{column} as text)" in projection
    assert " as sequence_length" in projection
    assert " as sequence_text" in projection
    assert " as recorded_at_length" in projection
    assert " as recorded_at_text" in projection
    assert response.status_code == 200
    assert len(response.content) < len(oversized)
    assert oversized not in response.text
    assert body["chain_valid"] is False
    assert body["chain_broken_at"] == 1
    assert entry[field] is None
    assert entry["record_hash_valid"] is False
    assert entry["record_valid"] is False
    assert expected_detail in entry["invalid_detail"]


@pytest.mark.parametrize(
    (
        "field",
        "expected_chain_valid",
        "expected_semantic_valid",
        "expected_record_hash_valid",
        "expected_detail",
    ),
    [
        ("kind", True, False, True, "record kind exceeds the 64-character inspection limit"),
        (
            "previous_hash",
            False,
            True,
            False,
            "previous_hash exceeds the 64-character inspection limit",
        ),
        (
            "record_hash",
            False,
            True,
            False,
            "record_hash exceeds the 64-character inspection limit",
        ),
    ],
)
def test_oversized_hash_chain_columns_are_sql_bounded_and_fail_closed(
    client: TestClient,
    headers: dict[str, str],
    field: str,
    expected_chain_valid: bool,
    expected_semantic_valid: bool,
    expected_record_hash_valid: bool,
    expected_detail: str,
) -> None:
    _append_receipt(client, _receipt(f"oversized-{field}"))
    oversized = "x" * 100_000
    database = _app(client).state.backend.runtime.database
    with database.sessions.begin() as session:
        row = session.scalar(
            select(HashChainRow).where(
                HashChainRow.stream == durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)
            )
        )
        assert row is not None
        if field == "kind":
            row.kind = oversized
        elif field == "previous_hash":
            row.previous_hash = oversized
        else:
            row.record_hash = oversized

    projected_sql: list[str] = []

    def capture_projection(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "FROM hash_chain_records" in statement:
            projected_sql.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_projection)
    try:
        response = client.get("/terminal/option-selections", headers=headers)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_projection)
    body = response.json()
    entry = body["entries"][0]

    assert len(projected_sql) == 1
    projection = projected_sql[0].partition("FROM hash_chain_records")[0].lower()
    for column in ("kind", "previous_hash", "record_hash"):
        assert f"typeof(hash_chain_records.{column})" in projection
        assert f"cast(hash_chain_records.{column} as text)" in projection
        assert f" as {column}_length" in projection
    assert response.status_code == 200
    assert len(response.content) < len(oversized)
    assert oversized not in response.text
    assert body["chain_valid"] is expected_chain_valid
    assert body["semantic_valid"] is expected_semantic_valid
    assert entry["record_hash_valid"] is expected_record_hash_valid
    assert entry["record_valid"] is False
    assert entry["kind"] is None if field == "kind" else entry["kind"] == "NO_TRADE"
    assert expected_detail in entry["invalid_detail"]


def test_invalid_utf8_blob_is_rejected_in_sql_without_driver_decode(
    client: TestClient, headers: dict[str, str]
) -> None:
    database = _app(client).state.backend.runtime.database
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hash_chain_records "
                "(stream, sequence, kind, payload_json, recorded_at, previous_hash, record_hash) "
                "VALUES (:stream, 1, 'NO_TRADE', :payload_json, :recorded_at, "
                ":previous_hash, :record_hash)"
            ),
            {
                "stream": durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
                "payload_json": b"\x80" * 100_000,
                "recorded_at": "2026-08-03 14:00:00.000000",
                "previous_hash": hash_chain.GENESIS_HASH,
                "record_hash": "0" * 64,
            },
        )

    response = client.get("/terminal/option-selections", headers=headers)
    body = response.json()
    entry = body["entries"][0]

    assert response.status_code == 200
    assert len(response.content) < 100_000
    assert body["chain_valid"] is False
    assert body["semantic_valid"] is False
    assert entry["canonical_receipt"] is None
    assert entry["record_hash_valid"] is False
    assert "record payload has an invalid storage type" in entry["invalid_detail"]


@pytest.mark.parametrize(
    ("field", "expected_detail"),
    [
        ("decision_id", "payload decision_id exceeds the 128-character inspection limit"),
        ("status", "payload status exceeds the 8-character inspection limit"),
        ("output_digest", "payload output_digest exceeds the 64-character inspection limit"),
        ("kind", "record kind exceeds the 64-character inspection limit"),
    ],
)
def test_oversized_database_text_is_suppressed_from_the_bounded_view(
    client: TestClient,
    headers: dict[str, str],
    field: str,
    expected_detail: str,
) -> None:
    receipt = _receipt(f"oversized-{field}")
    oversized = "x" * 100_000
    payload = {
        "decision_id": receipt.request.decision_id,
        "status": receipt.status.value,
        "output_digest": receipt.output_digest,
        "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
    }
    kind = receipt.status.value
    if field == "kind":
        kind = oversized
    else:
        payload[field] = oversized

    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind=kind,
            payload=payload,
            recorded_at=receipt.evaluated_at,
        )

    response = client.get("/terminal/option-selections", headers=headers)
    entry = response.json()["entries"][0]

    assert response.status_code == 200
    assert len(response.content) < len(oversized)
    assert oversized not in response.text
    assert entry[field] is None
    assert entry["receipt"] is None
    assert entry["receipt_valid"] is False
    assert expected_detail in entry["invalid_detail"]


def test_receipt_row_timestamp_must_match_evaluation_time(
    client: TestClient, headers: dict[str, str]
) -> None:
    receipt = _receipt("wrong-row-time")
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT),
            kind=receipt.status.value,
            payload={
                "decision_id": receipt.request.decision_id,
                "status": receipt.status.value,
                "output_digest": receipt.output_digest,
                "canonical_receipt": receipt.canonical_bytes().decode("utf-8"),
            },
            recorded_at=receipt.evaluated_at + timedelta(seconds=1),
        )

    entry = client.get("/terminal/option-selections", headers=headers).json()["entries"][0]
    assert entry["record_hash_valid"] is True
    assert entry["receipt_valid"] is False
    assert entry["record_valid"] is False
    assert "timestamp does not match" in entry["invalid_detail"]


def test_database_tamper_marks_record_and_chain_invalid(
    client: TestClient, headers: dict[str, str]
) -> None:
    _append_receipt(client, _receipt("tampered"))
    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        row = session.scalar(
            select(HashChainRow).where(
                HashChainRow.stream == durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)
            )
        )
        assert row is not None
        row.payload_json = '{"not":"complete"'

    body = client.get("/terminal/option-selections", headers=headers).json()
    entry = body["entries"][0]
    assert body["chain_valid"] is False
    assert body["chain_broken_at"] == 1
    assert entry["record_hash_valid"] is False
    assert entry["record_valid"] is False
    assert entry["chain_valid"] is False
    assert entry["receipt"] is None
    assert "record_hash" in entry["invalid_detail"]
    assert "not valid JSON" in entry["invalid_detail"]


def test_chain_break_preserves_prefix_lineage_and_first_failure_detail(
    client: TestClient, headers: dict[str, str]
) -> None:
    for decision_id in ("prefix", "broken", "tail"):
        _append_receipt(client, _receipt(decision_id))

    sessions = _app(client).state.backend.runtime.database.sessions
    with sessions.begin() as session:
        rows = list(
            session.scalars(
                select(HashChainRow)
                .where(
                    HashChainRow.stream == durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT)
                )
                .order_by(HashChainRow.sequence.asc())
            )
        )
        rows[1].payload_json = '{"not":"complete"'

    body = client.get("/terminal/option-selections", headers=headers).json()

    assert body["chain_valid"] is False
    assert body["chain_records"] == 3
    assert body["chain_broken_at"] == 2
    assert body["chain_detail"].startswith("record 2 was modified after it was written")
    assert [entry["sequence"] for entry in body["entries"]] == [3, 2, 1]
    assert [entry["chain_valid"] for entry in body["entries"]] == [False, False, True]
    assert [entry["record_hash_valid"] for entry in body["entries"]] == [True, False, True]
    assert "chain lineage is invalid" in body["entries"][0]["invalid_detail"]
    assert "chain lineage is invalid" not in body["entries"][2]["invalid_detail"]


def test_limit_is_clamped_and_results_are_newest_first(
    client: TestClient, headers: dict[str, str]
) -> None:
    for index in range(4):
        _append_receipt(client, _receipt(f"decision-{index}"))

    body = client.get("/terminal/option-selections?limit=2", headers=headers).json()
    assert body["limit"] == 2
    assert body["chain_records"] == 4
    assert [entry["sequence"] for entry in body["entries"]] == [4, 3]
    assert [entry["decision_id"] for entry in body["entries"]] == [
        "decision-3",
        "decision-2",
    ]

    assert client.get("/terminal/option-selections?limit=0", headers=headers).json()["limit"] == 1
    assert (
        client.get("/terminal/option-selections?limit=999999", headers=headers).json()["limit"]
        == views.MAX_OPTION_SELECTION_LIMIT
    )


def test_route_is_get_only_and_does_not_mutate_or_import_an_order_surface(
    client: TestClient, headers: dict[str, str]
) -> None:
    _append_receipt(client, _receipt("read-only"))
    sessions = _app(client).state.backend.runtime.database.sessions
    statement = select(
        HashChainRow.sequence,
        HashChainRow.kind,
        HashChainRow.payload_json,
        HashChainRow.previous_hash,
        HashChainRow.record_hash,
    ).where(HashChainRow.stream == durable.stream_for(OPTION_SELECTION_STREAM, _FINGERPRINT))
    with sessions.begin() as session:
        before = tuple(session.execute(statement).all())

    assert client.get("/terminal/option-selections", headers=headers).status_code == 200
    assert client.post("/terminal/option-selections", headers=headers).status_code == 405
    with sessions.begin() as session:
        after = tuple(session.execute(statement).all())
    assert after == before

    openapi_methods = set(_app(client).openapi()["paths"]["/terminal/option-selections"])
    assert openapi_methods == {"get"}

    from chronos.api.routes import terminal as terminal_routes

    for module_path in (Path(terminal_routes.__file__), Path(views.__file__)):
        imported: list[str] = []
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [
            name
            for name in imported
            if name.startswith(("chronos.broker", "chronos.execution", "chronos.orders"))
        ]


def test_read_only_backend_can_still_inspect_receipts(demo_env: Path) -> None:
    database = Database(f"sqlite:///{demo_env / 'chronos.db'}")
    database.initialize()
    lease = WriterLease(database.sessions, holder="receipt-test-writer")
    assert lease.acquire() is True
    try:
        with TestClient(create_app()) as read_only_client:
            token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()
            response = read_only_client.get(
                "/terminal/option-selections",
                headers={"X-Chronos-Token": token},
            )
            assert read_only_client.get("/health").json()["read_only"] is True
            assert response.status_code == 200
            assert response.json()["chain_valid"] is True
    finally:
        lease.release()
        database.dispose()

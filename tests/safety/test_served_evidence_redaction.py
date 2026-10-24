"""SDOC-1: the raw broker account id never reaches a served document or a model prompt.

Daybreak P1-1 (n3-decision-20261015): ``compose_served_document`` serialized the whole
``AccountSummary`` — raw ``account_id`` included — and the worker interpolated those bytes
into the Anthropic / xAI / local prompt. The legacy (binding-off) worker path canonicalized
``/account/summary`` and ``/account/positions`` wholesale the same way. The repository's
model-view contract (``chronos.autonomy.evidence``) says account identity appears only as a
pseudonymous fingerprint; these pins hold both paths to it.

Every assertion is over BYTES (the served document string, the serialized provider request),
not a parsed key, so a renamed or nested field cannot slip past. The id is a distinctive
synthetic value planted at the broker seam; no model client is real — each request is built
and recorded, never sent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from worker import evidence as worker_evidence
from worker import model as anthropic_model
from worker import model_local, model_xai
from worker.config import WorkerConfig

from chronos.broker import demo as demo_module
from chronos.domain.models import BrokerPosition, UnderlyingContract
from tests.safety.test_evidence_bundles_exercised import (
    PROPOSER_HEADER,
    TOKEN_HEADER,
    WORKER_CREDENTIAL,
    _api_token,
    _boot_with_evidence,
    demo_env,  # noqa: F401  (fixture reused from the evidence-bundle suite)
)

RAW_ID = "DU9999999"

_PROVIDERS = {
    "anthropic": anthropic_model.build_request,
    "xai": model_xai.build_request,
    "local": model_local.build_request,
}


def _config(client: TestClient, token: str) -> WorkerConfig:
    return WorkerConfig(
        provider="anthropic",
        anthropic_api_key="",
        xai_api_key="",
        local_api_key="",
        model="claude-opus-5",
        api_token=token,
        proposer_token=WORKER_CREDENTIAL,
        backend_url="http://testserver",
        local_base_url="http://127.0.0.1:11434/v1",
        symbols=("SPY",),
        kinds=frozenset({"OPEN", "HOLD"}),
        policy="Hold unless the case is overwhelming.",
        interval_seconds=300,
        lookback_days=5,
        forward=False,
        max_daily_tokens=None,
    )


@pytest.fixture
def raw_id_planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo broker reports RAW_ID as its account and holds one position under it."""

    monkeypatch.setattr(demo_module, "DEMO_ACCOUNT_ID", RAW_ID)

    async def _positions(self: Any) -> tuple[BrokerPosition, ...]:
        return (
            BrokerPosition(
                account_id=RAW_ID,
                contract=UnderlyingContract(con_id=756733, symbol="SPY"),
                quantity=Decimal("3"),
                average_cost=Decimal("500"),
                market_price=Decimal("505"),
                unrealized_pnl=Decimal("15"),
            ),
        )

    monkeypatch.setattr(demo_module.DemoBroker, "positions", _positions)


def _issue(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/autonomy/evidence",
        json={"kind": "backend_served", "symbols": ["SPY"], "lookback_days": 5},
        headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_planted_id_really_reaches_the_broker_facts(
    demo_env: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raw_id_planted: None,
) -> None:
    """Positive control: the raw routes DO carry the id, so absence below means something."""

    with _boot_with_evidence(monkeypatch, demo_env) as client:
        headers = {TOKEN_HEADER: _api_token(demo_env)}
        assert RAW_ID in client.get("/account/summary", headers=headers).text
        assert RAW_ID in client.get("/account/positions", headers=headers).text


def test_the_served_document_carries_no_raw_account_id(
    demo_env: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raw_id_planted: None,
) -> None:
    with _boot_with_evidence(monkeypatch, demo_env) as client:
        document = _issue(client)["document"]

    assert RAW_ID not in document
    facts = json.loads(document)
    # The allow-list keeps the facts a proposer reasons from.
    assert facts["account"]["net_liquidation"] == "250000.00"
    assert facts["positions"][0]["quantity"] == "3"
    assert facts["positions"][0]["contract"]["symbol"] == "SPY"


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_no_provider_request_built_from_the_served_document_carries_the_id(
    provider: str,
    demo_env: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raw_id_planted: None,
) -> None:
    with _boot_with_evidence(monkeypatch, demo_env) as client:
        config = _config(client, _api_token(demo_env))
        snapshot = worker_evidence.request_issued_bundle(config, client)
    assert isinstance(snapshot, worker_evidence.EvidenceSnapshot)

    request = _PROVIDERS[provider](config, snapshot)
    assert RAW_ID not in json.dumps(request, ensure_ascii=False)


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_no_provider_request_built_on_the_legacy_path_carries_the_id(
    provider: str,
    demo_env: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raw_id_planted: None,
) -> None:
    """Evidence binding off: the worker composes locally from the raw read routes."""

    with _boot_with_evidence(monkeypatch, demo_env, evidence=False) as client:
        config = _config(client, _api_token(demo_env))
        snapshot = worker_evidence.gather(config, client)
    assert snapshot is not None

    assert RAW_ID not in snapshot.canonical
    request = _PROVIDERS[provider](config, snapshot)
    assert RAW_ID not in json.dumps(request, ensure_ascii=False)
    facts = json.loads(snapshot.canonical)
    assert facts["account"]["net_liquidation"] == "250000.00"
    assert facts["positions"][0]["quantity"] == "3"


def test_the_worker_restates_the_backends_allow_lists_exactly() -> None:
    from chronos.api.routes import autonomy as served

    assert worker_evidence.ACCOUNT_FACT_FIELDS == served.ACCOUNT_FACT_FIELDS
    assert worker_evidence.POSITION_FACT_FIELDS == served.POSITION_FACT_FIELDS
    assert "account_id" not in served.ACCOUNT_FACT_FIELDS + served.POSITION_FACT_FIELDS

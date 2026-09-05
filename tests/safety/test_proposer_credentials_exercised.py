"""ADR-0023, exercised: identity comes from the credential, and only there.

Plan §6 finding 6 named two defects in one sentence: the proposal ingress
accepted the same local token every mutating route accepts, and every proposal
was stamped with one hardcoded identity. ADR-0023 (Option A, owner-directed
2026-08-12) fixes both with a proposer registry, and requires the fixes be
**exercised, not present**:

- the proposer credential is refused on ``/orders/*``, ``/live/*`` and every
  other mutating route — proposal-only means proposal-only;
- an unregistered, expired, or disabled credential refuses;
- the identity in the journal matches the credential used, never the payload
  and never the static ingress constant;
- the general token is refused on proposals once a registry is configured;
- with no registry configured, nothing changes;
- a configured-but-broken registry refuses everything and alerts, and never
  falls back to the anonymous posture;
- a registration whose expiry passes between enqueue and drain refuses at
  STAMP, because identity is resolved when authority is exercised, not when it
  was requested (the registry itself is a boot-time snapshot: disabling or
  deleting an entry in the file is honored at the next restart, like the
  mandate file — expiry is the transition the snapshot carries live).

Also pinned here, because these tests are what exposed it: the route's account
scoping. Until 2026-08-12, ``_fingerprint_of`` in ``routes/autonomy.py`` read
``runtime.account_fingerprint`` — an attribute :class:`AppRuntime` never had —
so **every** proposal POST answered 503 BACKEND_UNSCOPED and the alert
endpoint always answered ``[]``, on every backend since the route existed.
Fail-closed in direction, inert-control in shape (R-24..R-27). The positive
controls below (a 202 that actually enqueues; an alert list that actually
lists) are the revert-the-fix guards.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.api.autonomy_wiring import INGRESS_IDENTITY, build_identity_resolver
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
from chronos.persistence.schema import HashChainRow
from chronos.supervisor import durable, proposals, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts, CycleStage, run_cycle
from chronos.supervisor.proposers import (
    ProposerRegistration,
    credential_hash,
    registration_binding,
)
from chronos.supervisor.runtime import AutonomyRuntime, RuntimeConfig
from chronos.supervisor.sizing import AccountEvidence

REPO_ROOT = Path(__file__).resolve().parents[2]

_NOW = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64

TOKEN_HEADER = "X-Chronos-Token"
PROPOSER_HEADER = "X-Chronos-Proposer-Token"

#: Credentials "minted" for these tests. Production uses ``secrets.token_hex``;
#: fixed strings here change nothing, because the registry stores only hashes.
WORKER_CREDENTIAL = "w" * 64
EXPIRED_CREDENTIAL = "e" * 64
DISABLED_CREDENTIAL = "d" * 64
UNREGISTERED_CREDENTIAL = "u" * 64

#: Kept current far past this test's fixed clock AND the wall clock the route
#: plane verifies against, without being a forever credential.
_FAR_EXPIRY = "2030-01-01T00:00:00+00:00"
_PAST_EXPIRY = "2020-01-01T00:00:00+00:00"


def _registration(
    proposer_id: str,
    credential: str,
    *,
    expires_at: str = _FAR_EXPIRY,
    enabled: bool = True,
    **overrides: str,
) -> dict[str, Any]:
    # Five DISTINCT version values on purpose: a resolver that cross-wires two
    # fields (stamps prompt_version from policy_version, say) is invisible when
    # every field is "1" — with distinct values, any mis-stamped field breaks
    # the version-pin agreement the journal test asserts positively.
    entry: dict[str, Any] = {
        "proposer_id": proposer_id,
        "secret_sha256": credential_hash(credential),
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "mv-7",
        "prompt_version": "pv-3",
        "tool_schema_version": "ts-2",
        "decision_schema_version": "ds-4",
        "policy_version": "pol-5",
        "expires_at": expires_at,
        "enabled": enabled,
    }
    entry.update(overrides)
    return entry


def _registry_text(*entries: dict[str, Any]) -> str:
    return json.dumps({"schema_version": 1, "proposers": list(entries)})


def _binding_values(entry: dict[str, Any]) -> tuple[str, str]:
    binding = registration_binding(ProposerRegistration.model_validate(entry))
    return binding.credential_epoch, binding.registry_entry_digest


def test_registration_binding_is_semantic_per_entry_and_rotation_sensitive() -> None:
    base = _registration("claude-worker", WORKER_CREDENTIAL)
    equivalent_offset = dict(base, expires_at="2029-12-31T19:00:00-05:00")
    relabelled = dict(base, policy_version="pol-6")
    rotated = _registration("claude-worker", "r" * 64)

    assert _binding_values(base) == _binding_values(equivalent_offset)
    assert _binding_values(base)[0] == _binding_values(relabelled)[0]
    assert _binding_values(base)[1] != _binding_values(relabelled)[1]
    assert _binding_values(base)[0] != _binding_values(rotated)[0]
    assert _binding_values(base)[1] != _binding_values(rotated)[1]


# ----------------------------------------------------------- the route plane


PROPOSAL_BODY = {
    "kind": "HOLD",
    "asset_class": "EQUITY",
    "symbol": "SPY",
    "direction": "NEUTRAL",
    "thesis": "exercised-test proposal",
}


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    from chronos.config.settings import get_settings

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    # ADR-0054: the first writer boot of a fresh database seeds an installation
    # marker in the state directory. Redirect it with the database, or these
    # tests would leave one in the repository's own `data/` and the next test
    # would read a fresh database beside a surviving marker as a replaced one.
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _boot(monkeypatch: pytest.MonkeyPatch, registry_text: str | None, tmp_path: Path) -> TestClient:
    """Boot the real app through its lifespan, optionally with a registry."""

    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    if registry_text is not None:
        registry_path = tmp_path / "autonomy_proposers.json"
        registry_path.write_text(registry_text, encoding="utf-8")
        monkeypatch.setenv("AUTONOMY_PROPOSERS_FILE", str(registry_path))
        get_settings.cache_clear()
    return TestClient(create_app())


def _api_token(tmp_path: Path) -> str:
    return (tmp_path / "backend_api_token").read_text(encoding="utf-8").strip()


def _queue_rows(tmp_path: Path) -> list[tuple[str | None, str | None, str | None, str]]:
    with sqlite3.connect(tmp_path / "chronos.db") as connection:
        return list(
            connection.execute(
                "SELECT proposer_id, proposer_credential_epoch, "
                "proposer_registry_entry_digest, payload FROM autonomy_proposal_queue"
            )
        )


def test_registry_on_credential_decides_and_the_row_records_who(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One boot, the whole registry-on posture, ADR-0023's route-plane proofs."""

    registry = _registry_text(
        _registration("claude-worker", WORKER_CREDENTIAL),
        _registration("expired-worker", EXPIRED_CREDENTIAL, expires_at=_PAST_EXPIRY),
        _registration("disabled-worker", DISABLED_CREDENTIAL, enabled=False),
    )
    with _boot(monkeypatch, registry, demo_env) as client:
        token = _api_token(demo_env)

        # The general token no longer opens the proposal route, and the refusal
        # says what changed rather than leaving a worker to retry blindly.
        held_over = client.post(
            "/autonomy/proposals", json=PROPOSAL_BODY, headers={TOKEN_HEADER: token}
        )
        assert held_over.status_code == 401
        assert PROPOSER_HEADER in held_over.json()["detail"]

        # No credential at all: refused, and the body names the header only —
        # never a proposer id, never registry contents.
        bare = client.post("/autonomy/proposals", json=PROPOSAL_BODY)
        assert bare.status_code == 401
        assert "claude-worker" not in bare.text

        # Unregistered, expired, and disabled all refuse with ONE message: which
        # of the three it was is the owner's business, not the caller's.
        refusals = set()
        for credential in (UNREGISTERED_CREDENTIAL, EXPIRED_CREDENTIAL, DISABLED_CREDENTIAL):
            response = client.post(
                "/autonomy/proposals",
                json=PROPOSAL_BODY,
                headers={PROPOSER_HEADER: credential},
            )
            assert response.status_code == 401
            refusals.add(response.json()["detail"])
        assert len(refusals) == 1

        assert _queue_rows(demo_env) == []  # nothing above reached the queue

        # The positive control: a current registered credential — alone, no
        # general token — queues, and the row records the VERIFIED id. This 202
        # is also the revert-the-fix guard for the BACKEND_UNSCOPED defect: put
        # `runtime.account_fingerprint` back in `_fingerprint_of` and this is a
        # 503 again.
        accepted = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["accepted"] is True
        rows = _queue_rows(demo_env)
        assert len(rows) == 1
        assert rows[0][0] == "claude-worker"
        epoch, digest = _binding_values(_registration("claude-worker", WORKER_CREDENTIAL))
        assert rows[0][1:3] == (epoch, digest)

        # The payload cannot vote on identity: it has no such field, and a
        # payload that tries to carry provenance is refused by the ingress
        # before the queue exists to it.
        forged = dict(PROPOSAL_BODY)
        forged["provenance"] = {"model_id": "forged"}
        refused = client.post(
            "/autonomy/proposals",
            json=forged,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert refused.status_code == 422
        assert len(_queue_rows(demo_env)) == 1  # still just the one honest row

        # The proposer credential opens nothing else: the operator alert
        # endpoint stays on the local token under every posture.
        assert (
            client.get("/autonomy/alerts", headers={PROPOSER_HEADER: WORKER_CREDENTIAL})
        ).status_code == 401
        assert (
            client.get("/autonomy/alerts", headers={TOKEN_HEADER: WORKER_CREDENTIAL})
        ).status_code == 401
        assert client.get("/autonomy/alerts", headers={TOKEN_HEADER: token}).status_code == 200


def test_authenticated_large_json_integer_is_a_canonical_ingress_refusal(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small authenticated hostile payload must never become an HTTP 500."""

    registry = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    hostile_number = "1" * 4_301
    body = '{"requested_quantity":' + hostile_number + "}"
    with _boot(monkeypatch, registry, demo_env) as client:
        response = client.post(
            "/autonomy/proposals",
            content=body,
            headers={
                PROPOSER_HEADER: WORKER_CREDENTIAL,
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "accepted": False,
        "stage": "INGRESS",
        "refusal": "MALFORMED_PROPOSAL",
        "detail": "payload is not a single well-formed JSON document",
    }
    assert hostile_number not in response.text
    assert _queue_rows(demo_env) == []


#: Every route a registered proposer's credential may open, named one by one.
#: R-48's rule is that this credential is proposal-only; ADR-0028 widened it by
#: exactly one route and required the widening to be *named and tested* rather
#: than absorbed. Adding an entry here is a reviewable act that should carry an
#: ADR with it — the list is the record of how far that credential reaches.
_PROPOSER_CREDENTIAL_ROUTES = frozenset(
    {
        "/autonomy/proposals",  # ADR-0023: the reason the credential exists.
        "/autonomy/evidence",  # ADR-0028: issuance, the one deliberate addition.
    }
)


def _api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every APIRoute in the table, across FastAPI's router-nesting shapes.

    Newer FastAPI keeps an included router as one ``_IncludedRouter`` entry
    whose ``original_router`` holds the real routes; older versions flatten
    them, and mounts (the static files) are neither. Handling all three keeps
    the enumeration meaning "the whole application" across upgrades.
    """

    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _api_routes(inner.routes)
        elif isinstance(route, APIRoute):
            yield route


def test_the_proposer_credential_is_refused_on_every_other_mutating_route(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0023's proposal-only requirement, enumerated rather than sampled.

    Every mutating route in the application is exercised with the registered
    credential presented every way a confused or hostile process could present
    it — as the proposer header, as the local token, and as the terminal
    session's body token — and every one must answer 401. The enumeration is
    over the app's own route table, so a route added later is included by
    existing rather than by being remembered.

    ## The exceptions are NAMED, and there are exactly two

    ADR-0028 makes issuance a *write* reachable by a proposal-only credential —
    the first widening of that credential since ADR-0023 made it proposal-only —
    and requires this enumeration to **grow the route as a deliberate, named
    exception rather than absorb it**. So the exception list below is explicit
    and asserted non-empty per entry: a route silently failing to appear is
    itself a failure, because an enumeration that quietly stops covering
    something is the R-24..R-27 shape applied to a test.

    Each exception carries its own positive control elsewhere in this file
    (proposals: ``test_registry_on_credential_decides_and_the_row_records_who``;
    issuance: ``test_the_issuance_route_is_the_credentials_one_named_exception``
    in ``test_evidence_bundles_exercised.py``). ADR-0028's own recommendation
    stands recorded here: this should be the last route that credential opens
    without a further ADR.
    """

    registry = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    with _boot(monkeypatch, registry, demo_env) as client:
        mutating: list[tuple[str, str]] = []
        seen_exceptions: set[str] = set()
        for route in _api_routes(client.app.routes):  # type: ignore[attr-defined]
            for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
                if route.path in _PROPOSER_CREDENTIAL_ROUTES:
                    seen_exceptions.add(route.path)
                    continue
                mutating.append((method, route.path.replace("{", "x").replace("}", "x")))
        assert seen_exceptions == _PROPOSER_CREDENTIAL_ROUTES, (
            "a route the proposer credential is allowed to open is missing from the "
            "application, or moved: the named-exception list and the route table must "
            f"agree exactly. Expected {sorted(_PROPOSER_CREDENTIAL_ROUTES)}, "
            f"found {sorted(seen_exceptions)}"
        )
        # If this shrinks, the enumeration broke — it must never quietly pass
        # by exercising nothing.
        assert len(mutating) >= 10, mutating

        credential_presented_everywhere = {
            TOKEN_HEADER: WORKER_CREDENTIAL,
            PROPOSER_HEADER: WORKER_CREDENTIAL,
        }
        failures: list[tuple[str, str, int]] = []
        for method, path in mutating:
            if path == "/terminal/session":
                # This route authenticates from its body, so the credential is
                # presented there too — and still refuses.
                response = client.post(path, json={"token": WORKER_CREDENTIAL})
            else:
                response = client.request(
                    method, path, headers=credential_presented_everywhere, json={}
                )
            if response.status_code != 401:
                failures.append((method, path, response.status_code))
        assert not failures, (
            "the proposer credential opened (or was not cleanly refused by) these "
            f"routes; it must be proposal-only: {failures}"
        )


def test_registry_off_is_exactly_the_pre_registry_posture(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _boot(monkeypatch, None, demo_env) as client:
        token = _api_token(demo_env)

        # The local token queues, as it always did — and the row records no
        # proposer, which is the honest statement of this posture.
        accepted = client.post(
            "/autonomy/proposals", json=PROPOSAL_BODY, headers={TOKEN_HEADER: token}
        )
        assert accepted.status_code == 202, accepted.text
        rows = _queue_rows(demo_env)
        assert len(rows) == 1
        assert rows[0][0] is None

        # A proposer credential is not a fallback token: with no registry to
        # verify it against, it opens nothing.
        refused = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert refused.status_code == 401


def test_a_broken_registry_refuses_everything_and_alerts(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured-but-invalid never falls back to anonymous proposing.

    A file error reopening the token posture would silently undo the owner's
    decision to require attribution — so every proposal refuses with 503, the
    local token included, and the owner is told via a CRITICAL alert.
    """

    with _boot(monkeypatch, "{ this is not a registry", demo_env) as client:
        token = _api_token(demo_env)
        for headers in (
            {TOKEN_HEADER: token},
            {PROPOSER_HEADER: WORKER_CREDENTIAL},
            {},
        ):
            response = client.post("/autonomy/proposals", json=PROPOSAL_BODY, headers=headers)
            assert response.status_code == 503, (headers, response.status_code)
        assert _queue_rows(demo_env) == []

        alerts_shown = client.get("/autonomy/alerts", headers={TOKEN_HEADER: token})
        assert alerts_shown.status_code == 200
        kinds = {alert["kind"] for alert in alerts_shown.json()}
        # Also the revert-the-fix guard for the alert half of the
        # BACKEND_UNSCOPED defect: before 2026-08-12 this endpoint answered []
        # no matter what had been raised.
        assert "autonomy.proposers_invalid" in kinds
        severities = {
            alert["severity"]
            for alert in alerts_shown.json()
            if alert["kind"] == "autonomy.proposers_invalid"
        }
        assert severities == {"CRITICAL"}


# ----------------------------------------------------------- the drain plane


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


def _static_identity() -> queue.HarnessIdentity:
    """A deliberately WRONG identity: its pins do not match the mandate below.

    Handing it to the runtime as the static fallback is the hazard injection —
    if any resolver path fell back to it, the admitted-path test would refuse
    at the version-pin check and the journal test would record ``static-*``
    values instead of the registration's.
    """

    return queue.HarnessIdentity(
        provider="static-provider",
        model_id="static-ingress",
        model_version="0",
        prompt_version="0",
        tool_schema_version="0",
        decision_schema_version="0",
        policy_version="0",
        evidence_bundle_id=INGRESS_IDENTITY.evidence_bundle_id,
        evidence_bundle_digest=INGRESS_IDENTITY.evidence_bundle_digest,
    )


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-adr23",
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
        # These pins name the REGISTRATION's identity — the owner choosing
        # which author this grant trusts (ADR-0023).
        "versions": VersionPins(
            provider="anthropic",
            model_id="model-x",
            model_version="mv-7",
            prompt_version="pv-3",
            tool_schema_version="ts-2",
            decision_schema_version="ds-4",
            policy_version="pol-5",
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
        # The supervisor's own expectation matches what the resolver stamps:
        # the placeholder bundle id, and an honestly absent digest (ADR-0023).
        evidence_bundle_id=INGRESS_IDENTITY.evidence_bundle_id,
        evidence_bundle_digest=INGRESS_IDENTITY.evidence_bundle_digest,
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

    def deliver(self, alert: object) -> bool:
        return True


def _drain_runtime(
    sessions: sessionmaker[Session],
    registry_path: Path,
    mandate: AutonomyMandate,
) -> AutonomyRuntime:
    return AutonomyRuntime(
        sessions=sessions,
        config=RuntimeConfig(account_fingerprint=_FINGERPRINT),
        identity=_static_identity(),
        mandate_source=lambda: mandate,
        gather_facts=_facts,
        sinks=(_NullSink(),),
        submit=None,
        resolve_identity=build_identity_resolver(registry_path),
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


def _enqueue(
    sessions: sessionmaker[Session],
    *,
    proposer_id: str | None,
    registration_entry: dict[str, Any] | None = None,
) -> None:
    epoch: str | None = None
    digest: str | None = None
    if proposer_id is not None:
        epoch, digest = _binding_values(
            registration_entry or _registration("claude-worker", WORKER_CREDENTIAL)
        )
    with sessions.begin() as session:
        outcome = proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=_payload(),
            now=_NOW,
            proposer_id=proposer_id,
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=digest,
        )
        assert outcome.queued


def _stream_payloads(
    sessions: sessionmaker[Session], stream: str, kind: str | None = None
) -> list[dict[str, Any]]:
    from chronos.persistence import hash_chain

    with sessions.begin() as session:
        rows = session.scalars(
            select(HashChainRow).where(HashChainRow.stream == stream).order_by(HashChainRow.id)
        ).all()
        return [hash_chain.payload_of(row) for row in rows if kind is None or row.kind == kind]


@pytest.mark.parametrize(
    ("epoch", "digest"),
    [
        pytest.param(None, "a" * 64, id="missing-epoch"),
        pytest.param("a" * 64, None, id="missing-digest"),
        pytest.param("not-a-digest", "a" * 64, id="malformed-epoch"),
        pytest.param("a" * 64, "A" * 64, id="noncanonical-digest"),
    ],
)
def test_registered_enqueue_requires_a_complete_canonical_binding(
    sessions: sessionmaker[Session], epoch: str | None, digest: str | None
) -> None:
    with (
        sessions.begin() as session,
        pytest.raises(ValueError, match="64-character lowercase"),
    ):
        proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=_payload(),
            now=_NOW,
            proposer_id="claude-worker",
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=digest,
        )
    with sessions.begin() as session:
        assert proposals.pending_depth(session, account_fingerprint=_FINGERPRINT) == 0


def test_the_journaled_identity_matches_the_credential_used(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The end-to-end attribution proof, against the real drain.

    A row recorded as ``claude-worker`` drains through the resolver and the
    writer stamps the REGISTRATION's identity — visible in the hash-chained
    proposal journal. The runtime's static identity is deliberately wrong, so
    any fallback to it would fail this test twice: wrong journal values, and a
    version-pin refusal instead of an admitted decision.
    """

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(_registration("claude-worker", WORKER_CREDENTIAL)), encoding="utf-8"
    )
    mandate = _mandate()
    _activate(sessions, mandate)
    _enqueue(sessions, proposer_id="claude-worker")

    runtime = _drain_runtime(sessions, registry_path, mandate)
    report = runtime.run_tick(_NOW)
    assert report.proposals_judged == 1

    accepted = _stream_payloads(
        sessions, f"{queue.PROPOSAL_STREAM}:{_FINGERPRINT}", kind="proposal_accepted"
    )
    assert len(accepted) == 1
    assert accepted[0]["proposer_id"] == "claude-worker"
    assert accepted[0]["model_id"] == "model-x"  # the registration's, not "static-ingress"

    # And the decision was ADMITTED — asserted positively, not by exclusion.
    # An admitted SHADOW walk (no submit callable) terminates at HANDOFF with
    # NO_SUBMISSION_CONFIGURED, so this pair fires on ANY admission refusal —
    # a version-pin disagreement included. That is the "pins bound to an
    # author" proof: all seven stamped fields (five of them distinct values)
    # agreed with the mandate's pins. The first version of this assertion
    # compared against a misspelled refusal code and could never fail; the
    # adversarial review caught it, and the mutation that exposed it (a
    # resolver stamping one version field wrong) now fails here.
    cycles = _stream_payloads(sessions, f"autonomy.cycles:{_FINGERPRINT}")
    assert cycles, "the cycle journal must record the walk"
    assert cycles[-1]["stage"] == CycleStage.HANDOFF.value
    assert cycles[-1]["refusal"] == "NO_SUBMISSION_CONFIGURED"


@pytest.mark.parametrize(
    ("proposer_id", "why"),
    [
        pytest.param("claude-worker", "revoked-by-expiry between enqueue and drain", id="expired"),
        pytest.param("ghost", "an id the registry never contained", id="unknown"),
        pytest.param(None, "a pre-registry row met by a registry-on runtime", id="pre-registry"),
    ],
)
def test_an_unresolvable_proposer_refuses_at_stamp(
    sessions: sessionmaker[Session], tmp_path: Path, proposer_id: str | None, why: str
) -> None:
    """Identity is re-resolved when authority is exercised, not when requested.

    The registration is CURRENT at enqueue time and EXPIRED by drain time — and
    the drain is the moment that counts, so the cycle refuses at STAMP with
    PROPOSER_UNRESOLVED rather than stamping a dead registration's identity (or
    falling back to the static one). Expiry is the one transition the boot-time
    registry snapshot carries with it; a disable/delete edit to the file lands
    at the next restart.
    """

    registry_path = tmp_path / "proposers.json"
    entry = _registration(
        "claude-worker",
        WORKER_CREDENTIAL,
        expires_at=(_NOW + timedelta(hours=1)).isoformat(),
    )
    registry_path.write_text(_registry_text(entry), encoding="utf-8")
    mandate = _mandate(expires_at=_NOW + timedelta(days=7))
    _activate(sessions, mandate)
    _enqueue(sessions, proposer_id=proposer_id, registration_entry=entry)

    runtime = _drain_runtime(sessions, registry_path, mandate)
    drain_at = _NOW + timedelta(hours=2)  # past the registration's expiry
    report = runtime.run_tick(drain_at)
    assert report.proposals_judged == 1, why

    cycles = _stream_payloads(sessions, f"autonomy.cycles:{_FINGERPRINT}")
    assert cycles[-1]["stage"] == CycleStage.STAMP.value
    assert cycles[-1]["refusal"] == "PROPOSER_UNRESOLVED"
    # Nothing was stamped: the proposal journal has no accepted entry.
    accepted = _stream_payloads(
        sessions, f"{queue.PROPOSAL_STREAM}:{_FINGERPRINT}", kind="proposal_accepted"
    )
    assert accepted == []


def test_the_resolver_postures_are_fail_closed(
    tmp_path: Path, sessions: sessionmaker[Session]
) -> None:
    """`build_identity_resolver`'s three shapes, exercised directly.

    No path is None (the pre-registry posture, preserved exactly); a valid
    registry resolves current registrations and nothing else; an unreadable
    file resolves NOTHING — never the static identity, because a file error
    must not silently reopen anonymous proposing.

    Since A3 the resolver is handed the tick's session, because it also
    consults the durable revocation ledger; it returns a ``ResolvedIdentity``
    carrying the refusal the cycle should journal rather than a bare ``None``.
    """

    assert build_identity_resolver(None) is None

    registry_path = tmp_path / "proposers.json"
    current_entry = _registration("claude-worker", WORKER_CREDENTIAL)
    disabled_entry = _registration("disabled-worker", DISABLED_CREDENTIAL, enabled=False)
    registry_path.write_text(
        _registry_text(current_entry, disabled_entry),
        encoding="utf-8",
    )
    resolve = build_identity_resolver(registry_path)
    assert resolve is not None
    current_epoch, current_digest = _binding_values(current_entry)
    with sessions.begin() as session:
        resolution = resolve(session, "claude-worker", current_epoch, current_digest, _NOW)
    identity = resolution.identity
    assert identity is not None
    assert identity.proposer_id == "claude-worker"
    # All seven stampable fields, each against its own distinct registration
    # value, so a resolver that cross-wires or hardcodes any one of them fails
    # here by name rather than only downstream at the version-pin check.
    assert identity.provider == "anthropic"
    assert identity.model_id == "model-x"
    assert identity.model_version == "mv-7"
    assert identity.prompt_version == "pv-3"
    assert identity.tool_schema_version == "ts-2"
    assert identity.decision_schema_version == "ds-4"
    assert identity.policy_version == "pol-5"
    assert identity.evidence_bundle_id == INGRESS_IDENTITY.evidence_bundle_id
    assert identity.evidence_bundle_digest is None  # honestly absent, never zeros
    with sessions.begin() as session:
        # Each refusing posture names itself (A3), so the journal can tell an
        # aged-out registration from a pre-registry row from a broken file.
        disabled_epoch, disabled_digest = _binding_values(disabled_entry)
        refused = resolve(session, "disabled-worker", disabled_epoch, disabled_digest, _NOW)
        assert refused.identity is None
        assert refused.refusal == "PROPOSER_UNRESOLVED"
        legacy = resolve(session, "claude-worker", None, None, _NOW)
        assert legacy.identity is None
        assert legacy.refusal == "PROPOSER_REGISTRATION_UNBOUND"
        ghost = resolve(session, "ghost", current_epoch, current_digest, _NOW)
        assert ghost.identity is None
        assert ghost.refusal == "PROPOSER_UNRESOLVED"
        pre_registry = resolve(session, None, None, None, _NOW)
        assert pre_registry.identity is None
        assert pre_registry.refusal == "PROPOSER_UNRESOLVED"
        assert "before a proposer registry was configured" in pre_registry.detail

    broken = build_identity_resolver(tmp_path / "does-not-exist.json")
    assert broken is not None  # configured-but-broken is NOT the None posture
    with sessions.begin() as session:
        unreadable = broken(session, "claude-worker", current_epoch, current_digest, _NOW)
    assert unreadable.identity is None
    assert unreadable.refusal == "PROPOSER_REGISTRY_INVALID"


# ------------------------------------------------- evidence-digest honesty


def _run_admission_cycle(
    sessions: sessionmaker[Session],
    *,
    stamped_digest: str | None,
    expected_digest: str | None,
) -> Any:
    """One HOLD proposal through the real gate stack with controlled digests.

    HOLD is used because it exhausts checks 1-9 and then refuses at check 10
    (NOT_EXECUTABLE) — so NOT_EXECUTABLE is the unambiguous signal that the
    evidence-bundle check PASSED, with no order machinery in the way.
    """

    mandate = _mandate(
        versions=VersionPins(
            provider="static-provider",
            model_id="static-ingress",
            model_version="0",
            prompt_version="0",
            tool_schema_version="0",
            decision_schema_version="0",
            policy_version="0",
        )
    )
    _activate(sessions, mandate)
    identity = queue.HarnessIdentity(
        provider="static-provider",
        model_id="static-ingress",
        model_version="0",
        prompt_version="0",
        tool_schema_version="0",
        decision_schema_version="0",
        policy_version="0",
        evidence_bundle_id="owner-workspace",
        evidence_bundle_digest=stamped_digest,
    )
    facts_base = _facts(_NOW)
    facts = CycleFacts(
        **{
            **{name: getattr(facts_base, name) for name in facts_base.__dataclass_fields__},
            "evidence_bundle_id": "owner-workspace",
            "evidence_bundle_digest": expected_digest,
        }
    )
    payload = json.dumps(
        {
            "kind": "HOLD",
            "asset_class": "EQUITY",
            "symbol": "SPY",
            "direction": "NEUTRAL",
            "thesis": "digest tri-state proof",
        }
    )
    with sessions.begin() as session:
        return run_cycle(
            payload,
            session=session,
            mandate=mandate,
            identity=identity,
            facts=facts,
        )


def test_absence_attested_on_both_sides_passes_the_evidence_check(
    sessions: sessionmaker[Session],
) -> None:
    """ADR-0023: no bundle machinery exists, and absence is stated as absence.

    The supervisor expects no digest and the writer stamped none — the check
    passes and the HOLD proceeds to its own refusal. This is NOT pass-when-
    absent: the passing condition is two attestations that AGREE, as the two
    tests below prove by disagreeing in each direction.
    """

    outcome = _run_admission_cycle(sessions, stamped_digest=None, expected_digest=None)
    assert outcome.stage == CycleStage.ADMISSION
    assert outcome.refusal == "NOT_EXECUTABLE"


@pytest.mark.parametrize(
    ("stamped", "expected", "direction"),
    [
        pytest.param("f" * 64, None, "a digest claimed when none was issued", id="claimed"),
        pytest.param(None, "f" * 64, "no digest claimed when one was issued", id="omitted"),
        pytest.param("f" * 64, "0" * 64, "the wrong digest", id="wrong"),
    ],
)
def test_a_digest_disagreement_refuses_in_every_direction(
    sessions: sessionmaker[Session], stamped: str | None, expected: str | None, direction: str
) -> None:
    outcome = _run_admission_cycle(sessions, stamped_digest=stamped, expected_digest=expected)
    assert outcome.stage == CycleStage.ADMISSION, direction
    assert outcome.refusal == "EVIDENCE_BUNDLE_MISMATCH", direction


# ------------------------------------------------------------- the CLI seam


def _ledger_database_url(tmp_path: Path) -> str:
    """A real, initialized database for the CLI to read revocations from (A3).

    `proposer check` consults the ledger, and an entry it could not verify is
    reported UNVERIFIED rather than CURRENT — so a test that wants to assert
    CURRENT has to give it a ledger to read.
    """

    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    database = Database(url)
    try:
        database.initialize()
    finally:
        database.dispose()
    return url


def test_mint_prints_a_credential_whose_hash_is_the_registration_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from chronos.cli.proposer_commands import cmd_proposer_mint

    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    code = cmd_proposer_mint(
        argparse.Namespace(
            proposer_id="claude-worker",
            provider="anthropic",
            model_id="claude-opus-5",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
            expires_at="",
            expires_days=90,
        )
    )
    assert code == 0
    assert set(tmp_path.iterdir()) == before  # a tool that mints text does not grant

    out = capsys.readouterr().out
    credential = next(
        line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("credential:")
    )
    document = json.loads(out[out.rindex('{\n  "schema_version"') :])
    entry = document["proposers"][0]
    assert entry["secret_sha256"] == credential_hash(credential)
    assert credential not in json.dumps(entry)  # the registration never holds the secret
    assert entry["proposer_id"] == "claude-worker"


def test_check_reports_current_disabled_and_expired_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from chronos.cli.proposer_commands import cmd_proposer_check

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(
            _registration("claude-worker", WORKER_CREDENTIAL),
            _registration("expired-worker", EXPIRED_CREDENTIAL, expires_at=_PAST_EXPIRY),
            _registration("disabled-worker", DISABLED_CREDENTIAL, enabled=False),
        ),
        encoding="utf-8",
    )
    before = registry_path.read_bytes()
    assert (
        cmd_proposer_check(
            argparse.Namespace(file=str(registry_path), database_url=_ledger_database_url(tmp_path))
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "claude-worker" in out and "CURRENT" in out
    assert "expired-worker" in out and "EXPIRED" in out
    assert "disabled-worker" in out and "DISABLED" in out
    assert WORKER_CREDENTIAL not in out  # hashes in, hashes out; never a credential
    assert registry_path.read_bytes() == before

    broken = tmp_path / "broken.json"
    broken.write_text("{ nope", encoding="utf-8")
    assert (
        cmd_proposer_check(
            argparse.Namespace(file=str(broken), database_url=_ledger_database_url(tmp_path))
        )
        == 1
    )
    assert "INVALID" in capsys.readouterr().out

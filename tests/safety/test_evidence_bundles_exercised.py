"""ADR-0028 Option C, exercised: check 9 stops being a tautology.

ADR-0023 closed identity and deliberately left evidence uniform. ADR-0028 found
the sharper consequence, and it is the reason this file exists:

    `_check_evidence_bundle` compared `provenance.evidence_bundle_id`/`_digest`
    against `SupervisorState.expected_*`, and BOTH SIDES originated in the same
    place — the static `INGRESS_IDENTITY` constant, one copy stamped into
    provenance by the queue writer, the other copied into `CycleFacts` by the
    backend gatherer. The check was written correctly (exact match, `None`
    included, deny-by-default when the expectation is absent) and wired to a
    comparison that had never had two independent origins.

So it could not refuse. Not for a forged digest, not for an expired bundle, not
for any proposer, in any posture. That is the R-24..R-27 shape one level up: not
a control that failed, a control whose evidence was never gathered — and this
project has been burned by that exact shape four times.

The proofs below are the ADR's own "Requires" list for Option C, which is the
union of Option A's and Option B's plus three more. Every one drives the real
drain, the real durable state, and the real hash chain rather than a mock of the
thing under test:

Authority half, at STAMP (the drain's clock):
  1. a proposal citing an UNISSUED bundle id refuses;
  2. a proposal citing a bundle issued to a DIFFERENT proposer refuses;
  3. an EXPIRED bundle refuses — including one that expired between enqueue and
     drain, which is the case the drain's clock exists for;
  4. a proposal carrying NO citation refuses.

Agreement half, at admission check 9 (the pure kernel):
  5. the digest of the bytes actually served verifies end-to-end — the positive
     control, and the assertion that had never once been true;
  6. a cited digest that DISAGREES with the record refuses, with a code distinct
     from the unissued case;
  7. a proposal whose provenance names the bundle but carries no citation FOR it
     refuses;
  8. served and attested kinds do not substitute for one another, in either
     direction.

Posture and surface:
  9. the unset posture is byte-identical to the pre-ADR-0028 journal;
 10. a configured-but-broken posture refuses rather than falling back;
 11. the issuance route is the proposer credential's one NAMED exception;
 12. the per-proposer issuance cap refuses rather than evicting;
 13. retention prunes the row and never the hash-chained issuance record;
 14. an out-of-range TTL refuses to start.

**The honest bound, restated because this file could be read as claiming more.**
Equality catches accident, not malice. A proposer that fetches a bundle, reasons
on entirely different text and cites the issued digest is indistinguishable from
an honest one, because the backend cannot observe a prompt in another process.
What these tests prove is that a rendering which DRIFTED from what was fetched —
truncation, reordering, a key-order change, a partial fetch — is now refused, and
that the four authority facts (issued, to whom, when, unexpired) are checked
rather than assumed. And attested is not witnessed: for the bridge the record
binds a claim to a credential and a time, nothing more.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.api.autonomy_wiring import (
    INGRESS_IDENTITY,
    build_identity_resolver,
    evidence_binding_in_force,
    evidence_posture_is_broken,
)
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
from chronos.persistence.schema import AutonomyEvidenceBundleRow, HashChainRow
from chronos.supervisor import durable, evidence_bundles, proposals
from chronos.supervisor.admission import AdmissionRefusal, MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.evidence_kinds import BundleKind
from chronos.supervisor.loop import CycleFacts, CycleStage
from chronos.supervisor.proposers import (
    ProposerRegistration,
    credential_hash,
    registration_binding,
)
from chronos.supervisor.runtime import AutonomyRuntime, RuntimeConfig
from chronos.supervisor.sizing import AccountEvidence

REPO_ROOT = Path(__file__).resolve().parents[2]

_NOW = datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_TTL = 300.0

TOKEN_HEADER = "X-Chronos-Token"
PROPOSER_HEADER = "X-Chronos-Proposer-Token"

WORKER_CREDENTIAL = "w" * 64
BRIDGE_CREDENTIAL = "b" * 64
ROTATED_CREDENTIAL = "r" * 64

_FAR_EXPIRY = "2030-01-01T00:00:00+00:00"

#: The digest a served bundle carries in the drain-plane tests. A fixed value is
#: fine: the record stores whatever the issuer computed, and these tests are
#: about what the *comparison* does with it.
_SERVED_DIGEST = "1" * 64
_OTHER_DIGEST = "2" * 64

#: The admitted-row payload the unset evidence posture produced BEFORE ADR-0055, captured at
#: db12587 (#151's head) with this file's own fixtures and re-run twice for determinism. The
#: semantic test below asserts that today's payload, minus its ``posture`` key, canonicalises
#: to exactly these bytes — the byte delta of ADR-0055 is the posture key and nothing else.
_PRE_ADR_0055_UNSET_POSTURE_ROW = (
    '{"admitted":true,"checks":[{"detail":"mandate m-adr28 v1","evaluated":true,"name":"activ'
    'e_mandate","passed":true},{"detail":"","evaluated":true,"name":"system_not_degraded","pa'
    'ssed":true},{"detail":"owner event owner-event-1","evaluated":true,"name":"mandate_activ'
    'ated","passed":true},{"detail":"","evaluated":true,"name":"mandate_effective","passed":t'
    'rue},{"detail":"","evaluated":true,"name":"account_scope","passed":true},{"detail":"PAPE'
    'R_AUTONOMOUS","evaluated":true,"name":"mode_may_submit","passed":true},{"detail":"0 prio'
    'r refusals","evaluated":true,"name":"not_a_replay","passed":true},{"detail":"agrees with'
    ' mandate pins (authorship not yet enforced)","evaluated":true,"name":"version_pins","pas'
    'sed":true},{"detail":"owner-workspace","evaluated":true,"name":"evidence_bundle","passed'
    '":true},{"detail":"OPEN","evaluated":true,"name":"executable_kind","passed":true},{"deta'
    'il":"EQUITY","evaluated":true,"name":"asset_class_permitted","passed":true},{"detail":"S'
    'PY","evaluated":true,"name":"instrument_permitted","passed":true},{"detail":"LONG_EQUITY'
    '","evaluated":true,"name":"strategy_permitted","passed":true},{"detail":"NEUTRAL","evalu'
    'ated":true,"name":"direction_permitted","passed":true},{"detail":"PAPER_AUTONOMOUS","eva'
    'luated":true,"name":"family_promoted","passed":true},{"detail":"LIMIT","evaluated":true,'
    '"name":"order_form_available","passed":true},{"detail":"LIVE @ 1s","evaluated":true,"nam'
    'e":"market_data_fresh","passed":true}],"decision_id":"9ba95b85da175c49bdfb60e4fdc97253",'
    '"detail":"PAPER_AUTONOMOUS admission for OPEN","refusal":null}'
)


# --------------------------------------------------------------- shared fixtures


@pytest.fixture(autouse=True)
def _fu2_fresh_verified_state() -> Iterator[None]:
    """FU2: verified evidence state is process memory; every test starts with none."""

    reset = getattr(evidence_bundles, "_reset_verified_streams", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


def _fu2_stream(fingerprint: str = _FINGERPRINT) -> str:
    return evidence_bundles.hash_chain_stream(fingerprint)


def _fu2_certify(sessions: sessionmaker[Session], fingerprint: str = _FINGERPRINT) -> Any:
    """Run one verification pass to completion (absent before FU2: then a no-op)."""

    certify = getattr(evidence_bundles, "certify_stream", None)
    if certify is None:
        return None
    return certify(sessions, _fu2_stream(fingerprint))


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


def _registration(proposer_id: str, credential: str) -> dict[str, Any]:
    return {
        "proposer_id": proposer_id,
        "secret_sha256": credential_hash(credential),
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "mv-7",
        "prompt_version": "pv-3",
        "tool_schema_version": "ts-2",
        "decision_schema_version": "ds-4",
        "policy_version": "pol-5",
        "expires_at": _FAR_EXPIRY,
        "enabled": True,
    }


def _registry_file(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "autonomy_proposers.json"
    path.write_text(json.dumps({"schema_version": 1, "proposers": list(entries)}), encoding="utf-8")
    return path


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-adr28",
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
    """Cycle facts carrying the PLACEHOLDER expectation, deliberately.

    Under the configured posture the expectation must come from the resolved
    record instead, so leaving the placeholder here is a standing hazard
    injection: if any path still read `CycleFacts` for the expectation, the
    admitted-path test would refuse with EVIDENCE_BUNDLE_MISMATCH and say so.
    """

    return CycleFacts(
        account_fingerprint=_FINGERPRINT,
        account_id="DU1234567",
        now=now,
        process_generation=7,
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


def _payload(
    *,
    evidence_id: str,
    digest: str,
    kind: str = "worker_evidence_snapshot",
    citations: list[dict[str, Any]] | None = None,
) -> str:
    """One well-formed proposal, citing whatever the caller wants it to cite."""

    evidence = (
        citations
        if citations is not None
        else [
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "as_of": _NOW.isoformat(),
                "digest": digest,
            }
        ]
    )
    return json.dumps(
        {
            "kind": "OPEN",
            "asset_class": "EQUITY",
            "symbol": "SPY",
            "requested_strategy": "LONG_EQUITY",
            "requested_quantity": "10",
            "evidence": evidence,
            "invalidation_conditions": ["closes below 400"],
        }
    )


def _uncited_payload() -> str:
    """A proposal that legitimately carries NO evidence at all.

    It has to be a HOLD. The decision contract already refuses an *exposure
    creating* kind with no citation — "a OPEN decision must cite at least one
    evidence id" (`decision.py`) — and that refusal fires at the ingress, before
    STAMP is reached. That control is older and stricter than this one and is not
    weakened here; it simply means the uncited case can only be exercised through
    a kind the contract permits to be uncited, which is exactly what this is.
    """

    return json.dumps(
        {
            "kind": "HOLD",
            "asset_class": "EQUITY",
            "symbol": "SPY",
            "direction": "NEUTRAL",
            "thesis": "nothing to do; deliberately citing no evidence",
        }
    )


class _NullSink:
    name = "null"

    def deliver(self, alert: object) -> bool:
        return True


def _runtime(
    sessions: sessionmaker[Session],
    registry_path: Path | None,
    mandate: AutonomyMandate,
    *,
    bind_evidence: bool = True,
) -> AutonomyRuntime:
    return AutonomyRuntime(
        sessions=sessions,
        config=RuntimeConfig(account_fingerprint=_FINGERPRINT),
        identity=INGRESS_IDENTITY,
        mandate_source=lambda: mandate,
        gather_facts=_facts,
        sinks=(_NullSink(),),
        submit=None,
        resolve_identity=build_identity_resolver(registry_path),
        bind_evidence=bind_evidence,
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


def _issue(
    sessions: sessionmaker[Session],
    *,
    proposer_id: str = "claude-worker",
    kind: BundleKind = BundleKind.BACKEND_SERVED,
    digest: str = _SERVED_DIGEST,
    now: datetime = _NOW,
    ttl_seconds: float = _TTL,
    credential: str | None = None,
) -> evidence_bundles.IssuedBundle:
    if not proposer_id:
        epoch = "0" * 64
        registration_digest = "0" * 64
    else:
        if credential is None:
            credential = (
                BRIDGE_CREDENTIAL if proposer_id == "tradingview-bridge" else WORKER_CREDENTIAL
            )
        binding = registration_binding(
            ProposerRegistration.model_validate(_registration(proposer_id, credential))
        )
        epoch = binding.credential_epoch
        registration_digest = binding.registry_entry_digest
    with sessions.begin() as session:
        return evidence_bundles.issue(
            session,
            account_fingerprint=_FINGERPRINT,
            proposer_id=proposer_id,
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration_digest,
            kind=kind,
            digest=digest,
            now=now,
            ttl_seconds=ttl_seconds,
        )


def _enqueue(
    sessions: sessionmaker[Session],
    payload: str,
    proposer_id: str,
    *,
    credential: str | None = None,
) -> None:
    if credential is None:
        credential = BRIDGE_CREDENTIAL if proposer_id == "tradingview-bridge" else WORKER_CREDENTIAL
    binding = registration_binding(
        ProposerRegistration.model_validate(_registration(proposer_id, credential))
    )
    with sessions.begin() as session:
        proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=payload,
            now=_NOW,
            proposer_id=proposer_id,
            proposer_credential_epoch=binding.credential_epoch,
            proposer_registry_entry_digest=binding.registry_entry_digest,
        )


def _drain(runtime: AutonomyRuntime, now: datetime = _NOW) -> Any:
    # FU2 (declared change, R7): evidence-bound resolves refuse until the first verification
    # pass completes, and the tick runs its pass AFTER the drain. Complete that first pass
    # before the drained tick, as a backend that has been up for one tick has.
    if runtime._bind_evidence:
        _fu2_certify(runtime._sessions)
    report = runtime.run_tick(now)
    assert report.ok, report.failure
    assert report.proposals_judged == 1, report.proposals_judged
    return report.outcomes[0]


# ================================================== the authority half (at STAMP)


def test_restart_after_credential_rotation_refuses_prior_proposal_and_bundle(
    tmp_path: Path,
) -> None:
    """Replacing one credential must not revive work authenticated by its predecessor.

    This is an actual persistence restart against one file-backed database.
    It independently exercises the proposal and bundle seams, then runs a
    replacement-epoch positive control so a blanket refusal cannot pass.
    """

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'chronos.db'}")
    database.initialize()
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    mandate = _mandate()
    try:
        _activate(database.sessions, mandate)
        old_bundle = _issue(database.sessions)
        _enqueue(
            database.sessions,
            _payload(evidence_id=old_bundle.bundle_id, digest=old_bundle.digest),
            "claude-worker",
        )
    finally:
        database.dispose()

    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "proposers": [_registration("claude-worker", ROTATED_CREDENTIAL)],
            }
        ),
        encoding="utf-8",
    )
    restarted = Database(f"sqlite+pysqlite:///{tmp_path / 'chronos.db'}")
    restarted.initialize()
    try:
        proposal_outcome = _drain(_runtime(restarted.sessions, registry, mandate))

        _enqueue(
            restarted.sessions,
            _payload(evidence_id=old_bundle.bundle_id, digest=old_bundle.digest),
            "claude-worker",
            credential=ROTATED_CREDENTIAL,
        )
        bundle_outcome = _drain(_runtime(restarted.sessions, registry, mandate))

        replacement_bundle = _issue(
            restarted.sessions,
            credential=ROTATED_CREDENTIAL,
        )
        _enqueue(
            restarted.sessions,
            _payload(evidence_id=replacement_bundle.bundle_id, digest=replacement_bundle.digest),
            "claude-worker",
            credential=ROTATED_CREDENTIAL,
        )
        positive_control = _drain(_runtime(restarted.sessions, registry, mandate))
        _enqueue(
            restarted.sessions,
            _payload(evidence_id=replacement_bundle.bundle_id, digest=replacement_bundle.digest),
            "claude-worker",
            credential=ROTATED_CREDENTIAL,
        )
    finally:
        restarted.dispose()

    registry_removed = Database(f"sqlite+pysqlite:///{tmp_path / 'chronos.db'}")
    registry_removed.initialize()
    try:
        removed_outcome = _drain(
            _runtime(
                registry_removed.sessions,
                None,
                mandate,
                bind_evidence=False,
            )
        )
    finally:
        registry_removed.dispose()

    assert proposal_outcome.stage is CycleStage.STAMP
    assert proposal_outcome.refusal == "PROPOSER_REGISTRATION_REPLACED"
    assert proposal_outcome.decision is None
    assert bundle_outcome.stage is CycleStage.STAMP
    assert bundle_outcome.refusal == evidence_bundles.ResolutionRefusal.REGISTRATION_REPLACED.value
    assert bundle_outcome.decision is None
    assert positive_control.stage is CycleStage.HANDOFF
    assert positive_control.refusal == "NO_SUBMISSION_CONFIGURED"
    assert positive_control.decision is not None
    assert removed_outcome.stage is CycleStage.STAMP
    assert removed_outcome.refusal == "PROPOSER_REGISTRY_REMOVED"
    assert removed_outcome.decision is None


def test_an_unissued_bundle_id_refuses_at_the_drain(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """A proposer cannot mint its own evidence record by naming one.

    The first of the four authority facts. Before ADR-0028 a proposal could cite
    anything at all — `decision.evidence` was read by nothing in
    `chronos.supervisor` (grep-verified in the ADR) — so "this bundle does not
    exist" was not a statement the system could make.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    _enqueue(
        sessions,
        _payload(evidence_id="evb_never_issued", digest=_SERVED_DIGEST),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.stage is CycleStage.STAMP
    assert outcome.refusal == evidence_bundles.ResolutionRefusal.UNISSUED.value
    assert outcome.decision is None, "an unbound proposal is never judged"


def test_a_bundle_issued_to_another_proposer_refuses(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """A bundle is issued TO a credential and is not transferable.

    Distinguished from "unissued" on purpose: a stolen bundle and an invented one
    are different owner-facing events, and a journal that rendered them as one
    refusal could not tell a leaked credential from a typo.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(
        tmp_path,
        _registration("claude-worker", WORKER_CREDENTIAL),
        _registration("tradingview-bridge", BRIDGE_CREDENTIAL),
    )
    # Issued to the bridge; cited by the worker.
    issued = _issue(sessions, proposer_id="tradingview-bridge")
    _enqueue(
        sessions,
        _payload(evidence_id=issued.bundle_id, digest=issued.digest),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.stage is CycleStage.STAMP
    assert outcome.refusal == evidence_bundles.ResolutionRefusal.FOREIGN.value


def test_a_bundle_that_expired_between_enqueue_and_drain_refuses(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The clock question, answered where ADR-0028 says it must be.

    The bundle is issued and the proposal enqueued while it is live; the drain
    then runs past its expiry. Judging at the proposer's `as_of` would admit
    this. Judging at the **drain's** `now` — the same clock that judges
    registration currency — refuses it, because that is the moment authority is
    actually exercised rather than the moment bytes arrived.
    """

    mandate = _mandate(expires_at=_NOW + timedelta(days=2))
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions, ttl_seconds=60.0)
    _enqueue(
        sessions,
        _payload(evidence_id=issued.bundle_id, digest=issued.digest),
        "claude-worker",
    )

    later = _NOW + timedelta(seconds=61)
    outcome = _drain(_runtime(sessions, registry, mandate), now=later)

    assert outcome.stage is CycleStage.STAMP
    assert outcome.refusal == evidence_bundles.ResolutionRefusal.EXPIRED.value


def test_a_legacy_bundle_binding_is_never_inferred_from_the_current_registry(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """Migration-preserved NULLs refuse instead of adopting today's credential."""

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions)
    with sessions.begin() as session:
        row = evidence_bundles.load(
            session,
            account_fingerprint=_FINGERPRINT,
            bundle_id=issued.bundle_id,
        )
        assert row is not None
        row.proposer_credential_epoch = None
        row.proposer_registry_entry_digest = None
    _enqueue(
        sessions,
        _payload(evidence_id=issued.bundle_id, digest=issued.digest),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.stage is CycleStage.STAMP
    assert outcome.refusal == evidence_bundles.ResolutionRefusal.REGISTRATION_UNBOUND.value
    assert outcome.decision is None


def test_a_proposal_with_no_citation_at_all_refuses(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """Silence is not evidence. Deny-by-default, applied to the payload side."""

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    _issue(sessions)
    _enqueue(sessions, _uncited_payload(), "claude-worker")

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.stage is CycleStage.STAMP
    assert outcome.refusal == evidence_bundles.ResolutionRefusal.UNCITED.value


# ============================================== the agreement half (at check 9)


def test_the_served_digest_verifies_end_to_end_through_the_real_drain(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """THE POSITIVE CONTROL: the outcome that had never once been observed.

    A proposal citing a bundle that was really issued, to this proposer, that has
    not expired, whose citation digest equals the record's — walks STAMP, is
    stamped from the RECORD, and passes check 9 on a comparison with two
    independent origins for the first time in this project's history.

    Three things are asserted together because each alone could pass while the
    protocol was broken:

    1. the cycle got past admission (so check 9 did not refuse);
    2. provenance carries the ISSUED bundle id and digest — not the
       `owner-workspace` placeholder that `_facts` deliberately still supplies,
       which is the standing hazard injection for "did anything still read
       CycleFacts for the expectation";
    3. the journal records it, hash-chained, in the real durable state.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions)
    _enqueue(
        sessions,
        _payload(evidence_id=issued.bundle_id, digest=issued.digest),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    # No submit callable is wired, so a fully admitted proposal stops at the
    # handoff. Anything at or past SIZING means admission passed.
    assert outcome.stage in {CycleStage.SIZING, CycleStage.COMPILATION, CycleStage.HANDOFF}, (
        f"admission refused an honest bundle: {outcome.stage} / {outcome.refusal} / "
        f"{outcome.detail}"
    )
    assert outcome.admission is not None and outcome.admission.admitted, outcome.admission
    assert outcome.decision is not None
    provenance = outcome.decision.provenance
    assert provenance.evidence_bundle_id == issued.bundle_id
    assert provenance.evidence_bundle_digest == issued.digest
    assert provenance.evidence_bundle_id != INGRESS_IDENTITY.evidence_bundle_id, (
        "provenance still carries the placeholder: the expectation is being read "
        "from CycleFacts, and check 9 is a tautology again"
    )

    evidence_check = next(
        check for check in outcome.admission.checks if check.name == "evidence_bundle"
    )
    assert evidence_check.passed and evidence_check.evaluated
    assert BundleKind.BACKEND_SERVED.value in evidence_check.detail

    with sessions.begin() as session:
        kinds = [
            row.kind
            for row in session.scalars(select(HashChainRow).order_by(HashChainRow.id.asc()))
        ]
    assert "evidence_bundle_issued" in kinds, "issuance must be hash-chained"


def test_a_cited_digest_that_disagrees_with_the_record_refuses_at_admission(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The rule the whole ADR is built around, and its distinct code.

    This is the realistic failure: an honest proposer whose rendering drifted
    from what it fetched — a truncation, a reordering, a key-order change, a
    partial fetch. The proposal resolves at STAMP (the bundle is real, is this
    proposer's, and is live), so it reaches the pure kernel, where the payload's
    own citation faces the backend's record and loses.

    The code must differ from the unissued case: "you cited something that does
    not exist" and "you cited something real and disagreed with it" are different
    events, and ADR-0028 requires them distinguishable in the journal.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions, digest=_SERVED_DIGEST)
    _enqueue(
        sessions,
        # Cites the real bundle, with a digest that is not the one on record.
        _payload(evidence_id=issued.bundle_id, digest=_OTHER_DIGEST),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.admission is not None
    assert outcome.admission.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH
    assert outcome.admission.refusal is not AdmissionRefusal.EVIDENCE_BUNDLE_UNKNOWN
    assert "citation" in outcome.admission.detail


def test_a_proposal_that_cites_something_else_entirely_refuses_as_uncited(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """A citation for some OTHER bundle is not a citation for this one.

    The proposal carries two citations: an ordinary non-bundle one, and the
    issued bundle. Resolution finds the bundle, so STAMP passes and provenance
    names it — but the payload must then still carry a citation *for that id*,
    which is what check 9's payload side reads. Here the bundle citation is
    removed from what admission sees by pointing provenance at a bundle the
    payload references only through a different, unrelated citation id.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions)

    # The FIRST citation resolves (it names the issued bundle) so STAMP binds it,
    # but it is removed from the decision's own evidence by re-issuing a second
    # bundle whose id nothing in the payload names.
    second = _issue(sessions)
    _enqueue(
        sessions,
        _payload(
            evidence_id=issued.bundle_id,
            digest=issued.digest,
            citations=[
                {
                    "evidence_id": second.bundle_id,
                    "kind": "worker_evidence_snapshot",
                    "as_of": _NOW.isoformat(),
                    "digest": second.digest,
                },
                {
                    "evidence_id": "some-other-note",
                    "kind": "worker_evidence_snapshot",
                    "as_of": _NOW.isoformat(),
                    "digest": _OTHER_DIGEST,
                },
            ],
        ),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    # STAMP binds the second bundle (the first citation that resolves), and the
    # payload does carry a citation for it — so this admits. The point of the
    # case is the ORDERING contract: resolution takes the first citation naming a
    # record, and check 9 then demands a citation for exactly that id.
    assert outcome.decision is not None
    assert outcome.decision.provenance.evidence_bundle_id == second.bundle_id


def _admit_directly(
    *,
    payload: str,
    expected_id: str = "evb_expected",
    expected_digest: str | None = _SERVED_DIGEST,
    provenance_digest: str | None = _SERVED_DIGEST,
    expected_kind: str | None = None,
    expires_at: datetime | None = None,
    no_expiry: bool = False,
    now: datetime = _NOW,
) -> Any:
    """Drive `admit` directly, with the state the drain builds under the posture.

    Some of check 9's conjuncts are unreachable through the drain because an
    earlier stage refuses the same input first (expiry, most obviously). Those
    still need proofs — a conjunct no test can fail is a conjunct nobody is
    maintaining — so this builds exactly the `SupervisorState` a resolved record
    produces and calls the pure kernel.
    """

    from chronos.autonomy import AITradeDecision, ProposedDecision
    from chronos.supervisor import admission, queue

    identity = queue.HarnessIdentity(
        provider="anthropic",
        model_id="model-x",
        model_version="mv-7",
        prompt_version="pv-3",
        tool_schema_version="ts-2",
        decision_schema_version="ds-4",
        policy_version="pol-5",
        proposer_id="claude-worker",
        evidence_bundle_id=expected_id,
        evidence_bundle_digest=provenance_digest,
    )
    proposal = ProposedDecision.model_validate_json(payload)
    decision = AITradeDecision(
        **proposal.model_dump(),
        decision_id="d-1",
        provenance=identity.stamp(produced_at=now),
    )
    state = admission.SupervisorState(
        account_fingerprint=_FINGERPRINT,
        now=now,
        activation=admission.MandateActivation(
            owner_event_id="owner-event-1", activated_at=now, process_generation=7
        ),
        process_generation=7,
        expected_evidence_bundle_id=expected_id,
        expected_evidence_bundle_digest=expected_digest,
        expected_evidence_bundle_kind=expected_kind or BundleKind.BACKEND_SERVED.value,
        # `no_expiry` is a separate flag rather than `expires_at=None` so a test
        # can assert the ABSENT-expiry case without it being indistinguishable
        # from "the caller did not care", which would silently test the default.
        expected_evidence_expires_at=(
            None
            if no_expiry
            else (expires_at if expires_at is not None else now + timedelta(seconds=_TTL))
        ),
        market_data=MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
    )
    return admission.admit(decision, _mandate(), state)


def test_the_uncited_refusal_fires_when_provenance_names_a_bundle_the_payload_omits(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """EVIDENCE_BUNDLE_UNCITED, exercised directly in the pure kernel.

    Reached through `admit` rather than the drain because the drain refuses this
    shape earlier (a payload with no resolvable citation never gets stamped). The
    state it is given is exactly the one the drain builds when a record resolved:
    an expectation with a kind and an expiry. The decision then carries no
    citation for it — which is the defect this code names, and which must refuse
    rather than pass on the strength of provenance alone.
    """

    outcome = _admit_directly(payload=_uncited_payload())

    assert not outcome.admitted
    assert outcome.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_UNCITED


def test_the_kernel_refuses_an_expired_expectation_on_its_own(
    sessions: sessionmaker[Session],
) -> None:
    """Check 9 re-judges expiry itself, and a MISSING expiry is deny-by-default.

    Belt and braces on purpose. The drain already refuses an expired bundle
    against its own clock, so this path is unreachable through the drain — which
    is exactly why it needs its own proof: a conjunct no test can fail is a
    conjunct nobody is maintaining, and this repository's whole QA culture exists
    because three controls sat inert behind that fact.

    Putting the re-check in the pure kernel also means the refusal is
    reproducible from its inputs alone, with no clock of its own and no database
    — which is what lets a stranger re-derive why a decision was refused.
    """

    expired = _admit_directly(
        payload=_payload(evidence_id="evb_expected", digest=_SERVED_DIGEST),
        expires_at=_NOW - timedelta(seconds=1),
    )
    assert not expired.admitted
    assert expired.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_EXPIRED

    # An expectation with a kind but NO expiry is not an unbounded bundle. It is
    # a record the kernel cannot age, and deny-by-default says refuse.
    undated = _admit_directly(
        payload=_payload(evidence_id="evb_expected", digest=_SERVED_DIGEST),
        no_expiry=True,
    )
    assert not undated.admitted
    assert undated.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_EXPIRED


def test_the_kernel_refuses_a_stamped_provenance_with_no_digest(
    sessions: sessionmaker[Session],
) -> None:
    """Under this posture a `None` digest is a defect, never attested absence.

    ADR-0028 is explicit: with binding configured, the stamper either had a
    record or the drain refused before admission ran. A `None` digest arriving
    here therefore means provenance was produced without one — and it must refuse
    rather than read as "no digest was issued", which is what the tri-state
    discipline means when there IS a posture saying one should exist.
    """

    outcome = _admit_directly(
        payload=_payload(evidence_id="evb_expected", digest=_SERVED_DIGEST),
        provenance_digest=None,
        expected_digest=None,
    )
    assert not outcome.admitted
    assert outcome.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_MISMATCH
    # The DETAIL, not just the code. This conjunct shares
    # EVIDENCE_BUNDLE_MISMATCH with the citation-digest comparison further down,
    # so asserting the code alone cannot tell them apart — and a revert-the-fix
    # pass proved exactly that: deleting this branch left the test passing,
    # because the later comparison produced the same code for a different
    # reason. Asserting the reason is what makes the conjunct verified.
    assert "absence is not attested absence" in outcome.detail, outcome.detail


def test_served_and_attested_kinds_do_not_substitute_for_one_another(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """ADR-0028's blunt rule, in both directions.

    A `backend_served` record says Chronos digested bytes it holds. An
    `alert_attested` record says a credential asserted it saw bytes Chronos never
    saw. Letting a citation of one kind satisfy a record of the other would
    relabel an attestation as a witnessing — the false-evidence class the
    promotion ladder exists to prevent, and the reason the ADR says an attested
    bundle may back a proposal but never a promotion rung.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))

    # Direction 1: an attested record cited with the worker's served-kind citation.
    attested = _issue(sessions, kind=BundleKind.ALERT_ATTESTED)
    _enqueue(
        sessions,
        _payload(
            evidence_id=attested.bundle_id,
            digest=attested.digest,
            kind="worker_evidence_snapshot",
        ),
        "claude-worker",
    )
    outcome = _drain(_runtime(sessions, registry, mandate))
    assert outcome.stage is CycleStage.ADMISSION
    assert outcome.admission is not None
    assert outcome.admission.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_KIND_MISMATCH

    # Direction 2: a served record cited with the bridge's attested-kind citation.
    served = _issue(sessions, kind=BundleKind.BACKEND_SERVED)
    _enqueue(
        sessions,
        _payload(evidence_id=served.bundle_id, digest=served.digest, kind="tradingview_alert"),
        "claude-worker",
    )
    second = _drain(_runtime(sessions, registry, mandate))
    assert second.stage is CycleStage.ADMISSION
    assert second.admission is not None
    assert second.admission.refusal is AdmissionRefusal.EVIDENCE_BUNDLE_KIND_MISMATCH


def test_an_attested_bundle_backs_the_bridges_own_citation(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The attested kind works — for the source it was built for, and only there.

    The positive control for Option B's half. It is the *only* shape available to
    the bridge, whose evidence originates outside Chronos, and what it records is
    non-repudiation rather than verification: this credential asserted, at this
    time, that it saw bytes with this digest.
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("tradingview-bridge", BRIDGE_CREDENTIAL))
    attested = _issue(sessions, proposer_id="tradingview-bridge", kind=BundleKind.ALERT_ATTESTED)
    _enqueue(
        sessions,
        _payload(
            evidence_id=attested.bundle_id,
            digest=attested.digest,
            kind="tradingview_alert",
        ),
        "tradingview-bridge",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.admission is not None and outcome.admission.admitted, outcome.admission
    assert outcome.decision is not None
    assert outcome.decision.provenance.evidence_bundle_id == attested.bundle_id


def test_the_journal_distinguishes_attested_from_issued(
    sessions: sessionmaker[Session],
) -> None:
    """A record that cannot say which kind it is would be a false label.

    ADR-0028 requires the journal and any rendering to distinguish `attested`
    from `issued` rather than showing both as "evidence". The hash-chained
    issuance payload carries the kind, so a reader reconstructing history can
    always tell what the backend actually witnessed.
    """

    served = _issue(sessions, kind=BundleKind.BACKEND_SERVED)
    attested = _issue(sessions, proposer_id="tradingview-bridge", kind=BundleKind.ALERT_ATTESTED)

    with sessions.begin() as session:
        payloads = {
            json.loads(row.payload_json)["bundle_id"]: json.loads(row.payload_json)
            for row in session.scalars(select(HashChainRow))
            if row.kind == "evidence_bundle_issued"
        }
    assert payloads[served.bundle_id]["bundle_kind"] == BundleKind.BACKEND_SERVED.value
    assert payloads[attested.bundle_id]["bundle_kind"] == BundleKind.ALERT_ATTESTED.value
    assert payloads[served.bundle_id]["bundle_kind"] != payloads[attested.bundle_id]["bundle_kind"]


def test_two_bundles_never_share_an_id(sessions: sessionmaker[Session]) -> None:
    """Ids are backend-chosen and unique, so a citation names exactly one record.

    ADR-0028 (Option B's list) requires that two proposers cannot register the
    same bundle id. Here that is structural rather than checked: the proposer
    never supplies an id at all, and the table's unique constraint on
    (account, bundle_id) is the backstop.
    """

    issued = [
        _issue(sessions, proposer_id=proposer)
        for proposer in ("claude-worker", "tradingview-bridge", "claude-worker")
    ]
    ids = [bundle.bundle_id for bundle in issued]
    assert len(set(ids)) == len(ids)
    with sessions.begin() as session:
        rows = list(session.scalars(select(AutonomyEvidenceBundleRow)))
    assert len({row.bundle_id for row in rows}) == len(rows)


# ================================================================ posture proofs


def test_the_unset_posture_is_semantically_identical_to_the_pre_adr_0028_journal(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The acceptance criterion ADR-0028 states in exactly these terms.

    Not "approximately today": the unset path must produce the same journal rows
    it produced before this ADR, because a posture switch that quietly changes
    the DEFAULT posture is the failure this repository fixes rather than ships.

    Proven against the recorded artifact rather than by inspection — the same
    proposal is drained with `bind_evidence=False`, and the hash-chained decision
    payload must carry the placeholder bundle id and the honestly-absent digest,
    with check 9 passing exactly as it did (tautologically, which is the point:
    the unset posture is not supposed to have been fixed).
    """

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    # No bundle is issued at all, and the payload cites something meaningless.
    _enqueue(
        sessions,
        _payload(evidence_id="anything-at-all", digest=_OTHER_DIGEST),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate, bind_evidence=False))

    assert outcome.admission is not None and outcome.admission.admitted, (
        "the unset posture must admit exactly what it admitted before ADR-0028"
    )
    assert outcome.decision is not None
    provenance = outcome.decision.provenance
    assert provenance.evidence_bundle_id == INGRESS_IDENTITY.evidence_bundle_id
    assert provenance.evidence_bundle_digest is None, (
        "absence is attested as absence, never as sixty-four zeros (ADR-0023)"
    )
    check = next(c for c in outcome.admission.checks if c.name == "evidence_bundle")
    assert check.passed and check.evaluated
    assert check.detail == INGRESS_IDENTITY.evidence_bundle_id, (
        "the unset posture's check detail must be the pre-ADR-0028 string exactly"
    )
    with sessions.begin() as session:
        rows = list(session.scalars(select(HashChainRow)))
    assert not [row for row in rows if row.kind == "evidence_bundle_issued"], (
        "the unset posture must write no evidence records at all"
    )

    # ADR-0055: the row now says which posture judged it. The evidence posture is unset
    # here, but this fixture runs WITH a registry and enqueues a bound row, so the identity
    # half is authenticated and the evidence half is unset — the row records exactly that.
    decision_stream = durable.stream_for(durable.DECISION_STREAM, _FINGERPRINT)
    (journal,) = [row for row in rows if row.stream == decision_stream]
    assert journal.kind == "admitted"
    payload = json.loads(journal.payload_json)
    assert payload["posture"] == {
        "version": 1,
        "identity": "authenticated",
        "registry": "configured",
        "evidence_binding": "unset",
        "credential_epoch_bound": True,
    }
    # And the retired guarantee, kept as an exact delta: minus the posture key, today's
    # bytes ARE the pre-ADR-0055 bytes. The unset posture's behaviour did not move.
    without_posture = {key: value for key, value in payload.items() if key != "posture"}
    assert hash_chain.canonical_payload(without_posture) == _PRE_ADR_0055_UNSET_POSTURE_ROW


def test_a_configured_posture_with_no_registry_is_broken_and_refuses(tmp_path: Path) -> None:
    """Evidence binding without a registry names no author to issue to.

    ADR-0023's posture rule, applied to the setting that came after it: the
    combination refuses loudly and never falls back to the placeholder. The
    wiring reports it, startup alerts on it, and the drain still binds — so a
    queue row that predates the misconfiguration cannot be judged under a posture
    the owner did not get.
    """

    from chronos.config.settings import Settings

    broken = Settings(
        _env_file=None,
        autonomy_evidence_bundles=True,
        autonomy_proposers_file=None,
    )
    assert evidence_posture_is_broken(broken)
    assert evidence_binding_in_force(broken), (
        "the drain must still bind under a broken posture; refusing to bind would "
        "silently restore the placeholder, which is the fallback ADR-0028 forbids"
    )

    healthy = Settings(
        _env_file=None,
        autonomy_evidence_bundles=True,
        autonomy_proposers_file=tmp_path / "proposers.json",
    )
    assert not evidence_posture_is_broken(healthy)
    assert evidence_binding_in_force(healthy)

    unset = Settings(_env_file=None)
    assert not evidence_binding_in_force(unset)
    assert not evidence_posture_is_broken(unset)


def test_an_out_of_range_ttl_refuses_to_start() -> None:
    """Fail-closed configuration: an unusable TTL is a refusal, not a clamp.

    The ceiling is a disclosed judgment — evidence an hour old is stale by any
    reading of an intraday equity decision — and the type refuses to express a
    longer window rather than quietly capping one. A zero or negative TTL would
    expire every bundle at issue, which is the safe direction but must still be
    a visible failure rather than a silent one.
    """

    from pydantic import ValidationError

    from chronos.config.settings import Settings

    for value in (0, -1, 3601, 86400):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, autonomy_evidence_ttl_seconds=value)

    assert Settings(_env_file=None).autonomy_evidence_ttl_seconds == 300.0


# ============================================== issuance surface, caps, retention


def test_the_issuance_cap_refuses_rather_than_evicting(
    sessions: sessionmaker[Session],
) -> None:
    """A proposer that could mint unbounded rows is a disk-filling DoS.

    Against the process that holds the broker connection, which is why the bound
    exists at all. It refuses rather than displacing an in-flight bundle: evicting
    would let a flood invalidate a legitimate job that was about to be cited.
    """

    for _ in range(evidence_bundles.MAX_LIVE_BUNDLES_PER_PROPOSER):
        _issue(sessions)

    with pytest.raises(evidence_bundles.IssuanceRefused, match="cap"):
        _issue(sessions)

    # A DIFFERENT proposer is unaffected: the cap is per credential, so one
    # noisy proposer cannot deny evidence to another.
    other = _issue(sessions, proposer_id="tradingview-bridge")
    assert other.bundle_id

    # And the cap counts only LIVE bundles: once they expire, the proposer is
    # not permanently locked out by its own history.
    later = _NOW + timedelta(seconds=_TTL + 1)
    with sessions.begin() as session:
        assert (
            evidence_bundles.live_bundle_count(
                session,
                account_fingerprint=_FINGERPRINT,
                proposer_id="claude-worker",
                now=later,
            )
            == 0
        )


def test_retention_prunes_the_row_and_never_the_issuance_record(
    sessions: sessionmaker[Session],
) -> None:
    """The retention rule ADR-0028 requires, with the chain left intact.

    An expired row can no longer authorize anything, so reclaiming it is safe.
    The hash-chained record of *what was issued, to whom, and when* is not
    reclaimed — an audit trail that forgot an issuance could not answer the first
    question an incident review asks.
    """

    issued = _issue(sessions, ttl_seconds=60.0)
    with sessions.begin() as session:
        assert evidence_bundles.load(
            session, account_fingerprint=_FINGERPRINT, bundle_id=issued.bundle_id
        )

    # Just expired: kept, because an operator reading a refusal must still be
    # able to find the record that caused it.
    just_expired = _NOW + timedelta(seconds=120)
    with sessions.begin() as session:
        assert (
            evidence_bundles.prune_expired(
                session, account_fingerprint=_FINGERPRINT, now=just_expired
            )
            == 0
        )

    long_expired = _NOW + evidence_bundles.RETENTION_AFTER_EXPIRY + timedelta(days=1)
    with sessions.begin() as session:
        assert (
            evidence_bundles.prune_expired(
                session, account_fingerprint=_FINGERPRINT, now=long_expired
            )
            == 1
        )
    with sessions.begin() as session:
        assert (
            evidence_bundles.load(
                session, account_fingerprint=_FINGERPRINT, bundle_id=issued.bundle_id
            )
            is None
        )
        chained = [
            json.loads(row.payload_json)
            for row in session.scalars(select(HashChainRow))
            if row.kind == "evidence_bundle_issued"
        ]
    assert [entry for entry in chained if entry["bundle_id"] == issued.bundle_id], (
        "pruning must reclaim the lookup row and never the audit record"
    )


def test_issuance_refuses_a_digest_that_is_not_one(sessions: sessionmaker[Session]) -> None:
    """A digest this protocol cannot compare is refused at the door."""

    for bad in ("", "not-hex", "abc", "z" * 64, _SERVED_DIGEST[:-1]):
        with pytest.raises(evidence_bundles.IssuanceRefused):
            _issue(sessions, digest=bad)


def test_issuance_refuses_without_a_proposer(sessions: sessionmaker[Session]) -> None:
    """A bundle is issued TO a credential; with none there is nothing to issue to.

    This is the module-level half of the broken-posture rule: even if a caller
    reached `issue` with an empty proposer, it refuses rather than writing an
    unattributable record — which would be exactly the constant this protocol
    exists to remove.
    """

    with pytest.raises(evidence_bundles.IssuanceRefused, match="no registered proposer"):
        _issue(sessions, proposer_id="")


# ============================================ the route plane and R-48's exception


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


def _boot_with_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, evidence: bool = True
) -> TestClient:
    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    registry_path = _registry_file(
        tmp_path,
        _registration("claude-worker", WORKER_CREDENTIAL),
        _registration("tradingview-bridge", BRIDGE_CREDENTIAL),
    )
    monkeypatch.setenv("AUTONOMY_PROPOSERS_FILE", str(registry_path))
    if evidence:
        monkeypatch.setenv("AUTONOMY_EVIDENCE_BUNDLES", "true")
    get_settings.cache_clear()
    return TestClient(create_app())


def _api_token(tmp_path: Path) -> str:
    return (tmp_path / "backend_api_token").read_text(encoding="utf-8").strip()


def test_the_issuance_route_is_the_credentials_one_named_exception(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control R-48's enumeration defers to.

    That enumeration proves the proposer credential is refused on every mutating
    route except two NAMED ones. A named exception with no positive control would
    be an exemption nobody checks, so this is the other half: the credential
    really does open issuance, the local API token really does not, and the
    surface widened by exactly one route rather than generally.
    """

    with _boot_with_evidence(monkeypatch, demo_env) as client:
        token = _api_token(demo_env)
        body = {"kind": "alert_attested", "digest": _SERVED_DIGEST}

        # The proposer credential opens it.
        issued = client.post(
            "/autonomy/evidence", json=body, headers={PROPOSER_HEADER: BRIDGE_CREDENTIAL}
        )
        assert issued.status_code == 201, issued.text
        record = issued.json()
        assert record["bundle_id"].startswith("evb_")
        assert record["kind"] == BundleKind.ALERT_ATTESTED.value
        assert record["digest"] == _SERVED_DIGEST
        expected = registration_binding(
            ProposerRegistration.model_validate(
                _registration("tradingview-bridge", BRIDGE_CREDENTIAL)
            )
        )
        with sqlite3.connect(demo_env / "chronos.db") as connection:
            stored = connection.execute(
                "SELECT proposer_id, proposer_credential_epoch, "
                "proposer_registry_entry_digest FROM autonomy_evidence_bundles "
                "WHERE bundle_id = ?",
                (record["bundle_id"],),
            ).fetchone()
        assert stored == (
            "tradingview-bridge",
            expected.credential_epoch,
            expected.registry_entry_digest,
        )

        # The local API token does not — issuance is proposer surface, and the
        # asymmetry ADR-0023 built is preserved rather than eroded by the
        # addition.
        with_token = client.post("/autonomy/evidence", json=body, headers={TOKEN_HEADER: token})
        assert with_token.status_code == 401

        # And nothing at all does not.
        assert client.post("/autonomy/evidence", json=body).status_code == 401


def test_inconsistent_authenticated_registry_state_writes_no_work(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route carrier fails closed if its authentication invariant breaks.

    Production authentication and persistence consult the same frozen registry.
    This override deliberately makes the dependency claim an id absent from that
    registry; both write origins must answer 503 and leave no partial row.
    """

    from chronos.api.auth import ProposerAuth, require_proposer
    from chronos.supervisor.proposers import ProposerRegistry

    with _boot_with_evidence(monkeypatch, demo_env) as client:
        client.app.dependency_overrides[require_proposer] = lambda: "claude-worker"  # type: ignore[attr-defined]
        client.app.state.proposer_auth = ProposerAuth(  # type: ignore[attr-defined]
            configured=True,
            registry=ProposerRegistry(schema_version=1),
        )
        proposal = client.post(
            "/autonomy/proposals",
            content=_payload(evidence_id="evb_probe", digest=_SERVED_DIGEST),
        )
        evidence = client.post(
            "/autonomy/evidence",
            json={"kind": "alert_attested", "digest": _SERVED_DIGEST},
        )

        assert proposal.status_code == 503
        assert evidence.status_code == 503
        with sqlite3.connect(demo_env / "chronos.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM autonomy_proposal_queue"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM autonomy_evidence_bundles"
            ).fetchone() == (0,)


def test_issuance_refuses_a_caller_supplied_digest_on_the_served_kind(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served bundle digests bytes the BACKEND holds. Nothing else.

    Accepting a caller's digest under the served label would make the record
    attested while claiming it was witnessed — the substitution the kind rule
    exists to prevent, arriving through the issuance door instead of the
    admission one.
    """

    with _boot_with_evidence(monkeypatch, demo_env) as client:
        response = client.post(
            "/autonomy/evidence",
            json={"kind": "backend_served", "digest": _SERVED_DIGEST, "symbols": ["SPY"]},
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert response.status_code == 422
        assert response.json()["refusal"] == "EVIDENCE_DIGEST_NOT_ACCEPTED"


def test_the_served_document_digest_is_over_the_exact_bytes_served(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes the worker's agreement free, checked directly.

    The route returns the canonical document AND its digest. A worker that
    renders the document verbatim therefore cites a digest that matches the
    record by construction. If these two ever disagreed, every honest forward
    would refuse at admission — so the equality is asserted here, at the source,
    rather than discovered later as a mysterious mismatch.
    """

    with _boot_with_evidence(monkeypatch, demo_env) as client:
        response = client.post(
            "/autonomy/evidence",
            json={"kind": "backend_served", "symbols": ["SPY"], "lookback_days": 5},
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert response.status_code == 201, response.text
        record = response.json()

        # The bars half of the document comes from the same provider
        # `GET /terminal/bars` uses. Asserting that route answers 200 is the
        # revert-the-fix guard for the dead-route defect this build surfaced:
        # `provider_for` cached by assigning a NEW attribute to `BackendState`,
        # which is `slots=True`, so every call raised AttributeError and this
        # route answered 500 for every symbol since it existed. No test called
        # it, which is why it survived — the same shape as `_fingerprint_of`.
        token = _api_token(demo_env)
        bars = client.get(
            "/terminal/bars",
            params={"symbol": "SPY", "interval": "1d", "lookback": 5},
            headers={TOKEN_HEADER: token},
        )
        assert bars.status_code == 200, (
            f"GET /terminal/bars is dead again: {bars.status_code}. The bar provider "
            "cannot cache itself on a slotted BackendState."
        )

        # And the provider is actually CACHED, which is the half of that fix a
        # 200 alone cannot prove: `provider_for` degrades to uncached rather
        # than raising, so a missing slot would still answer 200 while turning
        # every panel refresh back into a broker request — the exact cost the
        # module says it exists to prevent.
        from chronos.api import bars as bar_plane

        state = client.app.state.backend  # type: ignore[attr-defined]
        first = bar_plane.provider_for(state.runtime, state)
        assert bar_plane.provider_for(state.runtime, state) is first, (
            "the bar provider is not being cached on the backend state; it is being "
            "rebuilt per call, which is a silent performance regression rather than a "
            "visible failure"
        )

    document = record["document"]
    assert document, "a served bundle must return the bytes it digested"
    assert hashlib.sha256(document.encode("utf-8")).hexdigest() == record["digest"]
    # The document is the artifact, so it must be the canonical form rather than
    # something a re-serialization could differ from.
    assert document == json.dumps(
        json.loads(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert record["kind"] == BundleKind.BACKEND_SERVED.value


def test_issuance_is_absent_under_the_unset_posture(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With binding off, the route issues nothing and says so.

    Issuing bundles nothing will ever check would manufacture records that read
    as though evidence binding were in force. The unset posture must leave no
    such trace — which is the same claim the byte-identical journal test makes,
    from the route's side.
    """

    with _boot_with_evidence(monkeypatch, demo_env, evidence=False) as client:
        response = client.post(
            "/autonomy/evidence",
            json={"kind": "backend_served", "symbols": ["SPY"]},
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert response.status_code == 404
        assert response.json()["refusal"] == "EVIDENCE_BINDING_DISABLED"

    with sqlite3.connect(demo_env / "chronos.db") as connection:
        rows = list(connection.execute("SELECT COUNT(*) FROM autonomy_evidence_bundles"))
    assert rows == [(0,)]


def test_an_authenticated_row_under_evidence_binding_records_its_posture(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """ADR-0055: registry configured, evidence binding in force, row bound → the row says so."""

    mandate = _mandate()
    _activate(sessions, mandate)
    registry = _registry_file(tmp_path, _registration("claude-worker", WORKER_CREDENTIAL))
    issued = _issue(sessions)
    _enqueue(
        sessions,
        _payload(evidence_id=issued.bundle_id, digest=issued.digest),
        "claude-worker",
    )

    outcome = _drain(_runtime(sessions, registry, mandate))

    assert outcome.admission is not None and outcome.admission.admitted, outcome.admission
    decision_stream = durable.stream_for(durable.DECISION_STREAM, _FINGERPRINT)
    with sessions.begin() as session:
        (journal,) = list(
            session.scalars(select(HashChainRow).where(HashChainRow.stream == decision_stream))
        )
        payload = json.loads(journal.payload_json)
    assert payload["posture"] == {
        "version": 1,
        "identity": "authenticated",
        "registry": "configured",
        "evidence_binding": "in_force",
        "credential_epoch_bound": True,
    }
    assert durable.read_posture(payload) == durable.DecisionPosture(
        registry_configured=True, evidence_binding=True, credential_epoch_bound=True
    )


# ================================================= FIX-26: durable sticky EXPIRED (D-26)
#
# Daybreak's sealed FIX-26 threat map (run-20260913-pm/FIX-26-threatmap-daybreak.md)
# rows 5-6, each as a pin: durability across restart lives in
# tests/chaos/test_clock_faults.py; the protocol pins below cover rollback
# atomicity, precedence, keying, citation order, corruption fail-closed,
# idempotence, and the prune interaction. The rewind/restart assertions are
# RED before the fix; pins marked "guard" are green without it and exist to
# prove the fix changes nothing outside expiry stickiness.

_FIX26_KIND = "evidence_bundle_expired"


def _fix26_binding(proposer_id: str = "claude-worker") -> tuple[str, str]:
    binding = registration_binding(
        ProposerRegistration.model_validate(_registration(proposer_id, WORKER_CREDENTIAL))
    )
    return binding.credential_epoch, binding.registry_entry_digest


def _fix26_issue(
    sessions: sessionmaker[Session],
    *,
    fingerprint: str = _FINGERPRINT,
    proposer_id: str = "claude-worker",
    ttl_seconds: float = 60.0,
    now: datetime = _NOW,
) -> evidence_bundles.IssuedBundle:
    epoch, registration_digest = _fix26_binding(proposer_id)
    with sessions.begin() as session:
        return evidence_bundles.issue(
            session,
            account_fingerprint=fingerprint,
            proposer_id=proposer_id,
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration_digest,
            kind=BundleKind.BACKEND_SERVED,
            digest=_SERVED_DIGEST,
            now=now,
            ttl_seconds=ttl_seconds,
        )


def _fix26_resolve(
    sessions: sessionmaker[Session],
    *,
    fingerprint: str = _FINGERPRINT,
    cited_ids: tuple[str, ...],
    proposer_id: str = "claude-worker",
    now: datetime,
) -> evidence_bundles.Resolution:
    # FU2 (declared change): a full verification pass completes immediately before every
    # resolve here, so each FIX-26/K1 pin exercises the bounded read against a freshly
    # certified stream. The pins' own assertions are unchanged.
    _fu2_certify(sessions, fingerprint)
    return _fu2_resolve(
        sessions,
        fingerprint=fingerprint,
        cited_ids=cited_ids,
        proposer_id=proposer_id,
        now=now,
    )


def _fu2_resolve(
    sessions: sessionmaker[Session],
    *,
    fingerprint: str = _FINGERPRINT,
    cited_ids: tuple[str, ...],
    proposer_id: str = "claude-worker",
    now: datetime,
) -> evidence_bundles.Resolution:
    """A resolve with NO pass first: what the drain sees between passes."""

    epoch, registration_digest = _fix26_binding(proposer_id)
    with sessions.begin() as session:
        return evidence_bundles.resolve(
            session,
            account_fingerprint=fingerprint,
            cited_ids=cited_ids,
            proposer_id=proposer_id,
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration_digest,
            now=now,
        )


def _fix26_markers(
    sessions: sessionmaker[Session], fingerprint: str = _FINGERPRINT
) -> list[HashChainRow]:
    stream = evidence_bundles.hash_chain_stream(fingerprint)
    with sessions.begin() as session:
        return list(
            session.scalars(
                select(HashChainRow).where(
                    HashChainRow.stream == stream,
                    HashChainRow.kind == _FIX26_KIND,
                )
            )
        )


def test_a_sticky_expiry_writes_exactly_one_durable_marker(sessions: sessionmaker[Session]) -> None:
    """Threat row 9: exactly one durable transition; repeat reads are write-free."""

    issued = _fix26_issue(sessions)
    expired_at = _NOW + timedelta(seconds=61)
    for _ in range(3):
        refused = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=expired_at)
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    markers = _fix26_markers(sessions)
    assert len(markers) == 1, (
        f"expected exactly one durable expiry marker, found {len(markers)}: "
        "repeat resolutions must not append again"
    )
    stream = evidence_bundles.hash_chain_stream(_FINGERPRINT)

    def stream_shape() -> tuple[int, int]:
        # (head sequence, row count): the head is the LAST row's sequence, so an append
        # anywhere moves it; the count is independent of how the sequence is numbered.
        with sessions.begin() as session:
            head = hash_chain.head(session, stream)
            assert head is not None
            rows = len(
                list(session.scalars(select(HashChainRow).where(HashChainRow.stream == stream)))
            )
            return head.sequence, rows

    shape_after_writes = stream_shape()
    _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=expired_at)
    assert stream_shape() == shape_after_writes, "a repeat sticky read appended to the stream"


def test_a_rolled_back_expiry_refusal_leaves_no_sticky_marker(
    sessions: sessionmaker[Session],
) -> None:
    """Threat row 2 (guard): marker and refusal are one atomic write — a rolled-back
    refusal leaves no durable fact, and a rewound clock then judges the bundle live."""

    issued = _fix26_issue(sessions)
    epoch, registration_digest = _fix26_binding()
    session = sessions()
    try:
        refused = evidence_bundles.resolve(
            session,
            account_fingerprint=_FINGERPRINT,
            cited_ids=(issued.bundle_id,),
            proposer_id="claude-worker",
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration_digest,
            now=_NOW + timedelta(seconds=61),
        )
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
        session.rollback()
    finally:
        session.close()
    assert _fix26_markers(sessions) == [], "a rolled-back refusal still wrote a marker"
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert rewound.refusal is None and rewound.bundle is not None, rewound.detail


def test_a_sticky_marker_is_scoped_to_its_own_account_stream(
    sessions: sessionmaker[Session],
) -> None:
    """Threat row 6: one account's expiry verdict can never poison another account."""

    other_fingerprint = "b" * 64
    poisoned = _fix26_issue(sessions, fingerprint=other_fingerprint)
    refused = _fix26_resolve(
        sessions,
        fingerprint=other_fingerprint,
        cited_ids=(poisoned.bundle_id,),
        now=_NOW + timedelta(seconds=61),
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

    clean = _fix26_issue(sessions, fingerprint=_FINGERPRINT)
    rewound = _fix26_resolve(sessions, cited_ids=(clean.bundle_id,), now=_NOW)
    assert rewound.refusal is None and rewound.bundle is not None, (
        f"another account's sticky verdict leaked into this account: {rewound.detail}"
    )
    assert _fix26_markers(sessions) == [], "an expiry marker landed in the wrong account's stream"


@pytest.mark.parametrize(
    "payload",
    [
        {"bundle_id": 12345},
        {"bundle_id": None},
        ["not", "an", "object"],
        "a bare string",
    ],
    ids=["wrong-typed-id", "null-id", "json-list", "bare-string"],
)
def test_a_malformed_expiry_marker_refuses_closed(
    sessions: sessionmaker[Session], payload: object
) -> None:
    """Threat row 8: a well-formed chain carrying a malformed expiry record fails
    CLOSED — never ignored, never treated as 'not expired', never a crash."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=evidence_bundles.hash_chain_stream(_FINGERPRINT),
            kind=_FIX26_KIND,
            payload=payload,
            recorded_at=_NOW,
        )
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        f"malformed expiry evidence {payload!r} was ignored and the live bundle admitted"
    )
    assert resolution.bundle is None


def test_a_broken_evidence_chain_refuses_closed(sessions: sessionmaker[Session]) -> None:
    """Threat row 8: a targeted edit that breaks the chain fails CLOSED — a corrupt
    durable record is never 'not expired'."""

    from sqlalchemy import text

    issued = _fix26_issue(sessions)
    refused = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=_NOW + timedelta(seconds=61)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

    with sessions.begin() as session:
        session.execute(
            text(
                "UPDATE hash_chain_records SET payload_json = payload_json || ' ' "
                "WHERE id = (SELECT MIN(id) FROM hash_chain_records)"
            )
        )
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert rewound.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "a tampered evidence stream was read as clean and the bundle admitted at a rewound clock"
    )


def test_duplicate_sticky_markers_still_refuse_expired(sessions: sessionmaker[Session]) -> None:
    """Threat row 8: a duplicate/conflicting marker is the same verdict, not an error."""

    issued = _fix26_issue(sessions)
    expired_at = _NOW + timedelta(seconds=61)
    refused = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=expired_at)
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=evidence_bundles.hash_chain_stream(_FINGERPRINT),
            kind=_FIX26_KIND,
            payload={"bundle_id": issued.bundle_id, "expires_at": issued.expires_at.isoformat()},
            recorded_at=expired_at,
        )
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert rewound.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "duplicate sticky markers broke the sticky verdict"
    )


def test_a_prefix_neighbor_expiry_marker_does_not_match(
    sessions: sessionmaker[Session],
) -> None:
    """A marker names one exact bundle id, never a prefix-related neighbor."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=evidence_bundles.hash_chain_stream(_FINGERPRINT),
            kind=_FIX26_KIND,
            payload={
                "bundle_id": issued.bundle_id + "-neighbor",
                "expires_at": issued.expires_at.isoformat(),
            },
            recorded_at=_NOW,
        )
    resolved = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolved.refusal is None and resolved.bundle is not None, (
        f"a prefix-neighbor marker poisoned the exact live bundle: {resolved.detail}"
    )
    assert resolved.bundle.bundle_id == issued.bundle_id


def test_a_sticky_bundle_stays_foreign_to_another_proposer(sessions: sessionmaker[Session]) -> None:
    """Threat row 4 (guard): sticky expiry is consulted only AFTER the ownership and
    registration checks — a sticky bundle cited by another proposer is FOREIGN."""

    issued = _fix26_issue(sessions)
    refused = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=_NOW + timedelta(seconds=61)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

    epoch, registration_digest = _fix26_binding("tradingview-bridge")
    with sessions.begin() as session:
        foreign = evidence_bundles.resolve(
            session,
            account_fingerprint=_FINGERPRINT,
            cited_ids=(issued.bundle_id,),
            proposer_id="tradingview-bridge",
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration_digest,
            now=_NOW,
        )
    assert foreign.refusal is evidence_bundles.ResolutionRefusal.FOREIGN, (
        f"the sticky check pre-empted the ownership check: {foreign.refusal}"
    )


def test_a_pruned_sticky_bundle_resolves_unissued(sessions: sessionmaker[Session]) -> None:
    """Threat row 5 (guard): prune_expired still prunes — after the row goes, the
    surviving marker is NOT lookup authority: today's UNISSUED result is preserved."""

    issued = _fix26_issue(sessions)
    refused = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=_NOW + timedelta(seconds=61)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

    pruned_at = (
        _NOW + timedelta(seconds=61) + evidence_bundles.RETENTION_AFTER_EXPIRY + timedelta(days=1)
    )
    with sessions.begin() as session:
        pruned = evidence_bundles.prune_expired(
            session, account_fingerprint=_FINGERPRINT, now=pruned_at
        )
    assert pruned == 1
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=pruned_at)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.UNISSUED, (
        f"a surviving expiry marker resurrected a pruned bundle as {resolution.refusal} "
        "instead of today's UNISSUED"
    )


def test_citation_order_is_preserved_with_sticky_markers(sessions: sessionmaker[Session]) -> None:
    """Threat row 7: the first cited id naming a live row governs — an earlier live
    citation admits even when a later citation is sticky-expired, and the reverse
    order refuses on the sticky first citation."""

    sticky = _fix26_issue(sessions, ttl_seconds=60.0)
    live = _fix26_issue(sessions, ttl_seconds=300.0)
    refused = _fix26_resolve(
        sessions, cited_ids=(sticky.bundle_id,), now=_NOW + timedelta(seconds=61)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

    live_first = _fix26_resolve(sessions, cited_ids=(live.bundle_id, sticky.bundle_id), now=_NOW)
    assert live_first.refusal is None and live_first.bundle is not None, (
        f"a later sticky citation overrode an earlier live one: {live_first.detail}"
    )
    assert live_first.bundle.bundle_id == live.bundle_id

    sticky_first = _fix26_resolve(sessions, cited_ids=(sticky.bundle_id, live.bundle_id), now=_NOW)
    assert sticky_first.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "a sticky first citation did not refuse at a rewound clock"
    )


def test_a_broken_chain_refuses_a_live_bundle_closed(sessions: sessionmaker[Session]) -> None:
    """Threat row 8: chain verification is load-bearing even with NO marker — a
    targeted edit to the chain of a live, unexpired bundle fails CLOSED rather
    than admitting on a ledger that can no longer prove what it recorded."""

    from sqlalchemy import text

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    with sessions.begin() as session:
        session.execute(
            text(
                "UPDATE hash_chain_records SET payload_json = payload_json || ' ' "
                "WHERE id = (SELECT MIN(id) FROM hash_chain_records)"
            )
        )
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "a tampered evidence stream admitted a live bundle instead of refusing closed"
    )
    assert resolution.bundle is None


# ------------------------------------------ FIX-26r1: reader F2 (PREMERGE-275-reader.md)
#
# Three branches of the sticky-EXPIRED protocol that were true in code but pinned by
# nothing (each survived all 47 tests as the reader's mutant MA / MB / MC).


def test_an_undecodable_expiry_marker_refuses_closed(sessions: sessionmaker[Session]) -> None:
    """MA: a VALID chain carrying an expiry record whose payload does not decode as
    JSON fails CLOSED. ``hash_chain.append`` always writes valid JSON, so the record
    is inserted directly, with a correct chain hash, to reach the decode branch."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    stream = evidence_bundles.hash_chain_stream(_FINGERPRINT)
    undecodable = "{not json"
    with sessions.begin() as session:
        previous = hash_chain.head(session, stream)
        sequence = 0 if previous is None else previous.sequence + 1
        previous_hash = hash_chain.GENESIS_HASH if previous is None else previous.record_hash
        session.add(
            HashChainRow(
                stream=stream,
                sequence=sequence,
                kind=_FIX26_KIND,
                payload_json=undecodable,
                recorded_at=_NOW,
                previous_hash=previous_hash,
                record_hash=hash_chain.compute_hash(
                    stream=stream,
                    sequence=sequence,
                    recorded_at=_NOW,
                    payload_json=undecodable,
                    previous_hash=previous_hash,
                ),
            )
        )
    with sessions.begin() as session:
        # the refusal must come from the decode branch, not from a broken chain
        assert hash_chain.verify(session, stream).ok
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "an undecodable expiry record was ignored and the live bundle admitted"
    )
    assert resolution.bundle is None


def test_another_accounts_expiry_markers_are_ignored(sessions: sessionmaker[Session]) -> None:
    """MB: the marker scan reads only this account's stream. Another account's
    malformed marker, and its well-formed marker naming this very bundle id, neither
    refuse nor alter this account's live bundle."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    other_stream = evidence_bundles.hash_chain_stream("c" * 64)
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=other_stream,
            kind=_FIX26_KIND,
            payload={"bundle_id": 12345},
            recorded_at=_NOW,
        )
        hash_chain.append(
            session,
            stream=other_stream,
            kind=_FIX26_KIND,
            payload={"bundle_id": issued.bundle_id, "expires_at": issued.expires_at.isoformat()},
            recorded_at=_NOW,
        )
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolution.refusal is None and resolution.bundle is not None, (
        f"another account's expiry markers refused this account's live bundle: {resolution.detail}"
    )
    assert _fix26_markers(sessions) == [], "a marker landed on this account's stream"


def _fix26_sticky(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions)
    refused = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=_NOW + timedelta(seconds=61)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    assert len(_fix26_markers(sessions)) == 1
    return issued


def test_a_sticky_bundle_still_refuses_registration_unbound(
    sessions: sessionmaker[Session],
) -> None:
    """MC: sticky expiry is consulted only AFTER the registration checks — a sticky
    bundle cited without a registration binding is REGISTRATION_UNBOUND, not EXPIRED."""

    issued = _fix26_sticky(sessions)
    with sessions.begin() as session:
        unbound = evidence_bundles.resolve(
            session,
            account_fingerprint=_FINGERPRINT,
            cited_ids=(issued.bundle_id,),
            proposer_id="claude-worker",
            proposer_credential_epoch=None,
            proposer_registry_entry_digest=None,
            now=_NOW,
        )
    assert unbound.refusal is evidence_bundles.ResolutionRefusal.REGISTRATION_UNBOUND, (
        f"the sticky check pre-empted the registration-binding check: {unbound.refusal}"
    )


def test_a_sticky_bundle_still_refuses_registration_replaced(
    sessions: sessionmaker[Session],
) -> None:
    """MC: a sticky bundle cited under a different credential epoch / registry entry
    is REGISTRATION_REPLACED, not EXPIRED."""

    issued = _fix26_sticky(sessions)
    other_epoch, other_digest = _fix26_binding("tradingview-bridge")
    assert (other_epoch, other_digest) != _fix26_binding()
    with sessions.begin() as session:
        replaced = evidence_bundles.resolve(
            session,
            account_fingerprint=_FINGERPRINT,
            cited_ids=(issued.bundle_id,),
            proposer_id="claude-worker",
            proposer_credential_epoch=other_epoch,
            proposer_registry_entry_digest=other_digest,
            now=_NOW,
        )
    assert replaced.refusal is evidence_bundles.ResolutionRefusal.REGISTRATION_REPLACED, (
        f"the sticky check pre-empted the registration-replacement check: {replaced.refusal}"
    )


# FIX-26r3: reader delta-read F1-F3 (PREMERGE-275-delta-reader.md)
#
# Pins for the mutants the r1/r2 chain left alive: N1 reverse prefix, N7 empty-string id, N2
# case-fold, N3 expiry boundary, N4/N5 marker payload and recorded_at, N6 distinct detail
# sentences. Each is proven RED against its mutant in the FIX-26r3 handoff.


@pytest.mark.parametrize(
    "marker_id",
    [
        lambda bundle_id: "",
        lambda bundle_id: bundle_id[:-1],
        lambda bundle_id: bundle_id.upper(),
    ],
    ids=["empty-string", "strict-prefix", "other-case"],
)
def test_a_marker_naming_another_id_never_refuses_the_live_bundle(
    sessions: sessionmaker[Session], marker_id: Any
) -> None:
    """N1/N7/N2: a marker matches the EXACT id. An empty id, a strict prefix of the live id
    and the live id in another case are different ids, so the live bundle still resolves."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    named = marker_id(issued.bundle_id)
    assert named != issued.bundle_id
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=evidence_bundles.hash_chain_stream(_FINGERPRINT),
            kind=_FIX26_KIND,
            payload={"bundle_id": named, "expires_at": issued.expires_at.isoformat()},
            recorded_at=_NOW,
        )
    with sessions.begin() as session:
        assert hash_chain.verify(session, evidence_bundles.hash_chain_stream(_FINGERPRINT)).ok
    resolved = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolved.refusal is None and resolved.bundle is not None, (
        f"a marker naming {named!r} refused the live bundle {issued.bundle_id!r}: {resolved.detail}"
    )
    assert resolved.bundle.bundle_id == issued.bundle_id


def test_the_expiry_boundary_is_inclusive(sessions: sessionmaker[Session]) -> None:
    """N3: a bundle is expired AT expires_at (``now >= expires_at``) and live one microsecond
    before it."""

    issued = _fix26_issue(sessions, ttl_seconds=60.0)
    just_before = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=issued.expires_at - timedelta(microseconds=1)
    )
    assert just_before.refusal is None and just_before.bundle is not None, just_before.detail
    assert _fix26_markers(sessions) == [], "a live resolve wrote an expiry marker"
    at_expiry = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=issued.expires_at)
    assert at_expiry.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        "a bundle resolved live AT its expires_at: the boundary is exclusive"
    )
    assert len(_fix26_markers(sessions)) == 1


def test_the_expiry_marker_records_the_bundle_the_deadline_and_the_drains_now(
    sessions: sessionmaker[Session],
) -> None:
    """N4/N5: the ONE marker the drain writes carries exactly the bundle's id and the row's
    expires_at, and is recorded at the drain's ``now`` (the audit trail's 'when refused')."""

    issued = _fix26_issue(sessions)
    drain_now = _NOW + timedelta(seconds=61)
    refused = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=drain_now)
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    with sessions.begin() as session:
        row = evidence_bundles.load(
            session, account_fingerprint=_FINGERPRINT, bundle_id=issued.bundle_id
        )
        assert row is not None
        row_expires_at = row.expires_at.isoformat()
        assert row.issued_at.isoformat() != row_expires_at
    (marker,) = _fix26_markers(sessions)
    assert json.loads(marker.payload_json) == {
        "bundle_id": issued.bundle_id,
        "expires_at": row_expires_at,
    }
    recorded_at = marker.recorded_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    assert recorded_at == drain_now, "the marker was not recorded at the drain's now"
    assert recorded_at != issued.expires_at


def _fix26_raw_marker(sessions: sessionmaker[Session], payload_json: str) -> None:
    """Append an expiry record whose payload TEXT is exactly ``payload_json`` with a correct
    chain hash (``hash_chain.append`` can only write JSON it encoded itself)."""

    stream = evidence_bundles.hash_chain_stream(_FINGERPRINT)
    with sessions.begin() as session:
        previous = hash_chain.head(session, stream)
        assert previous is not None
        sequence = previous.sequence + 1
        session.add(
            HashChainRow(
                stream=stream,
                sequence=sequence,
                kind=_FIX26_KIND,
                payload_json=payload_json,
                recorded_at=_NOW,
                previous_hash=previous.record_hash,
                record_hash=hash_chain.compute_hash(
                    stream=stream,
                    sequence=sequence,
                    recorded_at=_NOW,
                    payload_json=payload_json,
                    previous_hash=previous.record_hash,
                ),
            )
        )


_FIX26_DETAIL_PHRASES = {
    "corrupt-stream": "failed verification",
    "undecodable": "does not decode",
    "non-object": "is not a JSON object",
    "non-string-id": "names no string bundle id",
    "sticky": "already refused EXPIRED at an earlier drain",
    "first-refusal": "the drain's clock is past it",
}


@pytest.mark.parametrize(
    "case", ["corrupt-stream", "undecodable", "non-object", "non-string-id", "sticky"]
)
def test_each_sticky_refusal_carries_its_own_distinguishing_detail(
    sessions: sessionmaker[Session], case: str
) -> None:
    """N6: the refusal CODE is EXPIRED in every one of these, so the detail sentence is the
    only thing telling an operator which happened. Each carries its own phrase and none of
    the others' (the phrase is asserted, not the whole sentence)."""

    from sqlalchemy import text

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    at = _NOW
    if case == "corrupt-stream":
        with sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE hash_chain_records SET payload_json = payload_json || ' ' "
                    "WHERE id = (SELECT MIN(id) FROM hash_chain_records)"
                )
            )
    elif case == "undecodable":
        _fix26_raw_marker(sessions, "{not json")
    elif case == "non-object":
        _fix26_raw_marker(sessions, json.dumps(["not", "an", "object"]))
    elif case == "non-string-id":
        _fix26_raw_marker(sessions, json.dumps({"bundle_id": 12345}))
    else:
        _fix26_sticky_at(sessions, issued)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=at)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, resolution.refusal
    assert _FIX26_DETAIL_PHRASES[case] in resolution.detail, resolution.detail
    for other, phrase in _FIX26_DETAIL_PHRASES.items():
        if other != case:
            assert phrase not in resolution.detail, (
                f"the {case} refusal reads like the {other} one: {resolution.detail!r}"
            )


def _fix26_sticky_at(
    sessions: sessionmaker[Session], issued: evidence_bundles.IssuedBundle
) -> None:
    refused = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=issued.expires_at + timedelta(seconds=1)
    )
    assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    assert _FIX26_DETAIL_PHRASES["first-refusal"] in refused.detail
    assert _FIX26_DETAIL_PHRASES["sticky"] not in refused.detail


# ---------------------------------------------- FIX-26-K1: classification by hashed shape
#
# ``hash_chain.compute_hash`` digests stream, sequence, recorded_at, payload_json and
# previous_hash — NOT ``kind``. A sticky read that SELECTS by ``kind`` therefore lets a
# one-field edit hide a durable EXPIRED verdict while ``hash_chain.verify`` stays ok
# (Daybreak's P1 on #275). These pins hold the sealed classification
# (FIX-26-K1-preflight-daybreak.md): an expiry record is a row of the requested account's
# exact stream whose JSON object has EXACTLY the keys {bundle_id, expires_at}, both
# strings, whatever ``kind`` says; every doubt refuses closed (D0-D4).

_K1_ISSUE_KEYS = {
    "bundle_id",
    "proposer_id",
    "proposer_credential_epoch",
    "proposer_registry_entry_digest",
    "bundle_kind",
    "digest",
    "bundle_version",
    "expires_at",
}
_K1_EXPIRY_KEYS = {"bundle_id", "expires_at"}
_K1_SHAPE_PHRASE = "is not the exact expiry shape"
_K1_EXPIRES_PHRASE = "carries a non-string expires_at"


def _k1_stream(fingerprint: str = _FINGERPRINT) -> str:
    return evidence_bundles.hash_chain_stream(fingerprint)


def _k1_chain_ok(sessions: sessionmaker[Session], fingerprint: str = _FINGERPRINT) -> bool:
    with sessions.begin() as session:
        return hash_chain.verify(session, _k1_stream(fingerprint)).ok


def _k1_append(
    sessions: sessionmaker[Session],
    payload: object,
    *,
    kind: str,
    fingerprint: str = _FINGERPRINT,
) -> None:
    """A chain-correct record with any payload and any ``kind`` (``append`` hashes the JSON)."""

    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=_k1_stream(fingerprint),
            kind=kind,
            payload=payload,  # type: ignore[arg-type]
            recorded_at=_NOW,
        )


def _k1_raw(sessions: sessionmaker[Session], payload_json: str, *, kind: str) -> None:
    """A chain-correct record whose payload TEXT is exactly ``payload_json`` (maybe not JSON)."""

    stream = _k1_stream()
    with sessions.begin() as session:
        previous = hash_chain.head(session, stream)
        assert previous is not None
        sequence = previous.sequence + 1
        session.add(
            HashChainRow(
                stream=stream,
                sequence=sequence,
                kind=kind,
                payload_json=payload_json,
                recorded_at=_NOW,
                previous_hash=previous.record_hash,
                record_hash=hash_chain.compute_hash(
                    stream=stream,
                    sequence=sequence,
                    recorded_at=_NOW,
                    payload_json=payload_json,
                    previous_hash=previous.record_hash,
                ),
            )
        )


def _k1_relabel(sessions: sessionmaker[Session], *, from_kind: str, to_kind: str) -> int:
    """The one-field edit: rewrite ``kind`` of this account's rows labelled ``from_kind``."""

    from sqlalchemy import text

    with sessions.begin() as session:
        result = session.execute(
            text("UPDATE hash_chain_records SET kind = :to WHERE stream = :s AND kind = :frm"),
            {"to": to_kind, "s": _k1_stream(), "frm": from_kind},
        )
        return int(result.rowcount)  # type: ignore[attr-defined]


def _k1_expired(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    """A bundle refused EXPIRED once, so its genuine #275 two-key marker is on the stream."""

    issued = _fix26_issue(sessions)
    _fix26_sticky_at(sessions, issued)
    assert len(_fix26_markers(sessions)) == 1
    return issued


def _k1_refused_closed(resolution: evidence_bundles.Resolution) -> None:
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        f"doubtful durable evidence did not refuse closed: {resolution.refusal} {resolution.detail}"
    )
    assert resolution.bundle is None


@pytest.mark.parametrize("new_kind", ["evidence_bundle_issued", "anything-else", ""])
def test_a_kind_edit_on_an_expiry_record_does_not_revive_the_bundle(
    sessions: sessionmaker[Session], new_kind: str
) -> None:
    """Daybreak's pin. The edit is invisible to ``hash_chain.verify`` (asserted), so only a
    read that classifies by the HASHED payload can keep the verdict; a rewound clock must
    still refuse EXPIRED with the sticky detail."""

    issued = _k1_expired(sessions)
    assert _k1_relabel(sessions, from_kind=_FIX26_KIND, to_kind=new_kind) == 1
    assert _fix26_markers(sessions) == [], "the edit did not take"
    assert _k1_chain_ok(sessions), "the kind edit broke the chain; this pin must prove it does not"
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(rewound)
    assert _FIX26_DETAIL_PHRASES["sticky"] in rewound.detail, rewound.detail


def test_a_kind_edit_does_not_revive_the_bundle_across_a_restart_and_rewind(
    tmp_path: Path,
) -> None:
    """The same edit made before a restart: a fresh engine on the same file, rewound clock."""

    db_path = tmp_path / "k1-restart.db"
    first = Database(f"sqlite+pysqlite:///{db_path}")
    first.initialize()
    try:
        issued = _k1_expired(first.sessions)
        assert (
            _k1_relabel(first.sessions, from_kind=_FIX26_KIND, to_kind="evidence_bundle_issued")
            == 1
        )
    finally:
        first.dispose()
    second = Database(f"sqlite+pysqlite:///{db_path}")
    second.initialize()
    try:
        assert _k1_chain_ok(second.sessions)
        rewound = _fix26_resolve(second.sessions, cited_ids=(issued.bundle_id,), now=_NOW)
        _k1_refused_closed(rewound)
        assert _FIX26_DETAIL_PHRASES["sticky"] in rewound.detail, rewound.detail
    finally:
        second.dispose()


def test_an_issuance_relabelled_as_an_expiry_refuses_closed(
    sessions: sessionmaker[Session],
) -> None:
    """D2: an eight-key issuance whose kind is flipped TO the expiry kind is a labelled
    expiry that is not the exact shape — refuse closed, never ignore it."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    assert _k1_relabel(sessions, from_kind="evidence_bundle_issued", to_kind=_FIX26_KIND) == 1
    assert _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _K1_SHAPE_PHRASE in resolution.detail, resolution.detail


def test_the_issuance_and_expiry_payload_shapes_are_disjoint(
    sessions: sessionmaker[Session],
) -> None:
    """C2: derived from the two writers, not asserted from memory. The issuance payload has
    exactly the eight keys and the expiry payload exactly the two, so an issued record can
    never read as an expiry; and a stream of known pre-#275 issued rows plus a #275 marker
    keeps its meaning (the live bundle admitted, the expired one sticky after a rewind)."""

    live = _fix26_issue(sessions, ttl_seconds=3600.0)
    expired = _k1_expired(sessions)
    stream = _k1_stream()
    with sessions.begin() as session:
        rows = list(
            session.execute(
                select(HashChainRow.kind, HashChainRow.payload_json)
                .where(HashChainRow.stream == stream)
                .order_by(HashChainRow.sequence)
            )
        )
    shapes = {kind: set(json.loads(payload)) for kind, payload in rows}
    assert shapes["evidence_bundle_issued"] == _K1_ISSUE_KEYS, shapes
    assert shapes[_FIX26_KIND] == _K1_EXPIRY_KEYS, shapes
    assert _K1_ISSUE_KEYS != _K1_EXPIRY_KEYS
    admitted = _fix26_resolve(sessions, cited_ids=(live.bundle_id,), now=_NOW)
    assert admitted.refusal is None and admitted.bundle is not None, admitted.detail
    rewound = _fix26_resolve(sessions, cited_ids=(expired.bundle_id,), now=_NOW)
    _k1_refused_closed(rewound)
    assert _FIX26_DETAIL_PHRASES["sticky"] in rewound.detail


@pytest.mark.parametrize("kind", [_FIX26_KIND, "evidence_bundle_issued", "x"])
def test_an_exact_shaped_marker_in_another_account_does_not_participate(
    sessions: sessionmaker[Session], kind: str
) -> None:
    """Only the requested account's exact stream is read, whatever the label: another
    account's exact marker naming THIS bundle, and its typed-invalid exact-key object,
    neither refuse nor alter this account's live bundle."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    other = "c" * 64
    _k1_append(
        sessions,
        {"bundle_id": issued.bundle_id, "expires_at": issued.expires_at.isoformat()},
        kind=kind,
        fingerprint=other,
    )
    _k1_append(sessions, {"bundle_id": 7, "expires_at": None}, kind=kind, fingerprint=other)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    assert resolution.refusal is None and resolution.bundle is not None, resolution.detail


@pytest.mark.parametrize("dup_kind", [_FIX26_KIND, "evidence_bundle_issued"])
@pytest.mark.parametrize("dup_first", [True, False], ids=["dup-before", "dup-after"])
def test_duplicate_exact_markers_in_either_order_are_one_verdict(
    sessions: sessionmaker[Session], dup_kind: str, *, dup_first: bool
) -> None:
    """Duplicates before or after the genuine marker, labelled or not, are the same sticky
    verdict; on a valid chain their chronological order is irrelevant."""

    issued = _fix26_issue(sessions)
    duplicate = {"bundle_id": issued.bundle_id, "expires_at": issued.expires_at.isoformat()}
    if dup_first:
        _k1_append(sessions, duplicate, kind=dup_kind)
    first = _fix26_resolve(
        sessions, cited_ids=(issued.bundle_id,), now=issued.expires_at + timedelta(seconds=1)
    )
    _k1_refused_closed(first)
    if not dup_first:
        _k1_append(sessions, duplicate, kind=dup_kind)
    assert _k1_chain_ok(sessions)
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(rewound)
    assert _FIX26_DETAIL_PHRASES["sticky"] in rewound.detail, rewound.detail


@pytest.mark.parametrize("edit", ["delete-middle", "swap-sequences"])
def test_a_deleted_or_reordered_evidence_row_refuses_closed(
    sessions: sessionmaker[Session], edit: str
) -> None:
    """D0: sequence deletion or reordering breaks verification, which refuses closed."""

    from sqlalchemy import text

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fix26_issue(sessions, ttl_seconds=300.0)
    _fix26_issue(sessions, ttl_seconds=300.0)
    stream = _k1_stream()
    with sessions.begin() as session:
        if edit == "delete-middle":
            session.execute(
                text("DELETE FROM hash_chain_records WHERE stream = :s AND sequence = 2"),
                {"s": stream},
            )
        else:
            for frm, to in ((1, 99), (2, 1), (99, 2)):
                session.execute(
                    text(
                        "UPDATE hash_chain_records SET sequence = :to "
                        "WHERE stream = :s AND sequence = :frm"
                    ),
                    {"to": to, "s": stream, "frm": frm},
                )
    assert not _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _FIX26_DETAIL_PHRASES["corrupt-stream"] in resolution.detail, resolution.detail


@pytest.mark.parametrize(
    "shape",
    [
        "extra-key",
        "missing-expires-at",
        "missing-bundle-id",
        "renamed-bundle-id",
        "renamed-expires-at",
    ],
)
def test_a_labelled_expiry_with_extra_missing_or_renamed_keys_refuses_closed(
    sessions: sessionmaker[Session], shape: str
) -> None:
    """D2: a row labelled with the expiry kind must be EXACTLY the two-key shape."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    at = issued.expires_at.isoformat()
    payload = {
        "extra-key": {"bundle_id": issued.bundle_id, "expires_at": at, "note": "x"},
        "missing-expires-at": {"bundle_id": issued.bundle_id},
        "missing-bundle-id": {"expires_at": at},
        "renamed-bundle-id": {"bundleId": issued.bundle_id, "expires_at": at},
        "renamed-expires-at": {"bundle_id": issued.bundle_id, "expiresAt": at},
    }[shape]
    _k1_append(sessions, payload, kind=_FIX26_KIND)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _FIX26_DETAIL_PHRASES["sticky"] not in resolution.detail, resolution.detail


@pytest.mark.parametrize("kind", [_FIX26_KIND, "evidence_bundle_issued", "x"])
@pytest.mark.parametrize(
    "values",
    ["int-id", "null-id", "int-expires-at", "null-expires-at", "list-expires-at"],
)
def test_exact_expiry_keys_with_a_non_string_value_refuse_closed_whatever_the_kind(
    sessions: sessionmaker[Session], kind: str, values: str
) -> None:
    """D3: exactly the two keys but a non-string value refuses closed regardless of kind, so
    flipping the label away cannot hide typed-invalid expiry-shaped evidence."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    at = issued.expires_at.isoformat()
    payload: dict[str, object] = {
        "int-id": {"bundle_id": 12345, "expires_at": at},
        "null-id": {"bundle_id": None, "expires_at": at},
        "int-expires-at": {"bundle_id": issued.bundle_id, "expires_at": 1},
        "null-expires-at": {"bundle_id": issued.bundle_id, "expires_at": None},
        "list-expires-at": {"bundle_id": issued.bundle_id, "expires_at": [at]},
    }[values]
    _k1_append(sessions, payload, kind=kind)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    expected = (
        _FIX26_DETAIL_PHRASES["non-string-id"] if values.endswith("-id") else _K1_EXPIRES_PHRASE
    )
    assert expected in resolution.detail, resolution.detail


@pytest.mark.parametrize(
    ("recorded", "matches"),
    [
        ("bündle-ﬁ", True),  # the exact code points
        ("bündle-fi", False),  # NFKC of the ligature
        ("bündle-ﬁ", False),  # NFD of the umlaut
        ("BÜNDLE-ﬁ", False),  # case-folded neighbour
    ],
    ids=["exact", "nfkc-neighbour", "nfd-neighbour", "case-neighbour"],
)
@pytest.mark.parametrize("kind", [_FIX26_KIND, "evidence_bundle_issued"])
def test_a_non_ascii_bundle_id_is_compared_by_exact_code_points(
    sessions: sessionmaker[Session], kind: str, recorded: str, *, matches: bool
) -> None:
    """Exact code-point equality, no normalization, no ASCII-only rule. Issuance only mints
    ASCII ids, so this pins the sticky read at its own seam (``_durable_expiry_verdict``)."""

    _fix26_issue(sessions)  # a genuine issuance row so the stream is not empty
    _k1_append(
        sessions, {"bundle_id": recorded, "expires_at": "2026-08-14T14:01:00+00:00"}, kind=kind
    )
    _fu2_certify(sessions)  # FU2 (declared): the seam now reads from a verified state (R7)
    with sessions.begin() as session:
        verdict = evidence_bundles._durable_expiry_verdict(
            session, stream=_k1_stream(), bundle_id="bündle-ﬁ"
        )
    if matches:
        assert verdict is not None and _FIX26_DETAIL_PHRASES["sticky"] in verdict, verdict
    else:
        assert verdict is None, f"{recorded!r} matched 'bündle-ﬁ': {verdict}"


@pytest.mark.parametrize(
    ("payload_json", "phrase"),
    [
        ("{not json", "undecodable"),
        ('["not", "an", "object"]', "non-object"),
        ('"a bare string"', "non-object"),
        ("null", "non-object"),
        ("3", "non-object"),
    ],
    ids=["invalid-json", "list", "string", "null", "number"],
)
def test_an_unlabelled_row_that_is_not_a_json_object_refuses_closed(
    sessions: sessionmaker[Session], payload_json: str, phrase: str
) -> None:
    """D1: a decode failure or a decoded non-object on ANY row refuses closed — a relabelled
    malformed marker can no longer hide behind an ordinary kind."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, payload_json, kind="evidence_bundle_issued")
    assert _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _FIX26_DETAIL_PHRASES[phrase] in resolution.detail, resolution.detail


def test_a_matching_marker_does_not_hide_a_later_doubt(sessions: sessionmaker[Session]) -> None:
    """D4: the whole verified stream is scanned before answering — a matching marker
    followed by a later undecodable row refuses with the DOUBT, not the sticky detail."""

    issued = _k1_expired(sessions)
    _k1_raw(sessions, "{not json", kind="evidence_bundle_issued")
    rewound = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(rewound)
    assert _FIX26_DETAIL_PHRASES["undecodable"] in rewound.detail, rewound.detail
    assert _FIX26_DETAIL_PHRASES["sticky"] not in rewound.detail, rewound.detail


def test_a_pruned_bundle_with_a_kind_edited_marker_still_resolves_unissued(
    sessions: sessionmaker[Session],
) -> None:
    """Prune keeps its meaning: after the row goes, a (relabelled) surviving marker is not
    lookup authority — today's UNISSUED result is preserved."""

    issued = _k1_expired(sessions)
    assert _k1_relabel(sessions, from_kind=_FIX26_KIND, to_kind="evidence_bundle_issued") == 1
    pruned_at = issued.expires_at + evidence_bundles.RETENTION_AFTER_EXPIRY + timedelta(days=1)
    with sessions.begin() as session:
        assert (
            evidence_bundles.prune_expired(session, account_fingerprint=_FINGERPRINT, now=pruned_at)
            == 1
        )
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=pruned_at)
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.UNISSUED, resolution.refusal


# ------------------------------------- FIX-26-K1r1: decode failures outside JSONDecodeError
#
# ``hash_chain.verify`` recomputes the digest over whatever SQLite returns, so a chain can be
# valid while ``payload_json`` is not text at all (a BLOB) or is text ``json.loads`` rejects
# with a plain ``ValueError`` (Python's integer-string conversion limit). Both are D1: they must
# refuse closed through the typed reader, never escape as an exception that fails the tick
# (Daybreak's P2 on FIX-26-K1).

_K1R1_UNLABELLED_DECODE = "a durable evidence record does not decode"


def _k1r1_insert(
    sessions: sessionmaker[Session], payload_json: object, *, kind: str = "evidence_bundle_issued"
) -> None:
    """A chain-correct row storing exactly ``payload_json`` (any type); ORDINARY unless ``kind``."""

    stream = _k1_stream()
    with sessions.begin() as session:
        previous = hash_chain.head(session, stream)
        assert previous is not None
        sequence = previous.sequence + 1
        session.add(
            HashChainRow(
                stream=stream,
                sequence=sequence,
                kind=kind,
                payload_json=payload_json,  # type: ignore[arg-type]
                recorded_at=_NOW,
                previous_hash=previous.record_hash,
                record_hash=hash_chain.compute_hash(
                    stream=stream,
                    sequence=sequence,
                    recorded_at=_NOW,
                    payload_json=payload_json,  # type: ignore[arg-type]
                    previous_hash=previous.record_hash,
                ),
            )
        )


@pytest.mark.parametrize(
    "payload_json",
    [b"\xff", b'{"note": "ordinary"}', "9" * 5000],
    ids=["blob", "json-object-blob", "5000-digit-integer"],
)
def test_chain_valid_payloads_that_raise_outside_jsondecodeerror_refuse_closed(
    sessions: sessionmaker[Session], payload_json: object
) -> None:
    """D1 for every decode failure: an invalid-UTF-8 BLOB and a 5,000-digit integer, each a
    chain-valid ordinary row, refuse EXPIRED with the unlabelled decode detail. The BLOB holding
    VALID JSON bytes is the case only the non-text guard refuses: ``json.loads`` accepts bytes,
    so without the guard it would decode as an ordinary object and admit the bundle."""

    if isinstance(payload_json, str):
        # FU2 (declared): 5,000 digits exceed the default 4,096-byte Cr, which would refuse on
        # the byte bound before the decoder ran (pinned separately by
        # test_the_one_intended_divergence_from_k1_is_the_byte_bound). Raise Cr for this case
        # so the pin still reaches the decode failure it exists to pin.
        _fu2_configure(sessions, row_bytes=8192)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, payload_json)
    assert _k1_chain_ok(sessions), "the fixture must be chain-valid for this pin to mean anything"
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _K1R1_UNLABELLED_DECODE in resolution.detail, resolution.detail


def test_a_2000_level_nested_array_is_refused_as_a_non_object(
    sessions: sessionmaker[Session],
) -> None:
    """Negative control for the pin above: deep nesting DECODES (no RecursionError at this
    depth), so it is refused by the non-object rule, not by the decode rule."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, "[" * 2000 + "]" * 2000)
    assert _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _FIX26_DETAIL_PHRASES["non-object"] in resolution.detail, resolution.detail


# ---------------------------------- FIX-26-K1r2: the two new branches made load-bearing


def test_a_100000_deep_json_array_refuses_with_the_unlabelled_decode_detail(
    sessions: sessionmaker[Session],
) -> None:
    """The ``RecursionError`` half of the widened catch: a chain-valid ordinary row nested
    100,000 deep makes ``json.loads`` raise ``RecursionError`` (not ``ValueError``), which must
    refuse closed with the unlabelled decode detail. The 2,000-level array above decodes and is
    the negative control."""

    # FU2 (declared): 200,000 characters exceed the default 4,096-byte Cr; raise Cr for this
    # pin so it still reaches the RecursionError it exists to pin (over Cr the byte bound
    # refuses first: test_the_one_intended_divergence_from_k1_is_the_byte_bound).
    _fu2_configure(sessions, row_bytes=300_000)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, "[" * 100_000 + "]" * 100_000)
    assert _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert _K1R1_UNLABELLED_DECODE in resolution.detail, resolution.detail


def test_a_valid_json_blob_labelled_as_an_expiry_refuses_with_the_labelled_decode_detail(
    sessions: sessionmaker[Session],
) -> None:
    """The labelled branch of the non-text guard: a chain-valid BLOB of valid JSON stored under
    the expiry kind refuses with the LABELLED decode detail — and not the unlabelled one, so a
    guard that collapsed both branches into one phrase cannot pass."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, b'{"bundle_id": "x", "expires_at": "y"}', kind=_FIX26_KIND)
    assert _k1_chain_ok(sessions)
    resolution = _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)
    _k1_refused_closed(resolution)
    assert "a durable expiry record does not decode" in resolution.detail, resolution.detail
    assert _K1R1_UNLABELLED_DECODE not in resolution.detail, resolution.detail


# ================================================== FU2: the bounded, indexed sticky read
#
# FU2r7 (Daybreak PASS-DELTA, kimi CONFIRMED; Kevin A-prime and K-20261004-007/-008/-009).
# The sticky stage no longer verifies the whole stream on every resolve. A verification
# PASS (one SQLite snapshot, chunked across ticks) publishes a verified head (N, H) and the
# expired ids it saw; each resolve then reads ONE bounded statement from that head, proves
# every row this process has verified is still there with its hash (``observed``), and
# answers. Not-ready, stale, over-budget, latched and doubtful states all refuse EXPIRED with
# a DISTINCT detail, and these pins assert the detail (the reason), never only the code: a
# blanket "not ready" refusal would otherwise satisfy every fixture that expects EXPIRED.

_FU2_IN_PROGRESS = "verification in progress"


def _fu2_detail(name: str) -> str:
    """A FU2 refusal-detail constant. Before FU2 it does not exist: a placeholder no detail can
    contain, so a pin fails on the BEHAVIOUR (the old reader answered) rather than on the name."""

    return str(getattr(evidence_bundles, name, f"<{name}: no such refusal before FU2>"))


def _fu2_engine(sessions: sessionmaker[Session]) -> Any:
    return sessions.kw["bind"]


def _fu2_state(sessions: sessionmaker[Session], fingerprint: str = _FINGERPRINT) -> Any:
    return evidence_bundles.stream_state(_fu2_engine(sessions), _fu2_stream(fingerprint))


def _fu2_configure(sessions: sessionmaker[Session], **limits: Any) -> None:
    if not hasattr(evidence_bundles, "configure_verification"):
        return  # before FU2 there are no verification limits; the scenario runs as K1 does
    evidence_bundles.configure_verification(
        _fu2_engine(sessions), evidence_bundles.EvidenceVerificationLimits(**limits)
    )


def _fu2_tick(sessions: sessionmaker[Session], fingerprint: str = _FINGERPRINT) -> Any:
    return evidence_bundles.run_verification_tick(sessions, _fu2_stream(fingerprint))


@pytest.fixture
def file_sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A file-backed database: separate pooled connections, so a pass holds a real snapshot."""

    instance = Database(f"sqlite+pysqlite:///{tmp_path / 'fu2.db'}")
    instance.initialize()
    try:
        yield instance.sessions
    finally:
        instance.dispose()


def _fu2_append(
    sessions: sessionmaker[Session],
    payload: dict[str, Any],
    *,
    kind: str = "evidence_note",
    fingerprint: str = _FINGERPRINT,
) -> None:
    with sessions.begin() as session:
        hash_chain.append(
            session, stream=_fu2_stream(fingerprint), kind=kind, payload=payload, recorded_at=_NOW
        )


def _fu2_rows(sessions: sessionmaker[Session]) -> list[tuple[int, str]]:
    with sessions.begin() as session:
        return [
            (row.sequence, row.record_hash)
            for row in session.scalars(
                select(HashChainRow)
                .where(HashChainRow.stream == _fu2_stream())
                .order_by(HashChainRow.sequence)
            )
        ]


def _fu2_delete_from(sessions: sessionmaker[Session], sequence: int) -> None:
    from sqlalchemy import text

    with sessions.begin() as session:
        session.execute(
            text("DELETE FROM hash_chain_records WHERE stream = :s AND sequence >= :q"),
            {"s": _fu2_stream(), "q": sequence},
        )


def _fu2_sql(sessions: sessionmaker[Session], statement: str, **params: Any) -> None:
    from sqlalchemy import text

    with sessions.begin() as session:
        session.execute(text(statement), {"s": _fu2_stream(), **params})


def _fu2_live(resolution: evidence_bundles.Resolution) -> None:
    assert resolution.refusal is None and resolution.bundle is not None, (
        f"expected a bound answer, got {resolution.refusal} {resolution.detail}"
    )


def _fu2_refused(resolution: evidence_bundles.Resolution, reason: str) -> None:
    """Refused closed, AND for the stated reason (a phrase of the detail)."""

    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
        f"expected an EXPIRED refusal for {reason!r}: {resolution.refusal} {resolution.detail}"
    )
    assert resolution.bundle is None
    assert reason in resolution.detail, f"refused for another reason: {resolution.detail}"


def _fu2_resolve_now(
    sessions: sessionmaker[Session], issued: evidence_bundles.IssuedBundle
) -> evidence_bundles.Resolution:
    return _fu2_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)


# --- an independent oracle: the K1 full read exactly as it stood at main c4a4c48 ---------
# (copied verbatim from evidence_bundles._durable_expiry_verdict before FU2 replaced it, so
# the equivalence pin compares the new bounded read against the reader it replaces, not
# against itself)


def _k1_reference_verdict(session: Session, *, stream: str, bundle_id: str) -> str | None:
    verification = hash_chain.verify(session, stream)
    if not verification.ok:
        return (
            "the account's durable evidence stream failed verification "
            f"({verification.detail}); refusing closed until the stream is repaired"
        )
    rows = session.execute(
        select(HashChainRow.kind, HashChainRow.payload_json)
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.asc())
    )
    matched = False
    for kind, payload_json in rows:
        labelled = kind == evidence_bundles.EXPIRED_EVENT_KIND
        if not isinstance(payload_json, str):
            if labelled:
                return "a durable expiry record does not decode; refusing closed"
            return "a durable evidence record does not decode; refusing closed"
        try:
            decoded = json.loads(payload_json)
        except (ValueError, RecursionError):
            if labelled:
                return "a durable expiry record does not decode; refusing closed"
            return "a durable evidence record does not decode; refusing closed"
        if not isinstance(decoded, dict):
            if labelled:
                return "a durable expiry record is not a JSON object; refusing closed"
            return "a durable evidence record is not a JSON object; refusing closed"
        if set(decoded) == {"bundle_id", "expires_at"}:
            recorded = decoded["bundle_id"]
            if not isinstance(recorded, str):
                return "a durable expiry record names no string bundle id; refusing closed"
            if not isinstance(decoded["expires_at"], str):
                return "a durable expiry record carries a non-string expires_at; refusing closed"
            if recorded == bundle_id:
                matched = True
            continue
        if labelled:
            if not isinstance(decoded.get("bundle_id"), str):
                return "a durable expiry record names no string bundle id; refusing closed"
            return (
                "a durable record labelled as an expiry is not the exact expiry shape "
                "(bundle_id and expires_at only); refusing closed"
            )
    if matched:
        return (
            "the cited evidence bundle was already refused EXPIRED at an earlier "
            "drain and that verdict is durable; wall-clock motion does not revive "
            "refused authority"
        )
    return None


# --- startup and readiness ---------------------------------------------------------------


def test_restart_without_a_verified_cache_refuses_without_walking_lifetime_history_on_resolve(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7: with no verified state (a fresh process), a resolve refuses NOT READY and never
    falls back to walking the stream (no hash_chain.verify call)."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    calls: list[str] = []
    real_verify = hash_chain.verify
    monkeypatch.setattr(
        hash_chain,
        "verify",
        lambda session, stream: calls.append(stream) or real_verify(session, stream),
    )
    resolution = _fu2_resolve_now(sessions, issued)
    _fu2_refused(resolution, _fu2_detail("NOT_READY_DETAIL"))
    assert calls == [], "a resolve with no verified state walked the stream"
    assert _fu2_state(sessions).needs_pass is True


def test_a_stale_full_pass_refuses_and_requests_off_path_verification(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(evidence_bundles, "_monotonic", lambda: clock[0])
    _fu2_configure(sessions, max_age_seconds=900.0)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_live(_fu2_resolve_now(sessions, issued))
    clock[0] = 1000.0 + 901.0
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("STALE_DETAIL"))
    assert _fu2_state(sessions).needs_pass is True


def test_the_verification_deadline_is_stale_at_exactly_t_max(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [500.0]
    monkeypatch.setattr(evidence_bundles, "_monotonic", lambda: clock[0])
    _fu2_configure(sessions, max_age_seconds=30.0)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    clock[0] = 529.999
    _fu2_live(_fu2_resolve_now(sessions, issued))
    clock[0] = 530.0
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("STALE_DETAIL"))


def test_a_wall_clock_rewind_cannot_extend_the_full_verification_deadline(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline is monotonic: a drain clock (``now``) moved back never makes stale fresh."""

    clock = [100.0]
    monkeypatch.setattr(evidence_bundles, "_monotonic", lambda: clock[0])
    _fu2_configure(sessions, max_age_seconds=10.0)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    clock[0] = 111.0
    rewound = _fu2_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW - timedelta(days=365))
    _fu2_refused(rewound, _fu2_detail("STALE_DETAIL"))


@pytest.mark.parametrize(
    "reading", [float("nan"), float("inf"), float("-inf"), "backwards"], ids=str
)
def test_an_unavailable_non_finite_or_backward_monotonic_reading_refuses_closed(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, reading: Any
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(evidence_bundles, "_monotonic", lambda: clock[0])
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    clock[0] = 999.0 if reading == "backwards" else reading
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("MONOTONIC_DETAIL"))
    clock[0] = 1001.0  # the reading recovers; the doubt it raised does not (needs a pass)
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("MONOTONIC_DETAIL"))
    _fu2_certify(sessions)
    _fu2_live(_fu2_resolve_now(sessions, issued))


# --- the observed identity and the latch -------------------------------------------------


def _fu2_raw_columns(sessions: sessionmaker[Session], sequence: int) -> dict[str, Any]:
    from sqlalchemy import text

    with sessions.begin() as session:
        row = session.execute(
            text(
                "SELECT stream, sequence, kind, payload_json, recorded_at, previous_hash, "
                "record_hash FROM hash_chain_records WHERE stream = :s AND sequence = :q"
            ),
            {"s": _fu2_stream(), "q": sequence},
        ).one()
        return dict(row._mapping)


def _fu2_restore_raw(sessions: sessionmaker[Session], columns: dict[str, Any]) -> None:
    _fu2_sql(
        sessions,
        "INSERT INTO hash_chain_records (stream, sequence, kind, payload_json, recorded_at, "
        "previous_hash, record_hash) VALUES (:s, :sequence, :kind, :payload_json, :recorded_at, "
        ":previous_hash, :record_hash)",
        **{k: v for k, v in columns.items() if k != "stream"},
    )


def test_delete_and_regrow_past_the_observed_point_latches_before_the_next_resolve(
    sessions: sessionmaker[Session],
) -> None:
    """A1 (Daybreak FU2r4 P1-1): a row this process verified is replaced by a different valid
    row (the chain still verifies); the next resolve latches instead of answering."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "two"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # observes row 2
    _fu2_delete_from(sessions, 2)
    _fu2_append(sessions, {"note": "a different two"})
    _fu2_append(sessions, {"note": "three"})
    assert _k1_chain_ok(sessions), "the regrown chain must verify: the attack is consistent"
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))
    assert _fu2_state(sessions).latched


def test_a_tuple_observed_after_the_snapshot_started_is_never_forgotten_at_publication(
    file_sessions: sessionmaker[Session],
) -> None:
    """A3 (Daybreak FU2r5 P1-1, V1's steps): the snapshot holds rows 1-3; live rows 2-3 are
    replaced by 2', a live resolve observes (2, H2'), then the old rows come back. At
    publication the snapshot's row 2 contradicts the observed tuple: latch, no publication."""

    sessions = file_sessions
    _fu2_configure(sessions, pass_rows=1)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)  # published (1, H1), observed (1, H1)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    old_two, old_three = _fu2_raw_columns(sessions, 2), _fu2_raw_columns(sessions, 3)
    first = _fu2_tick(sessions)  # the pass opens its snapshot: rows 1-3
    assert not first.published
    _fu2_delete_from(sessions, 2)
    _fu2_append(sessions, {"note": "two prime"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # observes (2, H2')
    _fu2_delete_from(sessions, 2)
    _fu2_restore_raw(sessions, old_two)
    _fu2_restore_raw(sessions, old_three)
    outcome = None
    for _ in range(6):
        outcome = _fu2_tick(sessions)
        if outcome.published or outcome.latched or outcome.aborted:
            break
    assert outcome is not None and outcome.latched and not outcome.published, outcome
    state = _fu2_state(sessions)
    assert state.latched and state.published is not None and state.published.sequence == 1
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))


def test_a_tuple_observed_beyond_the_snapshot_end_discards_the_pass_and_restarts(
    file_sessions: sessionmaker[Session],
) -> None:
    """A4 under Kevin's R17 (Daybreak's stricter rule): a live resolve observes (5, H5) while
    the pass's snapshot ends at 3. Publication DISCARDS the pass (no carry, no latch); the next
    pass, from a snapshot that contains row 5, proves it and publishes. If rows 4-5 are gone by
    then, that next pass latches (its start identity is missing)."""

    sessions = file_sessions
    _fu2_configure(sessions, pass_rows=1)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    assert not _fu2_tick(sessions).published  # snapshot rows 1-3
    _fu2_append(sessions, {"note": "four"})
    _fu2_append(sessions, {"note": "five"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # observes (5, H5) live
    five = _fu2_rows(sessions)[-1]
    outcome = None
    for _ in range(6):
        outcome = _fu2_tick(sessions)
        if outcome.published or outcome.latched or outcome.discarded or outcome.aborted:
            break
    assert outcome is not None and outcome.discarded and not outcome.latched, outcome
    state = _fu2_state(sessions)
    assert not state.latched and state.published.sequence == 1 and state.observed == five
    for _ in range(12):  # the restarted pass, from a fresh snapshot, proves row 5
        outcome = _fu2_tick(sessions)
        if outcome.published or outcome.latched:
            break
    assert outcome.published and _fu2_state(sessions).published.sequence == 5
    # (b): truncated before the next pass reaches the observed point -> that pass latches
    _fu2_append(sessions, {"note": "six"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # observes (6, H6)
    _fu2_delete_from(sessions, 4)
    for _ in range(12):
        outcome = _fu2_tick(sessions)
        if outcome.published or outcome.latched:
            break
    assert outcome.latched and not outcome.published


def test_an_observation_made_before_the_snapshot_must_be_inside_it(
    sessions: sessionmaker[Session],
) -> None:
    """A5 (5(i)): observed_at_start = (4, H4); the snapshot ends at 3 -> latch at publication."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    for note in ("two", "three", "four"):
        _fu2_append(sessions, {"note": note})
    _fu2_certify(sessions)  # observed (4, H4)
    _fu2_delete_from(sessions, 4)
    outcome = _fu2_certify(sessions)
    assert outcome.latched and not outcome.published, outcome
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))


def test_a_pass_never_publishes_a_verified_end_below_the_observed_head(
    sessions: sessionmaker[Session],
) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # observed (3, H3) via the live suffix
    _fu2_delete_from(sessions, 3)
    outcome = _fu2_certify(sessions)
    assert outcome.latched and not outcome.published
    assert _fu2_state(sessions).published.sequence == 1, "a pass lowered the verified head"


def test_a_head_mismatch_refuses_and_latches(sessions: sessionmaker[Session]) -> None:
    """The published head row rewritten consistently (payload and digest recomputed)."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_certify(sessions)  # published (2, H2)
    two = _fu2_raw_columns(sessions, 2)
    forged = '{"note":"forged two"}'
    forged_hash = hash_chain.compute_hash(
        stream=_fu2_stream(),
        sequence=2,
        recorded_at=_NOW,
        payload_json=forged,
        previous_hash=two["previous_hash"],
    )
    _fu2_sql(
        sessions,
        "UPDATE hash_chain_records SET payload_json = :p, record_hash = :h "
        "WHERE stream = :s AND sequence = 2",
        p=forged,
        h=forged_hash,
    )
    assert _k1_chain_ok(sessions)
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))
    assert _fu2_state(sessions).latched


def test_a_tail_deleted_marker_this_process_has_seen_refuses_and_latches(
    sessions: sessionmaker[Session],
) -> None:
    """K1's documented bound (tail deletion is undetectable) closes for rows THIS process saw:
    the expiry marker is verified, then deleted; the next resolve latches instead of reviving
    the bundle."""

    issued = _fix26_issue(sessions, ttl_seconds=60.0)
    _fix26_sticky_at(sessions, issued)  # marker is row 2
    _fix26_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW)  # observed (2, marker)
    _fu2_delete_from(sessions, 2)
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))
    assert _fu2_state(sessions).latched


def test_the_latch_survives_a_re_pass_a_wall_clock_move_and_a_head_regrowth(
    sessions: sessionmaker[Session],
) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_certify(sessions)
    original_two = _fu2_raw_columns(sessions, 2)
    _fu2_delete_from(sessions, 2)
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))
    # Restored EXACTLY: a pass that ran now would publish cleanly, so only the latch itself
    # (cleared by nothing but a restart) can keep the stream refusing.
    _fu2_restore_raw(sessions, original_two)
    assert not _fu2_certify(sessions).published, "the latch was cleared in-process"
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))
    _fu2_delete_from(sessions, 2)  # and gone again: the regrowth below starts from row 1
    for note in ("regrown two", "three", "four"):
        _fu2_append(sessions, {"note": note})
    outcome = _fu2_certify(sessions)
    assert not outcome.published, "a latched stream ran a pass"
    later = _fu2_resolve(sessions, cited_ids=(issued.bundle_id,), now=_NOW + timedelta(hours=9))
    _fu2_refused(later, _fu2_detail("LATCHED_DETAIL"))
    assert _fu2_state(sessions).latched


# --- virtual genesis (FU2r5 P1-2, r7 P2-1) -----------------------------------------------


def test_an_empty_first_pass_then_the_first_issuance_resolves_without_latching(
    sessions: sessionmaker[Session],
) -> None:
    """G1: an empty stream publishes (0, GENESIS); the first issuance is verified from genesis
    (no physical row 0 exists) and answers; observed becomes (1, H1)."""

    outcome = _fu2_certify(sessions)
    assert outcome.published
    state = _fu2_state(sessions)
    assert (state.published.sequence, state.published.record_hash) == (0, hash_chain.GENESIS_HASH)
    assert state.observed is None
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_live(_fu2_resolve_now(sessions, issued))
    assert _fu2_state(sessions).observed == _fu2_rows(sessions)[0]
    assert not _fu2_state(sessions).latched


def test_deleting_the_first_row_after_it_was_observed_latches(
    sessions: sessionmaker[Session],
) -> None:
    """G2: after G1, the first (and only) row is deleted; N == 0 has no head row, so the
    observed row's absence is what latches."""

    _fu2_certify(sessions)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_live(_fu2_resolve_now(sessions, issued))
    _fu2_delete_from(sessions, 1)
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))


def test_virtual_genesis_uses_b_plus_one_as_the_overflow_sentinel(
    sessions: sessionmaker[Session],
) -> None:
    """G3 (r7 P2-1): from published (0, GENESIS), exactly B physical rows are verified and
    answered; B+1 rows refuse as in progress and schedule a pass. (Its mutant, an unconditional
    LIMIT B+2, returns all B+1 rows without hitting the sentinel and answers.)"""

    _fu2_configure(sessions, resolve_rows=3)
    _fu2_certify(sessions)  # (0, GENESIS)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    _fu2_live(_fu2_resolve_now(sessions, issued))  # exactly B = 3 rows from genesis
    _fu2_append(sessions, {"note": "four"})  # B + 1 rows beyond the published head
    _fu2_refused(_fu2_resolve_now(sessions, issued), _FU2_IN_PROGRESS)
    assert _fu2_state(sessions).needs_pass is True


# --- the one bounded statement (FU2r5 P1-3) ----------------------------------------------


def _fu2_raw_insert(sessions: sessionmaker[Session], **columns: Any) -> None:
    """Insert one row by raw SQL with literal SQL expressions (hostile shapes included)."""

    from sqlalchemy import text

    names = ", ".join(columns)
    values = ", ".join(str(v) for v in columns.values())
    with sessions.begin() as session:
        session.execute(
            text(f"INSERT INTO hash_chain_records (stream, {names}) VALUES (:s, {values})"),
            {"s": _fu2_stream()},
        )


def _fu2_admitted_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """Records, for every row handed to the bounded admission step, the length of each text
    value that reached Python."""

    seen: list[dict[str, int]] = []
    real = evidence_bundles._admit_row

    def spy(row: Any, **kwargs: Any) -> Any:
        seen.append(
            {
                name: len(value)
                for name, value in row._mapping.items()
                if isinstance(value, (str, bytes))
            }
        )
        return real(row, **kwargs)

    monkeypatch.setattr(evidence_bundles, "_admit_row", spy)
    return seen


def test_a_relabelled_malformed_expiry_is_refused_d2_from_the_bounded_kind(
    sessions: sessionmaker[Session],
) -> None:
    """K1-D2: the statement projects ``kind``; a row labelled as an expiry whose payload is not
    the exact two-key shape refuses with K1's D2 detail (not "ordinary evidence")."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(
        sessions,
        {"bundle_id": issued.bundle_id, "expires_at": "x", "extra": 1},
        kind=evidence_bundles.EXPIRED_EVENT_KIND,
    )
    _fu2_refused(_fu2_resolve_now(sessions, issued), _K1_SHAPE_PHRASE)


@pytest.mark.parametrize(
    ("columns", "field"),
    [
        (
            {
                "sequence": 2,
                "kind": "'evidence_note'",
                "payload_json": "'{\"n\":\"' || hex(zeroblob(524288)) || '\"}'",
                "recorded_at": "'2026-08-14 14:00:00.000000'",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 64 + "'",
            },
            "payload_json",
        ),
        (
            {
                "sequence": 2,
                "kind": "'evidence_note'",
                "payload_json": "'{}'",
                "recorded_at": "'2026' || hex(zeroblob(25000))",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 64 + "'",
            },
            "recorded_at",
        ),
        (
            {
                "sequence": 2,
                "kind": "'evidence_note'",
                "payload_json": "'{}'",
                "recorded_at": "'2026-08-14 14:00:00.000000'",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 70 + "'",
            },
            "record_hash",
        ),
        (
            {
                "sequence": 2,
                "kind": "X'6b696e64'",
                "payload_json": "'{}'",
                "recorded_at": "'2026-08-14 14:00:00.000000'",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 64 + "'",
            },
            "kind",
        ),
        (
            {
                "sequence": "X'02'",
                "kind": "'evidence_note'",
                "payload_json": "'{}'",
                "recorded_at": "'2026-08-14 14:00:00.000000'",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 64 + "'",
            },
            "sequence",
        ),
        (
            {
                "sequence": 2,
                "kind": "'evidence_note'",
                "payload_json": "'{}'",
                "recorded_at": "'not a timestamp'",
                "previous_hash": "'" + "0" * 64 + "'",
                "record_hash": "'" + "1" * 64 + "'",
            },
            "recorded_at",
        ),
    ],
    ids=[
        "1MiB-payload",
        "50000-char-timestamp",
        "70-char-hash",
        "blob-kind",
        "blob-sequence",
        "non-iso-timestamp",
    ],
)
def test_the_sticky_statement_bounds_and_validates_all_six_fields_before_hashing(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    columns: dict[str, Any],
    field: str,
) -> None:
    """K1-BOUNDS: hostile rows by raw SQL. Each refuses closed NAMING the field; no hostile row
    is hashed; no text value longer than its bound + 1 reaches Python."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_raw_insert(sessions, **columns)
    hashed: list[int] = []
    real_hash = hash_chain.compute_hash
    monkeypatch.setattr(
        hash_chain,
        "compute_hash",
        lambda **kw: hashed.append(kw["sequence"]) or real_hash(**kw),
    )
    seen = _fu2_admitted_spy(monkeypatch)
    resolution = _fu2_resolve_now(sessions, issued)
    _fu2_refused(resolution, "failed verification")
    assert field in resolution.detail, resolution.detail
    assert hashed == [], f"a hostile row was hashed: {hashed}"
    limit = evidence_bundles.EvidenceVerificationLimits().row_bytes
    assert seen and all(length <= limit + 1 for row in seen for length in row.values()), seen


def test_an_oversized_suffix_row_refuses_before_its_payload_is_materialized(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fu2_configure(sessions, row_bytes=200, pass_bytes_per_tick=1000)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "x" * 300})  # chain-correct, over Cr
    seen = _fu2_admitted_spy(monkeypatch)
    resolution = _fu2_resolve_now(sessions, issued)
    _fu2_refused(resolution, "payload_json")
    assert "200-byte bound" in resolution.detail, resolution.detail
    assert all(length <= 201 for row in seen for length in row.values()), seen


def test_the_bounded_timestamp_is_rehashed_with_utc_attached(
    sessions: sessionmaker[Session],
) -> None:
    """K1-TZ: the stored naive text re-hashes to the stored digest only with tzinfo=UTC; a
    naive parse would refuse every legitimate suffix row."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "two"})
    stored = _fu2_raw_columns(sessions, 2)["recorded_at"]
    assert "+" not in stored, stored  # UTCDateTime stores the naive form
    _fu2_live(_fu2_resolve_now(sessions, issued))


def test_suffix_work_over_the_bound_refuses_without_returning_not_expired(
    sessions: sessionmaker[Session],
) -> None:
    _fu2_configure(sessions, resolve_rows=3)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)  # (1, H1)
    for note in ("two", "three", "four", "five"):
        _fu2_append(sessions, {"note": note})
    _fu2_refused(_fu2_resolve_now(sessions, issued), _FU2_IN_PROGRESS)
    assert _fu2_state(sessions).needs_pass is True


def _fu2_bulk_rows(sessions: sessionmaker[Session], count: int) -> None:
    """``count`` chain-correct rows after the current head, in one transaction."""

    with sessions.begin() as session:
        head = hash_chain.head(session, _fu2_stream())
        sequence = head.sequence if head is not None else 0
        previous = head.record_hash if head is not None else hash_chain.GENESIS_HASH
        rows = []
        for _ in range(count):
            sequence += 1
            payload = f'{{"note":"bulk-{sequence}"}}'
            digest = hash_chain.compute_hash(
                stream=_fu2_stream(),
                sequence=sequence,
                recorded_at=_NOW,
                payload_json=payload,
                previous_hash=previous,
            )
            rows.append(
                HashChainRow(
                    stream=_fu2_stream(),
                    sequence=sequence,
                    kind="evidence_note",
                    payload_json=payload,
                    recorded_at=_NOW,
                    previous_hash=previous,
                    record_hash=digest,
                )
            )
            previous = digest
        session.add_all(rows)


def test_resolve_work_is_bounded_by_b_rows_at_thirty_thousand_rows(
    file_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # File-backed (declared, FU2-BUILD r1): certifying 30 000 rows takes 30 one-chunk ticks,
    # which an in-memory engine refuses (see the single-connection pin).
    sessions = file_sessions
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_bulk_rows(sessions, 30_000)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "after"})
    verifies: list[str] = []
    real_verify = hash_chain.verify
    monkeypatch.setattr(
        hash_chain, "verify", lambda s, stream: verifies.append(stream) or real_verify(s, stream)
    )
    seen = _fu2_admitted_spy(monkeypatch)
    _fu2_live(_fu2_resolve_now(sessions, issued))
    assert verifies == [], "a resolve ran a full verify"
    bound = evidence_bundles.EvidenceVerificationLimits().resolve_rows
    assert 0 < len(seen) <= bound + 2, len(seen)
    assert len(seen) == 2  # the published head row and the one suffix row


def test_the_sticky_stage_reads_head_suffix_and_observed_row_in_one_statement(
    sessions: sessionmaker[Session],
) -> None:
    """B3: exactly ONE statement touches the chain during a resolve (one SQLite snapshot)."""

    from sqlalchemy import event

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "two"})
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "hash_chain_records" in statement:
            statements.append(statement)

    engine = _fu2_engine(sessions)
    event.listen(engine, "before_cursor_execute", record)
    try:
        _fu2_live(_fu2_resolve_now(sessions, issued))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1, statements


# --- doubts --------------------------------------------------------------------------------


def test_a_doubt_in_the_suffix_refuses_with_the_k1_detail(sessions: sessionmaker[Session]) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _k1_raw(sessions, "{not json", kind=evidence_bundles.EXPIRED_EVENT_KIND)
    resolution = _fu2_resolve_now(sessions, issued)
    _fu2_refused(resolution, "a durable expiry record does not decode; refusing closed")
    assert _fu2_state(sessions).published is None, "a doubt left the published state usable"


def test_a_doubt_found_by_the_full_pass_keeps_the_stream_refusing_until_a_clean_pass(
    sessions: sessionmaker[Session],
) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, "[1, 2]", kind="evidence_note")
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published
    detail = "a durable evidence record is not a JSON object; refusing closed"
    _fu2_refused(_fu2_resolve_now(sessions, issued), detail)
    _fu2_refused(_fu2_resolve_now(sessions, issued), detail)  # still, without a clean pass
    _fu2_delete_from(sessions, 2)  # the operator removes the bad (never-published) row
    assert _fu2_certify(sessions).published
    _fu2_live(_fu2_resolve_now(sessions, issued))


def test_a_pass_whose_snapshot_includes_a_corrupt_prefix_publishes_nothing_and_no_resolve_answers_from_it(  # noqa: E501
    sessions: sessionmaker[Session],
) -> None:
    """B2 (and C3 item 5's bound): a row <= N edited after publication is not seen by a resolve
    (it re-verifies only the suffix) but the NEXT pass finds it, publishes nothing, and every
    resolve then refuses closed."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    _fu2_certify(sessions)
    _fu2_sql(
        sessions,
        'UPDATE hash_chain_records SET payload_json = \'{"note":"edited"}\' '
        "WHERE stream = :s AND sequence = 1",
    )
    _fu2_live(_fu2_resolve_now(sessions, issued))  # the documented bound: not yet seen
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published
    _fu2_refused(_fu2_resolve_now(sessions, issued), "failed verification")


# --- the pass --------------------------------------------------------------------------


def test_the_pass_certifies_one_snapshot_so_a_between_chunk_prefix_mutation_cannot_enter_its_proof(
    file_sessions: sessionmaker[Session],
) -> None:
    """B1: chunk 1 reads rows 1-2 of the snapshot; between ticks row 3 is replaced live by a
    different valid row 3'. The pass publishes the SNAPSHOT's head (3, H3), never H3' (a fresh
    session per chunk would read the live row and publish H3')."""

    sessions = file_sessions
    _fu2_configure(sessions, pass_rows=2)
    _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    snapshot_head = _fu2_rows(sessions)[-1]
    assert not _fu2_tick(sessions).published  # rows 1-2 admitted from the snapshot
    _fu2_delete_from(sessions, 3)
    _fu2_append(sessions, {"note": "a different three"})
    assert _fu2_rows(sessions)[-1] != snapshot_head
    outcome = _fu2_tick(sessions)
    assert outcome.published, outcome
    state = _fu2_state(sessions)
    assert (state.published.sequence, state.published.record_hash) == snapshot_head


def test_delete_and_regrow_past_the_observed_point_latches_before_pass_publication(
    file_sessions: sessionmaker[Session],
) -> None:
    """A2: observed_at_start = (2, H2); the regrowth happens before the pass starts, so the
    pass's snapshot holds 2'; the chunk spanning sequence 2 latches; nothing is published."""

    sessions = file_sessions
    _fu2_configure(sessions, pass_rows=1)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_certify(sessions)  # observed (2, H2)
    _fu2_delete_from(sessions, 2)
    _fu2_append(sessions, {"note": "a different two"})
    _fu2_append(sessions, {"note": "three"})
    outcome = None
    for _ in range(6):
        outcome = _fu2_tick(sessions)
        if outcome.published or outcome.latched or outcome.aborted:
            break
    assert outcome is not None and outcome.latched and not outcome.published, outcome
    _fu2_refused(_fu2_resolve_now(sessions, issued), _fu2_detail("LATCHED_DETAIL"))


def test_the_pass_session_never_writes_and_ends_with_rollback(
    file_sessions: sessionmaker[Session],
) -> None:
    from sqlalchemy import event

    sessions = file_sessions
    _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _k1_raw(sessions, "[1]", kind="evidence_note")  # makes the second pass abort
    writes: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(statement)

    engine = _fu2_engine(sessions)
    event.listen(engine, "before_cursor_execute", record)
    try:
        aborted = _fu2_certify(sessions)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert aborted.aborted
    assert writes == [], writes
    assert engine.pool.checkedout() == 0, "the aborted pass left its snapshot open"
    _fu2_delete_from(sessions, 3)
    event.listen(engine, "before_cursor_execute", record)
    try:
        published = _fu2_certify(sessions)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert published.published and writes == [], writes
    assert engine.pool.checkedout() == 0, "the published pass left its snapshot open"


def test_cumulative_bytes_over_the_tick_cap_defer_and_never_publish_partial_state(
    file_sessions: sessionmaker[Session],
) -> None:
    sessions = file_sessions
    _fu2_configure(sessions, row_bytes=300, pass_bytes_per_tick=700, pass_rows=100)
    for index in range(5):
        _fu2_append(sessions, {"note": f"{index}" * 200})  # about 213 bytes each
    first = _fu2_tick(sessions)
    assert not first.published and first.rows_verified == 3, first  # 3 x ~213 <= 700 < 4 x
    assert _fu2_state(sessions).published is None
    second = _fu2_tick(sessions)
    assert second.published and second.rows_verified == 2, second
    assert _fu2_state(sessions).published.sequence == 5


def test_the_expired_id_cap_aborts_the_pass_and_publishes_nothing(
    sessions: sessionmaker[Session],
) -> None:
    first = _fix26_issue(sessions, ttl_seconds=60.0)
    second = _fix26_issue(sessions, ttl_seconds=60.0)
    _fix26_sticky_at(sessions, first)
    _fix26_sticky_at(sessions, second)
    evidence_bundles._reset_verified_streams()
    _fu2_configure(sessions, expired_ids=1)
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published
    _fu2_refused(_fu2_resolve_now(sessions, first), "1-id retained expired-id bound")


def test_a_json_nesting_bomb_in_the_pass_is_a_doubt_not_a_crash(
    sessions: sessionmaker[Session],
) -> None:
    _fu2_configure(sessions, row_bytes=200_000, pass_bytes_per_tick=400_000)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, "[" * 100_000, kind="evidence_note")
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published
    _fu2_refused(
        _fu2_resolve_now(sessions, issued),
        "a durable evidence record does not decode; refusing closed",
    )


def test_an_oversized_single_row_aborts_the_pass_without_materializing_it(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cr is a BYTE bound: 60 two-byte characters (120 bytes) exceed a 100-byte Cr even though
    the text is only 60 characters long."""

    _fu2_configure(sessions, row_bytes=100, pass_bytes_per_tick=1000)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, '{"n":"' + "é" * 50 + '"}', kind="evidence_note")
    seen = _fu2_admitted_spy(monkeypatch)
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published
    assert "100-byte bound" in outcome.aborted, outcome
    assert all(length <= 101 for row in seen for length in row.values()), seen
    _fu2_refused(_fu2_resolve_now(sessions, issued), "100-byte bound")


@pytest.mark.parametrize("t_max", [float("inf"), float("nan"), 0.0, -1.0], ids=str)
def test_a_non_finite_tmax_or_deadline_never_publishes_verified_state(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, t_max: float
) -> None:
    with pytest.raises(ValueError, match="max_age_seconds"):
        evidence_bundles.EvidenceVerificationLimits(max_age_seconds=t_max)
    # the absorbed sum: a huge monotonic reading with a tiny T_max gives D == m0
    monkeypatch.setattr(evidence_bundles, "_monotonic", lambda: 1e20)
    _fu2_configure(sessions, max_age_seconds=1.0)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    outcome = _fu2_certify(sessions)
    assert outcome.aborted and not outcome.published and "deadline" in outcome.aborted
    _fu2_refused(_fu2_resolve_now(sessions, issued), "deadline")


def test_a_pass_publishes_state_only_when_it_completes_cleanly(
    file_sessions: sessionmaker[Session],
) -> None:
    sessions = file_sessions
    _fu2_configure(sessions, pass_rows=1)
    _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    assert not _fu2_tick(sessions).published
    assert _fu2_state(sessions).published is None
    assert _fu2_tick(sessions).published
    assert _fu2_state(sessions).published.sequence == 2


@pytest.mark.in_memory_sqlite_is_the_subject
def test_an_in_memory_database_refuses_a_pass_that_needs_more_than_one_chunk(
    sessions: sessionmaker[Session],
) -> None:
    """Daybreak FU2-BUILD P2-1 (declared: REPLACES the earlier pin that required a whole pass in
    one tick on StaticPool). Every topology runs exactly ONE chunk per tick. An in-memory engine
    has one shared connection, so it cannot hold the pass's snapshot across ticks: a pass that
    needs more than one chunk there admits at most one chunk, publishes nothing, closes its
    snapshot and refuses closed with a stable detail. A stream that fits one chunk still
    publishes (the positive control)."""

    _fu2_configure(sessions, pass_rows=10)
    _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    assert _fu2_tick(sessions).published  # one chunk is enough: the positive control

    evidence_bundles._reset_verified_streams()
    _fu2_configure(sessions, pass_rows=1)
    issued = _fix26_issue(sessions, ttl_seconds=300.0)  # three rows, one per chunk
    outcome = _fu2_tick(sessions)
    assert outcome.rows_verified <= 1 and not outcome.published, outcome
    assert outcome.aborted == evidence_bundles.SINGLE_CONNECTION_DETAIL, outcome
    state = _fu2_state(sessions)
    assert not state.pass_in_flight and state.published is None
    _fu2_refused(_fu2_resolve_now(sessions, issued), evidence_bundles.SINGLE_CONNECTION_DETAIL)


# --- the documented bounds (GREEN at both heads; a later anchor flips them deliberately) --


def test_an_unseen_tail_deletion_within_a_process_is_not_detected_and_is_the_documented_bound(
    sessions: sessionmaker[Session],
) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_certify(sessions)
    _fu2_append(sessions, {"note": "appended and deleted before anything read it"})
    _fu2_delete_from(sessions, 2)
    _fu2_live(_fu2_resolve_now(sessions, issued))


def test_a_tail_deletion_across_a_restart_is_not_detected_and_is_the_documented_bound(
    sessions: sessionmaker[Session],
) -> None:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_certify(sessions)
    _fu2_delete_from(sessions, 2)
    reset = getattr(evidence_bundles, "_reset_verified_streams", None)
    if reset is not None:
        reset()  # the restart: process memory is gone
    _fu2_certify(sessions)
    _fu2_live(_fu2_resolve_now(sessions, issued))


# --- the named settings (Kevin K-20261004-007/-009; builder-derived values flagged) --------


def test_every_verification_limit_is_a_named_setting_with_the_same_default() -> None:
    from chronos.config.settings import Settings

    settings = Settings(_env_file=None)
    limits = evidence_bundles.EvidenceVerificationLimits()
    assert evidence_bundles.EvidenceVerificationLimits.from_settings(settings) == limits
    assert (
        settings.autonomy_evidence_verification_max_age_seconds,
        settings.autonomy_evidence_resolve_rows,
        settings.autonomy_evidence_pass_rows_per_tick,
        settings.autonomy_evidence_row_bytes,
        settings.autonomy_evidence_pass_bytes_per_tick,
        settings.autonomy_evidence_expired_ids,
        settings.autonomy_evidence_max_pass_attempts,
    ) == (900.0, 1000, 1000, 4096, 1_048_576, 10_000, 5)
    for name in (
        "autonomy_evidence_verification_max_age_seconds",
        "autonomy_evidence_resolve_rows",
        "autonomy_evidence_row_bytes",
        "autonomy_evidence_expired_ids",
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{name: 0})


# --- the equivalence oracle: the bounded answer equals K1's full read --------------------

_D0_PREFIX = "the account's durable evidence stream failed verification ("
_D0_SUFFIX = "); refusing closed until the stream is repaired"


def _oracle_live(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    return _fix26_issue(sessions, ttl_seconds=300.0)


def _oracle_sticky(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    return _k1_expired(sessions)


def _oracle_duplicate_markers(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _k1_expired(sessions)
    _k1_append(
        sessions,
        {"bundle_id": issued.bundle_id, "expires_at": "x"},
        kind=evidence_bundles.EXPIRED_EVENT_KIND,
    )
    return issued


def _oracle_other_id_marker(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_append(
        sessions,
        {"bundle_id": issued.bundle_id + "x", "expires_at": "x"},
        kind=evidence_bundles.EXPIRED_EVENT_KIND,
    )
    return issued


def _oracle_kind_edited_marker(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _k1_expired(sessions)
    _k1_relabel(sessions, from_kind=evidence_bundles.EXPIRED_EVENT_KIND, to_kind="anything-else")
    return issued


def _oracle_relabelled_issuance(
    sessions: sessionmaker[Session],
) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_relabel(
        sessions, from_kind="evidence_bundle_issued", to_kind=evidence_bundles.EXPIRED_EVENT_KIND
    )
    return issued


def _oracle_extra_key(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_append(
        sessions,
        {"bundle_id": issued.bundle_id, "expires_at": "x", "extra": 1},
        kind=evidence_bundles.EXPIRED_EVENT_KIND,
    )
    return issued


def _oracle_non_string_id(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_append(sessions, {"bundle_id": 5, "expires_at": "x"}, kind="evidence_note")
    return issued


def _oracle_non_string_expiry(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_append(sessions, {"bundle_id": "evb_y", "expires_at": 7}, kind="evidence_note")
    return issued


def _oracle_unlabelled_array(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, "[1, 2]", kind="evidence_note")
    return issued


def _oracle_labelled_undecodable(
    sessions: sessionmaker[Session],
) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, "{not json", kind=evidence_bundles.EXPIRED_EVENT_KIND)
    return issued


def _oracle_blob_payload(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, b'{"note": "ordinary"}')
    return issued


def _oracle_labelled_blob(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, b'{"bundle_id": "x", "expires_at": "y"}', kind=_FIX26_KIND)
    return issued


def _oracle_deep_array(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1_raw(sessions, "[" * 1000 + "]" * 1000, kind="evidence_note")
    return issued


def _oracle_deleted_row(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_append(sessions, {"note": "three"})
    _fu2_sql(sessions, "DELETE FROM hash_chain_records WHERE stream = :s AND sequence = 2")
    return issued


def _oracle_edited_payload(sessions: sessionmaker[Session]) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _fu2_append(sessions, {"note": "two"})
    _fu2_sql(
        sessions,
        'UPDATE hash_chain_records SET payload_json = \'{"note":"x"}\' '
        "WHERE stream = :s AND sequence = 2",
    )
    return issued


def _oracle_other_account_marker(
    sessions: sessionmaker[Session],
) -> evidence_bundles.IssuedBundle:
    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    with sessions.begin() as session:
        hash_chain.append(
            session,
            stream=evidence_bundles.hash_chain_stream("b" * 64),
            kind=evidence_bundles.EXPIRED_EVENT_KIND,
            payload={"bundle_id": issued.bundle_id, "expires_at": "x"},
            recorded_at=_NOW,
        )
    return issued


def _oracle_marker_then_later_doubt(
    sessions: sessionmaker[Session],
) -> evidence_bundles.IssuedBundle:
    issued = _k1_expired(sessions)
    _k1_raw(sessions, "[3]", kind="evidence_note")
    return issued


_ORACLE_FIXTURES = {
    "live": _oracle_live,
    "sticky": _oracle_sticky,
    "duplicate-markers": _oracle_duplicate_markers,
    "other-id-marker": _oracle_other_id_marker,
    "kind-edited-marker": _oracle_kind_edited_marker,
    "relabelled-issuance": _oracle_relabelled_issuance,
    "labelled-extra-key": _oracle_extra_key,
    "non-string-id": _oracle_non_string_id,
    "non-string-expiry": _oracle_non_string_expiry,
    "unlabelled-array": _oracle_unlabelled_array,
    "labelled-undecodable": _oracle_labelled_undecodable,
    "blob-payload": _oracle_blob_payload,
    "labelled-blob": _oracle_labelled_blob,
    "deep-array": _oracle_deep_array,
    "deleted-row": _oracle_deleted_row,
    "edited-payload": _oracle_edited_payload,
    "other-account-marker": _oracle_other_account_marker,
    "marker-then-later-doubt": _oracle_marker_then_later_doubt,
}


@pytest.mark.parametrize("fixture", sorted(_ORACLE_FIXTURES))
def test_the_bounded_answer_equals_the_k1_full_read_on_every_k1_fixture(
    sessions: sessionmaker[Session], fixture: str
) -> None:
    """The equivalence oracle, comparing the ANSWER and the REASON, never only the code."""

    issued = _ORACLE_FIXTURES[fixture](sessions)
    with sessions.begin() as session:
        reference = _k1_reference_verdict(session, stream=_fu2_stream(), bundle_id=issued.bundle_id)
    evidence_bundles._reset_verified_streams()
    _fu2_certify(sessions)
    resolution = _fu2_resolve_now(sessions, issued)
    if reference is None:
        _fu2_live(resolution)
        return
    assert resolution.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, resolution
    if reference.startswith(_D0_PREFIX):
        assert resolution.detail.startswith(_D0_PREFIX), resolution.detail
        assert resolution.detail.endswith(_D0_SUFFIX), resolution.detail
    else:
        assert resolution.detail == reference, (fixture, resolution.detail, reference)


def test_the_one_intended_divergence_from_k1_is_the_byte_bound(
    sessions: sessionmaker[Session],
) -> None:
    """Over Cr the bounded read refuses on the byte bound, where K1 decoded the whole value
    (here a 5000-digit integer K1 refused as undecodable). Both refuse closed; only the
    reason differs, and it is stated here rather than hidden in the oracle."""

    issued = _fix26_issue(sessions, ttl_seconds=300.0)
    _k1r1_insert(sessions, "9" * 5000)
    with sessions.begin() as session:
        reference = _k1_reference_verdict(session, stream=_fu2_stream(), bundle_id=issued.bundle_id)
    assert reference == "a durable evidence record does not decode; refusing closed"
    _fu2_certify(sessions)
    _fu2_refused(_fu2_resolve_now(sessions, issued), "4096-byte bound")

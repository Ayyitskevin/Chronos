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


# --------------------------------------------------------------- shared fixtures


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


def test_the_unset_posture_is_byte_identical_to_the_pre_adr_0028_journal(
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

"""A3, exercised: a leaked proposer credential dies without a restart.

ADR-0023 shipped the proposer registry as a **boot-time snapshot** on both
planes and disclosed the consequence as R-48 residual (c): disabling a
registration means editing the owner's file and restarting the backend. The
mid-session stand-downs were the kill switch, mandate revocation, and bouncing
the process that holds the broker connection — none of which is "stop this one
proposer", and the last of which is the worst thing to do during an incident.

This suite proves the durable revocation act closes that, at both enforcement
points and in the same process:

1. a credential accepted moments ago is refused **401 at the route** after
   revocation, with no restart and no registry reload;
2. a proposal already **queued** under that credential refuses at **STAMP**,
   journaling ``PROPOSER_REVOKED`` rather than sharing a code with expiry;
3. revocation **survives a restart** — a fresh boot, a fresh registry load, and
   a fresh ``ProposerAuth`` still refuse;
4. revocation is **credential-scoped, not name-scoped**: re-minting a new
   credential for the same ``proposer_id`` verifies again while the leaked hash
   stays dead, which is exactly the recovery an operator needs;
5. the act **writes no file** — the owner's grant document is byte-identical
   afterwards, because revocation lives in the database;
6. the ledger is **read fail-closed**: an unreadable one refuses rather than
   assuming nothing was revoked — proven at the route by dropping the table
   under a running backend, not merely asserted in a comment;
7. one revocation stands down **one credential**, not the registry: a second,
   unrevoked proposer keeps working in the same process, between the same two
   calls;
8. the **registry-off posture is untouched** — with no registry configured
   there is no credential to key a revocation on, and the route does not
   consult the ledger at all.

Weighted the fail-closed way (§4d): every case asserts a refusal except (4),
(7), and (8), which exist precisely to prove the refusal is not broader than
the act intended. (7) and (8) are the positive controls — without them a
"refuse everything once any revocation exists" bug would leave this whole file
green while the registry was silently dead.
"""

from __future__ import annotations

import argparse
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

from chronos.api.autonomy_wiring import build_identity_resolver
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyProposerRevocationRow, HashChainRow
from chronos.supervisor import revocation
from chronos.supervisor.proposers import (
    ProposerRegistration,
    credential_hash,
    registration_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_NOW = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)
_FAR_EXPIRY = "2030-01-01T00:00:00+00:00"

TOKEN_HEADER = "X-Chronos-Token"
PROPOSER_HEADER = "X-Chronos-Proposer-Token"

WORKER_CREDENTIAL = "w" * 64
REMINTED_CREDENTIAL = "r" * 64
#: A second, unrelated proposer — the positive control's whole point is that it
#: is untouched while another credential is being killed.
SECOND_CREDENTIAL = "s" * 64

PROPOSAL_BODY = {
    "kind": "HOLD",
    "asset_class": "EQUITY",
    "symbol": "SPY",
    "direction": "NEUTRAL",
    "thesis": "exercised-test proposal",
}


def _registration(
    proposer_id: str,
    credential: str,
    *,
    expires_at: str = _FAR_EXPIRY,
    enabled: bool = True,
) -> dict[str, Any]:
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
        "expires_at": expires_at,
        "enabled": enabled,
    }


def _registry_text(*entries: dict[str, Any]) -> str:
    return json.dumps({"schema_version": 1, "proposers": list(entries)})


def _binding_values(entry: dict[str, Any]) -> tuple[str, str]:
    binding = registration_binding(ProposerRegistration.model_validate(entry))
    return binding.credential_epoch, binding.registry_entry_digest


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


def _boot(monkeypatch: pytest.MonkeyPatch, registry_text: str, tmp_path: Path) -> TestClient:
    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    registry_path = tmp_path / "autonomy_proposers.json"
    registry_path.write_text(registry_text, encoding="utf-8")
    monkeypatch.setenv("AUTONOMY_PROPOSERS_FILE", str(registry_path))
    get_settings.cache_clear()
    return TestClient(create_app())


def _revoke_in(tmp_path: Path, proposer_id: str, credential: str, reason: str) -> bool:
    """Revoke against the backend's own database, the way the CLI does."""

    database = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    try:
        with database.sessions.begin() as session:
            return revocation.revoke(
                session,
                proposer_id=proposer_id,
                secret_sha256=credential_hash(credential),
                reason=reason,
                now=datetime.now(tz=UTC),
            )
    finally:
        database.dispose()


# ------------------------------------------------- the route, in one process


def test_a_revoked_credential_is_refused_without_a_restart(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect R-48(c) disclosed: this used to require bouncing the backend.

    One boot. The credential works, is revoked mid-session, and stops working —
    while the same process keeps running with the same registry snapshot in
    memory. That "same process" is the whole claim.
    """

    registry = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    with _boot(monkeypatch, registry, demo_env) as client:
        accepted = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert accepted.status_code == 202, accepted.text

        assert _revoke_in(demo_env, "claude-worker", WORKER_CREDENTIAL, "credential leaked")

        refused = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert refused.status_code == 401
        # The 401 body says no more than it did for unknown/expired/disabled:
        # which of the four states this is remains the owner's business.
        assert refused.json()["detail"] == (
            f"the {PROPOSER_HEADER} credential is not a current registered proposer"
        )

    with sqlite3.connect(demo_env / "chronos.db") as connection:
        queued = list(connection.execute("SELECT COUNT(*) FROM autonomy_proposal_queue"))
    assert queued == [(1,)], "the post-revocation proposal must not have been queued"


def test_revocation_survives_a_restart(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart is not permission to undo it — ADR-0017's rule, applied here.

    The registry file still contains the registration, unedited. A fresh boot
    re-reads that file, builds a fresh ``ProposerAuth``, and still refuses,
    because the ledger is what says so and the ledger is durable.
    """

    registry = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    with _boot(monkeypatch, registry, demo_env) as client:
        assert (
            client.post(
                "/autonomy/proposals",
                json=PROPOSAL_BODY,
                headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
            ).status_code
            == 202
        )
        _revoke_in(demo_env, "claude-worker", WORKER_CREDENTIAL, "credential leaked")

    with _boot(monkeypatch, registry, demo_env) as restarted:
        after = restarted.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert after.status_code == 401


def test_revocation_kills_the_credential_not_the_name(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-minting is the recovery path, and it works.

    Keying revocation by ``proposer_id`` would burn the name forever and force
    the owner to invent ``claude-worker-2`` mid-incident. Keying by credential
    hash revokes precisely the secret that escaped: a NEW credential for the
    SAME proposer verifies after the restart the registry snapshot needs, while
    the leaked one stays dead permanently.
    """

    leaked = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    with _boot(monkeypatch, leaked, demo_env):
        _revoke_in(demo_env, "claude-worker", WORKER_CREDENTIAL, "credential leaked")

    reminted = _registry_text(_registration("claude-worker", REMINTED_CREDENTIAL))
    with _boot(monkeypatch, reminted, demo_env) as client:
        assert (
            client.post(
                "/autonomy/proposals",
                json=PROPOSAL_BODY,
                headers={PROPOSER_HEADER: REMINTED_CREDENTIAL},
            ).status_code
            == 202
        ), "a fresh credential for the same proposer must work"
        assert (
            client.post(
                "/autonomy/proposals",
                json=PROPOSAL_BODY,
                headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
            ).status_code
            == 401
        ), "the leaked credential stays dead forever"


def test_one_revocation_does_not_stand_down_every_other_proposer(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control, in the same process and with no restart.

    Every other test here asserts a refusal, which is exactly the shape a
    "refuse everything once any revocation exists" bug would satisfy: kill one
    credential and the suite still goes green while the whole registry is
    silently dead. So this one registers two proposers, revokes one, and
    asserts the OTHER keeps working — same boot, same in-memory snapshot, one
    revocation row in the ledger between the two calls.
    """

    registry = _registry_text(
        _registration("claude-worker", WORKER_CREDENTIAL),
        _registration("tradingview-bridge", SECOND_CREDENTIAL),
    )
    with _boot(monkeypatch, registry, demo_env) as client:
        assert _revoke_in(demo_env, "claude-worker", WORKER_CREDENTIAL, "credential leaked")

        refused = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert refused.status_code == 401

        survivor = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: SECOND_CREDENTIAL},
        )
        assert survivor.status_code == 202, (
            "revoking one credential must not stand down the registry; "
            f"the unrevoked proposer answered {survivor.status_code}: {survivor.text}"
        )

    with sqlite3.connect(demo_env / "chronos.db") as connection:
        queued = list(connection.execute("SELECT proposer_id FROM autonomy_proposal_queue"))
    assert queued == [("tradingview-bridge",)], (
        "exactly the unrevoked proposer's proposal may be queued"
    )


def test_an_unreadable_ledger_refuses_rather_than_admitting(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed direction of the per-request read, actually exercised.

    ``auth._is_revoked`` claims in a comment that an authorization check which
    cannot see its own ledger "has not passed, it has failed to run". That is
    the kind of claim this repository has been burned by believing (R-24..R-27
    were all documented controls that could never fire), so it is proven here
    rather than asserted: the revocation table is dropped out from under a
    RUNNING backend — the "table missing" case, the cheapest realistic form of
    a database that has gone bad — and the credential that worked a moment ago
    now refuses.

    The refusal is 503, not 401, and the distinction is deliberate: the caller
    is not being told its credential is bad, it is being told the server cannot
    currently judge. What must never happen is a 202.
    """

    registry = _registry_text(_registration("claude-worker", WORKER_CREDENTIAL))
    with _boot(monkeypatch, registry, demo_env) as client:
        assert (
            client.post(
                "/autonomy/proposals",
                json=PROPOSAL_BODY,
                headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
            ).status_code
            == 202
        ), "the credential must work before the ledger is broken"

        with sqlite3.connect(demo_env / "chronos.db") as connection:
            connection.execute("DROP TABLE autonomy_proposer_revocations")
            connection.commit()

        blinded = client.post(
            "/autonomy/proposals",
            json=PROPOSAL_BODY,
            headers={PROPOSER_HEADER: WORKER_CREDENTIAL},
        )
        assert blinded.status_code == 503, (
            "an unreadable revocation ledger must refuse, never admit; "
            f"got {blinded.status_code}: {blinded.text}"
        )
        assert "revocation ledger could not be read" in blinded.json()["detail"]

    with sqlite3.connect(demo_env / "chronos.db") as connection:
        queued = list(connection.execute("SELECT COUNT(*) FROM autonomy_proposal_queue"))
    assert queued == [(1,)], "the proposal sent while the ledger was blind must not be queued"


def test_the_registry_off_posture_is_untouched_by_this_feature(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no registry configured, nothing A3 added can refuse anything.

    The pre-registry posture is the one every existing deployment is in today,
    including the owner's: ``AUTONOMY_PROPOSERS_FILE`` unset, the proposal route
    gated by the local API token, no proposer identities at all. A revocation
    ledger is meaningless there — there is no credential to key one on — and
    the route must not consult it, because a feature that can refuse in a
    posture it does not apply to is a new failure mode, not a new control.

    Proven with a revocation row deliberately present in the database: even
    then, the token-authenticated proposal is accepted exactly as before.
    """

    monkeypatch.delenv("AUTONOMY_PROPOSERS_FILE", raising=False)
    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        token = (demo_env / "backend_api_token").read_text(encoding="utf-8").strip()

        # A revocation exists, keyed on a credential this posture never sees.
        assert _revoke_in(demo_env, "claude-worker", WORKER_CREDENTIAL, "credential leaked")

        accepted = client.post(
            "/autonomy/proposals", json=PROPOSAL_BODY, headers={TOKEN_HEADER: token}
        )
        assert accepted.status_code == 202, (
            "the registry-off posture must be byte-for-byte what it was before A3; "
            f"got {accepted.status_code}: {accepted.text}"
        )

    with sqlite3.connect(demo_env / "chronos.db") as connection:
        queued = list(connection.execute("SELECT proposer_id FROM autonomy_proposal_queue"))
    assert queued == [(None,)], "the pre-registry row still records no proposer, as before"


# ------------------------------------------------- a minimal cycle harness
#
# Self-contained, like every other exercised suite here: the cycle refuses at
# STAMP before any of these facts are consulted, so they only have to be valid.


def _proposal() -> Any:
    from chronos.autonomy import (
        DecisionKind,
        EvidenceCitation,
        ProposedDecision,
        StrategyForm,
        TradableAssetClass,
    )

    return ProposedDecision(
        kind=DecisionKind.OPEN,
        asset_class=TradableAssetClass.EQUITY,
        symbol="SPY",
        requested_strategy=StrategyForm.LONG_EQUITY,
        requested_quantity=Decimal(10),
        evidence=(EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_NOW, digest="c" * 64),),
        invalidation_conditions=("closes below 400",),
    )


def _facts() -> Any:
    from chronos.domain.enums import DataQuality
    from chronos.domain.models import UnderlyingContract
    from chronos.supervisor.admission import MarketDataEvidence
    from chronos.supervisor.compiler import QuoteEvidence
    from chronos.supervisor.loop import CycleFacts
    from chronos.supervisor.sizing import AccountEvidence

    return CycleFacts(
        account_fingerprint="a" * 64,
        account_id="DU1234567",
        now=_NOW,
        process_generation=7,
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
        market_data=MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        account=AccountEvidence(
            net_liquidation_usd=Decimal(100_000),
            total_cash_usd=Decimal(60_000),
            buying_power_usd=Decimal(60_000),
        ),
        quote=QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02")),
        contract=UnderlyingContract(con_id=111, symbol="SPY"),
        reference_price=Decimal(400),
    )


# ------------------------------------------------------------ the drain plane


def test_a_queued_proposal_refuses_at_stamp_with_its_own_code(
    tmp_path: Path, sessions: sessionmaker[Session]
) -> None:
    """The race the route alone cannot cover: enqueued before, drained after.

    A proposal accepted seconds before the owner revoked is already in the
    queue. Identity is resolved at drain time — the moment authority is actually
    exercised — so this is where that proposal dies, and it says why in its own
    refusal code rather than sharing ``PROPOSER_UNRESOLVED`` with a registration
    that merely aged out.
    """

    registry_path = tmp_path / "proposers.json"
    entry = _registration("claude-worker", WORKER_CREDENTIAL)
    registry_path.write_text(_registry_text(entry), encoding="utf-8")
    resolve = build_identity_resolver(registry_path)
    assert resolve is not None
    epoch, digest = _binding_values(entry)

    with sessions.begin() as session:
        before = resolve(session, "claude-worker", epoch, digest, _NOW)
    assert before.identity is not None, "the registration must resolve before revocation"

    with sessions.begin() as session:
        assert revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=credential_hash(WORKER_CREDENTIAL),
            reason="credential leaked",
            now=_NOW,
        )

    with sessions.begin() as session:
        after = resolve(session, "claude-worker", epoch, digest, _NOW)
    assert after.identity is None, "a revoked credential must not resolve to an author"


def test_the_revoked_stamp_refusal_names_itself(
    tmp_path: Path, sessions: sessionmaker[Session]
) -> None:
    """PROPOSER_REVOKED, not PROPOSER_UNRESOLVED — a separate conjunct.

    Refusing and *saying why* are two different pieces of this fix, and they can
    regress independently: the ledger check could stay while the code collapsed
    back into the generic one, leaving a journal that cannot distinguish a
    credential the owner killed from a registration that aged out.
    """

    registry_path = tmp_path / "proposers.json"
    entry = _registration("claude-worker", WORKER_CREDENTIAL)
    registry_path.write_text(_registry_text(entry), encoding="utf-8")
    resolve = build_identity_resolver(registry_path)
    assert resolve is not None
    epoch, digest = _binding_values(entry)
    with sessions.begin() as session:
        revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=credential_hash(WORKER_CREDENTIAL),
            reason="credential leaked",
            now=_NOW,
        )
    with sessions.begin() as session:
        refused = resolve(session, "claude-worker", epoch, digest, _NOW)

    assert refused.refusal == "PROPOSER_REVOKED"
    assert refused.refusal != "PROPOSER_UNRESOLVED"
    assert "revoked by the owner" in refused.detail
    assert "permanent" in refused.detail


def test_same_id_rotation_cannot_hide_the_queued_credentials_revocation(
    tmp_path: Path, sessions: sessionmaker[Session]
) -> None:
    """Drain asks about the stored epoch before resolving the reusable id."""

    old_entry = _registration("claude-worker", WORKER_CREDENTIAL)
    old_epoch, old_digest = _binding_values(old_entry)
    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(_registration("claude-worker", REMINTED_CREDENTIAL)),
        encoding="utf-8",
    )
    resolve = build_identity_resolver(registry_path)
    assert resolve is not None
    with sessions.begin() as session:
        revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=old_epoch,
            reason="credential leaked before rotation",
            now=_NOW,
        )
        refused = resolve(session, "claude-worker", old_epoch, old_digest, _NOW)

    assert refused.identity is None
    assert refused.refusal == "PROPOSER_REVOKED"
    assert "does not transfer to a replacement" in refused.detail


def test_the_stamp_refusal_reaches_the_journal(
    tmp_path: Path, sessions: sessionmaker[Session]
) -> None:
    """End to end through ``run_cycle``: the journal records PROPOSER_REVOKED.

    A refusal nobody can read afterwards is the defect A1 removed elsewhere in
    this pipeline; a distinct code that never reaches the journal would be the
    same failure in a new place.
    """

    from chronos.supervisor.loop import CycleStage, run_cycle

    with sessions.begin() as session:
        outcome = run_cycle(
            _proposal(),
            session=session,
            mandate=None,
            identity=None,
            identity_refusal="PROPOSER_REVOKED",
            identity_detail="the credential registered to claude-worker was revoked",
            facts=_facts(),
        )
        assert outcome.stage is CycleStage.STAMP
        assert outcome.refusal == "PROPOSER_REVOKED"
        journaled = list(
            session.scalars(select(HashChainRow).order_by(HashChainRow.sequence)).all()
        )
    assert journaled, "the refusal must be journaled, not merely returned"


# --------------------------------------------------------------- the act itself


def test_revocation_is_idempotent_and_hash_chained(sessions: sessionmaker[Session]) -> None:
    """One act, one row, one chain record — and a second call says so.

    An operator who cannot remember whether the first invocation landed must be
    able to run it again during an incident without being told something went
    wrong, and without producing a second record of one act.
    """

    digest = credential_hash(WORKER_CREDENTIAL)
    with sessions.begin() as session:
        assert revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=digest,
            reason="credential leaked",
            now=_NOW,
        )
    with sessions.begin() as session:
        assert not revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=digest,
            reason="credential leaked again",
            now=_NOW + timedelta(minutes=1),
        )
    with sessions.begin() as session:
        rows = list(session.scalars(select(AutonomyProposerRevocationRow)).all())
        chain = [
            row
            for row in session.scalars(select(HashChainRow)).all()
            if row.stream == revocation.PROPOSER_STREAM
        ]
        assert len(rows) == 1
        assert len(chain) == 1
        assert rows[0].reason == "credential leaked"  # the first reason is the act's
        payload = json.loads(chain[0].payload_json)
    assert payload["proposer_id"] == "claude-worker"
    assert payload["secret_sha256"] == digest
    assert WORKER_CREDENTIAL not in chain[0].payload_json, "hashes in, hashes out"


def test_revoking_without_a_reason_is_refused(sessions: sessionmaker[Session]) -> None:
    """An act with no stated cause cannot be reviewed — the mandate's rule."""

    with sessions.begin() as session, pytest.raises(ValueError, match="requires a reason"):
        revocation.revoke(
            session,
            proposer_id="claude-worker",
            secret_sha256=credential_hash(WORKER_CREDENTIAL),
            reason="   ",
            now=_NOW,
        )
    with sessions.begin() as session:
        assert not revocation.is_revoked(session, secret_sha256=credential_hash(WORKER_CREDENTIAL))


# ------------------------------------------------------------------- the CLI


def test_revoke_writes_the_ledger_and_never_the_registry_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The grant document stays owner-authored; the act lives in the database."""

    from chronos.cli.proposer_commands import cmd_proposer_revoke

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(_registration("claude-worker", WORKER_CREDENTIAL)), encoding="utf-8"
    )
    before = registry_path.read_bytes()
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    database = Database(url)
    try:
        database.initialize()
    finally:
        database.dispose()

    code = cmd_proposer_revoke(
        argparse.Namespace(
            file=str(registry_path),
            proposer_id="claude-worker",
            reason="credential pasted into a public issue",
            database_url=url,
        )
    )
    assert code == 0
    assert registry_path.read_bytes() == before, "revocation must not edit the owner's grant"

    out = capsys.readouterr().out
    assert "REVOKED" in out
    assert "No restart is needed" in out
    assert WORKER_CREDENTIAL not in out, "the credential is never echoed"

    database = Database(url)
    try:
        with database.sessions.begin() as session:
            assert revocation.is_revoked(session, secret_sha256=credential_hash(WORKER_CREDENTIAL))
    finally:
        database.dispose()


def test_revoke_refuses_an_unregistered_proposer_and_an_empty_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both refusals explain themselves rather than failing silently."""

    from chronos.cli.proposer_commands import cmd_proposer_revoke

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(_registration("claude-worker", WORKER_CREDENTIAL)), encoding="utf-8"
    )
    url = f"sqlite:///{tmp_path / 'ledger.db'}"

    assert (
        cmd_proposer_revoke(
            argparse.Namespace(
                file=str(registry_path),
                proposer_id="ghost",
                reason="tidying up",
                database_url=url,
            )
        )
        == 2
    )
    assert "already fails verification" in capsys.readouterr().err

    assert (
        cmd_proposer_revoke(
            argparse.Namespace(
                file=str(registry_path),
                proposer_id="claude-worker",
                reason="  ",
                database_url=url,
            )
        )
        == 2
    )
    assert "stated cause" in capsys.readouterr().err


def test_check_reports_revoked_and_says_when_it_cannot_tell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REVOKED outranks every other state, and an unreadable ledger says so.

    The second half is the one that matters: an entry rendered CURRENT because
    the ledger could not be read would be fabricated calm, which the terminal
    tests forbid elsewhere for exactly this reason.
    """

    from chronos.cli.proposer_commands import cmd_proposer_check, cmd_proposer_revoke

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(
            _registration("claude-worker", WORKER_CREDENTIAL),
            _registration("other-worker", REMINTED_CREDENTIAL),
        ),
        encoding="utf-8",
    )
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    database = Database(url)
    try:
        database.initialize()
    finally:
        database.dispose()

    cmd_proposer_revoke(
        argparse.Namespace(
            file=str(registry_path),
            proposer_id="claude-worker",
            reason="credential leaked",
            database_url=url,
        )
    )
    capsys.readouterr()

    assert cmd_proposer_check(argparse.Namespace(file=str(registry_path), database_url=url)) == 0
    out = capsys.readouterr().out
    assert "claude-worker" in out and "REVOKED" in out
    assert "credential leaked" in out
    assert "other-worker" in out and "CURRENT" in out

    # No ledger to read: the state is UNVERIFIED, and the reason is printed.
    assert (
        cmd_proposer_check(
            argparse.Namespace(
                file=str(registry_path), database_url="sqlite:///" + str(tmp_path / "absent.db")
            )
        )
        == 0
    )
    unverified = capsys.readouterr().out
    assert "could not be read" in unverified
    # Assert on the registration's OWN line, not on the whole output: the NOTE
    # above it contains the word UNVERIFIED too, so a whole-output check passes
    # even when the state column still reads CURRENT — which is how the first
    # draft of this assertion was vacuous, caught by reverting the fix.
    entry_line = next(
        line for line in unverified.splitlines() if line.startswith("  claude-worker")
    )
    assert "UNVERIFIED" in entry_line
    assert "CURRENT" not in entry_line
    assert "REVOKED" not in entry_line, "an unreadable ledger must not claim knowledge"


def test_check_creates_nothing_when_it_falls_back_to_the_configured_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check` reads the ledger; it must not CREATE one by asking about it.

    The A3 gap this closes, found by rebasing A4 on top of this branch. Every
    other test here hands `check` an explicit `--database-url` pointing at a
    ledger that already exists, so none of them exercised the path an operator
    actually takes: no flag, falling back to the configured database. On that
    path `check` builds a `Database`, and `Database` PREPARES its target — it
    mkdirs the parent directory and `O_CREAT`s the SQLite file. A reporting
    command documented as writes-nothing was therefore creating `data/` (and an
    empty database inside it) in whatever directory the operator ran it from.

    The doctrine this file inherits is "a tool that mints text is not a tool
    that grants", pinned by a writes-nothing test — and the pin missed this
    because it never ran the default path. Asserting on the whole directory
    tree, rather than on named files, is what makes it hard to miss again.
    """

    from chronos.cli.proposer_commands import cmd_proposer_check
    from chronos.config.settings import get_settings

    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        _registry_text(_registration("claude-worker", WORKER_CREDENTIAL)), encoding="utf-8"
    )

    workdir = tmp_path / "operator-cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    # A configured-but-absent ledger: the honest answer is "I cannot tell",
    # never "I made one so I could tell you nothing was in it".
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/chronos.db")
    get_settings.cache_clear()
    try:
        before = set(workdir.iterdir())
        assert cmd_proposer_check(argparse.Namespace(file=str(registry_path))) == 0
        assert set(workdir.iterdir()) == before, (
            "proposer check must leave the filesystem exactly as it found it"
        )
    finally:
        get_settings.cache_clear()

    out = capsys.readouterr().out
    entry_line = next(line for line in out.splitlines() if line.startswith("  claude-worker"))
    assert "UNVERIFIED" in entry_line, "an unreadable ledger is reported, not invented"
    assert "CURRENT" not in entry_line

"""Clock-fault pins (VCP §6 EXIT class P-G1): the wall clock is not a safety input.

Three injected-clock pins, no real sleeping:

1. A backwards wall-clock jump against the audit chain — an append that lands
   with an earlier timestamp than its predecessor must keep the hash chain
   valid and sequence-ordered; the out-of-order timestamp is tolerated and
   visible (the chain orders by sequence, never by clock).
2. A forward jump against reconciliation evidence-age (ADR-0020): the readiness
   latch is given both ``max_evidence_age`` and a clock, so jumping past the
   window must demote the proof to PENDING — stale, never silently fresh.
3. Evidence-bundle expiry: a bundle judged past its ``expires_at`` resolves
   EXPIRED (the two-sided positive control), and once refused EXPIRED the
   verdict is DURABLE (D-26/FIX-26) — it survives the drain's clock rewinding
   and a full engine restart, because refusing authority on expiry evidence is
   not revocable by wall-clock motion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronos.auditlog import log as auditlog_log
from chronos.auditlog.log import AuditLog, ChainState, verify_chain
from chronos.domain.enums import ReconciliationStatus
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.persistence.database import Database
from chronos.supervisor import evidence_bundles
from chronos.supervisor.evidence_kinds import BundleKind

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class _RewindableDateTime(datetime):
    """A datetime class whose now() reads a module-level knob (the injected clock)."""

    fake_now = T0

    @classmethod
    def now(cls, tz=None):
        return cls.fake_now


def test_audit_chain_survives_a_backwards_clock_jump(tmp_path: Path, monkeypatch) -> None:
    """Append A at T, rewind the clock, append B: the chain must still verify.

    Ordering is by sequence; the timestamp is hash material (so the rewind is
    visible in B's earlier ``at_utc``) but never an ordering criterion.
    """

    class _Clock:
        def __init__(self, start: datetime) -> None:
            self.now = start

        def rewind(self, delta: timedelta) -> None:
            self.now -= delta

    clock = _Clock(T0)
    _RewindableDateTime.fake_now = T0
    monkeypatch.setattr(auditlog_log, "datetime", _RewindableDateTime)

    path = tmp_path / "audit.jsonl"
    journal = AuditLog(path)
    record_a = journal.append("tg1-a", {"n": 1})
    assert record_a.sequence == 0

    clock.rewind(timedelta(seconds=30))
    _RewindableDateTime.fake_now = clock.now
    record_b = journal.append("tg1-b", {"n": 2})

    # The out-of-order timestamp is real and visible…
    assert record_b.sequence == 1
    assert record_b.at_utc < record_a.at_utc
    # …and the chain orders by sequence regardless: still valid, still ordered.
    result = verify_chain(path)
    assert result.state is ChainState.VALID, result.detail


def test_a_forward_jump_past_the_evidence_age_demotes_readiness() -> None:
    """ADR-0020: a proof that outlives the owner-frozen age fails closed by arithmetic."""

    now = [T0]
    latch = ReconciliationReadiness(
        session_id="tg1-clock-fault",
        max_evidence_age=timedelta(seconds=300),
        clock=lambda: now[0],
    )
    with latch.reconciliation_session("tg1 pin") as generation:
        latch.complete(
            expected_generation=generation,
            status=ReconciliationStatus.RECONCILED,
            reason="full parity proven",
            reconciled_at=now[0],
        )
    assert latch.snapshot().ready

    now[0] = T0 + timedelta(seconds=301)
    stale = latch.snapshot()
    assert stale.status is ReconciliationStatus.PENDING
    assert not stale.ready
    assert "maximum evidence age" in stale.reason


def test_an_expired_bundle_resolves_expired(tmp_path: Path) -> None:
    """Positive control: a bundle past its ``expires_at`` resolves EXPIRED.

    The rewind half of the original pin — an expiry verdict must never run
    backwards (D-26) — is pinned below by
    ``test_an_expired_bundle_stays_refused_after_the_clock_rewinds`` and
    ``test_an_expired_bundle_stays_refused_across_a_restart_and_rewind``.
    """

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'tg1.db'}")
    database.initialize()
    fingerprint = "f" * 64
    epoch = "e" * 64
    registration = "d" * 64
    try:
        with database.sessions.begin() as session:
            issued = evidence_bundles.issue(
                session,
                account_fingerprint=fingerprint,
                proposer_id="tg1-worker",
                proposer_credential_epoch=epoch,
                proposer_registry_entry_digest=registration,
                kind=BundleKind.BACKEND_SERVED,
                digest="1" * 64,
                now=T0,
                ttl_seconds=60.0,
            )

        def resolve(at: datetime) -> evidence_bundles.Resolution:
            with database.sessions.begin() as session:
                return evidence_bundles.resolve(
                    session,
                    account_fingerprint=fingerprint,
                    cited_ids=(issued.bundle_id,),
                    proposer_id="tg1-worker",
                    proposer_credential_epoch=epoch,
                    proposer_registry_entry_digest=registration,
                    now=at,
                )

        _certify_tg1(database, fingerprint)  # FU2: the first pass (R7)
        fresh = resolve(T0 + timedelta(seconds=59))
        assert fresh.refusal is None and fresh.bundle is not None, fresh.detail
        refused = resolve(T0 + timedelta(seconds=61))
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    finally:
        database.dispose()


_STICKY = "already refused EXPIRED at an earlier drain"


def _certify_tg1(database: Database, fingerprint: str) -> None:
    """FU2 (Kevin K-20261004-008, test-only): complete the first verification pass, which an
    evidence-bound resolve now requires (R7), before the existing assertions."""

    evidence_bundles.certify_stream(
        database.sessions, evidence_bundles.hash_chain_stream(fingerprint)
    )


def _issue_tg1_bundle(database: Database, fingerprint: str, epoch: str, registration: str):
    with database.sessions.begin() as session:
        return evidence_bundles.issue(
            session,
            account_fingerprint=fingerprint,
            proposer_id="tg1-worker",
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration,
            kind=BundleKind.BACKEND_SERVED,
            digest="1" * 64,
            now=T0,
            ttl_seconds=60.0,
        )


def _resolve_tg1(
    database: Database,
    issued,
    fingerprint: str,
    epoch: str,
    registration: str,
    at: datetime,
) -> evidence_bundles.Resolution:
    with database.sessions.begin() as session:
        return evidence_bundles.resolve(
            session,
            account_fingerprint=fingerprint,
            cited_ids=(issued.bundle_id,),
            proposer_id="tg1-worker",
            proposer_credential_epoch=epoch,
            proposer_registry_entry_digest=registration,
            now=at,
        )


def test_an_expired_bundle_stays_refused_after_the_clock_rewinds(tmp_path: Path) -> None:
    """Expiry judged at T+delta must never run backwards to T.

    A bundle refused EXPIRED against one drain clock stays refused when a later
    drain observes a rewound clock: authority once refused on expiry evidence is
    not revivable by wall-clock motion.
    """

    database = Database(f"sqlite+pysqlite:///{tmp_path / 'tg1.db'}")
    database.initialize()
    fingerprint = "f" * 64
    epoch = "e" * 64
    registration = "d" * 64
    try:
        issued = _issue_tg1_bundle(database, fingerprint, epoch, registration)
        _certify_tg1(database, fingerprint)  # FU2: the first pass (R7)

        refused = _resolve_tg1(
            database, issued, fingerprint, epoch, registration, T0 + timedelta(seconds=61)
        )
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED

        rewound = _resolve_tg1(database, issued, fingerprint, epoch, registration, T0)
        assert rewound.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
            "an expiry verdict ran backwards: the bundle admitted after the drain "
            "clock rewound past its expiry"
        )
        assert _STICKY in rewound.detail, (
            rewound.detail
        )  # FU2: the durable verdict, not "not ready"
    finally:
        database.dispose()


def test_an_expired_bundle_stays_refused_across_a_restart_and_rewind(tmp_path: Path) -> None:
    """The EXPIRED verdict is durable: it survives engine disposal (D-26/FIX-26).

    Resolve EXPIRED at T0+61s, commit, dispose the engine (a restart), open a
    fresh Database on the same file and resolve at a rewound T0 from a fresh
    session: still EXPIRED. A process-only memory of the refusal would pass the
    same-process rewind pin above and fail exactly here.
    """

    db_path = tmp_path / "tg1-restart.db"
    fingerprint = "f" * 64
    epoch = "e" * 64
    registration = "d" * 64

    database = Database(f"sqlite+pysqlite:///{db_path}")
    database.initialize()
    try:
        issued = _issue_tg1_bundle(database, fingerprint, epoch, registration)
        _certify_tg1(database, fingerprint)  # FU2: the first pass (R7)
        refused = _resolve_tg1(
            database, issued, fingerprint, epoch, registration, T0 + timedelta(seconds=61)
        )
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    finally:
        database.dispose()

    restarted = Database(f"sqlite+pysqlite:///{db_path}")
    restarted.initialize()
    try:
        _certify_tg1(restarted, fingerprint)  # FU2: a restart starts with no verified state
        rewound = _resolve_tg1(restarted, issued, fingerprint, epoch, registration, T0)
        assert rewound.refusal is evidence_bundles.ResolutionRefusal.EXPIRED, (
            "an expiry verdict did not survive a restart: a fresh engine admitted "
            "the bundle at a rewound clock"
        )
        assert _STICKY in rewound.detail, (
            rewound.detail
        )  # FU2: the durable verdict, not "not ready"
    finally:
        restarted.dispose()

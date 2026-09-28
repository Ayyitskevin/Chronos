"""Clock-fault pins (VCP §6 EXIT class P-G1): the wall clock is not a safety input.

Three injected-clock pins, no real sleeping:

1. A backwards wall-clock jump against the audit chain — an append that lands
   with an earlier timestamp than its predecessor must keep the hash chain
   valid and sequence-ordered; the out-of-order timestamp is tolerated and
   visible (the chain orders by sequence, never by clock).
2. A forward jump against reconciliation evidence-age (ADR-0020): the readiness
   latch is given both ``max_evidence_age`` and a clock, so jumping past the
   window must demote the proof to PENDING — stale, never silently fresh.
3. Evidence-bundle expiry positive control: a bundle judged past its
   ``expires_at`` resolves EXPIRED — the refusal the FIX-26 rewind pin
   (the D-26 finding) builds on. The rewind assertion itself is not here;
   it lands red inside FIX-26.
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
    backwards — is the D-26 finding and lands red inside FIX-26; it is
    deliberately not in this file.
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

        fresh = resolve(T0 + timedelta(seconds=59))
        assert fresh.refusal is None and fresh.bundle is not None, fresh.detail
        refused = resolve(T0 + timedelta(seconds=61))
        assert refused.refusal is evidence_bundles.ResolutionRefusal.EXPIRED
    finally:
        database.dispose()

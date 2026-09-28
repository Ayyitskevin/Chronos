"""Storage write-path fault pins (VCP §6 EXIT "disk failure" class, write-path
depth — flow-team TG2, from READ-EXIT6 P-G2). tmp_path fixtures only; no fault
is ever pointed at a real state dir.

Three failure shapes, each with a mutation that turns it red:

1. An audit-log append into a read-only directory raises and creates nothing
   (fresh shape), and on a pre-existing good pair it raises having written
   exactly one COMPLETE record — the designed, documented crash window
   (``src/chronos/auditlog/log.py:33-37,49-56``: the record is fsynced before
   the anchor publish, so the pair reads BROKEN and every later writer refuses
   BEFORE writing). Mutation: making the append path's refusal (``_refuse``)
   swallow instead of raise.
2. ENOSPC at the append's write — injected by proxying ``os.fdopen`` so the
   write call on the append's descriptor raises before any byte is accepted
   (the C-level ``os.write`` inside buffered IO is not interceptable from
   Python; the effect point is identical). Zero bytes land, the pair stays
   VALID, and after remediation the next append re-chains from the last good
   record. Mutation: swallowing ``OSError`` around the log write/flush/fsync
   so append reports success with nothing durable.
3. The supervised cycle's decision write (``durable.record_outcome`` — the
   attempt counter and the decision journal commit in one transaction, so a
   counter can never claim a refusal the journal cannot account for): a write
   failure raises, and afterwards no attempt row and no chain row exist.
   Mutation: suppressing the journal failure inside ``record_outcome``.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chronos.auditlog.log import AuditLog, AuditLogCorruptionError, ChainState, verify_chain
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.supervisor import durable as dur
from chronos.supervisor.admission import AdmissionCheck, AdmissionOutcome

requires_nonroot = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory write permission bits"
)

_NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
_FINGERPRINT = "b" * 64
_STATIC_POSTURE = dur.DecisionPosture(
    registry_configured=False, evidence_binding=False, credential_epoch_bound=False
)


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@requires_nonroot
def test_1_append_into_read_only_directory_raises_and_creates_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    try:
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(state / "audit.jsonl").append("cycle", {"index": 0})
        # Byte length and last record are unchanged: there is no log at all,
        # and no sibling (lock, anchor, temp) was left behind either.
        assert sorted(entry.name for entry in state.iterdir()) == []
    finally:
        state.chmod(0o755)


@requires_nonroot
def test_1b_append_failure_on_a_good_pair_is_the_documented_crash_window(tmp_path: Path) -> None:
    """A pre-existing pair: the refusal lands AFTER the record's fsync (anchor
    publish is what the read-only dir refuses), so the log gains one COMPLETE
    record, the pair reads BROKEN, and every later writer refuses before
    writing — fail closed, never a truncated tail."""
    state = tmp_path / "state"
    state.mkdir()
    path = state / "audit.jsonl"
    log = AuditLog(path)
    log.append("cycle", {"index": 0})
    second = log.append("cycle", {"index": 1})
    head_before = second.record_hash
    assert verify_chain(path).state is ChainState.VALID

    state.chmod(0o555)
    try:
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(path).append("cycle", {"index": 2})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3, "the durable record is complete; nothing truncated"
        appended = json.loads(lines[-1])
        assert appended["previous_hash"] == head_before
        assert appended["record_hash"], "a full record, not a partial tail"
        assert verify_chain(path).state is ChainState.BROKEN, "crash window: log ahead of anchor"
        # Every later writer refuses BEFORE writing: the byte length is now frozen.
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(path).append("cycle", {"index": 3})
        assert path.read_text(encoding="utf-8").splitlines() == lines
    finally:
        state.chmod(0o755)


def test_2_enospc_at_the_append_write_leaves_zero_bytes_and_rechains_after_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("cycle", {"index": 0})
    second = log.append("cycle", {"index": 1})
    head_before = second.record_hash
    bytes_before = path.read_bytes()

    real_fdopen = os.fdopen

    class _EnospcWrite:
        """The append's descriptor, reading normally, refusing every write the
        way a full disk does — before any byte is accepted."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._wrapped.__exit__(*exc_info)

        def write(self, data):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def fdopen_with_enospc(descriptor, mode="r", *args, **kwargs):
        handle = real_fdopen(descriptor, mode, *args, **kwargs)
        if "+" in mode:
            return _EnospcWrite(handle)
        return handle

    monkeypatch.setattr("os.fdopen", fdopen_with_enospc)
    with pytest.raises(OSError) as raised:
        AuditLog(path).append("cycle", {"index": 2})
    assert raised.value.errno == errno.ENOSPC
    assert path.read_bytes() == bytes_before, "a refused write leaves zero bytes behind"
    assert verify_chain(path).state is ChainState.VALID, "the pair is untouched"

    monkeypatch.undo()
    third = AuditLog(path).append("cycle", {"index": 2})
    assert third.sequence == 2
    assert third.previous_hash == head_before, "re-chains from the last good record — no gap"
    verification = verify_chain(path)
    assert verification.state is ChainState.VALID, verification.detail
    assert "3 records" in verification.detail


def test_3_a_failed_decision_write_journals_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions() as session:
            outcome = AdmissionOutcome(
                admitted=True,
                checks=(AdmissionCheck(name="mandate", passed=True, detail="in scope"),),
                detail="admitted",
            )

            def fail_append(*args, **kwargs):
                raise OSError("private filesystem detail")

            monkeypatch.setattr(hash_chain, "append", fail_append)
            with pytest.raises(OSError):
                dur.record_outcome(
                    session,
                    account_fingerprint=_FINGERPRINT,
                    decision_id="d-faulted",
                    outcome=outcome,
                    now=_NOW,
                    posture=_STATIC_POSTURE,
                )
            session.rollback()
            admitted, refusals = dur.load_attempts(session, account_fingerprint=_FINGERPRINT)
            assert "d-faulted" not in admitted, "no counter may claim what the journal cannot"
            assert "d-faulted" not in refusals
            stream = dur.stream_for(dur.DECISION_STREAM, _FINGERPRINT)
            assert hash_chain.head(session, stream) is None, "no journaled success"
    finally:
        database.dispose()

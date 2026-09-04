"""The single-writer lease is renewed and re-checked before transmit (R-24).

The M0 autonomy audit found that ``WriterLease.renew()`` had **no production
caller at all**: the lease expired after its 30-second TTL while the backend
went on believing it was the writer, so a second backend could acquire it and
both would consider themselves authoritative. ``writer_lease_held`` — the only
lease gate on the submission path — is a startup-time boolean that is never
re-derived, so it could not notice.

That is survivable when a human is watching one process. It is not survivable
under unattended autonomy, which is why ADR-0016 lists it as an M2 prerequisite.
This module pins the two halves of the fix:

1. ``WriterLease.holds()`` answers, from the database, whether *this* process
   still owns a live lease.
2. The submission boundary calls it immediately before the transmit line, so a
   stale holder is refused at the last possible moment. A refusal there is
   provably not-sent, exactly like the kill-switch re-read beside it.

Honest bound, restated from ``locking.py``: this narrows the window, it does not
close it. IBKR accepts an order without knowing about our lease, so true
broker-side fencing is not available and a sufficiently unlucky pause between
the check and the wire cannot be defended against from here.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chronos.utils.locking import WriterLease

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src" / "chronos"
_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}")
    return sessionmaker(bind=engine)


def test_holds_is_true_only_for_the_live_owner(sessions: sessionmaker) -> None:
    lease = WriterLease(sessions, holder="a", ttl=timedelta(seconds=30))
    assert lease.acquire(now=_NOW) is True
    assert lease.holds(now=_NOW) is True
    # Past its TTL the owner no longer holds it, even though nothing was told.
    assert lease.holds(now=_NOW + timedelta(seconds=31)) is False


def test_holds_is_false_after_another_process_takes_over(sessions: sessionmaker) -> None:
    """The exact split-brain the audit found: first process still thinks it writes."""

    first = WriterLease(sessions, holder="first", ttl=timedelta(seconds=30))
    second = WriterLease(sessions, holder="second", ttl=timedelta(seconds=30))
    assert first.acquire(now=_NOW) is True

    later = _NOW + timedelta(seconds=31)  # first lapsed because it never renewed
    assert second.acquire(now=later) is True

    assert second.holds(now=later) is True
    assert first.holds(now=later) is False  # the check the boundary now makes


def test_renew_keeps_the_lease_and_blocks_takeover(sessions: sessionmaker) -> None:
    first = WriterLease(sessions, holder="first", ttl=timedelta(seconds=30))
    second = WriterLease(sessions, holder="second", ttl=timedelta(seconds=30))
    assert first.acquire(now=_NOW) is True

    heartbeat = _NOW + timedelta(seconds=10)
    assert first.renew(now=heartbeat) is True

    # 31s after acquisition, but only 21s after the renewal: still owned.
    later = _NOW + timedelta(seconds=31)
    assert first.holds(now=later) is True
    assert second.acquire(now=later) is False


def test_renew_fails_once_the_lease_was_lost(sessions: sessionmaker) -> None:
    first = WriterLease(sessions, holder="first", ttl=timedelta(seconds=30))
    second = WriterLease(sessions, holder="second", ttl=timedelta(seconds=30))
    assert first.acquire(now=_NOW) is True
    later = _NOW + timedelta(seconds=31)
    assert second.acquire(now=later) is True
    # The heartbeat's signal to demote itself to read-only.
    assert first.renew(now=later) is False


def test_a_lease_verifier_may_not_be_bound_twice() -> None:
    """Two verifiers would mean two leases — the condition this exists to detect."""

    from chronos.orders.submission import OrderSubmissionBoundary

    boundary = object.__new__(OrderSubmissionBoundary)
    boundary._lease_verifier = None  # type: ignore[attr-defined]
    boundary.bind_lease_verifier(lambda: True)
    with pytest.raises(RuntimeError):
        boundary.bind_lease_verifier(lambda: True)


def test_the_unbound_sentinel_denies_and_yields_to_the_real_verifier() -> None:
    """R-64: unbound is a state, and it must be a refusing one.

    `bind_lease_verifier` runs in the backend lifespan, after construction, so a
    LIVE boundary is briefly -- or permanently, if that lifespan raised first --
    without a verifier. That state used to skip the pre-transmit re-check
    entirely. It now carries a sentinel that denies, which startup replaces
    exactly once; a second bind is still two leases and still refused.
    """

    from chronos.orders.submission import OrderSubmissionBoundary, _unbound_lease_denies

    assert _unbound_lease_denies() is False

    boundary = object.__new__(OrderSubmissionBoundary)
    boundary._lease_verifier = _unbound_lease_denies  # type: ignore[attr-defined]
    boundary.bind_lease_verifier(lambda: True)
    assert boundary._lease_verifier() is True  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        boundary.bind_lease_verifier(lambda: True)


def test_a_live_boundary_never_leaves_the_lease_verifier_unset() -> None:
    """The constructor is the place the sentinel is installed, so read it there.

    The behavioural proof that an unbound LIVE boundary transmits nothing lives in
    `tests/integration/test_live_submission.py`; this pins the source-level rule
    that makes it true, so a refactor that drops the install is visible here too.
    """

    source = (_SRC / "orders" / "submission.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    installs = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Name) and node.id == "_unbound_lease_denies"
    ]
    assert installs, "the LIVE constructor must install the unbound-lease sentinel"


def test_the_backend_lifespan_renews_and_binds_the_verifier() -> None:
    """Structural guard: production wiring must do both halves of R-24.

    A unit test can prove ``holds()`` and ``renew()`` behave; only an inspection
    of the wiring proves they are actually *called*, which is the defect the
    audit found — the methods existed and worked, and nothing invoked them.
    """

    source = (_SRC / "api" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Collect every attribute the module touches, whether called directly or
    # passed as a bound method — `asyncio.to_thread(lease.renew)` is a reference,
    # not a call, and it is exactly how the heartbeat invokes renewal.
    referenced = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "renew" in referenced, "the backend lifespan must renew the writer lease"
    assert "bind_lease_verifier" in referenced, "the boundary must receive the live lease check"
    assert "create_task" in referenced, "lease renewal must run on a heartbeat, not once"


def test_the_boundary_rechecks_the_lease_immediately_before_transmit() -> None:
    """The re-check must sit in the same tight window as the kill-switch re-read.

    Ordering is the whole point: a lease check that ran early would be just
    another startup-time flag. This asserts it is the last gate before the
    single ``transmit=True`` line.
    """

    lines = (_SRC / "orders" / "submission.py").read_text(encoding="utf-8").splitlines()
    transmit_line = next(
        index for index, line in enumerate(lines) if "to_order_request(transmit=True)" in line
    )
    # The last occurrence before the transmit line: the first is in
    # `bind_lease_verifier`, which is setup rather than the gate.
    verifier_line = max(
        index
        for index, line in enumerate(lines[:transmit_line])
        if "self._lease_verifier is not None" in line
    )
    assert verifier_line < transmit_line, "the lease re-check must precede the transmit line"
    assert transmit_line - verifier_line < 40, (
        "the lease re-check drifted away from the transmit line; it must stay in the "
        "tightest possible window"
    )

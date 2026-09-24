"""RC-1 — an unsolicited or uncorrelated broker execution/order-status observation invalidates
submission readiness (owner answer K8 = (a): tightening only; no new re-arm path).

The pins are numbered by the packet contract:

1. every adapter routes streamed executions / order statuses to ONE classifier; own-and-correlated
   is ignored, anything else demotes readiness to PENDING with a typed reason and a new generation;
2. nothing in ``chronos.broker`` calls a latch method other than ``invalidate`` (AST, both ways);
3. decision safety: no interleaving sends an order the observation-free run refused; positive
   controls through the real submission boundary; a send already started is never recalled;
4. the latch, the composite predicate, the cadence/age settings and pacing are unchanged.

What IBKR actually streams to which client id is UNVERIFIED on live (K4): demo/synthetic only.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chronos.broker import official_ibkr as official_module
from chronos.broker.callbacks import (
    UNSOLICITED_EXECUTION_REASON,
    UNSOLICITED_ORDER_STATUS_REASON,
    CallbackBridge,
    UnsolicitedObservationClassifier,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import OrderLifecycle, OrderSide, ReconciliationStatus
from chronos.domain.models import BrokerExecution, UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.submission import SubmissionRefusalCode
from tests.integration.test_order_pipeline import (
    _drive_to_confirmed,
    _Harness,
    _short_put_intent,
    harness,  # noqa: F401 — the pipeline's fixture, reused for the positive controls
)
from tests.support.order_fakes import FIXED_NOW
from tests.unit.test_ibkr_broker import FakeIB, make_broker

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
_OWN_CLIENT = 7
_OWN_ORDER = 101


def _warm(readiness: ReconciliationReadiness) -> int:
    """Publish a RECONCILED proof the way a pass does; return its generation."""

    generation = readiness.begin_reconciliation("test pass")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="full parity (test)",
        reconciled_at=_NOW,
    )
    assert readiness.snapshot().ready
    return generation


def _foreign_execution() -> BrokerExecution:
    return BrokerExecution(
        execution_id="0000e0a1.foreign.01",
        account_id=DEMO_ACCOUNT_ID,
        broker_order_id=0,  # a manual TWS order / assignment carries no Chronos order id
        client_id=0,
        contract=UnderlyingContract(con_id=265598, symbol="AAPL"),
        side=OrderSide.BUY,
        quantity=Decimal("100"),
        price=Decimal("190.25"),
        timestamp=_NOW,
    )


def _bridge(readiness: ReconciliationReadiness) -> CallbackBridge:
    bridge = CallbackBridge(
        RequestRegistry(), on_connection_uncertain=readiness.invalidate, client_id=_OWN_CLIENT
    )
    bridge.start_order_ack(_OWN_ORDER)  # the adapter records its own order before the send
    bridge.clear_order_ack(_OWN_ORDER)
    return bridge


def _execution(client_id: int | None, order_id: int | None) -> SimpleNamespace:
    fields: dict[str, int] = {}
    if client_id is not None:
        fields["clientId"] = client_id
    if order_id is not None:
        fields["orderId"] = order_id
    return SimpleNamespace(**fields)


# --- 1. one classifier behind every adapter ------------------------------------------------------


def test_1_a_simulated_foreign_demo_execution_demotes_readiness_with_the_exact_reason() -> None:
    readiness = ReconciliationReadiness()
    demo = DemoBroker(on_connection_uncertain=readiness.invalidate)
    asyncio.run(demo.connect())  # connect loads the demo book; the foreign fill joins it after
    booked = [e.execution_id for e in asyncio.run(demo.executions())]
    generation = _warm(readiness)

    demo.simulate_unsolicited_execution(_foreign_execution())

    snapshot = readiness.snapshot()
    assert snapshot.status is ReconciliationStatus.PENDING
    assert snapshot.generation == generation + 1
    assert snapshot.reason == "unsolicited broker execution observed; reconciliation required"
    assert snapshot.reason == UNSOLICITED_EXECUTION_REASON
    assert snapshot.reconciled_at is None

    after = [e.execution_id for e in asyncio.run(demo.executions())]
    assert after == [*booked, "0000e0a1.foreign.01"]  # a later pass sees the foreign fill


def test_1_official_bridge_ignores_own_and_correlated_and_demotes_everything_else() -> None:
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    generation = _warm(readiness)

    # own-and-correlated: this client id AND an order id this process submitted
    bridge.on_exec_details(-1, None, _execution(_OWN_CLIENT, _OWN_ORDER))
    bridge.observe_order_status_identity(_OWN_CLIENT, _OWN_ORDER)
    assert readiness.snapshot().ready and readiness.snapshot().generation == generation

    # a response to the adapter's own reqExecutions (request id >= 0) never invalidates
    bridge.on_exec_details(5, None, _execution(0, 0))
    assert readiness.snapshot().ready and readiness.snapshot().generation == generation

    cases: list[tuple[str, Any]] = [
        # the same order number from another client is NOT own (fail closed)
        ("execution", lambda: bridge.on_exec_details(-1, None, _execution(3, _OWN_ORDER))),
        ("execution", lambda: bridge.on_exec_details(-1, None, _execution(0, 0))),
        ("execution", lambda: bridge.on_exec_details(-1, None, _execution(None, None))),
        ("execution", lambda: bridge.on_exec_details(-1, None, _execution(_OWN_CLIENT, 999))),
        ("status", lambda: bridge.observe_order_status_identity(0, 0)),
        ("status", lambda: bridge.observe_order_status_identity(None, _OWN_ORDER)),
    ]
    for kind, observe in cases:
        before = _warm(readiness)
        observe()
        snapshot = readiness.snapshot()
        assert snapshot.status is ReconciliationStatus.PENDING, kind
        assert snapshot.generation == before + 1, kind
        expected = (
            UNSOLICITED_EXECUTION_REASON if kind == "execution" else UNSOLICITED_ORDER_STATUS_REASON
        )
        assert snapshot.reason == expected


def test_1_the_official_ewrapper_forwards_client_id_and_live_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EWrapper:
        pass

    class _EClient:
        def __init__(self, wrapper: object) -> None:
            del wrapper

    monkeypatch.setattr(official_module, "_load_ibapi", lambda: (_EClient, _EWrapper, None, None))
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    app = official_module._make_app(bridge)
    generation = _warm(readiness)

    # orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, parentId,
    #             lastFillPrice, clientId, whyHeld, mktCapPrice)
    app.orderStatus(_OWN_ORDER, "Filled", 1, 0, 1.5, 555, 0, 1.5, _OWN_CLIENT, "", 0.0)
    assert readiness.snapshot().generation == generation and readiness.snapshot().ready

    app.orderStatus(_OWN_ORDER, "Filled", 1, 0, 1.5, 556, 0, 1.5, _OWN_CLIENT + 1, "", 0.0)
    assert readiness.snapshot().reason == UNSOLICITED_ORDER_STATUS_REASON

    _warm(readiness)
    app.execDetails(-1, None, _execution(0, 0))
    assert readiness.snapshot().reason == UNSOLICITED_EXECUTION_REASON


def test_1_the_read_only_ib_async_adapter_treats_every_live_fill_and_status_as_unsolicited() -> (
    None
):
    readiness = ReconciliationReadiness()
    client = FakeIB()
    make_broker(client, on_connection_uncertain=readiness.invalidate)
    trade, fill, report = object(), object(), object()

    generation = _warm(readiness)
    client.openOrderEvent.emit(trade)  # not a fill or a status change: not subscribed
    client.commissionReportEvent.emit(trade, fill, report)
    assert readiness.snapshot().ready and readiness.snapshot().generation == generation

    client.execDetailsEvent.emit(trade, fill)
    assert readiness.snapshot().reason == UNSOLICITED_EXECUTION_REASON
    assert readiness.snapshot().generation == generation + 1

    generation = _warm(readiness)
    client.orderStatusEvent.emit(trade)
    assert readiness.snapshot().reason == UNSOLICITED_ORDER_STATUS_REASON
    assert readiness.snapshot().generation == generation + 1


def test_1_an_invalidator_that_raises_is_logged_and_never_raised_into_the_reader() -> None:
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("rc1.test.classifier")
    logger.addHandler(_Collect())
    logger.propagate = False

    def explode(reason: str) -> object:
        raise RuntimeError(f"latch unavailable: {reason}")

    classifier = UnsolicitedObservationClassifier(invalidate=explode, client_id=None, logger=logger)
    assert classifier.observe_execution(client_id=0, order_id=0) is False
    events = [getattr(r, "event", None) for r in records]
    assert events == [
        "unsolicited_broker_observation",
        "unsolicited_observation_invalidation_failed",
    ]

    positive = UnsolicitedObservationClassifier(
        invalidate=lambda reason: None, client_id=None, logger=logger
    )
    assert positive.observe_execution(client_id=0, order_id=0) is True


# --- 2. the callback path reaches the latch ONLY through invalidate() ---------------------------

_FORBIDDEN_CALLS = frozenset(
    {
        "complete",
        "begin_reconciliation",
        "reconciliation_session",
        "reconcile_submission_readiness",
        "submission_guard",
    }
)


def _forbidden_calls(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name in _FORBIDDEN_CALLS:
            found.append(f"{name}@{node.lineno}")
    return found


def test_2_nothing_in_chronos_broker_calls_a_latch_method_other_than_invalidate() -> None:
    offenders = {
        str(path.relative_to(_ROOT)): hits
        for path in sorted((_ROOT / "src/chronos/broker").rglob("*.py"))
        if (hits := _forbidden_calls(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
    # positive control: the detector sees each forbidden call shape
    for snippet in (
        "self._readiness.complete(expected_generation=1, status=s, reason='r')",
        "runtime.reconcile_submission_readiness(trigger='order_fill')",
        "readiness.begin_reconciliation('x')",
    ):
        assert _forbidden_calls(snippet), snippet


def test_2_the_classifier_touches_the_latch_through_its_injected_invalidate_only() -> None:
    tree = ast.parse((_ROOT / "src/chronos/broker/callbacks.py").read_text(encoding="utf-8"))
    classifier = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "UnsolicitedObservationClassifier"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(classifier)
        if isinstance(node, ast.Call) and ast.unparse(node.func).startswith("self.")
    ]
    assert set(calls) == {
        "self._invalidate",  # the ONE latch touch: readiness.invalidate, injected
        "self._is_own",
        "self._observe",
        "self._own_order_ids.add",
        "self._logger.warning",
        "self._logger.exception",
    }, calls
    assert calls.count("self._invalidate") == 1


# --- 3. decision safety ---------------------------------------------------------------------------

_POSITIONS = ("before_claim", "after_claim", "before_recheck", "before_at_send", "after_send")


def _attempt(
    readiness: ReconciliationReadiness,
    bridge: CallbackBridge,
    observations: list[tuple[str, bool]],
) -> str:
    """The submission path's latch steps (orders/submission.py:654-716, the adapter's at_send)."""

    def fire(position: str) -> None:
        for where, foreign in observations:
            if where == position:
                client, order = (0, 0) if foreign else (_OWN_CLIENT, _OWN_ORDER)
                bridge.on_exec_details(-1, None, _execution(client, order))

    generation = readiness.snapshot().generation
    fire("before_claim")
    result = "unset"
    with readiness.submission_guard(expected_generation=generation) as acquired:
        fire("after_claim")
        if not acquired:
            return "refused_claim"
        fire("before_recheck")
        if not readiness.submission_claim_is_current(expected_generation=generation):
            return "refused_not_current"
        fire("before_at_send")
        with readiness.at_send() as authorized:
            result = "sent" if authorized else "refused_before_send"
    fire("after_send")
    return result


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    warm=st.booleans(),
    observations=st.lists(st.tuples(st.sampled_from(_POSITIONS), st.booleans()), max_size=4),
)
def test_3_no_interleaving_sends_an_order_the_observation_free_run_refused(
    warm: bool, observations: list[tuple[str, bool]]
) -> None:
    def run(obs: list[tuple[str, bool]]) -> str:
        readiness = ReconciliationReadiness()
        bridge = _bridge(readiness)
        if warm:
            _warm(readiness)
        return _attempt(readiness, bridge, obs)

    baseline = run([])
    observed = run(observations)
    if observed == "sent":
        assert baseline == "sent"
    foreign_before_send = any(foreign and where != "after_send" for where, foreign in observations)
    if foreign_before_send:
        assert observed != "sent"  # strict tightening where it applies
    if not any(foreign for _, foreign in observations):
        assert observed == baseline  # own-and-correlated observations change nothing


def _foreign_via_bridge(readiness: ReconciliationReadiness) -> None:
    CallbackBridge(RequestRegistry(), on_connection_uncertain=readiness.invalidate).on_exec_details(
        -1, None, _execution(0, 0)
    )


def test_3_a_foreign_fill_between_claim_and_transmit_is_refused_not_sent(
    harness: _Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    original = harness.readiness.submission_claim_is_current
    checks = 0

    def observe_then_check(*, expected_generation: int) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            _foreign_via_bridge(harness.readiness)
        return original(expected_generation=expected_generation)

    monkeypatch.setattr(harness.readiness, "submission_claim_is_current", observe_then_check)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.refusal is SubmissionRefusalCode.RECONCILIATION_NOT_READY
    assert harness.broker.submit_calls == []
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.REJECTED
    assert harness.readiness.snapshot().reason == UNSOLICITED_EXECUTION_REASON


def test_3_a_foreign_fill_after_the_claim_check_is_refused_at_the_atomic_send(
    harness: _Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    original_run = harness.connection.run
    calls = 0

    def run_with_observation(coroutine: object, *, timeout: float | None = None) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            _foreign_via_bridge(harness.readiness)
        return original_run(coroutine, timeout=timeout)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(harness.connection, "run", run_with_observation)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]

    assert outcome.refusal is SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND
    assert harness.broker.submit_calls == []
    assert harness.readiness.snapshot().reason == UNSOLICITED_EXECUTION_REASON


def test_3_a_foreign_fill_during_an_authorized_send_does_not_recall_it(
    harness: _Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    original_at_send = harness.readiness.at_send
    observers: list[threading.Thread] = []

    @contextmanager
    def at_send_with_a_concurrent_fill() -> Iterator[bool]:
        with original_at_send() as authorized:
            # the reader thread's observation blocks on the latch lock while the send is
            # authorized and in progress — it cannot race the send it arrived during
            observer = threading.Thread(target=_foreign_via_bridge, args=(harness.readiness,))
            observer.start()
            observers.append(observer)
            observer.join(timeout=0.2)
            assert observer.is_alive()
            yield authorized

    monkeypatch.setattr(harness.readiness, "at_send", at_send_with_a_concurrent_fill)
    outcome = harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    for observer in observers:
        observer.join(timeout=5)
        assert not observer.is_alive()

    assert outcome.submitted is True
    assert len(harness.broker.submit_calls) == 1  # sent once, not recalled
    assert harness.readiness.snapshot().reason == UNSOLICITED_EXECUTION_REASON


# --- 4. the latch, the predicate, the settings and pacing are unchanged ------------------------

_UNCHANGED = (
    "src/chronos/orders/reconciliation_readiness.py",
    "src/chronos/config/settings.py",
    "src/chronos/marketdata/pacing.py",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_ROOT, check=True, capture_output=True, text=True
    ).stdout


def _rc1_commit() -> str:
    for line in _git("log", "--format=%H%x09%s", "-200").splitlines():
        sha, _, subject = line.partition("\t")
        if subject.endswith("(RC-1)"):
            return sha
    pytest.fail("the RC-1 commit (subject ending '(RC-1)') is not in the last 200 commits")


def _predicate(source: str) -> str:
    return next(
        ast.dump(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "_reconcile_submission_readiness_generation"
    )


def test_4_the_latch_settings_pacing_and_the_composite_predicate_are_untouched_by_rc1() -> None:
    commit = _rc1_commit()
    changed = _git("diff", "--name-only", f"{commit}^", commit).split()
    assert not set(changed) & set(_UNCHANGED), changed
    before = _git("show", f"{commit}^:src/chronos/runtime.py")
    after = _git("show", f"{commit}:src/chronos/runtime.py")
    assert _predicate(after) == _predicate(before)


# --- r2 (Daybreak HOLD at 72ee6ec): malformed identifiers are uncorrelated, never own ----------


class _HostileExecution:
    """A callback object whose identifier getter itself fails."""

    orderId = _OWN_ORDER

    @property
    def clientId(self) -> int:
        raise RuntimeError("decoder produced an unreadable clientId")


#: Every one of these is NOT an exact own-and-correlated observation: it must invalidate.
_MALFORMED_IDS: tuple[tuple[object, object], ...] = (
    (7.9, 101.9),  # Daybreak's probe: truncation would alias the own pair (7, 101)
    (7.0, 101.0),  # an exact float of the own pair (7.0 == 7 in Python) is still not an int
    (float("inf"), _OWN_ORDER),  # int(inf) raises OverflowError
    (_OWN_CLIENT, float("inf")),
    (float("-inf"), float("nan")),
    (True, _OWN_ORDER),  # bool is an int subclass; never an identifier
    ("7", "101"),  # a string is not coerced
    (Decimal("7"), Decimal("101")),
    (None, _OWN_ORDER),
    (_OWN_CLIENT, None),
)


@pytest.mark.parametrize(("client_id", "order_id"), _MALFORMED_IDS)
def test_2r2_a_malformed_live_execution_is_uncorrelated_and_invalidates(
    client_id: object, order_id: object
) -> None:
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    generation = _warm(readiness)

    bridge.on_exec_details(-1, None, SimpleNamespace(clientId=client_id, orderId=order_id))

    snapshot = readiness.snapshot()
    assert snapshot.status is ReconciliationStatus.PENDING
    assert snapshot.generation == generation + 1
    assert snapshot.reason == UNSOLICITED_EXECUTION_REASON


def test_2r2_an_unreadable_identifier_stays_inside_the_callback_and_invalidates() -> None:
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    generation = _warm(readiness)

    bridge.on_exec_details(-1, None, _HostileExecution())  # must not raise

    assert readiness.snapshot().status is ReconciliationStatus.PENDING
    assert readiness.snapshot().generation == generation + 1


@pytest.mark.parametrize(("client_id", "order_id"), _MALFORMED_IDS)
def test_2r2_a_malformed_order_status_identity_is_uncorrelated_and_invalidates(
    client_id: object, order_id: object
) -> None:
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    generation = _warm(readiness)

    bridge.observe_order_status_identity(client_id, order_id)

    snapshot = readiness.snapshot()
    assert snapshot.status is ReconciliationStatus.PENDING
    assert snapshot.generation == generation + 1
    assert snapshot.reason == UNSOLICITED_ORDER_STATUS_REASON


@pytest.mark.parametrize(
    ("client_id", "order_id"),
    [(7.9, 101.9), (7.0, 101.0), (float("inf"), _OWN_ORDER), (_OWN_CLIENT, float("nan"))],
)
def test_2r2_the_official_wrapper_classifies_raw_ids_first_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, client_id: object, order_id: object
) -> None:
    class _EWrapper:
        pass

    class _EClient:
        def __init__(self, wrapper: object) -> None:
            del wrapper

    monkeypatch.setattr(official_module, "_load_ibapi", lambda: (_EClient, _EWrapper, None, None))
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    app = official_module._make_app(bridge)

    generation = _warm(readiness)
    app.orderStatus(order_id, "Filled", 1, 0, 1.5, 555, 0, 1.5, client_id, "", 0.0)
    assert readiness.snapshot().status is ReconciliationStatus.PENDING
    assert readiness.snapshot().generation == generation + 1
    assert readiness.snapshot().reason == UNSOLICITED_ORDER_STATUS_REASON

    generation = _warm(readiness)
    app.execDetails(-1, None, SimpleNamespace(clientId=client_id, orderId=order_id))
    assert readiness.snapshot().status is ReconciliationStatus.PENDING
    assert readiness.snapshot().generation == generation + 1


def test_1r2_the_exact_own_pair_still_reads_own_and_malformed_never_restores_readiness() -> None:
    readiness = ReconciliationReadiness()
    bridge = _bridge(readiness)
    generation = _warm(readiness)

    # positive control: the exact integer own pair is still own-and-correlated on both paths
    bridge.on_exec_details(-1, None, SimpleNamespace(clientId=_OWN_CLIENT, orderId=_OWN_ORDER))
    bridge.observe_order_status_identity(_OWN_CLIENT, _OWN_ORDER)
    assert readiness.snapshot().ready and readiness.snapshot().generation == generation

    # from PENDING, malformed observations only ever advance the generation; never RECONCILED
    readiness.invalidate("cold start")
    for client_id, order_id in _MALFORMED_IDS:
        before = readiness.snapshot().generation
        bridge.on_exec_details(-1, None, SimpleNamespace(clientId=client_id, orderId=order_id))
        bridge.observe_order_status_identity(client_id, order_id)
        after = readiness.snapshot()
        assert after.status is ReconciliationStatus.PENDING
        assert after.generation == before + 2


def test_2r2_the_classifier_itself_never_reads_a_non_int_as_own() -> None:
    """Defense in depth: a future caller that forgets to parse cannot alias an own pair."""

    reasons: list[str] = []
    classifier = UnsolicitedObservationClassifier(
        invalidate=reasons.append, client_id=_OWN_CLIENT, logger=logging.getLogger("rc1.r2")
    )
    classifier.note_own_order(_OWN_ORDER)
    assert classifier.observe_execution(client_id=_OWN_CLIENT, order_id=_OWN_ORDER) is False
    for client_id, order_id in ((7.0, 101.0), (True, _OWN_ORDER), (_OWN_CLIENT, Decimal("101"))):
        assert classifier.observe_execution(client_id=client_id, order_id=order_id) is True  # type: ignore[arg-type]
    assert reasons == [UNSOLICITED_EXECUTION_REASON] * 3


# --- r3 (Daybreak HOLD at ce71d4b): ids outside the broker-valid domain are uncorrelated --------
#: The TWS API carries orderId and clientId as 32-bit signed ints. clientId in [0, 2**31-1]
#: (0 is the master client); orderId in [1, 2**31-1] (nextValidId issues positive ids; manual
#: TWS orders arrive as 0 or negative). Outside that, or not an exact int: never own.
_ID_MAX = 2**31 - 1
_OUT_OF_DOMAIN: tuple[tuple[int, int], ...] = (
    (-1, -1),  # Daybreak's probe
    (10**1000, 10**1000),  # Daybreak's probe
    (_OWN_CLIENT, _ID_MAX + 1),
    (_ID_MAX + 1, _OWN_ORDER),
    (_OWN_CLIENT, 0),  # order id 0: a manual TWS order's execution
    (-1, _OWN_ORDER),
    (_OWN_CLIENT, -5),
)
_OUT_OF_DOMAIN_IDS = (
    "neg-neg",
    "huge-huge",
    "order-2**31",
    "client-2**31",
    "order-0",
    "client-neg",
    "order-neg",
)


@pytest.mark.parametrize(("client_id", "order_id"), _OUT_OF_DOMAIN, ids=_OUT_OF_DOMAIN_IDS)
def test_1r3_an_out_of_domain_pair_can_neither_register_nor_read_as_own(
    client_id: int, order_id: int
) -> None:
    reasons: list[str] = []
    classifier = UnsolicitedObservationClassifier(
        invalidate=reasons.append, client_id=client_id, logger=logging.getLogger("rc1.r3")
    )
    classifier.note_own_order(order_id)  # Daybreak's probe registered these as own
    assert classifier.observe_execution(client_id=client_id, order_id=order_id) is True
    assert classifier.observe_order_status(client_id=client_id, order_id=order_id) is True
    assert reasons == [UNSOLICITED_EXECUTION_REASON, UNSOLICITED_ORDER_STATUS_REASON]


@pytest.mark.parametrize(("client_id", "order_id"), _OUT_OF_DOMAIN, ids=_OUT_OF_DOMAIN_IDS)
def test_1r3_the_bridge_invalidates_an_out_of_domain_pair_on_both_paths(
    client_id: int, order_id: int
) -> None:
    readiness = ReconciliationReadiness()
    bridge = CallbackBridge(
        RequestRegistry(), on_connection_uncertain=readiness.invalidate, client_id=client_id
    )
    bridge.start_order_ack(order_id)
    bridge.clear_order_ack(order_id)

    generation = _warm(readiness)
    bridge.on_exec_details(-1, None, SimpleNamespace(clientId=client_id, orderId=order_id))
    assert readiness.snapshot().status is ReconciliationStatus.PENDING
    assert readiness.snapshot().generation == generation + 1

    generation = _warm(readiness)
    bridge.observe_order_status_identity(client_id, order_id)
    assert readiness.snapshot().status is ReconciliationStatus.PENDING
    assert readiness.snapshot().generation == generation + 1


@pytest.mark.parametrize(
    ("client_id", "order_id"), [(0, 1), (_ID_MAX, _ID_MAX), (_OWN_CLIENT, _OWN_ORDER)]
)
def test_1r3_the_domain_boundaries_still_read_own_when_recorded(
    client_id: int, order_id: int
) -> None:
    reasons: list[str] = []
    classifier = UnsolicitedObservationClassifier(
        invalidate=reasons.append, client_id=client_id, logger=logging.getLogger("rc1.r3")
    )
    classifier.note_own_order(order_id)
    assert classifier.observe_execution(client_id=client_id, order_id=order_id) is False
    assert classifier.observe_order_status(client_id=client_id, order_id=order_id) is False
    assert reasons == []
    # one past a boundary on either field is not own
    assert classifier.observe_execution(client_id=client_id, order_id=order_id + 1) is True


def _function_calls(node: ast.AST) -> list[tuple[int, str]]:
    return sorted(
        (call.lineno, ast.unparse(call.func))
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    )


def test_1r3_one_validator_on_both_sides_and_no_int_coercion_on_the_id_surface() -> None:
    tree = ast.parse((_ROOT / "src/chronos/broker/callbacks.py").read_text(encoding="utf-8"))
    surface: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UnsolicitedObservationClassifier":
            surface[node.name] = node
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_broker_id",
            "start_order_ack",
            "on_exec_details",
            "observe_order_status_identity",
        ):
            surface[node.name] = node
    assert set(surface) == {
        "UnsolicitedObservationClassifier",
        "_broker_id",
        "start_order_ack",
        "on_exec_details",
        "observe_order_status_identity",
    }
    for name, node in surface.items():
        assert "int" not in {func for _, func in _function_calls(node)}, name  # no int() coercion
    classifier = surface["UnsolicitedObservationClassifier"]
    methods = {n.name: n for n in classifier.body if isinstance(n, ast.FunctionDef)}
    for method in ("__init__", "note_own_order", "_observe"):
        assert "_broker_id" in {f for _, f in _function_calls(methods[method])}, method
    for name in ("on_exec_details", "observe_order_status_identity"):
        assert "_broker_id" in {f for _, f in _function_calls(surface[name])}, name
    assert "_exact_int" not in (_ROOT / "src/chronos/broker/callbacks.py").read_text(
        encoding="utf-8"
    )


def test_1r3_the_official_wrapper_classifies_raw_ids_before_any_int() -> None:
    tree = ast.parse((_ROOT / "src/chronos/broker/official_ibkr.py").read_text(encoding="utf-8"))
    status = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "orderStatus"
    )
    calls = _function_calls(status)
    classify = [line for line, func in calls if func == "bridge.observe_order_status_identity"]
    coercions = [line for line, func in calls if func == "int"]
    assert len(classify) == 1 and coercions and classify[0] < min(coercions), calls

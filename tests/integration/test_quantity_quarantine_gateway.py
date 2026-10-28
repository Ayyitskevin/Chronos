"""D-43 over the FakeGateway: malformed venue figures at each official-adapter call site.

Every case drives the REAL ``OfficialIBKRBroker`` (its ``_App`` EWrapper, ``CallbackBridge`` and
normalizers) over the in-process FakeGateway; nothing reaches a network and every ALLOW_* flag is
false except inside the one test-built Settings object of the submit-ack cases, against the fake.
The fake only ever emits validated figures, so each case wraps one gateway method in a forwarding
client that rewrites the figures of the callbacks it emits (they still enter through the EWrapper's
``orderStatus`` / ``completedOrder``). Wire values: NaN, ±inf, a negative, a non-numeric string, an
sNaN ``Decimal`` (``float()`` refuses it) and ``1E+309`` (``float()`` turns it into inf, the
threat map's row-5 vector).

What is asserted, per Kevin's Q1/Q2 and the threat map (``out/D43-THREATMAP.md`` §3-§5):
- no exception escapes the adapter or the reader thread (the fixture fails on any callback raise),
  and the connection manager's "broker call failed" invalidation never fires;
- the open-orders row is a typed quarantine (``SUBMISSION_UNKNOWN`` + the cause, placeholder
  figures) while every other row of the same result is typed exactly as before;
- the submit ack is ``SUBMISSION_UNKNOWN`` with the cause in its message and its identity kept;
- the completed-orders record is dropped with a notice (unchanged); the cancel ack is unchanged;
- the reconciler and the restart pass refuse with a reason that names the cause, write nothing, and
  a quarantined row can resolve no order — operator resolve never writes REJECTED.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, cast

import pytest
from tests.support import fake_ibapi
from tests.support.order_fakes import FIXED_NOW, option_contract
from tests.support.settle_rig import (
    EARLIER_TODAY,
    NOW,
    Rig,
    ScriptedVenue,
    issues,
    make_rig,
    venue_order,
)

import chronos.broker.official_ibkr as official_module
from chronos.broker.base import BrokerSafetyError
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.domain.enums import (
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
)
from chronos.domain.models import BrokerOrder, LocalReconciliationEvidence, OrderRequest
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_option_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.services.reconciliation import ReconciliationCoordinator

UNKNOWN = OrderLifecycle.SUBMISSION_UNKNOWN
MANAGER_FAILURE = "broker call failed; connection state is uncertain"

# (wire value, the class word the cause must carry)
WIRE = [
    (float("nan"), "not finite"),
    (float("inf"), "not finite"),
    (float("-inf"), "not finite"),
    (-1.0, "negative"),
    ("abc", "not finite"),
    (Decimal("sNaN"), "not finite"),
    (Decimal("1E+309"), "not finite"),
]
WIRE_IDS = ["nan", "inf", "-inf", "negative", "non-numeric", "sNaN", "1E+309"]
SIDES = {"filled": 2, "remaining": 3}  # orderStatus argument index after (orderId, status)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    monkeypatch.setattr(official_module, "_REQUEST_TIMEOUT_S", 3.0)
    yield seeded
    # threat map §4: the reader thread must never raise on a figure
    assert seeded.callback_errors == []


class _Rewriting:
    """A FakeEClient stand-in for one gateway method: every emit goes to the real client (and so
    through the real EWrapper), after ``rewrite`` has had its say."""

    def __init__(
        self, client: Any, rewrite: Callable[[str, tuple[Any, ...]], list[tuple[str, tuple]]]
    ) -> None:
        self._client = client
        self._rewrite = rewrite

    def emit(self, name: str, *args: Any) -> None:
        for out_name, out_args in self._rewrite(name, args):
            self._client.emit(out_name, *out_args)

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self._client, attribute)


def _poison_status(order_id: int, side: str, value: object):  # type: ignore[no-untyped-def]
    def rewrite(name: str, args: tuple[Any, ...]) -> list[tuple[str, tuple]]:
        if name == "orderStatus" and args[0] == order_id:
            poisoned = list(args)
            poisoned[SIDES[side]] = value
            return [(name, tuple(poisoned))]
        return [(name, args)]

    return rewrite


def _wrap(
    monkeypatch: pytest.MonkeyPatch,
    gateway: fake_ibapi.FakeGateway,
    method: str,
    rewrite: Callable[[str, tuple[Any, ...]], list[tuple[str, tuple]]],
) -> None:
    original = getattr(gateway, method)

    def wrapped(client: Any, *args: Any) -> None:
        original(_Rewriting(client, rewrite), *args)

    monkeypatch.setattr(gateway, method, wrapped)


class _SpyReadiness:
    """The real latch, with every invalidation reason recorded."""

    def __init__(self) -> None:
        self.real = ReconciliationReadiness()
        self.reasons: list[str] = []

    def invalidate(self, reason: str) -> object:
        self.reasons.append(reason)
        return self.real.invalidate(reason)

    @contextmanager
    def invalidating_transition(self, reason: str) -> Iterator[None]:
        self.reasons.append(f"scope: {reason}")
        with self.real.invalidating_transition(reason):
            yield


@contextmanager
def _session(
    settings: Any = None,
) -> Iterator[tuple[OfficialIBKRBroker, BrokerConnectionManager, _SpyReadiness]]:
    readiness = _SpyReadiness()
    broker = OfficialIBKRBroker(
        settings or fake_ibapi.official_settings(),
        on_connection_uncertain=readiness.invalidate,
        on_managed_account_scope_change=readiness.invalidating_transition,
        order_ownership=fake_ibapi.InstalledGatewayLedger(),
    )
    manager = BrokerConnectionManager(broker, readiness=readiness)  # type: ignore[arg-type]
    manager.connect()
    try:
        yield broker, manager, readiness
    finally:
        manager.close()


def _notices(broker: OfficialIBKRBroker, tag: str) -> list[str]:
    return [note for note in broker.bridge.notices if tag in note]


# --- call site 1: the open-orders snapshot ----------------------------------------------------


def test_control_well_formed_figures_type_both_seeded_rows_as_before(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    with _session() as (broker, manager, readiness):
        rows = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
    owned, foreign = rows[fake_ibapi.OWNED_ID], rows[fake_ibapi.FOREIGN_ID]
    assert (owned.lifecycle, owned.filled_quantity, owned.remaining_quantity) == (
        OrderLifecycle.PARTIALLY_FILLED,
        Decimal("5"),
        Decimal("5"),
    )
    # getattr: this control also passes on the base, where the field does not exist yet
    assert getattr(owned, "quantity_quarantine", None) is None
    assert getattr(foreign, "quantity_quarantine", None) is None
    assert foreign.lifecycle is OrderLifecycle.SUBMITTED
    assert MANAGER_FAILURE not in readiness.reasons


@pytest.mark.parametrize("side", list(SIDES))
@pytest.mark.parametrize(("value", "cls"), WIRE, ids=WIRE_IDS)
def test_a_malformed_status_figure_quarantines_that_row_and_only_that_row(
    gateway: fake_ibapi.FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    value: object,
    cls: str,
) -> None:
    _wrap(monkeypatch, gateway, "open_orders", _poison_status(fake_ibapi.OWNED_ID, side, value))
    with _session() as (broker, manager, readiness):
        generation = manager.connection_observation().generation
        rows = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        generation_after = manager.connection_observation().generation
    owned, foreign = rows[fake_ibapi.OWNED_ID], rows[fake_ibapi.FOREIGN_ID]
    # the raw adapter row (FIX-27 lesson: never the reconciler's refined result)
    assert owned.lifecycle is UNKNOWN
    assert owned.quantity_quarantine is not None
    assert f"{side}_quantity is {cls}" in owned.quantity_quarantine
    # placeholder figures: nothing filled, the whole order outstanding — never the venue's figure
    assert (owned.filled_quantity, owned.remaining_quantity) == (Decimal("0"), owned.quantity)
    # the other row of the same result is typed exactly as in the control
    assert foreign.quantity_quarantine is None
    assert foreign.lifecycle is OrderLifecycle.SUBMITTED
    assert (foreign.filled_quantity, foreign.remaining_quantity) == (Decimal("0"), Decimal("1"))
    # the operator can see which order, and no exception reached the connection manager
    assert any(str(fake_ibapi.OWNED_ID) in note for note in _notices(broker, "[order-quantity]"))
    # a value float() cannot read is NaN to the check; the EWrapper's own notice keeps that it was
    # unreadable (the type only, never the raw text)
    unreadable = [note for note in _notices(broker, "[order-quantity]") if "unreadable" in note]
    if isinstance(value, str) or (isinstance(value, Decimal) and value.is_snan()):
        assert unreadable == [
            f"[order-quantity] orderStatus for order {fake_ibapi.OWNED_ID} carried an unreadable "
            f"{side} figure ({type(value).__name__})"
        ]
    else:
        assert unreadable == []
    assert MANAGER_FAILURE not in readiness.reasons
    assert generation_after == generation


def test_one_poisoned_status_cache_entry_no_longer_locks_every_snapshot(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Threat map row 8: ONE unsolicited orderStatus with filled=nan for a foreign order is cached;
    # every later open_orders() read it and raised until reconnect. The snapshot below does not
    # re-send that order's status, so the poisoned cache entry is what the adapter reads.
    def drop_foreign_status(name: str, args: tuple[Any, ...]) -> list[tuple[str, tuple]]:
        if name == "orderStatus" and args[0] == fake_ibapi.FOREIGN_ID:
            return []
        return [(name, args)]

    _wrap(monkeypatch, gateway, "open_orders", drop_foreign_status)
    with _session() as (broker, manager, readiness):
        client = gateway._clients[0]
        client.emit(
            "orderStatus", fake_ibapi.FOREIGN_ID, "Submitted", float("nan"), 1.0, 0.0, 0, 0, 0.0,
            fake_ibapi.MANUAL_CLIENT_ID, "", 0.0,
        )  # fmt: skip
        client.wait_dispatched()
        first = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        second = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        # the venue corrects the figure: the next snapshot types the row normally, no reconnect
        client.emit(
            "orderStatus", fake_ibapi.FOREIGN_ID, "Submitted", 0.0, 1.0, 0.0, 0, 0, 0.0,
            fake_ibapi.MANUAL_CLIENT_ID, "", 0.0,
        )  # fmt: skip
        client.wait_dispatched()
        healed = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
    for snapshot in (first, second):
        assert snapshot[fake_ibapi.FOREIGN_ID].lifecycle is UNKNOWN
        assert snapshot[fake_ibapi.FOREIGN_ID].quantity_quarantine is not None
        assert snapshot[fake_ibapi.OWNED_ID].lifecycle is OrderLifecycle.PARTIALLY_FILLED
    assert healed[fake_ibapi.FOREIGN_ID].lifecycle is OrderLifecycle.SUBMITTED
    assert healed[fake_ibapi.FOREIGN_ID].quantity_quarantine is None
    assert MANAGER_FAILURE not in readiness.reasons


# --- call site 2: the completed-orders record (already total; pinned unchanged) -------------


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("-1"), "abc"],
    ids=["nan", "inf", "-inf", "negative", "non-numeric"],
)
async def test_a_malformed_completed_record_is_dropped_with_a_notice(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    def poison(name: str, args: tuple[Any, ...]) -> list[tuple[str, tuple]]:
        if name == "completedOrder":
            args[1].filledQuantity = value
        return [(name, args)]

    adapter = OfficialIBKRBroker(
        fake_ibapi.official_settings(), order_ownership=fake_ibapi.InstalledGatewayLedger()
    )
    await adapter.connect()
    try:
        await adapter.cancel_order(fake_ibapi.OWNED_ID)
        assert len(await adapter.completed_orders()) == 1  # control: the record is readable
        _wrap(monkeypatch, gateway, "completed_orders", poison)
        records = await adapter.completed_orders()
    finally:
        await adapter.disconnect()
    assert records == ()
    assert _notices(adapter, "[completed-order]")


# --- call site 3: the submit acknowledgement ------------------------------------------------

TRANSMIT_PERM = 4242


def _transmit_settings() -> Any:
    """Transmit-true exists ONLY in this test-built Settings object, against the in-process fake."""

    return fake_ibapi.official_settings().model_copy(
        update={"allow_order_transmit": True, "ib_account_allowlist": (fake_ibapi.ACCOUNT,)}
    )


def _request() -> OrderRequest:
    return OrderRequest(
        correlation_id="d43-ack",
        account_id=fake_ibapi.ACCOUNT,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        order_ref="CHR-D43-ACK",
        transmit=True,
    )


class _AllowGuard:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield True


def _status_first(filled: object, remaining: object):  # type: ignore[no-untyped-def]
    """The venue answers placeOrder with an orderStatus BEFORE its openOrder (the status branch)."""

    def rewrite(name: str, args: tuple[Any, ...]) -> list[tuple[str, tuple]]:
        if name == "openOrder":
            adapter_id = args[0]
            status = (
                adapter_id, "Submitted", filled, remaining, 0.0, TRANSMIT_PERM, 0, 0.0,
                fake_ibapi.CLIENT_ID, "", 0.0,
            )  # fmt: skip
            return [("orderStatus", status), (name, args)]
        return [(name, args)]

    return rewrite


def _submit(
    gateway: fake_ibapi.FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    filled: object,
    remaining: object,
) -> tuple[Any, OfficialIBKRBroker, _SpyReadiness]:
    _wrap(monkeypatch, gateway, "place_order", _status_first(filled, remaining))
    with _session(_transmit_settings()) as (broker, manager, readiness):
        submission = manager.run(broker.submit_order(_request(), send_guard=_AllowGuard()))
    return submission, broker, readiness


def test_control_a_well_formed_status_ack_is_typed_by_its_status(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission, _broker, readiness = _submit(gateway, monkeypatch, 0.0, 1.0)
    assert submission.lifecycle is OrderLifecycle.SUBMITTED
    assert submission.permanent_id == TRANSMIT_PERM
    assert submission.message == ""
    assert MANAGER_FAILURE not in readiness.reasons


@pytest.mark.parametrize("side", list(SIDES))
@pytest.mark.parametrize(("value", "cls"), WIRE, ids=WIRE_IDS)
def test_a_malformed_ack_figure_is_the_typed_unknown_with_its_cause_and_identity_kept(
    gateway: fake_ibapi.FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    value: object,
    cls: str,
) -> None:
    filled, remaining = (value, 1.0) if side == "filled" else (0.0, value)
    submission, broker, readiness = _submit(gateway, monkeypatch, filled, remaining)
    assert submission.lifecycle is UNKNOWN
    assert f"{side}_quantity is {cls}" in submission.message
    assert "malformed broker fill figures" in submission.message
    # identity kept, so the boundary persists it for reconciliation (no figures are on the ack)
    assert submission.permanent_id == TRANSMIT_PERM
    assert submission.broker_order_id > 0
    assert _notices(broker, "[order-quantity]")
    assert MANAGER_FAILURE not in readiness.reasons


# --- call site 4: the cancel acknowledgement (reads no venue figure; unchanged) -------------


@pytest.mark.parametrize(("value", "_cls"), WIRE, ids=WIRE_IDS)
def test_a_malformed_cancel_ack_figure_changes_nothing_the_status_decides(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, value: object, _cls: str
) -> None:
    _wrap(
        monkeypatch, gateway, "cancel_order", _poison_status(fake_ibapi.OWNED_ID, "filled", value)
    )
    with _session() as (broker, manager, readiness):
        result = manager.run(broker.cancel_order(fake_ibapi.OWNED_ID))
    # the first ack is PendingCancel: the cancel site decides by the status word alone, as before
    assert result.lifecycle is OrderLifecycle.CANCEL_PENDING
    assert result.message == "PendingCancel"
    assert MANAGER_FAILURE not in readiness.reasons


# --- the portfolio reconciler -----------------------------------------------------------------


class _Local:
    def read(self, current_account_id: str) -> LocalReconciliationEvidence:
        del current_account_id
        return LocalReconciliationEvidence(complete=True)


def _reconcile(manager: BrokerConnectionManager) -> Any:
    coordinator = ReconciliationCoordinator(manager, _Local(), ("AAPL",))  # type: ignore[arg-type]
    return coordinator.reconcile()


def test_control_the_reconciler_publishes_a_snapshot_for_well_formed_figures(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    with _session() as (_broker, manager, readiness):
        result = _reconcile(manager)
    assert result.snapshot is not None
    assert MANAGER_FAILURE not in readiness.reasons


@pytest.mark.parametrize(("value", "cls"), WIRE[:4], ids=WIRE_IDS[:4])
def test_the_reconciler_refuses_a_quarantined_row_by_name_and_persists_no_placeholder(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, value: object, cls: str
) -> None:
    _wrap(monkeypatch, gateway, "open_orders", _poison_status(fake_ibapi.OWNED_ID, "filled", value))
    with _session() as (_broker, manager, readiness):
        result = _reconcile(manager)
    assert result.status is ReconciliationStatus.PENDING
    # no snapshot view: the placeholder figures never reach ReconciliationOrderView / the run record
    assert result.snapshot is None
    named = [reason for reason in result.reasons if str(fake_ibapi.OWNED_ID) in reason]
    assert named, result.reasons
    assert f"filled_quantity is {cls}" in named[0]
    # a typed refusal, not "Broker evidence could not be captured" from an exception
    assert not any("could not be captured" in reason for reason in result.reasons)
    assert MANAGER_FAILURE not in readiness.reasons


# --- the restart pass, the settle planner and operator resolve (real reconciler, real book) ---

CAUSE = "filled_quantity is not finite (NaN)"


def _quarantined() -> BrokerOrder:
    row = venue_order(UNKNOWN, "0")
    return BrokerOrder(**{**dict(row), "quantity_quarantine": CAUSE})


@pytest.fixture
def rig() -> Iterator[Rig]:
    built = make_rig(ScriptedVenue())
    built.seed()
    try:
        yield built
    finally:
        built.close()


def test_control_a_well_formed_row_proves_the_submitted_intent(rig: Rig) -> None:
    rig.venue.open = [venue_order(OrderLifecycle.SUBMITTED, "0")]
    report = rig.reconcile()
    assert issues(report) == []


def test_the_restart_pass_leaves_the_intent_locked_and_writes_nothing(rig: Rig) -> None:
    rig.venue.open = [_quarantined()]
    before = rig.events()
    report = rig.reconcile()
    assert rig.events() == before  # no placeholder figure was persisted
    assert rig.status() is OrderLifecycle.SUBMITTED
    assert any(CAUSE in reason for reason in issues(report))
    assert report.permits_new_opening_order is False


def test_the_settle_planner_refuses_a_quarantined_row_with_its_cause(rig: Rig) -> None:
    rig.pending()
    rig.venue.open = [_quarantined()]
    before = rig.events()
    report = rig.reconcile()
    assert rig.events() == before
    assert rig.status() is OrderLifecycle.CANCEL_PENDING
    assert any(CAUSE in reason for reason in issues(report))


def test_operator_resolve_never_writes_rejected_against_a_quarantined_row(rig: Rig) -> None:
    rig.event(UNKNOWN, at=EARLIER_TODAY, source="SUBMIT")
    rig.venue.open = [_quarantined()]
    before = rig.events()
    with pytest.raises(ValueError, match="does not settle this intent"):
        rig.reconciler.operator_resolve(
            "intent-1", operator_note="d43 pin", current_account_id=rig.account, now=NOW
        )
    assert rig.events() == before
    assert rig.status() is UNKNOWN


def test_control_operator_resolve_reads_absence_differently(rig: Rig) -> None:
    # The hazard Q2 ruled out: had the quarantined row been DROPPED, the order would read absent.
    rig.event(UNKNOWN, at=EARLIER_TODAY, source="SUBMIT")
    rig.venue.open = []
    with pytest.raises(ValueError, match="absence cannot safely prove rejection"):
        rig.reconciler.operator_resolve(
            "intent-1", operator_note="d43 pin", current_account_id=rig.account, now=NOW
        )


# --- D43-FIX1: a quarantined row demotes readiness and the evidence path refuses it -----------
# Kimi's HOLD (out/REVIEW-D43-SEC-xreview-kimi31.md): a malformed own-order figure arriving AFTER a
# valid reconciliation was quarantined, but readiness stayed READY and the risk evidence said
# RECONCILED, so the next opening submission could claim the stale proof.

QUARANTINE_REASON = "broker open order fill figures quarantined; reconciliation required"


def _drop_replayed_status(name: str, args: tuple[Any, ...]) -> list[tuple[str, tuple]]:
    # the snapshot does not re-send statuses, so the cached (poisoned) figure is what it reads
    return [] if name == "orderStatus" else [(name, args)]


def _raising_hook(_reason: str) -> None:
    raise RuntimeError("synthetic readiness invalidation failure")


@contextmanager
def _ready_window(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, *, hook: str = "hooked"
) -> Iterator[tuple[OfficialIBKRBroker, BrokerConnectionManager, ReconciliationReadiness, Any]]:
    """Kimi's probe fixture: an own order, a prior READY proof, then that order's filled figure
    poisoned through the real EWrapper. ``hook`` is how the adapter was built (D43-FIX2, Kimi's
    delta probe): ``hooked`` (the runtime's wiring), ``unhooked`` (no readiness hook) or
    ``raising`` (a hook that raises)."""

    _wrap(monkeypatch, gateway, "open_orders", _drop_replayed_status)
    readiness = ReconciliationReadiness(session_id="d43-fix1-ready-window")
    hooks: dict[str, Callable[[str], object] | None] = {
        "hooked": readiness.invalidate,
        "unhooked": None,
        "raising": _raising_hook,
    }
    broker = OfficialIBKRBroker(
        fake_ibapi.official_settings(),
        on_connection_uncertain=hooks[hook],
        on_managed_account_scope_change=readiness.invalidating_transition,
        order_ownership=fake_ibapi.InstalledGatewayLedger(),
    )
    manager = BrokerConnectionManager(broker, readiness=readiness)
    manager.connect()
    try:
        broker.bridge.note_own_order(fake_ibapi.OWNED_ID)
        client = gateway._clients[0]
        client.emit(
            "orderStatus", fake_ibapi.OWNED_ID, "Submitted", 5.0, 5.0, 0.0, 0, 0, 0.0,
            fake_ibapi.CLIENT_ID, "", 0.0,
        )  # fmt: skip
        client.emit(
            "orderStatus", fake_ibapi.FOREIGN_ID, "Submitted", 0.0, 1.0, 0.0, 0, 0, 0.0,
            fake_ibapi.MANUAL_CLIENT_ID, "", 0.0,
        )  # fmt: skip
        client.wait_dispatched()
        assert readiness.complete(
            expected_generation=readiness.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="synthetic prior parity proof",
            reconciled_at=FIXED_NOW,
        )
        assert readiness.snapshot().ready  # the window opens on a valid READY proof
        client.emit(
            "orderStatus", fake_ibapi.OWNED_ID, "Submitted", float("nan"), 5.0, 0.0, 0, 0, 0.0,
            fake_ibapi.CLIENT_ID, "", 0.0,
        )  # fmt: skip
        client.wait_dispatched()
        yield broker, manager, readiness, client
    finally:
        manager.close()


def _gather(manager: BrokerConnectionManager, readiness: ReconciliationReadiness) -> Any:
    provider = BrokerRiskEvidenceProvider(
        connection=manager,
        settings=fake_ibapi.official_settings(),
        reconciliation_readiness=readiness,
    )
    intent = build_option_intent(
        account_id=fake_ibapi.ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
    )
    return provider.gather(intent, now=FIXED_NOW)


def test_fix1_kimi_probe_ready_then_poisoned_row_refuses_evidence_and_the_guard(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _ready_window(gateway, monkeypatch) as (broker, manager, readiness, _client):
        generation = readiness.snapshot().generation
        evidence = _gather(manager, readiness)
        rows = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        after = readiness.snapshot()
        with readiness.submission_guard(expected_generation=generation) as acquired:
            pass
    assert rows[fake_ibapi.OWNED_ID].quantity_quarantine is not None
    assert evidence.reconciliation_status is ReconciliationStatus.PENDING
    assert evidence.reconciliation_generation is None
    assert after.ready is False
    assert after.reason == QUARANTINE_REASON
    assert acquired is False


def test_fix1_control_well_formed_figures_keep_the_ready_proof_usable(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _ready_window(gateway, monkeypatch) as (_broker, manager, readiness, client):
        client.emit(
            "orderStatus",
            fake_ibapi.OWNED_ID,
            "Submitted",
            5.0,
            5.0,
            0.0,
            0,
            0,
            0.0,
            fake_ibapi.CLIENT_ID,
            "",
            0.0,
        )  # fmt: skip  (the venue corrects the figure)
        client.wait_dispatched()
        evidence = _gather(manager, readiness)
        after = readiness.snapshot()
    assert evidence.reconciliation_status is ReconciliationStatus.RECONCILED
    assert after.ready


@pytest.mark.parametrize(("value", "cls"), WIRE, ids=WIRE_IDS)
def test_fix1_every_quarantined_read_invalidates_readiness_with_its_cause(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, value: object, cls: str
) -> None:
    _wrap(monkeypatch, gateway, "open_orders", _poison_status(fake_ibapi.OWNED_ID, "filled", value))
    with _session() as (broker, manager, readiness):
        before = list(readiness.reasons)
        manager.run(broker.open_orders())
        manager.run(broker.open_orders())
        added = readiness.reasons[len(before) :]
    # one typed invalidation per quarantining read (the RC-1 classifier adds its own unsolicited
    # reasons for the snapshot's statuses), never the connection-failure path
    assert added.count(QUARANTINE_REASON) == 2
    assert set(added) - {QUARANTINE_REASON} <= {
        "unsolicited broker order status observed; reconciliation required"
    }
    assert MANAGER_FAILURE not in readiness.reasons
    del cls


# --- D43-FIX2 (D-53): the evidence path refuses on its own; a raising hook fails the read closed --
# Kimi's delta review (HOLD P1=1 @ e3a975d): guard 1 is optional and fail-open. An adapter built
# without the readiness hook, or with one that raises, left READY usable and the evidence
# RECONCILED.


@pytest.mark.parametrize("hook", ["hooked", "unhooked"])
def test_fix2_the_evidence_refuses_a_quarantined_row_whatever_the_hook(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch, hook: str
) -> None:
    """READY, then a poisoned own-order figure: the risk evidence is not RECONCILED and carries no
    generation, so the submission guard, claimed the way the pipeline claims it (the evidence's
    generation, re-checked against readiness at submit time), is never acquired."""

    with _ready_window(gateway, monkeypatch, hook=hook) as (broker, manager, readiness, _client):
        evidence = _gather(manager, readiness)
        rows = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        after = readiness.snapshot()
        claim = evidence.reconciliation_generation
        with readiness.submission_guard(expected_generation=cast(int, claim)) as acquired:
            pass
    assert rows[fake_ibapi.OWNED_ID].quantity_quarantine is not None
    assert evidence.reconciliation_status is ReconciliationStatus.PENDING
    assert claim is None
    assert acquired is False
    # unhooked, nothing demoted readiness: the refusal above is the evidence path's own
    assert (after.ready, after.reason) == {
        "hooked": (False, QUARANTINE_REASON),
        "unhooked": (True, "synthetic prior parity proof"),
    }[hook]


def test_fix2_a_raising_hook_fails_the_quarantining_read_closed(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook raises: the read raises a typed BrokerSafetyError naming the order, the connection
    manager's own invalidation demotes readiness, and the old READY proof can no longer be claimed
    by the evidence path, position admission (its broker read fails) or the guard."""

    with _ready_window(gateway, monkeypatch, hook="raising") as (broker, manager, readiness, _c):
        generation = readiness.snapshot().generation
        with pytest.raises(
            BrokerSafetyError, match=rf"open order {fake_ibapi.OWNED_ID} .*not finite"
        ):
            manager.run(broker.open_orders())
        with pytest.raises(BrokerSafetyError):
            _gather(manager, readiness)
        after = readiness.snapshot()
        with readiness.submission_guard(expected_generation=generation) as acquired:
            pass
    assert after.ready is False
    assert after.reason == MANAGER_FAILURE
    assert acquired is False


def test_fix2_control_a_working_hook_still_returns_the_quarantined_row(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q2 stands when the hook works: the read returns the typed quarantine, it does not raise."""

    with _ready_window(gateway, monkeypatch) as (broker, manager, readiness, _client):
        rows = {row.broker_order_id: row for row in manager.run(broker.open_orders())}
        after = readiness.snapshot()
    assert rows[fake_ibapi.OWNED_ID].quantity_quarantine is not None
    assert rows[fake_ibapi.FOREIGN_ID].quantity_quarantine is None
    assert after.reason == QUARANTINE_REASON

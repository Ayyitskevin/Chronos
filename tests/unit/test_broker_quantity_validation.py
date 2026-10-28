"""D-43: venue fill figures are totally validated at the status-mapper boundary (RED order plane).

Kevin's rulings (2026-10-26, Q1-Q4): a malformed figure — NaN, sNaN, ±Infinity or negative, on
EITHER side — refuses to ``SUBMISSION_UNKNOWN`` (Q1, reused: every reader already fails closed on
it), whatever the status word says, and never raises; a malformed open-orders row is a typed
quarantine on the snapshot (Q2), never a substituted figure that reaches persistence. Finite
non-negative figures map exactly as before.

The expected outcomes come from the sealed pre-r0 threat map (``out/D43-THREATMAP.md`` §5, rows
1-7, each probed on 572be6d), not from the implementation. The adapter call sites, the reconciler
and the restart pass are pinned over the FakeGateway in
``tests/integration/test_quantity_quarantine_gateway.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.order_fakes import PAPER_ACCOUNT
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerOrder, UnderlyingContract
from chronos.orders.reconciliation_recovery import resolve_from_broker_evidence
from chronos.orders.tracker import broker_status_to_lifecycle
from chronos.persistence.order_repositories import OrderIntentRecord
from chronos.utils.identifiers import account_fingerprint

UNKNOWN = OrderLifecycle.SUBMISSION_UNKNOWN
NAN = Decimal("NaN")
SNAN = Decimal("sNaN")
INF = Decimal("Infinity")
NEG_INF = Decimal("-Infinity")
HUGE = Decimal("1E+999999")

# Every spelling the mapper recognises, stated independently of the implementation.
TERMINAL_SPELLINGS = (
    "Cancelled",
    "Canceled",
    "ApiCancelled",
    "ApiCanceled",
    "Inactive",
    "Rejected",
    "ApiRejected",
    "PendingCancel",
)
WORKING_SPELLINGS = ("Submitted", "PreSubmitted", "PendingSubmit")
FILL_SPELLINGS = ("Filled", "PartiallyFilled")
MALFORMED = (NAN, SNAN, INF, NEG_INF, Decimal("-1"), Decimal("-0.00000001"))


def _map(status: str, filled: Decimal, remaining: Decimal) -> OrderLifecycle:
    return broker_status_to_lifecycle(status, filled_quantity=filled, remaining_quantity=remaining)


# --- the threat map's section 5, row by row (each output observed on 572be6d) -----------------

THREAT_MAP_ROWS = [
    # row 1: a working status with NaN on either side RAISED decimal.InvalidOperation
    ("row1", "Submitted", NAN, Decimal("5")),
    ("row1", "Submitted", NAN, NAN),
    ("row1", "Submitted", Decimal("5"), NAN),
    ("row1", "Submitted", SNAN, Decimal("5")),
    ("row1", "PendingSubmit", NAN, NAN),
    # row 2: Filled with NaN remaining RAISED at the terminal check
    ("row2", "Filled", Decimal("10"), NAN),
    # row 3: Filled with NaN filled and zero remaining mapped FILLED
    ("row3", "Filled", NAN, Decimal("0")),
    # row 4: negatives on recognised statuses were accepted
    ("row4", "Filled", Decimal("10"), Decimal("-1")),
    ("row4", "Filled", Decimal("-10"), Decimal("0")),
    ("row4", "Filled", Decimal("10"), NEG_INF),
    ("row4", "Submitted", Decimal("-5"), Decimal("5")),
    ("row4", "Submitted", Decimal("5"), Decimal("-5")),
    ("row4", "Submitted", NEG_INF, NEG_INF),
    # row 5: +Infinity on a working status mapped PARTIALLY_FILLED (and wedged readiness later)
    ("row5", "Submitted", INF, INF),
    ("row5", "Submitted", INF, Decimal("5")),
    ("row5", "Submitted", Decimal("5"), INF),
    ("row5", "PreSubmitted", INF, INF),
    # row 6: PartiallyFilled decided by the status word alone
    ("row6", "PartiallyFilled", NAN, NAN),
    ("row6", "PartiallyFilled", INF, NEG_INF),
    ("row6", "PartiallyFilled", Decimal("-1"), Decimal("-1")),
    # row 7: terminal and administrative statuses never saw their figures
    ("row7", "Cancelled", NAN, NAN),
    ("row7", "PendingCancel", NAN, NAN),
    ("row7", "Inactive", NEG_INF, INF),
]


@pytest.mark.parametrize(
    ("row", "status", "filled", "remaining"),
    THREAT_MAP_ROWS,
    ids=[f"{r}-{s}-{f}-{m}" for r, s, f, m in THREAT_MAP_ROWS],
)
def test_threat_map_rows_1_to_7_refuse_to_the_typed_unknown(
    row: str, status: str, filled: Decimal, remaining: Decimal
) -> None:
    del row
    assert _map(status, filled, remaining) is UNKNOWN


@pytest.mark.parametrize("status", TERMINAL_SPELLINGS + FILL_SPELLINGS + WORKING_SPELLINGS)
@pytest.mark.parametrize("figure", MALFORMED, ids=str)
@pytest.mark.parametrize("side", ["filled", "remaining"])
def test_the_refusal_runs_before_status_precedence_for_every_recognised_spelling(
    status: str, figure: Decimal, side: str
) -> None:
    # Threat map §3.2: refusal precedes precedence. A finite positive partner keeps the other
    # side well formed, so only the malformed side can decide the outcome.
    filled, remaining = (figure, Decimal("5")) if side == "filled" else (Decimal("5"), figure)
    assert _map(status, filled, remaining) is UNKNOWN, (
        f"{status} with {side}={figure} must refuse before its status decides"
    )


# --- totality and "byte-identical otherwise" ----------------------------------------------------

_ANY_DECIMAL = st.decimals(allow_nan=True, allow_infinity=True)
_STATUS = st.one_of(
    st.sampled_from(TERMINAL_SPELLINGS + FILL_SPELLINGS + WORKING_SPELLINGS + ("ApiPending",)),
    st.text(max_size=12),
)


@settings(max_examples=400)
@given(status=_STATUS, filled=_ANY_DECIMAL, remaining=_ANY_DECIMAL)
def test_the_mapper_is_total_and_a_malformed_side_is_always_the_typed_unknown(
    status: str, filled: Decimal, remaining: Decimal
) -> None:
    result = _map(status, filled, remaining)  # must never raise, for any Decimal pair
    assert isinstance(result, OrderLifecycle)
    if any(not value.is_finite() or value < 0 for value in (filled, remaining)):
        assert result is UNKNOWN


def _mapping_at_572be6d(status: str, filled: Decimal, remaining: Decimal) -> OrderLifecycle:
    """The base mapper's decision table for FINITE NON-NEGATIVE figures, restated as the oracle.

    Copied from ``tracker.py:107-133`` at 572be6d (D-27 / FIX-27 semantics), so a change to how a
    well-formed figure maps fails here, not only in the four hand-picked pins.
    """

    normalized = status.strip().lower()
    if normalized in {"cancelled", "canceled", "apicancelled", "apicanceled"}:
        return OrderLifecycle.CANCELLED
    if normalized in {"inactive", "rejected", "apirejected"}:
        return OrderLifecycle.REJECTED
    if normalized == "pendingcancel":
        return OrderLifecycle.CANCEL_PENDING
    if normalized == "filled" and remaining <= 0:
        return OrderLifecycle.FILLED
    working = normalized in {"submitted", "presubmitted", "pendingsubmit"}
    if normalized in {"filled", "partiallyfilled"} or (working and filled > 0 and remaining > 0):
        return OrderLifecycle.PARTIALLY_FILLED
    if working:
        return OrderLifecycle.SUBMITTED
    return UNKNOWN


_WELL_FORMED = st.decimals(min_value=0, max_value=Decimal("1e12"), places=8)


@settings(max_examples=400)
@given(
    status=_STATUS,
    filled=st.one_of(st.just(Decimal("0")), _WELL_FORMED),
    remaining=st.one_of(st.just(Decimal("0")), _WELL_FORMED),
)
def test_finite_non_negative_figures_map_exactly_as_before(
    status: str, filled: Decimal, remaining: Decimal
) -> None:
    assert _map(status, filled, remaining) is _mapping_at_572be6d(status, filled, remaining)


def test_zero_and_negative_zero_are_well_formed_and_huge_finite_figures_are_out_of_scope() -> None:
    # Zero is a legitimate figure (a fresh order; a fully filled one); "-0" is zero, not negative.
    assert _map("Filled", Decimal("10"), Decimal("0")) is OrderLifecycle.FILLED
    assert _map("Submitted", Decimal("0"), Decimal("10")) is OrderLifecycle.SUBMITTED
    assert _map("Submitted", Decimal("-0"), Decimal("10")) is OrderLifecycle.SUBMITTED
    # Q3 (out of this PR, a residual): a huge FINITE figure passes the check unchanged.
    assert _map("Filled", HUGE, Decimal("0")) is OrderLifecycle.FILLED


# --- the cause the notice carries (Q1) -------------------------------------------------------


def test_quantity_refusal_names_the_field_and_the_class_and_accepts_well_formed_figures() -> None:
    # imported here so the mapper pins above report per row when the seam is missing (the RED)
    from chronos.orders.tracker import quantity_refusal

    assert quantity_refusal(Decimal("0"), Decimal("10")) is None
    assert quantity_refusal(Decimal("10"), Decimal("0")) is None
    assert quantity_refusal(HUGE, Decimal("0")) is None  # Q3: no magnitude bound
    assert quantity_refusal(NAN, Decimal("1")) == "filled_quantity is not finite (NaN)"
    assert quantity_refusal(SNAN, Decimal("1")) == "filled_quantity is not finite (sNaN)"
    assert quantity_refusal(Decimal("1"), INF) == "remaining_quantity is not finite (Infinity)"
    assert quantity_refusal(Decimal("1"), NEG_INF) == "remaining_quantity is not finite (-Infinity)"
    assert quantity_refusal(Decimal("-2"), Decimal("1")) == "filled_quantity is negative (-2)"
    assert (
        quantity_refusal(Decimal("1"), Decimal("-0.5")) == "remaining_quantity is negative (-0.5)"
    )
    # both malformed: the filled side is reported first
    assert quantity_refusal(Decimal("-1"), NAN) == "filled_quantity is negative (-1)"


# --- the Q2 quarantine row ------------------------------------------------------------------

ACCOUNT = PAPER_ACCOUNT
CLIENT_ID = 17
REF = "CHR-ORD-" + "Q" * 32
BROKER_ID = 9001
PERM_ID = 109001
QUANTITY = Decimal("10")
LIMIT = Decimal("150.00")
CON_ID = 265598
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
CAUSE = "filled_quantity is not finite (NaN)"


def _row(**update: object) -> BrokerOrder:
    fields: dict[str, object] = {
        "broker_order_id": BROKER_ID,
        "permanent_id": PERM_ID,
        "client_id": CLIENT_ID,
        "account_id": ACCOUNT,
        "order_ref": REF,
        "contract": UnderlyingContract(con_id=CON_ID, symbol="AAPL"),
        "side": OrderSide.BUY,
        "quantity": QUANTITY,
        "filled_quantity": Decimal("0"),
        "remaining_quantity": QUANTITY,
        "limit_price": LIMIT,
        "lifecycle": UNKNOWN,
        "quantity_quarantine": CAUSE,
    }
    fields.update(update)
    return BrokerOrder(**fields)  # type: ignore[arg-type]


def test_a_quarantined_row_has_exactly_one_shape() -> None:
    row = _row()
    assert row.quantity_quarantine == CAUSE
    # a quarantine is always the typed unknown with the placeholder figures (nothing filled, the
    # whole order outstanding): a reader can rely on that shape, and nothing else can carry a cause
    for bad in (
        {"lifecycle": OrderLifecycle.SUBMITTED},
        {"lifecycle": OrderLifecycle.PARTIALLY_FILLED},
        {"filled_quantity": Decimal("5"), "remaining_quantity": Decimal("5")},
        {"quantity_quarantine": "   "},
    ):
        with pytest.raises(ValidationError):
            _row(**bad)


def test_an_unquarantined_row_serialises_byte_identically_to_before() -> None:
    # No new key on any un-quarantined row: snapshot signatures, the admission payload and every
    # JSON dump of a well-formed row are unchanged by D-43.
    row = _row(quantity_quarantine=None, lifecycle=OrderLifecycle.SUBMITTED)
    assert "quantity_quarantine" not in row.model_dump()
    assert "quantity_quarantine" not in row.model_dump_json()
    assert "quantity_quarantine" in _row().model_dump_json()


def _intent(status: OrderLifecycle) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id="intent-1",
        idempotency_key="idem-intent-1",
        account_fingerprint=account_fingerprint(ACCOUNT, key=TEST_PERSISTENCE_KEY),
        environment="paper",
        product_family=ProductFamily.STOCK,
        wheel_cycle_id=None,
        symbol="AAPL",
        con_id=CON_ID,
        local_symbol=None,
        action=OrderSide.BUY,
        open_close_effect="OPEN",
        quantity=QUANTITY,
        order_type="LMT",
        limit_price=LIMIT,
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=REF,
        status=status,
        created_at=NOW,
        confirmed_at=NOW,
        submitted_at=None,
        expires_at=None,
    )


def _resolve(intent_status: OrderLifecycle, row: BrokerOrder) -> object:
    return resolve_from_broker_evidence(
        _intent(intent_status),
        open_orders=(row,),
        executions=(),
        current_account_id=ACCOUNT,
        expected_broker_client_id=CLIENT_ID,
        expected_limit_price=LIMIT,
        persisted_broker_order_id=BROKER_ID,
        persisted_permanent_id=PERM_ID,
        now=NOW,
    )


@pytest.mark.parametrize(
    "intent_status",
    [
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.SUBMISSION_UNKNOWN,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.CANCEL_PENDING,
    ],
)
def test_a_quarantined_row_never_resolves_an_intent(intent_status: OrderLifecycle) -> None:
    # Nothing is derived from the placeholder: no update, so nothing is ingested or persisted, the
    # intent stays exactly as recorded and locked, and operator resolve sees the order PRESENT.
    assert _resolve(intent_status, _row()) is None
    # positive control: the same row with real, well-formed figures does resolve
    control = _row(quantity_quarantine=None, lifecycle=OrderLifecycle.SUBMITTED)
    assert _resolve(intent_status, control) is not None


# --- the recorded residuals (Q3, Q4) say what the source does ---------------------------------

_ROOT = Path(__file__).resolve().parents[2]


def _limitations() -> str:
    return " ".join((_ROOT / "docs" / "limitations.md").read_text(encoding="utf-8").split())


def test_the_three_residual_lines_are_recorded_and_match_the_source() -> None:
    from chronos.config.settings import Settings
    from chronos.domain.enums import BrokerAdapter
    from chronos.orders.tracker import quantity_refusal

    text = _limitations()
    # Q3: finite and non-negative only — executed, not asserted
    assert "checked for finite and non-negative only, never for magnitude (D-43, Q3)" in text
    assert quantity_refusal(HUGE, Decimal("0")) is None
    # Q4: the positions and executions legs
    assert "The positions and executions legs are outside D-43 (Q4)" in text
    # Q4: the ib_async mapper, which is not the default adapter and still raises on a bad status
    assert "The `ib_async` adapter keeps its own status mapper (D-43, Q4)" in text
    assert Settings.model_fields["broker_adapter"].default is BrokerAdapter.OFFICIAL_IBKR
    ib_async = (_ROOT / "src" / "chronos" / "broker" / "ibkr.py").read_text(encoding="utf-8")
    assert 'raise BrokerDataError("IBKR open order has an unknown lifecycle status")' in ib_async


# --- D43-FIX1: position admission refuses a quarantined row on its own ------------------------


def _admission_with(open_orders: tuple[BrokerOrder, ...]) -> object:
    from tests.safety import test_managed_position_admission as admission_suite

    from chronos.persistence.database import Database
    from chronos.supervisor.position_admission import ManagedPositionAdmission

    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        admission_suite._seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=admission_suite._Runner(admission_suite._Broker(open_orders=open_orders)),
            sessions=database.sessions,
            readiness=admission_suite._readiness(),
            account_id=admission_suite._ACCOUNT,
            persistence_key=TEST_PERSISTENCE_KEY,
        )
        return admission.admit_opening(admission_suite._ORDER_REF, admission_suite._NOW)
    finally:
        database.dispose()


def test_fix1_admission_refuses_a_quarantined_row() -> None:
    from chronos.supervisor.position_admission import (
        AdmittedManagedPosition,
        OpeningAdmissionRefusalCode,
        RefusedManagedPositionAdmission,
    )

    # another symbol's order, so only the quarantine itself can be the reason to refuse
    working = _row(quantity_quarantine=None, lifecycle=OrderLifecycle.SUBMITTED)
    assert isinstance(_admission_with((working,)), AdmittedManagedPosition)  # control
    result = _admission_with((_row(),))
    assert isinstance(result, RefusedManagedPositionAdmission)
    assert result.code is OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE
    assert CAUSE in result.detail

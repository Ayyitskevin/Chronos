from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from chronos.domain.enums import OptionRight
from chronos.services.short_put_demo_approval import (
    ShortPutDemoApprovalContract,
    ShortPutDemoApprovalReceipt,
)
from chronos.ui.rehearsal_state import (
    DEFAULT_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
    MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
    DemoApprovalDisposition,
    DemoApprovalDispositionReason,
    DemoApprovalSessionRecord,
    abandon_demo_approval_record,
    refresh_demo_approval_record,
    retain_demo_approval_receipt,
    supersede_demo_approval_record,
)

APPROVAL_REFERENCE = "CHR-APR-0123456789ABCDEF0123456789ABCDEF"
SECOND_APPROVAL_REFERENCE = "CHR-APR-FEDCBA9876543210FEDCBA9876543210"
WHAT_IF_REFERENCE = "CHR-WIF-00112233445566778899AABBCCDDEEFF"
SECOND_WHAT_IF_REFERENCE = "CHR-WIF-FFEEDDCCBBAA99887766554433221100"
RETAINED_AT = 100.0
RETENTION_SECONDS = 20.0


def _receipt(
    *,
    approval_reference: str = APPROVAL_REFERENCE,
    what_if_reference: str = WHAT_IF_REFERENCE,
    symbol: str = "SPY",
) -> ShortPutDemoApprovalReceipt:
    what_if_evaluated_at = datetime(2026, 7, 16, 14, 30, tzinfo=UTC)
    contract = ShortPutDemoApprovalContract(
        con_id=81_234_567,
        symbol=symbol,
        expiration=date(2026, 8, 21),
        strike=Decimal("600"),
        right=OptionRight.PUT,
        currency="USD",
        multiplier=Decimal("100"),
        min_tick=Decimal("0.01"),
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )
    return ShortPutDemoApprovalReceipt(
        approval_reference=approval_reference,
        what_if_reference=what_if_reference,
        symbol=symbol,
        selected_contract=contract,
        quantity=1,
        limit_price=Decimal("2.50"),
        gross_assignment_obligation=Decimal("60000"),
        currency="USD",
        what_if_evaluated_at=what_if_evaluated_at,
        approval_rehearsed_at=what_if_evaluated_at + timedelta(seconds=1),
        risk_terms_acknowledged=True,
        demo_rehearsal_only=True,
        lifecycle_transition_recorded=False,
        persistence_recorded=False,
        approval_authority_created=False,
        broker_request_included=False,
        broker_text_included=False,
        submission_authorized=False,
        submission_locked=True,
        opening_actions_locked=True,
    )


def _retained_record(
    *,
    receipt: ShortPutDemoApprovalReceipt | None = None,
) -> DemoApprovalSessionRecord:
    return retain_demo_approval_receipt(
        receipt or _receipt(),
        now_monotonic=RETAINED_AT,
        retention_seconds=RETENTION_SECONDS,
    )


def test_retain_receipt_uses_default_display_lease_and_locked_safe_defaults() -> None:
    receipt = _receipt()

    record = retain_demo_approval_receipt(receipt, now_monotonic=RETAINED_AT)

    assert DEFAULT_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS == 15 * 60.0
    assert record == DemoApprovalSessionRecord(
        approval_reference=APPROVAL_REFERENCE,
        symbol="SPY",
        receipt=receipt,
        disposition=DemoApprovalDisposition.RETAINED,
        retained_at_monotonic=RETAINED_AT,
        last_observed_at_monotonic=RETAINED_AT,
        display_expires_at_monotonic=(
            RETAINED_AT + DEFAULT_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS
        ),
        disposed_at_monotonic=None,
        disposition_reason=None,
        authority_created=False,
        persistence_recorded=False,
        actions_locked=True,
    )


def test_retain_receipt_accepts_custom_and_exact_maximum_retention() -> None:
    custom = retain_demo_approval_receipt(
        _receipt(),
        now_monotonic=RETAINED_AT,
        retention_seconds=0.125,
    )
    maximum = retain_demo_approval_receipt(
        _receipt(),
        now_monotonic=RETAINED_AT,
        retention_seconds=MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
    )

    assert MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS == 60 * 60.0
    assert custom.display_expires_at_monotonic == RETAINED_AT + 0.125
    assert maximum.display_expires_at_monotonic == (
        RETAINED_AT + MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS
    )


@pytest.mark.parametrize(
    "retention_seconds",
    [
        pytest.param(True, id="bool"),
        pytest.param("10", id="string"),
        pytest.param(Decimal("10"), id="decimal"),
        pytest.param(None, id="none"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**1000, id="oversized-integer"),
        pytest.param(-0.001, id="negative"),
        pytest.param(0.0, id="zero"),
        pytest.param(
            MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS + 0.001,
            id="above-maximum",
        ),
    ],
)
def test_retain_receipt_rejects_malformed_or_out_of_bounds_retention(
    retention_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="retention_seconds"):
        retain_demo_approval_receipt(
            _receipt(),
            now_monotonic=RETAINED_AT,
            retention_seconds=retention_seconds,  # type: ignore[arg-type]
        )


def test_record_is_retained_at_and_immediately_before_the_deadline() -> None:
    record = _retained_record()

    at_retention = refresh_demo_approval_record(record, now_monotonic=RETAINED_AT)
    just_before_deadline_at = math.nextafter(
        record.display_expires_at_monotonic,
        -math.inf,
    )
    just_before_deadline = refresh_demo_approval_record(
        record,
        now_monotonic=just_before_deadline_at,
    )

    assert at_retention == record
    assert just_before_deadline.disposition is DemoApprovalDisposition.RETAINED
    assert just_before_deadline.last_observed_at_monotonic == just_before_deadline_at
    assert just_before_deadline.display_expires_at_monotonic == (
        record.display_expires_at_monotonic
    )
    assert record.receipt is not None
    assert record.disposition is DemoApprovalDisposition.RETAINED


def test_record_expires_at_the_exact_display_deadline() -> None:
    record = _retained_record()

    expired = refresh_demo_approval_record(
        record,
        now_monotonic=record.display_expires_at_monotonic,
    )

    assert expired.disposition is DemoApprovalDisposition.EXPIRED
    assert expired.disposition_reason is DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED
    assert expired.disposed_at_monotonic == record.display_expires_at_monotonic
    assert expired.receipt is None
    assert expired.approval_reference == record.approval_reference
    assert expired.symbol == record.symbol
    assert expired.retained_at_monotonic == record.retained_at_monotonic
    assert expired.display_expires_at_monotonic == record.display_expires_at_monotonic


def test_monotonic_clock_regression_expires_fail_closed_at_retention_time() -> None:
    record = _retained_record()

    expired = refresh_demo_approval_record(
        record,
        now_monotonic=math.nextafter(RETAINED_AT, -math.inf),
    )

    assert expired.disposition is DemoApprovalDisposition.EXPIRED
    assert expired.disposition_reason is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
    assert expired.disposed_at_monotonic == RETAINED_AT
    assert expired.receipt is None


def test_monotonic_clock_regression_from_last_observation_expires_fail_closed() -> None:
    record = _retained_record()
    observed = refresh_demo_approval_record(
        record,
        now_monotonic=RETAINED_AT + 10.0,
    )

    expired = refresh_demo_approval_record(
        observed,
        now_monotonic=RETAINED_AT + 5.0,
    )

    assert observed.last_observed_at_monotonic == RETAINED_AT + 10.0
    assert expired.disposition is DemoApprovalDisposition.EXPIRED
    assert expired.disposition_reason is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
    assert expired.disposed_at_monotonic == RETAINED_AT + 10.0
    assert expired.last_observed_at_monotonic == RETAINED_AT + 10.0
    assert expired.receipt is None


@pytest.mark.parametrize(
    ("operation", "expected_disposition", "expected_reason"),
    [
        pytest.param(
            "abandon",
            DemoApprovalDisposition.ABANDONED,
            DemoApprovalDispositionReason.OPERATOR_ABANDONED,
            id="abandoned",
        ),
        pytest.param(
            "supersede",
            DemoApprovalDisposition.SUPERSEDED,
            DemoApprovalDispositionReason.NEW_EVIDENCE_ATTEMPT,
            id="superseded",
        ),
    ],
)
def test_explicit_terminal_action_discards_receipt_terms(
    operation: str,
    expected_disposition: DemoApprovalDisposition,
    expected_reason: DemoApprovalDispositionReason,
) -> None:
    record = _retained_record()
    action_at = RETAINED_AT + 5.0

    if operation == "abandon":
        terminal = abandon_demo_approval_record(record, now_monotonic=action_at)
    else:
        terminal = supersede_demo_approval_record(record, now_monotonic=action_at)

    assert terminal.disposition is expected_disposition
    assert terminal.disposition_reason is expected_reason
    assert terminal.disposed_at_monotonic == action_at
    assert terminal.receipt is None
    assert terminal.approval_reference == record.approval_reference
    assert terminal.symbol == record.symbol


@pytest.mark.parametrize("operation", ["abandon", "supersede"])
def test_expiry_or_regression_wins_over_a_requested_terminal_action(operation: str) -> None:
    record = _retained_record()

    if operation == "abandon":
        at_deadline = abandon_demo_approval_record(
            record,
            now_monotonic=record.display_expires_at_monotonic,
        )
        regressed = abandon_demo_approval_record(
            record,
            now_monotonic=math.nextafter(RETAINED_AT, -math.inf),
        )
    else:
        at_deadline = supersede_demo_approval_record(
            record,
            now_monotonic=record.display_expires_at_monotonic,
        )
        regressed = supersede_demo_approval_record(
            record,
            now_monotonic=math.nextafter(RETAINED_AT, -math.inf),
        )

    assert at_deadline.disposition is DemoApprovalDisposition.EXPIRED
    assert at_deadline.disposition_reason is DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED
    assert regressed.disposition is DemoApprovalDisposition.EXPIRED
    assert regressed.disposition_reason is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION


@pytest.mark.parametrize("first", ["abandon", "supersede", "deadline", "regression"])
@pytest.mark.parametrize("later", ["refresh", "abandon", "supersede"])
def test_terminal_disposition_is_first_wins_and_idempotent(first: str, later: str) -> None:
    record = _retained_record()
    if first == "abandon":
        terminal = abandon_demo_approval_record(record, now_monotonic=RETAINED_AT + 1.0)
    elif first == "supersede":
        terminal = supersede_demo_approval_record(record, now_monotonic=RETAINED_AT + 1.0)
    elif first == "deadline":
        terminal = refresh_demo_approval_record(
            record,
            now_monotonic=record.display_expires_at_monotonic,
        )
    else:
        terminal = refresh_demo_approval_record(
            record,
            now_monotonic=math.nextafter(RETAINED_AT, -math.inf),
        )

    if later == "refresh":
        result = refresh_demo_approval_record(terminal, now_monotonic=1_000.0)
    elif later == "abandon":
        result = abandon_demo_approval_record(terminal, now_monotonic=1_000.0)
    else:
        result = supersede_demo_approval_record(terminal, now_monotonic=1_000.0)

    assert result == terminal


def test_exact_replay_neither_extends_nor_resurrects_the_display_lease() -> None:
    receipt = _receipt()
    record = _retained_record(receipt=receipt)

    replayed = retain_demo_approval_receipt(
        receipt.model_copy(),
        now_monotonic=record.display_expires_at_monotonic - 1.0,
        retention_seconds=MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
        previous=record,
    )
    expired_replay = retain_demo_approval_receipt(
        receipt,
        now_monotonic=record.display_expires_at_monotonic,
        retention_seconds=MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS,
        previous=record,
    )
    later_replay = retain_demo_approval_receipt(
        receipt,
        now_monotonic=record.display_expires_at_monotonic + 100.0,
        previous=expired_replay,
    )

    assert replayed.disposition is DemoApprovalDisposition.RETAINED
    assert replayed.last_observed_at_monotonic == record.display_expires_at_monotonic - 1.0
    assert replayed.display_expires_at_monotonic == RETAINED_AT + RETENTION_SECONDS
    assert expired_replay.disposition is DemoApprovalDisposition.EXPIRED
    assert expired_replay.receipt is None
    assert later_replay == expired_replay


def test_conflicting_same_reference_replay_is_rejected() -> None:
    record = _retained_record()
    conflicting_receipt = _receipt(what_if_reference=SECOND_WHAT_IF_REFERENCE)

    with pytest.raises(ValueError, match="conflicting receipt terms"):
        retain_demo_approval_receipt(
            conflicting_receipt,
            now_monotonic=RETAINED_AT + 1.0,
            previous=record,
        )


@pytest.mark.parametrize(
    "previous_kind",
    ["superseded", "abandoned", "expired"],
)
def test_new_receipt_can_follow_terminal_without_history(previous_kind: str) -> None:
    previous = _retained_record()
    if previous_kind == "superseded":
        previous = supersede_demo_approval_record(
            previous,
            now_monotonic=RETAINED_AT + 1.0,
        )
        now = RETAINED_AT + 2.0
    elif previous_kind == "abandoned":
        previous = abandon_demo_approval_record(
            previous,
            now_monotonic=RETAINED_AT + 1.0,
        )
        now = RETAINED_AT + 2.0
    elif previous_kind == "expired":
        previous = refresh_demo_approval_record(
            previous,
            now_monotonic=previous.display_expires_at_monotonic,
        )
        now = previous.display_expires_at_monotonic + 1.0
    current = retain_demo_approval_receipt(
        _receipt(
            approval_reference=SECOND_APPROVAL_REFERENCE,
            what_if_reference=SECOND_WHAT_IF_REFERENCE,
        ),
        now_monotonic=now,
        previous=previous,
    )

    assert current.disposition is DemoApprovalDisposition.RETAINED
    assert "predecessor_reference" not in DemoApprovalSessionRecord.model_fields
    assert "history" not in current.model_dump(mode="python")


def test_new_receipt_requires_active_previous_to_be_terminalized() -> None:
    previous = _retained_record()

    with pytest.raises(ValueError, match="active receipt must be terminalized"):
        retain_demo_approval_receipt(
            _receipt(
                approval_reference=SECOND_APPROVAL_REFERENCE,
                what_if_reference=SECOND_WHAT_IF_REFERENCE,
            ),
            now_monotonic=RETAINED_AT + 1.0,
            previous=previous,
        )

    assert previous.disposition is DemoApprovalDisposition.RETAINED
    assert previous.receipt is not None


@pytest.mark.parametrize("previous_kind", ["retained", "abandoned", "expired"])
def test_new_receipt_cannot_activate_after_monotonic_regression(previous_kind: str) -> None:
    previous = _retained_record()
    if previous_kind == "abandoned":
        previous = abandon_demo_approval_record(
            previous,
            now_monotonic=RETAINED_AT + 5.0,
        )
        regressed_now = RETAINED_AT + 4.0
    elif previous_kind == "expired":
        previous = refresh_demo_approval_record(
            previous,
            now_monotonic=previous.display_expires_at_monotonic,
        )
        regressed_now = previous.display_expires_at_monotonic - 1.0
    else:
        regressed_now = RETAINED_AT - 1.0

    result = retain_demo_approval_receipt(
        _receipt(
            approval_reference=SECOND_APPROVAL_REFERENCE,
            what_if_reference=SECOND_WHAT_IF_REFERENCE,
        ),
        now_monotonic=regressed_now,
        previous=previous,
    )

    assert result.approval_reference == APPROVAL_REFERENCE
    assert result.receipt is None
    if previous_kind == "retained":
        assert result.disposition is DemoApprovalDisposition.EXPIRED
        assert result.disposition_reason is DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
    else:
        assert result == previous


def test_terminal_json_is_an_allowlisted_term_free_tombstone() -> None:
    receipt = _receipt()
    terminal = supersede_demo_approval_record(
        _retained_record(receipt=receipt),
        now_monotonic=RETAINED_AT + 1.0,
    )

    serialized = terminal.model_dump_json()
    payload = json.loads(serialized)

    assert set(payload) == {
        "approval_reference",
        "symbol",
        "receipt",
        "disposition",
        "retained_at_monotonic",
        "last_observed_at_monotonic",
        "display_expires_at_monotonic",
        "disposed_at_monotonic",
        "disposition_reason",
        "authority_created",
        "persistence_recorded",
        "actions_locked",
    }
    assert payload["receipt"] is None
    for forbidden_field in (
        "what_if_reference",
        "selected_contract",
        "con_id",
        "expiration",
        "strike",
        "quantity",
        "limit_price",
        "gross_assignment_obligation",
        "currency",
        "broker_text",
        "account_id",
    ):
        assert forbidden_field not in serialized
    for forbidden_value in (
        receipt.what_if_reference,
        str(receipt.selected_contract.con_id),
        receipt.selected_contract.expiration.isoformat(),
        str(receipt.selected_contract.strike),
        str(receipt.limit_price),
        str(receipt.gross_assignment_obligation),
        receipt.currency,
        "DU1234567 raw broker description",
    ):
        assert forbidden_value not in serialized


@pytest.mark.parametrize(
    "case",
    [
        "invalid-reference",
        "invalid-symbol",
        "non-strict-retained-clock",
        "non-strict-observation-clock",
        "non-strict-expiry-clock",
        "observation-before-retention",
        "observation-at-expiry",
        "non-forward-expiry",
        "overlong-display-lease",
        "missing-receipt",
        "disposed-retained-record",
        "reason-on-retained-record",
        "receipt-reference-mismatch",
        "receipt-symbol-mismatch",
        "authority-created",
        "persistence-recorded",
        "actions-unlocked",
        "extra-broker-text",
    ],
)
def test_retained_model_rejects_malformed_state(case: str) -> None:
    record = _retained_record()
    payload = record.model_dump(mode="python")

    if case == "invalid-reference":
        payload["approval_reference"] = "CHR-APR-not-canonical"
    elif case == "invalid-symbol":
        payload["symbol"] = "spy"
    elif case == "non-strict-retained-clock":
        payload["retained_at_monotonic"] = "100.0"
    elif case == "non-strict-observation-clock":
        payload["last_observed_at_monotonic"] = "100.0"
    elif case == "non-strict-expiry-clock":
        payload["display_expires_at_monotonic"] = True
    elif case == "observation-before-retention":
        payload["last_observed_at_monotonic"] = RETAINED_AT - 1.0
    elif case == "observation-at-expiry":
        payload["last_observed_at_monotonic"] = payload["display_expires_at_monotonic"]
    elif case == "non-forward-expiry":
        payload["display_expires_at_monotonic"] = RETAINED_AT
    elif case == "overlong-display-lease":
        payload["display_expires_at_monotonic"] = (
            RETAINED_AT + MAX_DEMO_APPROVAL_DISPLAY_RETENTION_SECONDS + 0.001
        )
    elif case == "missing-receipt":
        payload["receipt"] = None
    elif case == "disposed-retained-record":
        payload["disposed_at_monotonic"] = RETAINED_AT + 1.0
    elif case == "reason-on-retained-record":
        payload["disposition_reason"] = DemoApprovalDispositionReason.OPERATOR_ABANDONED
    elif case == "receipt-reference-mismatch":
        payload["receipt"] = _receipt(approval_reference=SECOND_APPROVAL_REFERENCE)
    elif case == "receipt-symbol-mismatch":
        payload["receipt"] = _receipt(symbol="QQQ")
    elif case == "authority-created":
        payload["authority_created"] = True
    elif case == "persistence-recorded":
        payload["persistence_recorded"] = True
    elif case == "actions-unlocked":
        payload["actions_locked"] = False
    else:
        payload["raw_broker_text"] = "DU1234567 raw broker description"

    with pytest.raises(ValidationError):
        DemoApprovalSessionRecord.model_validate(payload)


@pytest.mark.parametrize(
    "case",
    [
        "receipt-present",
        "missing-disposal-time",
        "missing-reason",
        "non-strict-disposal-clock",
        "observation-disposal-mismatch",
        "abandonment-at-deadline",
        "supersession-before-retention",
        "expiry-at-wrong-time",
        "regression-at-deadline",
        "wrong-abandonment-reason",
        "wrong-supersession-reason",
    ],
)
def test_terminal_model_rejects_malformed_state(case: str) -> None:
    terminal = abandon_demo_approval_record(
        _retained_record(),
        now_monotonic=RETAINED_AT + 1.0,
    )
    payload = terminal.model_dump(mode="python")

    if case == "receipt-present":
        payload["receipt"] = _receipt()
    elif case == "missing-disposal-time":
        payload["disposed_at_monotonic"] = None
    elif case == "missing-reason":
        payload["disposition_reason"] = None
    elif case == "non-strict-disposal-clock":
        payload["disposed_at_monotonic"] = "101.0"
    elif case == "observation-disposal-mismatch":
        payload["last_observed_at_monotonic"] = RETAINED_AT + 2.0
    elif case == "abandonment-at-deadline":
        payload["disposed_at_monotonic"] = RETAINED_AT + RETENTION_SECONDS
        payload["last_observed_at_monotonic"] = RETAINED_AT + RETENTION_SECONDS
    elif case == "supersession-before-retention":
        payload["disposition"] = DemoApprovalDisposition.SUPERSEDED
        payload["disposition_reason"] = DemoApprovalDispositionReason.NEW_EVIDENCE_ATTEMPT
        payload["disposed_at_monotonic"] = RETAINED_AT - 1.0
        payload["last_observed_at_monotonic"] = RETAINED_AT - 1.0
    elif case == "expiry-at-wrong-time":
        payload["disposition"] = DemoApprovalDisposition.EXPIRED
        payload["disposition_reason"] = DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED
        payload["disposed_at_monotonic"] = RETAINED_AT + 1.0
    elif case == "regression-at-deadline":
        payload["disposition"] = DemoApprovalDisposition.EXPIRED
        payload["disposition_reason"] = DemoApprovalDispositionReason.MONOTONIC_CLOCK_REGRESSION
        payload["disposed_at_monotonic"] = RETAINED_AT + RETENTION_SECONDS
        payload["last_observed_at_monotonic"] = RETAINED_AT + RETENTION_SECONDS
    elif case == "wrong-abandonment-reason":
        payload["disposition_reason"] = DemoApprovalDispositionReason.NEW_EVIDENCE_ATTEMPT
    else:
        payload["disposition"] = DemoApprovalDisposition.SUPERSEDED
        payload["disposition_reason"] = DemoApprovalDispositionReason.OPERATOR_ABANDONED

    with pytest.raises(ValidationError):
        DemoApprovalSessionRecord.model_validate(payload)


@pytest.mark.parametrize("operation", ["retain", "refresh", "abandon", "supersede"])
@pytest.mark.parametrize(
    "now_monotonic",
    [
        pytest.param(True, id="bool"),
        pytest.param("100", id="string"),
        pytest.param(Decimal("100"), id="decimal"),
        pytest.param(None, id="none"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**1000, id="oversized-integer"),
        pytest.param(-0.001, id="negative"),
    ],
)
def test_public_operations_reject_malformed_monotonic_clocks(
    operation: str,
    now_monotonic: object,
) -> None:
    receipt = _receipt()
    record = _retained_record(receipt=receipt)

    with pytest.raises(ValueError, match="now_monotonic"):
        if operation == "retain":
            retain_demo_approval_receipt(
                receipt,
                now_monotonic=now_monotonic,  # type: ignore[arg-type]
            )
        elif operation == "refresh":
            refresh_demo_approval_record(
                record,
                now_monotonic=now_monotonic,  # type: ignore[arg-type]
            )
        elif operation == "abandon":
            abandon_demo_approval_record(
                record,
                now_monotonic=now_monotonic,  # type: ignore[arg-type]
            )
        else:
            supersede_demo_approval_record(
                record,
                now_monotonic=now_monotonic,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(
    "case",
    ["raw-what-if-reference", "unsafe-flags", "unverified-deliverable"],
)
def test_retain_round_trip_revalidates_forged_exact_receipts(case: str) -> None:
    receipt = _receipt()
    if case == "raw-what-if-reference":
        forged = receipt.model_copy(update={"what_if_reference": "DU1234567 raw broker text"})
    elif case == "unsafe-flags":
        forged = receipt.model_copy(
            update={"approval_authority_created": True, "submission_locked": False}
        )
    else:
        forged_contract = receipt.selected_contract.model_copy(
            update={"deliverable_verified": False}
        )
        forged = receipt.model_copy(update={"selected_contract": forged_contract})

    with pytest.raises(ValueError, match="valid scalar DEMO approval receipt"):
        retain_demo_approval_receipt(forged, now_monotonic=RETAINED_AT)


def test_retain_rejects_receipt_subclasses_before_serialization() -> None:
    class ReceiptSubclass(ShortPutDemoApprovalReceipt):
        pass

    receipt = ReceiptSubclass.model_validate(_receipt().model_dump(mode="python"))

    with pytest.raises(ValueError, match="exact scalar DEMO approval receipt"):
        retain_demo_approval_receipt(receipt, now_monotonic=RETAINED_AT)


@pytest.mark.parametrize("case", ["forged-base", "subclass"])
def test_session_record_direct_construction_revalidates_nested_receipts(case: str) -> None:
    receipt = _receipt()
    if case == "forged-base":
        nested_receipt = receipt.model_copy(
            update={"what_if_reference": "DU1234567 raw broker text"}
        )
    else:

        class ReceiptSubclass(ShortPutDemoApprovalReceipt):
            pass

        nested_receipt = ReceiptSubclass.model_validate(receipt.model_dump(mode="python"))

    with pytest.raises(ValidationError):
        DemoApprovalSessionRecord(
            approval_reference=APPROVAL_REFERENCE,
            symbol="SPY",
            receipt=nested_receipt,
            disposition=DemoApprovalDisposition.RETAINED,
            retained_at_monotonic=RETAINED_AT,
            last_observed_at_monotonic=RETAINED_AT,
            display_expires_at_monotonic=RETAINED_AT + RETENTION_SECONDS,
        )


@pytest.mark.parametrize("operation", ["retain", "refresh", "abandon", "supersede"])
def test_public_operations_round_trip_revalidate_forged_session_records(
    operation: str,
) -> None:
    record = _retained_record()
    forged = record.model_copy(update={"authority_created": True})

    with pytest.raises(ValueError, match="valid immutable DEMO approval session record"):
        if operation == "retain":
            retain_demo_approval_receipt(
                _receipt(
                    approval_reference=SECOND_APPROVAL_REFERENCE,
                    what_if_reference=SECOND_WHAT_IF_REFERENCE,
                ),
                now_monotonic=RETAINED_AT + 1.0,
                previous=forged,
            )
        elif operation == "refresh":
            refresh_demo_approval_record(forged, now_monotonic=RETAINED_AT + 1.0)
        elif operation == "abandon":
            abandon_demo_approval_record(forged, now_monotonic=RETAINED_AT + 1.0)
        else:
            supersede_demo_approval_record(forged, now_monotonic=RETAINED_AT + 1.0)


def test_public_operations_reject_session_record_subclasses() -> None:
    class SessionRecordSubclass(DemoApprovalSessionRecord):
        pass

    record = SessionRecordSubclass.model_validate(_retained_record().model_dump(mode="python"))

    with pytest.raises(ValueError, match="exact immutable DEMO approval session record"):
        refresh_demo_approval_record(record, now_monotonic=RETAINED_AT + 1.0)


def test_public_boundary_uses_canonical_record_before_disposition_checks() -> None:
    record = _retained_record()
    forged = record.model_copy(update={"disposition": "RETAINED"})

    expired = refresh_demo_approval_record(
        forged,
        now_monotonic=record.display_expires_at_monotonic,
    )

    assert expired.disposition is DemoApprovalDisposition.EXPIRED
    assert expired.receipt is None
    assert expired.disposition_reason is DemoApprovalDispositionReason.DISPLAY_LEASE_EXPIRED

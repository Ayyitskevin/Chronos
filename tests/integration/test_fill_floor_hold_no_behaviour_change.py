"""FLOORHOLD: the typed blocker is observability only. Nothing refused before may be allowed now.

Each fill-floor scenario (plus a clean control and two holds that are not fill-floor holds) is run
through the REAL ``AppRuntime.reconcile_submission_readiness`` over the order-pipeline harness and
then through the real paper submission boundary, trying to transmit a close. ``GOLDEN`` is what the
same driver recorded at the base commit (134e57a, #361's R7 head, before the blocker existed), as
plain literals: status, published reason, ``permits_new_opening_order``, the blockers, the
submit outcome, and (FLOORHOLD-R2 P3-3) what the pass wrote: the intent status, every order
event row, the counts and run-row decision content and the reply fields.
The test compares every one of them, and allows exactly one difference: ``FILL_FLOOR_HOLD`` among
the blockers of the scenarios that are fill-floor holds.

To regenerate (only ever on a commit before the blocker exists), run ``observe_all()`` there.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from tests.integration.test_order_pipeline import _Harness
from tests.integration.test_order_plane_disclosed_holds import _runtime_over, _StockCloseEvidence
from tests.support.fill_floor_scenarios import SCENARIOS, FloorScenario
from tests.support.order_fakes import PAPER_ACCOUNT, paper_settings, stock_contract
from tests.support.settle_rig import NOW, Rig, ScriptedVenue

from chronos.domain.enums import OrderIntent
from chronos.orders.intent import build_stock_intent

CONTROL = "control-no-hold"


def observe(scenario: FloorScenario | None, *, now: datetime = NOW) -> dict[str, Any]:
    venue = ScriptedVenue()
    evidence = _StockCloseEvidence()
    harness = _Harness(venue, paper_settings(), evidence_provider=cast(Any, evidence))
    evidence.readiness = harness.readiness
    try:
        book = Rig(
            harness.db,
            venue,
            harness.intents,
            harness.tracker_repo,
            harness.tracker,
            harness.readiness,
            harness.connection,
            cast(Any, None),
            PAPER_ACCOUNT,
        )
        if scenario is not None:
            scenario.setup(book)
        published = _runtime_over(harness, venue).reconcile_submission_readiness(
            now=now, trigger="operator"
        )
        close = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.CLOSE_LONG_STOCK,
            contract=stock_contract(),
            quantity=100,
            limit_price=Decimal("100"),
            correlation_id="CHR-ORD-" + "C" * 32,
            intent_id="close-1",
        )
        proposal = harness.service.propose(close, now=now)
        harness.service.preview(close, now=now)
        harness.service.confirm(close, risk_decision_id=proposal.risk.decision_id, now=now)
        outcome = harness.service.submit(close, writer_lease_held=True, now=now)
        return {
            # FLOORHOLD-R2 P3-3: what the pass WROTE and what the reply and run row carry
            "intent_status": book.status().value if scenario is not None else None,
            "events": [
                [
                    e.source,
                    e.from_status.value if e.from_status else None,
                    e.to_status.value,
                    str(e.filled_quantity),
                    str(e.remaining_quantity),
                    re.sub(
                        r":test:\d+", ":test", e.event_key
                    ),  # the rig numbers its own test events
                ]
                for e in (book.events() if scenario is not None else ())
            ],
            "counts": [
                len(published.restart.proven),
                len(published.restart.unresolved),
                len(published.restart.remaining_active),
                len(published.restart.applied_updates),
            ],
            "reply_fields": {
                "status": published.readiness.status.value,
                "generation": published.readiness.generation,
                "unresolved_count": len(published.restart.unresolved),
                "remaining_active_count": len(published.restart.remaining_active),
            },
            # the content of every run-row decision (the observations the row is built from)
            "run_row": [
                {
                    "intent_id": item.intent_id,
                    "local_status": item.local_status.value,
                    "broker_status": item.broker_status.value if item.broker_status else None,
                    "transition_applied": item.transition_applied,
                    "reason": item.reason,
                }
                for item in (*published.restart.proven, *published.restart.unresolved)
            ],
            "status": published.readiness.status.value,
            "reason": published.readiness.reason,
            "permits_new_opening_order": published.restart.permits_new_opening_order,
            "blockers": sorted(str(b) for b in published.readiness.blockers),
            "unresolved_reasons": sorted(item.reason[:60] for item in published.restart.unresolved),
            "submitted": outcome.submitted,
            "refusal": outcome.refusal.value,
            "broker_submit_calls": len(venue.submit_calls),
        }
    finally:
        harness.close()


def observe_all() -> dict[str, dict[str, Any]]:
    result = {CONTROL: observe(None)}
    result.update({scenario.name: observe(scenario) for scenario in SCENARIOS})
    return result


# Recorded at 134e57a (#361 R7 head, before the blocker existed) by observe_all() (see the module
# docstring); FLOORHOLD-R2 re-recorded it there after the merge and extended what it observes.
GOLDEN: dict[str, dict[str, Any]] = {
    "atomic-floor-on-legacy-write": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["TEST", "SUBMITTED", "SUBMISSION_UNKNOWN", "None", "None", "intent-1:test"],
            [
                "ORDER_STATUS",
                "SUBMISSION_UNKNOWN",
                "PARTIALLY_FILLED",
                "5.00000000",
                "5.00000000",
                "intent-1:9001:PARTIALLY_FILLED:5",
            ],
        ],
        "intent_status": "PARTIALLY_FILLED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": "CANCELLED",
                "intent_id": "intent-1",
                "local_status": "SUBMISSION_UNKNOWN",
                "reason": "recorded fills advanced: intent "
                "'intent-1' has 5 filled on record, "
                "above the 0 this write carries; "
                "nothing was written and the intent "
                "stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["recorded fills advanced: intent 'intent-1' has 5 filled on r"],
    },
    "atomic-floor-on-planned-write": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
            [
                "FILL_PROGRESS",
                "CANCEL_PENDING",
                "CANCEL_PENDING",
                "1.00000000",
                "9.00000000",
                "intent-1:9001:CANCEL_PENDING:fill_progress:1:109001:17",
            ],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": "CANCELLED",
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "fill_floor_conflict: "
                "RecordedFillsAdvanced: recorded "
                "fills advanced: intent 'intent-1' "
                "has 1 filled on record, above the 0 "
                "this write carries; nothing was "
                "written and the intent stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["fill_floor_conflict: RecordedFillsAdvanced: recorded fills a"],
    },
    "control-no-hold": {
        "blockers": [],
        "broker_submit_calls": 1,
        "counts": [0, 0, 0, 0],
        "events": [],
        "intent_status": None,
        "permits_new_opening_order": True,
        "reason": "full broker/local parity proven with no sent-active order intents",
        "refusal": "NOT_REFUSED",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 0,
            "status": "RECONCILED",
            "unresolved_count": 0,
        },
        "run_row": [],
        "status": "RECONCILED",
        "submitted": True,
        "unresolved_reasons": [],
    },
    "held-but-not-below-a-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "7.00000000",
                "3.00000000",
                "intent-1:test",
            ],
            ["CANCEL", "PARTIALLY_FILLED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "cancel pending; order absent; no terminal record",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["cancel pending; order absent; no terminal record"],
    },
    "held-on-untyped-fills": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"]
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "owned broker order is typed CANCELLED (not "
                "a working lifecycle) but reports fills: "
                "fill quantity cannot settle it, the intent "
                "stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["owned broker order is typed CANCELLED (not a working lifecyc"],
    },
    "legacy-placeholder-execution": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"]
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "snapshot fill conflict: terminal "
                "venue figure is below the witnessed "
                "lower bound "
                "1.701411834604692317316873037E+38 "
                "from row-bound snapshot executions, "
                "the intent stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: terminal venue figure is below the w"],
    },
    "legacy-placeholder-recorded-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "SUBMITTED",
                "100000000000000000000.00000000",
                "0E-8",
                "intent-1:test",
            ],
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after "
        "restart reconciliation; no new order can be "
        "submitted until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "owned broker order is typed "
                "CANCELLED at 0 filled, below "
                "the 100000000000000000000 "
                "filled already recorded: a "
                "terminal write would drop "
                "recorded fills, the intent "
                "stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["owned broker order is typed CANCELLED at 0 filled, below the"],
    },
    "legacy-recorded-fills": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "SUBMITTED",
                "7.00000000",
                "3.00000000",
                "intent-1:test",
            ],
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "owned broker order is typed CANCELLED at 0 "
                "filled, below the 7 filled already "
                "recorded: a terminal write would drop "
                "recorded fills, the intent stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["owned broker order is typed CANCELLED at 0 filled, below the"],
    },
    "legacy-snapshot-executions": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"]
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until "
        "they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "snapshot fill conflict: terminal venue "
                "figure is below the witnessed lower "
                "bound 5 from row-bound snapshot "
                "executions, the intent stays locked",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: terminal venue figure is below the w"],
    },
    "planner-placeholder-execution": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "snapshot fill conflict: row-bound "
                "snapshot executions "
                "100000000000000000000 exceed the "
                "order quantity 10.00000000; held "
                "for review",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: row-bound snapshot executions 100000"],
    },
    "planner-placeholder-recorded-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "100000000000000000000.00000000",
                "0E-8",
                "intent-1:test",
            ],
            ["CANCEL", "PARTIALLY_FILLED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after "
        "restart reconciliation; no new order can "
        "be submitted until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "witnessed lower bound "
                "100000000000000000000.0000000 "
                "exceeds the order quantity "
                "10.00000000; held for review",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["witnessed lower bound 100000000000000000000.0000000 exceeds "],
    },
    "planner-placeholder-terminal-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after "
        "restart reconciliation; no new order can "
        "be submitted until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "venue fill figure "
                "170141183460469231731687303715884105727 "
                "exceeds the order quantity "
                "10.00000000; held for review",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 170141183460469231731687303715884105727 ex"],
    },
    "planner-placeholder-working-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after "
        "restart reconciliation; no new order can be "
        "submitted until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "venue fill figure "
                "170141183460469231731687303715884105727 "
                "exceeds the order quantity "
                "10.00000000; held for review",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 170141183460469231731687303715884105727 ex"],
    },
    "planner-row-bound": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "snapshot fill conflict: venue fill figure 0 is "
                "below the witnessed lower bound 4 from "
                "row-bound snapshot executions",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: venue fill figure 0 is below the wit"],
    },
    "planner-row-bound-at-quantity": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "snapshot fill conflict: venue fill "
                "figure 0 is below the witnessed "
                "lower bound 10 from row-bound "
                "snapshot executions",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: venue fill figure 0 is below the wit"],
    },
    "planner-row-bound-over-quantity": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "snapshot fill conflict: row-bound "
                "snapshot executions 10.00000001 "
                "exceed the order quantity "
                "10.00000000; held for review",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: row-bound snapshot executions 10.000"],
    },
    "planner-witnessed-terminal": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "7.00000000",
                "3.00000000",
                "intent-1:test",
            ],
            ["CANCEL", "PARTIALLY_FILLED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until "
        "they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "venue fill figure 5 is below the witnessed lower bound 7.00000000",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 5 is below the witnessed lower bound 7.000"],
    },
    "planner-working-order": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            [
                "FILL_PROGRESS",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "3.00000000",
                "7.00000000",
                "intent-1:test",
            ],
            ["CANCEL", "PARTIALLY_FILLED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
        ],
        "intent_status": "CANCEL_PENDING",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "CANCEL_PENDING",
                "reason": "venue fill figure 4 is below the witnessed lower bound 5.00000000",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 4 is below the witnessed lower bound 5.000"],
    },
    "recovered-submitted-row-bound": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "counts": [0, 1, 1, 0],
        "events": [
            ["SUBMIT", "USER_CONFIRMED", "SUBMITTED", "0E-8", "10.00000000", "intent-1:submit"],
            ["CANCEL", "SUBMITTED", "CANCEL_PENDING", "None", "None", "intent-1:test"],
            [
                "RECONCILE_RECOVERY",
                "CANCEL_PENDING",
                "SUBMITTED",
                "None",
                "None",
                "intent-1:test",
            ],
        ],
        "intent_status": "SUBMITTED",
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "reply_fields": {
            "generation": 1,
            "remaining_active_count": 1,
            "status": "PENDING",
            "unresolved_count": 1,
        },
        "run_row": [
            {
                "broker_status": None,
                "intent_id": "intent-1",
                "local_status": "SUBMITTED",
                "reason": "snapshot fill conflict: venue fill "
                "figure 0 is below the witnessed "
                "lower bound 5 from row-bound "
                "snapshot executions",
                "transition_applied": False,
            }
        ],
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: venue fill figure 0 is below the wit"],
    },
}


@pytest.mark.parametrize("name", [CONTROL, *[s.name for s in SCENARIOS]])
def test_status_permit_and_submit_outcome_are_what_they_were_before_the_blocker(
    name: str,
) -> None:
    scenario = next((s for s in SCENARIOS if s.name == name), None)
    before = dict(GOLDEN[name])
    after = observe(scenario)
    typed = scenario is not None and scenario.basis is not None

    blockers = after.pop("blockers")
    before_blockers = before.pop("blockers")
    assert after == before
    assert blockers == sorted([*before_blockers, "FILL_FLOOR_HOLD"] if typed else before_blockers)


def test_every_held_scenario_stays_refused_and_nothing_transmits() -> None:
    held = [s for s in SCENARIOS]
    assert held
    for scenario in held:
        after = observe(scenario)
        assert after["status"] == "PENDING"
        assert after["permits_new_opening_order"] is False
        assert after["submitted"] is False and after["broker_submit_calls"] == 0
        assert after["refusal"] == "RECONCILIATION_NOT_READY"
    control = observe(None)
    assert control["status"] == "RECONCILED" and control["submitted"] is True

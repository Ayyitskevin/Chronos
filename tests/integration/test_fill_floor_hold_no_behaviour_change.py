"""FLOORHOLD: the typed blocker is observability only. Nothing refused before may be allowed now.

Each fill-floor scenario (plus a clean control and two holds that are not fill-floor holds) is run
through the REAL ``AppRuntime.reconcile_submission_readiness`` over the order-pipeline harness and
then through the real paper submission boundary, trying to transmit a close. ``GOLDEN`` is what the
same driver recorded at the base commit (b8e1041, before the blocker existed), as plain literals:
status, published reason, ``permits_new_opening_order``, the blockers, and the submit outcome.
The test compares every one of them, and allows exactly one difference: ``FILL_FLOOR_HOLD`` among
the blockers of the scenarios that are fill-floor holds.

To regenerate (only ever on a commit before the blocker exists), run ``observe_all()`` there.
"""

from __future__ import annotations

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
        if scenario is not None:
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


# Recorded at b8e1041 by observe_all() (see the module docstring).
GOLDEN: dict[str, dict[str, Any]] = {
    "atomic-floor-on-legacy-write": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["recorded fills advanced: intent 'intent-1' has 5 filled on r"],
    },
    "atomic-floor-on-planned-write": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["fill_floor_conflict: RecordedFillsAdvanced: recorded fills a"],
    },
    "control-no-hold": {
        "blockers": [],
        "broker_submit_calls": 1,
        "permits_new_opening_order": True,
        "reason": "full broker/local parity proven with no sent-active order intents",
        "refusal": "NOT_REFUSED",
        "status": "RECONCILED",
        "submitted": True,
        "unresolved_reasons": [],
    },
    "held-but-not-below-a-figure": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["cancel pending; order absent; no terminal record"],
    },
    "held-on-untyped-fills": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["owned broker order is typed CANCELLED (not a working lifecyc"],
    },
    "legacy-recorded-fills": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["owned broker order is typed CANCELLED at 0 filled, below the"],
    },
    "legacy-snapshot-executions": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until "
        "they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: terminal venue figure is below the w"],
    },
    "planner-row-bound": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: venue fill figure 0 is below the wit"],
    },
    "planner-row-bound-over-quantity": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["snapshot fill conflict: row-bound snapshot executions 10.000"],
    },
    "planner-witnessed-terminal": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until "
        "they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 5 is below the witnessed lower bound 7.000"],
    },
    "planner-working-order": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted until they "
        "resolve",
        "refusal": "RECONCILIATION_NOT_READY",
        "status": "PENDING",
        "submitted": False,
        "unresolved_reasons": ["venue fill figure 4 is below the witnessed lower bound 5.000"],
    },
    "recovered-submitted-row-bound": {
        "blockers": ["RESTART_BROKER_EVIDENCE_INCOMPLETE", "RESTART_SENT_ACTIVE"],
        "broker_submit_calls": 0,
        "permits_new_opening_order": False,
        "reason": "sent-active order intents remain after restart "
        "reconciliation; no new order can be submitted "
        "until they resolve",
        "refusal": "RECONCILIATION_NOT_READY",
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

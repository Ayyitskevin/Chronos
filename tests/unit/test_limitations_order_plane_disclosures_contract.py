"""Drift tripwires: the two INTEGRATION-READ findings (2026-11-06) stay disclosed while the code
keeps the shape they describe. The behaviour is pinned by
``tests/integration/test_order_plane_disclosed_holds.py``; these read text and source together.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
RUNBOOK = ROOT / "docs" / "IBKR_RUNBOOK.md"
RECOVERY = ROOT / "src" / "chronos" / "orders" / "reconciliation_recovery.py"
SERVICE = ROOT / "src" / "chronos" / "orders" / "service.py"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_recorded_floor_hold_is_disclosed_with_no_operator_route_and_an_escalation() -> None:
    recovery = RECOVERY.read_text(encoding="utf-8")
    assert "is below the witnessed lower bound" in recovery
    assert "operator resolution applies only to SUBMISSION_UNKNOWN intents" in recovery

    limitations, runbook = _flat(LIMITATIONS), _flat(RUNBOOK)
    assert (
        "holds the intent in `CANCEL_PENDING` and blocks every new order submission through "
        "Chronos, closing orders included"
    ) in limitations
    assert (
        "clears only if the venue later reports a figure at or above the recorded one"
        in limitations
    )
    assert "no operator route" in limitations
    assert "stops and escalates" in limitations
    assert "`below the witnessed lower bound` hold" in runbook
    assert "do not edit the database" in runbook
    assert "closing orders included" in runbook
    assert "close the position in TWS" in runbook
    assert "at or above the recorded one" in runbook
    assert "manual trade" in runbook and "does not clear this hold" in runbook
    assert "UNEXPLAINED_POSITION" in runbook and "rearm with a note" in runbook
    assert "escalate to the owner" in runbook


def test_the_abandoned_draft_cap_count_is_disclosed_where_an_operator_looks() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    assert service.index("self._intents.create(record") < service.index("self._evidence.gather(")

    limitations, runbook = _flat(LIMITATIONS), _flat(RUNBOOK)
    assert "leaves a DRAFT with no risk decision that still counts" in limitations
    assert "Daily opening cap refuses after broker errors at propose time" in runbook

"""docs/limitations.md discloses two recovery behaviours settled under #356 (DEFER356 P3-2, P3-3).

P3-2: the snapshot witness rule matches a contract by ``con_id`` AND symbol, as ruled in
RECONFLOOR r2, so an execution with the same ``con_id`` but a different symbol is treated as a
different contract and a terminal row settles rather than holds. Whether IBKR's ``con_id`` alone is
the contract identity is unverified; the line says it needs checking at the first paper session.
P3-3: a blank-reference execution on a reused broker id can hold another order's intent; the hold
fails closed, blocks new opening orders while it lasts, and has no operator exit. Each pin reads the
source and the document together, so the disclosure cannot outlive the rule it describes.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
RECOVERY = ROOT / "src" / "chronos" / "orders" / "reconciliation_recovery.py"


def _flat() -> str:
    return " ".join(LIMITATIONS.read_text(encoding="utf-8").split())


def test_the_con_id_and_symbol_witness_rule_is_disclosed_for_the_first_paper_session() -> None:
    source = RECOVERY.read_text(encoding="utf-8")
    assert "execution.contract.con_id == order.contract.con_id" in source
    assert "execution.contract.symbol == order.contract.symbol" in source

    flat = _flat()
    assert "the same `con_id` but a different symbol" in flat
    assert "needs checking at the first paper session" in flat


def test_reused_broker_id_holds_are_disclosed_as_fail_closed_with_no_operator_exit() -> None:
    source = RECOVERY.read_text(encoding="utf-8")
    assert "execution.broker_order_id == order.broker_order_id" in source

    flat = _flat()
    assert "another order that reused the broker id" in flat
    assert "blocks new opening orders while the execution stays in the snapshot" in flat
    assert "has no operator exit" in flat

"""Two documents said the inverse of the source; these pins hold them to it (D-1 audit).

At 3ab88ec `docs/limitations.md` said periodic reconciliation on a timer was not implemented
while reconnect and order/fill-event reconciliation were. The source had it the other way
round: a writer-only periodic task IS wired from the lifespan
(`src/chronos/api/reconciliation_loop.py`), a reconnect only INVALIDATES readiness
(`src/chronos/broker/callbacks.py`), and no broker callback calls
`reconcile_submission_readiness` at all. In the same audit `AccountView`'s docstring called
every view "Broker-derived" when all three constructors supply configured or simulated cash.

A documentation claim about what the code does is a test that has not been run yet. Each
pin below reads the source and the document and asserts they agree, so the next time one of
them moves without the other this file fails rather than an operator's expectation:

- the lifespan source references the periodic task (source read, not import);
- no module under `src/chronos/broker/` contains the reconciliation token — the day
  someone wires callback-driven reconciliation, the limitation text must move with it;
- the limitations bullet states the callers the source has, and no longer says the timer
  is unimplemented; the operator route it names is registered;
- `AccountView.__doc__` names the three sources and does not open with "Broker-derived",
  and the constructor set it describes is the one the source has.
"""

from __future__ import annotations

import re
from pathlib import Path

from chronos.risk.engine import AccountView

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
LIFESPAN = ROOT / "src" / "chronos" / "api" / "main.py"
ORDER_ROUTES = ROOT / "src" / "chronos" / "api" / "routes" / "orders.py"
BROKER_PACKAGE = ROOT / "src" / "chronos" / "broker"
CHRONOS_PACKAGE = ROOT / "src" / "chronos"

RECONCILE_TOKEN = "reconcile_submission_readiness"
_ACCOUNT_VIEW_CALL = re.compile(r"(?<![A-Za-z_])AccountView\(")


def _limitations_section() -> str:
    """The reconciliation section, whitespace-collapsed so a pin survives re-wrapping."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    heading = "## Order pipeline and reconciliation\n"
    start = text.index(heading) + len(heading)
    rest = text[start:]
    stop = re.search(r"^## ", rest, re.M)
    section = rest[: stop.start()] if stop else rest
    return " ".join(section.split())


# ------------------------------------------------------------------ (a) periodic task wired


def test_the_lifespan_source_wires_the_periodic_reconciliation_task() -> None:
    source = LIFESPAN.read_text(encoding="utf-8")
    assert "from chronos.api.reconciliation_loop import reconciliation_task" in source
    assert "reconciliation_task(" in source, "the lifespan no longer constructs the task"


# ------------------------------------------------------- (b) no callback-driven reconciliation


def test_no_broker_module_calls_reconciliation() -> None:
    modules = sorted(BROKER_PACKAGE.rglob("*.py"))
    assert modules, BROKER_PACKAGE
    offenders = [
        module.relative_to(ROOT).as_posix()
        for module in modules
        if RECONCILE_TOKEN in module.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} reference {RECONCILE_TOKEN}: callback-driven reconciliation is now "
        "wired, and the docs/limitations.md reconciliation bullet must say so"
    )


# -------------------------------------------------------- (c) the limitation says what is


def test_limitations_states_the_reconciliation_callers_the_source_has() -> None:
    text = LIMITATIONS.read_text(encoding="utf-8")
    assert "on a timer is not implemented" not in text
    section = _limitations_section()
    for phrase in (
        "writer-only periodic task",
        "`POST /orders/reconcile`",
        "only invalidate readiness",
        "a reconnect does not reconcile",
        "do not trigger reconciliation",
        "order/fill-event-driven reconciliation is absent",
    ):
        assert phrase in section, phrase


def test_the_operator_route_the_limitation_names_is_registered() -> None:
    assert '@router.post("/orders/reconcile"' in ORDER_ROUTES.read_text(encoding="utf-8")


# ------------------------------------------------- (d) AccountView says who supplies it


def test_account_view_docstring_names_its_three_sources() -> None:
    doc = AccountView.__doc__ or ""
    assert not doc.startswith("Broker-derived"), doc
    for source in ("broker-observed", "configured", "simulated"):
        assert source in doc, source
    assert "docs/RISK_POLICY.md" in doc


def test_the_constructor_set_the_docstring_describes_is_the_source_s() -> None:
    constructors = {
        module.relative_to(ROOT).as_posix()
        for module in CHRONOS_PACKAGE.rglob("*.py")
        if _ACCOUNT_VIEW_CALL.search(module.read_text(encoding="utf-8"))
    }
    assert constructors == {
        "src/chronos/backtest/engine.py",
        "src/chronos/research/shadow.py",
        "src/chronos/service/cycle.py",
    }, "a constructor appeared or vanished; AccountView's docstring names each source"

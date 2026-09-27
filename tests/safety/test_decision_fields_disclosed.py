"""VCP section 6 finding 7: every economic-looking decision field is ENFORCED or disclosed INERT.

``chronos.autonomy.decision_enforcement`` is the map; these are its pins. The
shape follows ``test_supervisor_gateway.py``'s mandate pins, with one change:
readers are found by *attribute access* in the module's AST, not by substring.
A substring scan would count ``orders/risk.py``'s
``signal_time_initial_stop_price_usd`` as a read of ``stop_price``, and would
count a docstring that mentions a field as code that acts on it.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from types import ModuleType

from chronos.autonomy.decision import EntryPlan, ExitPlan, ProposedDecision
from chronos.autonomy.decision_enforcement import (
    DECISION_FIELD_ENFORCEMENT,
    ENFORCED,
    INERT,
    NOT_A_LIMIT,
    NOT_TRANSMITTED,
)
from chronos.execution.intents import OrderIntent

_MODELS: dict[str, type] = {
    "ProposedDecision": ProposedDecision,
    "EntryPlan": EntryPlan,
    "ExitPlan": ExitPlan,
    "OrderIntent": OrderIntent,
}


def _fields(model: type) -> set[str]:
    if dataclasses.is_dataclass(model):
        return {f.name for f in dataclasses.fields(model)}
    return set(model.model_fields)  # type: ignore[attr-defined]


def _attributes_read(modules: tuple[ModuleType, ...]) -> set[str]:
    names: set[str] = set()
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    return names


def _kernel_modules() -> tuple[ModuleType, ...]:
    """The deterministic path that admits, sizes and compiles a decision."""

    import chronos.supervisor.admission as admission
    import chronos.supervisor.compiler as compiler
    import chronos.supervisor.durable as durable
    import chronos.supervisor.loop as loop
    import chronos.supervisor.sizing as sizing

    return (admission, sizing, durable, compiler, loop)


def _transmit_modules() -> tuple[ModuleType, ...]:
    """The live execution path an OrderIntent takes to a broker.

    Deliberately absent: ``execution/intents.py`` (defines, validates and
    fingerprints it), the ledgers (store it), ``portfolio/sizer.py`` (produces
    it), ``monitoring`` (displays it) and ``backtest/engine.py`` (simulates it).
    None of those sends an order.
    """

    import chronos.execution.brokers.ibkr_paper as ibkr_paper
    import chronos.execution.brokers.port as port
    import chronos.execution.brokers.simulated as simulated
    import chronos.execution.engine as engine
    import chronos.execution.reconciliation as reconciliation
    import chronos.execution.state_machine as state_machine
    import chronos.risk.engine as risk_engine

    return (engine, ibkr_paper, port, simulated, state_machine, reconciliation, risk_engine)


def _readers_for(model_name: str) -> set[str]:
    if model_name == "OrderIntent":
        return _attributes_read(_transmit_modules())
    return _attributes_read(_kernel_modules())


def test_t3_every_field_is_classified_exactly_once() -> None:
    """A field cannot just appear: it is a limit (ENFORCED or INERT) or it is not."""

    assert set(DECISION_FIELD_ENFORCEMENT) == set(_MODELS) == set(NOT_A_LIMIT)
    for name, model in _MODELS.items():
        classified = set(DECISION_FIELD_ENFORCEMENT[name])
        exempt = set(NOT_A_LIMIT[name])
        assert not classified & exempt, f"{name}: in both maps: {sorted(classified & exempt)}"
        actual = _fields(model)
        assert classified | exempt == actual, (
            f"{name} fields changed: {sorted(actual ^ (classified | exempt))}. Classify "
            "each in chronos.autonomy.decision_enforcement: ENFORCED, INERT, or NOT_A_LIMIT."
        )
        assert set(DECISION_FIELD_ENFORCEMENT[name].values()) <= {ENFORCED, INERT}


def test_t3_the_finding_7_fields_are_classified_as_the_record_says() -> None:
    """The specific claims, so a relabel cannot pass by keeping the key set intact."""

    decision = DECISION_FIELD_ENFORCEMENT["ProposedDecision"]
    assert decision["requested_quantity"] == ENFORCED
    assert decision["entry_plan"] == ENFORCED
    for field in (
        "requested_risk_budget_usd",
        "max_acceptable_loss_usd",
        "protective_order_required",
        "exit_plan",
    ):
        assert decision[field] == INERT, field
    assert dict(DECISION_FIELD_ENFORCEMENT["ExitPlan"]) == {
        "profit_target": INERT,
        "protective_stop": INERT,
        "time_exit": INERT,
    }
    assert DECISION_FIELD_ENFORCEMENT["EntryPlan"]["trigger"] == ENFORCED
    assert DECISION_FIELD_ENFORCEMENT["EntryPlan"]["valid_until"] == INERT
    # A risk input on the live path (risk/engine.py STOP_REQUIRED + per-trade
    # risk), and not transmitted: see the broker-adapter pin below.
    assert DECISION_FIELD_ENFORCEMENT["OrderIntent"]["stop_price"] == ENFORCED
    assert "stop_price" in NOT_TRANSMITTED


def test_t4_the_classification_matches_what_the_acting_path_reads() -> None:
    """Guard the guard: INERT means no admission, sizing or transmit module reads it."""

    for name, classified in DECISION_FIELD_ENFORCEMENT.items():
        read = _readers_for(name)
        for field, status in classified.items():
            if status == INERT:
                assert field not in read, (
                    f"{name}.{field} is classified INERT but the acting path now reads it "
                    "— reclassify it in chronos.autonomy.decision_enforcement (and if it "
                    "now changes an admission, a size or an order, that is an owner gate)"
                )
            else:
                assert field in read, f"{name}.{field} is classified ENFORCED but nothing reads it"


def test_t4_the_scan_sees_a_real_read() -> None:
    """Positive control: the AST scan finds reads it must find, so an empty scan cannot pass."""

    assert "requested_quantity" in _readers_for("ProposedDecision")
    assert "limit_price" in _readers_for("OrderIntent")
    # The substring trap the AST scan exists to avoid: this is not a stop_price read.
    import chronos.orders.risk as orders_risk

    assert "stop_price" in inspect.getsource(orders_risk)
    assert "stop_price" not in _attributes_read((orders_risk,))


def test_t4_a_stop_price_is_not_sent_to_any_broker() -> None:
    """Finding 7's half that ENFORCED hides: no broker adapter places a stop.

    If one starts to, this fails, and the protection lifecycle (F7-C, owner
    gated) has begun: reclassify deliberately, never by accident.
    """

    import chronos.execution.brokers.ibkr_paper as ibkr_paper
    import chronos.execution.brokers.port as port
    import chronos.execution.brokers.simulated as simulated

    sent = _attributes_read((ibkr_paper, port, simulated))
    assert "limit_price" in sent  # positive control: the adapters are scanned
    for field in NOT_TRANSMITTED:
        assert field not in sent, f"a broker adapter now reads OrderIntent.{field}"

"""VCP section 6 finding 7, strategy half: every economic-looking strategy param classified.

``chronos.strategies.param_enforcement`` is the map; these are its pins. The
shape follows ``tests/safety/test_decision_fields_disclosed.py``: readers are
found by *attribute access* in each strategies module's AST, not by substring,
so a comment or a same-named string key cannot count as a read.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from chronos.strategies.base import ProposalDirection, StrategyProposal
from chronos.strategies.baselines import (
    BuyAndHoldStrategy,
    DeterministicRandomEntries,
    SmaTrendBaseline,
)
from chronos.strategies.mean_reversion import MeanReversionParams
from chronos.strategies.param_enforcement import (
    ADVISORY_DECLARATION,
    ENFORCED,
    INERT,
    PARAM_ENFORCEMENT,
    POLICY_CAP,
    PROPOSAL_FLOOR,
)
from chronos.strategies.regime_trend import RegimeTrendParams

_CLASSES: dict[str, type] = {
    "MeanReversionParams": MeanReversionParams,
    "RegimeTrendParams": RegimeTrendParams,
    "BuyAndHoldStrategy": BuyAndHoldStrategy,
    "SmaTrendBaseline": SmaTrendBaseline,
    "DeterministicRandomEntries": DeterministicRandomEntries,
}


def _fields(cls: type) -> set[str]:
    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)}
    return set()


def _strategy_reads() -> set[str]:
    """Attribute names read anywhere in src/chronos/strategies/*.py (the map module excluded)."""

    modules: tuple[ModuleType, ...] = _strategy_modules()
    names: set[str] = set()
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    return names


#: The package's vocabulary types: not parameter blocks.
_NOT_PARAMS = frozenset({"PositionState", "StrategyContext", "StrategyProposal"})


def _strategy_modules() -> tuple[ModuleType, ...]:
    import chronos.strategies as package

    return tuple(
        importlib.import_module(f"chronos.strategies.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
        if info.name != "param_enforcement"
    )


def test_t1_every_parameter_block_in_the_package_is_in_the_table() -> None:
    """A new params dataclass (or a new strategies module) cannot arrive unclassified."""

    found = {
        name
        for module in _strategy_modules()
        for name, obj in vars(module).items()
        if dataclasses.is_dataclass(obj)
        and isinstance(obj, type)
        and obj.__module__ == module.__name__
        and name not in _NOT_PARAMS
    }
    assert found <= set(PARAM_ENFORCEMENT), (
        f"unclassified parameter blocks: {sorted(found - set(PARAM_ENFORCEMENT))}"
    )


def test_t1_every_param_field_is_classified_exactly_once() -> None:
    """A parameter cannot just appear: every field is in the table, with a status and a reader."""

    assert set(PARAM_ENFORCEMENT) == set(_CLASSES)
    for name, cls in _CLASSES.items():
        classified = set(PARAM_ENFORCEMENT[name])
        actual = _fields(cls)
        assert classified == actual, (
            f"{name} fields changed: {sorted(actual ^ classified)}. Classify each in "
            "chronos.strategies.param_enforcement: (ENFORCED, reader) or (INERT, '')."
        )
        for field, (status, reader) in PARAM_ENFORCEMENT[name].items():
            assert status in (ENFORCED, INERT), f"{name}.{field}: bad status {status!r}"
            if status == ENFORCED:
                assert reader, f"{name}.{field}: ENFORCED without a file:line reader"
            else:
                assert not reader, f"{name}.{field}: INERT must not carry a reader"


_OWNER: dict[str, str] = {
    "MeanReversionParams": "chronos.strategies.mean_reversion",
    "RegimeTrendParams": "chronos.strategies.regime_trend",
    "BuyAndHoldStrategy": "chronos.strategies.baselines",
    "SmaTrendBaseline": "chronos.strategies.baselines",
    "DeterministicRandomEntries": "chronos.strategies.baselines",
}


def _module_reads(module_name: str) -> set[str]:
    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_t2_the_classification_matches_what_the_strategies_read() -> None:
    """Guard the guard: ENFORCED means the OWNING module reads it; INERT means no module does.

    Per owner, not a union: both params classes carry atr_len, ema_filter_len and
    exposure_fraction, so a union scan lets one strategy's read certify the other's.
    """

    read = _strategy_reads()
    for name, classified in PARAM_ENFORCEMENT.items():
        owner_reads = _module_reads(_OWNER[name])
        for field, (status, _reader) in classified.items():
            if status == ENFORCED:
                assert field in owner_reads, (
                    f"{name}.{field} is classified ENFORCED but no strategies/*.py module reads "
                    "it — reclassify it in chronos.strategies.param_enforcement"
                )
            else:
                assert field not in read, (
                    f"{name}.{field} is classified INERT but a strategies/*.py module now reads "
                    "it — reclassify it (and if it now shapes a proposal, that is an owner gate)"
                )


def test_t2_the_scan_sees_a_real_read() -> None:
    """Positive control: the AST scan finds reads it must find, so an empty scan cannot pass."""

    read = _strategy_reads()
    assert "time_stop_bars" in read  # mean_reversion.py:134
    assert "markov_min_stay" in read  # regime_trend.py:138
    assert "holding_bars" in read  # baselines.py:140


def _proposal(**overrides: object) -> StrategyProposal:
    kwargs: dict[str, object] = {
        "strategy_id": "test",
        "strategy_version": "0",
        "timestamp_utc": datetime(2026, 9, 27, tzinfo=UTC),
        "symbol": "TEST",
        "direction": ProposalDirection.HOLD,
        "desired_exposure_fraction": 0.5,
        "stop_fraction": 0.1,
    }
    kwargs.update(overrides)
    return StrategyProposal(**kwargs)  # type: ignore[arg-type]


def test_t3_the_proposal_floor_binds_wherever_params_set_the_values() -> None:
    """The base.py __post_init__ bounds are the enforcement floor under every param-set value."""

    _proposal(desired_exposure_fraction=0.0)
    _proposal(desired_exposure_fraction=1.0)
    _proposal(stop_fraction=None)
    with pytest.raises(ValueError, match="desired_exposure_fraction"):
        _proposal(desired_exposure_fraction=1.5)
    with pytest.raises(ValueError, match="desired_exposure_fraction"):
        _proposal(desired_exposure_fraction=-0.1)
    with pytest.raises(ValueError, match="stop_fraction"):
        _proposal(stop_fraction=0.0)
    with pytest.raises(ValueError, match="stop_fraction"):
        _proposal(stop_fraction=1.0)
    with pytest.raises(ValueError, match="ENTER_LONG"):
        _proposal(direction=ProposalDirection.ENTER_LONG, desired_exposure_fraction=0.0)


def test_t3_the_two_level_reading_is_cited_as_data() -> None:
    """Advisory lane, enforced mechanics: the declaration and the policy cap are on record."""

    assert "base.py" in ADVISORY_DECLARATION
    assert "sizer.py:99" in POLICY_CAP
    assert set(PROPOSAL_FLOOR) == {
        "desired_exposure_fraction",
        "stop_fraction",
        "enter_long_positive",
    }


def test_t4_nothing_under_src_imports_the_table() -> None:
    """The table is a record, not a mechanism: no src/ module imports it."""

    import chronos

    src_root = Path(inspect.getfile(chronos)).parent
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if path.name == "param_enforcement.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "chronos.strategies.param_enforcement"
            ):
                offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path}:{node.lineno}"
                    for alias in node.names
                    if alias.name == "chronos.strategies.param_enforcement"
                )
    assert not offenders, f"src/ imports the classification table: {offenders}"

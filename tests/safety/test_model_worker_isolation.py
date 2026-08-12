"""The model worker holds nothing, and the repo holds no model (ADR-0027).

Four guarantees, each structural:

1. **Total import isolation.** The worker imports NOTHING from ``chronos`` —
   stronger than the bridge, which borrows ``chronos.utils.time``. The worker
   holds an LLM API key and consumes untrusted model output; it is exactly
   where a prompt injection would land, so nothing that can act may be in its
   address space. An AST walk plus a subprocess probe enforce it.

2. **No LLM SDK anywhere in the dependency tree.** "Chronos ships no model, no
   provider SDK, and no API key in the broker-holding process" was previously
   re-verified by a manual grep of pyproject/requirements; the worker calling
   the Messages API over raw httpx makes it permanent, and this file turns the
   grep into a test.

3. **No drift in the restated vocabulary.** The worker restates the decision
   vocabulary because it cannot import it; every restatement is pinned EQUAL
   to the real enum or frozenset, so a contract change fails here until the
   worker learns it.

4. **The backend does not depend on the worker.** The dependency is one-way,
   and the direction is the safety property: a model worker inside the process
   that holds the broker connection is the design ADR-0016 §3 exists to forbid.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "chronos"
_WORKER = _REPO_ROOT / "worker"

#: Everything the worker may not import. ``chronos`` covers the whole package —
#: including ``chronos.autonomy`` and ``chronos.bridge`` — and the SDK names
#: cover the "no provider SDK" invariant.
_FORBIDDEN = (
    "chronos",
    "anthropic",
    "openai",
    "litellm",
    "langchain",
    "sqlalchemy",
    "sqlite3",
    "ib_async",
    "ibapi",
)

#: Provider SDKs that must never enter the repo's declared dependencies.
_FORBIDDEN_DEPENDENCIES = ("anthropic", "openai", "litellm", "langchain")


def _module_files() -> list[Path]:
    return sorted(_WORKER.glob("*.py"))


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


# --------------------------------------------------------------------------- isolation


def test_the_package_contains_the_modules_this_file_guards() -> None:
    """Guard the guard: an empty glob would make every assertion below vacuous."""

    names = {path.stem for path in _module_files()}
    assert {
        "config",
        "cycle",
        "evidence",
        "model",
        "propose",
        "vocabulary",
        "__main__",
    } <= names


def test_worker_modules_import_nothing_forbidden() -> None:
    for path in _module_files():
        for name in _imported_names(path.read_text(encoding="utf-8")):
            for forbidden in _FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"worker/{path.name} imports forbidden module {name!r}. The worker "
                    "holds an API key and reads model output; nothing that can act — and "
                    "no chronos module at all — may be in its address space."
                )


def test_importing_the_worker_leaks_no_forbidden_module() -> None:
    probe = (
        "import worker, worker.cycle, worker.model, worker.__main__, sys; "
        f"bad=[m for m in sys.modules if m.startswith({_FORBIDDEN!r})]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], f"importing the worker leaked forbidden modules: {leaked}"


def test_nothing_in_chronos_imports_the_worker() -> None:
    """The dependency is one-way, and the direction is the safety property."""

    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for name in _imported_names(path.read_text(encoding="utf-8")):
            if name == "worker" or name.startswith("worker."):
                importers.append(str(path.relative_to(_SRC)))
    assert importers == [], (
        f"the model worker must never enter the broker-holding process, but "
        f"{sorted(set(importers))} import it. ADR-0016 §3 exists to forbid exactly this."
    )


def test_no_provider_sdk_in_the_declared_dependencies() -> None:
    """The manual re-verification grep, made permanent.

    The worker talks to the Messages API over raw httpx precisely so that this
    stays true: no LLM SDK is a dependency of anything in this repository.
    """

    for filename in ("pyproject.toml", "requirements.txt", "requirements-dev.lock"):
        path = _REPO_ROOT / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for sdk in _FORBIDDEN_DEPENDENCIES:
            for line in text.splitlines():
                stripped = line.strip().strip('"').strip("'")
                assert not stripped.startswith(sdk), (
                    f"{filename} declares the provider SDK {sdk!r}. The no-SDK-in-repo "
                    "invariant is load-bearing for ADR-0016's isolation story; the worker "
                    "calls the API over raw httpx on purpose."
                )


# --------------------------------------------------------- the restatements do not drift


def test_restated_decision_vocabulary_matches_the_contract() -> None:
    from worker import vocabulary

    from chronos.autonomy.enums import (
        DecisionDirection,
        DecisionKind,
        StrategyForm,
        TimeHorizon,
    )

    assert {member.value for member in DecisionKind} == vocabulary.DECISION_KINDS
    assert {member.value for member in DecisionDirection} == vocabulary.DIRECTIONS
    assert {member.value for member in StrategyForm} == vocabulary.STRATEGY_FORMS
    assert {member.value for member in TimeHorizon} == vocabulary.TIME_HORIZONS


def test_restated_kind_classifications_match_the_contract() -> None:
    from worker import vocabulary

    from chronos.autonomy.enums import (
        EXPOSURE_CREATING_DECISION_KINDS,
        TARGETED_DECISION_KINDS,
    )

    assert {
        member.value for member in EXPOSURE_CREATING_DECISION_KINDS
    } == vocabulary.EXPOSURE_CREATING_KINDS
    assert {member.value for member in TARGETED_DECISION_KINDS} == vocabulary.TARGETED_KINDS


def test_restated_payload_rules_match_the_contract() -> None:
    from worker import vocabulary

    from chronos.autonomy import decision as decision_module

    assert {member.value for member in decision_module._SIZELESS_KINDS} == vocabulary.SIZELESS_KINDS
    assert {member.value for member in decision_module._NO_ENTRY_KINDS} == vocabulary.NO_ENTRY_KINDS


def test_restated_symbol_alphabet_and_reference_pattern_match_the_contract() -> None:
    from worker import vocabulary

    from chronos.autonomy import decision as decision_module

    assert vocabulary.SYMBOL_ALPHABET == decision_module._SYMBOL_ALPHABET
    assert vocabulary.CHRONOS_REFERENCE_PATTERN == decision_module._CHRONOS_REFERENCE_PATTERN


def test_the_restated_token_header_matches_the_backend() -> None:
    """A silently renamed header would make every read and every POST 401."""

    from worker.evidence import TOKEN_HEADER

    from chronos.api import auth

    assert TOKEN_HEADER == auth._TOKEN_HEADER


def test_the_tool_schema_cannot_express_a_naked_short_option() -> None:
    """ADR-0016 §6, enforced at the schema the model is handed."""

    from worker.model import PROPOSE_DECISION_TOOL

    strategy = PROPOSE_DECISION_TOOL["input_schema"]["properties"]["strategy"]
    enum_values = strategy["anyOf"][0]["enum"]
    assert not [name for name in enum_values if "NAKED" in name]
    assert not [name for name in enum_values if name.startswith(("SHORT_CALL", "SHORT_PUT"))]


def test_the_tool_schema_has_no_writer_owned_or_order_capable_field() -> None:
    """The model cannot even be ASKED for what it may never author.

    ``strict: true`` plus ``additionalProperties: false`` means the schema is
    the complete universe of what the model can emit — so the absence of these
    names here is the structural guarantee, not just a convention.
    """

    from worker.model import PROPOSE_DECISION_TOOL

    schema = PROPOSE_DECISION_TOOL["input_schema"]
    assert schema["additionalProperties"] is False
    assert PROPOSE_DECISION_TOOL["strict"] is True
    field_names = set(schema["properties"])
    forbidden = {
        "provenance",
        "decision_id",
        "account",
        "account_id",
        "order_id",
        "broker_order_id",
        "client_id",
        "exchange",
        "routing",
        "transmit",
        "order_type",
        "limit_price",
        "price",
        "mandate",
        "mandate_id",
    }
    overlap = field_names & forbidden
    assert not overlap, (
        f"the propose_decision schema offers the model writer-owned or order-capable "
        f"field(s) {sorted(overlap)}"
    )

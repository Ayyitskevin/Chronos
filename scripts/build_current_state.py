#!/usr/bin/env python
"""Build the repository-scoped capability matrix and current-state page.

The outputs are deliberately a pure function of committed source.  This script
does not read environment variables, a mandate, promotion files, a database, or
a broker.  Consequently it can report code paths and repository defaults, but
it can never report that a deployment is authorized or that operational
evidence exists.

Usage:

    .venv/bin/python scripts/build_current_state.py
    .venv/bin/python scripts/build_current_state.py --check
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import sys
import textwrap
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any

from chronos.api.autonomy_wiring import INGRESS_IDENTITY, BackendGatherers
from chronos.autonomy.enums import (
    MINIMUM_PROMOTION_FOR_MODE,
    SUBMITTING_AUTONOMY_MODES,
    AutonomyMode,
    DecisionKind,
    StrategyForm,
    TradableAssetClass,
)
from chronos.broker.demo import DemoBroker
from chronos.broker.ibkr import IBKRBroker
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings
from chronos.domain.enums import BrokerAdapter, BrokerMode, IBEnvironment
from chronos.runtime import build_runtime
from chronos.supervisor.compiler import _CAPABILITY_MATRIX, _CLOSING_MATRIX
from chronos.supervisor.evidence_kinds import BundleKind, citation_kinds_for

ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = Path("docs/generated/capability-matrix.json")
CURRENT_STATE_PATH = Path("docs/generated/CURRENT_STATE.md")
SCHEMA_VERSION = "chronos-capability-matrix-v1"
MATRIX_COLUMNS = (
    "asset_family",
    "decision_kind",
    "strategy_shape",
    "order_intent",
    "broker_adapter",
    "mode",
    "evidence_source",
    "promotion_status",
    "instrument_facts_status",
    "adapter_mode_status",
    "current_status",
)

SOURCE_PATHS = (
    Path("src/chronos/supervisor/compiler.py"),
    Path("src/chronos/autonomy/enums.py"),
    Path("src/chronos/config/settings.py"),
    Path("src/chronos/domain/enums.py"),
    Path("src/chronos/runtime.py"),
    Path("src/chronos/api/autonomy_wiring.py"),
    Path("src/chronos/supervisor/evidence_kinds.py"),
    Path("src/chronos/broker/demo.py"),
    Path("src/chronos/broker/official_ibkr.py"),
    Path("src/chronos/broker/ibkr.py"),
)

_ADAPTER_IMPLEMENTATIONS: dict[BrokerAdapter, type[Any]] = {
    BrokerAdapter.DEMO: DemoBroker,
    BrokerAdapter.OFFICIAL_IBKR: OfficialIBKRBroker,
    BrokerAdapter.IB_ASYNC: IBKRBroker,
}

_ADAPTER_EVIDENCE_SOURCES = {
    BrokerAdapter.DEMO: "DEMO_BROKER_FIXTURE",
    BrokerAdapter.OFFICIAL_IBKR: "IBKR_GATEWAY_OFFICIAL_API",
    BrokerAdapter.IB_ASYNC: "IBKR_GATEWAY_IB_ASYNC_READ_ONLY",
}


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot render {type(value).__name__} as JSON")


def _setting_default(name: str) -> object:
    return Settings.model_fields[name].default


def _function_ast(function: object) -> ast.FunctionDef | ast.AsyncFunctionDef:
    parsed = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = parsed.body[0]
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        raise RuntimeError(f"{function!r} is not a function")
    return node


def _if_chain(node: ast.If) -> tuple[list[str], list[list[ast.stmt]], list[ast.stmt]]:
    tests: list[str] = []
    bodies: list[list[ast.stmt]] = []
    current = node
    while True:
        tests.append(ast.unparse(current.test))
        bodies.append(current.body)
        if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
            current = current.orelse[0]
            continue
        return tests, bodies, current.orelse


def _assigned_constructor(statements: list[ast.stmt], target: str) -> str:
    for statement in statements:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == target
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
        ):
            return statement.value.func.id
    raise RuntimeError(f"no constructor assignment found for {target}")


def _runtime_selector() -> dict[str, str]:
    runtime_ast = _function_ast(build_runtime)
    selector = next(
        (
            node
            for node in ast.walk(runtime_ast)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "settings.broker_mode is BrokerMode.DEMO"
        ),
        None,
    )
    if selector is None:
        raise RuntimeError("build_runtime no longer has the recognized broker selector")
    tests, bodies, fallback = _if_chain(selector)
    if tests != [
        "settings.broker_mode is BrokerMode.DEMO",
        "settings.broker_adapter is BrokerAdapter.IB_ASYNC",
    ]:
        raise RuntimeError("build_runtime broker selector changed; update reporting derivation")
    return {
        "demo_mode": _assigned_constructor(bodies[0], "broker"),
        "ib_async": _assigned_constructor(bodies[1], "broker"),
        "ibkr_fallback": _assigned_constructor(fallback, "broker"),
    }


def _production_instrument_routes() -> set[tuple[str, str | None]]:
    gatherer_ast = _function_ast(BackendGatherers.instrument_facts)
    try_node = next((node for node in ast.walk(gatherer_ast) if isinstance(node, ast.Try)), None)
    if try_node is None or not try_node.body or not isinstance(try_node.body[0], ast.If):
        raise RuntimeError("production instrument gatherer no longer has the recognized selector")
    tests, _, fallback = _if_chain(try_node.body[0])
    expected = [
        "decision.asset_class is TradableAssetClass.EQUITY",
        "decision.asset_class is TradableAssetClass.CRYPTO",
        (
            "decision.asset_class is TradableAssetClass.EQUITY_OPTION and decision.kind is "
            "DecisionKind.OPEN"
        ),
    ]
    if tests != expected:
        raise RuntimeError("production instrument routes changed; update reporting derivation")
    if (
        len(fallback) != 1
        or not isinstance(fallback[0], ast.Return)
        or not isinstance(fallback[0].value, ast.Constant)
        or fallback[0].value.value is not None
    ):
        raise RuntimeError("production instrument gatherer fallback no longer refuses")
    return {
        (TradableAssetClass.EQUITY.value, None),
        (TradableAssetClass.CRYPTO.value, None),
        (TradableAssetClass.EQUITY_OPTION.value, DecisionKind.OPEN.value),
    }


def _method_refuses_unconditionally(method: object) -> bool:
    node = _function_ast(method)
    return bool(node.body) and isinstance(node.body[-1], ast.Raise)


def _settings_path_configurable(adapter: BrokerAdapter, *, live: bool) -> bool:
    broker_mode = BrokerMode.DEMO if adapter is BrokerAdapter.DEMO else BrokerMode.IBKR
    account_id = "U12345" if live else "DU12345"
    values = {
        "broker_mode": broker_mode,
        "broker_adapter": adapter,
        "ib_environment": IBEnvironment.LIVE if live else IBEnvironment.PAPER,
        "allow_order_transmit": True,
        "allow_live_trading": live,
        "ib_account_id": account_id,
        "ib_account_allowlist": (account_id,),
    }
    try:
        settings = Settings.model_validate(values)
    except ValueError:
        return False
    return settings.live_transmission_possible if live else settings.transmission_possible


def _source_fingerprints() -> list[dict[str, str]]:
    return [
        {
            "path": str(path),
            "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
        }
        for path in SOURCE_PATHS
    ]


def _compiler_capabilities() -> list[dict[str, str | None]]:
    capabilities: list[dict[str, str | None]] = []
    for (asset, decision, strategy), intent in _CAPABILITY_MATRIX.items():
        capabilities.append(
            {
                "asset_family": asset.value,
                "decision_kind": decision.value,
                "strategy_shape": strategy.value,
                "order_intent": intent.value,
                "compiler_source": "src/chronos/supervisor/compiler.py:_CAPABILITY_MATRIX",
            }
        )
    for (asset, decision), intent in _CLOSING_MATRIX.items():
        capabilities.append(
            {
                "asset_family": asset.value,
                "decision_kind": decision.value,
                "strategy_shape": None,
                "order_intent": intent.value,
                "compiler_source": "src/chronos/supervisor/compiler.py:_CLOSING_MATRIX",
            }
        )
    return sorted(
        capabilities,
        key=lambda row: (
            str(row["asset_family"]),
            str(row["decision_kind"]),
            str(row["strategy_shape"] or ""),
        ),
    )


def _instrument_facts_status(capability: dict[str, str | None], adapter: BrokerAdapter) -> str:
    asset = capability["asset_family"]
    decision = capability["decision_kind"]
    routes = _production_instrument_routes()
    if (str(asset), str(decision)) not in routes and (str(asset), None) not in routes:
        return "UNAVAILABLE_IN_PRODUCTION_GATHERER"
    implementation = _ADAPTER_IMPLEMENTATIONS[adapter]
    if asset == TradableAssetClass.CRYPTO.value and _method_refuses_unconditionally(
        implementation.qualify_crypto
    ):
        return "UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO"
    if asset == TradableAssetClass.EQUITY_OPTION.value and decision == DecisionKind.OPEN.value:
        if _setting_default("enable_autonomy_option_selection") is not False:
            raise RuntimeError("option selection no longer defaults off; update status derivation")
        return "OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT"
    return "BROKER_QUALIFIED_CONTRACT_AND_QUOTE"


def _adapter_mode_status(profile: dict[str, object], mode: AutonomyMode) -> str:
    if mode not in SUBMITTING_AUTONOMY_MODES:
        return "NOT_APPLICABLE_NON_SUBMITTING_MODE"
    if mode is AutonomyMode.PAPER_AUTONOMOUS:
        available = bool(profile["paper_submission_path"])
    else:
        available = bool(profile["live_submission_path"])
    return "CONFIGURABLE_SUBMISSION_PATH" if available else "NO_SUBMISSION_PATH"


def _row_status(*, mode: AutonomyMode, instrument_status: str, adapter_mode_status: str) -> str:
    if mode not in SUBMITTING_AUTONOMY_MODES:
        return "REFUSED_NON_SUBMITTING_MODE"
    if instrument_status == "UNAVAILABLE_IN_PRODUCTION_GATHERER":
        return "REFUSED_NO_INSTRUMENT_FACT_ROUTE"
    if instrument_status == "UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO":
        return "REFUSED_ADAPTER_INSTRUMENT_FACTS"
    if instrument_status == "OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT":
        return "REFUSED_OPTION_SELECTION_DISABLED_BY_DEFAULT"
    if adapter_mode_status == "NO_SUBMISSION_PATH":
        return "REFUSED_ADAPTER_MODE"
    return "CONDITIONAL_OWNER_AND_EVIDENCE_GATED"


def _mode_profiles() -> list[dict[str, object]]:
    return [
        {
            "mode": mode.value,
            "submission_class": (
                "SUBMITTING" if mode in SUBMITTING_AUTONOMY_MODES else "NON_SUBMITTING"
            ),
            "minimum_promotion": MINIMUM_PROMOTION_FOR_MODE[mode].value,
            "default_promotion_status": "NOT_CONFIGURED_BY_DEFAULT",
        }
        for mode in AutonomyMode
    ]


def _adapter_profiles() -> list[dict[str, object]]:
    if set(_ADAPTER_IMPLEMENTATIONS) != set(BrokerAdapter):
        raise RuntimeError("BrokerAdapter vocabulary changed without a reporting profile")
    selector = _runtime_selector()
    expected_selector = {
        "demo_mode": DemoBroker.__name__,
        "ib_async": IBKRBroker.__name__,
        "ibkr_fallback": OfficialIBKRBroker.__name__,
    }
    if selector != expected_selector:
        raise RuntimeError("broker implementation map disagrees with build_runtime")

    profiles: list[dict[str, object]] = []
    for adapter in BrokerAdapter:
        implementation = _ADAPTER_IMPLEMENTATIONS[adapter]
        submit_refused = _method_refuses_unconditionally(implementation.submit_order)
        paper_path = _settings_path_configurable(adapter, live=False) and not submit_refused
        live_path = _settings_path_configurable(adapter, live=True) and not submit_refused
        if adapter is BrokerAdapter.DEMO:
            note = (
                "Effective only with BrokerMode.DEMO; its submit_order ends in an "
                "unconditional refusal. Under BrokerMode.IBKR this enum value aliases to the "
                "official fallback instead of selecting DemoBroker."
            )
        elif submit_refused:
            note = (
                "Settings can satisfy the paper conjunction, but this read-only adapter's "
                "submit_order ends in an unconditional refusal; no submission path exists."
            )
        else:
            note = (
                "Configuration can select paper or live submission, subject to every runtime "
                "gate; repository generation does not establish gateway evidence or authority."
            )
        profiles.append(
            {
                "broker_adapter": adapter.value,
                "effective_implementation": (
                    f"{implementation.__module__}.{implementation.__qualname__}"
                ),
                "market_evidence_source": _ADAPTER_EVIDENCE_SOURCES[adapter],
                "submit_order_status": (
                    "UNCONDITIONAL_REFUSAL" if submit_refused else "IMPLEMENTED"
                ),
                "paper_submission_path": paper_path,
                "live_submission_path": live_path,
                "note": note,
            }
        )
    return profiles


def _evidence_profiles() -> list[dict[str, object]]:
    profiles: list[dict[str, object]] = [
        {
            "evidence_source": "placeholder_unbound",
            "binding_status": "DEFAULT_UNBOUND",
            "citation_kinds": [],
            "configuration_required": False,
            "note": (
                f"Default ingress identity names {INGRESS_IDENTITY.evidence_bundle_id!r} with "
                "no digest; both sides of admission's legacy comparison originate in the backend."
            ),
        }
    ]
    for kind in BundleKind:
        profiles.append(
            {
                "evidence_source": kind.value,
                "binding_status": "BOUND_DURABLE_RECORD",
                "citation_kinds": sorted(citation_kinds_for(kind)),
                "configuration_required": True,
                "note": (
                    "Backend composed and hashed the served bytes."
                    if kind is BundleKind.BACKEND_SERVED
                    else "Proposer attested to bytes the backend did not witness."
                ),
            }
        )
    return profiles


def _matrix_rows(
    capabilities: list[dict[str, str | None]], adapter_profiles: list[dict[str, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    evidence_profiles = _evidence_profiles()
    profiles_by_adapter = {
        BrokerAdapter(str(profile["broker_adapter"])): profile for profile in adapter_profiles
    }
    for capability in capabilities:
        for adapter in BrokerAdapter:
            profile = profiles_by_adapter[adapter]
            instrument_status = _instrument_facts_status(capability, adapter)
            for mode in AutonomyMode:
                adapter_status = _adapter_mode_status(profile, mode)
                for evidence in evidence_profiles:
                    rows.append(
                        {
                            "asset_family": capability["asset_family"],
                            "decision_kind": capability["decision_kind"],
                            "strategy_shape": capability["strategy_shape"],
                            "order_intent": capability["order_intent"],
                            "broker_adapter": adapter.value,
                            "mode": mode.value,
                            "evidence_source": evidence["evidence_source"],
                            "promotion_status": "NOT_CONFIGURED_BY_DEFAULT",
                            "instrument_facts_status": instrument_status,
                            "adapter_mode_status": adapter_status,
                            "current_status": _row_status(
                                mode=mode,
                                instrument_status=instrument_status,
                                adapter_mode_status=adapter_status,
                            ),
                        }
                    )
    return rows


def build_matrix() -> dict[str, object]:
    """Return the deterministic, repository-scoped matrix document."""

    capabilities = _compiler_capabilities()
    adapter_profiles = _adapter_profiles()
    runtime_selector = _runtime_selector()
    rows = _matrix_rows(capabilities, adapter_profiles)
    mapped_assets = {str(item["asset_family"]) for item in capabilities}
    mapped_decisions = {str(item["decision_kind"]) for item in capabilities}
    mapped_strategies = {
        str(item["strategy_shape"]) for item in capabilities if item["strategy_shape"] is not None
    }
    defaults = {
        name: _setting_default(name)
        for name in (
            "broker_mode",
            "broker_adapter",
            "ib_environment",
            "allow_order_transmit",
            "allow_live_trading",
            "autonomy_mandate_file",
            "autonomy_proposers_file",
            "autonomy_evidence_bundles",
            "enable_autonomy_option_selection",
            "autonomy_option_resolver_promotion_file",
        )
    }
    if defaults["autonomy_mandate_file"] is not None:
        raise RuntimeError("the default runtime is no longer mandate-inert; update this report")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "scripts/build_current_state.py",
        "scope": "committed source and validated defaults only",
        "authority_semantics": {
            "matrix_is": "a static report of mapped and refused code paths",
            "matrix_is_not": "a mandate, promotion, deployment probe, or authorization",
            "reads_external_state": False,
            "default_runtime_status": "INERT_NO_MANDATE",
        },
        "source_fingerprints": _source_fingerprints(),
        "repository_defaults": defaults,
        "configuration_findings": [
            {
                "id": "BROKER_ADAPTER_DEMO_IBKR_ALIAS",
                "status": "UNRESOLVED",
                "observation": (
                    f"BrokerMode.DEMO selects {runtime_selector['demo_mode']} without consulting "
                    "broker_adapter; BrokerMode.IBKR plus BrokerAdapter.DEMO reaches "
                    f"build_runtime's fallback and constructs {runtime_selector['ibkr_fallback']}."
                ),
                "source": "src/chronos/runtime.py:build_runtime",
            }
        ],
        "mode_profiles": _mode_profiles(),
        "adapter_profiles": adapter_profiles,
        "evidence_profiles": _evidence_profiles(),
        "compiler_capabilities": capabilities,
        "unmapped_vocabulary": {
            "asset_families": sorted(
                item.value for item in TradableAssetClass if item.value not in mapped_assets
            ),
            "decision_kinds": sorted(
                item.value for item in DecisionKind if item.value not in mapped_decisions
            ),
            "strategy_shapes": sorted(
                item.value for item in StrategyForm if item.value not in mapped_strategies
            ),
        },
        "matrix_columns": list(MATRIX_COLUMNS),
        "matrix_row_count": len(rows),
        "matrix_rows": [[row[column] for column in MATRIX_COLUMNS] for row in rows],
    }


def render_json(matrix: dict[str, object]) -> str:
    """Render metadata readably and each columnar matrix row on one diffable line."""

    metadata = {key: value for key, value in matrix.items() if key != "matrix_rows"}
    rows = matrix["matrix_rows"]
    assert isinstance(rows, list)
    rendered = json.dumps(metadata, indent=2, default=_json_default, ensure_ascii=False)
    assert rendered.endswith("\n}")
    prefix = rendered[:-2] + ',\n  "matrix_rows": [\n'
    row_lines = ",\n".join(
        "    " + json.dumps(row, default=_json_default, ensure_ascii=False) for row in rows
    )
    return prefix + row_lines + "\n  ]\n}\n"


def _matrix_records(matrix: dict[str, object]) -> list[dict[str, object]]:
    columns = matrix["matrix_columns"]
    rows = matrix["matrix_rows"]
    assert isinstance(columns, list)
    assert isinstance(rows, list)
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    rendered = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    rendered.extend(
        "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows
    )
    return rendered


def _display_default(value: object) -> str:
    return json.dumps(value, default=_json_default)


def render_markdown(matrix: dict[str, object]) -> str:
    """Render the human page from the same matrix document."""

    defaults = matrix["repository_defaults"]
    assert isinstance(defaults, dict)
    capabilities = matrix["compiler_capabilities"]
    assert isinstance(capabilities, list)
    modes = matrix["mode_profiles"]
    assert isinstance(modes, list)
    adapters = matrix["adapter_profiles"]
    assert isinstance(adapters, list)
    evidence_profiles = matrix["evidence_profiles"]
    assert isinstance(evidence_profiles, list)
    rows = _matrix_records(matrix)
    unmapped = matrix["unmapped_vocabulary"]
    assert isinstance(unmapped, dict)
    fingerprints = matrix["source_fingerprints"]
    assert isinstance(fingerprints, list)

    status_counts = Counter(str(row["current_status"]) for row in rows)
    instrument_by_key = {
        (
            str(row["asset_family"]),
            str(row["decision_kind"]),
            str(row["strategy_shape"] or "—"),
            str(row["broker_adapter"]),
        ): str(row["instrument_facts_status"])
        for row in rows
    }

    lines = [
        "# Chronos current state",
        "",
        (
            "> **Generated file — do not hand-edit.** Run "
            "`.venv/bin/python scripts/build_current_state.py` after changing a source "
            "listed below."
        ),
        "",
        (
            "This page reports committed code paths and validated repository defaults. It reads "
            "no environment, mandate, promotion file, database, broker, account, or market data. "
            "A mapped path is therefore **not authorization**, and `MITIGATED` is not `CLOSED`."
        ),
        "",
        "## Default posture",
        "",
    ]
    lines.extend(
        _markdown_table(
            ("Setting", "Committed default"),
            [(str(name), "`" + _display_default(value) + "`") for name, value in defaults.items()],
        )
    )
    lines.extend(
        [
            "",
            (
                "The default runtime is `INERT_NO_MANDATE`: no autonomy runtime starts without "
                "an owner-supplied mandate, transmission defaults off, and autonomous option "
                "selection defaults off."
            ),
            "",
            "## Compiler capabilities",
            "",
        ]
    )
    capability_rows: list[tuple[str, ...]] = []
    for capability in capabilities:
        for adapter in BrokerAdapter:
            key = (
                str(capability["asset_family"]),
                str(capability["decision_kind"]),
                str(capability["strategy_shape"] or "—"),
                adapter.value,
            )
            capability_rows.append(
                (
                    key[0],
                    key[1],
                    key[2],
                    str(capability["order_intent"]),
                    adapter.value,
                    instrument_by_key[key],
                )
            )
    lines.extend(
        _markdown_table(
            (
                "Asset family",
                "Decision",
                "Strategy",
                "Order intent",
                "Adapter",
                "Production facts route",
            ),
            capability_rows,
        )
    )
    lines.extend(
        [
            "",
            (
                "`UNAVAILABLE_IN_PRODUCTION_GATHERER` means the compiler can express the intent "
                "but the backend cannot currently obtain that decision's own qualified contract "
                "and quote. Opening equity options have a receipt-bound route, but it is disabled "
                "by default. `UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO` means the production gatherer "
                "has a crypto branch but that adapter refuses crypto qualification."
            ),
            "",
            "## Cross-product status",
            "",
            (
                f"The JSON expands {len(capabilities)} compiler mappings across {len(adapters)} "
                f"broker adapters, {len(modes)} autonomy modes, and {len(evidence_profiles)} "
                f"decision-evidence sources: **{len(rows)} rows**."
            ),
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("Current status", "Rows"),
            [(status, str(count)) for status, count in sorted(status_counts.items())],
        )
    )
    lines.extend(["", "## Autonomy modes and promotion", ""])
    lines.extend(
        _markdown_table(
            ("Mode", "Submission class", "Minimum promotion", "Default promotion status"),
            [
                (
                    str(item["mode"]),
                    str(item["submission_class"]),
                    str(item["minimum_promotion"]),
                    str(item["default_promotion_status"]),
                )
                for item in modes
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "Promotion values in a supplied mandate are external owner state. This generator "
                "does not load or validate one, so every row reports "
                "`NOT_CONFIGURED_BY_DEFAULT` rather than guessing an earned rung."
            ),
            "",
            "## Broker adapters and market-evidence sources",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            (
                "Adapter",
                "Effective implementation",
                "Market-evidence source",
                "Submit implementation",
                "Paper path",
                "Live path",
            ),
            [
                (
                    str(item["broker_adapter"]),
                    str(item["effective_implementation"]),
                    str(item["market_evidence_source"]),
                    str(item["submit_order_status"]),
                    "yes" if item["paper_submission_path"] else "no",
                    "yes" if item["live_submission_path"] else "no",
                )
                for item in adapters
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "Evidence-source labels identify where the runtime would gather facts; they do "
                "not prove that a gateway was connected or that observations were correct. "
                "`BrokerAdapter.DEMO` has an unresolved naming alias: with `BrokerMode.IBKR`, the "
                "runtime fallback constructs `OfficialIBKRBroker`."
            ),
            "",
            "## Decision-evidence sources",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("Evidence source", "Binding", "Citation kinds", "Configuration required"),
            [
                (
                    str(item["evidence_source"]),
                    str(item["binding_status"]),
                    ", ".join(str(kind) for kind in item["citation_kinds"]) or "—",
                    "yes" if item["configuration_required"] else "no",
                )
                for item in evidence_profiles
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "`placeholder_unbound` is the committed default because evidence binding and the "
                "proposer registry both default off. `backend_served` means Chronos witnessed and "
                "hashed the bytes; `alert_attested` means the proposer attested to bytes Chronos "
                "did not witness. None of these labels establishes that the facts were true."
            ),
            "",
            "## Explicitly unmapped vocabulary",
            "",
            "- Asset families: " + ", ".join(f"`{item}`" for item in unmapped["asset_families"]),
            "- Decision kinds: " + ", ".join(f"`{item}`" for item in unmapped["decision_kinds"]),
            "- Strategy shapes: " + ", ".join(f"`{item}`" for item in unmapped["strategy_shapes"]),
            "",
            (
                "Unmapped means refused by the compiler whitelist. Vocabulary presence alone is "
                "not a capability."
            ),
            "",
            "## Source fingerprint",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("Source", "SHA-256"),
            [(str(item["path"]), "`" + str(item["sha256"]) + "`") for item in fingerprints],
        )
    )
    lines.extend(
        [
            "",
            "Machine-readable detail: [`capability-matrix.json`](capability-matrix.json).",
            "",
        ]
    )
    return "\n".join(lines)


def _check_current(path: Path, expected: str) -> str | None:
    displayed = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    if not path.exists():
        return f"generated artifact missing: {displayed}"
    if path.read_text(encoding="utf-8") != expected:
        return f"generated artifact stale: {displayed}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--matrix-output", type=Path, default=ROOT / MATRIX_PATH)
    parser.add_argument("--current-state-output", type=Path, default=ROOT / CURRENT_STATE_PATH)
    args = parser.parse_args()

    matrix = build_matrix()
    matrix_text = render_json(matrix)
    current_state_text = render_markdown(matrix)

    if args.check:
        errors = [
            error
            for error in (
                _check_current(args.matrix_output, matrix_text),
                _check_current(args.current_state_output, current_state_text),
            )
            if error is not None
        ]
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"current-state artifacts are current ({matrix['matrix_row_count']} matrix rows)")
        return 0

    args.matrix_output.parent.mkdir(parents=True, exist_ok=True)
    args.current_state_output.parent.mkdir(parents=True, exist_ok=True)
    args.matrix_output.write_text(matrix_text, encoding="utf-8")
    args.current_state_output.write_text(current_state_text, encoding="utf-8")
    print(f"wrote {args.matrix_output} and {args.current_state_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

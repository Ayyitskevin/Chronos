#!/usr/bin/env python
"""Build the repository-scoped capability matrix and current-state page.

The outputs are deliberately a pure function of committed source.  This script
does not read environment variables, a mandate, promotion files, a database, or
a broker.  Consequently it can report code paths and repository defaults, but
it can never report that a deployment is authorized or that operational
evidence exists.

The page also carries the repository's milestone state (VCP §5: one generated
current-state page), derived from the table documents and source rather than
from HANDOFF.md/TASKS.md prose: the D/ADR/R id watermarks by the §7 scans, the
risk register's status counts and OPEN rows, the plan's §6 findings with a
status read mechanically from the plan's own markers (UNKNOWN when it cannot),
the public ``chronos.auditlog`` names, and the forwarding flags as declared in
source.  Facts that change without a commit — the default branch, HEAD, a test
count — appear as the command that measures them, never as a copied value.

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
import re
import sys
import textwrap
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any

import chronos.auditlog
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


# ------------------------------------------------ repository state (VCP §5: one generated page)
#
# Milestone facts a reader used to take from HANDOFF.md / TASKS.md prose, derived from the
# table documents and source instead.  Still a pure function of committed bytes: anything that
# changes without a commit (the default branch, HEAD, a test count) is a COMMAND here, never a
# copied value — docs/AGENT_PROTOCOL.md: "copied values are how sixteen skills went stale".

STATE_SOURCE_PATHS = (
    Path("DECISIONS.md"),
    Path("RISK_REGISTER.md"),
    Path("docs/VISION_COMPLETION_PLAN.md"),
    Path("src/chronos/auditlog/__init__.py"),
    Path("src/chronos/bridge/config.py"),
    Path("worker/config.py"),
)
ADR_DIR = Path("docs/adr")
_VOLATILE_FACTS = (
    ("Default branch", "`git ls-remote --symref origin HEAD`"),
    ("Current commit", "`git rev-parse HEAD`"),
    (
        "Test / skip / fail counts",
        "`make gates` — read the pytest line; a commit's `Gate:` footer carries what its author "
        "measured at that head, never what this page says",
    ),
)
_ID_SCANS = (
    (
        "`DECISIONS.md` D-nn",
        "grep -oE '^\\| D-[0-9]+'  DECISIONS.md      | grep -oE '[0-9]+' | sort -n | tail -1",
    ),
    ("`docs/adr/` ADR-nnnn", "ls docs/adr/ | grep -oE 'ADR-[0-9]{4}' | sort | tail -1"),
    (
        "`RISK_REGISTER.md` R-nn",
        "grep -oE '^\\| R-[0-9]+'  RISK_REGISTER.md  | grep -oE '[0-9]+' | sort -n | tail -1",
    ),
)
_FORWARD_FLAGS = (
    ("CHRONOS_TV_BRIDGE_FORWARD", Path("src/chronos/bridge/config.py")),
    ("CHRONOS_WORKER_FORWARD", Path("worker/config.py")),
)
_REGISTER_ROW = re.compile(r"^\| (R-\d+[^ |]*) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|")
_STRUCK = re.compile(r"~~.*?~~", re.S)
_BOLD_SPAN = re.compile(r"\*\*([^*]+?)\*\*", re.S)
_LEADING_CLOSURE = re.compile(r"^\s*(?:addressed|closed)\b", re.I)
_QUALIFIED_CLOSURE = re.compile(r"^\s*(?P<qualifier>[^:.;]*?)\baddressed\s+\d{4}-\d{2}-\d{2}", re.I)
_NEGATING_QUALIFIERS = frozenset({"not", "never", "un", "partially", "partial", "unaddressed"})
_RESIDUAL_MARKER = re.compile(r"Still open from this\s+finding", re.I)
_PLAN_SECTION_6 = re.compile(r"^## 6\. .*$", re.M)
_PLAN_SECTION_END = re.compile(r"^(Required design outcomes:|## )", re.M)
_PLAN_ITEM = re.compile(r"^(\d+)\. ", re.M)
_TITLE_LIMIT = 96


def _max_row_id(path: Path, prefix: str) -> int | None:
    """Protocol §7 scan: the highest ``| <prefix>-nn`` table row id — max, not last row."""

    pattern = rf"^\| {re.escape(prefix)}-(\d+)"
    numbers = re.findall(pattern, path.read_text(encoding="utf-8"), re.M)
    return max((int(number) for number in numbers), default=None)


def _max_adr(directory: Path) -> str | None:
    listing = "\n".join(sorted(entry.name for entry in directory.iterdir()))
    names: list[str] = [str(name) for name in re.findall(r"ADR-\d{4}", listing)]
    return max(names, default=None)


def _status_phrase(cell: str) -> str:
    """``MITIGATED (posture restated 2026-07-25)`` counts as ``MITIGATED``."""

    return re.sub(r"\s*\(.*$", "", cell, flags=re.S).strip()


def _register_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _REGISTER_ROW.match(line)
        if match is None:
            continue
        row_id, risk, severity, status = (cell.strip() for cell in match.groups())
        rows.append(
            {"id": row_id, "risk": risk, "severity": severity, "status": _status_phrase(status)}
        )
    return rows


def _is_positive_marker(bold_text: str) -> bool:
    """Is one bold marker a positive closure statement, by the plan's own two forms?

    Positive, and nothing else: bold text that BEGINS with the word "Addressed" or
    "Closed" (``**Addressed 2026-08-13 (A1; R-49):**``), or "<qualifier> addressed <date>"
    where the qualifier carries no negation (``**Kill-engaged half addressed 2026-09-03
    (…):**``).  "Unaddressed", "Not addressed", "Partially addressed", "Never addressed"
    and a sentence that merely contains the word are not closure — Daybreak's HOLD on
    #221 found the substring match reading every one of them as ADDRESSED.
    """

    if _LEADING_CLOSURE.match(bold_text) is not None:
        return True
    qualified = _QUALIFIED_CLOSURE.match(bold_text)
    if qualified is None:
        return False
    qualifier_words = set(re.findall(r"[a-z]+", qualified.group("qualifier").lower()))
    return not (qualifier_words & _NEGATING_QUALIFIERS)


def _finding_status(text: str) -> str:
    """Read a §6 finding's status from the plan's own markers; UNKNOWN whenever unsure.

    A finding whose statement is struck through and that carries a positive bold marker
    (``_is_positive_marker``) is ADDRESSED — ADDRESSED_WITH_RESIDUAL when unstruck text
    still says "Still open from this finding".  An unstruck statement is OPEN.  Anything
    else (struck with no positive marker, a negated marker, empty) is UNKNOWN: the reader
    never infers closure.
    """

    stripped = text.strip()
    if not stripped:
        return "UNKNOWN"
    unstruck = _STRUCK.sub("", stripped)
    if not stripped.startswith("~~"):
        return "OPEN"
    if not any(_is_positive_marker(span) for span in _BOLD_SPAN.findall(unstruck)):
        return "UNKNOWN"
    if _RESIDUAL_MARKER.search(unstruck) is not None:
        return "ADDRESSED_WITH_RESIDUAL"
    return "ADDRESSED"


def _finding_title(raw: str) -> str:
    """The finding's first sentence (struck or not), whitespace collapsed, bounded."""

    statement = _STRUCK.match(raw.strip())
    text = statement.group(0) if statement is not None else raw
    plain = " ".join(text.replace("~~", "").replace("**", "").split())
    sentence = re.match(r".+?[.!?](?=\s|$)", plain)
    if sentence is not None:
        plain = sentence.group(0)
    if len(plain) > _TITLE_LIMIT:
        return plain[: _TITLE_LIMIT - 1].rstrip() + "…"
    return plain


def _finding_marker(raw: str) -> str:
    unstruck = _STRUCK.sub("", raw)
    match = _BOLD_SPAN.search(unstruck)
    if match is None:
        return "—"
    return " ".join(match.group(1).split()).rstrip(":")


def _unknown_finding(reason: str) -> dict[str, str]:
    return {"number": "—", "finding": reason, "status": "UNKNOWN", "marker": "—"}


def _plan_findings(path: Path) -> list[dict[str, str]]:
    """The VCP §6 numbered findings with their mechanically read status."""

    text = path.read_text(encoding="utf-8")
    heading = _PLAN_SECTION_6.search(text)
    if heading is None:
        return [_unknown_finding(f"§6 heading not found in {path.name}")]
    body = text[heading.end() :]
    end = _PLAN_SECTION_END.search(body)
    if end is not None:
        body = body[: end.start()]
    parts = _PLAN_ITEM.split(body)
    if len(parts) < 3:
        return [_unknown_finding(f"§6 numbered findings not found in {path.name}")]
    findings: list[dict[str, str]] = []
    for number, raw in zip(parts[1::2], parts[2::2], strict=True):
        findings.append(
            {
                "number": number,
                "finding": _finding_title(raw),
                "status": _finding_status(raw),
                "marker": _finding_marker(raw),
            }
        )
    return findings


def _declared_flag_default(path: Path, name: str) -> str:
    """The ``default=`` a source file declares for an environment flag, read by AST.

    ``worker/`` is never imported (D-23): its source is parsed as text.  ``not declared``
    when no call names the flag; ``unspecified`` when the call has no ``default`` keyword.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(isinstance(arg, ast.Constant) and arg.value == name for arg in node.args):
            continue
        for keyword in node.keywords:
            if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                return repr(keyword.value.value)
        return "unspecified"
    return "not declared"


def _state_fingerprints() -> list[dict[str, str]]:
    return [
        {
            "path": str(path),
            "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
        }
        for path in STATE_SOURCE_PATHS
    ]


def build_state() -> dict[str, object]:
    """The repository-state document: milestone facts derived from the tree."""

    rows = _register_rows(ROOT / "RISK_REGISTER.md")
    counts = Counter(row["status"] for row in rows)
    return {
        "volatile_facts": [{"fact": fact, "command": command} for fact, command in _VOLATILE_FACTS],
        "id_watermarks": [
            {
                "namespace": _ID_SCANS[0][0],
                "highest": _display_id("D", _max_row_id(ROOT / "DECISIONS.md", "D")),
                "scan": _ID_SCANS[0][1],
            },
            {
                "namespace": _ID_SCANS[1][0],
                "highest": _max_adr(ROOT / ADR_DIR) or "none",
                "scan": _ID_SCANS[1][1],
            },
            {
                "namespace": _ID_SCANS[2][0],
                "highest": _display_id("R", _max_row_id(ROOT / "RISK_REGISTER.md", "R")),
                "scan": _ID_SCANS[2][1],
            },
        ],
        "register_status_counts": [
            {"status": status, "rows": count} for status, count in sorted(counts.items())
        ],
        "register_row_count": len(rows),
        "register_open_rows": [row for row in rows if row["status"] == "OPEN"],
        "plan_findings": _plan_findings(ROOT / "docs/VISION_COMPLETION_PLAN.md"),
        "auditlog_public_names": list(chronos.auditlog.__all__),
        "forwarding_flags": [
            {
                "flag": flag,
                "declared_in": str(path),
                "declared_default": _declared_flag_default(ROOT / path, flag),
            }
            for flag, path in _FORWARD_FLAGS
        ],
        "state_fingerprints": _state_fingerprints(),
    }


def _display_id(prefix: str, number: int | None) -> str:
    return "none" if number is None else f"{prefix}-{number}"


def render_state(state: dict[str, object]) -> list[str]:
    """The ``## Repository state`` section of the page."""

    volatile = state["volatile_facts"]
    assert isinstance(volatile, list)
    watermarks = state["id_watermarks"]
    assert isinstance(watermarks, list)
    counts = state["register_status_counts"]
    assert isinstance(counts, list)
    open_rows = state["register_open_rows"]
    assert isinstance(open_rows, list)
    findings = state["plan_findings"]
    assert isinstance(findings, list)
    names = state["auditlog_public_names"]
    assert isinstance(names, list)
    flags = state["forwarding_flags"]
    assert isinstance(flags, list)
    fingerprints = state["state_fingerprints"]
    assert isinstance(fingerprints, list)

    lines = [
        "## Repository state",
        "",
        (
            "Milestone facts derived from the table documents and source listed under "
            "*State inputs* — never from HANDOFF.md, TASKS.md or any other prose, which retain "
            "history but cannot present old milestone state as current truth (plan §5)."
        ),
        "",
        "### Volatile facts — run, do not copy",
        "",
        (
            "These change without a commit, so this page carries the command that measures "
            "each one and never its value."
        ),
        "",
    ]
    lines.extend(
        _markdown_table(
            ("Fact", "Command"),
            [(str(item["fact"]), str(item["command"])) for item in volatile],
        )
    )
    lines.extend(["", "### ID watermarks (protocol §7 scans)", ""])
    lines.extend(
        _markdown_table(
            ("Namespace", "Highest allocated"),
            [(str(item["namespace"]), str(item["highest"])) for item in watermarks],
        )
    )
    lines.extend(
        [
            "",
            (
                "The next id is `max + 1`, scanned in the same session by the same PR that "
                "claims it (docs/AGENT_PROTOCOL.md §7); this table is a reading, not a "
                "reservation. The scans, verbatim:"
            ),
            "",
            "```bash",
        ]
    )
    lines.extend(str(item["scan"]) for item in watermarks)
    lines.extend(["```", "", "### Risk register", ""])
    count_rows: list[tuple[str, ...]] = [
        (str(item["status"]), str(item["rows"])) for item in counts
    ]
    count_rows.append(("all rows", str(state["register_row_count"])))
    lines.extend(_markdown_table(("Status", "Rows"), count_rows))
    lines.extend(
        [
            "",
            (
                "Status is the register's own column with its parenthetical qualifier "
                "stripped; `MITIGATED` is not `CLOSED`. Open rows:"
            ),
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("ID", "Risk", "Sev"),
            [(str(row["id"]), str(row["risk"]), str(row["severity"])) for row in open_rows]
            or [("—", "no OPEN rows", "—")],
        )
    )
    lines.extend(["", "### Vision plan §6 findings", ""])
    lines.extend(
        _markdown_table(
            ("#", "Finding", "Status", "Marker"),
            [
                (
                    str(item["number"]),
                    str(item["finding"]),
                    str(item["status"]),
                    str(item["marker"]),
                )
                for item in findings
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "Status is read mechanically from the plan's own markers: a struck-through "
                "finding carrying a bold *addressed* marker is `ADDRESSED`, or "
                '`ADDRESSED_WITH_RESIDUAL` when unstruck text still says "Still open from '
                'this finding"; an unstruck finding is `OPEN`; anything else is `UNKNOWN`. '
                "UNKNOWN is never closed."
            ),
            "",
            "### `chronos.auditlog` public names",
            "",
            ", ".join(f"`{name}`" for name in names)
            + " — from `chronos.auditlog.__all__`, in declared order.",
            "",
            "### Forwarding flags — declared, never read here",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("Flag", "Declared in", "Declared default", "Value"),
            [
                (
                    f"`{item['flag']}`",
                    f"`{item['declared_in']}`",
                    f"`{item['declared_default']}`",
                    "not read (this page reads no environment)",
                )
                for item in flags
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "Both are built inert and enabled only by the owner (plan §11); a value "
                "would be a claim about a deployment, which this page cannot make."
            ),
            "",
            "### State inputs",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ("Source", "SHA-256"),
            [(str(item["path"]), "`" + str(item["sha256"]) + "`") for item in fingerprints],
        )
    )
    return lines


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


def render_markdown(matrix: dict[str, object], state: dict[str, object] | None = None) -> str:
    """Render the human page from the matrix document and the repository-state document."""

    if state is None:
        state = build_state()
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
        ]
    )
    lines.extend(render_state(state))
    lines.extend(["", "## Source fingerprint", ""])
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
    current_state_text = render_markdown(matrix, build_state())

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

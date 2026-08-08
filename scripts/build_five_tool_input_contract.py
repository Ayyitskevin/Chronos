#!/usr/bin/env python3
"""Build the frozen Five-Tool Pine input contract.

The output is JSON-formatted YAML. JSON is a strict subset of YAML, which lets
the research runtime validate the contract with the Python standard library
instead of making PyYAML part of the parity trust boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EXPECTED_INPUT_COUNT = 219

# These are deliberately hand-maintained semantics. The input list below them
# is mechanical; these rules describe ordering and state that syntax alone
# cannot recover from Pine.
SEMANTICS: dict[str, Any] = {
    "parity_status": "UNVERIFIED",
    "timing": [
        {
            "id": "closed-primary-bars",
            "rule": "Evaluate signals only after the primary chart bar is confirmed.",
        },
        {
            "id": "next-bar-orders",
            "rule": (
                "Pine process_orders_on_close=false: a market order created on a confirmed "
                "bar is eligible no earlier than the next bar."
            ),
        },
        {
            "id": "prior-completed-htf",
            "rule": (
                "Higher-timeframe filters consume the prior completed HTF close and EMA; "
                "the in-progress HTF bar is never eligible."
            ),
        },
        {
            "id": "pivot-confirmation-lag",
            "rule": "Divergence pivots become observable piv_r primary bars after the pivot bar.",
        },
        {
            "id": "history-pinned-markov",
            "rule": (
                "Markov counts and dwell samples are path dependent. Every evaluation must pin "
                "an inclusive primary-history start timestamp."
            ),
        },
    ],
    "dependency_order": [
        {"ordinal": 1, "stage": "closed primary OHLCV", "depends_on": []},
        {
            "ordinal": 2,
            "stage": "benchmark and prior-completed higher-timeframe alignment",
            "depends_on": ["closed primary OHLCV"],
        },
        {
            "ordinal": 3,
            "stage": "volatility, EMA, ADX/ER, RSI/MFI, ATR, and relative strength",
            "depends_on": [
                "closed primary OHLCV",
                "benchmark and prior-completed higher-timeframe alignment",
            ],
        },
        {
            "ordinal": 4,
            "stage": "raw and confirmed regime state",
            "depends_on": ["volatility, EMA, ADX/ER, RSI/MFI, ATR, and relative strength"],
        },
        {
            "ordinal": 5,
            "stage": "Markov transitions, dwell history, and AVWAP anchors",
            "depends_on": ["raw and confirmed regime state"],
        },
        {
            "ordinal": 6,
            "stage": "divergence and named long/short setup gates",
            "depends_on": [
                "volatility, EMA, ADX/ER, RSI/MFI, ATR, and relative strength",
                "Markov transitions, dwell history, and AVWAP anchors",
            ],
        },
        {
            "ordinal": 7,
            "stage": "entry decision, sizing plan, and exit updates",
            "depends_on": ["divergence and named long/short setup gates"],
        },
    ],
    "warmups": [
        {
            "feature": "regime z-score",
            "requirement": "lookback + one return; volatility model may add its own seed",
        },
        {
            "feature": "volatility percentile adjustment",
            "requirement": "vol_percentile_len realized-volatility observations",
        },
        {"feature": "Mansfield relative strength", "requirement": "mans_len aligned bars"},
        {
            "feature": "benchmark and HTF EMA filters",
            "requirement": "their configured EMA history plus one completed HTF bar",
        },
        {
            "feature": "divergence",
            "requirement": "two confirmed pivots separated by min_gap..max_gap, each delayed piv_r",
        },
        {
            "feature": "Markov probability gates",
            "requirement": "configured row sample floor after the pinned history start",
        },
        {
            "feature": "dwell percentile gates",
            "requirement": "at least five completed spells for the current regime",
        },
        {
            "feature": "AVWAP",
            "requirement": "a qualifying anchor and a valid weighting observation",
        },
    ],
    "deviations": [
        {
            "id": "tradingview-owner-export",
            "classification": "verification gap",
            "status": "UNVERIFIED",
            "description": (
                "No genuine owner-run TradingView export is checked in; synthetic fixtures can "
                "verify Chronos determinism but cannot establish platform parity."
            ),
        },
        {
            "id": "intrabar-fill-priority",
            "classification": "platform ambiguity",
            "status": "must-be-explicit",
            "description": (
                "TradingView bar-magnifier fills and same-bar stop/target priority are not "
                "recoverable from bar-close exports; Chronos must label its fill policy."
            ),
        },
        {
            "id": "three-leg-milestones",
            "classification": "known Pine inference",
            "status": "must-not-copy-silently",
            "description": (
                "The Pine script infers target milestones from leg absence. Chronos must retain "
                "explicit per-leg exit reason and milestone state."
            ),
        },
        {
            "id": "side-switch-attribution",
            "classification": "known Pine accounting risk",
            "status": "must-not-copy-silently",
            "description": (
                "Blended sleeve attribution around side switches requires explicit side-owned "
                "fills and fees rather than position-sign inference."
            ),
        },
        {
            "id": "stop-guard-asymmetry",
            "classification": "known Pine asymmetry",
            "status": "documented",
            "description": (
                "Long entry validation checks a positive stop price while short validation checks "
                "a positive stop distance; Chronos applies an explicit positive-distance rule."
            ),
        },
        {
            "id": "duplicate-alert-paths",
            "classification": "known Pine operational risk",
            "status": "must-not-copy",
            "description": (
                "Static alertcondition and dynamic alert paths can describe the same event; "
                "Chronos emits one event identity per decision."
            ),
        },
        {
            "id": "profit-factor-infinity",
            "classification": "known Pine presentation sentinel",
            "status": "must-not-copy",
            "description": (
                "Pine displays profit factor 999 when gross loss is zero; Chronos represents the "
                "value as undefined/infinite with explicit gross components."
            ),
        },
        {
            "id": "daily-loss-on-daily-bars",
            "classification": "known Pine semantic defect",
            "status": "must-not-copy-silently",
            "description": (
                "On daily bars the script resets its day-start equity on every bar, making its "
                "daily-loss halt inert; Chronos records session semantics explicitly."
            ),
        },
        {
            "id": "long-plus-age-gate",
            "classification": "prose/code mismatch",
            "status": "source-code-authoritative",
            "description": (
                "Despite comments describing optional bull age gating, strict LONG+ Markov mode "
                "also applies configured age and maturity ceilings; only the dwell gate is toggled."
            ),
        },
    ],
}

_INPUT_START = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"input\.(?P<type>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\("
)
_STRING_CONSTANT = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r'(?P<value>"(?:[^"\\]|\\.)*")[ \t]*(?://.*)?$'
)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def _matching_parenthesis(source: str, opening: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    index = opening
    while index < len(source):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "/" and index + 1 < len(source) and source[index + 1] == "/":
            newline = source.find("\n", index + 2)
            if newline == -1:
                break
            index = newline
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                break
        index += 1
    raise ValueError(f"unterminated input call beginning at byte {opening}")


def _split_top_level(expression: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    matching = {")": "(", "]": "[", "}": "{"}
    in_string = False
    escaped = False
    for index, char in enumerate(expression):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in depths:
            depths[char] += 1
        elif char in matching:
            opener = matching[char]
            depths[opener] -= 1
            if depths[opener] < 0:
                raise ValueError(f"unbalanced expression: {expression!r}")
        elif char == delimiter and not any(depths.values()):
            parts.append(expression[start:index].strip())
            start = index + 1
    if in_string or any(depths.values()):
        raise ValueError(f"unbalanced expression: {expression!r}")
    parts.append(expression[start:].strip())
    return parts


def _named_argument(part: str) -> tuple[str, str] | None:
    in_string = False
    escaped = False
    depths = {"(": 0, "[": 0, "{": 0}
    matching = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(part):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in depths:
            depths[char] += 1
        elif char in matching:
            depths[matching[char]] -= 1
        elif char == "=" and not any(depths.values()):
            name = part[:index].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                return name, part[index + 1 :].strip()
            return None
    return None


def _literal(expression: str, constants: dict[str, str]) -> object:
    expression = expression.strip()
    if expression in constants:
        return constants[expression]
    if expression.startswith('"') and expression.endswith('"'):
        return json.loads(expression)
    if expression == "true":
        return True
    if expression == "false":
        return False
    if _NUMBER.fullmatch(expression):
        return float(expression) if any(char in expression for char in ".eE") else int(expression)
    if expression.startswith("[") and expression.endswith("]"):
        body = expression[1:-1].strip()
        return [] if not body else [_literal(part, constants) for part in _split_top_level(body)]
    return expression


def _value_record(expression: str, constants: dict[str, str]) -> dict[str, object]:
    stripped = expression.strip()
    value = _literal(stripped, constants)
    if stripped.startswith("timestamp("):
        timestamp_args = _split_top_level(stripped[len("timestamp(") : -1])
        value = _literal(timestamp_args[0], constants) if timestamp_args else stripped
        kind = "timestamp"
    elif (
        stripped in constants
        or stripped in {"true", "false"}
        or stripped.startswith('"')
        or _NUMBER.fullmatch(stripped)
        or stripped.startswith("[")
    ):
        kind = "literal"
    else:
        kind = "expression"
    return {"expression": stripped, "kind": kind, "value": value}


def parse_inputs(source: str) -> list[dict[str, object]]:
    """Parse ordered Pine input declarations, including multiline calls."""

    constants = {
        match.group("name"): json.loads(match.group("value"))
        for match in _STRING_CONSTANT.finditer(source)
    }
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for ordinal, match in enumerate(_INPUT_START.finditer(source), start=1):
        name = match.group("name")
        if name in seen:
            raise ValueError(f"duplicate Pine input name: {name}")
        seen.add(name)
        opening = match.end() - 1
        closing = _matching_parenthesis(source, opening)
        arguments = _split_top_level(source[opening + 1 : closing])
        positional: list[str] = []
        named: dict[str, str] = {}
        for argument in arguments:
            parsed_named = _named_argument(argument)
            if parsed_named is None:
                if named:
                    raise ValueError(f"positional argument after named argument for {name}")
                positional.append(argument)
            else:
                key, value = parsed_named
                if key in named:
                    raise ValueError(f"duplicate named argument {key} for {name}")
                named[key] = value
        if len(positional) < 2:
            raise ValueError(f"input {name} has fewer than two positional arguments")

        title_value = _literal(positional[1], constants)
        if not isinstance(title_value, str):
            raise ValueError(f"input {name} has non-string title: {positional[1]}")
        group_expression = named.get("group")
        group_value = _literal(group_expression, constants) if group_expression else None
        if group_value is not None and not isinstance(group_value, str):
            raise ValueError(f"input {name} has non-string group: {group_expression}")
        options_value = _literal(named["options"], constants) if "options" in named else None
        if options_value is not None and not isinstance(options_value, list):
            raise ValueError(f"input {name} has non-list options: {named['options']}")
        tooltip_value = _literal(named["tooltip"], constants) if "tooltip" in named else None
        if tooltip_value is not None and not isinstance(tooltip_value, str):
            raise ValueError(f"input {name} has non-string tooltip")

        known_named = {"options", "minval", "maxval", "step", "group", "tooltip", "inline"}
        records.append(
            {
                "ordinal": ordinal,
                "name": name,
                "pine_type": match.group("type"),
                "declaration_line": source.count("\n", 0, match.start()) + 1,
                "declaration": source[match.start() : closing + 1].strip(),
                "default": _value_record(positional[0], constants),
                "title": title_value,
                "title_expression": positional[1],
                "options": options_value,
                "options_expression": named.get("options"),
                "minval": (
                    _value_record(named["minval"], constants) if "minval" in named else None
                ),
                "maxval": (
                    _value_record(named["maxval"], constants) if "maxval" in named else None
                ),
                "step": _value_record(named["step"], constants) if "step" in named else None,
                "group": group_value,
                "group_expression": group_expression,
                "tooltip": tooltip_value,
                "tooltip_expression": named.get("tooltip"),
                "inline": _literal(named["inline"], constants) if "inline" in named else None,
                "extra_positional_arguments": positional[2:],
                "extra_named_arguments": {
                    key: value for key, value in named.items() if key not in known_named
                },
            }
        )
    return records


def build_contract(source_path: Path) -> dict[str, object]:
    source_bytes = source_path.read_bytes()
    source = source_bytes.decode("utf-8")
    inputs = parse_inputs(source)
    if len(inputs) != EXPECTED_INPUT_COUNT:
        raise ValueError(
            f"expected {EXPECTED_INPUT_COUNT} Pine inputs, parsed {len(inputs)}; "
            "review the source and parser before changing the pin"
        )
    version_match = re.search(r"(?m)^//@version=(\d+)$", source)
    script_version_match = re.search(r"Five-Tool Confluence AIO v([0-9.]+)", source)
    if version_match is None or script_version_match is None:
        raise ValueError("Pine language or script version marker is missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy_id": "five_tool_confluence_v3_6",
        "capability_scope": "research-only",
        "owner_approved": False,
        "pine": {
            "source_path": "research/pine/00_five_tool_confluence_aio.pine",
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_line_count": len(source.splitlines()),
            "pine_language_version": int(version_match.group(1)),
            "script_version": script_version_match.group(1),
            "input_count": len(inputs),
        },
        "semantics": SEMANTICS,
        "inputs": inputs,
    }


def render_contract(contract: dict[str, object]) -> str:
    return json.dumps(contract, indent=2, ensure_ascii=False) + "\n"


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=repo_root / "research/pine/00_five_tool_confluence_aio.pine",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "specs/five_tool_confluence_v3_6.yaml",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in artifact differs from deterministic regeneration",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    rendered = render_contract(build_contract(args.source))
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"five-tool input contract is stale: {args.output}")
            return 1
        print(f"five-tool input contract is current: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

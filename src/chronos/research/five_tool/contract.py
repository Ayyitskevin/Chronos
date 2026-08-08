"""Immutable loader for the frozen Five-Tool Pine input contract.

This module is research-only. It intentionally performs no strategy, order,
broker, or runtime registration. Loading is fail-closed: the referenced Pine
source must still have the exact bytes pinned by the generated artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

Scalar = str | int | float | bool | None
InputDefault = str | int | float | bool
ValueKind = Literal["literal", "expression", "timestamp"]


class FiveToolContractError(ValueError):
    """Base class for malformed or stale Five-Tool contracts."""


class ContractSchemaError(FiveToolContractError):
    """The generated artifact does not satisfy the runtime schema."""


class ContractDriftError(FiveToolContractError):
    """The live Pine source no longer matches the frozen artifact."""


@dataclass(frozen=True, slots=True)
class PineValue:
    """A Pine expression plus its lossless expression text and parsed scalar."""

    expression: str
    kind: ValueKind
    value: Scalar


@dataclass(frozen=True, slots=True)
class PineInput:
    """One ordered ``input.*`` declaration from the Pine source."""

    ordinal: int
    name: str
    pine_type: str
    declaration_line: int
    declaration: str
    default: PineValue
    title: str
    title_expression: str
    options: tuple[Scalar, ...] | None
    options_expression: str | None
    minval: PineValue | None
    maxval: PineValue | None
    step: PineValue | None
    group: str | None
    group_expression: str | None
    tooltip: str | None
    tooltip_expression: str | None
    inline: Scalar
    extra_positional_arguments: tuple[str, ...]
    extra_named_arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PineSource:
    source_path: str
    source_sha256: str
    source_line_count: int
    pine_language_version: int
    script_version: str
    input_count: int


@dataclass(frozen=True, slots=True)
class TimingRule:
    id: str
    rule: str


@dataclass(frozen=True, slots=True)
class DependencyStage:
    ordinal: int
    stage: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WarmupRule:
    feature: str
    requirement: str


@dataclass(frozen=True, slots=True)
class Deviation:
    id: str
    classification: str
    status: str
    description: str


@dataclass(frozen=True, slots=True)
class FiveToolSemantics:
    parity_status: str
    timing: tuple[TimingRule, ...]
    dependency_order: tuple[DependencyStage, ...]
    warmups: tuple[WarmupRule, ...]
    deviations: tuple[Deviation, ...]


@dataclass(frozen=True, slots=True)
class FiveToolContract:
    schema_version: int
    document_kind: str
    strategy_id: str
    capability_scope: str
    owner_approved: bool
    pine: PineSource
    semantics: FiveToolSemantics
    inputs: tuple[PineInput, ...]

    def input(self, name: str) -> PineInput:
        """Return a named input or fail rather than silently applying a fallback."""

        for item in self.inputs:
            if item.name == name:
                return item
        raise KeyError(name)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_contract_path() -> Path:
    return _repo_root() / "specs/five_tool_confluence_v3_6.yaml"


def default_source_path() -> Path:
    return _repo_root() / "research/pine/00_five_tool_confluence_aio.pine"


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContractSchemaError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ContractSchemaError(f"{context} must be an array")
    return cast(list[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ContractSchemaError(f"{context} must be a string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSchemaError(f"{context} must be an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ContractSchemaError(f"{context} must be a boolean")
    return value


def _optional_string(value: object, context: str) -> str | None:
    return None if value is None else _string(value, context)


def _scalar(value: object, context: str) -> Scalar:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise ContractSchemaError(f"{context} must be a scalar")


def _value(value: object, context: str) -> PineValue:
    raw = _object(value, context)
    expression = _string(raw.get("expression"), f"{context}.expression")
    kind_text = _string(raw.get("kind"), f"{context}.kind")
    if kind_text not in {"literal", "expression", "timestamp"}:
        raise ContractSchemaError(f"{context}.kind is unsupported: {kind_text!r}")
    return PineValue(
        expression=expression,
        kind=cast(ValueKind, kind_text),
        value=_scalar(raw.get("value"), f"{context}.value"),
    )


def _optional_value(value: object, context: str) -> PineValue | None:
    return None if value is None else _value(value, context)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{context}[{index}]") for index, item in enumerate(_array(value, context))
    )


def _parse_input(value: object, index: int) -> PineInput:
    context = f"inputs[{index}]"
    raw = _object(value, context)
    options_raw = raw.get("options")
    options = (
        None
        if options_raw is None
        else tuple(
            _scalar(item, f"{context}.options[{option_index}]")
            for option_index, item in enumerate(_array(options_raw, f"{context}.options"))
        )
    )
    extra_named_raw = _object(raw.get("extra_named_arguments"), f"{context}.extra_named_arguments")
    return PineInput(
        ordinal=_integer(raw.get("ordinal"), f"{context}.ordinal"),
        name=_string(raw.get("name"), f"{context}.name"),
        pine_type=_string(raw.get("pine_type"), f"{context}.pine_type"),
        declaration_line=_integer(raw.get("declaration_line"), f"{context}.declaration_line"),
        declaration=_string(raw.get("declaration"), f"{context}.declaration"),
        default=_value(raw.get("default"), f"{context}.default"),
        title=_string(raw.get("title"), f"{context}.title"),
        title_expression=_string(raw.get("title_expression"), f"{context}.title_expression"),
        options=options,
        options_expression=_optional_string(
            raw.get("options_expression"), f"{context}.options_expression"
        ),
        minval=_optional_value(raw.get("minval"), f"{context}.minval"),
        maxval=_optional_value(raw.get("maxval"), f"{context}.maxval"),
        step=_optional_value(raw.get("step"), f"{context}.step"),
        group=_optional_string(raw.get("group"), f"{context}.group"),
        group_expression=_optional_string(
            raw.get("group_expression"), f"{context}.group_expression"
        ),
        tooltip=_optional_string(raw.get("tooltip"), f"{context}.tooltip"),
        tooltip_expression=_optional_string(
            raw.get("tooltip_expression"), f"{context}.tooltip_expression"
        ),
        inline=_scalar(raw.get("inline"), f"{context}.inline"),
        extra_positional_arguments=_string_tuple(
            raw.get("extra_positional_arguments"), f"{context}.extra_positional_arguments"
        ),
        extra_named_arguments=tuple(
            sorted(
                (
                    _string(key, f"{context}.extra_named_arguments key"),
                    _string(item, f"{context}.extra_named_arguments.{key}"),
                )
                for key, item in extra_named_raw.items()
            )
        ),
    )


def _parse_semantics(value: object) -> FiveToolSemantics:
    raw = _object(value, "semantics")
    parity_status = _string(raw.get("parity_status"), "semantics.parity_status")
    if parity_status != "UNVERIFIED":
        raise ContractSchemaError(
            "Five-Tool platform parity must remain UNVERIFIED without exports"
        )
    timing = tuple(
        TimingRule(
            id=_string(item_raw.get("id"), f"semantics.timing[{index}].id"),
            rule=_string(item_raw.get("rule"), f"semantics.timing[{index}].rule"),
        )
        for index, item in enumerate(_array(raw.get("timing"), "semantics.timing"))
        for item_raw in [_object(item, f"semantics.timing[{index}]")]
    )
    dependencies = tuple(
        DependencyStage(
            ordinal=_integer(
                item_raw.get("ordinal"), f"semantics.dependency_order[{index}].ordinal"
            ),
            stage=_string(item_raw.get("stage"), f"semantics.dependency_order[{index}].stage"),
            depends_on=_string_tuple(
                item_raw.get("depends_on"), f"semantics.dependency_order[{index}].depends_on"
            ),
        )
        for index, item in enumerate(
            _array(raw.get("dependency_order"), "semantics.dependency_order")
        )
        for item_raw in [_object(item, f"semantics.dependency_order[{index}]")]
    )
    warmups = tuple(
        WarmupRule(
            feature=_string(item_raw.get("feature"), f"semantics.warmups[{index}].feature"),
            requirement=_string(
                item_raw.get("requirement"), f"semantics.warmups[{index}].requirement"
            ),
        )
        for index, item in enumerate(_array(raw.get("warmups"), "semantics.warmups"))
        for item_raw in [_object(item, f"semantics.warmups[{index}]")]
    )
    deviations = tuple(
        Deviation(
            id=_string(item_raw.get("id"), f"semantics.deviations[{index}].id"),
            classification=_string(
                item_raw.get("classification"),
                f"semantics.deviations[{index}].classification",
            ),
            status=_string(item_raw.get("status"), f"semantics.deviations[{index}].status"),
            description=_string(
                item_raw.get("description"), f"semantics.deviations[{index}].description"
            ),
        )
        for index, item in enumerate(_array(raw.get("deviations"), "semantics.deviations"))
        for item_raw in [_object(item, f"semantics.deviations[{index}]")]
    )
    if not timing or not dependencies or not warmups or not deviations:
        raise ContractSchemaError(
            "semantic timing, dependencies, warmups, and deviations are required"
        )
    return FiveToolSemantics(
        parity_status=parity_status,
        timing=timing,
        dependency_order=dependencies,
        warmups=warmups,
        deviations=deviations,
    )


def _parse_pine(value: object) -> PineSource:
    raw = _object(value, "pine")
    return PineSource(
        source_path=_string(raw.get("source_path"), "pine.source_path"),
        source_sha256=_string(raw.get("source_sha256"), "pine.source_sha256"),
        source_line_count=_integer(raw.get("source_line_count"), "pine.source_line_count"),
        pine_language_version=_integer(
            raw.get("pine_language_version"), "pine.pine_language_version"
        ),
        script_version=_string(raw.get("script_version"), "pine.script_version"),
        input_count=_integer(raw.get("input_count"), "pine.input_count"),
    )


def _verify_source(source_path: Path, pine: PineSource) -> None:
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise ContractDriftError(f"cannot read pinned Pine source {source_path}: {exc}") from exc
    actual_sha = hashlib.sha256(source_bytes).hexdigest()
    if actual_sha != pine.source_sha256:
        raise ContractDriftError(
            f"Pine source SHA256 drift: expected {pine.source_sha256}, got {actual_sha}"
        )
    source = source_bytes.decode("utf-8")
    actual_lines = len(source.splitlines())
    if actual_lines != pine.source_line_count:
        raise ContractDriftError(
            f"Pine source line-count drift: expected {pine.source_line_count}, got {actual_lines}"
        )
    version_match = re.search(r"(?m)^//@version=(\d+)$", source)
    if version_match is None or int(version_match.group(1)) != pine.pine_language_version:
        raise ContractDriftError("Pine language version no longer matches the frozen contract")


def load_contract(
    contract_path: Path | None = None,
    source_path: Path | None = None,
) -> FiveToolContract:
    """Load, structurally validate, and source-verify the Five-Tool contract."""

    resolved_contract = contract_path or default_contract_path()
    resolved_source = source_path or default_source_path()
    try:
        raw_document: object = json.loads(resolved_contract.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractSchemaError(f"cannot read contract {resolved_contract}: {exc}") from exc
    raw = _object(raw_document, "contract")
    schema_version = _integer(raw.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ContractSchemaError(f"unsupported Five-Tool contract schema {schema_version}")
    document_kind = _string(raw.get("document_kind"), "document_kind")
    if document_kind != "pine_input_contract":
        raise ContractSchemaError("Five-Tool artifact document_kind must be 'pine_input_contract'")
    capability_scope = _string(raw.get("capability_scope"), "capability_scope")
    owner_approved = _boolean(raw.get("owner_approved"), "owner_approved")
    if capability_scope != "research-only" or owner_approved:
        raise ContractSchemaError("Five-Tool contract must remain research-only and unapproved")
    pine = _parse_pine(raw.get("pine"))
    inputs = tuple(
        _parse_input(item, index) for index, item in enumerate(_array(raw.get("inputs"), "inputs"))
    )
    if len(inputs) != pine.input_count:
        raise ContractSchemaError(
            "input count mismatch: "
            f"Pine metadata says {pine.input_count}, artifact has {len(inputs)}"
        )
    if [item.ordinal for item in inputs] != list(range(1, len(inputs) + 1)):
        raise ContractSchemaError("input ordinals must be contiguous and source ordered")
    names = [item.name for item in inputs]
    if len(names) != len(set(names)):
        raise ContractSchemaError("input names must be unique")
    _verify_source(resolved_source, pine)
    return FiveToolContract(
        schema_version=schema_version,
        document_kind=document_kind,
        strategy_id=_string(raw.get("strategy_id"), "strategy_id"),
        capability_scope=capability_scope,
        owner_approved=owner_approved,
        pine=pine,
        semantics=_parse_semantics(raw.get("semantics")),
        inputs=inputs,
    )


def default_input_values() -> dict[str, InputDefault]:
    """Return source-ordered, normalized Pine defaults for engine configuration.

    Source expressions (for example ``close``), timestamp calls, sessions,
    symbols, and timeframes are represented by the scalar text preserved in
    :class:`PineValue`. A missing/non-scalar default is contract corruption and
    fails closed instead of being guessed.
    """

    defaults: dict[str, InputDefault] = {}
    for item in load_contract().inputs:
        value = item.default.value
        if value is None:
            raise ContractSchemaError(f"input {item.name} has no normalized default")
        defaults[item.name] = value
    return defaults


def input_contract_digest() -> str:
    """Return a canonical SHA-256 identity for the source-bound input contract."""

    contract = load_contract()
    payload = {
        "source_sha256": contract.pine.source_sha256,
        "inputs": [asdict(item) for item in contract.inputs],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def semantic_contract_digest() -> str:
    """Return the source-bound identity of the declared execution semantics.

    Input identity and semantic identity are deliberately separate locks.  A
    campaign cannot use the input digest as evidence that timing, dependency,
    warm-up, and deviation declarations were also reviewed.
    """

    contract = load_contract()
    payload = {
        "source_sha256": contract.pine.source_sha256,
        "semantics": asdict(contract.semantics),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

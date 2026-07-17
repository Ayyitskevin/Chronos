"""Versioned promotion records and objective gate evaluation (Phase 11).

A promotion record is evidence, not a switch: creating one never changes the
running mode. The operator writes the record via the CLI, reviews the gate
report, and only then reconfigures the requested mode — which the mode lock
re-derives from live evidence at startup. Promotion into CANARY_LIVE or LIVE
is refused unconditionally in this build (ADR-0007).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronos.control.modes import TradingMode

_ORDERED = [
    TradingMode.RESEARCH,
    TradingMode.BACKTEST,
    TradingMode.REPLAY,
    TradingMode.SHADOW,
    TradingMode.PAPER,
    TradingMode.CANARY_LIVE,
    TradingMode.LIVE,
]


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    created_at_utc: str
    current_mode: str
    proposed_mode: str
    code_commit: str
    strategy_versions: dict[str, str]
    risk_policy_version: str
    risk_policy_hash: str
    config_hash: str
    gate_checks: list[dict[str, object]]
    all_gates_passed: bool
    known_limitations: list[str]
    outstanding_incidents: list[str]
    owner_approval: str
    rollback_plan: str


def evaluate_promotion(
    *,
    current_mode: TradingMode,
    proposed_mode: TradingMode,
    checks: list[GateCheck],
) -> tuple[bool, list[GateCheck]]:
    """Return (allowed, augmented checks) for a proposed promotion."""

    augmented = list(checks)
    if proposed_mode in (TradingMode.CANARY_LIVE, TradingMode.LIVE):
        augmented.append(
            GateCheck(
                name="live_capability_hard_disabled",
                passed=False,
                detail=(
                    "CANARY_LIVE and LIVE are hard-disabled in this build; promotion "
                    "requires a future reviewed release plus explicit owner approval"
                ),
            )
        )
    try:
        current_index = _ORDERED.index(current_mode)
        proposed_index = _ORDERED.index(proposed_mode)
        if proposed_index > current_index + 1:
            augmented.append(
                GateCheck(
                    name="single_step_promotion",
                    passed=False,
                    detail=(
                        f"cannot skip modes: {current_mode} -> {proposed_mode}; promote "
                        "one step at a time"
                    ),
                )
            )
    except ValueError:
        augmented.append(
            GateCheck(name="known_modes", passed=False, detail="unknown mode in promotion request")
        )
    return all(check.passed for check in augmented), augmented


def write_promotion_record(
    path: Path,
    *,
    current_mode: TradingMode,
    proposed_mode: TradingMode,
    code_commit: str,
    strategy_versions: dict[str, str],
    risk_policy_version: str,
    risk_policy_hash: str,
    config_hash: str,
    checks: list[GateCheck],
    known_limitations: list[str],
    outstanding_incidents: list[str],
    owner_approval: str,
    rollback_plan: str,
) -> PromotionRecord:
    allowed, augmented = evaluate_promotion(
        current_mode=current_mode, proposed_mode=proposed_mode, checks=checks
    )
    record = PromotionRecord(
        created_at_utc=datetime.now(tz=UTC).isoformat(),
        current_mode=current_mode.value,
        proposed_mode=proposed_mode.value,
        code_commit=code_commit,
        strategy_versions=strategy_versions,
        risk_policy_version=risk_policy_version,
        risk_policy_hash=risk_policy_hash,
        config_hash=config_hash,
        gate_checks=[asdict(check) for check in augmented],
        all_gates_passed=allowed,
        known_limitations=known_limitations,
        outstanding_incidents=outstanding_incidents,
        owner_approval=owner_approval,
        rollback_plan=rollback_plan,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    return record

"""Compact operator review surface over a paper decision ledger."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from chronos.config.settings import Settings
from chronos.orders.live_block import LIVE_TRADING_BLOCKED, evaluate_live_trading_block
from chronos.paperops.ledger import DecisionLedgerError, load_and_verify
from chronos.paperops.records import DecisionRecord


@dataclass(frozen=True, slots=True)
class OperatorReview:
    ledger_ok: bool
    ledger_detail: str
    total_records: int
    considered: int
    rejected: int
    allowed: int
    acted: int  # paper fills + proposed allows
    reason_histogram: dict[str, int]
    data_health_labels: dict[str, int]
    anomalies: tuple[str, ...]
    risk_state_summary: str
    data_health_summary: str
    live_trading_blocked: bool
    live_outcome: str
    lines: tuple[str, ...]

    def render(self) -> str:
        return "\n".join(self.lines)


def _is_considered(record: DecisionRecord) -> bool:
    return record.kind in {
        "candidate_signal",
        "proposed_order",
        "risk_decision",
        "rejection",
        "data_health",
        "control_refusal",
    }


def _is_rejected(record: DecisionRecord) -> bool:
    return record.outcome == "deny"


def _is_allowed(record: DecisionRecord) -> bool:
    return record.outcome == "allow"


def _is_acted(record: DecisionRecord) -> bool:
    return record.kind in {
        "paper_fill",
        "proposed_order",
        "risk_decision",
    } and record.outcome == "allow"


def build_operator_review(
    path: Path,
    *,
    settings: Settings | None = None,
) -> OperatorReview:
    """Build a brutally clear text review of a paper decision ledger."""

    ok, detail, records = load_and_verify(path)
    if settings is None:
        settings = Settings()
    live = evaluate_live_trading_block(settings)

    if not ok:
        fail_lines = (
            "Chronos paper-ops operator review",
            "=================================",
            f"LEDGER: FAILED — {detail}",
            "Status: FAIL CLOSED (do not evaluate paper results from a corrupt ledger)",
            f"LIVE: {live.outcome}",
        )
        return OperatorReview(
            ledger_ok=False,
            ledger_detail=detail,
            total_records=0,
            considered=0,
            rejected=0,
            allowed=0,
            acted=0,
            reason_histogram={},
            data_health_labels={},
            anomalies=(f"ledger integrity failure: {detail}",),
            risk_state_summary="unknown (ledger unreadable)",
            data_health_summary="unknown (ledger unreadable)",
            live_trading_blocked=live.blocked,
            live_outcome=live.outcome,
            lines=fail_lines,
        )

    reasons: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    anomalies: list[str] = []
    considered = rejected = allowed = acted = 0
    control_denies = 0
    data_denies = 0

    for record in records:
        reasons[record.reason_code] += 1
        labels[record.data_quality_label] += 1
        if _is_considered(record):
            considered += 1
        if _is_rejected(record):
            rejected += 1
        if _is_allowed(record):
            allowed += 1
        if _is_acted(record):
            acted += 1
        if record.kind == "control_refusal":
            control_denies += 1
        if (
            record.kind == "data_health" or record.reason_code.startswith("DATA_")
        ) and record.outcome == "deny":
            data_denies += 1
        label = record.data_quality_label.upper()
        degraded_labels = {
            "DEMO",
            "SYNTHETIC",
            "DELAYED",
            "DELAYED_FROZEN",
            "STALE",
            "UNKNOWN",
            "FROZEN",
        }
        if label in degraded_labels:
            anomalies.append(
                f"seq={record.sequence} degraded data label={label} "
                f"source={record.data_source} reason={record.reason_code}"
            )
        if record.outcome == "deny" and record.reason_code in {
            "HALTED",
            "KILL_SWITCH_ENGAGED",
            "DAILY_LOSS_LIMIT",
            "DUPLICATE_ORDER",
        }:
            anomalies.append(f"seq={record.sequence} control anomaly: {record.reason_code}")

    # De-dupe anomalies while preserving order
    seen: set[str] = set()
    unique_anomalies: list[str] = []
    for item in anomalies:
        if item not in seen:
            seen.add(item)
            unique_anomalies.append(item)

    risk_summary = f"control refusals={control_denies}; allowed={allowed}; denied={rejected}"
    data_summary = (
        f"labels={dict(labels)}; data-related denies={data_denies}; only LIVE authorizes opens"
    )

    lines: list[str] = [
        "Chronos paper-ops operator review",
        "=================================",
        f"ledger: {'OK' if ok else 'FAILED'} — {detail}",
        f"records: {len(records)}",
        f"LIVE: {live.outcome} (blocked={live.blocked})",
        "",
        "What Chronos considered / rejected / acted",
        f"  considered: {considered}",
        f"  rejected:   {rejected}",
        f"  allowed:    {allowed}",
        f"  acted:      {acted}  (proposed allows + fills)",
        "",
        "Reason codes:",
    ]
    if reasons:
        ranked = sorted(reasons.items(), key=lambda x: (-x[1], x[0]))
        lines.extend(f"  {code:<36} {count}" for code, count in ranked)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Data-health labels (brutal clarity):")
    if labels:
        lines.extend(f"  {lab:<20} {count}" for lab, count in sorted(labels.items()))
    else:
        lines.append("  (none)")
    lines.append(f"  summary: {data_summary}")

    lines.append("")
    lines.append(f"Risk / control state: {risk_summary}")
    lines.append("")
    lines.append("Unresolved anomalies:")
    if unique_anomalies:
        lines.extend(f"  - {a}" for a in unique_anomalies[:50])
        if len(unique_anomalies) > 50:
            lines.append(f"  ... and {len(unique_anomalies) - 50} more")
    else:
        lines.append("  (none recorded)")

    lines.append("")
    lines.append(
        "NOTE: This report is operational audit only. Research readiness may "
        "still be INSUFFICIENT_EVIDENCE — do not treat paper fills as edge proof."
    )
    lines.append(f"Canonical live outcome token: {LIVE_TRADING_BLOCKED}")

    return OperatorReview(
        ledger_ok=True,
        ledger_detail=detail,
        total_records=len(records),
        considered=considered,
        rejected=rejected,
        allowed=allowed,
        acted=acted,
        reason_histogram=dict(reasons),
        data_health_labels=dict(labels),
        anomalies=tuple(unique_anomalies),
        risk_state_summary=risk_summary,
        data_health_summary=data_summary,
        live_trading_blocked=live.blocked,
        live_outcome=live.outcome,
        lines=tuple(lines),
    )


def review_or_raise(path: Path) -> OperatorReview:
    review = build_operator_review(path)
    if not review.ledger_ok:
        raise DecisionLedgerError(review.ledger_detail)
    return review

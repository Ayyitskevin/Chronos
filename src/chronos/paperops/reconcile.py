"""Soak DB metrics ↔ decision-ledger stage reconcile (operator audit).

Two planes, one report:
- SQL soak view (intents / lifecycle / events) from the paper order database
- JSONL decision ledger (propose / submit / fill pipeline stages)

Honest mismatch flags — not forced 1:1 equality theater. Corrupt or missing
ledger fails closed for the ledger half; DB metrics still render when available.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from chronos.config.settings import Settings
from chronos.orders.live_block import LIVE_TRADING_BLOCKED, evaluate_live_trading_block
from chronos.paperops.ledger import load_and_verify
from chronos.paperops.records import DecisionRecord


@dataclass(frozen=True, slots=True)
class SoakSnapshot:
    """DB-side paper session activity (mirrors SoakReport fields)."""

    total_intents: int
    status_counts: dict[str, int]
    event_source_counts: dict[str, int]
    submission_unknown_resolutions: int
    risk_check_failures: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SoakSnapshot:
        return cls(
            total_intents=int(data.get("total_intents") or 0),
            status_counts=dict(data.get("status_counts") or {}),  # type: ignore[arg-type]
            event_source_counts=dict(data.get("event_source_counts") or {}),  # type: ignore[arg-type]
            submission_unknown_resolutions=int(data.get("submission_unknown_resolutions") or 0),
            risk_check_failures=dict(data.get("risk_check_failures") or {}),  # type: ignore[arg-type]
        )

    @classmethod
    def from_soak_report(cls, report: object) -> SoakSnapshot:
        """Build from scripts.paper_soak_report.SoakReport (duck-typed)."""

        return cls(
            total_intents=int(getattr(report, "total_intents", 0)),
            status_counts=dict(getattr(report, "status_counts", {}) or {}),
            event_source_counts=dict(getattr(report, "event_source_counts", {}) or {}),
            submission_unknown_resolutions=int(
                getattr(report, "submission_unknown_resolutions", 0) or 0
            ),
            risk_check_failures=dict(getattr(report, "risk_check_failures", {}) or {}),
        )


@dataclass(frozen=True, slots=True)
class LedgerStageSummary:
    chain_ok: bool
    chain_detail: str
    total_records: int
    stage_counts: dict[str, int]
    outcome_counts: dict[str, int]
    reason_counts: dict[str, int]
    path: str

    @property
    def propose_count(self) -> int:
        return int(self.stage_counts.get("propose", 0))

    @property
    def submit_count(self) -> int:
        return int(self.stage_counts.get("submit", 0))

    @property
    def fill_count(self) -> int:
        return int(self.stage_counts.get("fill", 0))


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Unified operator audit of soak DB + decision ledger."""

    ok: bool
    soak: SoakSnapshot
    ledger: LedgerStageSummary
    flags: tuple[str, ...]
    live_trading_blocked: bool
    live_outcome: str
    lines: tuple[str, ...]

    def render(self) -> str:
        return "\n".join(self.lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "live_trading_blocked": self.live_trading_blocked,
            "live_outcome": self.live_outcome,
            "flags": list(self.flags),
            "soak": {
                "total_intents": self.soak.total_intents,
                "status_counts": dict(self.soak.status_counts),
                "event_source_counts": dict(self.soak.event_source_counts),
                "submission_unknown_resolutions": self.soak.submission_unknown_resolutions,
                "risk_check_failures": dict(self.soak.risk_check_failures),
            },
            "ledger": {
                "path": self.ledger.path,
                "chain_ok": self.ledger.chain_ok,
                "chain_detail": self.ledger.chain_detail,
                "total_records": self.ledger.total_records,
                "stage_counts": dict(self.ledger.stage_counts),
                "outcome_counts": dict(self.ledger.outcome_counts),
                "reason_counts": dict(self.ledger.reason_counts),
            },
        }


def summarize_ledger_stages(path: Path) -> LedgerStageSummary:
    """Tally pipeline_stage / outcomes / reasons from a decision ledger."""

    if not path.exists():
        return LedgerStageSummary(
            chain_ok=False,
            chain_detail="decision ledger file does not exist",
            total_records=0,
            stage_counts={},
            outcome_counts={},
            reason_counts={},
            path=str(path),
        )
    ok, detail, records = load_and_verify(path)
    if not ok:
        return LedgerStageSummary(
            chain_ok=False,
            chain_detail=detail,
            total_records=0,
            stage_counts={},
            outcome_counts={},
            reason_counts={},
            path=str(path),
        )
    stages: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for record in records:
        stage = _pipeline_stage(record)
        if stage:
            stages[stage] += 1
        outcomes[record.outcome] += 1
        reasons[record.reason_code] += 1
    return LedgerStageSummary(
        chain_ok=True,
        chain_detail=detail,
        total_records=len(records),
        stage_counts=dict(stages),
        outcome_counts=dict(outcomes),
        reason_counts=dict(reasons),
        path=str(path),
    )


def _pipeline_stage(record: DecisionRecord) -> str | None:
    payload = record.payload
    raw = payload.get("pipeline_stage")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    # Fallback: kind mapping for non-pipeline pure paperops rows
    kind = record.kind
    if kind in {"proposed_order", "risk_decision", "rejection", "data_health", "control_refusal"}:
        return "propose"
    if kind == "paper_fill":
        return "fill"
    if kind == "state_transition":
        return "submit"
    return None


def reconcile_soak_and_ledger(
    *,
    soak: SoakSnapshot,
    ledger_path: Path,
    settings: Settings | None = None,
) -> ReconcileReport:
    """Compare soak DB snapshot with decision-ledger stage tallies."""

    settings = settings or Settings()
    live = evaluate_live_trading_block(settings)
    ledger = summarize_ledger_stages(ledger_path)
    flags: list[str] = []

    if not ledger.chain_ok:
        if not Path(ledger.path).exists():
            flags.append(f"LEDGER_MISSING: {ledger.chain_detail}")
        else:
            flags.append(f"LEDGER_CORRUPT_OR_INCOMPLETE: {ledger.chain_detail}")

    # Incompleteness when DB shows activity but ledger has no propose audit.
    if soak.total_intents > 0 and ledger.chain_ok and ledger.propose_count == 0:
        flags.append(
            f"LEDGER_MISSING_PROPOSE_AUDIT: db has {soak.total_intents} intent(s) "
            "but ledger has 0 propose-stage records"
        )

    db_fills = int(soak.status_counts.get("FILLED", 0)) + int(
        soak.status_counts.get("PARTIALLY_FILLED", 0)
    )
    if db_fills > 0 and ledger.chain_ok and ledger.fill_count == 0:
        flags.append(
            f"LEDGER_MISSING_FILL_AUDIT: db has {db_fills} FILLED/PARTIALLY_FILLED "
            "status row(s) but ledger has 0 fill-stage records"
        )

    if ledger.chain_ok and ledger.fill_count > 0 and db_fills == 0:
        flags.append(
            f"DB_MISSING_FILL_STATUS: ledger has {ledger.fill_count} fill-stage "
            "record(s) but db has 0 FILLED/PARTIALLY_FILLED intents"
        )

    # Soft delta: more proposes than intents is suspicious (should not happen).
    if ledger.chain_ok and ledger.propose_count > soak.total_intents and soak.total_intents >= 0:
        flags.append(
            f"PROPOSE_EXCEEDS_INTENTS: ledger propose={ledger.propose_count} "
            f"> db intents={soak.total_intents}"
        )

    # Submit activity vs DB terminal/submitted statuses (informational mismatch).
    db_submitted_ish = sum(
        int(soak.status_counts.get(s, 0))
        for s in (
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "SUBMISSION_UNKNOWN",
        )
    )
    if (
        ledger.chain_ok
        and ledger.submit_count > 0
        and db_submitted_ish == 0
        and soak.total_intents > 0
    ):
        flags.append(
            f"DB_MISSING_SUBMIT_STATUS: ledger has {ledger.submit_count} submit-stage "
            "record(s) but db shows no submitted/terminal lifecycle statuses"
        )

    ok = ledger.chain_ok and not any(
        f.startswith("LEDGER_CORRUPT")
        or f.startswith("LEDGER_MISSING:")
        or f.startswith("LEDGER_MISSING_PROPOSE")
        or f.startswith("LEDGER_MISSING_FILL")
        for f in flags
    )
    # Soft flags (DB_MISSING_*, PROPOSE_EXCEEDS) still set ok=False for operator attention
    if flags:
        ok = False

    lines = _render_lines(soak, ledger, flags, live.blocked, live.outcome, ok)
    return ReconcileReport(
        ok=ok,
        soak=soak,
        ledger=ledger,
        flags=tuple(flags),
        live_trading_blocked=live.blocked,
        live_outcome=live.outcome,
        lines=tuple(lines),
    )


def _render_lines(
    soak: SoakSnapshot,
    ledger: LedgerStageSummary,
    flags: list[str],
    live_blocked: bool,
    live_outcome: str,
    ok: bool,
) -> list[str]:
    lines = [
        "Chronos paper soak ↔ decision-ledger audit",
        "==========================================",
        f"overall: {'OK' if ok else 'ATTENTION'}  |  LIVE: {live_outcome} (blocked={live_blocked})",
        "",
        "DB soak (order database)",
        f"  order intents: {soak.total_intents}",
        "  lifecycle status:",
    ]
    if soak.status_counts:
        lines.extend(
            f"    {status:<22} {count}" for status, count in sorted(soak.status_counts.items())
        )
    else:
        lines.append("    (none)")
    lines.append("  order events by source:")
    if soak.event_source_counts:
        lines.extend(
            f"    {source:<22} {count}"
            for source, count in sorted(soak.event_source_counts.items())
        )
    else:
        lines.append("    (none)")
    lines.append(f"  SUBMISSION_UNKNOWN resolutions: {soak.submission_unknown_resolutions}")
    lines.append("  risk-check FAIL/UNKNOWN:")
    if soak.risk_check_failures:
        lines.extend(
            f"    {name:<32} {count}" for name, count in sorted(soak.risk_check_failures.items())
        )
    else:
        lines.append("    (none)")

    lines.append("")
    lines.append(f"Decision ledger ({ledger.path})")
    lines.append(f"  chain: {'OK' if ledger.chain_ok else 'FAILED'} — {ledger.chain_detail}")
    lines.append(f"  records: {ledger.total_records}")
    lines.append("  pipeline stages:")
    if ledger.stage_counts:
        lines.extend(
            f"    {stage:<22} {count}" for stage, count in sorted(ledger.stage_counts.items())
        )
    else:
        lines.append("    (none)")
    lines.append(
        f"  propose={ledger.propose_count}  submit={ledger.submit_count}  fill={ledger.fill_count}"
    )

    lines.append("")
    lines.append("Reconcile flags:")
    if flags:
        lines.extend(f"  - {flag}" for flag in flags)
    else:
        lines.append("  (none — views are consistent within honest rules)")

    lines.append("")
    lines.append(
        "NOTE: DB soak and decision ledger are different planes; flags call out "
        "missing audit or status halves, not forced equality. Research may still "
        "be INSUFFICIENT_EVIDENCE — this report is operational, not edge proof."
    )
    lines.append(f"Canonical live outcome token: {LIVE_TRADING_BLOCKED}")
    return lines

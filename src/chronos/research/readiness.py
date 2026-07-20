"""Research readiness gate — paper vs live evidence contracts.

Turns campaign / walk-forward results into an auditable readiness assessment
without enabling trading. Live capital is always reported as blocked from the
research plane; paper readiness requires explicit PASS evidence that the
current Chronos strategies have not produced (INSUFFICIENT_EVIDENCE remains
first-class and valid).

Pure research-plane module: no broker or order imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chronos.research.campaign import CampaignReport
from chronos.research.manifest import ResearchRunManifest, manifest_from_campaign
from chronos.research.walkforward import WalkForwardVerdict

# Canonical outcome token mirrored for operator docs and tests. Research never
# clears this; only a future live-trading review process (owner + ADR-0009)
# can consider lifting live blocks outside this module.
LIVE_TRADING_BLOCKED = "LIVE TRADING BLOCKED"


class ReadinessPlane(StrEnum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE_REVIEW = "live_review"


class PaperReadiness(StrEnum):
    """Whether evidence supports *starting* supervised paper trading."""

    NOT_READY = "not_ready"
    READY_FOR_PAPER = "ready_for_paper"


class LiveReviewReadiness(StrEnum):
    """Whether evidence supports *scheduling* a future live-trading review.

    This never authorizes live orders. LIVE_TRADING_BLOCKED remains in force
    until a separate owner-mediated process clears ADR-0009 + go-live gates.
    """

    NOT_ELIGIBLE = "not_eligible"
    ELIGIBLE_FOR_REVIEW = "eligible_for_review"


@dataclass(frozen=True, slots=True)
class ReadinessAssessment:
    """Structured answer for operators: what the evidence supports today."""

    live_trading_blocked: bool
    live_outcome: str
    overall_verdict: str
    paper: PaperReadiness
    live_review: LiveReviewReadiness
    paper_blockers: tuple[str, ...]
    live_review_blockers: tuple[str, ...]
    evidence_required_for_paper: tuple[str, ...]
    evidence_required_for_live_review: tuple[str, ...]
    manifest: ResearchRunManifest

    def to_dict(self) -> dict[str, object]:
        return {
            "live_trading_blocked": self.live_trading_blocked,
            "live_outcome": self.live_outcome,
            "overall_verdict": self.overall_verdict,
            "paper": self.paper.value,
            "live_review": self.live_review.value,
            "paper_blockers": list(self.paper_blockers),
            "live_review_blockers": list(self.live_review_blockers),
            "evidence_required_for_paper": list(self.evidence_required_for_paper),
            "evidence_required_for_live_review": list(self.evidence_required_for_live_review),
            "manifest": self.manifest.to_dict(),
            "manifest_fingerprint": self.manifest.fingerprint(),
        }


# Frozen evidence contracts — operator documentation pins these lists.
PAPER_EVIDENCE_REQUIRED: tuple[str, ...] = (
    "At least one strategy x symbol cell with walk-forward verdict PASS "
    "(Sharpe bootstrap CI strictly > 0, deflated Sharpe >= 0.95, OOS trades >= min_trades)",
    "Campaign stage_end strictly before the sealed holdout wall (2022-01-01); "
    "holdout never consumed by research automation",
    "Deterministic research-run manifest with code_commit, policy_hash, and "
    "holdout-free data_hashes for every cell that ran",
    "Research risk policy (config/risk.research.yaml) used only for simulation - "
    "never as a paper or live transmission policy",
    "Zero candidates selected under frozen selection criteria "
    "(research/selection_manifest.json) is a valid NOT_READY outcome - do not "
    "relax criteria after seeing results",
    "Owner has reviewed docs/STRATEGY_SELECTION.md and docs/RESEARCH_REPORT.md",
)

LIVE_REVIEW_EVIDENCE_REQUIRED: tuple[str, ...] = (
    "All paper-trading evidence requirements above are met and paper soak has "
    "completed with documented outcomes (scripts/paper_soak_report.py)",
    "Re-validation on a trusted, uniformly-adjusted historical feed (preferably "
    "IBKR) with a fresh, untouched holdout window reserved",
    "Shadow gate exercised on the production decision path with NO_ORDERS "
    "capability (chronos.service in SHADOW); no silent submission",
    "Paper transmission path verified under ADR-0007 mode lock with "
    "ALLOW_LIVE_TRADING=false; live conjunction still refused",
    "Independent adversarial review of live gate stack, kill switch, arming, "
    "and single-transmit-site invariants still green",
    "Owner-signed go-live checklist (docs/GO_LIVE_CHECKLIST.md Gates 4-5) - "
    "this module never signs it",
    f"Runtime remains {LIVE_TRADING_BLOCKED} until ADR-0009 conjunction + "
    "ten-gate live stack + human authorization all pass outside research",
)


def assess_campaign_readiness(report: CampaignReport) -> ReadinessAssessment:
    """Assess paper/live-review readiness from a campaign without enabling either.

    Live trading is always blocked here. INSUFFICIENT_EVIDENCE and FAIL overall
    verdicts yield NOT_READY for paper — they are not engineered into PASS.
    """

    manifest = manifest_from_campaign(report)
    overall = manifest.overall_verdict

    paper_blockers: list[str] = []
    live_blockers: list[str] = [
        f"{LIVE_TRADING_BLOCKED}: research plane cannot authorize live capital",
    ]

    pass_cells = manifest.verdict_counts.get(WalkForwardVerdict.PASS.value, 0)
    if pass_cells < 1:
        paper_blockers.append(
            f"no PASS cells (overall_verdict={overall!r}); "
            "INSUFFICIENT_EVIDENCE/FAIL are valid outcomes, not defects"
        )
    elif overall != WalkForwardVerdict.PASS.value:
        paper_blockers.append(
            f"overall verdict {overall!r} does not support paper promotion "
            "(require unanimous PASS across recorded cells)"
        )
    if manifest.errored_count > 0:
        paper_blockers.append(f"{manifest.errored_count} campaign cell(s) errored")

    paper = (
        PaperReadiness.READY_FOR_PAPER
        if not paper_blockers and pass_cells >= 1 and overall == WalkForwardVerdict.PASS.value
        else PaperReadiness.NOT_READY
    )

    # Live review requires paper readiness first — research alone never unlocks it.
    if paper is not PaperReadiness.READY_FOR_PAPER:
        live_blockers.append("paper readiness not met; live review is not eligible")
    live_blockers.append(
        "owner-mediated go-live gates (docs/GO_LIVE_CHECKLIST.md) not cleared by automation"
    )

    live_review = LiveReviewReadiness.NOT_ELIGIBLE  # never auto-eligible from research alone

    return ReadinessAssessment(
        live_trading_blocked=True,
        live_outcome=LIVE_TRADING_BLOCKED,
        overall_verdict=overall,
        paper=paper,
        live_review=live_review,
        paper_blockers=tuple(dict.fromkeys(paper_blockers)),  # stable unique
        live_review_blockers=tuple(dict.fromkeys(live_blockers)),
        evidence_required_for_paper=PAPER_EVIDENCE_REQUIRED,
        evidence_required_for_live_review=LIVE_REVIEW_EVIDENCE_REQUIRED,
        manifest=manifest,
    )

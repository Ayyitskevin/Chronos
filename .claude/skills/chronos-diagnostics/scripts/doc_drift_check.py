#!/usr/bin/env python3
"""Chronos documentation staleness detector — READ-ONLY diagnostic.

Greps (pure Python, no external tools) for the known-stale claims recorded in
the 2026-08-02 contradiction ledger (see the chronos-docs-map skill). Each
rule states the file, the pattern, why the claim is stale, and what "fixed"
would look like. Per rule the verdict is:

    PRESENT      the stale claim is still in the file, uncorrected (a finding)
    CORRECTED    the original wording is still present but every occurrence sits
                 next to a dated in-place correction — this repo's house style
                 keeps history visible, so a repaired document still matches the
                 pattern (counted as fixed; added 2026-08-02)
    ABSENT       the pattern no longer matches (text removed or rewritten)
    FILE-MISSING the document itself is gone (rule needs maintenance)

A rule may set `annotation_is_not_a_fix=True` to refuse the CORRECTED verdict:
where the real resolution is an OWNER decision an agent may not take (flipping
an ADR status line, for example), writing a note about the problem is not
fixing it, and the rule stays PRESENT until the text itself changes.

Also prints the live pytest --collect-only count when a project venv exists
(read-only invocation: -p no:cacheprovider + PYTHONDONTWRITEBYTECODE=1),
compared with the current dated evidence parsed from docs/TEST_RESULTS.md.

Rules are DATA (the RULES list below): future sessions extend the ledger by
appending a Rule, not by editing logic. Patterns are matched against the whole
file text (re.MULTILINE), so multi-line stale phrases work.

STRICTLY READ-ONLY: files are opened for reading only; nothing in the repo is
created, modified, or deleted; no network access.

Exit codes: 0 = no rule is PRESENT (every rule ABSENT or CORRECTED), 1 = at
least one PRESENT or FILE-MISSING, 2 = could not run.

Usage:
    python3 .claude/skills/chronos-diagnostics/scripts/doc_drift_check.py
Optional env: CHRONOS_REPO_ROOT, CHRONOS_DIAG_VENV_PYTHON (see state_inventory.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from validation_snapshot import read_validation_snapshot


@dataclass(frozen=True)
class Rule:
    """One known-stale claim: where it lives, how to spot it, what fixed means."""

    rule_id: str
    file: str  # path relative to repo root
    pattern: str  # regex, matched against whole file text with re.MULTILINE
    meaning: str  # why this text is stale/misleading (as of 2026-08-02)
    fixed_when: str  # what the repaired document looks like
    # Set True when a nearby dated note must NOT count as a repair.
    #
    # Added 2026-08-02 after the first correction pass. This repository corrects
    # documents IN PLACE — strikethrough plus a dated "Corrected"/"Amended" note,
    # so history stays visible (chronos-docs-map house style). A plain substring
    # check therefore keeps matching the ORIGINAL wording forever and reports a
    # correctly-repaired document as still stale. check_rule() now looks for a
    # dated correction marker near each match and returns CORRECTED instead.
    #
    # A few rules must resist that: where the real resolution is an OWNER
    # decision (e.g. flipping an ADR status line, which an agent may not do),
    # annotating the problem is not fixing it. Those set this flag and stay
    # PRESENT until the underlying text actually changes.
    annotation_is_not_a_fix: bool = False


# ---------------------------------------------------------------------------
# THE RULES LEDGER — extend by appending; keep ids stable; date new entries.
# Sources: contradiction ledger in chronos-docs-map (2026-08-02).
# ---------------------------------------------------------------------------
RULES: tuple[Rule, ...] = (
    # -- "wired into nothing" class: autonomy stack described as M1-era ------
    Rule(
        "ARCH-AUTONOMY-M1",
        "docs/ARCHITECTURE.md",
        r"wired into nothing",
        "Autonomy paragraph frozen at Milestone 1; the whole stack is built and "
        "wired through M7.5/ADR-0017 (README 'Current status').",
        "Paragraph rewritten to the current autonomy posture, or a dated "
        "staleness banner added scoping it to M1 history.",
    ),
    Rule(
        "HANDOFF-AUTONOMY-M1",
        "HANDOFF.md",
        r"wired into nothing",
        "HANDOFF body still claims M1 'contracts only, wired into nothing' "
        "beside its own M11 updates — internally contradictory.",
        "Body updated or the banner re-scoped to name which sections are stale.",
    ),
    # -- Test-count staleness ------------------------------------------------
    Rule(
        "TESTRESULTS-STALE-COUNT",
        "docs/TEST_RESULTS.md",
        r"1901 passed",
        "The section originally labeled 'current' reports the historical M2a "
        "count (1901); use the explicitly current dated summary instead.",
        "The 'current' summary re-run and updated, or the section relabeled "
        "'historical — superseded' like its older sibling table.",
    ),
    # -- The ~USD 3,000 capital premise (live owner decision: ~USD 110) ------
    Rule(
        "ASSUMPTIONS-A10-3K",
        "ASSUMPTIONS.md",
        r"A-10[^\n]*\n?[^\n]*USD 3,000",
        "A-10 still assumes a ~USD 3,000 cash account; the verified snapshot "
        "is ~USD 110 (VISION_COMPLETION_PLAN.md section 2). Capital question "
        "is a LIVE, unresolved owner decision — never quietly assume either.",
        "A-10 amended in place (the file already does this for A-12) to state "
        "the ~USD 110 reality and reference the open owner decision.",
    ),
    Rule(
        "RISKREG-R10-3K",
        "RISK_REGISTER.md",
        r"USD 3k account economics",
        "R-10 accepts USD 3k economics; at ~USD 110 the economics are ~27x "
        "worse — stale in the conservative direction but still stale.",
        "R-10 restated against the ~USD 110 snapshot with a dated note.",
    ),
    Rule(
        "GOLIVE-3K",
        "docs/GO_LIVE_CHECKLIST.md",
        r"USD 3,000 cash account",
        "Gate 4 frames the owner decision around a ~USD 3,000 account.",
        "Framing updated to the ~USD 110 reality / open capital decision.",
    ),
    Rule(
        "HANDOFF-3K",
        "HANDOFF.md",
        r"USD 3,000 cash account",
        "Owner-action list still poses the automated-trading question for a ~USD 3,000 account.",
        "Owner action restated against the ~USD 110 snapshot.",
    ),
    Rule(
        "ADR0008-3K",
        "docs/adr/ADR-0008-executable-candidate-scope.md",
        r"USD 3,000",
        "ADR-0008's candidate scope is premised on ~USD 3,000. ADRs are "
        "historical records — the fix is an amendment note, not a rewrite.",
        "A dated amendment note referencing the ~USD 110 revision and the "
        "open owner capital decision.",
    ),
    # -- IBKR-integration staleness ------------------------------------------
    Rule(
        "IBKRINT-ONLY-PATH",
        "docs/IBKR_INTEGRATION.md",
        r"ONLY code path",
        "Claims the quarantined platform paper adapter is 'The ONLY code path "
        "... that can hand an equity order to IBKR' — false since M5-M7: the "
        "chronos.orders plane holds the one reachable transmit site.",
        "Claim corrected (or struck through with a dated correction like "
        "GO_LIVE_CHECKLIST's) naming the chronos.orders submission boundary.",
    ),
    Rule(
        "IBKRRUN-NO-SERVICE",
        "docs/IBKR_RUNBOOK.md",
        r"no long-running shadow/paper service loop",
        "Says no service loop exists; python -m chronos.service (M2) exists and is tested.",
        "Sentence removed or corrected to reference chronos.service.",
    ),
    # -- SECURITY.md's two stale claims --------------------------------------
    Rule(
        "SECURITY-LIVE-RAISE",
        "docs/SECURITY.md",
        r"makes settings validation raise",
        "Says ALLOW_LIVE_TRADING=true makes settings validation raise; since "
        "Milestone 7 the flag is honored under the full ADR-0009 conjunction.",
        "Claim corrected to describe the ADR-0009 live conjunction "
        "(DEPLOYMENT.md:78 already carries the 2026-07-25 correction).",
    ),
    Rule(
        "SECURITY-NO-AUTH",
        "docs/SECURITY.md",
        r"Neither system implements remote access",
        "Says no remote access/authentication exists; the FastAPI backend "
        "listens on loopback 8765 with token auth + terminal cookie sessions. "
        "Reality is STRONGER than the doc, but the doc is wrong.",
        "Section rewritten to describe the backend token + terminal-session model.",
    ),
    # -- DEPLOYMENT.md denies the service entry point ------------------------
    Rule(
        "DEPLOY-SERVICE-DENIAL",
        "docs/DEPLOYMENT.md",
        r"no such entry point exists",
        "'FUTURE WORK — no such entry point exists' about "
        "python -m chronos.service, which exists and is tested "
        "(tests/platform_unit/test_service.py).",
        "Section updated to document the real chronos.service entry point.",
    ),
    # -- Mandate-replaces-arming trio (code: every LIVE submit needs an arm) --
    Rule(
        "MANDATE-ARMING-RUNBOOK",
        "docs/live_trading_runbook.md",
        r"replaces gates 7",
        "Claims an active AutonomyMandate replaces gates 7+8. Code disagrees: "
        "submission.py:441 unconditionally requires a current arm; grep for "
        "'mandate' in src/chronos/orders/ returns nothing. Open Phase-1 "
        "defect (VISION_COMPLETION_PLAN.md finding 4).",
        "Either the reviewed authority model lands in code (new ADR + owner "
        "decision) and prose stays, or the prose is corrected to match code.",
    ),
    Rule(
        "MANDATE-ARMING-GAMEPLAN",
        "docs/AI_QUANT_GAME_PLAN.md",
        r"only gate autonomy replaces",
        "Same contradiction, third variant of the story (banner-protected "
        "historical doc, but the claim is load-bearing prose).",
        "Same resolution as MANDATE-ARMING-RUNBOOK, mirrored here.",
    ),
    Rule(
        "MANDATE-ARMING-WHEELPLAN",
        "docs/LIVE_WHEEL_GAME_PLAN.md",
        r"mandate replaces per-order\s+confirmation and session arming",
        "Same contradiction (the phrase wraps across lines in this file).",
        "Same resolution as MANDATE-ARMING-RUNBOOK, mirrored here.",
    ),
    Rule(
        "ADR17-NO-RITUAL",
        "docs/adr/ADR-0017-owner-directed-maximal-autonomy.md",
        r"sufficient to trade; there is no per-boot ritual",
        "ADR-0017 says a running backend + valid mandate suffices to trade; "
        "false for LIVE while gate 7 (session arming, TTL <= 120 min process "
        "memory) stands.",
        "Amendment note on the ADR, or the reviewed authority model lands.",
    ),
    # -- ADR status lines never flipped --------------------------------------
    Rule(
        "ADR0012-PROPOSED",
        "docs/adr/ADR-0012-options-forward-capture.md",
        r"^Status: proposed",
        "Status still 'proposed' while DECISIONS D-14 records the decision and "
        "chronos.histdata options is shipped.",
        "Status line flipped to accepted/implemented with a date.",
    ),
    Rule(
        "ADR0014-PROPOSED",
        "docs/adr/ADR-0014-walkforward-and-statistics.md",
        r"^Status: proposed",
        "Status 'proposed (design-review pending)' while "
        "src/chronos/research/walkforward.py and stats.py exist and run. "
        "REFINED 2026-08-02: unlike ADR-0012 (accepted via DECISIONS.md D-14), this "
        "ADR has NO DECISIONS.md row, so its acceptance is recorded nowhere and "
        "'design-review pending' may be accurate rather than stale. A dated status "
        "note was added to the ADR on 2026-08-02; the status itself is unresolved.",
        "OWNER DECISION, not an edit: either record acceptance in DECISIONS.md and "
        "flip the status line, or run the pending design review. An agent must not "
        "mark an ADR accepted (AGENTS.md precedence, tier 3).",
        annotation_is_not_a_fix=True,
    ),
    Rule(
        "ADR0015-PROPOSED",
        "docs/adr/ADR-0015-revalidation-campaign.md",
        r"^Status: proposed",
        "Status 'proposed (design-review pending)' while "
        "src/chronos/research/campaign.py exists and 'research campaign' runs. "
        "REFINED 2026-08-02: no DECISIONS.md row exists for this ADR either, so "
        "acceptance is unrecorded. A dated status note was added on 2026-08-02.",
        "OWNER DECISION, not an edit: record acceptance in DECISIONS.md and flip the "
        "status line, or run the pending design review.",
        annotation_is_not_a_fix=True,
    ),
    # -- Ledger #1: the two runbooks named only the deterministic platform's --
    # -- halt. Corrected 2026-08-02; these rules guard against regression.  ---
    Rule(
        "INCIDENT-HALT-ONLY",
        "docs/INCIDENT_RESPONSE.md",
        r"1\. \*\*Halt\. Always safe, never harmful",
        "Immediate actions opened with the deterministic platform's halt as step 1 "
        "and never mentioned the live order plane's kill switch, so an operator "
        "following it engaged the stop that does not cover the plane able to place "
        "an order. Corrected 2026-08-02: the live kill switch is now step 1 and "
        "mandate revocation step 3.",
        "Step 1 stops the live plane (POST /live/kill); the platform halt is a "
        "separate, explicitly-scoped step. Regression = this pattern reappearing.",
    ),
    Rule(
        "BACKUP-RESTORE-GUARANTEE",
        "docs/BACKUP_AND_RECOVERY.md",
        r"the code guarantees it",
        "The design premise claimed restore can never auto-resume trading and that "
        "the code guarantees it. True for the deterministic platform; false for the "
        "chronos.orders live plane, where a MISSING kill-switch file reads DISENGAGED "
        "and a valid AUTONOMY_MANDATE_FILE auto-activates on boot. Corrected in place "
        "2026-08-02; data/live_kill_switch.json and the mandate file were also added "
        "to the backup table, which had omitted both.",
        "The guarantee is scoped to the deterministic platform and the live-plane "
        "exceptions are stated, OR open finding 3 lands (recovery boots kill-engaged, "
        "read-only, unreconciled — an owner-reviewed code change, not a doc edit).",
    ),
)


def find_repo_root() -> Path | None:
    override = os.environ.get("CHRONOS_REPO_ROOT")
    if override:
        root = Path(override)
        return root if (root / "AGENTS.md").is_file() else None
    root = Path(__file__).resolve().parents[4]
    return root if (root / "AGENTS.md").is_file() else None


def line_of_offset(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, offset) + 1


# A dated in-place correction, in any of the spellings this repo actually uses:
# "Corrected 2026-08-02", "*(Amended 2026-07-25.)*", "Amendment note, 2026-08-02",
# "Status correction, 2026-08-02", "Relabeled 2026-08-02", "restated 2026-08-02".
_CORRECTION_MARKER = re.compile(
    r"(?:Corrected|Amended|Amendment note|Status correction|Status note|Relabeled|[Rr]estated)"
    r"[^\n]{0,20}?\d{4}-\d{2}-\d{2}"
)

# How far from the stale wording a correction note may sit and still be read as
# annotating it. Generous enough for a struck-through block plus its replacement
# paragraph; tight enough that an unrelated correction elsewhere in a long file
# does not launder a different stale claim.
_CORRECTION_WINDOW_LINES = 25


def _corrected_near(text: str, matches: list[re.Match[str]]) -> bool:
    """True when EVERY match sits within the correction window of a dated note.

    Every match, not any: a file may repeat a stale claim in several places and
    fixing one of them is not fixing the rule.
    """
    lines = text.splitlines()
    marker_lines = {index for index, line in enumerate(lines) if _CORRECTION_MARKER.search(line)}
    if not marker_lines:
        return False
    for match in matches:
        match_line = line_of_offset(text, match.start()) - 1  # line_of_offset is 1-based
        if not any(abs(marker - match_line) <= _CORRECTION_WINDOW_LINES for marker in marker_lines):
            return False
    return True


def check_rule(root: Path, rule: Rule) -> tuple[str, str]:
    """Evaluate one rule. Returns (verdict, detail-line)."""
    path = root / rule.file
    if not path.is_file():
        return "FILE-MISSING", f"{rule.file} does not exist — retire or repoint this rule"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return "FILE-MISSING", f"{rule.file} unreadable: {error}"
    matches = list(re.finditer(rule.pattern, text, re.MULTILINE))
    if not matches:
        return "ABSENT", "pattern no longer matches — claim fixed/removed since 2026-08-02"
    lines = ", ".join(str(line_of_offset(text, m.start())) for m in matches[:5])
    first = text[matches[0].start() : matches[0].end()].replace("\n", " ")
    if not rule.annotation_is_not_a_fix and _corrected_near(text, matches):
        return (
            "CORRECTED",
            f"line(s) {lines}: original wording retained under a dated in-place "
            f"correction (house style — history stays visible)",
        )
    return "PRESENT", f"line(s) {lines}: ...{first[:70]}..."


def live_collection_count(root: Path) -> None:
    print("\n== Live test-collection cross-check ==")
    snapshot = read_validation_snapshot(root / "docs" / "TEST_RESULTS.md")
    if snapshot is None:
        print("[WARN] docs/TEST_RESULTS.md has no coherent current validation summary")
    candidate = os.environ.get("CHRONOS_DIAG_VENV_PYTHON") or str(root / ".venv" / "bin" / "python")
    if not Path(candidate).is_file():
        print(f"[SKIP] no venv python at {candidate}")
        if snapshot is not None:
            print(f"       latest dated evidence: {snapshot.describe()}")
        return
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = "src"
    try:
        proc = subprocess.run(
            [candidate, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
        if match:
            count = int(match.group(1))
            comparison = f"; documented {snapshot.describe()}" if snapshot is not None else ""
            print(f"[INFO] live pytest --collect-only: {count} tests{comparison}")
            if snapshot is not None and count < snapshot.collected:
                print(f"[WARN] live collection is below the documented {snapshot.collected} tests")
        else:
            print(f"[WARN] could not parse pytest output (rc={proc.returncode})")
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[WARN] pytest collection failed: {error}")


def main() -> int:
    root = find_repo_root()
    if root is None:
        print("[CRIT] cannot locate Chronos repo root (no AGENTS.md); set CHRONOS_REPO_ROOT")
        return 2
    print("CHRONOS DOC-DRIFT CHECK (read-only) — repo:", root)
    print(f"{len(RULES)} rules from the 2026-08-02 contradiction ledger\n")

    present = missing = absent = corrected = 0
    for rule in RULES:
        verdict, info = check_rule(root, rule)
        if verdict == "PRESENT":
            present += 1
        elif verdict == "FILE-MISSING":
            missing += 1
        elif verdict == "CORRECTED":
            corrected += 1
        else:
            absent += 1
        print(f"[{verdict:>12}] {rule.rule_id}  ({rule.file})")
        print(f"               {info}")
        if verdict == "PRESENT":
            print(f"               why stale: {rule.meaning}")
            print(f"               fixed when: {rule.fixed_when}")

    live_collection_count(root)

    print(
        f"\n== SUMMARY: {present} PRESENT (still stale), {corrected} CORRECTED "
        f"(fixed in place, original wording retained), {absent} ABSENT (text removed), "
        f"{missing} FILE-MISSING =="
    )
    print("PRESENT entries are documentation findings — route fixes via the")
    print("chronos-docs-map skill (doc authority + house style), under")
    print("chronos-change-control rules. Do NOT bulk-edit docs from this output.")
    return 1 if (present or missing) else 0


if __name__ == "__main__":
    sys.exit(main())

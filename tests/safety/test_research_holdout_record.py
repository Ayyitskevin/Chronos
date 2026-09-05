"""``research_all.json`` is the sole record of a burned holdout, and must not be regenerated.

The file holds 170 runs. The current harness produces 165: `scripts/run_research.py --stage all`
deliberately excludes the final stage, so that the reserved window cannot be consumed as a side
effect of a routine re-run. That guard landed in the M5 adversarial review (`--stage all` had
already computed and committed final-window results, burning QQQ's one-shot holdout —
`docs/INDEPENDENT_REVIEW_M5.md` finding 1, HIGH) **twenty-four minutes after** the results file was
committed. So the file is a faithful artifact of the harness that wrote it, and is irreproducible
by design: reproducing it would mean burning a holdout that has already been burned once.

That makes those five runs irreplaceable. They exist in no other file — there is no
`research_final.json`, and `research_all.json` is the only file in `research/results/` containing a
``"final"`` tag — and `docs/RESEARCH_REPORT.md` discloses them on the explicit ground that "hiding
or deleting them would compound the error".

Nothing here stops a regeneration; a test cannot. What it does is make one fail *loudly, saying
why*, instead of surfacing indirectly as a puzzling assertion in the SKB compiler suite
(`tests/unit/test_skb_compiler.py` requires a ``final`` partition to be attached) — which is not
where someone regenerating a results file would look.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from chronos.research.campaign import FINAL_START

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "research/results/research_all.json"

#: The committed shape. These are not thresholds to tune — they describe one frozen artifact, and
#: a change to either means the file was rewritten by something.
_TOTAL_RUNS = 170
_FINAL_RUNS = 5
_HOLDOUT_SYMBOL = "QQQ"

_DO_NOT_REGENERATE = (
    "research/results/research_all.json MUST NOT be regenerated. It is the only copy of the five "
    "final-window runs that burned QQQ's one-shot holdout (M5 review finding 1; disclosed in "
    "docs/RESEARCH_REPORT.md). Today's `--stage all` excludes the final stage by design, so "
    "re-running it produces 165 runs and silently destroys that record. If you are here because "
    "you regenerated the file: restore it from git rather than re-running anything, and never run "
    "`--stage final` to recreate the missing five — the reserved window is spent."
)


def _runs() -> list[dict[str, object]]:
    return list(json.loads(RESULTS.read_text(encoding="utf-8"))["runs"])


def test_the_committed_results_still_hold_every_run_including_the_holdout() -> None:
    """170 runs, of which exactly five are the final-window record."""

    runs = _runs()
    final = [run for run in runs if run["tag"] == "final"]

    assert len(runs) == _TOTAL_RUNS, (
        f"research_all.json holds {len(runs)} runs, expected {_TOTAL_RUNS}. {_DO_NOT_REGENERATE}"
    )
    assert len(final) == _FINAL_RUNS, (
        f"research_all.json holds {len(final)} final-tagged runs, expected {_FINAL_RUNS}. "
        f"{_DO_NOT_REGENERATE}"
    )


def test_the_holdout_runs_are_qqq_inside_the_reserved_window() -> None:
    """The five are the reserved window's runs, not five arbitrary rows carrying the tag."""

    final = [run for run in _runs() if run["tag"] == "final"]

    symbols = {run["symbol"] for run in final}
    assert symbols == {_HOLDOUT_SYMBOL}, (
        f"final-tagged runs cover {sorted(symbols)}, expected only {_HOLDOUT_SYMBOL} — the sole "
        f"symbol whose data reaches past the reserved wall. {_DO_NOT_REGENERATE}"
    )
    for run in final:
        start = date.fromisoformat(str(run["start"]))
        assert start >= FINAL_START, (
            f"a final-tagged run starts {start}, before the reserved wall {FINAL_START}; the tag "
            f"and the window disagree. {_DO_NOT_REGENERATE}"
        )

"""The research runner's symbol contract, pinned where it is load-bearing.

``scripts/run_research.py`` emits its ``runs`` array in ``SYMBOLS`` iteration order, and
``research/results/research_dev.json`` and ``research_val.json`` are committed with that
order baked in. So the tuple carries two independent facts:

* its **membership** must equal the campaign universe — checked by the script itself, at
  import, against ``chronos.research.data_intake.CAMPAIGN_SYMBOLS``;
* its **order** must not change — which no set comparison can see, and which this file
  pins.

Both are needed. A membership check alone stays silent while a reorder rewrites every row
of the committed results: measured, by swapping SPY and QQQ and regenerating, which
produced a modified ``research_dev.json`` with the guard saying nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "run_research.py"

#: The order the committed research artifacts encode. Deliberately written out here and
#: NOT derived from ``CAMPAIGN_SYMBOLS``: the constant's order differs (QQQ before SPY),
#: and adopting it would rewrite ``research_dev.json`` / ``research_val.json`` rather than
#: describe them. This literal is the artifacts' contract, not the universe's.
_COMMITTED_RESULT_ORDER = ("SPY", "QQQ", "IWM", "DIA", "GLD", "TLT")


def _load_run_research() -> ModuleType:
    """Import the script for its module-level contract only; ``main()`` is not called."""

    spec = importlib.util.spec_from_file_location("_run_research_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_research_symbol_order_matches_the_committed_results() -> None:
    """A reorder is not cosmetic: it rewrites every row of the committed research results.

    ``runs`` is appended per symbol in ``SYMBOLS`` order, so changing that order changes
    the serialized array and dirties ``research/results/*.json`` — with the script's own
    membership check silent, because a set cannot see order. If this fails, either restore
    the order or regenerate and review the artifacts deliberately.
    """

    module = _load_run_research()
    assert module.SYMBOLS == _COMMITTED_RESULT_ORDER, (
        "scripts/run_research.py SYMBOLS order changed; `runs` is emitted in this order "
        "and research/results/research_dev.json and research_val.json are committed with "
        "it, so a reorder rewrites those artifacts. Restore the order, or regenerate and "
        "review the result diff on purpose."
    )


def test_the_research_symbol_membership_is_still_guarded_by_the_script() -> None:
    """The script's own import-time check is the membership half; this pins that it exists.

    Kept separate from the order pin so a future edit cannot delete one while the other
    keeps the file green.
    """

    from chronos.research.data_intake import CAMPAIGN_SYMBOLS

    module = _load_run_research()
    assert set(module.SYMBOLS) == set(CAMPAIGN_SYMBOLS)
    assert "CAMPAIGN_SYMBOLS" in _SCRIPT.read_text(encoding="utf-8")

"""Source-measured per-script properties (issue #181).

Two properties the strategy-selection work needs — how many positions a script
can hold at once, and whether it fixes a timeframe — are stated by **no corpus
input**. The registry and the forensic findings do not carry them, and they are
not derivable from the existing categoricals, so they cannot be compiled the way
:mod:`chronos.skb.disposition` compiles disposition. They were obtained the only
honest way available: by reading the Pine source line by line.

That makes this module a table of measurements rather than a rule, which is a
weaker kind of fact — so every claim here carries the file and line it was read
from, and ``tests/unit/test_skb_source_properties.py`` opens each Pine file and
asserts the cited line still contains the cited token. A citation that stops
matching its source fails the suite instead of quietly becoming decoration.

Only the five standalone strategies under ``research/pine`` were read. The other
37 scripts keep the null/unknown default: the point of the exercise is that
"unknown" means unmeasured, and filling it by inference would destroy exactly the
distinction the fields exist to draw.

Two findings are worth stating because they invert the obvious reading:

1. ``pyramiding`` is not the position count. Four of the five declare
   ``pyramiding = 3``, which reads as three concurrent positions. It is not:
   every one of the five gates entry on ``strategy.position_size == 0``, so a
   position opens only from flat, and the three ``strategy.entry`` calls fire in
   one block on one bar — three legs of a single scaled entry. All five are
   one-position strategies, and ``forensic_flags.pyramiding_gt_0`` would have
   disqualified four of them.
2. No script pins a timeframe. All five read ``timeframe.period`` at every
   ``request.security`` call, so they inherit the chart. The ``"1d"`` in the two
   canonical specs is a porting decision, not an inherited property.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronos.skb.schema import TimeframeBinding


@dataclass(frozen=True)
class LineCitation:
    """One checked claim: ``line`` of the script's Pine file contains ``contains``."""

    line: int
    contains: str


@dataclass(frozen=True)
class MeasuredProperties:
    """The measured properties of one Pine script, with the evidence for each."""

    filename: str
    max_concurrent_positions: int
    timeframe_binding: TimeframeBinding
    position_citations: tuple[LineCitation, ...]
    timeframe_citations: tuple[LineCitation, ...]
    note: str

    def render_citation(self) -> str:
        """The provenance string stored on the entry.

        Rendered from the citation tuples rather than written out by hand, so the
        stored prose cannot drift away from the lines the test actually checks.
        """

        def cites(items: tuple[LineCitation, ...]) -> str:
            return ", ".join(f"{self.filename}:{c.line} ({c.contains})" for c in items)

        return (
            f"max_concurrent_positions={self.max_concurrent_positions} from "
            f"{cites(self.position_citations)}; "
            f"timeframe_binding={self.timeframe_binding.value} from "
            f"{cites(self.timeframe_citations)}. {self.note}"
        )


_ONE_POSITION_NOTE = (
    "pyramiding is leg-splitting, not concurrency: entry is gated on flat, and the "
    "same-bar strategy.entry calls place legs of one scaled entry."
)
_CHART_TF_NOTE = "The script pins no timeframe; it evaluates on the chart's, at bar close."


def _props(
    filename: str,
    *,
    positions: tuple[LineCitation, ...],
    timeframe: tuple[LineCitation, ...],
    note: str,
) -> MeasuredProperties:
    return MeasuredProperties(
        filename=filename,
        max_concurrent_positions=1,
        timeframe_binding=TimeframeBinding.CHART_TF,
        position_citations=positions,
        timeframe_citations=timeframe,
        note=note,
    )


#: catalog_number -> measured properties. Read at Chronos commit 21e0769.
SOURCE_PROPERTIES: dict[str, MeasuredProperties] = {
    "00": _props(
        "00_five_tool_confluence_aio.pine",
        positions=(
            LineCitation(1469, "strategy.position_size == 0"),
            LineCitation(32, "pyramiding = 3"),
        ),
        timeframe=(
            LineCitation(743, "timeframe.period"),
            LineCitation(33, "calc_on_every_tick = false"),
        ),
        note=f"{_ONE_POSITION_NOTE} {_CHART_TF_NOTE}",
    ),
    "01": _props(
        "01_markov_regime_bull_plus.pine",
        positions=(
            LineCitation(1086, "strategy.position_size == 0"),
            LineCitation(52, "pyramiding = 3"),
        ),
        timeframe=(
            LineCitation(712, "timeframe.period"),
            LineCitation(53, "calc_on_every_tick = false"),
        ),
        note=f"{_ONE_POSITION_NOTE} {_CHART_TF_NOTE}",
    ),
    "02": _props(
        "02_markov_regime_bear_plus.pine",
        positions=(
            LineCitation(1076, "strategy.position_size == 0"),
            LineCitation(55, "pyramiding = 3"),
        ),
        timeframe=(
            LineCitation(715, "timeframe.period"),
            LineCitation(56, "calc_on_every_tick = false"),
        ),
        note=f"{_ONE_POSITION_NOTE} {_CHART_TF_NOTE}",
    ),
    "0A": _props(
        "0A_confluence_swing_strategy_archived.pine",
        positions=(
            LineCitation(356, "strategy.position_size == 0"),
            LineCitation(78, "pyramiding = 0"),
        ),
        timeframe=(
            LineCitation(259, "timeframe.period"),
            LineCitation(79, "calc_on_every_tick = false"),
        ),
        note=(
            "The only pyramiding = 0 script: one strategy.entry, scaled OUT in three "
            f"strategy.exit tranches. {_CHART_TF_NOTE}"
        ),
    ),
    "16": _props(
        "16_pullback_to_value_playbook.pine",
        positions=(
            LineCitation(310, "strategy.position_size == 0"),
            LineCitation(311, "strategy.position_size == 0"),
            LineCitation(32, "pyramiding = 3"),
        ),
        timeframe=(
            LineCitation(192, "timeframe.period"),
            LineCitation(33, "calc_on_every_tick = false"),
        ),
        note=f"Both the long and short gate require flat. {_ONE_POSITION_NOTE} {_CHART_TF_NOTE}",
    ),
}

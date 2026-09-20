"""docs/VISION_COMPLETION_PLAN.md §7 partial-delivery paragraphs cannot drift from the tree.

VCP-1: the 2026-09-20 paragraph records what landed through #247, what sits at the gate,
and what remains open, and it must keep the honest exit sentence. Each pin reads the plan
at test time — nothing is hard-coded — so a paragraph edit, a date bump without the exit
sentence, or a deleted header fails here, not in an owner session.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"

HEADER_2026_09_20 = "**Partial delivery (updated 2026-09-20, through #247 / ADR-0059):**"
EXIT_SENTENCE = "This does not satisfy the Phase 2 exit."


def _paragraph_after(page: str, header: str) -> str:
    """The text under ``header`` up to the next same-or-higher heading."""
    start = page.index(header) + len(header)
    rest = page[start:]
    next_heading = rest.find("\n### ")
    next_section = rest.find("\n## ")
    ends = [i for i in (next_heading, next_section) if i != -1]
    return rest[: min(ends)] if ends else rest


def test_partial_delivery_2026_09_20_header_present() -> None:
    assert HEADER_2026_09_20 in PLAN.read_text(encoding="utf-8")


def test_partial_delivery_2026_09_20_keeps_the_phase_2_exit_sentence() -> None:
    paragraph = _paragraph_after(PLAN.read_text(encoding="utf-8"), HEADER_2026_09_20)
    assert EXIT_SENTENCE in paragraph

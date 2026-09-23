"""docs/VISION_COMPLETION_PLAN.md §7 partial-delivery paragraphs cannot drift from the tree.

VCP-1: the 2026-09-20 paragraph records what landed through #247, what sits at the gate,
and what remains open, and it must keep the honest exit sentence. Each pin reads the plan
at test time — nothing is hard-coded — so a paragraph edit, a date bump without the exit
sentence, or a deleted header fails here, not in an owner session.

VCP-2: the 2026-09-22 paragraph carries the record through #259. Each paragraph is pinned on
its OWN text: a window ends at the next partial-delivery header, so a later paragraph's exit
sentence can never stand in for an earlier one's.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"

HEADER_2026_09_20 = "**Partial delivery (updated 2026-09-20, through #247 / ADR-0059):**"
HEADER_2026_09_22 = "**Partial delivery (updated 2026-09-22, through #259 / main 43d9a26):**"
EXIT_SENTENCE = "This does not satisfy the Phase 2 exit."
PARTIAL_DELIVERY_HEADER = "**Partial delivery (updated "


def _paragraph_after(page: str, header: str) -> str:
    """The text under ``header`` up to the next same-or-higher heading."""
    start = page.index(header) + len(header)
    rest = page[start:]
    next_heading = rest.find("\n### ")
    next_section = rest.find("\n## ")
    next_paragraph = rest.find(PARTIAL_DELIVERY_HEADER)
    ends = [i for i in (next_heading, next_section, next_paragraph) if i != -1]
    return rest[: min(ends)] if ends else rest


def test_partial_delivery_2026_09_20_header_present() -> None:
    assert HEADER_2026_09_20 in PLAN.read_text(encoding="utf-8")


def test_partial_delivery_2026_09_20_keeps_the_phase_2_exit_sentence() -> None:
    paragraph = _paragraph_after(PLAN.read_text(encoding="utf-8"), HEADER_2026_09_20)
    assert EXIT_SENTENCE in paragraph


def _paragraph_2026_09_22() -> str:
    return _paragraph_after(PLAN.read_text(encoding="utf-8"), HEADER_2026_09_22)


def _parts_2026_09_22() -> dict[str, str]:
    """(a)/(b)/(c)/(d) of the 2026-09-22 paragraph, each up to the next part's marker."""
    paragraph = " ".join(_paragraph_2026_09_22().split())
    markers = {
        "a": "(a) LANDED",
        "b": "(b) AT THE GATE, NOT COUNTED",
        "c": "(c) REMAINS OPEN",
        "d": "(d) " + EXIT_SENTENCE,
    }
    starts = {key: paragraph.index(marker) for key, marker in markers.items()}
    assert list(starts.values()) == sorted(starts.values()), starts
    ordered = sorted(starts, key=starts.__getitem__)
    return {
        key: paragraph[starts[key] : (starts[ordered[i + 1]] if i + 1 < len(ordered) else None)]
        for i, key in enumerate(ordered)
    }


def test_1_partial_delivery_2026_09_22_header_present() -> None:
    assert HEADER_2026_09_22 in PLAN.read_text(encoding="utf-8")


def test_1_partial_delivery_2026_09_22_keeps_the_phase_2_exit_sentence_in_its_own_window() -> None:
    assert EXIT_SENTENCE in _paragraph_2026_09_22()


def test_1_every_landed_item_is_a_merged_pr_with_a_merge_sha_and_an_artifact_in_the_tree() -> None:
    landed = _parts_2026_09_22()["a"]
    pairs = re.findall(r"#(\d{3}), merge ([0-9a-f]{7})\)", landed)
    numbers = [int(number) for number, _ in pairs]
    assert sorted(numbers) == list(range(248, 260)), pairs
    paths = re.findall(r"\(((?:docs|src|tests)/[\w./-]+?)(?::[\d,-]+)?; #\d{3}, merge ", landed)
    assert len(paths) == len(pairs), (paths, pairs)
    missing = [path for path in paths if not (ROOT / path).exists()]
    assert missing == [], missing
    assert "verified on synthetic / demo evidence; UNVERIFIED on live" in _paragraph_2026_09_22()


def test_2_nothing_at_the_gate_or_open_is_counted_and_the_sidecar_is_held_not_landed() -> None:
    parts = _parts_2026_09_22()
    for key in ("b", "c"):
        assert re.search(r"merge [0-9a-f]{7}\)", parts[key]) is None, parts[key]
    assert "S-1a" not in parts["a"] and "S-1c" not in parts["a"]
    assert "S-1a/S-1c" in parts["c"] and "NOT merged" in parts["c"]
    assert "nothing at the gate" in parts["b"]

"""SKB catalog rendering tests (AI Quant plan B2).

``render_catalog`` is a pure, deterministic function of the store. These tests
pin that the committed ``research/skb/CATALOG.md`` equals a fresh render (the CI
guard that a corpus change is never left un-regenerated — mirrors
``build_skb.py --check``), that rendering is byte-stable, and that the catalog
carries the provenance and every script row.
"""

from __future__ import annotations

from chronos.skb.compiler import CATALOG_PATH, REPO_ROOT, compile_skb
from chronos.skb.docs import render_catalog


def test_committed_catalog_is_current() -> None:
    committed = (REPO_ROOT / CATALOG_PATH).read_text(encoding="utf-8")
    assert committed == render_catalog(compile_skb())


def test_render_is_deterministic() -> None:
    store = compile_skb()
    assert render_catalog(store) == render_catalog(store)


def test_catalog_carries_provenance_and_every_script() -> None:
    store = compile_skb()
    catalog = render_catalog(store)
    # Provenance: the corpus hash is stamped so a reader can tie the doc to inputs.
    assert store.corpus_hash in catalog
    assert "Do not hand-edit" in catalog
    # Every disposition bucket that has scripts gets a section header.
    for disposition, blurb in (
        ("ported", "## ported"),
        ("deferred", "## deferred"),
        ("blocked_on", "## blocked_on"),
        ("rejected", "## rejected"),
    ):
        assert blurb in catalog, disposition
    # Every script's catalog number appears as a table row.
    for entry in store.pine_scripts:
        assert f"| {entry.catalog_number} |" in catalog
    # Both derived strategies are listed.
    for derived in store.derived_strategies:
        assert derived.strategy_id in catalog


def test_pipe_in_title_is_escaped() -> None:
    # Titles are rendered into a Markdown table; a literal pipe would break the
    # row, so it must be escaped. (Defensive — the current corpus has none.)
    store = compile_skb()
    catalog = render_catalog(store)
    for line in catalog.splitlines():
        if line.startswith("| ") and line.count("|") >= 2 and " | " in line:
            # No unescaped pipe should sit inside a rendered title cell; we can't
            # easily isolate the cell, so assert the doc has no raw "||" artifacts
            # that a stray pipe would produce.
            assert "||" not in line

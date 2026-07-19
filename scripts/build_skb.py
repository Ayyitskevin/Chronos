#!/usr/bin/env python
"""Regenerate the Strategy Knowledge Base store (AI Quant plan B1/B2).

Deterministic: both artifacts are a pure function of the corpus inputs (no
timestamps generated), so re-running on an unchanged corpus produces
byte-identical files. A drift in any input changes ``corpus_hash`` /
``source_hashes``, and the compile fails closed if the registry↔findings join is
not complete or any categorical falls outside the controlled vocabulary.

Two artifacts are written from the one compiled store:

- ``research/skb/skb.json``   — the validated machine store
- ``research/skb/CATALOG.md`` — the human-readable catalog (rendered from it)

    python scripts/build_skb.py            # write both artifacts
    python scripts/build_skb.py --check    # verify both committed artifacts are current
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chronos.skb.compiler import CATALOG_PATH, REPO_ROOT, STORE_PATH, compile_skb, store_to_json
from chronos.skb.docs import render_catalog
from chronos.skb.schema import SKBStore


def _check_current(path: Path, rel: Path, rendered: str) -> str | None:
    """Return an error message if ``path`` is missing or stale, else ``None``."""

    if not path.exists():
        return f"SKB artifact missing: {rel}"
    if path.read_text(encoding="utf-8") != rendered:
        return f"SKB artifact is stale: {rel} differs from a fresh compile"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the SKB store and catalog.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail (non-zero) if either committed artifact differs from a fresh compile",
    )
    args = parser.parse_args()

    store: SKBStore = compile_skb(REPO_ROOT)
    store_json = store_to_json(store)
    catalog_md = render_catalog(store)
    store_path = REPO_ROOT / STORE_PATH
    catalog_path = REPO_ROOT / CATALOG_PATH

    if args.check:
        errors = [
            error
            for error in (
                _check_current(store_path, STORE_PATH, store_json),
                _check_current(catalog_path, CATALOG_PATH, catalog_md),
            )
            if error is not None
        ]
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            print("run `python scripts/build_skb.py`", file=sys.stderr)
            return 1
        print(
            f"SKB is current ({store.pine_script_count} scripts, corpus {store.corpus_hash[:12]})"
        )
        return 0

    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(store_json, encoding="utf-8")
    catalog_path.write_text(catalog_md, encoding="utf-8")
    print(
        f"Wrote {STORE_PATH} and {CATALOG_PATH}: {store.pine_script_count} Pine scripts, "
        f"{store.derived_strategy_count} derived strategies, corpus {store.corpus_hash[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

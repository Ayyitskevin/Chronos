#!/usr/bin/env python
"""Regenerate the Strategy Knowledge Base store (AI Quant plan B1).

Deterministic: the store is a pure function of the corpus inputs (no timestamps
generated), so re-running on an unchanged corpus produces a byte-identical file.
A drift in any input changes ``corpus_hash`` / ``source_hashes``, and the
compile fails closed if the registry↔findings join is not complete or any
categorical falls outside the controlled vocabulary.

    python scripts/build_skb.py            # write research/skb/skb.json
    python scripts/build_skb.py --check    # verify the committed store is current
"""

from __future__ import annotations

import argparse
import sys

from chronos.skb.compiler import REPO_ROOT, STORE_PATH, compile_skb, store_to_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the SKB store.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail (non-zero) if the committed store is not byte-identical to a fresh compile",
    )
    args = parser.parse_args()

    store = compile_skb(REPO_ROOT)
    rendered = store_to_json(store)
    out_path = REPO_ROOT / STORE_PATH

    if args.check:
        if not out_path.exists():
            print(f"SKB store missing: {STORE_PATH}", file=sys.stderr)
            return 1
        if out_path.read_text(encoding="utf-8") != rendered:
            print(
                f"SKB store is stale: {STORE_PATH} differs from a fresh compile; "
                "run `python scripts/build_skb.py`",
                file=sys.stderr,
            )
            return 1
        print(
            f"SKB store is current ({store.pine_script_count} scripts, "
            f"corpus {store.corpus_hash[:12]})"
        )
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(
        f"Wrote {STORE_PATH}: {store.pine_script_count} Pine scripts, "
        f"{store.derived_strategy_count} derived strategies, corpus {store.corpus_hash[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

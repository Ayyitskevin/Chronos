"""Locked tradable book for the SHADOW learning loop.

GLD, IWM, and QQQ may be proposed. SPY is a companion and must not appear on
a proposal. QQQM is not in the book. Autonomy duplicates this identity so the
worker can fail closed without importing research. Changing the set is a new
digest.
"""

from __future__ import annotations

import hashlib
import json

BOOK_SCHEMA = "chronos-five-tool-tradable-book-v1"
TRADABLE_SYMBOLS = ("GLD", "IWM", "QQQ")
COMPANION_ONLY_SYMBOLS = ("RSP", "SPY", "VIX", "VIX3M")
RESEARCH_PROXY: dict[str, str] = {}
DEFAULT_HOLD_SYMBOL = "QQQ"


def is_tradable(symbol: str) -> bool:
    return symbol.strip().upper() in TRADABLE_SYMBOLS


def book_payload() -> dict[str, object]:
    return {
        "companion_only": list(COMPANION_ONLY_SYMBOLS),
        "research_proxy": dict(RESEARCH_PROXY),
        "schema_version": BOOK_SCHEMA,
        "tradable_symbols": list(TRADABLE_SYMBOLS),
    }


def book_digest() -> str:
    encoded = json.dumps(book_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

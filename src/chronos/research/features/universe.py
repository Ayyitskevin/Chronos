"""Locked tradable book versus pairing companions.

The book is GLD, IWM, and QQQ. Changing it is a new identity. SPY is the
benchmark and is never traded. QQQ is both a tradable and the Nasdaq-100
breadth series. There is no QQQM proxy. This module does not download or
certify bytes.
"""

from __future__ import annotations

import hashlib
import json

BOOK_SCHEMA = "chronos-five-tool-tradable-book-v1"
TRADABLE_SYMBOLS = ("GLD", "IWM", "QQQ")
COMPANION_ONLY_SYMBOLS = ("RSP", "SPY", "VIX", "VIX3M")
RESEARCH_PROXY: dict[str, str] = {}
OPTIONAL_INTERNALS = ("ADD", "TICK", "VOLD")


def is_tradable(symbol: str) -> bool:
    return symbol.strip().upper() in TRADABLE_SYMBOLS


def research_series_for(symbol: str) -> str:
    """Each tradable is its own research series. Companions map to themselves."""

    normalized = symbol.strip().upper()
    return RESEARCH_PROXY.get(normalized, normalized)


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

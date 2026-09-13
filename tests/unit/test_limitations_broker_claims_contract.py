"""Three docs/limitations.md broker bullets say exactly what the source proves (D-3 sweep).

At 3ab88ec the "Broker integration" and "Order pipeline" sections carried three claims wider
or narrower than the code: that the official `ibapi` package is "not installable" (it is
absent from the locked dependency set, imported lazily, owner-installable, and the adapter
fails fast without it); that IBKR order what-if beyond the demo path is "not fully wired"
(outside demo the runtime injects the official adapter's connection into
`OrderPreviewService`, and `preview_order` sends `whatIf=True` — it is wired, and has never
been exercised against a real gateway); and that canonicalizing the limit price "would alter
every existing hash" (only hashes whose persisted price spelling is non-canonical would move:
`format(Decimal("3.20"), "f")` changes under `.normalize()`, `Decimal("3.2")` does not).

These pins read the source and the document and hold them together; the Decimal claim is
executed rather than asserted, so the narrowed sentence is machine-true at every run.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
RUNTIME = ROOT / "src" / "chronos" / "runtime.py"
OFFICIAL_ADAPTER = ROOT / "src" / "chronos" / "broker" / "official_ibkr.py"
INTENT = ROOT / "src" / "chronos" / "orders" / "intent.py"
LOCKS = sorted(ROOT.glob("requirements*.lock")) + sorted(ROOT.glob("requirements*.txt"))

IBAPI_BULLET = "- **The official `ibapi` package"
LIMIT_PRICE_BULLET = "- The confirmation summary hash and idempotency key"
WHAT_IF_BULLET = "- Covered-call scenarios remain blocked"


def _bullet(anchor: str) -> str:
    """One markdown bullet, whitespace-collapsed, from its anchor to the next bullet/heading."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    start = text.index("\n" + anchor) + 1
    stop = re.search(r"^(- |## )", text[start + 1 :], re.M)
    assert stop is not None, anchor
    return " ".join(text[start : start + 1 + stop.start()].split())


# ------------------------------------------------------------ (a) what-if IS wired, unexercised


def test_the_official_what_if_path_is_wired_in_the_source() -> None:
    assert "OrderPreviewService(" in RUNTIME.read_text(encoding="utf-8")
    assert "order.whatIf = " in OFFICIAL_ADAPTER.read_text(encoding="utf-8")


def test_the_what_if_bullet_says_wired_and_never_exercised_against_a_gateway() -> None:
    bullet = _bullet(WHAT_IF_BULLET)
    assert "wired" in bullet
    assert "never been exercised against a real gateway" in bullet
    assert "`OrderPreviewService`" in bullet and "`whatIf=True`" in bullet


# ---------------------------------------------------- (b) the two over-claims are gone


def test_the_over_claims_are_gone() -> None:
    assert "not fully wired" not in _bullet(WHAT_IF_BULLET)
    assert "not installable" not in _bullet(IBAPI_BULLET)


# ------------------------------------------------ ibapi: absent from locks, lazy, fails fast


def test_ibapi_is_absent_from_every_lock_and_loaded_lazily() -> None:
    assert LOCKS, "no requirements locks found"
    for lock in LOCKS:
        assert "ibapi" not in lock.read_text(encoding="utf-8").lower(), lock.name
    adapter = OFFICIAL_ADAPTER.read_text(encoding="utf-8")
    assert "def _load_ibapi(" in adapter
    assert "_INSTALL_GUIDANCE" in adapter
    bullet = _bullet(IBAPI_BULLET)
    for phrase in ("absent from the locked", "lazily", "fails fast", "owner installs"):
        assert phrase in bullet, phrase
    assert "does not enumerate a live-acceptance list" in bullet


# ----------------------------------------------- (c) the Decimal claim, executed


def test_normalizing_a_limit_price_moves_only_non_canonical_spellings() -> None:
    assert format(Decimal("3.2"), "f") == format(Decimal("3.2").normalize(), "f")
    assert format(Decimal("3.20"), "f") != format(Decimal("3.20").normalize(), "f")
    assert format(Decimal("100"), "f") == format(Decimal("100").normalize(), "f")


def test_the_limit_price_bullet_cites_the_canonicalization_the_source_has() -> None:
    intent = INTENT.read_text(encoding="utf-8")
    assert "canonical_quantity(self.quantity)" in intent
    assert 'format(self.limit_price, "f")' in intent
    bullet = _bullet(LIMIT_PRICE_BULLET)
    assert "whose persisted price spelling is non-canonical" in bullet
    assert "would alter every existing hash" not in bullet
    assert "`canonical_quantity`" in bullet and '`format(limit_price, "f")`' in bullet

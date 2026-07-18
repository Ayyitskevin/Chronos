"""Product-family eligibility (options / stocks / crypto), deny-by-default.

One pipeline, three families (docs/LIVE_WHEEL_GAME_PLAN.md §6b). A symbol is
tradable only when its family is enabled AND the symbol is on that family's
allowlist. Crypto is disabled entirely while ``CRYPTO_ALLOWLIST`` is empty.
Every denial carries its reasons; unknown symbols resolve to no family and
are denied — never guessed from the ticker shape beyond the explicit
allowlists (the broker's qualified contract details remain the authority on
what a symbol actually is at order time).
"""

from __future__ import annotations

from chronos.config.settings import Settings
from chronos.domain.enums import ProductFamily
from chronos.domain.models import ChronosModel


class ProductEligibility(ChronosModel):
    symbol: str
    family: ProductFamily | None
    allowed: bool
    reasons: tuple[str, ...]


def classify_symbol(settings: Settings, symbol: str) -> ProductFamily | None:
    """Map a symbol to its configured family (allowlists only, no inference).

    Equity/ETF symbols on SYMBOL_ALLOWLIST serve both the STOCK family and,
    as underlyings, the OPTION family; CRYPTO_ALLOWLIST is separate. A symbol
    on both lists is a configuration error surfaced by ``evaluate``.
    """

    normalized = symbol.strip().upper()
    on_equity = normalized in settings.symbol_allowlist
    on_crypto = normalized in settings.crypto_allowlist
    if on_equity and not on_crypto:
        return ProductFamily.STOCK
    if on_crypto and not on_equity:
        return ProductFamily.CRYPTO
    return None


def evaluate(settings: Settings, symbol: str, family: ProductFamily) -> ProductEligibility:
    """Deny-by-default eligibility for one (symbol, family) pair."""

    normalized = symbol.strip().upper()
    reasons: list[str] = []
    on_equity = normalized in settings.symbol_allowlist
    on_crypto = normalized in settings.crypto_allowlist

    if on_equity and on_crypto:
        reasons.append(
            f"{normalized} appears on both SYMBOL_ALLOWLIST and CRYPTO_ALLOWLIST; "
            "ambiguous configuration is refused"
        )
    if family in (ProductFamily.OPTION, ProductFamily.STOCK):
        if not on_equity:
            reasons.append(f"{normalized} is not on SYMBOL_ALLOWLIST")
    elif family is ProductFamily.CRYPTO:
        if not settings.crypto_allowlist:
            reasons.append(
                "the crypto family is disabled (CRYPTO_ALLOWLIST is empty); "
                "deny-by-default until the owner enables it"
            )
        elif not on_crypto:
            reasons.append(f"{normalized} is not on CRYPTO_ALLOWLIST")

    return ProductEligibility(
        symbol=normalized,
        family=family if not reasons else classify_symbol(settings, normalized),
        allowed=not reasons,
        reasons=tuple(reasons),
    )

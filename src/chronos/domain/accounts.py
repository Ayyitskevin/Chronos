"""IBKR account-id pattern vocabulary shared by settings and the orders plane.

IBKR paper accounts match ``D[UF]\\d{4,}`` (e.g. DU1234567); live accounts match
``U\\d{4,}`` (e.g. U1234567). The patterns are anchored full-matches on the
already-stripped id — anything else is evidence of live/paper confusion and
must fail closed at the caller.

``chronos.control.modes`` keeps its own inline paper pattern by design: that
module is the autonomous plane's hard lock (ADR-0007) and is not modified by
the live-Wheel work (ADR-0009).
"""

from __future__ import annotations

import re

LIVE_ACCOUNT_ID_PATTERN = re.compile(r"U\d{4,}")
PAPER_ACCOUNT_ID_PATTERN = re.compile(r"D[UF]\d{4,}")


def is_live_account_id(account_id: str) -> bool:
    """True only for a well-formed IBKR live account id (``U`` + digits)."""

    return LIVE_ACCOUNT_ID_PATTERN.fullmatch(account_id.strip()) is not None


def is_paper_account_id(account_id: str) -> bool:
    """True only for a well-formed IBKR paper account id (``DU``/``DF`` + digits)."""

    return PAPER_ACCOUNT_ID_PATTERN.fullmatch(account_id.strip()) is not None

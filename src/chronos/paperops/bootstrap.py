"""Runtime helpers for enabling the paper decision ledger (no orders imports).

Kept free of ``chronos.orders`` so cold paperops imports and runtime bootstrap
cannot reintroduce the paperops↔orders cycle.
"""

from __future__ import annotations

from pathlib import Path

from chronos.config.settings import Settings
from chronos.domain.enums import IBEnvironment
from chronos.paperops.ledger import DecisionLedger

DEFAULT_PAPER_DECISION_LEDGER = Path("data/paper_decision_ledger.jsonl")


def open_paper_decision_ledger(settings: Settings) -> DecisionLedger | None:
    """Open the decision ledger for paper sessions when enabled.

    - LIVE environment: always ``None`` (never auto-record on live capital path).
    - PAPER environment: open when ``enable_paper_decision_ledger`` is true.
    - Corrupt existing file: :class:`DecisionLedger` fails closed on first append
      (constructor recovers the tail; verify is separate).
    """

    if settings.ib_environment is not IBEnvironment.PAPER:
        return None
    if not settings.enable_paper_decision_ledger:
        return None
    path = settings.paper_decision_ledger_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return DecisionLedger(path)

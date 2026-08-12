"""The decision vocabulary, restated where the worker can see it (ADR-0027).

The worker imports nothing from ``chronos`` — see the package docstring for why
that is the load-bearing choice — so, like the TradingView bridge before it, it
restates the slice of the decision vocabulary it needs. The liability of a
restatement is drift, and drift is answered the same way it was for the bridge:
``tests/safety/test_model_worker_isolation.py`` pins every set below **equal**
to the real enum or frozenset in ``chronos.autonomy``, so a vocabulary change
fails the safety suite until this file is updated with it.

Nothing here decides authority. The worker restates *shape* — which strings are
valid, which combinations are incoherent — and none of admission, sizing, or
compilation. The mandate and the gateway remain the only opinion about what a
proposal may become.
"""

from __future__ import annotations

from typing import Final

#: Mirrors ``chronos.autonomy.enums.DecisionKind``.
DECISION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "HOLD",
        "OPEN",
        "INCREASE",
        "REDUCE",
        "CLOSE",
        "HEDGE",
        "ROLL",
        "CANCEL",
        "REPLACE",
    }
)

#: Mirrors ``chronos.autonomy.enums.DecisionDirection``.
DIRECTIONS: Final[frozenset[str]] = frozenset({"LONG", "SHORT", "NEUTRAL"})

#: Mirrors ``chronos.autonomy.enums.StrategyForm``. No naked-short-option
#: member exists, so the tool schema built from this set cannot ask for one.
STRATEGY_FORMS: Final[frozenset[str]] = frozenset(
    {
        "LONG_EQUITY",
        "SHORT_EQUITY",
        "CASH_SECURED_PUT",
        "COVERED_CALL",
        "LONG_CALL",
        "LONG_PUT",
        "VERTICAL_DEBIT_SPREAD",
        "VERTICAL_CREDIT_SPREAD",
        "LONG_FUTURE",
        "SHORT_FUTURE",
    }
)

#: Mirrors ``chronos.autonomy.enums.TimeHorizon``.
TIME_HORIZONS: Final[frozenset[str]] = frozenset({"INTRADAY", "SWING", "POSITION", "LONG_TERM"})

#: Mirrors ``chronos.autonomy.enums.EXPOSURE_CREATING_DECISION_KINDS``.
EXPOSURE_CREATING_KINDS: Final[frozenset[str]] = frozenset(
    {"OPEN", "INCREASE", "HEDGE", "ROLL", "REPLACE"}
)

#: Mirrors ``chronos.autonomy.enums.TARGETED_DECISION_KINDS``.
TARGETED_KINDS: Final[frozenset[str]] = frozenset(
    {"INCREASE", "REDUCE", "CLOSE", "ROLL", "CANCEL", "REPLACE"}
)

#: Mirrors ``chronos.autonomy.decision._SIZELESS_KINDS``.
SIZELESS_KINDS: Final[frozenset[str]] = frozenset({"HOLD", "CANCEL"})

#: Mirrors ``chronos.autonomy.decision._NO_ENTRY_KINDS``.
NO_ENTRY_KINDS: Final[frozenset[str]] = frozenset({"HOLD", "REDUCE", "CLOSE", "CANCEL"})

#: The only asset class this worker emits — the same reasoning as the bridge:
#: the autonomy wiring can gather instrument facts for equities; emitting a
#: class the runtime cannot price queues a guaranteed refusal.
SUPPORTED_ASSET_CLASS: Final[str] = "EQUITY"

#: Mirrors ``chronos.autonomy.decision._CHRONOS_REFERENCE_PATTERN``.
CHRONOS_REFERENCE_PATTERN: Final[str] = r"^CHR-[A-Z0-9]+-[0-9A-F]{32}$"

#: Mirrors ``chronos.autonomy.decision._SYMBOL_ALPHABET``.
SYMBOL_ALPHABET: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")

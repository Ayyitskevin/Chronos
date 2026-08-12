"""The decision vocabulary, restated where the bridge can see it (ADR-0026).

This module is a deliberate duplicate of parts of :mod:`chronos.autonomy.enums`
and the payload-coherence rules in :mod:`chronos.autonomy.decision`. The bridge
does not import those modules — see this package's ``__init__`` for why the
weaker, non-importing position was chosen — so it has to name the vocabulary it
translates into.

Duplication is a liability in exactly one way: it can drift. That liability is
answered structurally rather than by discipline.
``tests/safety/test_tradingview_bridge_isolation.py`` asserts every set below is
**equal** to the real enum or frozenset it mirrors, so adding a
``DecisionKind`` member, renaming a ``StrategyForm``, or reclassifying a kind as
exposure-creating fails the safety suite until this file is updated with it.

What is NOT restated here, on purpose: anything that decides whether a decision
may become an order. The bridge restates *shape* — which strings are valid, which
combinations are incoherent — and restates none of admission, sizing, or
compilation. A bridge that knew what a mandate permits would be a second opinion
about authority, and there is only ever one.
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

#: Mirrors ``chronos.autonomy.enums.StrategyForm``. Note what is absent and must
#: stay absent: there is no uncovered/naked short option member, so no alert can
#: ask for one (ADR-0016 §6).
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

#: Mirrors ``chronos.autonomy.enums.EXPOSURE_CREATING_DECISION_KINDS``. These
#: must cite evidence and state their invalidation conditions, so an alert that
#: carries neither cannot become one.
EXPOSURE_CREATING_KINDS: Final[frozenset[str]] = frozenset(
    {"OPEN", "INCREASE", "HEDGE", "ROLL", "REPLACE"}
)

#: Mirrors ``chronos.autonomy.enums.TARGETED_DECISION_KINDS``. These act on an
#: existing Chronos-owned order or position and must name it.
TARGETED_KINDS: Final[frozenset[str]] = frozenset(
    {"INCREASE", "REDUCE", "CLOSE", "ROLL", "CANCEL", "REPLACE"}
)

#: Mirrors ``chronos.autonomy.decision._SIZELESS_KINDS``: kinds that either do
#: nothing or act on an existing order without resizing it.
SIZELESS_KINDS: Final[frozenset[str]] = frozenset({"HOLD", "CANCEL"})

#: Mirrors ``chronos.autonomy.decision._NO_ENTRY_KINDS``: anything that only
#: removes or holds exposure has no entry to describe.
NO_ENTRY_KINDS: Final[frozenset[str]] = frozenset({"HOLD", "REDUCE", "CLOSE", "CANCEL"})

#: The only asset class this bridge will emit. Equities are what the autonomy
#: wiring can actually gather instrument facts for; options refuse at that seam
#: because chain resolution is not owned there, and futures are out of scope in
#: this release. Emitting a class the runtime cannot price would produce a
#: proposal guaranteed to be refused — an honest refusal here beats a queued one
#: there.
SUPPORTED_ASSET_CLASS: Final[str] = "EQUITY"

#: The Chronos-owned reference shape, mirroring
#: ``chronos.autonomy.decision._CHRONOS_REFERENCE_PATTERN``. An alert may name a
#: Chronos correlation reference and may never name a broker order id.
CHRONOS_REFERENCE_PATTERN: Final[str] = r"^CHR-[A-Z0-9]+-[0-9A-F]{32}$"

#: Mirrors ``chronos.autonomy.decision._SYMBOL_ALPHABET``.
SYMBOL_ALPHABET: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")

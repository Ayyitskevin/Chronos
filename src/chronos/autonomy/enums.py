"""Vocabulary for controlled autonomous model authority (ADR-0016).

This vocabulary is deliberately **separate** from the deterministic strategy
platform's :mod:`chronos.control.modes`. ADR-0007's unconditional denial of
``TradingMode.CANARY_LIVE`` / ``TradingMode.LIVE`` in that plane is unchanged by
ADR-0016 and stays unchanged: an autonomy mode describes *who may originate a
decision* under an owner-authored mandate, never what the deterministic kernel
permits. Nothing here widens a gate.

Two members are **recognized but refused**, following the ADR-0007 precedent
that refusing in code beats refusing in configuration:

- :attr:`TradableAssetClass.FUTURE_OPTION` — named so a mandate can be written
  about it and rejected, rather than silently accepted (ADR-0016 §6).
- there is no ``MARKET`` member of :class:`OrderForm`, and no naked-short-option
  member of :class:`StrategyForm`; both absences are asserted by tests.
"""

from __future__ import annotations

from enum import StrEnum


class AutonomyMode(StrEnum):
    """Operating mode of an :class:`~chronos.autonomy.mandate.AutonomyMandate`."""

    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    PAPER_AUTONOMOUS = "PAPER_AUTONOMOUS"
    CANARY_LIVE_AUTONOMOUS = "CANARY_LIVE_AUTONOMOUS"
    LIVE_AUTONOMOUS = "LIVE_AUTONOMOUS"


class PromotionLevel(StrEnum):
    """One rung of the per-asset-family promotion ladder (ADR-0016 §7).

    Promotion is per asset family: a stock promotion authorizes neither futures
    nor options.
    """

    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    PAPER_AUTONOMOUS = "PAPER_AUTONOMOUS"
    CANARY_LIVE_AUTONOMOUS = "CANARY_LIVE_AUTONOMOUS"
    CAPPED_LIVE_AUTONOMOUS = "CAPPED_LIVE_AUTONOMOUS"


class TradableAssetClass(StrEnum):
    """Asset classes an autonomy mandate can speak about.

    Named ``Tradable...`` to avoid any confusion with
    :class:`chronos.skb.schema.AssetClass`, which is a Pine-corpus taxonomy
    label and has no execution meaning.
    """

    EQUITY = "EQUITY"
    EQUITY_OPTION = "EQUITY_OPTION"
    INDEX_OPTION = "INDEX_OPTION"
    FUTURE = "FUTURE"
    #: Recognized vocabulary, refused by the mandate validator (ADR-0016 §6).
    FUTURE_OPTION = "FUTURE_OPTION"
    CRYPTO = "CRYPTO"


class StrategyForm(StrEnum):
    """Position structures a mandate may permit.

    There is deliberately **no uncovered/naked short option member**. A naked
    short call or put is not expressible in this vocabulary, so no mandate can
    authorize one and no decision can request one (ADR-0016 §6).
    """

    LONG_EQUITY = "LONG_EQUITY"
    SHORT_EQUITY = "SHORT_EQUITY"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    COVERED_CALL = "COVERED_CALL"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    VERTICAL_DEBIT_SPREAD = "VERTICAL_DEBIT_SPREAD"
    VERTICAL_CREDIT_SPREAD = "VERTICAL_CREDIT_SPREAD"
    LONG_FUTURE = "LONG_FUTURE"
    SHORT_FUTURE = "SHORT_FUTURE"


class OrderForm(StrEnum):
    """Order forms the deterministic compiler may select.

    There is deliberately **no MARKET member**. Market entries provide no price
    protection and stay disabled; any protective stop-market or emergency
    liquidation policy requires its own instrument-specific ADR, tests, and
    mandate permission before this enum grows (ADR-0016 §6).
    """

    LIMIT = "LIMIT"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"


class RestartBehavior(StrEnum):
    """What a process restart does to an otherwise-unexpired mandate."""

    #: Fail closed: a restart suspends the mandate until the owner reactivates.
    REQUIRE_REACTIVATION = "REQUIRE_REACTIVATION"
    #: The mandate stays effective across a restart until its expiry.
    RESUME_UNTIL_EXPIRY = "RESUME_UNTIL_EXPIRY"


class TradingSession(StrEnum):
    """Sessions in which a mandate may permit activity."""

    REGULAR = "REGULAR"
    EXTENDED = "EXTENDED"
    OVERNIGHT = "OVERNIGHT"


class DecisionKind(StrEnum):
    """What an :class:`~chronos.autonomy.decision.AITradeDecision` asks for."""

    HOLD = "HOLD"
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    HEDGE = "HEDGE"
    ROLL = "ROLL"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class DecisionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class TimeHorizon(StrEnum):
    """Intended holding horizon; advisory context for the kernel, never a gate."""

    INTRADAY = "INTRADAY"
    SWING = "SWING"
    POSITION = "POSITION"
    LONG_TERM = "LONG_TERM"


class TriggerComparator(StrEnum):
    AT_OR_ABOVE = "AT_OR_ABOVE"
    AT_OR_BELOW = "AT_OR_BELOW"


class PriceReference(StrEnum):
    """Which observed price a trigger is measured against."""

    LAST = "LAST"
    MID = "MID"
    BID = "BID"
    ASK = "ASK"
    UNDERLYING_LAST = "UNDERLYING_LAST"


#: Modes in which a decision can become a real order against real money.
LIVE_AUTONOMY_MODES: frozenset[AutonomyMode] = frozenset(
    {AutonomyMode.CANARY_LIVE_AUTONOMOUS, AutonomyMode.LIVE_AUTONOMOUS}
)

#: Modes in which no broker order may be transmitted for any reason.
NON_SUBMITTING_AUTONOMY_MODES: frozenset[AutonomyMode] = frozenset(
    {
        AutonomyMode.RESEARCH,
        AutonomyMode.BACKTEST,
        AutonomyMode.REPLAY,
        AutonomyMode.SHADOW,
    }
)

#: Modes in which a mandate must state an explicit instrument scope.
SUBMITTING_AUTONOMY_MODES: frozenset[AutonomyMode] = frozenset(
    {AutonomyMode.PAPER_AUTONOMOUS} | LIVE_AUTONOMY_MODES
)

#: Startup default. An environment variable alone may never select a live mode.
DEFAULT_AUTONOMY_MODE: AutonomyMode = AutonomyMode.SHADOW

#: The ladder, in order. Promotion is single-step and per asset family.
PROMOTION_LADDER: tuple[PromotionLevel, ...] = (
    PromotionLevel.BACKTEST,
    PromotionLevel.REPLAY,
    PromotionLevel.SHADOW,
    PromotionLevel.PAPER_AUTONOMOUS,
    PromotionLevel.CANARY_LIVE_AUTONOMOUS,
    PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
)

#: The minimum promotion rung each mode requires. A mode may never exceed the
#: rung the asset family has actually earned.
MINIMUM_PROMOTION_FOR_MODE: dict[AutonomyMode, PromotionLevel] = {
    AutonomyMode.RESEARCH: PromotionLevel.BACKTEST,
    AutonomyMode.BACKTEST: PromotionLevel.BACKTEST,
    AutonomyMode.REPLAY: PromotionLevel.REPLAY,
    AutonomyMode.SHADOW: PromotionLevel.SHADOW,
    AutonomyMode.PAPER_AUTONOMOUS: PromotionLevel.PAPER_AUTONOMOUS,
    AutonomyMode.CANARY_LIVE_AUTONOMOUS: PromotionLevel.CANARY_LIVE_AUTONOMOUS,
    AutonomyMode.LIVE_AUTONOMOUS: PromotionLevel.CAPPED_LIVE_AUTONOMOUS,
}

#: Decision kinds that reduce or remove exposure **by their kind alone**. The
#: kernel may permit these under degraded conditions where new exposure is
#: refused; the classification lives here as vocabulary, and the *enforcement*
#: is the deterministic supervisor's (M2), never this module's.
#:
#: CANCEL is deliberately **not** here. Whether a cancel reduces risk depends
#: entirely on what it cancels: cancelling a resting *opening* order removes
#: prospective exposure, but cancelling a *closing* or protective order leaves
#: an open position running and therefore *increases* net risk. A blanket
#: risk-reducing classification would let the degraded-state rule (ADR-0016 §8)
#: permit exactly the cancel that should be refused. The supervisor must
#: classify a CANCEL from the target order's own open/close effect, not from the
#: decision kind. Found by the M1 adversarial review.
RISK_REDUCING_DECISION_KINDS: frozenset[DecisionKind] = frozenset(
    {DecisionKind.HOLD, DecisionKind.REDUCE, DecisionKind.CLOSE}
)

#: Kinds whose risk direction cannot be known from the kind alone — the
#: supervisor must inspect the target before treating one as risk-reducing.
CONTEXT_DEPENDENT_DECISION_KINDS: frozenset[DecisionKind] = frozenset({DecisionKind.CANCEL})

#: Decision kinds that can create or extend exposure. ROLL and REPLACE are here
#: because each closes and re-opens: the re-open is new exposure.
EXPOSURE_CREATING_DECISION_KINDS: frozenset[DecisionKind] = frozenset(
    {
        DecisionKind.OPEN,
        DecisionKind.INCREASE,
        DecisionKind.HEDGE,
        DecisionKind.ROLL,
        DecisionKind.REPLACE,
    }
)

#: Kinds that act on an existing Chronos-owned order or position and therefore
#: must name one.
TARGETED_DECISION_KINDS: frozenset[DecisionKind] = frozenset(
    {
        DecisionKind.INCREASE,
        DecisionKind.REDUCE,
        DecisionKind.CLOSE,
        DecisionKind.ROLL,
        DecisionKind.CANCEL,
        DecisionKind.REPLACE,
    }
)


def promotion_rank(level: PromotionLevel) -> int:
    """Return the ladder index of ``level`` (higher is more authority)."""

    return PROMOTION_LADDER.index(level)

"""Compiling an admitted decision into an executable order intent (ADR-0016 §2, M4).

This is the step M2 and M3 both stopped short of, and the reason the gateway was
until now "a gate with nothing routed through it". Admission says *may this
proceed*; sizing says *how much*; compilation says **exactly what order, on
exactly which contract, at exactly what price** — and it is where the model's
description of a trade stops being words and becomes something the order plane
can execute.

Everything here is deterministic. The model contributes an intent and a
*request*; every number that reaches a venue is derived by this module from
facts the supervisor gathered.

## The capability matrix is a whitelist, and unmapped means refused

``_CAPABILITY_MATRIX`` maps ``(asset class, decision kind, strategy)`` to the
order-plane ``OrderIntent`` that expresses it. A combination that is not in the
matrix **refuses**. That is the asset-class authority matrix the directive asks
for, made executable rather than documented:

- It is a whitelist, so a new ``StrategyForm`` or ``DecisionKind`` is unusable
  until someone deliberately maps it. Adding an enum member cannot silently
  become a new tradable capability.
- It cannot express an uncovered short option or a naked short, because the
  order plane's ``OrderIntent`` has no such member and this matrix cannot invent
  one. The impossibility is structural at two layers, not a policy check.
- Futures and futures options map to nothing at all. They are refused here as
  well as by the mandate validator, so no single mistake enables them.

## Why the contract is an input, never a lookup

:func:`compile_order` takes an **already-qualified** ``Instrument``. It does not
resolve one, because resolution needs a broker, and a module that reached a
broker would be one the model could reach through. The caller resolves via
:class:`ContractResolver` — deterministic code, outside the model process — and
hands the result in. This module's job is to *check* that contract, hard:

- it is the right instrument type for the asset class,
- its symbol matches the decision's (a resolver that returned a different
  instrument is a bug, and a bug here would trade the wrong thing),
- its exchange is in the mandate's scope, and its trading class is in the
  mandate's contract families — the two ``InstrumentScope`` fields M2 disclosed
  as unenforceable precisely because no qualified contract existed to check,
- its currency is permitted,
- and the limit price conforms to its tick.

## Where the limit price comes from

**Entirely from the supervisor's quote.** The model supplies no price at all,
and there is no field through which it could: ``EntryPlan.trigger`` is a
``PriceTrigger``, which is a *condition* (comparator, reference, value) rather
than a price to pay. An earlier draft of this module read the trigger's value as
a limit and then clamped it — which would have been a semantic stretch dressed
up as a safety feature. Reading it correctly turns out to give the stronger
guarantee: the compiled price is 100% deterministic, so there is no model input
to clamp in the first place.

The trigger is still honoured, in the only direction it can safely act. It is
evaluated against the supervisor's quote and can **prevent** compilation — an
unmet entry condition refuses. A condition that cannot be evaluated (it
references a price the supervisor did not gather) also refuses, because a
condition we cannot check is not a condition we can call satisfied. So a trigger
can stop an order and can never create, widen, or reprice one.

``OrderForm.MARKET`` exists since ADR-0017 and compiles to a **protected
marketable limit**: the touch plus :data:`MARKET_PROTECTION_COLLAR`, tick-
conformed away from aggression. Every compiled intent is therefore still a
positive-price limit at the order plane — the fill behavior of a market order
on every ordinary day, with a ceiling on the catastrophic one. A literally
unbounded venue market order remains unexpressible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    DecisionKind,
    OrderForm,
    PriceReference,
    StrategyForm,
    TradableAssetClass,
    TriggerComparator,
)
from chronos.domain.enums import OrderIntent, OrderSide, ProductFamily
from chronos.domain.models import (
    CryptoContract,
    Instrument,
    OptionContract,
    UnderlyingContract,
)
from chronos.orders.intent import WheelOrderIntent, new_correlation_id, side_for_intent

#: The protection collar on an ``OrderForm.MARKET`` compilation (ADR-0017): the
#: limit is set this far through the touch. 1% fills like a market order in any
#: sane book; what it refuses to chase is a broken print. IBKR's own
#: "market with protection" products exist for the same reason.
MARKET_PROTECTION_COLLAR = Decimal("0.01")

#: ``(asset class, decision kind, strategy)`` -> the order-plane intent that
#: expresses it. A WHITELIST: anything absent is refused, so adding an enum
#: member can never silently become a tradable capability.
#:
#: Deliberately absent, and each absence is a safety property rather than an
#: omission:
#:
#: - every naked/uncovered short option shape (the order plane cannot express
#:   one, and this matrix cannot invent what it cannot name);
#: - ``TradableAssetClass.FUTURE`` and ``FUTURE_OPTION`` (no contract model, no
#:   adapter support; refused here as well as by the mandate validator);
#: - ``SHORT_EQUITY``/``SHORT_FUTURE`` (no borrow, no short capability);
#: - multi-leg spreads (constructing one leg at a time would create the
#:   temporary naked exposure ADR-0016 §6 forbids).
_CAPABILITY_MATRIX: dict[tuple[TradableAssetClass, DecisionKind, StrategyForm], OrderIntent] = {
    # --- equity: long-only, one strategy ---
    (
        TradableAssetClass.EQUITY,
        DecisionKind.OPEN,
        StrategyForm.LONG_EQUITY,
    ): OrderIntent.OPEN_LONG_STOCK,
    (
        TradableAssetClass.EQUITY,
        DecisionKind.INCREASE,
        StrategyForm.LONG_EQUITY,
    ): OrderIntent.OPEN_LONG_STOCK,
    # --- equity options: cash-secured put and covered call only ---
    (
        TradableAssetClass.EQUITY_OPTION,
        DecisionKind.OPEN,
        StrategyForm.CASH_SECURED_PUT,
    ): OrderIntent.OPEN_SHORT_PUT,
    (
        TradableAssetClass.EQUITY_OPTION,
        DecisionKind.OPEN,
        StrategyForm.COVERED_CALL,
    ): OrderIntent.OPEN_COVERED_CALL,
    # --- crypto: long-only spot. `LONG_EQUITY` is the vocabulary's "long the
    # underlying" form and the mandate's own strategy/asset-class map already
    # admits it for CRYPTO; inventing a LONG_CRYPTO member would have split one
    # concept across two names that must then be kept in agreement forever. ---
    (
        TradableAssetClass.CRYPTO,
        DecisionKind.OPEN,
        StrategyForm.LONG_EQUITY,
    ): OrderIntent.OPEN_LONG_CRYPTO,
    (
        TradableAssetClass.CRYPTO,
        DecisionKind.INCREASE,
        StrategyForm.LONG_EQUITY,
    ): OrderIntent.OPEN_LONG_CRYPTO,
}

#: Risk-reducing kinds carry no strategy (the decision contract forbids it), so
#: they map on ``(asset class, kind)`` alone.
_CLOSING_MATRIX: dict[tuple[TradableAssetClass, DecisionKind], OrderIntent] = {
    (TradableAssetClass.EQUITY, DecisionKind.CLOSE): OrderIntent.CLOSE_LONG_STOCK,
    (TradableAssetClass.EQUITY, DecisionKind.REDUCE): OrderIntent.CLOSE_LONG_STOCK,
    (TradableAssetClass.EQUITY_OPTION, DecisionKind.CLOSE): OrderIntent.CLOSE_SHORT_OPTION,
    (TradableAssetClass.EQUITY_OPTION, DecisionKind.REDUCE): OrderIntent.CLOSE_SHORT_OPTION,
    (TradableAssetClass.CRYPTO, DecisionKind.CLOSE): OrderIntent.CLOSE_LONG_CRYPTO,
    (TradableAssetClass.CRYPTO, DecisionKind.REDUCE): OrderIntent.CLOSE_LONG_CRYPTO,
}

_PRODUCT_FAMILY: dict[TradableAssetClass, ProductFamily] = {
    TradableAssetClass.EQUITY: ProductFamily.STOCK,
    TradableAssetClass.EQUITY_OPTION: ProductFamily.OPTION,
    TradableAssetClass.CRYPTO: ProductFamily.CRYPTO,
}

_EXPECTED_CONTRACT: dict[TradableAssetClass, type[Instrument]] = {
    TradableAssetClass.EQUITY: UnderlyingContract,
    TradableAssetClass.EQUITY_OPTION: OptionContract,
    TradableAssetClass.CRYPTO: CryptoContract,
}


class CompilationRefusal:
    """Refusal codes. Strings rather than an enum so a caller can log them raw."""

    NO_CAPABILITY = "NO_CAPABILITY"
    CONTRACT_TYPE_MISMATCH = "CONTRACT_TYPE_MISMATCH"
    CONTRACT_SYMBOL_MISMATCH = "CONTRACT_SYMBOL_MISMATCH"
    EXCHANGE_NOT_PERMITTED = "EXCHANGE_NOT_PERMITTED"
    FAMILY_NOT_PERMITTED = "FAMILY_NOT_PERMITTED"
    CURRENCY_NOT_PERMITTED = "CURRENCY_NOT_PERMITTED"
    NO_ORDER_FORM = "NO_ORDER_FORM"
    NO_QUOTE = "NO_QUOTE"
    PRICE_NOT_DERIVABLE = "PRICE_NOT_DERIVABLE"
    ENTRY_CONDITION_UNMET = "ENTRY_CONDITION_UNMET"
    ENTRY_CONDITION_UNEVALUABLE = "ENTRY_CONDITION_UNEVALUABLE"
    TICK_NOT_KNOWN = "TICK_NOT_KNOWN"
    QUANTITY_NOT_SIZED = "QUANTITY_NOT_SIZED"
    INTENT_REJECTED = "INTENT_REJECTED"


@dataclass(frozen=True, slots=True)
class QuoteEvidence:
    """The supervisor's own quote. Never model-supplied.

    ``bid``/``ask`` are the only inputs to the compiled limit price. A last
    trade is deliberately not accepted as a substitute: it is a historical fact,
    not a price you can transact at, and using one would let a stale print set
    what we pay.
    """

    bid: Decimal
    ask: Decimal

    @property
    def is_crossed(self) -> bool:
        """A crossed or zero book is not a book. Refuse rather than compute."""

        return self.bid <= 0 or self.ask <= 0 or self.bid > self.ask


class ContractResolver(Protocol):
    """How the supervisor obtains a qualified contract, without reaching a broker.

    Deterministic code outside the model process implements this. It is a
    Protocol rather than a concrete class for one reason that matters: the
    supervisor must be testable, and therefore *auditable*, without a broker
    anywhere near it. A resolver that returned an unqualified or wrong-symbol
    contract is caught by :func:`compile_order`, which re-checks everything it
    is handed rather than trusting its caller.
    """

    def resolve(
        self, *, asset_class: TradableAssetClass, symbol: str
    ) -> Instrument | None:  # pragma: no cover - Protocol
        ...


@dataclass(frozen=True, slots=True)
class CompilationOutcome:
    """A compiled order, or a refusal. ``intent`` is never a default."""

    intent: WheelOrderIntent | None
    refusal: str = ""
    detail: str = ""
    #: Every derivation step, in order, so a compiled price is explainable.
    steps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def compiled(self) -> bool:
        return self.intent is not None


def _refuse(refusal: str, detail: str, steps: list[str]) -> CompilationOutcome:
    return CompilationOutcome(intent=None, refusal=refusal, detail=detail, steps=tuple(steps))


def select_intent(decision: AITradeDecision) -> OrderIntent | None:
    """Map a decision onto the order plane, or ``None`` when it cannot be expressed.

    Separate from :func:`compile_order` so the capability matrix can be tested
    exhaustively over every enum combination — which is how you prove a
    whitelist is a whitelist rather than trusting that it is.
    """

    closing = _CLOSING_MATRIX.get((decision.asset_class, decision.kind))
    if closing is not None:
        return closing
    if decision.requested_strategy is None:
        return None
    return _CAPABILITY_MATRIX.get(
        (decision.asset_class, decision.kind, decision.requested_strategy)
    )


def compile_order(
    *,
    decision: AITradeDecision,
    mandate: AutonomyMandate,
    contract: Instrument | None,
    quantity: Decimal | None,
    quote: QuoteEvidence | None,
    account_id: str,
    correlation_id: str | None = None,
    intent_id: str | None = None,
) -> CompilationOutcome:
    """Turn an admitted, sized decision into an executable ``WheelOrderIntent``.

    Every argument except ``decision`` and ``mandate`` is supervisor-gathered.
    The decision contributes *what it wants*; this function decides everything
    that reaches a venue.

    Never raises. A compilation failure is data the supervisor records, exactly
    like an admission refusal — an exception here would strand a decision that
    had already passed every gate, which is the worst possible moment to lose it.
    """

    steps: list[str] = []
    try:
        return _compile(
            decision=decision,
            mandate=mandate,
            contract=contract,
            quantity=quantity,
            quote=quote,
            account_id=account_id,
            correlation_id=correlation_id,
            intent_id=intent_id,
            steps=steps,
        )
    except (InvalidOperation, ArithmeticError, ValueError) as error:
        return _refuse(
            CompilationRefusal.PRICE_NOT_DERIVABLE,
            f"compilation failed deterministically and produced no order: {error}",
            steps,
        )


def _compile(
    *,
    decision: AITradeDecision,
    mandate: AutonomyMandate,
    contract: Instrument | None,
    quantity: Decimal | None,
    quote: QuoteEvidence | None,
    account_id: str,
    correlation_id: str | None,
    intent_id: str | None,
    steps: list[str],
) -> CompilationOutcome:
    # 1. Can the order plane express this at all? Unmapped => refuse.
    order_intent = select_intent(decision)
    if order_intent is None:
        strategy = decision.requested_strategy
        named = strategy.value if strategy else "(no strategy)"
        return _refuse(
            CompilationRefusal.NO_CAPABILITY,
            f"no order-plane capability expresses {decision.kind.value} {named} on "
            f"{decision.asset_class.value}; this combination is not tradable",
            steps,
        )
    steps.append(f"capability: {order_intent.value}")

    # 2. Sizing must have produced a number. A refused size is not a zero size.
    if quantity is None or quantity <= 0:
        return _refuse(
            CompilationRefusal.QUANTITY_NOT_SIZED,
            "sizing produced no executable quantity, so there is nothing to compile",
            steps,
        )

    # 3. The qualified contract, re-checked rather than trusted.
    outcome = _check_contract(decision, mandate, contract, steps)
    if outcome is not None:
        return outcome
    assert contract is not None  # narrowed by _check_contract

    # 4. Order form. The mandate must permit one, and MARKET does not exist.
    form = _select_order_form(mandate)
    if form is None:
        return _refuse(
            CompilationRefusal.NO_ORDER_FORM,
            "the mandate permits no order form, so no order can be built",
            steps,
        )
    steps.append(f"order form: {form.value}")

    # 5. The limit price, derived from the supervisor's quote.
    if quote is None:
        return _refuse(
            CompilationRefusal.NO_QUOTE,
            "no quote evidence accompanies this decision; a limit price may never be "
            "derived from anything the model supplied",
            steps,
        )
    if quote.is_crossed:
        return _refuse(
            CompilationRefusal.NO_QUOTE,
            f"quote is crossed or empty (bid={quote.bid}, ask={quote.ask}); a crossed "
            "book is not a book",
            steps,
        )

    # 5b. An entry condition, if the decision carries one, may only PREVENT.
    outcome = _check_entry_condition(decision, quote, steps)
    if outcome is not None:
        return outcome

    side = side_for_intent(order_intent)
    priced = _derive_limit_price(quote=quote, side=side, form=form, contract=contract, steps=steps)
    if isinstance(priced, CompilationOutcome):
        return priced

    # 6. Build the intent. Its own validators are the last structural gate, and
    #    a failure there is a refusal rather than an exception escaping upward.
    family = _PRODUCT_FAMILY[decision.asset_class]
    try:
        built = WheelOrderIntent(
            intent_id=intent_id or f"AI-{decision.decision_id}",
            correlation_id=correlation_id or new_correlation_id(),
            account_id=account_id,
            product_family=family,
            intent=order_intent,
            contract=contract,
            quantity=quantity,
            limit_price=priced,
        )
    except ValueError as error:
        return _refuse(
            CompilationRefusal.INTENT_REJECTED,
            f"the order plane refused the compiled intent: {error}",
            steps,
        )
    steps.append(f"compiled {order_intent.value} {quantity} @ {priced}")
    return CompilationOutcome(intent=built, steps=tuple(steps))


def _check_contract(
    decision: AITradeDecision,
    mandate: AutonomyMandate,
    contract: Instrument | None,
    steps: list[str],
) -> CompilationOutcome | None:
    """Re-check the resolver's output. A resolver bug must not become a trade."""

    if contract is None:
        return _refuse(
            CompilationRefusal.CONTRACT_TYPE_MISMATCH,
            "no qualified contract was resolved; nothing may be compiled against a "
            "contract we could not qualify",
            steps,
        )
    expected = _EXPECTED_CONTRACT.get(decision.asset_class)
    if expected is None or not isinstance(contract, expected):
        return _refuse(
            CompilationRefusal.CONTRACT_TYPE_MISMATCH,
            f"{decision.asset_class.value} requires {
                expected.__name__ if expected else 'a contract type Chronos does not model'
            }, got {type(contract).__name__}",
            steps,
        )
    if contract.symbol != decision.symbol:
        return _refuse(
            CompilationRefusal.CONTRACT_SYMBOL_MISMATCH,
            f"the resolver returned {contract.symbol!r} for a decision about "
            f"{decision.symbol!r}; compiling this would trade the wrong instrument",
            steps,
        )

    scope = mandate.scope
    exchange = getattr(contract, "exchange", "")
    if scope.exchanges and exchange not in scope.exchanges:
        return _refuse(
            CompilationRefusal.EXCHANGE_NOT_PERMITTED,
            f"the qualified contract routes to {exchange!r}, which the mandate does not permit",
            steps,
        )
    trading_class = getattr(contract, "trading_class", "")
    if scope.contract_families and trading_class not in scope.contract_families:
        return _refuse(
            CompilationRefusal.FAMILY_NOT_PERMITTED,
            f"the qualified contract's trading class {trading_class!r} is not in the "
            "mandate's permitted contract families",
            steps,
        )
    currency = getattr(contract, "currency", "USD")
    if currency != "USD":
        # Every mandate limit is denominated in USD. Compiling a non-USD
        # contract would compare a EUR notional against a USD ceiling and call
        # it compliant, which is a silent limit breach rather than a refusal.
        return _refuse(
            CompilationRefusal.CURRENCY_NOT_PERMITTED,
            f"the qualified contract is denominated in {currency}, but every mandate "
            "limit is USD; compiling it would compare unlike currencies",
            steps,
        )
    steps.append(f"contract {contract.symbol} on {exchange or 'n/a'} verified against scope")
    return None


def _select_order_form(mandate: AutonomyMandate) -> OrderForm | None:
    """Prefer the MOST aggressive granted form (ADR-0017).

    M4 preferred the passive form, reasoning that paying the spread should be an
    explicit grant rather than a default. ADR-0017 keeps that reasoning and
    flips the conclusion around it: listing an aggressive form in the mandate's
    ``order_forms`` *is* the explicit grant, and an owner who granted MARKET
    granted it to be used — a compiler that quietly preferred LIMIT anyway would
    be second-guessing a written authorization. An owner who wants passive fills
    grants only LIMIT, and nothing here overrides that either.
    """

    forms = mandate.scope.order_forms
    for form in (OrderForm.MARKET, OrderForm.MARKETABLE_LIMIT, OrderForm.LIMIT):
        if form in forms:
            return form
    return None


def _derive_limit_price(
    *,
    quote: QuoteEvidence,
    side: OrderSide,
    form: OrderForm,
    contract: Instrument,
    steps: list[str],
) -> Decimal | CompilationOutcome:
    """Compute the limit price from the quote alone. No model input reaches here."""

    if form is OrderForm.MARKET:
        # ADR-0017's protected market: cross the spread PLUS a bounded collar,
        # so it fills immediately in any sane book while a flash-crash print
        # cannot fill it at an absurd price. This is the deliberate difference
        # from the reference project's unbounded ``MKT`` — same fill behavior
        # on every ordinary day, a ceiling on the catastrophic one.
        if side is OrderSide.BUY:
            base = quote.ask * (Decimal(1) + MARKET_PROTECTION_COLLAR)
        else:
            base = quote.bid * (Decimal(1) - MARKET_PROTECTION_COLLAR)
    elif form is OrderForm.MARKETABLE_LIMIT:
        # Cross the spread: buy at the ask, sell at the bid.
        base = quote.ask if side is OrderSide.BUY else quote.bid
    else:
        # Rest on our own side: buy at the bid, sell at the ask.
        base = quote.bid if side is OrderSide.BUY else quote.ask
    steps.append(f"base price {base} from {form.value} on {side.value}")

    tick = _min_tick(contract)
    if tick is None or tick <= 0:
        return _refuse(
            CompilationRefusal.TICK_NOT_KNOWN,
            "the qualified contract carries no minimum tick, so a conforming price "
            "cannot be derived; venue metadata is never assumed",
            steps,
        )
    # Round AWAY from aggression so tick conformance can never make an order
    # more expensive than the price we just derived.
    ticks = base / tick
    conformed = (
        ticks.to_integral_value(rounding="ROUND_FLOOR")
        if side is OrderSide.BUY
        else ticks.to_integral_value(rounding="ROUND_CEILING")
    ) * tick
    if conformed <= 0:
        return _refuse(
            CompilationRefusal.PRICE_NOT_DERIVABLE,
            f"tick conformance reduced the limit price to {conformed}; a non-positive "
            "limit is a market order by another name",
            steps,
        )
    steps.append(f"tick-conformed to {conformed} (tick {tick})")
    return conformed


def _min_tick(contract: Instrument) -> Decimal | None:
    """The venue's tick, from the qualified contract only.

    Equities have no ``min_tick`` on ``UnderlyingContract``, so the US equity
    convention of a penny is used — and only for equities, where it is a
    regulated minimum rather than an assumption. Options and crypto must carry
    their own, and refuse when they do not.
    """

    if isinstance(contract, UnderlyingContract):
        return Decimal("0.01")
    tick = getattr(contract, "min_tick", None)
    return tick if isinstance(tick, Decimal) else None


def _check_entry_condition(
    decision: AITradeDecision, quote: QuoteEvidence, steps: list[str]
) -> CompilationOutcome | None:
    """Evaluate the decision's entry trigger, if it has one.

    A ``PriceTrigger`` is a condition, not a price. It can do exactly one thing
    here: stop the order. It cannot set, widen, or reprice anything, because the
    limit price is derived from the quote before this is ever consulted.

    A trigger referencing a price the supervisor did not gather is refused
    rather than skipped. Deny-by-default applies to conditions as much as to
    ceilings: a condition we cannot evaluate is not a condition we can call
    satisfied, and silently ignoring one would let a model attach conditions
    that look protective and do nothing.
    """

    plan = decision.entry_plan
    if plan is None or plan.trigger is None:
        return None
    trigger = plan.trigger

    observed = _observed_price(trigger.reference, quote)
    if observed is None:
        return _refuse(
            CompilationRefusal.ENTRY_CONDITION_UNEVALUABLE,
            f"the decision's entry trigger references {trigger.reference.value}, which the "
            "supervisor did not gather; an unevaluable condition is not a satisfied one",
            steps,
        )

    if trigger.comparator is TriggerComparator.AT_OR_ABOVE:
        satisfied = observed >= trigger.value
    else:
        satisfied = observed <= trigger.value
    if not satisfied:
        return _refuse(
            CompilationRefusal.ENTRY_CONDITION_UNMET,
            f"the decision's entry condition is not met: {trigger.reference.value} "
            f"{observed} is not {trigger.comparator.value} {trigger.value}",
            steps,
        )
    steps.append(
        f"entry condition met: {trigger.reference.value} {observed} "
        f"{trigger.comparator.value} {trigger.value}"
    )
    return None


def _observed_price(reference: PriceReference, quote: QuoteEvidence) -> Decimal | None:
    """The supervisor's observation for a trigger reference, or ``None``.

    ``LAST`` and ``UNDERLYING_LAST`` return ``None`` deliberately: a last trade
    is a historical print rather than something transactable, and the underlying
    is a different instrument entirely. Neither is in ``QuoteEvidence``, and
    inventing one from the bid/ask would be fabricating the very observation the
    condition is supposed to test.
    """

    if reference is PriceReference.BID:
        return quote.bid
    if reference is PriceReference.ASK:
        return quote.ask
    if reference is PriceReference.MID:
        return (quote.bid + quote.ask) / Decimal(2)
    return None

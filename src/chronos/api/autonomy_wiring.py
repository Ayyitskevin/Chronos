"""Wiring the autonomy runtime into the backend, for real (ADR-0017, M7.5).

Until this module, every piece of the autonomy stack existed and nothing
assembled it: the tick had no facts, the cycle had no handoff, the mandate had
no store. ADR-0017 is the owner's direction that assembling it should be the
*default* — a persistent mandate file plus a running backend is enough to trade,
with no per-boot ritual.

This module lives in the **app plane** deliberately. It imports both the
supervisor (to drive it) and the broker surface (to gather facts), which is
exactly the combination the supervisor itself is structurally forbidden from
holding — the seam stays a callable, and the callable lives here.

## The persistent mandate (ADR-0017 §2)

``AUTONOMY_MANDATE_FILE`` names an owner-authored JSON document validated
against :class:`~chronos.autonomy.mandate.AutonomyMandate` on every boot.
Present and valid → it is loaded and **auto-activated**: the activation row is
written with an owner-event id derived from the file's digest, so the audit
trail shows *which text* granted the authority. This supersedes ADR-0016's
"an environment variable alone may not activate live autonomous trading" — the
supersession is the point, it is owner-directed, and it is recorded in
ADR-0017/D-17 rather than done quietly.

What auto-activation deliberately does **not** override:

- **A revoked activation stays revoked.** Revocation is the owner standing the
  system down, and a restart must not undo it — otherwise revoking would mean
  racing the process supervisor. Re-granting after a revocation is a new
  mandate_version in the file, which is a fresh owner act.
- **An invalid or unreadable mandate file is no mandate.** The backend boots,
  trading stays inert, and a CRITICAL owner alert says why. A broken grant must
  not take down the process that can still close positions.
- **Expiry still expires.** Admission refuses an expired mandate regardless of
  what this module loaded.

## Facts, honestly sourced

The account slice comes from the broker's account summary each tick. The
market-data evidence admission checks is a **probe quote** of the mandate's
first scoped symbol — a real quote with a real age, standing in for "is the
feed alive", with per-instrument staleness additionally caught at pricing time
(a crossed or empty book refuses compilation). The per-decision instrument
slice qualifies the named symbol and quotes it fresh, so a batch of proposals
about different symbols each price against their own book.

Anything that cannot be gathered returns ``None``, and the runtime already
treats that as a refusal-to-run with an owner alert — facts are never invented
to keep a tick alive.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from chronos.api.option_selection import AutonomousOptionSelectionService
from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    DecisionKind,
    OrderForm,
    StrategyForm,
    TradableAssetClass,
)
from chronos.domain.enums import OptionRight
from chronos.domain.models import Instrument, MarketQuote
from chronos.orders.intent import WheelOrderIntent
from chronos.runtime import AppRuntime
from chronos.supervisor import alerts, delivery, durable, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts, Handoff, InstrumentFacts
from chronos.supervisor.option_selection import (
    OptionSelectionPolicy,
    OptionSelectionRequest,
    canonical_digest,
)
from chronos.supervisor.runtime import AutonomyRuntime, FactGatherer, RuntimeConfig
from chronos.supervisor.sizing import AccountEvidence
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.autonomy")

#: The provenance the queue writer stamps for proposals arriving over the
#: ingress. The external worker's own model identity is unknowable from here —
#: it runs in another process by design — so the stamp names the boundary that
#: accepted the proposal. The mandate's pins must agree with these values,
#: which a persistent-mandate author sets once.
INGRESS_IDENTITY = queue.HarnessIdentity(
    provider="external-worker",
    model_id="ingress",
    model_version="1",
    prompt_version="1",
    tool_schema_version="1",
    decision_schema_version="1",
    policy_version="1",
    evidence_bundle_id="owner-workspace",
    evidence_bundle_digest="0" * 64,
)


@dataclass(frozen=True, slots=True)
class LoadedMandate:
    """A validated persistent mandate and the digest of the text that grants it."""

    mandate: AutonomyMandate
    digest: str


def load_persistent_mandate(path: Path) -> LoadedMandate | None:
    """Read and validate the owner's standing grant. Invalid is None, loudly.

    The digest is over the raw bytes, so the activation row records exactly
    which document authorized trading — edit the file and the next boot writes
    a new, distinguishable activation.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        _logger.exception("Autonomy mandate file %s is unreadable", path)
        return None
    try:
        mandate = AutonomyMandate.model_validate_json(raw)
    except ValidationError:
        _logger.exception("Autonomy mandate file %s does not validate; autonomy stays inert", path)
        return None
    return LoadedMandate(mandate=mandate, digest=hashlib.sha256(raw).hexdigest())


def ensure_activation(
    runtime: AppRuntime,
    loaded: LoadedMandate,
    *,
    fingerprint: str,
    process_generation: int,
    now: datetime,
) -> bool:
    """Auto-activate the persistent mandate, honouring revocation.

    Returns False — and raises an owner alert — when a prior activation of this
    exact mandate version was revoked: a restart is not permission to undo the
    owner standing the system down.
    """

    with runtime.database.sessions.begin() as session:
        existing = durable.load_activation(
            session, account_fingerprint=fingerprint, mandate=loaded.mandate
        )
        if existing is not None and existing.revoked:
            alerts.raise_alert(
                session,
                account_fingerprint=fingerprint,
                severity=alerts.AlertSeverity.WARNING,
                kind="autonomy.revoked_mandate_present",
                summary=(
                    "the persistent mandate on disk was revoked and will not auto-activate; "
                    "author a new mandate_version to re-grant"
                ),
                now=now,
            )
            return False
        if existing is None:
            durable.activate(
                session,
                account_fingerprint=fingerprint,
                mandate=loaded.mandate,
                owner_event_id=f"persistent-mandate:{loaded.digest[:16]}",
                now=now,
                process_generation=process_generation,
            )
            _logger.info(
                "Persistent mandate %s v%d auto-activated (digest %s)",
                loaded.mandate.mandate_id,
                loaded.mandate.mandate_version,
                loaded.digest[:16],
                extra={"event": "autonomy_mandate_activated"},
            )
        return True


class RiskRefusedByOrderPlane(RuntimeError):
    """The order plane's risk engine refused the proposal."""


class ConfirmationRefusedByOrderPlane(RuntimeError):
    """The order plane refused to confirm the intent."""


def order_plane_handoff(runtime: AppRuntime) -> Handoff:
    """The full existing pipeline, as one callable: propose → preview → confirm → submit.

    Nothing is skipped. An AI-compiled intent walks the same risk engine,
    preview, confirmation and ten-gate live stack a human proposal walks, and a
    refusal at any of those surfaces back to the cycle as ORDER_PLANE_REFUSED —
    autonomy added a gate stack and removed none, which is the sentence that has
    governed every milestone since M2.
    """

    def _submit(intent: WheelOrderIntent) -> object:
        service = runtime.order_management
        now = utc_now()
        proposal = service.propose(intent, now=now)
        if not proposal.risk.approved:
            raise RiskRefusedByOrderPlane(proposal.risk.decision_id)
        service.preview(intent, now=utc_now())
        service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=utc_now())
        return service.submit(intent, writer_lease_held=True, now=utc_now())

    return _submit


def _request_for(decision: AITradeDecision, runtime: AppRuntime) -> OptionSelectionRequest:
    """Translate authorized economics; broker identity remains unexpressible."""

    del runtime
    strategy = decision.requested_strategy or StrategyForm.LONG_PUT
    right = {
        StrategyForm.CASH_SECURED_PUT: OptionRight.PUT,
        StrategyForm.COVERED_CALL: OptionRight.CALL,
        StrategyForm.LONG_CALL: OptionRight.CALL,
    }.get(strategy, OptionRight.PUT)
    return OptionSelectionRequest(
        decision_id=decision.decision_id,
        symbol=decision.symbol,
        strategy=strategy,
        right=right,
        requested_contracts=decision.requested_quantity,
    )


def _policy_for(
    decision: AITradeDecision,
    mandate: AutonomyMandate,
    runtime: AppRuntime,
) -> OptionSelectionPolicy:
    """Intersect runtime evidence settings with the exact owner mandate."""

    del decision
    settings = runtime.settings
    mandate_age = mandate.market_data.max_quote_age_seconds
    max_quote_age = min(
        Decimal(settings.max_quote_age_seconds),
        mandate_age if mandate_age > 0 else Decimal(settings.max_quote_age_seconds),
    )
    mandate_spread = mandate.market_data.max_relative_spread
    max_spread = (
        min(settings.max_relative_spread, mandate_spread) if mandate_spread > 0 else Decimal(0)
    )
    order_form = next(
        (
            item
            for item in (OrderForm.MARKET, OrderForm.MARKETABLE_LIMIT, OrderForm.LIMIT)
            if item in mandate.scope.order_forms
        ),
        OrderForm.LIMIT,
    )
    return OptionSelectionPolicy(
        max_quote_age_seconds=max_quote_age,
        max_chain_age_seconds=Decimal("3600"),
        max_candidate_expirations=settings.max_expirations,
        max_strikes_per_expiration=settings.max_strikes_per_expiration,
        min_dte=settings.min_dte,
        target_dte=settings.target_dte,
        max_dte=settings.max_dte,
        min_abs_delta=settings.min_abs_delta,
        target_abs_delta=settings.target_abs_delta,
        max_abs_delta=settings.max_abs_delta,
        permitted_data_qualities=mandate.market_data.permitted_data_qualities,
        min_option_volume=max(settings.min_option_volume, mandate.market_data.min_option_volume),
        min_open_interest=max(
            settings.min_open_interest,
            mandate.market_data.min_open_interest,
        ),
        max_relative_spread=max_spread,
        permitted_exchanges=mandate.scope.exchanges,
        permitted_trading_classes=mandate.scope.contract_families,
        permitted_sessions=mandate.sessions.permitted_sessions,
        required_multiplier=Decimal("100"),
        order_form=order_form,
        market_timezone=settings.market_timezone,
        delta_weight=settings.delta_weight,
        spread_weight=settings.spread_weight,
        dte_weight=settings.dte_weight,
        liquidity_weight=settings.liquidity_weight,
    )


class BackendGatherers:
    """Facts from the running backend, per tick and per decision."""

    def __init__(
        self,
        runtime: AppRuntime,
        fingerprint: str,
        generation: int,
        *,
        mandate: AutonomyMandate,
        mandate_digest: str,
    ) -> None:
        self._runtime = runtime
        self._fingerprint = fingerprint
        self._generation = generation
        self._mandate = mandate
        self._option_selection = AutonomousOptionSelectionService(
            runtime,
            autonomy_mode=mandate.mode.value,
            mandate_digest=mandate_digest,
            account_fingerprint=fingerprint,
        )

    def _probe_symbol(self, mandate: AutonomyMandate) -> str:
        symbols = mandate.scope.symbols
        return symbols[0] if symbols else ""

    def cycle_facts(self, mandate: AutonomyMandate) -> FactGatherer:
        def _gather(now: datetime) -> CycleFacts | None:
            runtime = self._runtime
            try:
                summary = runtime.connection.run(runtime.broker.account_summary())
                probe = self._probe_symbol(mandate)
                if not probe:
                    return None
                contract = runtime.connection.run(runtime.broker.qualify_underlying(probe))
                quote = runtime.connection.run(runtime.broker.request_underlying_quote(contract))
            except Exception:
                _logger.exception("Autonomy fact gathering failed")
                return None
            market = _market_evidence(quote, now)
            probe_quote = _quote_evidence(quote)
            if market is None or probe_quote is None:
                return None
            return CycleFacts(
                account_fingerprint=self._fingerprint,
                account_id=summary.account_id,
                now=now,
                process_generation=self._generation,
                evidence_bundle_id=INGRESS_IDENTITY.evidence_bundle_id,
                evidence_bundle_digest=INGRESS_IDENTITY.evidence_bundle_digest,
                market_data=market,
                account=AccountEvidence(
                    net_liquidation_usd=summary.net_liquidation,
                    total_cash_usd=summary.total_cash,
                    buying_power_usd=summary.buying_power,
                ),
                quote=probe_quote,
                contract=contract,
                reference_price=_reference_price(quote),
                multiplier=Decimal(1),
                market_timezone=runtime.settings.autonomy_market_timezone,
            )

        return _gather

    def instrument_facts(self, decision: AITradeDecision) -> InstrumentFacts | None:
        """Qualify and quote the decision's own instrument, fresh.

        Equities and crypto use their existing direct qualification path.
        Opening equity options use the read-only evidence service and carry its
        canonical receipt back to the cycle. The cycle persists and verifies
        that receipt before it uses the selected contract.
        """

        runtime = self._runtime
        contract: Instrument
        quote: MarketQuote
        option_request: OptionSelectionRequest | None = None
        option_policy: OptionSelectionPolicy | None = None
        try:
            if decision.asset_class is TradableAssetClass.EQUITY:
                equity = runtime.connection.run(runtime.broker.qualify_underlying(decision.symbol))
                quote = runtime.connection.run(runtime.broker.request_underlying_quote(equity))
                contract = equity
            elif decision.asset_class is TradableAssetClass.CRYPTO:
                crypto = runtime.connection.run(runtime.broker.qualify_crypto(decision.symbol))
                quote = runtime.connection.run(runtime.broker.request_crypto_quote(crypto))
                contract = crypto
            elif (
                decision.asset_class is TradableAssetClass.EQUITY_OPTION
                and decision.kind is DecisionKind.OPEN
            ):
                option_request = _request_for(decision, runtime)
                option_policy = _policy_for(decision, self._mandate, runtime)
                receipt = self._option_selection.resolve(
                    request=option_request,
                    policy=option_policy,
                ).receipt
                selected = receipt.selected
                if selected is None:
                    return InstrumentFacts(
                        contract=None,
                        quote=None,
                        reference_price=Decimal(0),
                        multiplier=Decimal(1),
                        selection_receipt=receipt,
                        selection_activation_validator=self._option_selection.validate_handoff,
                    )
                selected_quote = _quote_evidence(selected.quote)
                if selected_quote is None:
                    return InstrumentFacts(
                        contract=None,
                        quote=None,
                        reference_price=Decimal(0),
                        multiplier=Decimal(1),
                        selection_receipt=receipt,
                        selection_activation_validator=self._option_selection.validate_handoff,
                    )
                return InstrumentFacts(
                    contract=selected.contract,
                    quote=selected_quote,
                    reference_price=selected.sizing_reference_price,
                    multiplier=selected.deliverable_shares,
                    selection_receipt=receipt,
                    selection_activation_validator=self._option_selection.validate_handoff,
                )
            else:
                return None
        except Exception as error:
            _logger.exception("Instrument fact gathering failed for %s", decision.symbol or "?")
            if (
                decision.asset_class is TradableAssetClass.EQUITY_OPTION
                and decision.kind is DecisionKind.OPEN
                and option_request is not None
                and option_policy is not None
            ):
                try:
                    receipt = self._option_selection.refusal_for_exception(
                        option_request,
                        option_policy,
                        error,
                    ).receipt
                    return InstrumentFacts(
                        contract=None,
                        quote=None,
                        reference_price=Decimal(0),
                        multiplier=Decimal(1),
                        selection_receipt=receipt,
                        selection_activation_validator=self._option_selection.validate_handoff,
                    )
                except Exception:
                    _logger.exception("Could not canonicalize the option resolver failure")
            return None
        reference = _reference_price(quote)
        if reference <= 0:
            return None
        return InstrumentFacts(
            contract=contract,
            quote=_quote_evidence(quote),
            reference_price=reference,
            multiplier=Decimal(1),
        )


def _quote_evidence(quote: MarketQuote) -> QuoteEvidence | None:
    if quote.bid is None or quote.ask is None:
        return None
    return QuoteEvidence(bid=quote.bid, ask=quote.ask)


def _reference_price(quote: MarketQuote) -> Decimal:
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        return (quote.bid + quote.ask) / Decimal(2)
    return Decimal(0)


def _market_evidence(quote: MarketQuote, now: datetime) -> MarketDataEvidence | None:
    age = (now - quote.timestamp).total_seconds()
    if age < 0:
        age = 0.0
    return MarketDataEvidence(
        quote_age_seconds=Decimal(str(round(age, 3))),
        quality=quote.data_quality,
    )


def build_autonomy_runtime(
    runtime: AppRuntime, *, process_generation: int
) -> AutonomyRuntime | None:
    """Assemble the whole autonomy stack from settings, or None when no grant exists.

    No mandate file configured → no runtime. This is the one remaining default
    that is not autonomy-maximal, and it is kept on purpose: a fresh checkout
    with no owner-authored grant anywhere must boot inert, because the grant is
    the owner act everything else hangs from.
    """

    settings = runtime.settings
    path = settings.autonomy_mandate_file
    if path is None:
        return None
    loaded = load_persistent_mandate(path)
    fingerprint = account_fingerprint(runtime.order_management.account_id)
    now = utc_now()
    if loaded is None:
        _alert_bad_mandate(runtime, fingerprint, now)
        return None
    if loaded.mandate.account_fingerprint != fingerprint:
        _logger.error("Persistent mandate is scoped to a different account; autonomy stays inert")
        _alert_bad_mandate(runtime, fingerprint, now)
        return None
    if not ensure_activation(
        runtime,
        loaded,
        fingerprint=fingerprint,
        process_generation=process_generation,
        now=now,
    ):
        return None

    mandate = loaded.mandate
    gatherers = BackendGatherers(
        runtime,
        fingerprint,
        process_generation,
        mandate=mandate,
        mandate_digest=canonical_digest(mandate),
    )
    return AutonomyRuntime(
        sessions=runtime.database.sessions,
        config=RuntimeConfig(
            account_fingerprint=fingerprint,
            minimum_interval_seconds=settings.autonomy_min_interval_seconds,
            idle_interval_seconds=settings.autonomy_idle_interval_seconds,
        ),
        identity=INGRESS_IDENTITY,
        mandate_source=lambda: mandate,
        gather_facts=gatherers.cycle_facts(mandate),
        gather_instrument=gatherers.instrument_facts,
        sinks=delivery.default_sinks(settings.autonomy_alert_file),
        submit=order_plane_handoff(runtime),
    )


def _alert_bad_mandate(runtime: AppRuntime, fingerprint: str, now: datetime) -> None:
    try:
        with runtime.database.sessions.begin() as session:
            alerts.raise_alert(
                session,
                account_fingerprint=fingerprint,
                severity=alerts.AlertSeverity.CRITICAL,
                kind="autonomy.mandate_invalid",
                summary=(
                    "the persistent mandate file is unreadable, invalid, or scoped to a "
                    "different account; autonomy is inert until it is fixed"
                ),
                now=now,
            )
    except Exception:
        _logger.exception("Could not record the invalid-mandate alert")


async def autonomy_tick_task(autonomy: AutonomyRuntime) -> None:
    """Drive ticks forever, off the event loop's back.

    One-second poll granularity against the runtime's own schedule (whose floor
    is configuration, not this constant). `run_tick` never raises by contract,
    and the loop exits when the runtime stops itself — which it does only after
    consecutive failures, with a CRITICAL alert already raised.
    """

    while not autonomy.stopped:
        now = utc_now()
        wait = autonomy.seconds_until_next_tick(now)
        if wait > 0:
            await asyncio.sleep(min(wait, 1.0))
            continue
        await asyncio.to_thread(autonomy.run_tick, now)
    _logger.warning(
        "Autonomy tick task exiting: the runtime stopped itself",
        extra={"event": "autonomy_task_exit"},
    )

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from chronos.autonomy import AITradeDecision, AutonomyMandate, TradableAssetClass
from chronos.domain.models import Instrument, MarketQuote
from chronos.orders.intent import WheelOrderIntent
from chronos.runtime import AppRuntime
from chronos.supervisor import alerts, delivery, durable, proposers, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts, Handoff, InstrumentFacts
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
    # No bundle machinery exists yet; absence is attested as absence rather
    # than as sixty-four zeros that read like a computed digest (ADR-0023).
    evidence_bundle_digest=None,
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


def build_identity_resolver(
    settings_path: Path | None,
) -> Callable[[str | None, datetime], queue.HarnessIdentity | None] | None:
    """The per-proposal identity resolver, or None for the pre-registry posture.

    ADR-0023: identity comes from which credential authenticated. The route
    records the verified proposer_id on the queue row; this resolver turns it
    back into the registration's identity at drain time — re-checking currency,
    because a registration revoked or expired between enqueue and drain must
    refuse at the moment authority is exercised.

    Fail-closed by shape: with a registry configured, EVERY path that cannot
    positively resolve — a pre-registry row, an unknown id, a disabled or
    expired registration, an unreadable file — returns None, which the cycle
    records as a STAMP-stage refusal. A configured-but-broken registry never
    falls back to the static identity, because that would let a file error
    silently reopen anonymous proposing.
    """

    if settings_path is None:
        return None
    loaded = proposers.load_proposer_registry(settings_path)
    if loaded is None:
        _logger.error(
            "Proposer registry %s is unreadable or invalid; every queued proposal "
            "will refuse at STAMP until it is fixed",
            settings_path,
        )

        def _refuse_all(proposer_id: str | None, now: datetime) -> queue.HarnessIdentity | None:
            return None

        return _refuse_all

    registry = loaded.registry
    _logger.info(
        "Proposer registry loaded (digest %s, %d registration(s))",
        loaded.digest[:16],
        len(registry.proposers),
        extra={"event": "autonomy_proposers_loaded"},
    )

    def _resolve(proposer_id: str | None, now: datetime) -> queue.HarnessIdentity | None:
        if proposer_id is None:
            # A row enqueued under the pre-registry posture, met by a
            # registry-on runtime: refusing beats stamping the static identity
            # onto a proposal whose author the owner has since required.
            return None
        registration = registry.find(proposer_id)
        if registration is None or not registration.is_current(now):
            return None
        return queue.HarnessIdentity(
            provider=registration.provider,
            model_id=registration.model_id,
            model_version=registration.model_version,
            prompt_version=registration.prompt_version,
            tool_schema_version=registration.tool_schema_version,
            decision_schema_version=registration.decision_schema_version,
            policy_version=registration.policy_version,
            proposer_id=registration.proposer_id,
            evidence_bundle_id=INGRESS_IDENTITY.evidence_bundle_id,
            evidence_bundle_digest=INGRESS_IDENTITY.evidence_bundle_digest,
        )

    return _resolve


def order_plane_handoff(runtime: AppRuntime, *, is_writer: Callable[[], bool]) -> Handoff:
    """The full existing pipeline, as one callable: propose → preview → confirm → submit.

    Nothing is skipped. An AI-compiled intent walks the same risk engine,
    preview, confirmation and ten-gate live stack a human proposal walks, and a
    refusal at any of those surfaces back to the cycle as ORDER_PLANE_REFUSED —
    autonomy added a gate stack and removed none, which is the sentence that has
    governed every milestone since M2.

    ``is_writer`` is read **per submission**, never captured as a value. Until
    2026-08-05 this passed the literal ``True``, which was not a bypass — the
    submission boundary re-checks lease ownership in the database inside the
    CAS-to-transmit window (R-24), so a demoted process was still refused before
    anything reached the broker. It was wrong in two smaller ways that were worth
    fixing:

    - **It refused late.** A backend demoted mid-session by the lease heartbeat
      would run propose, preview and confirm — writing intent, preview and
      confirmation state — before the final re-check turned it away. Gate 1
      exists to stop that at the door.
    - **It refused with the wrong reason.** The early gate answers
      ``READ_ONLY_LEASE``; the late one does not, so the operator-facing
      explanation for "this backend is read-only" was the least specific of the
      available refusals.

    The human path has always passed the live value (``state.writer`` in
    ``chronos.api.routes.orders``). This makes the autonomous path identical,
    which is the property ADR-0018 §4 and every milestone since M2 assert: the
    autonomous path IS the human path.
    """

    def _submit(intent: WheelOrderIntent) -> object:
        service = runtime.order_management
        now = utc_now()
        proposal = service.propose(intent, now=now)
        if not proposal.risk.approved:
            raise RiskRefusedByOrderPlane(proposal.risk.decision_id)
        service.preview(intent, now=utc_now())
        service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=utc_now())
        return service.submit(intent, writer_lease_held=is_writer(), now=utc_now())

    return _submit


class BackendGatherers:
    """Facts from the running backend, per tick and per decision."""

    def __init__(self, runtime: AppRuntime, fingerprint: str, generation: int) -> None:
        self._runtime = runtime
        self._fingerprint = fingerprint
        self._generation = generation

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

        Equities and crypto resolve here. Options do not yet: chain resolution
        needs strike/expiry selection this wiring does not own, so an option
        decision refuses at this seam rather than pricing against a guess —
        disclosed in ADR-0017's residuals.
        """

        runtime = self._runtime
        contract: Instrument
        quote: MarketQuote
        try:
            if decision.asset_class is TradableAssetClass.EQUITY:
                equity = runtime.connection.run(runtime.broker.qualify_underlying(decision.symbol))
                quote = runtime.connection.run(runtime.broker.request_underlying_quote(equity))
                contract = equity
            elif decision.asset_class is TradableAssetClass.CRYPTO:
                crypto = runtime.connection.run(runtime.broker.qualify_crypto(decision.symbol))
                quote = runtime.connection.run(runtime.broker.request_crypto_quote(crypto))
                contract = crypto
            else:
                return None
        except Exception:
            _logger.exception("Instrument fact gathering failed for %s", decision.symbol or "?")
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
    runtime: AppRuntime, *, process_generation: int, is_writer: Callable[[], bool]
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

    gatherers = BackendGatherers(runtime, fingerprint, process_generation)
    mandate = loaded.mandate
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
        submit=order_plane_handoff(runtime, is_writer=is_writer),
        resolve_identity=build_identity_resolver(settings.autonomy_proposers_file),
    )


def alert_invalid_proposer_registry(runtime: AppRuntime) -> None:
    """CRITICAL owner alert: the proposer registry exists and does not load.

    Raised from startup (the mandate-file precedent): the backend boots and
    stays able to close positions, the proposal route refuses with 503, the
    drain refuses at STAMP — and the owner is told without having to look at
    a log file (R-32).
    """

    fingerprint = account_fingerprint(runtime.order_management.account_id)
    try:
        with runtime.database.sessions.begin() as session:
            alerts.raise_alert(
                session,
                account_fingerprint=fingerprint,
                severity=alerts.AlertSeverity.CRITICAL,
                kind="autonomy.proposers_invalid",
                summary=(
                    "the proposer registry file is unreadable or invalid; every proposal "
                    "refuses until it is fixed"
                ),
                now=utc_now(),
            )
    except Exception:
        _logger.exception("Could not record the invalid-proposer-registry alert")


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

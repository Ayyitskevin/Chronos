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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from chronos.api.option_selection import AutonomousOptionSelectionService
from chronos.autonomy import (
    AITradeDecision,
    AutonomyMandate,
    DecisionKind,
    OrderForm,
    StrategyForm,
    TradableAssetClass,
)
from chronos.autonomy.enums import SUBMITTING_AUTONOMY_MODES
from chronos.config.settings import Settings
from chronos.domain.enums import OptionRight, OrderLifecycle
from chronos.domain.models import Instrument, MarketQuote
from chronos.orders.intent import WheelOrderIntent
from chronos.orders.submission import SubmissionOutcome, SubmissionRefusalCode
from chronos.runtime import AppRuntime
from chronos.supervisor import alerts, delivery, durable, proposers, queue, revocation
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import SUBMIT_RAISED_CODE, HandoffResult
from chronos.supervisor.loop import CycleFacts, Handoff, InstrumentFacts
from chronos.supervisor.option_selection import (
    OptionSelectionPolicy,
    OptionSelectionRequest,
    canonical_digest,
)
from chronos.supervisor.runtime import (
    AutonomyRuntime,
    FactGatherer,
    ResolvedIdentity,
    RuntimeConfig,
)
from chronos.supervisor.sizing import AccountEvidence
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.secure_files import (
    AuthorityMode,
    UnsafeAuthorityFile,
    read_authority_file,
)
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

    Read through the descriptor-bound authority-file contract in
    ``AuthorityMode.GRANT``: no symlink, a regular file owned by this effective
    user, one link, not writable by group or other, and the digest taken over
    the same bytes every check ran against. GRANT rather than SECRET because a
    mandate is an owner-authored decision, not a secret — what matters is that
    nobody else could have WRITTEN it. 0644 is accepted, which is what
    ``chronos.cli mandate template > file`` produces under a normal umask;
    demanding 0600 would refuse the mandate our own docs tell the owner to
    create.
    """

    try:
        contents = read_authority_file(path, label="autonomy mandate", mode=AuthorityMode.GRANT)
    except FileNotFoundError:
        _logger.error("Autonomy mandate file %s does not exist; autonomy stays inert", path)
        return None
    except UnsafeAuthorityFile as error:
        # Not "invalid content" — the file is not one this process may trust at
        # all. CRITICAL, and refused rather than repaired: chmodding an
        # owner-authored grant on the owner's behalf is the process editing the
        # grant. The typed startup fault for this path is noted where the
        # runtime is assembled.
        _logger.critical("Autonomy mandate file is unsafe; autonomy stays inert: %s", error)
        return None
    try:
        mandate = AutonomyMandate.model_validate_json(contents.data)
    except ValidationError:
        _logger.exception("Autonomy mandate file %s does not validate; autonomy stays inert", path)
        return None
    return LoadedMandate(mandate=mandate, digest=contents.sha256)


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


#: Which submission refusals prove nothing left the process, and which cannot
#: (A1). This table is the whole translation: every code the order plane can
#: answer with is classified as *provably not sent* or *possibly sent*, and the
#: supervisor never sees the code's type — only the disposition it implies.
#:
#: ``True`` means **provably not sent**: the refusal is returned from a gate that
#: runs before the single ``transmit=True`` site, including the two re-checks
#: inside the CAS-to-transmit window (kill switch, writer lease) whose own
#: details say "nothing was sent", and ``BROKER_REFUSED_BEFORE_SEND``, which
#: ADR-0009 §6 defines as the adapter refusing locally before any network send.
#:
#: ``False`` means the wire state is not established from the code alone.
#: ``BROKER_SUBMIT_FAILED`` is the only such code, and it covers three real
#: shapes that :func:`classify_submission_outcome` separates.
#:
#: Adding a member to ``SubmissionRefusalCode`` without adding it here fails
#: ``test_typed_handoff_outcomes_exercised.py`` — a new refusal must be
#: classified deliberately, not defaulted into silence.
_PROVABLY_NOT_SENT: dict[SubmissionRefusalCode, bool] = {
    SubmissionRefusalCode.NOT_REFUSED: True,
    SubmissionRefusalCode.READ_ONLY_LEASE: True,
    SubmissionRefusalCode.TRANSMISSION_NOT_POSSIBLE: True,
    SubmissionRefusalCode.MODE_FORBIDS: True,
    SubmissionRefusalCode.ACCOUNT_MISMATCH: True,
    SubmissionRefusalCode.RISK_NOT_APPROVED: True,
    SubmissionRefusalCode.RISK_EXPIRED: True,
    SubmissionRefusalCode.CONFIRMATION_MISSING: True,
    SubmissionRefusalCode.RISK_EVIDENCE_STALE: True,
    SubmissionRefusalCode.CONFIRMATION_EXPIRED: True,
    SubmissionRefusalCode.CONFIRMATION_MISMATCH: True,
    SubmissionRefusalCode.INTENT_NOT_CONFIRMED: True,
    SubmissionRefusalCode.RECONCILIATION_NOT_READY: True,
    SubmissionRefusalCode.LIVE_DEPENDENCIES_MISSING: True,
    SubmissionRefusalCode.LIVE_GRANT_DENIED: True,
    SubmissionRefusalCode.LIVE_GATE_BLOCKED: True,
    SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND: True,
    SubmissionRefusalCode.BROKER_SUBMIT_FAILED: False,
}

#: Lifecycles that mean the venue holds something that can still act. Anything
#: else in an acknowledged send is the venue answering "not working".
_ACTIVE_LIFECYCLES: frozenset[OrderLifecycle] = frozenset(
    {
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.FILLED,
        OrderLifecycle.CANCEL_PENDING,
    }
)


def classify_submission_outcome(outcome: SubmissionOutcome) -> HandoffResult:
    """Translate the order plane's answer into the supervisor's vocabulary (A1).

    This function is the seam plan §6 finding 5 asked for. It lives in the app
    plane because it must read a ``SubmissionOutcome``, and the supervisor is
    structurally forbidden from importing one; what crosses back is a
    supervisor-owned :class:`~chronos.supervisor.handoff.HandoffResult`.

    The hard case is ``BROKER_SUBMIT_FAILED``, which the boundary returns for
    three materially different events. They are separated by the evidence the
    outcome carries, not by parsing its prose:

    1. **No submission object.** ``BrokerError`` came out of the transmit call
       itself, so bytes may have left and the intent stays
       ``SUBMISSION_UNKNOWN`` for reconciliation → ``SENT_AMBIGUOUS``.
    2. **A submission with an active lifecycle.** The send completed and the
       broker acknowledged it, but Chronos could not persist the acknowledgement
       (the intent row disappeared, or a newer lifecycle won the CAS). An order
       may be working at the venue that this process cannot track →
       ``SENT_AMBIGUOUS``. Treating "we lost the record of a live order" as a
       clean rejection is the single most dangerous reading available here.
    3. **A submission with a non-active lifecycle.** The venue saw the order and
       answered ``REJECTED``/``CANCELLED``/unknown-and-inactive →
       ``REJECTED_AFTER_SEND``, which counts as an attempt and needs no manual
       reconciliation.

    A ``submitted=True`` outcome is ``SUBMITTED``. Everything whose code is
    classified provably-not-sent is ``REFUSED_NOT_SENT`` and consumes no activity
    attempt — the half of the old behavior that was wrong in the *permissive*
    direction, spending an opening-order budget on orders that never existed.
    """

    if outcome.submitted:
        return HandoffResult.submitted(
            order_plane_code=outcome.refusal.value,
            detail=outcome.detail,
            raw=outcome,
        )
    # An unclassified code fails closed as possibly-sent rather than defaulting
    # to "nothing happened"; the pinning test makes the omission loud, and until
    # someone fixes it the journal over-reports risk instead of under-reporting.
    if _PROVABLY_NOT_SENT.get(outcome.refusal, False):
        return HandoffResult.refused_not_sent(
            order_plane_code=outcome.refusal.value,
            detail=outcome.detail,
            raw=outcome,
        )
    submission = outcome.submission
    if submission is not None and submission.lifecycle not in _ACTIVE_LIFECYCLES:
        return HandoffResult.rejected_after_send(
            order_plane_code=outcome.refusal.value,
            detail=f"{outcome.detail} (broker lifecycle {submission.lifecycle.value})",
            raw=outcome,
        )
    return HandoffResult.sent_ambiguous(
        order_plane_code=outcome.refusal.value,
        detail=outcome.detail,
        raw=outcome,
    )


def build_identity_resolver(
    settings_path: Path | None,
) -> Callable[[Session, str | None, str | None, str | None, datetime], ResolvedIdentity] | None:
    """The per-proposal identity resolver, or None for the pre-registry posture.

    ADR-0023/ADR-0048: identity comes from which exact credential authenticated.
    The route records the verified proposer id, credential epoch, and complete
    entry digest on the queue row; this resolver requires that immutable binding
    before reconstructing identity at drain — re-checking currency against the
    drain's clock, so a registration that EXPIRED between enqueue and drain
    refuses at the moment authority is exercised. Be precise about
    what "currency" means here: the registry itself is read once, at wiring
    time, exactly like the mandate file — so disabling or deleting a
    registration in the file takes effect at the next backend restart, not
    mid-session. Expiry is the transition the snapshot carries with it; the
    live mid-session stand-downs remain the kill switch, mandate revocation,
    and a restart.

    Fail-closed by shape: with a registry configured, EVERY path that cannot
    positively resolve — a pre-registry or legacy-unbound row, an unknown id, a
    replaced, disabled or expired registration, an unreadable file — returns no
    identity, which the cycle records as a STAMP-stage refusal. A
    configured-but-broken registry never falls back to the static identity,
    because that would let a file error silently reopen anonymous proposing.
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

        def _refuse_all(
            session: Session,
            proposer_id: str | None,
            proposer_credential_epoch: str | None,
            proposer_registry_entry_digest: str | None,
            now: datetime,
        ) -> ResolvedIdentity:
            return ResolvedIdentity(
                refusal="PROPOSER_REGISTRY_INVALID",
                detail=(
                    "the proposer registry is configured and does not load, so no "
                    "proposal can be attributed; repairing the file is an owner act"
                ),
            )

        return _refuse_all

    registry = loaded.registry
    _logger.info(
        "Proposer registry loaded (digest %s, %d registration(s))",
        loaded.digest[:16],
        len(registry.proposers),
        extra={"event": "autonomy_proposers_loaded"},
    )

    def _resolve(
        session: Session,
        proposer_id: str | None,
        proposer_credential_epoch: str | None,
        proposer_registry_entry_digest: str | None,
        now: datetime,
    ) -> ResolvedIdentity:
        if proposer_id is None:
            # A row enqueued under the pre-registry posture, met by a
            # registry-on runtime: refusing beats stamping the static identity
            # onto a proposal whose author the owner has since required.
            return ResolvedIdentity(
                detail=(
                    "the proposal was enqueued before a proposer registry was configured, "
                    "so it has no author to resolve; it is refused rather than stamped "
                    "with the static identity the owner has since replaced"
                )
            )
        if proposer_credential_epoch is None or proposer_registry_entry_digest is None:
            return ResolvedIdentity(
                refusal="PROPOSER_REGISTRATION_UNBOUND",
                detail=(
                    "the proposal predates immutable registration binding, so its credential "
                    "epoch cannot be inferred from current configuration"
                ),
            )
        # A3/ADR-0048: ask about the exact persisted credential before looking
        # up today's entry by reusable id. Rotation must not hide that the owner
        # revoked the credential which authenticated this queued work.
        if revocation.is_revoked(session, secret_sha256=proposer_credential_epoch):
            _logger.warning(
                "Refused a revoked proposer at STAMP: %s",
                proposer_id,
                extra={"event": "proposer_revoked_refused"},
            )
            return ResolvedIdentity(
                refusal="PROPOSER_REVOKED",
                detail=(
                    f"the credential epoch recorded for {proposer_id} was revoked by the "
                    "owner; revocation is permanent for that credential, and queued work "
                    "does not transfer to a replacement"
                ),
            )
        registration = registry.find(proposer_id)
        if registration is None:
            return ResolvedIdentity(
                detail=(
                    "the proposal's registered proposer is missing from the current registry, "
                    "so identity cannot be stamped; re-registering is an owner act"
                )
            )
        current_binding = proposers.registration_binding(registration)
        if (
            current_binding.credential_epoch != proposer_credential_epoch
            or current_binding.registry_entry_digest != proposer_registry_entry_digest
        ):
            return ResolvedIdentity(
                refusal="PROPOSER_REGISTRATION_REPLACED",
                detail=(
                    "the proposal was authenticated under a different credential epoch or "
                    "registry entry; queued authority does not transfer across replacement"
                ),
            )
        if not registration.is_current(now):
            return ResolvedIdentity(
                detail=(
                    "the proposal's exact registration is disabled or expired, so identity "
                    "cannot be stamped; re-registering or renewing the proposer is an owner act"
                )
            )
        return ResolvedIdentity(
            identity=queue.HarnessIdentity(
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
            ),
            proposer_credential_epoch=proposer_credential_epoch,
            proposer_registry_entry_digest=proposer_registry_entry_digest,
        )

    return _resolve


def order_plane_handoff(runtime: AppRuntime, *, is_writer: Callable[[], bool]) -> Handoff:
    """The full existing pipeline, as one callable: propose → preview → confirm → submit.

    Nothing is skipped. An AI-compiled intent walks the same risk engine,
    preview, confirmation and ten-gate live stack a human proposal walks, and a
    refusal at any of those surfaces back to the cycle — classified, since A1, by
    whether it can prove nothing reached the wire. Autonomy added a gate stack and
    removed none, which is the sentence that has governed every milestone since
    M2.

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

    ## What it returns, since A1

    A :class:`~chronos.supervisor.handoff.HandoffResult`, not the order plane's
    own object. ``service.submit`` **returns** its refusals, and the cycle used
    to read only exceptions — so a read-only lease or a kill switch tripped
    inside the CAS window journaled as ``COMPLETE`` and spent an activity
    attempt. Translating here keeps the supervisor free of order-plane types
    while giving it something it can actually record. The raw
    ``SubmissionOutcome`` is still carried on the result, unread by the
    supervisor.

    Exceptions keep their old shape with one deliberate split. A risk veto and a
    refused confirmation still *raise* — both happen strictly before the wire, so
    "nothing was sent" is provable and the cycle's exception branch records it
    correctly. Anything the **submit call itself** raises is caught and returned
    as ``SENT_AMBIGUOUS``: from outside the boundary a raise mid-submit does not
    prove the wire stayed quiet, and the fail-closed reading of an unknown wire
    is "an order may exist".
    """

    def _submit(intent: WheelOrderIntent) -> HandoffResult:
        service = runtime.order_management
        now = utc_now()
        proposal = service.propose(intent, now=now)
        if not proposal.risk.approved:
            raise RiskRefusedByOrderPlane(proposal.risk.decision_id)
        service.preview(intent, now=utc_now())
        service.confirm(intent, risk_decision_id=proposal.risk.decision_id, now=utc_now())
        try:
            outcome = service.submit(intent, writer_lease_held=is_writer(), now=utc_now())
        except Exception as error:
            # Only the exception's TYPE is recorded. Its message may quote text
            # that arrived with a hostile proposal (R-30), and the journal is
            # append-only: what goes in cannot be taken back out.
            _logger.exception("The autonomous submission call raised")
            return HandoffResult.sent_ambiguous(
                detail=(
                    f"the submission call raised {type(error).__name__}; whether the "
                    "intent reached the venue cannot be established from outside the "
                    "boundary, so it is treated as possibly sent and reconciliation "
                    "owns the truth"
                ),
                refusal_code=SUBMIT_RAISED_CODE,
            )
        return classify_submission_outcome(outcome)

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
    if submitting_posture_is_unauthenticated(settings, loaded.mandate):
        # ADR-0051. Refused BEFORE activation: an unauthenticated submitting
        # grant never becomes an activation row, so nothing downstream can read
        # it as a mandate that was once in force on this posture.
        _logger.critical(
            "Mandate %s is %s, a submitting mode, but this backend's proposer identity "
            "is static (AUTONOMY_PROPOSERS_FILE %s, AUTONOMY_EVIDENCE_BUNDLES %s); "
            "refusing to assemble autonomy. A submitting mandate requires a proposer "
            "registry AND evidence binding, or re-author it at SHADOW",
            loaded.mandate.mandate_id,
            loaded.mandate.mode.value,
            "set" if settings.autonomy_proposers_file is not None else "unset",
            "on" if settings.autonomy_evidence_bundles else "off",
            extra={"event": "autonomy_posture_unauthenticated", "passed": False},
        )
        _alert_unauthenticated_submitting_mandate(runtime, fingerprint, now, loaded.mandate)
        raise UnauthenticatedSubmittingMandate(
            f"mandate {loaded.mandate.mandate_id} is {loaded.mandate.mode.value} but the "
            "proposer posture is static; configure AUTONOMY_PROPOSERS_FILE and "
            "AUTONOMY_EVIDENCE_BUNDLES or author the mandate at SHADOW"
        )
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
        submit=order_plane_handoff(runtime, is_writer=is_writer),
        resolve_identity=build_identity_resolver(settings.autonomy_proposers_file),
        bind_evidence=evidence_binding_in_force(settings),
    )


def evidence_binding_in_force(settings: Settings) -> bool:
    """Whether ADR-0028's evidence binding applies to this backend's drain.

    Two settings decide it, and the pairing is deliberate. A bundle is issued
    **to** a credential, so evidence binding without a proposer registry names
    no author to issue to. That combination does not silently degrade to the
    placeholder posture — :func:`evidence_posture_is_broken` reports it, startup
    raises a CRITICAL owner alert, and the proposal route refuses. Returning
    ``True`` here in that case is intentional: the drain must refuse those
    proposals too, so a queue row that predates the misconfiguration cannot be
    judged under a posture the owner did not get.
    """

    return settings.autonomy_evidence_bundles


def evidence_posture_is_broken(settings: Settings) -> bool:
    """Evidence binding configured with no proposer registry to issue against."""

    return settings.autonomy_evidence_bundles and settings.autonomy_proposers_file is None


class UnauthenticatedSubmittingMandate(RuntimeError):
    """A PAPER- or LIVE-capable mandate met a backend whose proposer identity is static.

    ADR-0051: a mandate in a submitting mode assembles only on the authenticated
    provenance posture — a proposer registry (ADR-0023) AND evidence binding
    (ADR-0028), together. On the static posture the stamped identity is the
    ingress placeholder and admission check 9 compares a constant against
    itself; that is a SHADOW-grade posture, and a decision that can reach the
    wire is not admitted on it. Raised from assembly so the backend boots inert
    with a typed startup fault rather than running a submitting mandate on
    declarations nobody authenticated.
    """


def authenticated_provenance_in_force(settings: Settings) -> bool:
    """Proposals are attributed to a registered credential AND bound to issued evidence.

    Both halves, deliberately: a registry alone authenticates *who* proposed but
    leaves check 9 comparing the placeholder against itself; evidence binding
    alone has no credential to issue a bundle to (:func:`evidence_posture_is_broken`).
    """

    return settings.autonomy_proposers_file is not None and settings.autonomy_evidence_bundles


def submitting_posture_is_unauthenticated(settings: Settings, mandate: AutonomyMandate) -> bool:
    """ADR-0051's cap: a submitting mandate on the static posture may not assemble."""

    return mandate.mode in SUBMITTING_AUTONOMY_MODES and not authenticated_provenance_in_force(
        settings
    )


def alert_broken_evidence_posture(runtime: AppRuntime) -> None:
    """CRITICAL owner alert: evidence binding is on with no proposer registry.

    Same shape as the invalid-registry alert, and for the same reason (ADR-0028
    follows ADR-0023's posture rules verbatim): the backend boots and stays able
    to close positions, every proposal refuses at the route and at the drain, and
    the owner is told without having to read a log file (R-32). A process that
    can still flatten a position never dies because a grant was malformed.
    """

    fingerprint = account_fingerprint(runtime.order_management.account_id)
    try:
        with runtime.database.sessions.begin() as session:
            alerts.raise_alert(
                session,
                account_fingerprint=fingerprint,
                severity=alerts.AlertSeverity.CRITICAL,
                kind="autonomy.evidence_posture_invalid",
                summary=(
                    "AUTONOMY_EVIDENCE_BUNDLES is set with no proposer registry; a bundle "
                    "is issued to a credential, so every proposal refuses until the owner "
                    "configures AUTONOMY_PROPOSERS_FILE or unsets evidence binding"
                ),
                now=utc_now(),
            )
    except Exception:
        _logger.exception("Could not record the broken-evidence-posture alert")


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


def _alert_unauthenticated_submitting_mandate(
    runtime: AppRuntime, fingerprint: str, now: datetime, mandate: AutonomyMandate
) -> None:
    try:
        with runtime.database.sessions.begin() as session:
            alerts.raise_alert(
                session,
                account_fingerprint=fingerprint,
                severity=alerts.AlertSeverity.CRITICAL,
                kind="autonomy.posture_unauthenticated",
                summary=(
                    f"mandate {mandate.mandate_id} is {mandate.mode.value} but this backend "
                    "attributes proposals to a static identity; autonomy is inert until a "
                    "proposer registry and evidence binding are configured, or the mandate "
                    "is re-authored at SHADOW (ADR-0051)"
                ),
                now=now,
            )
    except Exception:
        _logger.exception("Could not record the unauthenticated-posture alert")


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


async def autonomy_tick_task(
    autonomy: AutonomyRuntime,
    *,
    on_progress: Callable[[], None] | None = None,
) -> None:
    """Drive ticks forever, off the event loop's back.

    One-second poll granularity against the runtime's own schedule (whose floor
    is configuration, not this constant). `run_tick` never raises by contract,
    and the loop exits when the runtime stops itself — which it does only after
    consecutive failures, with a CRITICAL alert already raised.
    """

    while not autonomy.stopped:
        if on_progress is not None:
            on_progress()
        now = utc_now()
        wait = autonomy.seconds_until_next_tick(now)
        if wait > 0:
            await asyncio.sleep(min(wait, 1.0))
            continue
        await asyncio.to_thread(autonomy.run_tick, now)
        if on_progress is not None:
            on_progress()
    _logger.warning(
        "Autonomy tick task exiting: the runtime stopped itself",
        extra={"event": "autonomy_task_exit"},
    )

"""The decision-queue writer: identity and attribution a model cannot author (M4).

M2 and M3 both disclosed the same honest bound: the mandate's ``VersionPins``
check proved a decision **agreed** with the pins, not that an approved model
produced it. A model asked to state which model it is could simply claim a
pinned one, so the check was an integrity check wearing authentication's
clothes. This module is the promised upgrade.

## How the boundary actually works

The model process emits a :class:`~chronos.autonomy.decision.ProposedDecision`,
which **has no provenance field and no id**. There is nothing for it to forge.
:func:`accept` — deterministic code, running outside that process — stamps both
and returns the :class:`~chronos.autonomy.decision.AITradeDecision` that the
gateway will judge.

The configuration used for stamping is held *here*, by the harness that actually
loaded the prompt and called the provider. That is the whole mechanism: identity
comes from the thing that knows it first-hand, not from the thing being
identified.

A type cannot enforce who constructs it, and this module does not pretend
otherwise. What makes the boundary real is that the model process is handed a
proposal constructor and no submission path — pinned structurally by the
autonomy isolation tests — so a forged ``AITradeDecision`` built inside the
model process would have nowhere to go.

## Why the writer also owns the id

If a model chose its own ``decision_id`` it could escape R-31's re-submission
bound by picking a fresh one each time: the attempt counters are keyed by id, so
a new id is a new budget, and "a refusal may not be routed around by repetition"
would hold only for a model that cooperated.

So the id is **derived from the proposal's economic content** — a UUIDv5 over
the fields that determine what would actually be traded. Re-proposing the same
trade produces the same id and is recognized as a replay. Rewording the thesis
does not, because narrative is deliberately excluded from the digest: a model
that changes its explanation has not changed its trade.

That closes the dedup residual R-31 has carried since M2, where deduplication
was by id alone and a model re-issuing the same trade under a fresh id was
explicitly not caught.

**Honest bound.** The digest covers economic content, so genuinely different
trades get different ids, as they must. A model that wants a second attempt can
still get one by changing the trade — a different size, a different strike. That
is not a hole: a different trade *is* a different decision, and it faces every
gate again, including the mandate ceilings that bound how much can be attempted
in total. What is closed is the free retry of an *identical* refused trade.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from chronos.autonomy import (
    AITradeDecision,
    DecisionProvenance,
    ProposedDecision,
)
from chronos.persistence import hash_chain

#: A fixed namespace for autonomy decision identity. Never security-bearing:
#: it makes ids deterministic and collision-resistant, not unguessable.
_DECISION_NAMESPACE = uuid.UUID("9d1f4b6e-2c7a-5f80-b3e1-a10de0c1de5a")

#: Hash-chain stream recording every proposal the writer accepted or rejected.
PROPOSAL_STREAM = "autonomy.proposals"


class ProposalRejected(ValueError):
    """The writer refused to stamp a proposal.

    A distinct type because rejection here is categorically different from an
    admission refusal: admission rejects a *decision the system understood*,
    while this rejects something that never became a decision at all.
    """


@dataclass(frozen=True, slots=True)
class HarnessIdentity:
    """What the harness knows first-hand about the model it just called.

    Held by the process that loaded the prompt and made the provider call. The
    model process never receives this, which is what makes the resulting
    provenance an attribution rather than a self-report.
    """

    provider: str
    model_id: str
    model_version: str
    prompt_version: str
    tool_schema_version: str
    decision_schema_version: str
    policy_version: str
    #: The bundle the harness issued for this run, and its digest. Both are
    #: harness facts: a model cannot cite evidence it was not given, because it
    #: does not get to say which bundle it read. The digest is ``None`` in the
    #: placeholder era — no bundle machinery exists yet, and absence is
    #: represented as absence rather than as sixty-four zeros (ADR-0023).
    evidence_bundle_id: str
    evidence_bundle_digest: str | None
    #: Which registered proposer's credential authenticated the submission
    #: (ADR-0023). Empty under the pre-registry static identity.
    proposer_id: str = ""

    def stamp(self, *, produced_at: datetime) -> DecisionProvenance:
        return DecisionProvenance(
            provider=self.provider,
            model_id=self.model_id,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            tool_schema_version=self.tool_schema_version,
            decision_schema_version=self.decision_schema_version,
            policy_version=self.policy_version,
            proposer_id=self.proposer_id,
            evidence_bundle_id=self.evidence_bundle_id,
            evidence_bundle_digest=self.evidence_bundle_digest,
            produced_at=produced_at,
        )


def economic_fingerprint(proposal: ProposedDecision) -> str:
    """A stable digest of what would actually be traded.

    Narrative is excluded on purpose. ``thesis``, ``rationale``,
    ``key_uncertainties`` and ``confidence`` describe *why*, and a model that
    rewrites its explanation has not proposed a different trade — if they were
    included, rewording would mint a fresh id and hand back the retry budget
    this fingerprint exists to take away.

    ``invalidation_conditions`` is likewise excluded even though it is
    load-bearing for review: it is prose, and prose is not an economic term.
    """

    material = "|".join(
        str(part)
        for part in (
            proposal.kind.value,
            proposal.asset_class.value,
            proposal.symbol,
            proposal.futures_root,
            proposal.direction.value,
            proposal.requested_strategy.value if proposal.requested_strategy else "",
            proposal.requested_quantity if proposal.requested_quantity is not None else "",
            (
                proposal.requested_risk_budget_usd
                if proposal.requested_risk_budget_usd is not None
                else ""
            ),
            proposal.target_client_reference or "",
            proposal.max_acceptable_loss_usd
            if proposal.max_acceptable_loss_usd is not None
            else "",
            proposal.protective_order_required,
            _plan_fingerprint(proposal),
        )
    )
    return uuid.uuid5(_DECISION_NAMESPACE, material).hex


def _plan_fingerprint(proposal: ProposedDecision) -> str:
    """Entry and exit plans are economic: a different trigger is a different trade."""

    parts: list[str] = []
    entry = proposal.entry_plan
    if entry is not None and entry.trigger is not None:
        parts.append(
            f"entry:{entry.trigger.comparator.value}:{entry.trigger.reference.value}:"
            f"{entry.trigger.value}"
        )
    exit_plan = proposal.exit_plan
    if exit_plan is not None:
        for label, trigger in (
            ("target", exit_plan.profit_target),
            ("stop", exit_plan.protective_stop),
        ):
            if trigger is not None:
                parts.append(
                    f"{label}:{trigger.comparator.value}:{trigger.reference.value}:{trigger.value}"
                )
    return ";".join(parts)


def accept(
    proposal: ProposedDecision,
    *,
    identity: HarnessIdentity,
    produced_at: datetime,
    session: object | None = None,
    account_fingerprint: str = "",
) -> AITradeDecision:
    """Stamp a proposal into a decision the gateway can judge.

    ``session`` is optional so the writer stays usable — and testable — without
    a database. When one is supplied, the acceptance is recorded in the
    hash-chained proposal stream, so the queue's own history is as tamper-evident
    as the decision journal it feeds.

    Raises :class:`ProposalRejected` rather than returning a sentinel: a
    proposal that cannot be stamped is a programming or configuration error at
    the harness boundary, not a policy outcome the supervisor should record and
    move past. Policy outcomes are admission's job, and conflating the two would
    make a misconfigured harness look like a refused trade.
    """

    if produced_at.tzinfo is None:
        raise ProposalRejected(
            "produced_at must be timezone-aware; a naive timestamp cannot be ordered "
            "against the mandate's effective window"
        )
    if not isinstance(proposal, ProposedDecision):  # pragma: no cover - defensive
        raise ProposalRejected("only a ProposedDecision may be stamped into a decision")
    if isinstance(proposal, AITradeDecision):
        # Already stamped. Re-stamping would let a caller overwrite provenance
        # with a different identity, which is the forgery this module exists to
        # prevent — and it would do so through the very function that is
        # supposed to be the boundary.
        raise ProposalRejected(
            "this decision already carries provenance; re-stamping would overwrite an "
            "existing attribution with a different one"
        )

    decision = AITradeDecision(
        decision_id=economic_fingerprint(proposal),
        provenance=identity.stamp(produced_at=produced_at),
        **proposal.model_dump(),
    )

    if session is not None and account_fingerprint:
        hash_chain.append(
            session,  # type: ignore[arg-type]
            stream=f"{PROPOSAL_STREAM}:{account_fingerprint}",
            kind="proposal_accepted",
            payload={
                "decision_id": decision.decision_id,
                "kind": decision.kind.value,
                "asset_class": decision.asset_class.value,
                "symbol": decision.symbol or decision.futures_root,
                "model_id": identity.model_id,
                "model_version": identity.model_version,
                # Which registered credential authenticated the submission
                # (ADR-0023). Empty under the pre-registry static identity, so
                # the journal distinguishes attributed from anonymous eras.
                "proposer_id": identity.proposer_id,
            },
            recorded_at=produced_at,
        )
    return decision

"""Which economic-looking decision and order-intent fields anything acts on — as data.

:mod:`chronos.autonomy.enforcement` answers this for the mandate. This module
answers it for the two contracts a proposal travels in: the model's
:class:`~chronos.autonomy.decision.ProposedDecision` (with its
:class:`~chronos.autonomy.decision.EntryPlan` and
:class:`~chronos.autonomy.decision.ExitPlan`) and the execution plane's
:class:`~chronos.execution.intents.OrderIntent`.

The gap it records is VCP section 6 finding 7. A decision can say
``protective_order_required=True``, carry a protective stop and a time exit,
and name the most it is willing to lose, and every one of those validates, is
fingerprinted for deduplication (:mod:`chronos.supervisor.queue`), and changes
nothing: no admission check, no sizing input, no order. ADR-0021 is the owner's
open decision about that.

``OrderIntent.stop_price`` is different, and easy to misread as the same gap.
It is ENFORCED: :mod:`chronos.risk.engine` refuses an entry that carries none
(``STOP_REQUIRED``) and sizes the per-trade risk from it. But it is a *risk
input*, not an order. The live plane is limit-only, and no broker adapter sends
a stop, so nothing protects the position at the broker. The pin checks that half
separately, because "enforced" must not be read as "protected".

**This module enforces nothing.** It records, per model, whether a deterministic
module on the path that admits, sizes or transmits reads each field today.

``ENFORCED``
    Read there. For a decision, that means :mod:`chronos.supervisor.admission`,
    ``sizing``, ``durable``, ``compiler`` or ``loop``. For an order intent, the
    live execution modules the pin names, including the risk engine that must
    approve every submission.
``INERT``
    A value there changes no admission, size or order. Being read by the dedup
    fingerprint, by the model's own validators, or by a ledger that stores it
    does not count; none of those acts on it.

Fields that are not economic at all (identity, prose, provenance) are listed in
:data:`NOT_A_LIMIT`, so every field of every model here is in exactly one of the
two maps. ``tests/safety/test_decision_fields_disclosed.py`` pins both maps
against the models, so a field cannot just appear. It also pins them against the
readers' source, so an INERT field that something starts reading fails, and so
does an ENFORCED field nothing reads.

Neither label is a safety claim. ENFORCED says the path consults the field, not
that the resulting behaviour has an exercised test behind it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from chronos.autonomy.enforcement import ENFORCED, INERT

__all__ = [
    "DECISION_FIELD_ENFORCEMENT",
    "ENFORCED",
    "INERT",
    "NOT_A_LIMIT",
    "NOT_TRANSMITTED",
]

#: Every economic-looking field, classified. Keyed by model class name.
DECISION_FIELD_ENFORCEMENT: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "ProposedDecision": MappingProxyType(
            {
                # A request, not a size: supervisor/sizing.py computes and clamps.
                "requested_quantity": ENFORCED,
                # The trigger is compiled against the contract (EntryPlan below).
                "entry_plan": ENFORCED,
                # Fingerprinted by supervisor/queue.py; sizing uses mandate limits.
                "requested_risk_budget_usd": INERT,
                # ADR-0021 (proposed, owner decision required): no admission
                # check, no protective order, no exit. Fingerprint only.
                "exit_plan": INERT,
                "protective_order_required": INERT,
                "max_acceptable_loss_usd": INERT,
            }
        ),
        "EntryPlan": MappingProxyType(
            {
                # supervisor/compiler.py compiles it into the order's trigger.
                "trigger": ENFORCED,
                # Nothing expires an entry at this time.
                "valid_until": INERT,
            }
        ),
        "ExitPlan": MappingProxyType(
            {
                "profit_target": INERT,
                "protective_stop": INERT,
                "time_exit": INERT,
            }
        ),
        "OrderIntent": MappingProxyType(
            {
                "quantity": ENFORCED,
                "limit_price": ENFORCED,
                # risk/engine.py refuses an entry without one (STOP_REQUIRED) and
                # sizes per-trade risk from it. It is NOT transmitted: no broker
                # adapter sends a stop (limit-only plane); see NOT_TRANSMITTED.
                "stop_price": ENFORCED,
            }
        ),
    }
)

#: Fields that carry no economic limit: what the proposal is about, who made it,
#: and why. They are listed so that a NEW field has to be put in one map or the
#: other, rather than arriving unclassified.
NOT_A_LIMIT: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "ProposedDecision": frozenset(
            {
                "kind",
                "asset_class",
                "symbol",
                "futures_root",
                "direction",
                "requested_strategy",
                "time_horizon",
                "target_client_reference",
                "thesis",
                "rationale",
                "confidence",
                "key_uncertainties",
                "evidence",
                "invalidation_conditions",
                "reassess_at",
            }
        ),
        "EntryPlan": frozenset(),
        "ExitPlan": frozenset(),
        "OrderIntent": frozenset(
            {
                "strategy_id",
                "strategy_version",
                "symbol",
                "side",
                "time_in_force",
                "decision_timestamp_utc",
                "source_bar_sequence_id",
                "proposal_reason",
            }
        ),
    }
)

#: OrderIntent fields a risk check reads but no broker adapter sends. Recorded
#: because ENFORCED above is easy to read as "protected": a stop price that the
#: risk engine requires still places no stop order.
NOT_TRANSMITTED: frozenset[str] = frozenset({"stop_price"})

"""Which mandate limits the deterministic kernel actually reads — as data.

An :class:`~chronos.autonomy.mandate.AutonomyMandate` is a *contract*, and a
contract can carry a field that nothing acts on. Four whole limit groups were
once in exactly that state while the mandate docstring implied the supervisor
re-derived them all; the M2 adversarial review found it. That is the same shape
as this repository's four inert kernel defects (R-24 … R-27): a control that is
written down, validated, and unable to change any outcome.

The prose disclosure lives in :mod:`chronos.supervisor.admission`, where a
reader of the gateway will find it. This module is the same fact in a form code
can read, so three consumers stay in agreement instead of drifting:

* ``tests/safety/test_supervisor_gateway.py`` pins it against the mandate models
  themselves — a limit that appears without being classified fails there;
* the same test pins it against the kernel's own source — a field classified
  INERT that some module starts reading fails there, and vice versa;
* ``chronos.cli.mandate_check`` reports it to the owner at authoring time, so a
  number typed into an inert field is called out before it is relied on.

**This module enforces nothing.** It records, per limits model, whether some
deterministic module reads each field today. ``ENFORCED`` means read by
:mod:`chronos.supervisor.sizing`, :mod:`chronos.supervisor.admission`, or
:mod:`chronos.supervisor.durable`. ``INERT`` means a value the owner writes
there changes no decision — the mandate will still validate, activate, and look
complete.

Neither label is a safety claim. ENFORCED says the kernel consults the field,
not that the resulting refusal has an exercised test behind it; that evidence is
the individual control's own.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: Some deterministic module reads this field today.
ENFORCED = "ENFORCED"

#: Nothing reads this field. Setting it constrains nothing.
INERT = "INERT"

#: Every field of every mandate limits model, classified. Keyed by the model's
#: class name in :mod:`chronos.autonomy.mandate`.
#:
#: An earlier version of this map listed inert names by hand inside the test
#: that used it, which caught a field becoming *enforced* but not a field being
#: *added* — a new limit could arrive inert and undisclosed without failing
#: anything. It is compared against the models themselves now, so an
#: unclassified field is a test failure.
LIMIT_ENFORCEMENT: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "CapitalLimits": MappingProxyType(
            {
                # ADR-0017: the owner's model-self-sizing grant. Read by sizing
                # (it decides whether an unset ceiling binds), so it is ENFORCED
                # in the sense this map means — the kernel consults it.
                "model_discretion": ENFORCED,
                "allocated_capital_usd": ENFORCED,
                "max_order_notional_usd": ENFORCED,
                "max_position_notional_usd": ENFORCED,
                "max_gross_exposure_usd": ENFORCED,
                "max_net_exposure_usd": ENFORCED,
                "max_contracts_per_order": ENFORCED,
                "max_shares_per_order": ENFORCED,
                "max_leverage": ENFORCED,
                "max_margin_utilization_pct": ENFORCED,
                "min_buying_power_usd": ENFORCED,
                "min_cash_floor_usd": ENFORCED,
            }
        ),
        "LossLimits": MappingProxyType(
            {
                # M3: enforced against durable per-session counters in
                # supervisor.durable, which turns a breach into a DegradedReason
                # that stops new exposure.
                "max_session_loss_usd": ENFORCED,
                "max_daily_loss_usd": ENFORCED,
                "max_peak_to_trough_drawdown_usd": ENFORCED,
                "max_peak_to_trough_drawdown_pct": ENFORCED,
            }
        ),
        "ConcentrationLimits": MappingProxyType(
            {
                "max_symbol_exposure_pct": ENFORCED,
                # Need a sector/family/correlation map Chronos does not have yet.
                "max_sector_exposure_pct": INERT,
                "max_family_exposure_pct": INERT,
                "max_correlated_exposure_pct": INERT,
            }
        ),
        "ActivityLimits": MappingProxyType(
            {
                # M3: enforced against durable per-session counters (see LossLimits).
                "max_orders_per_session": ENFORCED,
                "max_cancellations_per_session": ENFORCED,
                "max_replacements_per_session": ENFORCED,
                "max_turnover_usd_per_session": ENFORCED,
            }
        ),
        "MarketDataRequirements": MappingProxyType(
            {
                "max_quote_age_seconds": ENFORCED,
                "permitted_data_qualities": ENFORCED,
                "max_relative_spread": ENFORCED,
                # Need option-chain evidence the supervisor does not gather yet.
                "min_option_volume": INERT,
                "min_open_interest": INERT,
            }
        ),
        "SessionPolicy": MappingProxyType(
            {
                # Need a session clock in the supervisor; the orders plane has its own.
                "permitted_sessions": INERT,
                "allow_overnight_holding": INERT,
            }
        ),
    }
)

#: Scope fields no deterministic module matches a contract against. Empty, and
#: deliberately kept rather than deleted.
#:
#: The first draft of this module listed ``exchanges`` and ``contract_families``
#: here, on the strength of the disclosure in
#: :mod:`chronos.supervisor.admission`, which still said no compilation step
#: existed. One does: ``chronos.supervisor.compiler`` refuses
#: ``EXCHANGE_NOT_PERMITTED`` and ``FAMILY_NOT_PERMITTED`` against the qualified
#: contract (M4), and ``docs/limitations.md`` had already been corrected. Both
#: fields bind. Reporting them as inert would have told an owner that a
#: restriction which does hold constrains nothing — the same error as the one
#: this module exists to catch, pointing the other way.
#:
#: The lesson is why this stays: a scope field is inert only if no module in
#: admission, sizing, durable **or compiler** reads it. Check the compiler too.
INERT_SCOPE_FIELDS: Mapping[str, str] = MappingProxyType({})


def enforcement_of(model_name: str, field: str) -> str | None:
    """Return ``ENFORCED``/``INERT`` for one field, or ``None`` if unclassified.

    ``None`` is the honest answer for a name this map has never heard of; it is
    not a claim that the field is enforced. The safety pin makes an
    unclassified *mandate* field a test failure, so a ``None`` here means the
    caller asked about something that is not a mandate limit.
    """

    return LIMIT_ENFORCEMENT.get(model_name, {}).get(field)


def inert_fields(model_name: str) -> tuple[str, ...]:
    """Field names of ``model_name`` that no deterministic module reads."""

    classified = LIMIT_ENFORCEMENT.get(model_name, {})
    return tuple(field for field, status in classified.items() if status == INERT)

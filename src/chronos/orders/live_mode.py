"""Live-submission grant for the orders plane (ADR-0009 §3).

A deny-by-default mirror of the paper mode lock's multi-condition shape, owned
by ``chronos.orders`` so the autonomous plane's hard lock
(``chronos.control.modes``, ADR-0007) stays byte-identical: that lock continues
to refuse CANARY_LIVE/LIVE unconditionally, and this module grants nothing
unless every condition below independently passes against FRESH broker
evidence supplied by the caller — the submission boundary re-reads connection
status and managed accounts inside ``submit()``, never trusting values bound at
startup (a settings echo is not broker evidence).
"""

from __future__ import annotations

from dataclasses import dataclass

from chronos.domain.accounts import (
    ObservedEnvironment,
    is_live_account_id,
    is_paper_account_id,
)


@dataclass(frozen=True, slots=True)
class LiveSubmissionGrant:
    """Immutable evidence of whether live submission is permitted right now.

    ``denial_reasons`` is non-empty whenever the grant was refused; it is
    surfaced verbatim in the submission refusal detail.
    """

    live_account_id: str | None
    denial_reasons: tuple[str, ...]

    @property
    def may_submit_live(self) -> bool:
        return not self.denial_reasons and self.live_account_id is not None


def resolve_live_submission(
    *,
    order_transmission_enabled: bool,
    live_trading_enabled: bool,
    live_account_allowlist: tuple[str, ...],
    broker_reported_account_id: str | None,
    broker_observed_environment: ObservedEnvironment | None,
) -> LiveSubmissionGrant:
    """Derive the live grant the evidence supports, denying by default."""

    reasons: list[str] = []

    if not order_transmission_enabled:
        reasons.append("order transmission is disabled (ALLOW_ORDER_TRANSMIT is false)")
    if not live_trading_enabled:
        reasons.append("live trading is disabled (ALLOW_LIVE_TRADING is false)")
    if not live_account_allowlist:
        reasons.append("live account allowlist is empty")

    account = (broker_reported_account_id or "").strip()
    if not account:
        reasons.append("broker did not report an account id")
    else:
        if account not in live_account_allowlist:
            reasons.append("broker-reported account id is not on the live allowlist")
        if is_paper_account_id(account):
            reasons.append(
                "broker-reported account id is an IBKR PAPER account; refusing live/paper confusion"
            )
        elif not is_live_account_id(account):
            reasons.append(
                "broker-reported account id does not match the IBKR live account pattern"
            )

    if broker_observed_environment is not ObservedEnvironment.LIVE:
        observed = (
            "unverified"
            if broker_observed_environment is None
            else broker_observed_environment.value
        )
        reasons.append(
            f"broker-observed environment is not verified as live (observed: {observed})"
        )

    if reasons:
        return LiveSubmissionGrant(live_account_id=None, denial_reasons=tuple(reasons))
    return LiveSubmissionGrant(live_account_id=account, denial_reasons=())

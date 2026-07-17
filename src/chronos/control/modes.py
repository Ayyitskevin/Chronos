"""Operating modes and the multi-condition mode lock (ADR-0007).

Modes:

RESEARCH, BACKTEST, REPLAY  — no broker connection, no order capability.
SHADOW                      — live/replayed data, produces order intents,
                              cannot submit anywhere.
PAPER                       — may submit only to an explicitly verified IBKR
                              paper account, only when every paper lock passes.
CANARY_LIVE, LIVE           — recognized vocabulary, REFUSED by this build:
                              resolving a live-capable mode always returns a
                              lock whose capability is DENIED_LIVE_DISABLED.

The lock is deliberately not a boolean. The execution engine requires a
``ModeLock`` whose ``capability`` grants submission, and the only constructor
is ``resolve_mode_lock``, which re-derives everything from configuration and
runtime evidence. One mistyped environment variable cannot produce a
submission-capable lock: PAPER additionally requires the broker-reported
account id to appear on the operator-maintained paper allowlist AND the
broker-reported environment to be paper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TradingMode(StrEnum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    REPLAY = "replay"
    SHADOW = "shadow"
    PAPER = "paper"
    CANARY_LIVE = "canary_live"
    LIVE = "live"


class ExecutionCapability(StrEnum):
    """What the resolved mode permits the execution plane to do."""

    NO_ORDERS = "NO_ORDERS"
    SIMULATED_ONLY = "SIMULATED_ONLY"
    PAPER_SUBMISSION = "PAPER_SUBMISSION"
    DENIED_LIVE_DISABLED = "DENIED_LIVE_DISABLED"


_SIMULATED_MODES = frozenset({TradingMode.BACKTEST, TradingMode.REPLAY})
_LIVE_MODES = frozenset({TradingMode.CANARY_LIVE, TradingMode.LIVE})


@dataclass(frozen=True, slots=True)
class ModeLock:
    """Immutable evidence of what the current process may do.

    ``denial_reasons`` is non-empty whenever a requested capability was
    downgraded; it is surfaced verbatim by the CLI and monitoring page.
    """

    mode: TradingMode
    capability: ExecutionCapability
    paper_account_id: str | None
    denial_reasons: tuple[str, ...]

    @property
    def may_submit_paper(self) -> bool:
        return (
            self.capability is ExecutionCapability.PAPER_SUBMISSION
            and self.paper_account_id is not None
        )


def resolve_mode_lock(
    *,
    requested_mode: TradingMode,
    paper_account_allowlist: tuple[str, ...],
    broker_reported_account_id: str | None,
    broker_reported_environment_is_paper: bool | None,
    order_transmission_enabled: bool,
) -> ModeLock:
    """Derive the strongest capability the evidence supports, denying by default."""

    reasons: list[str] = []

    if requested_mode in _LIVE_MODES:
        return ModeLock(
            mode=requested_mode,
            capability=ExecutionCapability.DENIED_LIVE_DISABLED,
            paper_account_id=None,
            denial_reasons=(
                "live-capable modes are hard-disabled in this build; "
                "no configuration can enable them",
            ),
        )

    if requested_mode in (TradingMode.RESEARCH, TradingMode.SHADOW):
        return ModeLock(
            mode=requested_mode,
            capability=ExecutionCapability.NO_ORDERS,
            paper_account_id=None,
            denial_reasons=(),
        )

    if requested_mode in _SIMULATED_MODES:
        return ModeLock(
            mode=requested_mode,
            capability=ExecutionCapability.SIMULATED_ONLY,
            paper_account_id=None,
            denial_reasons=(),
        )

    # PAPER: every condition below must independently pass.
    if not order_transmission_enabled:
        reasons.append("order transmission is disabled (ALLOW_ORDER_TRANSMIT is false)")
    if not paper_account_allowlist:
        reasons.append("paper account allowlist is empty")
    if broker_reported_account_id is None or not broker_reported_account_id.strip():
        reasons.append("broker did not report an account id")
    if broker_reported_environment_is_paper is not True:
        reasons.append("broker-reported environment is not verified as paper")
    if (
        broker_reported_account_id is not None
        and broker_reported_account_id not in paper_account_allowlist
    ):
        reasons.append("broker-reported account id is not on the paper allowlist")
    # IBKR paper account ids match DU/DF + digits (e.g. DU1234567). Anything
    # else is treated as evidence of live/paper confusion and fails closed.
    if broker_reported_account_id is not None and not re.fullmatch(
        r"D[UF]\d{4,}", broker_reported_account_id
    ):
        reasons.append("broker-reported account id does not match the IBKR paper account pattern")

    if reasons:
        return ModeLock(
            mode=TradingMode.PAPER,
            capability=ExecutionCapability.NO_ORDERS,
            paper_account_id=None,
            denial_reasons=tuple(reasons),
        )
    return ModeLock(
        mode=TradingMode.PAPER,
        capability=ExecutionCapability.PAPER_SUBMISSION,
        paper_account_id=broker_reported_account_id,
        denial_reasons=(),
    )

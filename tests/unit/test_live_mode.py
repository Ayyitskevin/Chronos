"""ADR-0009 §3: the orders-plane live grant and observed-environment evidence.

Deny-by-default: every condition independently refuses, a paper account on the
live allowlist is refused by pattern, and the autonomous plane's hard lock is
untouched (its own tests keep proving CANARY_LIVE/LIVE stay denied there).
"""

from __future__ import annotations

import pytest

from chronos.domain.accounts import (
    ObservedEnvironment,
    classify_observed_environment,
    is_live_account_id,
    is_paper_account_id,
)
from chronos.orders.live_mode import LiveSubmissionGrant, resolve_live_submission


def _grant(**overrides: object) -> LiveSubmissionGrant:
    kwargs: dict[str, object] = {
        "order_transmission_enabled": True,
        "live_trading_enabled": True,
        "live_account_allowlist": ("U7654321",),
        "broker_reported_account_id": "U7654321",
        "broker_observed_environment": ObservedEnvironment.LIVE,
    }
    kwargs.update(overrides)
    return resolve_live_submission(**kwargs)  # type: ignore[arg-type]


# --- account pattern vocabulary -------------------------------------------


@pytest.mark.parametrize(
    ("account", "live", "paper"),
    [
        ("U7654321", True, False),
        ("U1234", True, False),
        ("DU1234567", False, True),
        ("DF123456", False, True),
        ("u7654321", False, False),  # case matters: not a verified live id
        ("U123", False, False),  # too short
        ("X9999999", False, False),
        ("", False, False),
        ("  U7654321  ", True, False),  # strip, then full-match
    ],
)
def test_account_patterns(account: str, live: bool, paper: bool) -> None:
    assert is_live_account_id(account) is live
    assert is_paper_account_id(account) is paper


@pytest.mark.parametrize(
    ("accounts", "expected"),
    [
        (("U7654321",), ObservedEnvironment.LIVE),
        (("U7654321", "U7654322"), ObservedEnvironment.LIVE),
        (("DU1234567",), ObservedEnvironment.PAPER),
        (("U7654321", "DU1234567"), ObservedEnvironment.PAPER),  # any paper => paper
        ((), ObservedEnvironment.UNKNOWN),
        (("",), ObservedEnvironment.UNKNOWN),
        (("U7654321", "X999"), ObservedEnvironment.UNKNOWN),
        (("FA-ADVISOR",), ObservedEnvironment.UNKNOWN),
    ],
)
def test_classify_observed_environment(
    accounts: tuple[str, ...], expected: ObservedEnvironment
) -> None:
    assert classify_observed_environment(accounts) is expected


# --- the grant ------------------------------------------------------------


def test_full_evidence_grants_live_submission() -> None:
    grant = _grant()
    assert grant.may_submit_live
    assert grant.live_account_id == "U7654321"
    assert grant.denial_reasons == ()


@pytest.mark.parametrize(
    ("override", "reason_fragment"),
    [
        ({"order_transmission_enabled": False}, "ALLOW_ORDER_TRANSMIT"),
        ({"live_trading_enabled": False}, "ALLOW_LIVE_TRADING"),
        ({"live_account_allowlist": ()}, "allowlist is empty"),
        ({"broker_reported_account_id": None}, "did not report an account id"),
        ({"broker_reported_account_id": "   "}, "did not report an account id"),
        ({"broker_reported_account_id": "U9999999"}, "not on the live allowlist"),
        ({"broker_observed_environment": ObservedEnvironment.PAPER}, "observed: paper"),
        ({"broker_observed_environment": ObservedEnvironment.UNKNOWN}, "observed: unknown"),
        ({"broker_observed_environment": None}, "observed: unverified"),
    ],
)
def test_each_condition_independently_denies(
    override: dict[str, object], reason_fragment: str
) -> None:
    grant = _grant(**override)
    assert not grant.may_submit_live
    assert grant.live_account_id is None
    assert any(reason_fragment in reason for reason in grant.denial_reasons)


def test_paper_account_on_live_allowlist_is_denied_by_pattern() -> None:
    # The operator mistake ADR-0009 names explicitly: a DU account allowlisted
    # for live must still be refused, with the confusion called out.
    grant = _grant(
        live_account_allowlist=("DU1234567",),
        broker_reported_account_id="DU1234567",
    )
    assert not grant.may_submit_live
    assert any("PAPER account" in reason for reason in grant.denial_reasons)


def test_malformed_account_on_live_allowlist_is_denied_by_pattern() -> None:
    grant = _grant(
        live_account_allowlist=("X9999999",),
        broker_reported_account_id="X9999999",
    )
    assert not grant.may_submit_live
    assert any("live account pattern" in reason for reason in grant.denial_reasons)


def test_multiple_failures_report_every_reason() -> None:
    grant = _grant(
        order_transmission_enabled=False,
        live_trading_enabled=False,
        broker_observed_environment=ObservedEnvironment.PAPER,
    )
    assert not grant.may_submit_live
    assert len(grant.denial_reasons) == 3


def test_grant_is_immutable() -> None:
    grant = _grant()
    with pytest.raises(AttributeError):
        grant.live_account_id = "U0000000"  # type: ignore[misc]


def test_live_mode_imports_nothing_from_the_autonomous_plane() -> None:
    # ADR-0009 §3: the orders-plane grant must not import the autonomous
    # plane's lock (whose hard live-denial is proven by its own tests).
    import ast
    from pathlib import Path

    import chronos.orders.live_mode as live_mode

    tree = ast.parse(Path(live_mode.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("chronos.control", "chronos.execution", "chronos.risk")
    assert not [name for name in imported if name.startswith(forbidden)]

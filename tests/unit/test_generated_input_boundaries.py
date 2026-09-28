"""Generated-input pins at two untrusted boundaries (VCP §6 EXIT "fuzz" class,
tests-only half — flow-team TG3A, from READ-EXIT6 P-G3a).

Two boundaries take bytes the operator or the broker controls, and both already
fail closed by design; these pins hold that under generated input rather than
chosen examples:

1. ``broker_status_to_lifecycle`` maps an IBKR order-status *string* — plus the
   fill quantities the broker reports alongside it — to a lifecycle. An
   unrecognized status with NO partial-fill shape (one of filled/remaining is
   zero) must land ``SUBMISSION_UNKNOWN`` (reconciled, never assumed benign),
   and it must never raise. Mutation that turns this red: mapping unknown
   strings to ``SUBMITTED``. The D-27 shape — an unrecognized status with
   filled>0 AND remaining>0, which today falls through the partial-fill
   heuristic — is deliberately NOT pinned here; it is the D-27 finding and is
   held red inside FIX-27 (Muse ruling 2026-09-28: land the green pins alone).
2. Terminal statuses are decided FIRST: a cancel-shaped status with
   filled>0 AND remaining>0 is a partial fill that was then cancelled, and must
   land ``CANCELLED`` — never ``PARTIALLY_FILLED``, which would wedge the order
   out of every terminal state. Mutation that turns this red: evaluating the
   partial heuristic before the cancel check.
3. The terminal command grammar (``chronos.terminal.commands.parse``) over
   control/whitespace-dominated generated input: it never raises and never
   partially applies — tokens after the command reach ``args`` verbatim and in
   order (folding or dropping them is midas's bug shape). Mutation that turns
   this red: normalizing ``args`` (e.g. upper-casing) when building the
   ``ParsedCommand``.

``tests/unit/test_terminal_commands.py`` already proves totality over
``st.text()``; 3a/3b here aim at the control/whitespace class and the
verbatim-args invariant specifically, so each pin has a mutation the existing
suite does not.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from chronos.domain.enums import OrderLifecycle
from chronos.orders.tracker import broker_status_to_lifecycle
from chronos.terminal.commands import COMMANDS, ParsedCommand, parse, resolve

# The normalized spellings broker_status_to_lifecycle recognizes, stated
# independently of the implementation so the pin fails when the implementation
# drifts rather than tracking it.
_KNOWN_STATUSES = {
    "cancelled",
    "canceled",
    "apicancelled",
    "apicanceled",
    "inactive",
    "rejected",
    "apirejected",
    "pendingcancel",
    "filled",
    "partiallyfilled",
    "submitted",
    "presubmitted",
    "pendingsubmit",
}

_CANCEL_SHAPED = st.sampled_from(["cancelled", "canceled", "apicancelled", "apicanceled"])
_NONNEG_DECIMAL = st.decimals(min_value=0, max_value=Decimal("1e9"))
_POSITIVE_DECIMAL = st.decimals(min_value=Decimal("1e-9"), max_value=Decimal("1e9"))
_WHITESPACE = st.text(
    alphabet=st.characters(whitelist_categories=("Zs", "Zl", "Zp"), max_codepoint=0xFFFF)
)


@settings(max_examples=200)
@given(
    status=st.text(),
    zero_side=st.sampled_from(["filled", "remaining"]),
    other=_NONNEG_DECIMAL,
)
def test_1_unknown_status_fails_closed_and_never_raises(
    status: str, zero_side: str, other: Decimal
) -> None:
    # D-27 probe-proven caution: an unrecognized status WITH a partial-fill
    # shape (filled>0 AND remaining>0) currently maps to PARTIALLY_FILLED —
    # that shape is the D-27 finding, pinned red inside FIX-27, not here. The
    # live pin holds the no-partial-shape class by zeroing one side.
    filled = Decimal(0) if zero_side == "filled" else other
    remaining = Decimal(0) if zero_side == "remaining" else other
    result = broker_status_to_lifecycle(
        status, filled_quantity=filled, remaining_quantity=remaining
    )
    assert isinstance(result, OrderLifecycle)
    if status.strip().lower() not in _KNOWN_STATUSES:
        assert result is OrderLifecycle.SUBMISSION_UNKNOWN, (
            f"unrecognized status {status!r} mapped to {result}, "
            "an unrecognized broker state must be reconciled, never assumed benign"
        )


@settings(max_examples=150)
@given(
    status=_CANCEL_SHAPED,
    pad_left=_WHITESPACE,
    pad_right=_WHITESPACE,
    shout=st.booleans(),
    filled=_POSITIVE_DECIMAL,
    remaining=_POSITIVE_DECIMAL,
)
def test_2_cancel_shaped_status_with_fills_is_terminal_never_partial(
    status: str, pad_left: str, pad_right: str, shout: bool, filled: Decimal, remaining: Decimal
) -> None:
    if shout:
        status = status.upper()
    result = broker_status_to_lifecycle(
        pad_left + status + pad_right, filled_quantity=filled, remaining_quantity=remaining
    )
    assert result is OrderLifecycle.CANCELLED, (
        f"{status!r} with filled={filled} remaining={remaining} mapped to {result}; "
        "a partial fill that was then cancelled must reach a terminal state"
    )


_CONTROL_HEAVY = st.text(
    alphabet=st.one_of(
        st.characters(categories=("Cc", "Cf", "Zs", "Zl", "Zp")),
        st.characters(whitelist_categories=("L", "N")),
    )
)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(line=_CONTROL_HEAVY)
def test_3a_parse_never_raises_over_control_and_whitespace_input(line: str) -> None:
    parsed = parse(line)
    assert isinstance(parsed, ParsedCommand)
    assert parsed.raw == line.strip()
    assert parsed.command is None or parsed.command in COMMANDS
    assert all(token in line for token in parsed.args)
    assert parsed.symbol == "" or parsed.symbol in line


_WORD = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1, max_size=8
)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    prefix=st.lists(_WORD, max_size=3),
    command=st.sampled_from([token for c in COMMANDS for token in (c.code, *c.aliases)]),
    suffix=st.lists(_WORD, min_size=1, max_size=4),
)
def test_3b_args_after_the_command_arrive_verbatim_never_partially_applied(
    prefix: list[str], command: str, suffix: list[str]
) -> None:
    # A suffix token that itself resolves would supersede the command by the
    # grammar's last-resolving-token rule; that shape belongs to the existing
    # trailing-token test, not to this pin.
    assume(all(resolve(token) is None for token in suffix))
    line = " ".join([*prefix, command, *suffix])
    parsed = parse(line)
    assert parsed.command is resolve(command)
    assert parsed.args == tuple(suffix), (
        f"args {parsed.args} != suffix {tuple(suffix)}: tokens after the command "
        "must reach the command verbatim — folded or dropped tokens are a partial apply"
    )


@settings(max_examples=200)
@given(status=st.text(), filled=_POSITIVE_DECIMAL, remaining=_POSITIVE_DECIMAL)
def test_4_d27_unknown_status_with_partial_shape_fails_closed(
    status: str, filled: Decimal, remaining: Decimal
) -> None:
    # FIX-27 / D-27: the TG3A-removed shape. An unrecognized status WITH a
    # partial-fill shape (filled>0 AND remaining>0) must still land
    # SUBMISSION_UNKNOWN — the quantity heuristic is scoped to known working
    # statuses, so a novel broker status is never granted working-order
    # authority from quantities alone. Status-first short-circuit: quantities
    # are never compared for an unrecognized status.
    result = broker_status_to_lifecycle(
        status, filled_quantity=filled, remaining_quantity=remaining
    )
    assert isinstance(result, OrderLifecycle)
    if status.strip().lower() not in _KNOWN_STATUSES:
        assert result is OrderLifecycle.SUBMISSION_UNKNOWN, (
            f"unrecognized status {status!r} with filled={filled} remaining={remaining} "
            f"mapped to {result}; quantities must not upgrade an unknown status"
        )


def test_5_d27_unknown_status_nonfinite_quantities_fail_closed() -> None:
    # Threat-map row: "regardless of quantities" — a NaN comparison would raise
    # InvalidOperation if the heuristic ever evaluated it; status-first
    # short-circuiting must return SUBMISSION_UNKNOWN deterministically.
    for qty in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        result = broker_status_to_lifecycle(
            "FutureWorkingState", filled_quantity=qty, remaining_quantity=qty
        )
        assert result is OrderLifecycle.SUBMISSION_UNKNOWN, (
            f"unknown status with quantity {qty} mapped to {result}"
        )

"""``RiskPolicy`` may not carry an undisclosed field that no code reads.

AGENTS.md:29-30: "Every economic-looking field must be mechanically enforced, explicitly
advisory, or forbidden. Inert authority, risk, exit, or protection fields are release
blockers."  A read-only review at ``a1a9f59`` (2026-09-11) found eight ``RiskPolicy``
fields that nothing under ``src/chronos`` reads: the schema validates them, ``config_hash``
digests them, every checked-in profile sets them, and none of them can change a decision.
One has since moved: ``allow_margin`` is ENFORCED as of 2026-09-12 (``MARGIN_FORBIDDEN``,
P3-B-1) — this pin failed in its designed direction when the engine first named the field,
and was reclassified only after that failure was recorded. Seven remain inert.

They are not a live defect. The deterministic platform's mode lock hard-denies live
(ADR-0007), the sizer cannot create a short (ADR-0004 §2), ``OrderIntent`` has no market
order type, and the plane has no option intent -- so most of what the names promise is
true by structure.  But a YAML that says ``allow_margin: false`` reads as a control, and
the failure this repository keeps repeating (R-24..R-27, ``max_opening_orders_per_day``)
is a field everyone assumed something read.  Choosing enforce / advisory / remove for each
one is a capital and risk-limit decision (AGENTS.md; docs/AGENT_PROTOCOL.md §5, §9) and
therefore the owner's.  Until it is made, this file makes the interim state mechanical:
the exact set is pinned, disclosed in ``docs/RISK_POLICY.md``, and cannot move in either
direction without failing here.

Mirrors ``test_five_tool_inert_fields_disclosed.py`` and the mandate-plane pin in
``test_supervisor_gateway.py`` / ``chronos.autonomy.enforcement``.  The classification
lives in this test rather than in a ``chronos.risk`` module because the risk plane has no
owner-time surface (no policy-check CLI) to report it through; moving it into ``src``
belongs to the disposition change, not to this disclosure.

**Honest residual -- read before trusting a READ verdict.**  The read scanner matches
attribute *names* across ``src/chronos``, not accesses resolved to a type.  Four enforced
names are also read on other objects (``max_daily_loss_usd`` and
``max_position_notional_usd`` on mandate limits, ``max_quote_age_seconds`` on the mandate
and the orders plane, ``policy_version`` on provenance pins), so a dropped engine read of
one of those would still score READ here.  The breach-implies-deny suite in
``tests/platform_unit/test_risk_engine_limits.py`` is what pins those reads.  The scanner
over-reports reads, so it is a floor on the inert set.  The inert direction uses the
stricter mechanism -- any mention of the name in source text -- so a field that starts
being read through ``getattr``, ``model_dump()[...]``, or a keyword argument fails here
too.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import chronos
from chronos.risk.policy import RiskPolicy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = Path(chronos.__file__).resolve().parent
_POLICY_MODULE = _SRC_ROOT / "risk" / "policy.py"
_DISCLOSURE = _REPO_ROOT / "docs" / "RISK_POLICY.md"
_DISCLOSURE_PHRASE = "declared, not enforced"

#: Some module under ``src/chronos`` reads this field today.
ENFORCED = "ENFORCED"

#: Nothing reads this field. Setting it constrains nothing.
INERT = "INERT"

#: Every ``RiskPolicy`` field, classified.  Compared against the model itself, so a field
#: that is added without being classified fails, a field classified INERT that some module
#: starts reading fails, and a field classified ENFORCED that nothing reads fails.
#:
#: ENFORCED is not a safety claim: it says the engine consults the field, not that the
#: resulting refusal has an exercised test behind it (those live in
#: ``tests/platform_unit/test_risk_engine_limits.py`` and
#: ``tests/safety/test_safety_invariants.py``).
POLICY_ENFORCEMENT = MappingProxyType(
    {
        # Stamped on every decision and approval (engine.py: RiskDecision / RiskApproval).
        "policy_version": ENFORCED,
        # Identity / permission.
        "allowed_symbols": ENFORCED,
        "allowed_strategy_ids": ENFORCED,
        "allow_long_entries": ENFORCED,
        # Sells beyond held shares are denied structurally (SELL_WITHOUT_POSITION) and the
        # sizer cannot create a short (ADR-0004 §2); this flag itself is read by nothing.
        "allow_short_entries": INERT,
        # Capital and exposure.
        "max_bot_capital_usd": ENFORCED,
        "max_position_notional_usd": ENFORCED,
        "max_aggregate_exposure_usd": ENFORCED,
        "max_symbol_exposure_fraction": ENFORCED,
        "max_risk_per_trade_fraction": ENFORCED,
        "max_simultaneous_positions": ENFORCED,
        "max_open_orders": ENFORCED,
        # Loss limits.
        "max_daily_loss_usd": ENFORCED,
        "max_weekly_loss_usd": ENFORCED,
        "max_drawdown_fraction": ENFORCED,
        "max_consecutive_losses": ENFORCED,
        # Data freshness.
        "max_quote_age_seconds": ENFORCED,
        "max_bar_age_seconds": ENFORCED,
        "max_price_deviation_fraction": ENFORCED,
        # Operational counters.  The backtest counts ``risk_rejections`` but compares the
        # count to nothing; no caller supplies a per-day rejection count to the engine.
        "max_order_rejections_per_day": INERT,
        # A halt clears only by an explicit operator rearm (control.halt); no bar-counted
        # cooldown exists after it.
        "cooldown_bars_after_loss_halt": INERT,
        # Behavioral prohibitions.
        # No market order type exists in this plane (execution.intents.OrderIntent is
        # limit-only); the autonomy plane's protected MARKET form is granted by the
        # mandate's ``order_forms``, not by this flag.
        "allow_market_orders": INERT,
        # MARGIN_FORBIDDEN: an entry whose notional exceeds ``AccountView.cash_usd`` is
        # denied unless the flag is set (engine.py, 2026-09-12). Breach ⇒ deny, the flag
        # lifting it, and the strict-above-cash boundary are pinned in
        # ``tests/platform_unit/test_risk_engine_limits.py``.
        "allow_margin": ENFORCED,
        # No session clock and no end-of-session flatten exist in this plane; positions
        # are always carried overnight, whatever the flag says.
        "allow_overnight_positions": INERT,
        # Any add to an existing position is denied by PYRAMIDING_FORBIDDEN while
        # ``allow_pyramiding`` is False; nothing distinguishes an add below cost.
        "allow_averaging_down": INERT,
        "allow_pyramiding": ENFORCED,
        # This plane has no option intent (docs/RISK_POLICY.md "Options").
        "allow_options": INERT,
    }
)

INERT_FIELDS: frozenset[str] = frozenset(
    field for field, status in POLICY_ENFORCEMENT.items() if status == INERT
)


def _source_files() -> list[Path]:
    """Every module under ``src/chronos`` except the schema that declares the fields."""

    return sorted(path for path in _SRC_ROOT.rglob("*.py") if path != _POLICY_MODULE)


def _attribute_names_read() -> set[str]:
    """Every attribute name loaded anywhere under ``src/chronos``, plus literal getattr."""

    names: set[str] = set()
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
    return names


def _modules_mentioning(name: str) -> list[str]:
    """Source files whose text contains ``name`` at all -- the strict read test."""

    return [
        str(path.relative_to(_REPO_ROOT))
        for path in _source_files()
        if name in path.read_text(encoding="utf-8")
    ]


def test_every_risk_policy_field_is_classified_enforced_or_inert() -> None:
    """A policy field cannot just appear: it must be declared one way or the other."""

    actual = set(RiskPolicy.model_fields)
    declared = set(POLICY_ENFORCEMENT)
    assert actual == declared, (
        f"RiskPolicy fields changed: {sorted(actual ^ declared)}. Every field must be "
        "classified ENFORCED or INERT in POLICY_ENFORCEMENT, and the inert ones disclosed in "
        "docs/RISK_POLICY.md (AGENTS.md:29-30)."
    )


def test_nothing_under_src_reads_an_inert_field() -> None:
    """The strict direction: an inert field that any module so much as names fails."""

    for field in sorted(INERT_FIELDS):
        mentioned_in = _modules_mentioning(field)
        assert not mentioned_in, (
            f"RiskPolicy.{field} is classified INERT but is mentioned in {mentioned_in}. "
            "If it is now read, reclassify it ENFORCED, remove it from the disclosure in "
            "docs/RISK_POLICY.md, and give the refusal a breach-implies-deny test."
        )


def test_every_enforced_field_is_read_somewhere_under_src() -> None:
    """The other direction: a field labelled ENFORCED that nothing reads is the R-25 shape."""

    read = _attribute_names_read()
    unread = sorted(
        field
        for field, status in POLICY_ENFORCEMENT.items()
        if status == ENFORCED and field not in read
    )
    assert not unread, (
        f"RiskPolicy field(s) {unread} are classified ENFORCED but no module under "
        "src/chronos loads that attribute. Reclassify them INERT and disclose them."
    )


def test_the_scanner_actually_sees_the_engine() -> None:
    """Positive control: an empty or misdirected scan must not pass as 'nothing reads it'."""

    read = _attribute_names_read()
    engine_reads = {"allowed_symbols", "max_bot_capital_usd", "allow_pyramiding"}
    assert engine_reads <= read, sorted(engine_reads - read)
    assert _modules_mentioning("allow_pyramiding") == ["src/chronos/risk/engine.py"]


def test_the_inert_set_is_disclosed_where_an_operator_will_look() -> None:
    """Every inert field is named, verbatim and under the agreed phrase, in the policy doc."""

    disclosure = _DISCLOSURE.read_text(encoding="utf-8")
    assert _DISCLOSURE_PHRASE in disclosure, (
        f"docs/RISK_POLICY.md must carry the phrase {_DISCLOSURE_PHRASE!r} introducing the "
        "inert set"
    )
    section = disclosure[disclosure.index(_DISCLOSURE_PHRASE) :]
    for field in sorted(INERT_FIELDS):
        assert f"`{field}`" in section, (
            f"docs/RISK_POLICY.md must disclose `{field}` as declared, not enforced"
        )

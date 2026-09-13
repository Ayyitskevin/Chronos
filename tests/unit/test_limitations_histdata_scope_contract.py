"""Two docs/limitations.md history-store bullets say what the source proves.

D-4 sweep, items 8 + 9.

At 4e7068e the C1 holdout bullet assigned the once-only, owner-typed, logged unlock and the
registry-brokered read to "Phase C2's job", and the options bullet said "no expired-options
history exists at any spend". The registry guardian (``src/chronos/registry/holdout_guardian.py``)
already grants a single-use unlock and performs the only sanctioned unmasking read inside the
ledger's locked, freshly verified critical section, recording the burn BEFORE any bar is
returned; the direct-file bypass remains (``histdata.holdout.read_embargoed_bars`` masks by
default, and ``bars/<SYMBOL>.csv`` is a plain file). Expired-options history is absent from THIS
zero-budget store because IBKR cannot backfill expired contracts; the repository's own plan
(``docs/VISION_COMPLETION_PLAN.md``) names licensed history as the alternative. No subscription
tier, price, or account fact is asserted anywhere here — those are owner-only.

Doc pins are anchored allowlists plus whole-document scans for the old forms; source pins are
AST/text pins on the exact guard shapes the prose relies on, so a removed guard fails here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"
GUARDIAN = ROOT / "src" / "chronos" / "registry" / "holdout_guardian.py"
HISTDATA_HOLDOUT = ROOT / "src" / "chronos" / "histdata" / "holdout.py"
STORE = ROOT / "src" / "chronos" / "histdata" / "store.py"

HOLDOUT_BULLET = (
    "- **The holdout embargo is a default-masked accessor, not a structural guardian.**"
)
OPTIONS_HEADING = "## Options forward capture"


def _bullet(anchor: str) -> str:
    """One markdown bullet, whitespace-collapsed, from its anchor to the next bullet/heading."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    start = text.index("\n" + anchor) + 1
    stop = re.search(r"^(- |## )", text[start + 1 :], re.M)
    assert stop is not None, anchor
    return " ".join(text[start : start + 1 + stop.start()].split())


def _first_bullet_after(heading: str) -> str:
    text = LIMITATIONS.read_text(encoding="utf-8")
    section = text[text.index("\n" + heading) + 1 :]
    first = re.search(r"^- ", section, re.M)
    assert first is not None, heading
    return _bullet(section[first.start() :].split("\n", 1)[0].rstrip())


# ------------------------------------------------------- 1. the mediated read is present


def test_1_the_holdout_bullet_names_the_present_mediated_read_and_keeps_the_bypass() -> None:
    bullet = _bullet(HOLDOUT_BULLET)
    accepted = (
        "A caller that reads `bars/<SYMBOL>.csv` directly bypasses it.",
        "mediated read exists",
        "`src/chronos/registry/holdout_guardian.py`",
        "single-use unlock grant",
        "locked critical section",
        "burn recorded before any bar is unmasked",
        "direct-file bypass above is untouched",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert re.search(r"\.py:\d", bullet) is None, "no line numbers in prose"
    whole = LIMITATIONS.read_text(encoding="utf-8")
    assert "Phase C2's job" not in whole, "the deferral to C2 must not survive anywhere"
    assert "registry-brokered reads are" not in whole


# ------------------------------------------------ 2. expired options: this store, not any spend


def test_2_the_options_bullet_scopes_the_absence_to_this_store_and_names_the_owner_option() -> None:
    bullet = _first_bullet_after(OPTIONS_HEADING)
    accepted = (
        "absent from this zero-budget store",
        "IBKR cannot backfill expired contracts",
        "forward-only",
        "Licensed vendor history remains an owner option",
        "`docs/VISION_COMPLETION_PLAN.md`",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert re.search(r"\.py:\d|\.md:\d", bullet) is None, "no line numbers in prose"
    whole = LIMITATIONS.read_text(encoding="utf-8")
    assert "at any spend" not in whole, "the universal claim must not survive anywhere"
    for owner_only in ("$", "tier", "subscription"):
        assert owner_only not in bullet, f"no owner-only tier/price fact: {owner_only!r}"


# --------------------------------------------------------- 3. the source facts the prose rests on


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Attribute) and call.func.attr == name)
            or (isinstance(call.func, ast.Name) and call.func.id == name)
        )
    ]


def test_3a_the_guardian_mediated_read_is_locked_single_use_and_records_the_burn_first() -> None:
    """Pinned by shape: the whole read runs inside `with _fresh_verified_ledger(ledger) as fresh`
    (the locked, verified critical section); a consumed grant is refused as single-use; and the
    KIND_CONSUME append happens inside that block, before the function returns any series."""

    source = GUARDIAN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "def request_unlock(" in source and "Grant a single-use holdout unlock" in source
    read = _function(tree, "mediated_holdout_read")
    withs = [node for node in read.body if isinstance(node, ast.With)]
    assert len(withs) == 1, "one critical section"
    (critical,) = withs
    ctx = critical.items[0].context_expr
    assert isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name)
    assert ctx.func.id == "_fresh_verified_ledger", "the verified-ledger lock guards the read"
    raises = [
        node.exc.args[0].value
        for node in ast.walk(critical)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and node.exc.args
        and isinstance(node.exc.args[0], ast.Constant)
    ]
    assert "unlock grant already consumed (single-use)" in raises, (
        "single-use refusal inside the lock"
    )
    appends = _calls_named(critical, "append")
    consume = [
        c
        for c in appends
        if c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "KIND_CONSUME"
    ]
    assert len(consume) == 1, "exactly one burn record inside the critical section"
    returns_in_block = [node for node in ast.walk(critical) if isinstance(node, ast.Return)]
    assert not returns_in_block, (
        "nothing is returned from inside the block: the burn is durable first"
    )
    assert isinstance(read.body[-1], ast.Return), (
        "the unmasked series is returned only after the block"
    )


def test_3b_the_direct_file_bypass_remains() -> None:
    """The store's bars are a plain file at bars/<SYMBOL>.csv and the histdata reader masks by
    default — a caller opening the file itself is not mediated by anything."""

    tree = ast.parse(HISTDATA_HOLDOUT.read_text(encoding="utf-8"))
    reader = _function(tree, "read_embargoed_bars")
    kwonly = dict(
        zip([a.arg for a in reader.args.kwonlyargs], reader.args.kw_defaults, strict=True)
    )
    default = kwonly["unlocked"]
    assert isinstance(default, ast.Constant) and default.value is False, "masked by default"
    assert 'root / "bars" / f"{symbol}.csv"' in STORE.read_text(encoding="utf-8")


def test_3c_the_plan_names_licensed_expired_options_history_as_the_alternative() -> None:
    """Anchored sentence, not a substring of a negation: the repo's own plan says validation is
    calendar-bound WITHOUT licensed history, which is what makes it an owner option."""

    plan = " ".join(PLAN.read_text(encoding="utf-8").split())
    assert (
        "Without licensed expired-options history, option validation becomes calendar-bound and "
        "can take multiple years."
    ) in plan

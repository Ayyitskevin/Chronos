"""Two docs/limitations.md autonomy bullets say exactly what the startup source proves.

D-4 sweep, items 1 + 5.

At 4e7068e the "built and wired" bullet said a backend booted with a valid ``AUTONOMY_MANDATE_FILE``
"auto-activates" autonomy, and the struck R-36 bullet said "no shipped entrypoint constructs" the
runtime. Neither is what the source does. The backend lifespan (``src/chronos/api/main.py``) does
not construct the runtime at all under a recovery hold (ADR-0054); ``build_autonomy_runtime``
(``src/chronos/api/autonomy_wiring.py``) returns None with no mandate file, alerts and returns None
for a mandate scoped to another account, refuses a submitting-mode mandate on a static proposer
posture (ADR-0051), and only assembles after the activation is recorded durably; and the lifespan
binds whatever it built to the app. A valid mandate is necessary, not sufficient — and the
entrypoint exists; what is left of R-36 is service supervision and an operational proof.

These pins hold the document and the source together in both directions: the doc pins are an
anchored allowlist of accepted phrases (a negation that contains the words does not pass), and the
source pins call the guards with minimal inputs or, where the guard lives inside the lifespan, pin
its exact AST shape — so a removed guard fails here, not only in prose.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

from chronos.api import autonomy_wiring
from chronos.api.autonomy_wiring import (
    build_autonomy_runtime,
    submitting_posture_is_unauthenticated,
)
from chronos.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
MAIN = ROOT / "src" / "chronos" / "api" / "main.py"
WIRING = ROOT / "src" / "chronos" / "api" / "autonomy_wiring.py"

# The bullet's own en dash between M1 and M7.5, spelled as an escape so the anchor stays ASCII.
WIRED_BULLET = "- **The autonomy stack is built and wired (M1\u2013M7.5).**"
R36_BULLET = "- ~~**The proposal route does not run the cycle (R-36)**~~"


def _bullet(anchor: str) -> str:
    """One markdown bullet, whitespace-collapsed, from its anchor to the next bullet/heading."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    start = text.index("\n" + anchor) + 1
    stop = re.search(r"^(- |## )", text[start + 1 :], re.M)
    assert stop is not None, anchor
    return " ".join(text[start : start + 1 + stop.start()].split())


# ------------------------------------------------------------- 1. the activation predicate


def test_1_the_wired_bullet_states_the_activation_predicate_and_cites_both_modules() -> None:
    bullet = _bullet(WIRED_BULLET)
    accepted = (
        "does not construct the runtime at all under a recovery hold",
        "with no mandate file configured, autonomy is inert",
        "scoped to this account",
        "submitting mode additionally requires a proposer registry and evidence binding",
        "recorded durably before a runtime exists",
        "necessary, not sufficient",
        "`src/chronos/api/main.py`",
        "`src/chronos/api/autonomy_wiring.py`",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert "auto-activates" not in bullet, "the over-claim is gone"
    assert re.search(r"\.py:\d", bullet) is None, "no line numbers in prose"


# ------------------------------------------------------ 2. the entrypoint exists; what is open


def test_2_the_r36_bullet_says_the_lifespan_constructs_and_binds_the_runtime() -> None:
    bullet = _bullet(R36_BULLET)
    accepted = (
        "the backend lifespan (`src/chronos/api/main.py`) calls `build_autonomy_runtime`",
        "binds the result to the app",
        "nothing in the repository installs, enables, or starts it",
        "no supervised run or operational proof",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert "no shipped entrypoint constructs it" not in bullet, "the stale claim is gone"
    assert re.search(r"\.py:\d", bullet) is None, "no line numbers in prose"


# ------------------------------------------------------- 3. the source facts the prose rests on


def _if_not_recovery_held_calls_build(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        guards_hold = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "recovery_held"
        )
        if not guards_hold:
            continue
        for inner in node.body:
            for call in ast.walk(inner):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "build_autonomy_runtime"
                ):
                    return True
    return False


def test_3a_the_lifespan_constructs_the_runtime_only_when_no_recovery_hold_is_in_force() -> None:
    """ADR-0054's guard, pinned by shape: `if not recovery_held:` encloses the one call that
    assembles autonomy. Removing the guard (mutation) or the call fails this, not only the prose."""

    source = MAIN.read_text(encoding="utf-8")
    assert _if_not_recovery_held_calls_build(ast.parse(source))
    assert "app.state.autonomy = autonomy" in source, "the lifespan binds the runtime to the app"


def test_3b_no_mandate_file_means_no_runtime() -> None:
    """The first thing the wiring reads is the configured mandate path; None short-circuits
    before any other runtime attribute is touched, so a bare namespace is enough."""

    settings = Settings()
    assert settings.autonomy_mandate_file is None, "the fresh-checkout default is no grant"
    runtime = SimpleNamespace(settings=settings)
    assert build_autonomy_runtime(runtime, process_generation=0, is_writer=lambda: False) is None


def test_3c_a_submitting_mandate_on_the_static_posture_is_refused_before_assembly() -> None:
    """ADR-0051: with no proposer registry and no evidence binding (the Settings defaults), a
    submitting mode may not assemble; a non-submitting mode on the same posture may."""

    settings = Settings()
    assert settings.autonomy_proposers_file is None and not settings.autonomy_evidence_bundles
    submitting = next(iter(autonomy_wiring.SUBMITTING_AUTONOMY_MODES))
    assert submitting_posture_is_unauthenticated(settings, SimpleNamespace(mode=submitting)) is True
    non_submitting = [
        mode for mode in type(submitting) if mode not in autonomy_wiring.SUBMITTING_AUTONOMY_MODES
    ]
    assert non_submitting, "at least one non-submitting mode must exist to be the control"
    assert (
        submitting_posture_is_unauthenticated(settings, SimpleNamespace(mode=non_submitting[0]))
        is False
    )


def test_3d_account_scope_and_durable_activation_gate_assembly_in_the_wiring_source() -> None:
    """The two remaining conjuncts of the predicate, pinned to the wiring's own text: a mandate
    scoped to another account alerts and returns None, and a runtime is assembled only after
    `ensure_activation` recorded the activation."""

    wiring = WIRING.read_text(encoding="utf-8")
    assert "loaded.mandate.account_fingerprint != fingerprint" in wiring
    assert "if not ensure_activation(" in wiring
    assert "BackendGatherers(" in wiring, "the lifespan's runtime carries the backend's gatherers"


# ----------------------------------- 5. the introduction makes the same (qualified) claim


INTRO_ACCEPTED = (
    "activates autonomy at boot unless a recovery hold is in force or, for a submitting mode, "
    "the proposer posture is static"
)


def _intro() -> str:
    text = LIMITATIONS.read_text(encoding="utf-8")
    return " ".join(text[: text.index("\n## ")].split())


def test_5_the_introduction_carries_the_qualified_claim_and_the_bare_form_appears_nowhere() -> None:
    """Daybreak's P1: the intro said a valid mandate "auto-activates and trades inside that
    mandate" — the unconditional form the corrected bullet refutes — and called the document
    the single source of truth. The intro now states the same predicate in one clause, and
    the bare `auto-activates` claim appears nowhere in the whole document (the anchored
    allowlist covers the accepted sentence; the scan covers every other line)."""

    intro = _intro()
    assert INTRO_ACCEPTED in intro, intro
    assert "account-matching" in intro, intro
    assert "without one, autonomy is inert" in intro, intro
    whole = LIMITATIONS.read_text(encoding="utf-8")
    assert "auto-activates" not in whole, "the bare unconditional form must not survive anywhere"


# ---------------------------------- 6. the service-supervision residual is supportable


SERVICE_TEMPLATE = ROOT / "docs" / "ops" / "chronos-backend.service"
OPS_README = ROOT / "docs" / "ops" / "README.md"


def test_6a_a_backend_service_template_ships_and_nothing_in_the_repo_installs_it() -> None:
    """Daybreak's P2: "no service unit supervises" was an unmeasured environment negative.
    What the repository proves: a template ships with the backend as ExecStart and
    Restart=on-failure, and its README says nothing installs, enables, or starts it."""

    unit = SERVICE_TEMPLATE.read_text(encoding="utf-8")
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert len(exec_lines) == 1 and exec_lines[0].endswith("scripts/run_backend.py"), exec_lines
    assert "Restart=on-failure" in unit.splitlines(), "the template restarts on failure, not always"
    assert "installs, enables, or starts" in OPS_README.read_text(encoding="utf-8")


def test_6b_the_r36_residual_says_a_template_ships_and_no_supervised_run_is_demonstrated() -> None:
    bullet = _bullet(R36_BULLET)
    accepted = (
        "a service unit template ships",
        "`docs/ops/chronos-backend.service`",
        "nothing in the repository installs, enables, or starts it",
        "no supervised run or operational proof",
    )
    for phrase in accepted:
        assert phrase in bullet, phrase
    assert "no service unit supervises" not in bullet, "the environment negative is gone"
    assert re.search(r"\.py:\d", bullet) is None, "no line numbers in prose"

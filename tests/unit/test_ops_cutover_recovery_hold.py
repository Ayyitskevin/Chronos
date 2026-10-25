"""C1R-F2: step 7 of the N3 cutover runbook gates on the recovery hold it is going to meet.

The cutover archive (step 3) moves only ``chronos.db`` and its ``-wal``/``-shm``. The ADR-0054
installation marker, ``state_generation.json``, stays beside the kill-switch file, and the fresh
keyed store has no ``installation_identity`` row: ``create_all`` leaves that table empty, and only
migration 0012 writes the adoption sentinel. Marker present and row absent is
``DATABASE_REPLACED``, so the first start of the keyed backend is held (read-only, unreconciled,
no autonomy runtime) while ``/health`` still answers 200 (kimi GONOGO-READ-V2 P1). Fix shape (a),
Kevin K-20261024-006: the runbook EXPECTS the hold, proves its reason, has Kevin acknowledge it
with a note, restarts, and proves the hold is gone before steps 8 and 9.

Two kinds of test. The text pins hold the gate's sentences in step 7, in order, and an in-test
negative control shows that removing the expect-the-hold, acknowledge or prove-cleared part is
caught. The behavioural tests run the gate's own commands, extracted from the runbook, against a
SYNTHETIC store and state directory under ``tmp_path``: the real app through its real lifespan
(the seam ``tests/safety/test_restore_recovery_hold.py`` uses), the suite's derived persistence
key (never printed), the demo broker, no network. The expected-output blocks the runbook quotes
are compared with what those commands print.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.safety.test_restore_recovery_hold import _mandate

from chronos.api import auth
from chronos.api.dependencies import require_lease_holder, require_writer
from chronos.api.main import create_app
from chronos.api.routes import autonomy as autonomy_routes
from chronos.api.routes import live as live_routes
from chronos.cli.campaign_preflight import cmd_campaign_preflight
from chronos.cli.campaign_status import _recovery_status
from chronos.config.settings import Settings, get_settings
from chronos.operations import cutover_archive as cutover
from chronos.orders.recovery_hold import RecoveryHoldReason
from chronos.orders.state_generation import DEFAULT_MARKER_NAME

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "ops" / "N3-CUTOVER.md"
_STAMP = "20261024T000000Z"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _step(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _step7(text: str | None = None) -> str:
    return _step(
        text if text is not None else RUNBOOK.read_text(encoding="utf-8"), "7. **", "8. **Bind"
    )


# --- the gate's text --------------------------------------------------------------------------

#: Each part of the gate, as the sentences (whitespace-normalized) the runbook must carry in
#: step 7. Together they cover every line the gate adds; a removed line breaks at least one.
_GATE: dict[str, tuple[str, ...]] = {
    "health-200-is-not-proof": (
        'That 200 proves only that the process answers: `/health` says `"status":"ok"` under a '
        "recovery hold too.",
        "**Steps 8 and 9 wait for the gate below.**",
    ),
    "expect-the-hold": (
        "**Expect the recovery hold, reason `database_replaced` (ADR-0054). "
        "It is not an incident.**",
        "Step 3 moved only `chronos.db` and its `-wal`/`-shm`.",
        "The installation marker `state_generation.json` beside the kill-switch file survives, and "
        "a fresh store has no `installation_identity` row.",
        "A marker with no row reads as a database replaced beneath surviving state files, so the "
        "backend boots held: read-only, unreconciled, no autonomy runtime.",
        "Read the hold from `<checkout>`.",
        "The token comes from the backend's own token file (`BACKEND_TOKEN_FILE`, "
        "`data/backend_api_token` by default; use your path if it differs): the shell puts it in "
        "the header, and nothing prints it.",
        "The log is your `LOG_FILE` (`logs/chronos.log` by default).",
        "curl -sS -w ' HTTP %{http_code}\\n' -H \"X-Chronos-Token: $(cat data/backend_api_token)\" "
        "http://127.0.0.1:<port>/autonomy/alerts",
        "It must print these four lines, then one log line whose message begins `This boot cannot "
        "prove it does not follow a restore (database_replaced)`:",
        "`GET /autonomy/alerts` only reads, but it sits behind the same writer check as every "
        "mutating route: its 409 is the hold's refusal, and its 200 later means writes are "
        "allowed.",
        "**Anything else is not the cutover's state: stop the backend (`systemctl --user stop "
        "chronos-backend`), acknowledge nothing, and stop here.**",
        "That includes another reason in the log or the 409 (`state_directory_lost`, "
        "`marker_unreadable`, `installation_mismatch`, `restore_pending`), another startup fault "
        "beside `recovery_unverified`, and no hold at all: `startup_faults: none` here means the "
        "marker was not where the backend looks, so the live-safety state this host ran with is "
        "missing.",
        "`python -m chronos.cli campaign status` and `campaign preflight` cannot show this reason: "
        "until the restart after your acknowledgement they report the fresh store's missing "
        "`installation_identity` row as UNVERIFIED.",
        "That restart writes the row from the marker's id (ADR-0054, amended 2026-10-25), and from "
        "then on both read the pair as consistent: `recovery hold: CLEAR` and "
        "`PASS [ADR-0054] state identity`.",
    ),
    "acknowledge": (
        "**Then you acknowledge it: your act, with a typed note** saying this is the expected N3 "
        "database replacement.",
        "The same token file, never printed:",
        "curl -fsS -X POST http://127.0.0.1:<port>/live/recovery/acknowledge \\ "
        '-H "X-Chronos-Token: $(cat data/backend_api_token)" \\ '
        "-H 'Content-Type: application/json' \\",
        'Expect `{"acknowledged":true,"reason":"database_replaced","binding":"<64 hex>",'
        '"effective":"next start"}`.',
        "A `reason` other than `database_replaced`: stop.",
        "The acknowledgement binds this exact observation and takes effect at the next start, so "
        "this process stays held.",
        "It does not disengage the kill switch, arm, or activate a mandate.",
    ),
    "prove-cleared": (
        "**Restart, then prove the hold is gone, before step 8.**",
        "`systemctl --user restart chronos-backend`, then `systemctl --user is-active "
        "chronos-backend` → `active`, then the `/health` and `/autonomy/alerts` commands above "
        "again. They must print:",
        "`startup_faults: none`: the hold is cleared.",
        "`HTTP 200`: writes are allowed (the body lists unacknowledged alerts; it may hold the "
        "mandate alert below instead of `[]`).",
        "`reconciliation: RECONCILED` with `reconciliation=RUNNING`: startup reconciliation ran "
        "and the refresher runs (`STARTING` in the first seconds: re-run).",
        "`autonomy=NOT_EXPECTED` is right until step 9, for one of two reasons: no "
        "`AUTONOMY_MANDATE_FILE` is configured, or the configured mandate still carries the "
        "unkeyed fingerprint.",
        "Then the log says `Persistent mandate is scoped to a different account; autonomy stays "
        'inert`, and the alert list holds `"kind":"autonomy.mandate_invalid"`.',
        "Step 9 re-authors the mandate; autonomy starts after it.",
        "**Anything else (a startup fault, a 409, `reconciliation` not `RECONCILED`, "
        "`autonomy=RUNNING`: a grant active before step 8 proved the key) means stop the backend "
        "and stop here.**",
        "A bare `/health` 200 is not this proof.",
        "Steps 8 and 9 start only after it passes.",
    ),
}

#: Each part's lead-in, which starts its own paragraph inside list item 7, in this order.
_LEAD_INS = (
    ("health-200-is-not-proof", "That 200 proves only"),
    ("expect-the-hold", "**Expect the recovery hold"),
    ("acknowledge", "**Then you acknowledge it"),
    ("prove-cleared", "**Restart, then prove the hold is gone"),
)

_STEP8_RETAKES_THE_GATE = (
    "then start again, pass step 7's recovery-hold gate again (the new store holds no "
    "acknowledgement, so the hold comes back), and repeat this step."
)


def _gate_problems(step7: str) -> list[str]:
    """Every gate sentence missing from step 7, named by its part."""

    flat = _flat(step7)
    return [
        f"{part}: {sentence[:72]}"
        for part, sentences in _GATE.items()
        for sentence in sentences
        if _flat(sentence) not in flat
    ]


def _without_part(step7: str, part: str) -> str:
    """Step 7 with one part of the gate cut out, from its lead-in to the next one."""

    leads = [lead for _, lead in _LEAD_INS]
    index = [name for name, _ in _LEAD_INS].index(part)
    start = step7.index(leads[index])
    end = step7.index(leads[index + 1]) if index + 1 < len(leads) else len(step7)
    return step7[:start] + step7[end:]


def test_c1rf2_step_7_carries_the_whole_gate_in_order() -> None:
    step7 = _step7()
    assert _gate_problems(step7) == []
    starts = [step7.index("\n\n   " + lead) for _, lead in _LEAD_INS]
    assert starts == sorted(starts), "the parts run: 200 is not proof, expect, acknowledge, prove"
    # the gate sits after the backend's first start and before step 8 (the slice ends there)
    assert step7.index("systemctl --user start chronos-backend") < starts[0]


def test_c1rf2_negative_control_each_removed_part_is_caught() -> None:
    """The pin is not vacuous: cut any one part out and the check names exactly that part."""

    step7 = _step7()
    for part, _ in _LEAD_INS:
        problems = _gate_problems(_without_part(step7, part))
        assert problems, part
        assert {problem.split(":", 1)[0] for problem in problems} == {part}, problems


def test_c1rf2_step_8_retakes_the_gate_after_a_re_archive() -> None:
    step8 = _step(RUNBOOK.read_text(encoding="utf-8"), "8. **Bind", "9. **")
    assert _flat(_STEP8_RETAKES_THE_GATE) in _flat(step8)


def test_c1rf2_the_gate_commands_never_print_the_token() -> None:
    """The token reaches only a request header, through the shell; nothing echoes it."""

    gate = _step7()[_step7().index("That 200 proves only") :]
    blocks = [body for info, body in _fences(gate) if info == "bash"]
    assert len(blocks) == 2, "the read block and the acknowledgement block"
    for block in blocks:
        assert "backend_api_token" in block
        uses = block.count("backend_api_token")
        assert block.count('-H "X-Chronos-Token: $(cat data/backend_api_token)"') == uses
        for word in (" -v", "--verbose", "--trace", "set -x", "echo ", "printf ", "tee "):
            assert word not in block, word
    # the header and the default file are the code's own
    assert auth._TOKEN_HEADER == "X-Chronos-Token"
    assert Settings.model_fields["backend_token_file"].default == Path("data/backend_api_token")
    assert Settings.model_fields["log_file"].default == Path("logs/chronos.log")


def test_c1rf2_the_routes_the_gate_reads_are_the_ones_it_says() -> None:
    """`/autonomy/alerts` is a GET behind `require_writer`; the acknowledgement is not."""

    routes = {
        (route.path, method): route
        for router in (autonomy_routes.router, live_routes.router)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    def calls(route: APIRoute) -> set[object]:
        return {dependency.call for dependency in route.dependant.dependencies}

    assert require_writer in calls(routes[("/autonomy/alerts", "GET")])
    acknowledge = routes[("/live/recovery/acknowledge", "POST")]
    assert require_lease_holder in calls(acknowledge) and require_writer not in calls(acknowledge)


def test_c1rf2_each_part_starts_its_own_paragraph() -> None:
    step7 = _step7()
    for _, lead in _LEAD_INS:
        assert "\n\n   " + lead in step7, lead
    # and the gate's last sentence ends step 7, a blank line before step 8 like every step
    assert step7.endswith(" Steps 8 and 9 start only after it passes.\n\n")


# --- the gate, run on a synthetic cutover -------------------------------------------------------


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A synthetic deployment: store, state files, token and log all under ``tmp_path``."""

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("ALLOW_ORDER_TRANSMIT", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    monkeypatch.delenv("AUTONOMY_MANDATE_FILE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _boot() -> TestClient:
    get_settings.cache_clear()
    return TestClient(create_app())


def _token(root: Path) -> dict[str, str]:
    return {"X-Chronos-Token": (root / "backend_api_token").read_text(encoding="utf-8").strip()}


def _archive_per_cutover(root: Path) -> list[str]:
    """Step 3's effect on the directory: the store family moves to ``.archived-<stamp>`` names."""

    family = ["chronos.db", *("chronos.db" + suffix for suffix in cutover.SIDECAR_SUFFIXES)]
    moved = []
    for name in family:
        if (root / name).exists():
            (root / name).rename(root / f"{name}{cutover.ARCHIVED_INFIX}{_STAMP}")
            moved.append(name)
    return moved


def _fences(text: str) -> list[tuple[str, str]]:
    """Each fenced block as ``(info string, dedented body)``, read line by line."""

    blocks: list[tuple[str, str]] = []
    info: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        fence = re.fullmatch(r"\s*```(\w*)", line)
        if fence is None:
            if info is not None:
                body.append(line)
        elif info is None:
            info, body = fence.group(1), []
        else:
            assert fence.group(1) == "", f"a closing fence carries an info string: {line!r}"
            blocks.append((info, textwrap.dedent("\n".join(body) + "\n")))
            info = None
    assert info is None, "a fence never closes"
    return blocks


def _gate_blocks() -> tuple[str, str, str, str]:
    """The read block, the held output, the acknowledgement block and the cleared output."""

    gate = _step7()[_step7().index("That 200 proves only") :]
    blocks = _fences(gate)
    assert [info for info, _ in blocks] == ["bash", "", "bash", ""], blocks
    return blocks[0][1], blocks[1][1], blocks[2][1], blocks[3][1]


def _health_lines(health_body: str) -> list[str]:
    """The read block's ``/health`` filter, exactly as the runbook gives it, over a real body."""

    read_block = _gate_blocks()[0]
    program = re.search(r"python -I -c '(.*?)'\n", read_block, re.S)
    assert program is not None
    done = subprocess.run(
        [sys.executable, "-I", "-c", program.group(1)],
        input=health_body,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.splitlines()


def _alerts_line(client: TestClient, root: Path) -> str:
    """What the read block's ``curl -sS -w ' HTTP %{http_code}\\n'`` line prints."""

    response = client.get("/autonomy/alerts", headers=_token(root))
    return f"{response.text} HTTP {response.status_code}"


def _last_hold_log_line(root: Path) -> str:
    """The read block's ``grep`` line, run as written in the directory the log path is under."""

    read_block = _gate_blocks()[0]
    (line,) = [entry for entry in read_block.splitlines() if entry.startswith("grep ")]
    done = subprocess.run(
        ["bash", "-c", line], cwd=root, capture_output=True, text=True, timeout=60, check=False
    )
    return done.stdout.strip()


def test_c1rf2_a_synthetic_cutover_boots_held_then_clears_after_the_acknowledgement(
    demo_env: Path,
) -> None:
    _, held_output, ack_block, cleared_output = _gate_blocks()

    # the pre-cutover installation: its writer seeded the marker and the identity row
    with _boot():
        pass
    assert (demo_env / DEFAULT_MARKER_NAME).exists()

    # step 3: only the store family moves; the marker is not in the cutover's namespace
    namespace = {"chronos.db", *("chronos.db" + suffix for suffix in cutover.LIVE_NAME_SUFFIXES)}
    assert DEFAULT_MARKER_NAME not in namespace
    assert _archive_per_cutover(demo_env) == ["chronos.db"]
    assert (demo_env / DEFAULT_MARKER_NAME).exists()
    assert not (demo_env / "chronos.db").exists()

    # step 7's start: held, for exactly the reason the runbook expects
    with _boot() as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is not None
        assert state.recovery_hold.reason is RecoveryHoldReason.DATABASE_REPLACED
        assert client.get("/health").status_code == 200  # the bare 200 the gate refuses to trust
        measured = [*_health_lines(client.get("/health").text), _alerts_line(client, demo_env)]
        assert measured == held_output.splitlines()
        # a real write route refuses too, not only the read the runbook uses
        assert (
            client.post("/live/arm", json={"phrase": "x"}, headers=_token(demo_env)).status_code
            == 409
        )
        log_line = _last_hold_log_line(demo_env)
        assert json.loads(log_line)["message"].startswith(
            "This boot cannot prove it does not follow a restore (database_replaced)"
        )

        # the acknowledgement, with the runbook's own body
        (body,) = re.findall(r"-d '(\{.*\})'", ack_block)
        response = client.post(
            "/live/recovery/acknowledge", json=json.loads(body), headers=_token(demo_env)
        )
        assert response.status_code == 200
        expected = re.escape(
            '{"acknowledged":true,"reason":"database_replaced","binding":"<64 hex>",'
            '"effective":"next start"}'
        ).replace(re.escape("<64 hex>"), "[0-9a-f]{64}")
        assert re.fullmatch(expected, response.text), response.text
        assert state.may_write is False  # this process stays held
        assert _alerts_line(client, demo_env).endswith(" HTTP 409")

    # the restart: cleared, writes allowed, reconciled, autonomy inert for the named reason
    with _boot() as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is None and state.may_write is True
        measured = [*_health_lines(client.get("/health").text), _alerts_line(client, demo_env)]
        assert measured == cleared_output.splitlines()
        assert getattr(client.app.state, "autonomy", None) is None  # no mandate configured


def test_c1rf2_an_unkeyed_mandate_stays_inert_after_the_gate_for_the_named_reason(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before step 9 the configured grant still carries the old fingerprint: inert, and said."""

    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
    mandate = _mandate(datetime.now(UTC)).model_copy(update={"account_fingerprint": "0" * 64})
    mandate_file = demo_env / "mandate.json"
    mandate_file.write_text(mandate.model_dump_json(), encoding="utf-8")
    mandate_file.chmod(0o600)
    monkeypatch.setenv("AUTONOMY_MANDATE_FILE", str(mandate_file))
    monkeypatch.setenv("AUTONOMY_ALERT_FILE", str(demo_env / "owner_alerts.jsonl"))
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
        assert getattr(client.app.state, "autonomy", None) is None
        health = _health_lines(client.get("/health").text)
        assert health == _gate_blocks()[3].splitlines()[:3]
        alerts = client.get("/autonomy/alerts", headers=_token(demo_env))
        assert alerts.status_code == 200
        assert '"kind":"autonomy.mandate_invalid"' in alerts.text
    log = (demo_env / "logs" / "chronos.log").read_text(encoding="utf-8")
    assert "Persistent mandate is scoped to a different account; autonomy stays inert" in log


def test_c1rf2_a_re_archived_store_brings_the_hold_back(demo_env: Path) -> None:
    """Step 8's wrong-key path: the acknowledgement lived in the store that moved aside."""

    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
    for name in ("chronos.db", *("chronos.db" + suffix for suffix in cutover.SIDECAR_SUFFIXES)):
        if (demo_env / name).exists():
            (demo_env / name).rename(demo_env / f"{name}.wrong-key")
    with _boot() as client:
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None and hold.reason is RecoveryHoldReason.DATABASE_REPLACED


def test_c1rf2_campaign_status_cannot_show_this_reason(demo_env: Path) -> None:
    """The runbook's claim about `campaign status` and `campaign preflight`, measured at each step.

    UNVERIFIED while held, still UNVERIFIED in the acknowledging process (the note takes effect
    at the next start), then CLEAR and PASS after the restart writes the identity row (PGAP-I).
    """

    def preflight_line() -> str:
        arguments = argparse.Namespace(
            mandate=demo_env / "mandate.json",
            registry=demo_env / "registry.json",
            state_dir=demo_env,
            unit=demo_env / "worker.service",
            policy=demo_env / "policy.md",
            provider="local",
            model="model-tag",
            worker_backend_url="http://127.0.0.1:8765",
            worker_symbols="SPY",
            backend_symbols="SPY",
            backend_host="127.0.0.1",
            backend_port=8765,
            evidence=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cmd_campaign_preflight(arguments)
        (line,) = [
            line for line in output.getvalue().splitlines() if "ADR-0054] state identity" in line
        ]
        return line

    unverified = (
        "FAIL [SHADOW_CAMPAIGN ADR-0054] state identity: UNVERIFIED; missing "
        "installation_identity row; boot the backend writer once, then re-run preflight"
    )
    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        status = _recovery_status(demo_env, demo_env / "chronos.db")
        assert (status.state.value, status.detail) == (
            "UNVERIFIED",
            "missing installation_identity row",
        )
        assert preflight_line() == unverified
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
        status = _recovery_status(demo_env, demo_env / "chronos.db")
        assert status.state.value == "UNVERIFIED"
        assert preflight_line() == unverified
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
    result = _recovery_status(demo_env, demo_env / "chronos.db")
    assert (result.name, result.state.value, result.detail) == (
        "recovery hold",
        "CLEAR",
        "installation witnesses agree",
    )
    assert preflight_line() == (
        "PASS [ADR-0054] state identity: marker/database pair consistent; no recovery hold"
    )

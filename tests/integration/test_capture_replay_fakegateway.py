"""B1 U-f2: the campaign's capture + replay, end to end, over the FakeGateway (tests only).

``capture_readonly.py``'s REAL ``main()`` runs in this process with the REAL official adapter
over the in-process FakeGateway (``FakeGateway.capture_day()``, U-f1): no gateway, no socket
(``fake_ibapi.install`` forbids them), every ALLOW_* false, Settings from the environment only
(the cwd is an empty temporary directory, so no ``.env`` is read), and a THROWAWAY pepper this
test generates (``secrets.token_hex(32)``), never Kevin's. The output root is ``tmp_path``.

Then ``replay_check.py`` replays the session offline: the checklist's EXIT condition. The
harness's own refusal without a pepper is driven the same way and must write nothing and make
no broker contact. The leak search and the no-order transcript are U-f3, not here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from tests.support import fake_ibapi

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts"
LABEL = "uf2-fakegateway"

#: the environment a B1 paper capture runs under (env only; the cwd has no .env)
PAPER_ENV = {
    "BROKER_MODE": "ibkr",
    "BROKER_ADAPTER": "official_ibkr",
    "IB_ENVIRONMENT": "paper",
    "IB_HOST": "127.0.0.1",
    "IB_PORT": "7497",
    "IB_CLIENT_ID": str(fake_ibapi.CLIENT_ID),
    "IB_ACCOUNT_ID": fake_ibapi.ACCOUNT,
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
}
#: anything the ambient environment could set that would change what the capture does
SCRUB = (
    "AUTONOMY_MANDATE_FILE",
    "IB_ACCOUNT_ALLOWLIST",
    "SYMBOL_ALLOWLIST",
    "CHRONOS_CAPTURE_PEPPER",
    "DEMO_PROFILE",
)


def _load(name: str) -> ModuleType:
    module_name = f"uf2_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _paper_environment(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    for key in SCRUB:
        monkeypatch.delenv(key, raising=False)
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(workdir)  # no .env here: the environment is the only settings source


def _run_capture_main(
    monkeypatch: pytest.MonkeyPatch, capture: ModuleType, out: Path
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["capture_readonly.py", "--out", str(out), "--label", LABEL])
    import contextlib
    import io

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = capture.main()
    return code, printed.getvalue()


class Captured:
    def __init__(self, out: Path, pepper_hex: str, code: int, printed: str, gateway: Any) -> None:
        self.out = out
        self.pepper_hex = pepper_hex
        self.code = code
        self.printed = printed
        self.gateway = gateway

    def json(self, name: str) -> Any:
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    @property
    def steps(self) -> dict[str, Any]:
        steps: dict[str, Any] = self.json("capture.json")["steps"]
        return steps


@pytest.fixture
def captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Captured:
    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    _paper_environment(monkeypatch, workdir)
    pepper_hex = secrets.token_hex(32)  # throwaway: generated here, discarded with tmp_path
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", pepper_hex)
    out = tmp_path / "out" / "session-1"
    code, printed = _run_capture_main(monkeypatch, _load("capture_readonly"), out)
    return Captured(out, pepper_hex, code, printed, gateway)


#: The workload the harness runs for the default allowlist's first two symbols (AAPL, MSFT),
#: written from capture_readonly.run_capture, not read back from its output.
EXPECTED_STEPS = {
    "connect",
    "connection_status",
    "server_time",
    "server_time_offset",
    "account_summary",
    "positions",
    "executions",
    "open_orders",
    "completed_orders",
    "symbol:AAPL:qualify_underlying",
    "symbol:AAPL:underlying_quote",
    "symbol:AAPL:option_chain_parameters",
    "symbol:AAPL:option_specs",
    "symbol:AAPL:qualify_option_contracts",
    "symbol:AAPL:option_market_rules",
    "symbol:MSFT:qualify_underlying",
    "symbol:MSFT:underlying_quote",
    "symbol:MSFT:option_chain_parameters",
    "symbol:AAPL:historical_bars_1d_30d",
    "active_subscription_count_before_disconnect",
    "callback_notices",
    "disconnect",
    "final_connection_status",
}


# --- the capture ran, over the adapter, and the manifest is complete --------------------------


def test_the_capture_runs_end_to_end_over_the_adapter_and_says_so(captured: Captured) -> None:
    assert captured.code == 0, captured.printed
    assert "capture written to" in captured.printed
    sent = {method for _client, method in captured.gateway.transcript}
    # the real adapter drove the fake's wire: handshake, reads, option and bar requests
    assert {"connect", "reqContractDetails", "reqMarketRule", "reqHistoricalData"} <= sent


def test_the_session_directory_holds_exactly_the_three_files(captured: Captured) -> None:
    assert sorted(p.name for p in captured.out.iterdir()) == [
        "capture.json",
        "derived_liquid_hours.json",
        "manifest.json",
    ]


def test_the_manifest_is_complete(captured: Captured) -> None:
    manifest = captured.json("manifest.json")
    assert set(manifest) == {
        "campaign",
        "created_utc",
        "files",
        "gateway_evidence",
        "identifier_pseudonyms",
        "label",
    }
    assert manifest["campaign"] == "chronos-real-gateway-campaign"
    assert manifest["label"] == LABEL
    assert manifest["created_utc"] == captured.json("capture.json")["meta"]["captured_at_utc"]
    # every written file is listed, with the sha256 of its bytes as written
    assert set(manifest["files"]) == {"capture.json", "derived_liquid_hours.json"}
    for name, digest in manifest["files"].items():
        assert digest == sha256((captured.out / name).read_bytes()).hexdigest(), name
    # the keyed scheme and the fingerprint of THIS throwaway pepper (computed independently)
    expected_fingerprint = sha256(bytes.fromhex(captured.pepper_hex)).hexdigest()[:16]
    assert manifest["identifier_pseudonyms"] == {
        "scheme": "hmac-sha256-v2",
        "pepper_fingerprint": expected_fingerprint,
    }


def test_the_capture_records_every_workload_step(captured: Captured) -> None:
    assert set(captured.steps) == EXPECTED_STEPS
    meta = captured.json("capture.json")["meta"]
    assert meta["label"] == LABEL
    assert (meta["broker_mode"], meta["broker_adapter"], meta["ib_environment"]) == (
        "ibkr",
        "official_ibkr",
        "paper",
    )
    assert meta["allow_order_transmit"] is False and meta["allow_live_trading"] is False


def test_every_aapl_step_succeeded_and_the_one_error_is_the_fakes_missing_msft_chain(
    captured: Captured,
) -> None:
    """Errors are observations in the harness; over the fake exactly one is expected: the fake
    models AAPL options only, and the adapter refuses an empty MSFT chain with a typed error."""

    errors = {
        name: value["error"]
        for name, value in captured.steps.items()
        if isinstance(value, dict) and "error" in value
    }
    assert errors == {
        "symbol:MSFT:option_chain_parameters": (
            "MarketDataUnavailableError: Broker returned no option-chain metadata for MSFT"
        )
    }
    assert captured.steps["connect"] == {"ok": True} and captured.steps["disconnect"] == {
        "ok": True
    }


def test_the_option_workload_narrowed_to_the_front_expiry_puts_around_spot(
    captured: Captured,
) -> None:
    """U-f1's caveat, answered: spot 100, DTE 7..45 around 21 from the sim date 2026-10-12."""

    specs = captured.steps["symbol:AAPL:option_specs"]
    assert {(s["expiration"], s["right"], s["strike"]) for s in specs} == {
        ("2026-11-20", "P", "95.0"),
        ("2026-11-20", "P", "100.0"),
    }
    qualified = captured.steps["symbol:AAPL:qualify_option_contracts"]
    assert len(qualified) == 2 and all(q["deliverable_verified"] for q in qualified)
    assert all(q["liquid_hours"] and q["time_zone_id"] for q in qualified)
    rules = captured.steps["symbol:AAPL:option_market_rules"]
    assert {r["con_id"] for r in rules} == {q["con_id"] for q in qualified}
    assert all(r["price_increments"] for r in rules)
    bars = captured.steps["symbol:AAPL:historical_bars_1d_30d"]["bars"]
    assert len(bars) == 21


def test_session_evidence_was_derived_for_every_contract_that_carries_hours(
    captured: Captured,
) -> None:
    derived = captured.json("derived_liquid_hours.json")
    assert derived["probe_instant_utc"] == captured.json("capture.json")["meta"]["captured_at_utc"]
    keys = set(derived["contracts"])
    assert "AAPL:999" in keys  # the underlying (no local symbol)
    assert any(key.startswith("AAPL:AAPL  261120P") for key in keys)  # the options (OSI)
    assert all(entry["parsed"] is not None for entry in derived["contracts"].values())


def test_identifiers_and_the_account_are_pseudonymised_in_the_capture(captured: Captured) -> None:
    """A spot check only (the whole-tree leak search is U-f3)."""

    executions = captured.steps["executions"]
    assert all(e["broker_order_id"].startswith("ORD-") for e in executions)
    assert all(e["account_id"].startswith("ACCT-") for e in executions)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "U-F2-F1: capture_readonly.to_jsonable converts datetime but not date, so a bar's "
        "session_date is written as the Python repr 'datetime.date(2026, 9, 14)', not an ISO date; "
        "the fixture still replays byte-exactly, but the field is not data. Harness gap (the "
        "campaign script), tests only here: not fixed."
    ),
)
def test_a_bars_session_date_is_written_as_an_iso_date(captured: Captured) -> None:
    bars = captured.steps["symbol:AAPL:historical_bars_1d_30d"]["bars"]
    assert bars[0]["session_date"] == "2026-09-14"


# --- replay: the checklist's EXIT condition --------------------------------------------------


def test_every_fixture_replays_offline_exactly(captured: Captured) -> None:
    """Replayed as what it is, a REHEARSAL (``allow_demo``): FakeGateway output is not section-7
    gateway evidence (U-F2-F3 below), but its fixtures must still replay exactly."""

    replay = _load("replay_check")
    assert replay.check_session(captured.out, allow_demo=True) == []


def test_the_replay_cli_passes_the_session_with_exit_zero(
    captured: Captured, tmp_path: Path
) -> None:
    """The command the checklist runs, as a separate process with no broker and no network."""

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "replay_check.py"), "--allow-demo", str(captured.out)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith(f"[PASS] {captured.out}")


@pytest.mark.parametrize("name", ["capture.json", "derived_liquid_hours.json"])
def test_replay_refuses_a_fixture_that_drifted(captured: Captured, name: str) -> None:
    """Positive control for "exactly": one changed value (the file stays valid JSON) is caught."""

    path = captured.out / name
    text = path.read_text(encoding="utf-8")
    needle = '"USD"' if name == "capture.json" else '"US/Eastern"'
    assert needle in text
    path.write_text(text.replace(needle, needle[:1] + "x" + needle[1:], 1), encoding="utf-8")
    failures = _load("replay_check").check_session(captured.out)
    assert any(f"sha256 drift in {name}" in f for f in failures), failures


@pytest.mark.xfail(
    strict=True,
    raises=json.JSONDecodeError,
    reason=(
        "U-F2-F2: replay_check.check_session reports the sha256 drift of a capture.json that is "
        "no longer JSON, then json.loads it and raises instead of returning the failure; the CLI "
        "still exits non-zero (fail closed) but with a traceback, not its [FAIL] report. Harness "
        "gap (the campaign script), tests only here: not fixed."
    ),
)
def test_replay_reports_a_capture_that_is_no_longer_json_as_a_failure(
    captured: Captured,
) -> None:
    path = captured.out / "capture.json"
    path.write_bytes(path.read_bytes()[:-3])  # truncated mid-document
    failures = _load("replay_check").check_session(captured.out)
    assert any("sha256 drift in capture.json" in f for f in failures), failures


def test_replay_refuses_a_hand_edited_capture_even_with_a_matching_manifest(
    captured: Captured,
) -> None:
    """Re-hashing in the manifest does not help for DERIVED data: the liquid-hours derivation is
    re-run from capture.json. (Non-derived data is protected only by the manifest's sha256:
    see the limit control below.)"""

    capture_path = captured.out / "capture.json"
    text = capture_path.read_text(encoding="utf-8").replace("0930-", "1000-", 1)
    capture_path.write_text(text, encoding="utf-8")
    manifest_path = captured.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["capture.json"] = sha256(text.encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    failures = _load("replay_check").check_session(captured.out)
    assert any("does NOT replay byte-exactly" in f for f in failures), failures


def test_limit_a_hand_edit_of_non_derived_data_with_a_rehashed_manifest_still_passes(
    captured: Captured,
) -> None:
    """The replay's stated limit (reader P3-1), recorded so the "exactly" claim is bounded:
    only derived_liquid_hours.json is re-derived; any other capture field (a position quantity,
    a bar close) is protected by the manifest's sha256 alone, which the same hand can rewrite."""

    capture_path = captured.out / "capture.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["steps"]["positions"][0]["quantity"] = "999.0"
    text = json.dumps(capture, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    capture_path.write_text(text, encoding="utf-8")
    manifest_path = captured.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["capture.json"] = sha256(text.encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _load("replay_check").check_session(captured.out, allow_demo=True) == []


def test_replay_refuses_a_session_whose_listed_file_is_missing(captured: Captured) -> None:
    """Branch (l), reader P3-2."""

    (captured.out / "derived_liquid_hours.json").unlink()
    failures = _load("replay_check").check_session(captured.out, allow_demo=True)
    assert any("listed file missing: derived_liquid_hours.json" in f for f in failures), failures


def test_replay_refuses_a_mutation_shaped_step_in_the_capture(captured: Captured) -> None:
    """Branch (k), reader P3-2: the scan the U-f3 no-order transcript leans on."""

    capture_path = captured.out / "capture.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["steps"]["symbol:AAPL:submit_order"] = {"ok": True}
    text = json.dumps(capture, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    capture_path.write_text(text, encoding="utf-8")
    manifest_path = captured.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["capture.json"] = sha256(text.encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    failures = _load("replay_check").check_session(captured.out, allow_demo=True)
    assert failures == [
        f"{captured.out}: mutation-shaped step in capture: symbol:AAPL:submit_order"
    ], failures


# --- reader P2-1: a rehearsal must never count as section-7 gateway evidence -----------------


def _stamp_as_rehearsal(out: Path) -> None:
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gateway_evidence"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_a_rehearsal_manifest_is_refused_as_gateway_evidence_unless_allowed(
    captured: Captured,
) -> None:
    """Branch (j), reader P2-1's positive control: a session whose manifest says
    gateway_evidence=false is refused without --allow-demo, and validates with it."""

    _stamp_as_rehearsal(captured.out)
    replay = _load("replay_check")
    refused = replay.check_session(captured.out)
    assert len(refused) == 1 and "gateway_evidence=false" in refused[0], refused
    assert "cannot count toward the section 7 gate" in refused[0]
    assert replay.check_session(captured.out, allow_demo=True) == []


def test_the_replay_cli_refuses_a_rehearsal_without_allow_demo(
    captured: Captured, tmp_path: Path
) -> None:
    _stamp_as_rehearsal(captured.out)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "replay_check.py"), str(captured.out)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 1 and done.stdout.startswith(f"[FAIL] {captured.out}"), done.stdout


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "U-F2-F3 (reader P2-1): capture_readonly.run_capture stamps gateway_evidence = "
        "(broker_mode is IBKR), true for the official adapter over the FakeGateway, so a fake "
        "capture replays [PASS] as section-7 gateway evidence. The harness cannot tell a fake "
        "from a gateway; the smallest fix is in the campaign script (not a test): see RESULT."
    ),
)
def test_a_fakegateway_capture_is_not_gateway_evidence(captured: Captured) -> None:
    assert captured.json("manifest.json")["gateway_evidence"] is False
    assert captured.json("capture.json")["meta"]["gateway_evidence"] is False
    failures = _load("replay_check").check_session(captured.out)
    assert any("gateway_evidence=false" in f for f in failures), failures


# --- the harness's own refusal without a pepper -----------------------------------------------


@pytest.mark.parametrize("pepper", [None, "0123456789"], ids=["absent", "too_short"])
def test_without_a_usable_pepper_the_capture_writes_nothing_and_opens_no_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pepper: str | None
) -> None:
    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    sockets: list[object] = []
    real_init = socket.socket.__init__

    def recording_init(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        sockets.append(args)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "__init__", recording_init)
    from chronos.broker import official_ibkr

    constructed: list[object] = []
    real_adapter_init = official_ibkr.OfficialIBKRBroker.__init__

    def recording_adapter_init(self: Any, *args: Any, **kwargs: Any) -> None:
        constructed.append(args)
        real_adapter_init(self, *args, **kwargs)

    monkeypatch.setattr(official_ibkr.OfficialIBKRBroker, "__init__", recording_adapter_init)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    _paper_environment(monkeypatch, workdir)
    if pepper is not None:
        monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", pepper)
    out = tmp_path / "out" / "session-1"
    code, printed = _run_capture_main(monkeypatch, _load("capture_readonly"), out)
    assert code == 2, printed
    assert printed.startswith("REFUSED: CHRONOS_CAPTURE_PEPPER"), printed
    assert "(nothing was written; no broker connection was made)" in printed
    assert "capture written" not in printed
    assert not (tmp_path / "out").exists()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == ["cwd"]
    assert gateway.transcript == []  # no handshake, no read: the fake saw nothing
    assert constructed == []  # the adapter was never even built
    assert sockets == []  # and no socket object was created

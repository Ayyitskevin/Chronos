"""Executable contracts for the owner-gated real-gateway campaign helper."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from chronos.config.settings import Settings
from chronos.utils.identifiers import account_fingerprint

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/chronos-real-gateway-campaign/SKILL.md"
CAPTURE = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py"


def _load_capture_module() -> ModuleType:
    module_name = "chronos_real_gateway_capture"
    spec = importlib.util.spec_from_file_location(module_name, CAPTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_capture_retains_partial_qualification_for_market_rules() -> None:
    capture_module = _load_capture_module()
    args = argparse.Namespace(
        label="market-rule-contract",
        symbols=["AAPL"],
        max_symbols=1,
        skip_options=False,
        skip_bars=True,
        account_ids=set(),
    )

    capture = asyncio.run(capture_module.run_capture(Settings(broker_mode="demo"), args))
    steps = capture["steps"]

    assert "error" in steps["symbol:AAPL:qualify_option_contracts"]
    rules = steps["symbol:AAPL:option_market_rules"]
    assert rules == [
        {
            "con_id": 2002,
            "exchange": "SMART",
            "market_rule_id": 26,
            "price_increments": [
                {"increment": "0.01", "low_edge": "0"},
                {"increment": "0.05", "low_edge": "3"},
            ],
            "source": "demo-fixture-v1",
        }
    ]
    assert steps["active_subscription_count_before_disconnect"] == 0
    assert steps["disconnect"] == {"ok": True}


def test_gateway_skill_describes_the_executable_market_rule_capture() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "`option_market_rules`" in text
    assert "read nowhere" not in lowered
    assert "gateway-unobservable today" not in lowered


# --------------------------------------------------------------------------- G-1
# The capture harness pseudonymizes broker identifiers (execution_id, broker_order_id,
# permanent_id), not only account ids — the M4 checklist's [GAP] that no order-id sanitizer exists.
# Tests are numbered to the G-1 contract.

REPLAY_CHECK = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py"
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# G-1 r1: test-only peppers — 32 bytes each, so their hex is the 64-digit grammar the harness
# accepts. Derived from fixed labels rather than written as hex literals: a 64-hex constant in
# the tree is exactly what the release security gate's secret scan (tracked files AND git
# history) refuses, and no test needs randomness. Never a real pepper.
PEPPER = sha256(b"chronos-real-gateway-campaign: test pepper A").digest()
PEPPER_HEX = PEPPER.hex()
OTHER_PEPPER = sha256(b"chronos-real-gateway-campaign: test pepper B").digest()
SALT = ("session-1", "2026-09-13T19:30:00+00:00")  # the public (label, captured_at_utc) pair


def _identifier_payload(label: str = "session-1") -> dict:
    """A capture-shaped tree with one execution and one open order (demo-adapter values)."""

    return {
        "meta": {
            "captured_at_utc": "2026-09-13T19:30:00+00:00",
            "label": label,
            "gateway_evidence": False,
        },
        "steps": {
            "executions": [
                {
                    "execution_id": "DEMO-EXEC-0001",
                    "broker_order_id": 7001,
                    "permanent_id": 9001,
                    "order_ref": "CHR-DEMO-PARTIAL",
                    "quantity": "17001",
                    "executed_at": "2026-07-21T14:00:07.001+00:00",
                }
            ],
            "open_orders": [
                {"broker_order_id": 7001, "permanent_id": None, "order_ref": "CHR-DEMO-PARTIAL"}
            ],
            "account_summary": {"account_id": "DU1234567", "net_liquidation": "700100"},
        },
    }


def _demo_capture(capture_module: ModuleType, label: str) -> dict:
    args = argparse.Namespace(
        label=label,
        symbols=["AAPL"],
        max_symbols=1,
        skip_options=True,
        skip_bars=True,
        account_ids=set(),
    )
    return asyncio.run(capture_module.run_capture(Settings(broker_mode="demo"), args))


def test_1_identifier_values_become_stable_session_scoped_pseudonyms() -> None:
    m = _load_capture_module()
    text_a = m.sanitize_capture(_identifier_payload("session-1"), set(), PEPPER)
    text_a_again = m.sanitize_capture(_identifier_payload("session-1"), set(), PEPPER)
    text_b = m.sanitize_capture(_identifier_payload("session-2"), set(), PEPPER)
    tree_a = json.loads(text_a)
    execution = tree_a["steps"]["executions"][0]
    order = tree_a["steps"]["open_orders"][0]
    # the account-id shape: <KIND>-<16 hex>, an HMAC-SHA256 prefix over a namespaced string
    for token, kind in (
        (execution["execution_id"], "EXEC"),
        (execution["broker_order_id"], "ORD"),
        (execution["permanent_id"], "PERM"),
    ):
        assert re.fullmatch(rf"{kind}-[0-9a-f]{{16}}", token), token
    salt = m.session_salt(_identifier_payload("session-1"))
    assert execution["broker_order_id"] == m.identifier_pseudonym("ORD", 7001, salt, PEPPER)
    # same id → same token within a session (the order and its execution agree)
    assert order["broker_order_id"] == execution["broker_order_id"]
    assert text_a == text_a_again
    # different session (different salt) → different tokens
    tree_b = json.loads(text_b)
    assert tree_b["steps"]["executions"][0]["broker_order_id"] != execution["broker_order_id"]
    # never a reversible transform: the raw values do not survive anywhere in the bytes
    for raw in ("DEMO-EXEC-0001", '"broker_order_id": 7001', '"permanent_id": 9001'):
        assert raw not in text_a, raw
    # None stays None; order_ref is Chronos's own id and is left alone
    assert order["permanent_id"] is None
    assert order["order_ref"] == "CHR-DEMO-PARTIAL"


def test_2_numeric_identifiers_are_replaced_as_whole_values_never_as_digit_substrings() -> None:
    m = _load_capture_module()
    tree = json.loads(m.sanitize_capture(_identifier_payload(), set(), PEPPER))
    execution = tree["steps"]["executions"][0]
    # the digits 7001 also occur inside a quantity, a timestamp and a balance — all untouched
    assert execution["quantity"] == "17001"
    assert execution["executed_at"] == "2026-07-21T14:00:07.001+00:00"
    assert tree["steps"]["account_summary"]["net_liquidation"] == "700100"
    assert execution["broker_order_id"].startswith("ORD-")


def test_3_account_id_sanitization_is_byte_identical_before_and_after() -> None:
    m = _load_capture_module()
    payload = _identifier_payload()
    account_ids = {"DU1234567"}
    expected_token = f"ACCT-{account_fingerprint('DU1234567')[:16]}"
    # sanitize() itself: unchanged contract
    assert m.sanitize('{"account_id": "DU1234567"}', account_ids) == (
        f'{{"account_id": "{expected_token}"}}'
    )
    # the ACCT- token the identifier pass produces is the one the textual pass alone produces
    with_identifiers = m.sanitize_capture(payload, account_ids, PEPPER)
    textual_only = m.sanitize(m.canonical_json(payload), account_ids)
    assert with_identifiers.count(expected_token) == textual_only.count(expected_token) == 1
    assert "DU1234567" not in with_identifiers


def test_4_replay_check_byte_integrity_and_mutation_scan_hold_on_a_sanitized_capture(
    tmp_path: Path,
) -> None:
    m = _load_capture_module()
    capture = _demo_capture(m, "g1-replay")
    session = tmp_path / "session"
    m.write_session(session, capture, set(), "g1-replay", PEPPER)
    run = [sys.executable, str(REPLAY_CHECK), "--allow-demo", str(session)]
    passed = subprocess.run(
        run,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **SAFE_ENV},
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "[PASS]" in passed.stdout
    # integrity still bites: one byte of the sanitized capture edited → FAIL
    target = session / "capture.json"
    target.write_text(
        target.read_text(encoding="utf-8").replace("ORD-", "ORX-", 1), encoding="utf-8"
    )
    tampered = subprocess.run(
        run,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **SAFE_ENV},
    )
    assert tampered.returncode != 0
    assert "sha256 drift" in tampered.stdout + tampered.stderr


def test_5_demo_capture_round_trips_with_no_raw_identifier_in_the_bytes(tmp_path: Path) -> None:
    m = _load_capture_module()
    capture = _demo_capture(m, "g1-roundtrip")
    raw_executions = capture["steps"]["executions"]
    raw_orders = capture["steps"]["open_orders"]
    # positive control: the demo adapter really yields identifiers to sanitize
    assert raw_executions and raw_orders
    raw_exec_id = raw_executions[0]["execution_id"]  # steps hold to_jsonable() dicts
    raw_order_id = raw_executions[0]["broker_order_id"]
    raw_perm_id = raw_executions[0]["permanent_id"]
    assert isinstance(raw_exec_id, str) and isinstance(raw_order_id, int) and raw_perm_id
    session = tmp_path / "session"
    m.write_session(session, capture, set(), "g1-roundtrip", PEPPER)
    text = (session / "capture.json").read_text(encoding="utf-8")
    tree = json.loads(text)  # parses
    assert raw_exec_id not in text
    assert re.search(rf'"broker_order_id": {raw_order_id}\b', text) is None
    assert re.search(rf'"permanent_id": {raw_perm_id}\b', text) is None
    execution = tree["steps"]["executions"][0]
    order = tree["steps"]["open_orders"][0]
    assert execution["execution_id"].startswith("EXEC-")
    assert execution["broker_order_id"].startswith("ORD-")
    assert execution["permanent_id"].startswith("PERM-")
    assert order["broker_order_id"] == execution["broker_order_id"]


# ------------------------------------------------------------------------ G-1 r1
# Daybreak's HOLD at 6d21d00 (P1): a public salt over bounded numeric broker ids is
# enumerable. Muse's ruling (2026-09-16): keyed HMAC-SHA256 under a per-install secret
# pepper, CHRONOS_CAPTURE_PEPPER. Tests are numbered to the G-1r1 contract.


def _message(value: int | str, kind: str = "ord", salt: tuple[str, str] = SALT) -> bytes:
    """The HMAC message: a canonical JSON array of the four fields (r2, injective)."""

    label, captured_at = salt
    fields = [f"chronos-{kind}", label, captured_at, str(value)]
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _old_message(value: int) -> bytes:
    """The r1 colon-joined message Daybreak's r1 probe enumerated (kept as the (a) control)."""

    return f"chronos-ord:{SALT[0]}:{SALT[1]}:{value}".encode()


def test_r1_1_tokens_resist_bounded_enumeration_without_the_pepper() -> None:
    m = _load_capture_module()
    # the token the harness mints for order id 7001 under the public session salt
    assert m.session_salt(_identifier_payload("session-1")) == SALT
    tree = json.loads(m.sanitize_capture(_identifier_payload("session-1"), set(), PEPPER))
    token = tree["steps"]["executions"][0]["broker_order_id"]
    assert token == m.identifier_pseudonym("ORD", 7001, SALT, PEPPER)
    # the construction, literally: kind prefix + HMAC-SHA256(pepper, message)[:16]
    assert token == "ORD-" + hmac.new(PEPPER, _message(7001), "sha256").hexdigest()[:16]
    tail = token.removeprefix("ORD-")
    candidates = range(1, 10_001)
    # (a) Daybreak's probe — the OLD public-salt sha256 construction — recovers nothing
    assert [v for v in candidates if sha256(_old_message(v)).hexdigest()[:16] == tail] == []
    # (b) HMAC under an empty key, and under a different pepper — nothing
    for key in (b"", OTHER_PEPPER):
        hits = [
            v for v in candidates if hmac.new(key, _message(v), "sha256").hexdigest()[:16] == tail
        ]
        assert hits == [], key
    # (c) positive control: with the pepper the enumeration finds exactly 7001
    hits = [
        v for v in candidates if hmac.new(PEPPER, _message(v), "sha256").hexdigest()[:16] == tail
    ]
    assert hits == [7001]


def test_r1_2_no_pepper_is_a_typed_refusal_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _load_capture_module()
    guidance = ("CHRONOS_CAPTURE_PEPPER", "secrets.token_hex(32)", ".env", "0600")
    monkeypatch.delenv("CHRONOS_CAPTURE_PEPPER", raising=False)
    with pytest.raises(m.CaptureRefused) as absent:
        m.load_pepper(env_file=None)
    for needle in guidance:
        assert needle in str(absent.value), needle
    # a 10-character pepper: refused as too short (5 bytes; 32 are required)
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", "0123456789")
    with pytest.raises(m.CaptureRefused, match="too short") as short:
        m.load_pepper(env_file=None)
    for needle in guidance:
        assert needle in str(short.value), needle
    # not hex → refused too; no derivation, no default
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", "z" * 64)
    with pytest.raises(m.CaptureRefused, match="hex"):
        m.load_pepper(env_file=None)
    # the pepper lives in the untracked per-machine env file: the file source is read
    monkeypatch.delenv("CHRONOS_CAPTURE_PEPPER", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"CHRONOS_CAPTURE_PEPPER={PEPPER_HEX}\n", encoding="utf-8")
    assert m.load_pepper(env_file=env_file) == PEPPER
    # the environment wins over the file (the repo's settings precedence)
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", OTHER_PEPPER.hex())
    assert m.load_pepper(env_file=env_file) == OTHER_PEPPER
    # the write path refuses BEFORE creating the session directory
    session = tmp_path / "session"
    for bad in (b"", b"short", PEPPER[:31]):
        with pytest.raises(m.CaptureRefused):
            m.sanitize_capture(_identifier_payload(), set(), bad)
        with pytest.raises(m.CaptureRefused):
            m.write_session(session, _identifier_payload(), set(), "g1r1-nopepper", bad)
        assert not session.exists()
    # and the CLI: no pepper anywhere → REFUSED, exit 2, no session directory, no broker step
    monkeypatch.delenv("CHRONOS_CAPTURE_PEPPER", raising=False)
    env = {key: value for key, value in os.environ.items() if key != "CHRONOS_CAPTURE_PEPPER"}
    bare = tmp_path / "bare"  # a cwd with no .env: the only source is the (absent) variable
    bare.mkdir()
    out = bare / "cli-session"
    refused = subprocess.run(
        [
            sys.executable,
            str(CAPTURE),
            "--out",
            str(out),
            "--label",
            "g1r1-nopepper",
            "--allow-demo",
            "--skip-options",
            "--skip-bars",
        ],
        cwd=bare,
        env={**env, **SAFE_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert refused.stdout.startswith("REFUSED: CHRONOS_CAPTURE_PEPPER"), refused.stdout
    assert "secrets.token_hex(32)" in refused.stdout
    assert "capture written" not in refused.stdout
    assert not out.exists()


def test_r1_3_the_pepper_appears_in_no_output_byte_and_no_log_line(tmp_path: Path) -> None:
    session = tmp_path / "session"
    written = subprocess.run(
        [
            sys.executable,
            str(CAPTURE),
            "--out",
            str(session),
            "--label",
            "g1r1-pepper",
            "--allow-demo",
            "--skip-options",
            "--skip-bars",
        ],
        cwd=tmp_path,
        env={**os.environ, **SAFE_ENV, "CHRONOS_CAPTURE_PEPPER": PEPPER_HEX},
        capture_output=True,
        check=False,
    )
    assert written.returncode == 0, written.stdout + written.stderr
    files = {path.name: path.read_bytes() for path in session.iterdir()}
    assert {"capture.json", "derived_liquid_hours.json", "manifest.json"} <= set(files)
    # positive control: the capture really carries pseudonymized identifiers, and THIS pepper
    # minted them (the manifest's fingerprint) — used, yet present nowhere as bytes
    assert b'"broker_order_id": "ORD-' in files["capture.json"]
    manifest = json.loads(files["manifest.json"])
    assert (
        manifest["identifier_pseudonyms"]["pepper_fingerprint"] == sha256(PEPPER).hexdigest()[:16]
    )
    needles = (PEPPER_HEX.encode(), PEPPER_HEX.upper().encode(), PEPPER)
    for name, data in files.items():
        for needle in needles:
            assert needle not in data, name
    for stream in (written.stdout, written.stderr):
        for needle in needles:
            assert needle not in stream


def test_r1_4_manifest_records_the_scheme_and_a_pepper_fingerprint(tmp_path: Path) -> None:
    m = _load_capture_module()
    account_ids = {"DU1234567"}
    acct = f"ACCT-{account_fingerprint('DU1234567')[:16]}"
    sessions = {}
    for name, pepper in (("a", PEPPER), ("b", OTHER_PEPPER)):
        directory = tmp_path / name
        m.write_session(directory, _identifier_payload(), account_ids, "g1r1-manifest", pepper)
        sessions[name] = (
            json.loads((directory / "manifest.json").read_text(encoding="utf-8")),
            json.loads((directory / "capture.json").read_text(encoding="utf-8")),
            (directory / "manifest.json").read_text(encoding="utf-8"),
        )
    manifest_a, capture_a, manifest_text_a = sessions["a"]
    manifest_b, capture_b, _ = sessions["b"]
    expected = {"scheme": "hmac-sha256-v2", "pepper_fingerprint": sha256(PEPPER).hexdigest()[:16]}
    assert manifest_a["identifier_pseudonyms"] == expected
    fingerprint = manifest_a["identifier_pseudonyms"]["pepper_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    # the fingerprint is not the pepper (nor any slice of it)
    assert fingerprint not in PEPPER_HEX and PEPPER_HEX not in manifest_text_a
    # a rotated pepper → a different fingerprint and different identifier tokens ...
    assert manifest_b["identifier_pseudonyms"]["pepper_fingerprint"] != fingerprint
    assert manifest_b["identifier_pseudonyms"]["scheme"] == "hmac-sha256-v2"
    order_a = capture_a["steps"]["executions"][0]["broker_order_id"]
    order_b = capture_b["steps"]["executions"][0]["broker_order_id"]
    assert order_a.startswith("ORD-") and order_b.startswith("ORD-") and order_a != order_b
    # ... while the account tokens do not move with the pepper
    assert capture_a["steps"]["account_summary"]["account_id"] == acct
    assert capture_b["steps"]["account_summary"]["account_id"] == acct
    # the file sha256 map is unchanged in shape (replay_check reads only that)
    assert set(manifest_a["files"]) == {"capture.json", "derived_liquid_hours.json"}


def test_r1_5_docs_state_the_keyed_scheme_and_where_the_pepper_lives() -> None:
    skill = " ".join(SKILL.read_text(encoding="utf-8").split())
    script = " ".join(CAPTURE.read_text(encoding="utf-8").split())
    for text in (skill, script):
        assert "CHRONOS_CAPTURE_PEPPER" in text
        assert "HMAC-SHA256" in text
        # the retired claims: the salt is no longer the whole story, and the secret is decided
        assert "a secret salt is an owner decision" not in text
        assert "does not resist brute force" not in text
    assert "old fixtures stay valid" in skill and "unlinkable" in skill
    assert "losing the pepper loses nothing but linkability" in skill
    assert "0600" in skill and "secrets.token_hex(32)" in skill
    # session_salt's docstring says what the salt is for now: cross-session unlinkability
    m = _load_capture_module()
    salt_doc = " ".join((m.session_salt.__doc__ or "").split())
    assert "public" in salt_doc and "pepper" in salt_doc
    # no salt sentence anywhere else in docs/ or .claude/ says the old thing
    matches = subprocess.run(
        ["git", "grep", "-n", "-i", "salt", "--", "docs", ".claude"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "owner decision" not in matches.lower()


def test_r1_6_no_new_gap_literal_and_the_retired_mechanism_token_is_absent() -> None:
    marker = "[" + "GAP" + "]"  # never spelled whole here: this file must not add one
    gaps = subprocess.run(
        ["git", "grep", "-n", "-F", marker, "--", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    # nothing new: every marker line under tests/ is either #232's own checker or the one
    # pre-existing header comment of this file (6d21d00)
    holders = {line.split(":", 1)[0] for line in gaps}
    assert holders <= {
        "tests/unit/test_m4_session_checklist_contract.py",
        "tests/unit/test_real_gateway_campaign.py",
    }, holders
    own = [line for line in gaps if line.startswith("tests/unit/test_real_gateway_campaign.py:")]
    assert len(own) == 1 and "that no order-id sanitizer exists" in own[0], own
    # #232's mechanism token (never spelled whole here) stays absent from the tree #232 searches,
    # the checklist line that declares the gap and #232's own checker excepted, as there
    token = "sanitize_" + "order_ids"
    absent = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "-F",
            token,
            "--",
            "src",
            "scripts",
            "tests",
            "docs",
            ".claude/skills",
            ":!docs/ops/m4-read-only-gate-session-checklist.md",
            ":!tests/unit/test_m4_session_checklist_contract.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert absent.returncode == 1 and absent.stdout == ""


# ------------------------------------------------------------------------ G-1 r2
# Daybreak's HOLD at 68b76ff (P1): the colon-joined HMAC message was not injective — two
# distinct (label, captured_at, value) triples with byte-identical joins shared a token. The
# message is now a canonical JSON array of the four fields. Tests are numbered to the G-1r2
# contract.

T1 = "2026-09-17T00:00:00+00:00"
T2 = "2026-09-17T00:00:01+00:00"


def _execution_capture(label: str, captured_at: str, execution_id: str) -> dict:
    return {
        "meta": {"label": label, "captured_at_utc": captured_at, "gateway_evidence": False},
        "steps": {"executions": [{"execution_id": execution_id}]},
    }


def _execution_token(m: ModuleType, capture: dict, pepper: bytes = PEPPER) -> str:
    sanitized = json.loads(m.sanitize_capture(capture, set(), pepper))
    return sanitized["steps"]["executions"][0]["execution_id"]


def test_r2_1a_daybreaks_colon_collision_yields_distinct_tokens() -> None:
    """logs/daybreak-probe-G1r1-delimiter-collision.py, verbatim: distinct sessions and distinct
    execution ids whose r1 colon-joins were byte-identical."""

    m = _load_capture_module()
    pepper = bytes.fromhex("42" * 32)  # the probe's pepper: 32 bytes, not a secret
    capture_a = _execution_capture("alpha", T1, f"bridge:{T2}:9001")
    capture_b = _execution_capture(f"alpha:{T1}:bridge", T2, "9001")
    # the r1 message would have been byte-identical for both (the finding)
    joined_a = f"chronos-exec:alpha:{T1}:bridge:{T2}:9001"
    joined_b = f"chronos-exec:alpha:{T1}:bridge:{T2}:9001"
    assert joined_a == joined_b
    token_a = _execution_token(m, capture_a, pepper)
    token_b = _execution_token(m, capture_b, pepper)
    assert token_a != token_b, (token_a, token_b)
    # and directly: session_salt() is the pair, identifier_pseudonym keys the JSON array
    assert m.session_salt(capture_a) == ("alpha", T1)
    assert token_a == m.identifier_pseudonym("EXEC", f"bridge:{T2}:9001", ("alpha", T1), pepper)
    assert (
        token_a
        == "EXEC-"
        + hmac.new(
            pepper, _message(f"bridge:{T2}:9001", "exec", ("alpha", T1)), "sha256"
        ).hexdigest()[:16]
    )
    assert (
        token_b
        == "EXEC-"
        + hmac.new(
            pepper, _message("9001", "exec", (f"alpha:{T1}:bridge", T2)), "sha256"
        ).hexdigest()[:16]
    )
    # a joined-string salt (the r1 shape, what the probe's direct call passes) is refused, not
    # silently unpacked into two characters
    with pytest.raises(TypeError, match="never a joined string"):
        m.identifier_pseudonym("EXEC", value_a := f"bridge:{T2}:9001", f"alpha:{T1}", pepper)
    assert value_a == f"bridge:{T2}:9001"
    # the two JSON messages differ where the colon-joins did not
    assert _message(f"bridge:{T2}:9001", "exec", ("alpha", T1)) != _message(
        "9001", "exec", (f"alpha:{T1}:bridge", T2)
    )


def test_r2_1b_a_hostile_label_stays_distinct_across_sessions_and_stable_within() -> None:
    m = _load_capture_module()
    hostile = 'alpha","x",[1],\n]'  # a quote, brackets, a comma and a newline
    assert all(ch in hostile for ch in ('"', "[", "]", ",", "\n"))
    capture = _execution_capture(hostile, T1, "9001")
    once = _execution_token(m, capture)
    again = _execution_token(m, _execution_capture(hostile, T1, "9001"))
    assert once == again  # stable within the session
    # distinct from a plain label, from the same label at another instant, and from a label
    # whose bytes try to spell the hostile one's JSON encoding
    plain = _execution_token(m, _execution_capture("alpha", T1, "9001"))
    later = _execution_token(m, _execution_capture(hostile, T2, "9001"))
    spelled = _execution_token(m, _execution_capture(json.dumps(hostile)[1:-1], T1, "9001"))
    assert len({once, plain, later, spelled}) == 4
    # the encoding is the one prescribed: json.dumps escapes the quote and the newline
    encoded = _message("9001", "exec", (hostile, T1))
    assert encoded == ('["chronos-exec","alpha\\",\\"x\\",[1],\\n]","' + T1 + '","9001"]').encode(
        "utf-8"
    )
    assert once == "EXEC-" + hmac.new(PEPPER, encoded, "sha256").hexdigest()[:16]
    # the same hostile string in the VALUE slot is a different token from the label slot
    swapped = _execution_token(m, _execution_capture("9001", T1, hostile))
    assert swapped != once


def test_r2_2_scheme_is_v2_and_the_docs_describe_the_json_array_message(tmp_path: Path) -> None:
    m = _load_capture_module()
    assert m.PSEUDONYM_SCHEME == "hmac-sha256-v2"
    session = tmp_path / "session"
    m.write_session(session, _identifier_payload(), set(), "g1r2-scheme", PEPPER)
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["identifier_pseudonyms"]["scheme"] == "hmac-sha256-v2"
    skill = " ".join(SKILL.read_text(encoding="utf-8").split())
    script = " ".join(CAPTURE.read_text(encoding="utf-8").split())
    for text in (skill, script):
        assert '["chronos-<kind>", label, captured_at_utc, str(value)]' in text
        assert "hmac-sha256-v2" in text
        assert "chronos-<kind>:<salt>:<value>" not in text  # the r1 colon form is gone
    assert "injective" in script and "unambiguous" in script


# ------------------------------------------------------------------------ T-2
# The G-1 tail: .env.example documents the pepper; --label is validated before broker contact
# (Daybreak G-1 r2 P2: a surrogate-escaped argv byte raised UnicodeEncodeError at encode time).

ENV_EXAMPLE = ROOT / ".env.example"


def test_t2_2_env_example_documents_the_pepper_without_an_example_value() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "#CHRONOS_CAPTURE_PEPPER=" in lines  # commented, next to the other CHRONOS_ secrets
    block = "\n".join(lines[max(0, lines.index("#CHRONOS_CAPTURE_PEPPER=") - 12) :])
    assert "python3 -c 'import secrets; print(secrets.token_hex(32))'" in block
    for phrase in ("HMAC", "0600", "Never commit it", "old fixtures stay valid"):
        assert phrase in block, phrase
    # no literal value follows the variable, anywhere in the file
    assert re.search(r"CHRONOS_CAPTURE_PEPPER=\s*[0-9a-fA-F]{64}", text) is None
    assert re.search(r"CHRONOS_CAPTURE_PEPPER=\S", text) is None


def _capture_cli(tmp_path: Path, label: str, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CAPTURE),
            "--out",
            str(out),
            "--label",
            label,
            "--allow-demo",
            "--skip-options",
            "--skip-bars",
        ],
        cwd=tmp_path,
        env={**os.environ, **SAFE_ENV, "CHRONOS_CAPTURE_PEPPER": PEPPER_HEX},
        capture_output=True,
        text=True,
        errors="surrogateescape",
        check=False,
    )


def test_t2_3_a_label_that_is_not_utf8_encodable_is_refused_before_any_broker_contact(
    tmp_path: Path,
) -> None:
    m = _load_capture_module()
    surrogate = os.fsdecode(b"label-\xff")  # Daybreak's probe value: a non-UTF-8 argv byte
    with pytest.raises(m.CaptureRefused, match="UTF-8"):
        m.validate_label(surrogate)
    # the CLI: typed refusal, exit 2, no session directory, no capture step ran
    out = tmp_path / "session"
    refused = _capture_cli(tmp_path, surrogate, out)
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert refused.stdout.startswith("REFUSED: --label"), refused.stdout
    assert "UTF-8" in refused.stdout and "UnicodeEncodeError" not in refused.stderr
    assert "capture written" not in refused.stdout and not out.exists()
    # control characters and length are refused by name, too
    for bad, rule in (
        ("a\tb", "control"),
        ("a\nb", "control"),
        ("a\rb", "control"),
        ("x" * 65, "64"),
    ):
        with pytest.raises(m.CaptureRefused, match=rule):
            m.validate_label(bad)
    # positive control: a normal label passes the CLI and mints the same tokens as before
    ok = _capture_cli(tmp_path, "session-1", tmp_path / "ok")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert m.validate_label("session-1") == "session-1"
    assert m.validate_label("x" * 64) == "x" * 64
    tree = json.loads(m.sanitize_capture(_identifier_payload("session-1"), set(), PEPPER))
    assert tree["steps"]["executions"][0]["broker_order_id"] == m.identifier_pseudonym(
        "ORD", 7001, ("session-1", "2026-09-13T19:30:00+00:00"), PEPPER
    )

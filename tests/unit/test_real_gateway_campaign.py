"""Executable contracts for the owner-gated real-gateway campaign helper."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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
# permanent_id), not only account ids — the M4 checklist's "[GAP] no sanitize_order_ids".
# Tests are numbered to the G-1 contract.

REPLAY_CHECK = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py"
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _identifier_payload(label: str = "session-1") -> dict:
    """A capture-shaped tree with one execution and one open order (demo-adapter values)."""

    return {
        "meta": {"captured_at_utc": "2026-09-13T19:30:00+00:00", "label": label},
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
    text_a = m.sanitize_capture(_identifier_payload("session-1"), set())
    text_a_again = m.sanitize_capture(_identifier_payload("session-1"), set())
    text_b = m.sanitize_capture(_identifier_payload("session-2"), set())
    tree_a = json.loads(text_a)
    execution = tree_a["steps"]["executions"][0]
    order = tree_a["steps"]["open_orders"][0]
    # the account-id shape: <KIND>-<16 hex>, a sha256 prefix over a namespaced string
    for token, kind in (
        (execution["execution_id"], "EXEC"),
        (execution["broker_order_id"], "ORD"),
        (execution["permanent_id"], "PERM"),
    ):
        assert re.fullmatch(rf"{kind}-[0-9a-f]{{16}}", token), token
    salt = m.session_salt(_identifier_payload("session-1"))
    assert execution["broker_order_id"] == m.identifier_pseudonym("ORD", 7001, salt)
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
    tree = json.loads(m.sanitize_capture(_identifier_payload(), set()))
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
    with_identifiers = m.sanitize_capture(payload, account_ids)
    textual_only = m.sanitize(m.canonical_json(payload), account_ids)
    assert with_identifiers.count(expected_token) == textual_only.count(expected_token) == 1
    assert "DU1234567" not in with_identifiers


def test_4_replay_check_byte_integrity_and_mutation_scan_hold_on_a_sanitized_capture(
    tmp_path: Path,
) -> None:
    m = _load_capture_module()
    capture = _demo_capture(m, "g1-replay")
    session = tmp_path / "session"
    m.write_session(session, capture, set(), "g1-replay")
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
    m.write_session(session, capture, set(), "g1-roundtrip")
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

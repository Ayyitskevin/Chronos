"""EV-1 — replay-through-persistence contract.

A captured session directory (the exact ``capture_readonly.py`` layout: ``manifest.json``,
``capture.json``, ``derived_liquid_hours.json``) is compared with what the repository's OWN
broker-facing persistence path wrote for the same broker facts:

* executions → the reconciliation seam (``ReconciliationCoordinator.reconcile`` with an
  injected ``ExecutionRepository``, the wiring ``chronos.runtime`` uses) → ``fills`` +
  ``commissions`` rows;
* order identity → ``resolve_from_broker_evidence`` → ``OrderTracker.ingest`` (the ONE
  callback→event call site) → ``order_events.permanent_id`` / ``client_id``.

Equality is under the capture's own keyed pseudonym scheme: the persisted RAW identifiers are
pseudonymized with the capture tool's ``identifier_pseudonym`` (pepper from the environment,
salt = the session's public label + capture time) and compared with the captured tokens. The
capture is never de-pseudonymized; the pepper is never written. ``client_id`` is not in the
tool's ``IDENTIFIER_KEYS`` and stays raw in a capture, so it compares raw.

Two modes, one comparison:
* a DEMO rehearsal (``manifest.gateway_evidence`` false, no ``CHRONOS_REPLAY_DATABASE_URL``):
  the test drives the runtime path itself against the in-process demo broker — the same canned
  broker facts the fixture was captured from — into a temporary database;
* a REAL session (``gateway_evidence`` true, or ``CHRONOS_REPLAY_DATABASE_URL`` set): the rows
  come from the database the runtime wrote during that read-only session; nothing is driven.

The test never passes vacuously: a session with zero executions FAILS, and a missing or wrong
pepper FAILS with the capture tool's own refusal text (a skip is not allowed).
Numbered to the EV-1 packet contract.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.config.settings import Settings
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.orders.reconciliation_recovery import resolve_from_broker_evidence
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import ExecutionRepository
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.persistence.repositories import LocalReconciliationRepository, account_fingerprint
from chronos.services.reconciliation import ReconciliationCoordinator

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".claude/skills/chronos-real-gateway-campaign/scripts"
#: A committed demo rehearsal, when one exists. It does not yet: the tracked-file secret scan
#: flags two hex strings in any capture manifest (a file sha256 and the pepper fingerprint), and
#: the reviewed baseline that would admit them is in flight elsewhere — so, until that entry lands,
#: the rehearsal is MINTED at test time by the capture tool itself (same code, same pepper phrase).
COMMITTED_SESSION = ROOT / "tests/fixtures/ibkr_demo/rehearsal"
DEMO_LABEL = "ev1-demo-rehearsal"
SESSION_ENV = "CHRONOS_REPLAY_SESSION_DIR"
DATABASE_ENV = "CHRONOS_REPLAY_DATABASE_URL"
PEPPER_ENV = "CHRONOS_CAPTURE_PEPPER"
#: The committed demo fixture was minted with a pepper DERIVED from this public phrase. It is
#: a rehearsal key for a rehearsal fixture (gateway_evidence=false); it can never unlock a real
#: session because the manifest's pepper fingerprint would not match.
DEMO_FIXTURE_PEPPER_PHRASE = b"chronos-ev1-demo-rehearsal-fixture-pepper"
#: The demo broker's canned partial fill (src/chronos/broker/demo.py, SAFETY_CASES profile).
DEMO_CLIENT_ID = 17
DEMO_ORDER_REF = "CHR-DEMO-PARTIAL"
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}

ExecutionFact = tuple[str, str | None, int | None, Decimal, str]
IdentityFact = tuple[str, str | None, int | None]


# ------------------------------------------------------------------ the capture tool's own code


def _load(name: str) -> ModuleType:
    module_name = f"chronos_real_gateway_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _capture_module() -> ModuleType:
    return _load("capture_readonly")


def _replay_module() -> ModuleType:
    if str(SCRIPTS) not in sys.path:  # replay_check imports capture_readonly by directory
        sys.path.insert(0, str(SCRIPTS))
    return _load("replay_check")


def demo_fixture_pepper() -> bytes:
    return hashlib.sha256(DEMO_FIXTURE_PEPPER_PHRASE).digest()


# ------------------------------------------------------------------ the session directory


_MINTED: dict[str, Path] = {}


def mint_demo_rehearsal() -> Path:
    """Run the capture tool's own demo rehearsal (``--allow-demo --skip-bars``) once per process.

    The exact production write path (``run_capture`` + ``write_session``), the demo broker's
    canned facts, and the derived rehearsal pepper — so the minted directory is byte-for-byte the
    kind of session ``capture_readonly.py --out <dir> --label ev1-demo-rehearsal --allow-demo
    --skip-bars`` writes, apart from ``captured_at_utc``.
    """

    if "dir" not in _MINTED:
        capture_module = _capture_module()
        args = argparse.Namespace(
            label=DEMO_LABEL,
            symbols=None,
            max_symbols=2,
            skip_options=False,
            skip_bars=True,
            account_ids=set(),
        )
        capture = asyncio.run(capture_module.run_capture(Settings(broker_mode="demo"), args))
        out_dir = Path(tempfile.mkdtemp(prefix="chronos-ev1-rehearsal-")) / DEMO_LABEL
        capture_module.write_session(
            out_dir, capture, args.account_ids, DEMO_LABEL, demo_fixture_pepper()
        )
        _MINTED["dir"] = out_dir
    return _MINTED["dir"]


def session_dir() -> Path:
    override = os.environ.get(SESSION_ENV)
    if override:
        path = Path(override)
        if not path.is_dir():
            pytest.fail(f"session directory {path} ({SESSION_ENV}) does not exist")
        return path
    if COMMITTED_SESSION.is_dir():
        return COMMITTED_SESSION
    return mint_demo_rehearsal()


def load_session(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Byte integrity, derivation replay and the mutation scan — the replay tool's own check."""

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    failures = _replay_module().check_session(
        directory, allow_demo=not bool(manifest.get("gateway_evidence", False))
    )
    if failures:
        pytest.fail("replay_check refused the session: " + "; ".join(failures))
    capture = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    return manifest, capture


def resolve_pepper(manifest: dict[str, Any]) -> bytes:
    """The pepper that minted this session's tokens, or a refusal — never a skip."""

    capture = _capture_module()
    expected = str(manifest.get("identifier_pseudonyms", {}).get("pepper_fingerprint", ""))
    supplied = os.environ.get(PEPPER_ENV, "")
    if supplied.strip():
        try:
            pepper = capture.pepper_from_text(supplied)
        except capture.CaptureRefused as error:
            pytest.fail(f"refused: {error}")
        if capture.pepper_fingerprint(pepper) != expected:
            pytest.fail(
                f"refused: {PEPPER_ENV} does not fingerprint to the session's "
                f"pepper_fingerprint {expected!r}; tokens cannot be compared"
            )
        return pepper
    demo = demo_fixture_pepper()
    if not manifest.get("gateway_evidence", False) and capture.pepper_fingerprint(demo) == expected:
        return demo
    pytest.fail(
        f"refused: {PEPPER_ENV} is not set and this session was not minted with the demo "
        f"rehearsal pepper (manifest pepper_fingerprint {expected!r}); identifier pseudonyms "
        "are keyed and a comparison without the key is refused"
    )


def _step(capture: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = capture["steps"].get(name)
    if not isinstance(value, list):
        pytest.fail(f"the capture's {name!r} step is not a list of observations: {value!r}")
    return value


def captured_executions(capture: dict[str, Any]) -> set[ExecutionFact]:
    facts = {
        (
            str(item["execution_id"]),
            None if item.get("permanent_id") is None else str(item["permanent_id"]),
            None if item.get("client_id") is None else int(item["client_id"]),
            Decimal(str(item["commission"])),
            str(item["commission_currency"]),
        )
        for item in _step(capture, "executions")
    }
    if not facts:
        pytest.fail(
            "the session has zero captured executions: this contract has nothing to compare "
            "and refuses to pass vacuously (a session with executions is required)"
        )
    return facts


def captured_order_identities(capture: dict[str, Any]) -> set[IdentityFact]:
    return {
        (
            str(item["broker_order_id"]),
            None if item.get("permanent_id") is None else str(item["permanent_id"]),
            None if item.get("client_id") is None else int(item["client_id"]),
        )
        for item in _step(capture, "open_orders") + _step(capture, "executions")
    }


# ------------------------------------------------------------------ the persisted rows, tokenized


def _token(capture: ModuleType, kind: str, value: str | int, salt: Any, pepper: bytes) -> str:
    return str(capture.identifier_pseudonym(kind, value, salt, pepper))


def persisted_executions(
    database: Database, capture_json: dict[str, Any], pepper: bytes
) -> set[ExecutionFact]:
    capture = _capture_module()
    salt = capture.session_salt(capture_json)
    with database.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT f.execution_id, f.permanent_id, f.client_id, c.amount, c.currency "
                "FROM fills f JOIN commissions c ON c.execution_id = f.execution_id"
            )
        ).all()
    return {
        (
            _token(capture, "EXEC", execution_id, salt, pepper),
            None if permanent_id is None else _token(capture, "PERM", permanent_id, salt, pepper),
            None if client_id is None else int(client_id),
            Decimal(str(amount)),
            str(currency),
        )
        for execution_id, permanent_id, client_id, amount, currency in rows
    }


def persisted_order_identities(
    database: Database, capture_json: dict[str, Any], pepper: bytes
) -> set[IdentityFact]:
    capture = _capture_module()
    salt = capture.session_salt(capture_json)
    with database.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT broker_order_id, permanent_id, client_id FROM order_events "
                "WHERE permanent_id IS NOT NULL OR client_id IS NOT NULL"
            )
        ).all()
    return {
        (
            _token(capture, "ORD", broker_order_id, salt, pepper),
            None if permanent_id is None else _token(capture, "PERM", permanent_id, salt, pepper),
            None if client_id is None else int(client_id),
        )
        for broker_order_id, permanent_id, client_id in rows
    }


# ------------------------------------------------------------------ driving the runtime path (demo)


def drive_demo_runtime_path(database: Database) -> tuple[str, bool]:
    """Persist the demo broker's facts through the SAME entry points the runtime wires.

    Returns the owned intent id and whether the identity ingest changed its lifecycle.
    """

    broker = DemoBroker()
    connection = BrokerConnectionManager(broker)
    connection.connect()
    try:
        # (1) executions: chronos.runtime wires ReconciliationCoordinator(connection,
        #     LocalReconciliationRepository(sessions), allowlist,
        #     execution_repository=ExecutionRepository(sessions)) — the BP-1b seam.
        result = ReconciliationCoordinator(
            connection,
            LocalReconciliationRepository(database.sessions),
            ("MSFT", "AMD", "TSLA"),
            execution_repository=ExecutionRepository(database.sessions),
        ).reconcile()
        assert result.snapshot is not None, result.reasons
        # (2) order identity: an OWNED working order reaches order_events through
        #     resolve_from_broker_evidence → OrderTracker.ingest (BP-2's ingest). The demo
        #     broker's canned partial fill is owned by recording the intent the runtime would
        #     have recorded at submission, with that order's CHR- reference.
        open_orders = connection.run(broker.open_orders())
        executions = connection.run(broker.executions())
    finally:
        connection.close()
    owned = [order for order in open_orders if order.order_ref == DEMO_ORDER_REF]
    assert len(owned) == 1, "the demo broker exposes exactly one canned working order"
    order = owned[0]
    intents = OrderIntentRepository(database.sessions)
    tracker = OrderTracker(intents, OrderTrackerRepository(database.sessions))
    intent = OrderIntentRecord(
        intent_id="ev1-demo-intent",
        idempotency_key="ev1-demo-intent-key",
        account_fingerprint=account_fingerprint(DEMO_ACCOUNT_ID),
        environment="paper",
        product_family=ProductFamily.OPTION,
        wheel_cycle_id=None,
        symbol=order.contract.symbol,
        con_id=order.contract.con_id,
        local_symbol=order.contract.local_symbol,
        action=OrderSide(order.side),
        open_close_effect="OPEN",
        quantity=order.quantity,
        order_type="LMT",
        limit_price=order.limit_price,
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=order.order_ref,
        status=OrderLifecycle.SUBMITTED,
        created_at=datetime.now(tz=UTC),
        confirmed_at=None,
        submitted_at=None,
        expires_at=None,
    )
    assert intents.create(intent, current_account_id=DEMO_ACCOUNT_ID)
    update = resolve_from_broker_evidence(
        intent,
        open_orders=open_orders,
        executions=executions,
        current_account_id=DEMO_ACCOUNT_ID,
        expected_broker_client_id=DEMO_CLIENT_ID,
        expected_limit_price=order.limit_price,
        persisted_broker_order_id=None,
        now=datetime.now(tz=UTC),
    )
    assert update is not None, "the runtime's resolver did not match the owned demo order"
    outcome = tracker.ingest(update, current_account_id=DEMO_ACCOUNT_ID)
    return intent.intent_id, bool(outcome.lifecycle_changed)


def _demo_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'ev1-replay.db'}")
    database.initialize()
    database.bind_scope(broker_mode="demo", environment="paper", account_id=DEMO_ACCOUNT_ID)
    return database


def _rows_for(manifest: dict[str, Any], tmp_path: Path) -> tuple[Database, bool]:
    """The database whose rows are compared, and whether this test drove them (demo)."""

    url = os.environ.get(DATABASE_ENV, "")
    if url.strip():
        return Database(url), False
    if manifest.get("gateway_evidence", False):
        pytest.fail(
            f"refused: this session is gateway evidence, so its rows must come from the "
            f"database the runtime wrote during the session — set {DATABASE_ENV}"
        )
    database = _demo_database(tmp_path)
    drive_demo_runtime_path(database)
    return database, True


# ------------------------------------------------------------------ 1. the path is the runtime's


def test_1_the_entry_points_this_test_drives_are_the_ones_the_runtime_wires() -> None:
    runtime = (ROOT / "src/chronos/runtime.py").read_text(encoding="utf-8")
    assert "ReconciliationCoordinator(" in runtime
    assert re.search(r"execution_repository=ExecutionRepository\(", runtime), (
        "chronos.runtime does not inject ExecutionRepository into the reconciliation seam "
        "(BP-1b, #250): the fills/commissions path this test replays through is missing"
    )
    recovery = (ROOT / "src/chronos/orders/reconciliation_recovery.py").read_text(encoding="utf-8")
    assert "self._tracker.ingest(update" in recovery, "the recovery path no longer ingests"
    tracker = (ROOT / "src/chronos/orders/tracker.py").read_text(encoding="utf-8")
    assert re.search(
        r"permanent_id=update\.permanent_id,\s*client_id=update\.client_id", tracker
    ), (
        "OrderTracker.ingest does not record permanent_id/client_id columns (BP-2, #249): "
        "the order_events identity path this test replays through is missing"
    )


# ------------------------------------------------------------------ 2. rows == captured bytes


def test_2_persisted_rows_equal_the_captured_bytes_under_the_capture_scheme(
    tmp_path: Path,
) -> None:
    directory = session_dir()
    manifest, capture = load_session(directory)
    pepper = resolve_pepper(manifest)
    expected_executions = captured_executions(capture)
    expected_identities = captured_order_identities(capture)

    database, driven = _rows_for(manifest, tmp_path)
    try:
        assert persisted_executions(database, capture, pepper) == expected_executions
        identities = persisted_order_identities(database, capture, pepper)
        # every persisted identity names an order the session observed; in a read-only session
        # with no owned intent this set is empty and the executions above carry the proof
        assert identities <= expected_identities, identities - expected_identities
        if driven:
            assert (
                identities == {fact for fact in expected_identities if fact[2] == DEMO_CLIENT_ID}
                and identities
            ), "the owned demo order did not reach order_events with identity"
    finally:
        database.dispose()


def test_2b_a_missing_or_wrong_pepper_is_refused_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = load_session(session_dir())
    foreign = dict(manifest)
    foreign["identifier_pseudonyms"] = {"scheme": "hmac-sha256-v2", "pepper_fingerprint": "0" * 16}
    monkeypatch.delenv(PEPPER_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception, match=f"refused: {PEPPER_ENV} is not set"):
        resolve_pepper(foreign)
    monkeypatch.setenv(PEPPER_ENV, "ab" * 32)
    with pytest.raises(pytest.fail.Exception, match="does not fingerprint"):
        resolve_pepper(manifest)
    monkeypatch.setenv(PEPPER_ENV, "not-hex")
    with pytest.raises(pytest.fail.Exception, match="refused: "):
        resolve_pepper(manifest)


def test_2c_a_session_with_zero_executions_is_refused(tmp_path: Path) -> None:
    _, capture = load_session(session_dir())
    emptied = json.loads(json.dumps(capture))
    emptied["steps"]["executions"] = []
    with pytest.raises(pytest.fail.Exception, match="zero captured executions"):
        captured_executions(emptied)


# ------------------------------------------------------------------ 3. the committed demo fixture


def test_3_the_demo_rehearsal_fixture_replays_and_carries_no_raw_identifier() -> None:
    fixture = COMMITTED_SESSION if COMMITTED_SESSION.is_dir() else mint_demo_rehearsal()
    replay = _replay_module()
    assert replay.check_session(fixture, allow_demo=True) == []
    assert replay.check_session(fixture, allow_demo=False), (
        "a demo rehearsal must be refused without --allow-demo: it is not gateway evidence"
    )
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gateway_evidence"] is False
    capture_module = _capture_module()
    assert manifest["identifier_pseudonyms"]["pepper_fingerprint"] == (
        capture_module.pepper_fingerprint(demo_fixture_pepper())
    )
    text = "".join(
        (fixture / name).read_text(encoding="utf-8")
        for name in ("manifest.json", "capture.json", "derived_liquid_hours.json")
    )
    assert demo_fixture_pepper().hex() not in text, "the fixture must never carry its pepper"
    assert DEMO_ACCOUNT_ID not in text, "raw account id in the fixture"
    assert "DEMO-EXEC-" not in text, "raw execution id in the fixture"
    capture = json.loads((fixture / "capture.json").read_text(encoding="utf-8"))
    for item in _step(capture, "executions") + _step(capture, "open_orders"):
        assert str(
            item["execution_id" if "execution_id" in item else "broker_order_id"]
        ).startswith(("EXEC-", "ORD-"))
        assert str(item["broker_order_id"]).startswith("ORD-")
        assert item["permanent_id"] is None or str(item["permanent_id"]).startswith("PERM-")
    assert len(_step(capture, "executions")) >= 1

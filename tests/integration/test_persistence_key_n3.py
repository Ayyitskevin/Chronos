"""N3-1: the persisted account fingerprint is keyed, and an unkeyed database is never adopted.

Kevin's ruling (iii): a fresh keyed database at schema 18 under ``CHRONOS_PERSISTENCE_KEY``;
schema-17 databases are refused; no legacy acceptance of the unkeyed value anywhere. These tests
pin that at public seams:

* T1-T4 build the real runtime (``build_runtime``, demo broker) against a temporary database.
  T1 compares the stored value with an HMAC the TEST computes from the key it set, so the
  expected value never comes from the code under test.
* 0017 / fresh init / startup refusal run Alembic and ``Database.initialize`` on real files.
* The formula scan reads ``src`` for the unkeyed construction (kills a dual-read path).

Every key is derived at run time (``secrets.token_hex(32)``); no key literal appears here.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from tests.integration.test_migrations import _alembic_config

from chronos import runtime as runtime_module
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.config.settings import Settings
from chronos.persistence.database import Database
from chronos.persistence.schema import Base

_KEY_ENV = "CHRONOS_PERSISTENCE_KEY"
_SRC = Path(__file__).resolve().parents[2] / "src" / "chronos"
#: The schema this slice establishes, written as a literal on purpose: the test must not read
#: SCHEMA_VERSION from the code it is checking.
_SCHEMA_18 = 18


def _keyed(key_hex: str, account_id: str) -> str:
    """The value the ruling requires: HMAC-SHA256(key, "chronos-account:" + id)."""

    message = f"chronos-account:{account_id}".encode()
    return hmac.new(bytes.fromhex(key_hex), message, hashlib.sha256).hexdigest()


def _unkeyed(account_id: str) -> str:
    """What anyone holding only the database can compute: no secret at all."""

    return hashlib.sha256(f"chronos-account:{account_id}".encode()).hexdigest()


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chronos.db'}",
            "log_file": tmp_path / "logs" / "chronos.log",
        }
    )


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory: relative data/ paths and any .env lookup land in it."""

    directory = tmp_path / "runtime"
    directory.mkdir()
    monkeypatch.chdir(directory)
    logging_calls: list[object] = []
    monkeypatch.setattr(
        runtime_module, "configure_logging", lambda *a, **k: logging_calls.append(a)
    )
    monkeypatch.setattr(runtime_module, "get_settings", lambda: _settings(directory))
    return directory


def _build_with_key(monkeypatch: pytest.MonkeyPatch, key_hex: str | None):
    if key_hex is None:
        monkeypatch.delenv(_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(_KEY_ENV, key_hex)
    return runtime_module.build_runtime(register_atexit=False)


def _stored_scope(directory: Path) -> tuple[str, str, str]:
    uri = f"file:{directory / 'chronos.db'}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute(
            "SELECT broker_mode, environment, account_fingerprint FROM database_scope WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return tuple(row)


# ----------------------------------------------------------------------------- T1-T4


def test_t1_the_stored_fingerprint_is_the_keyed_hmac_and_not_enumerable(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_hex = secrets.token_hex(32)
    _build_with_key(monkeypatch, key_hex).close()

    stored = _stored_scope(runtime_dir)[2]

    assert stored == _keyed(key_hex, DEMO_ACCOUNT_ID)
    assert stored != _unkeyed(DEMO_ACCOUNT_ID)
    # The attacker's move over the synthetic id space around the demo account: no secret,
    # just candidates. With the keyed value nothing in the space reproduces it.
    space = [f"DU123{n:04d}" for n in range(10_000)]
    assert DEMO_ACCOUNT_ID in space
    assert [candidate for candidate in space if _unkeyed(candidate) == stored] == []


def test_t2_the_same_key_rebinds_the_same_database_after_a_restart(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_hex = secrets.token_hex(32)
    _build_with_key(monkeypatch, key_hex).close()
    first = _stored_scope(runtime_dir)

    _build_with_key(monkeypatch, key_hex).close()  # a restart: binds again, no refusal

    assert _stored_scope(runtime_dir) == first
    assert first[2] == _keyed(key_hex, DEMO_ACCOUNT_ID)


_MALFORMED = {
    "absent": None,
    "empty": "",
    "uppercase": "AB" * 32,
    "short": "ab" * 31,
    "long": "ab" * 33,
    "not-hex": "zz" * 32,
    "0x-prefixed": "0x" + "ab" * 31,
    "padded": " " + "ab" * 32,
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_t3_an_absent_or_malformed_key_refuses_demo_startup_and_writes_nothing(
    case: str, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _MALFORMED[case]
    logged: list[object] = []
    monkeypatch.setattr(runtime_module, "configure_logging", lambda *a, **k: logged.append(a))

    with pytest.raises(RuntimeError, match=_KEY_ENV) as refused:
        _build_with_key(monkeypatch, value)

    # Demo mode included, and refused BEFORE anything exists: no database file, no sidecar,
    # no log file, no data/ directory -- the working directory is still empty.
    assert sorted(path.name for path in runtime_dir.iterdir()) == []
    assert logged == []
    if value:
        assert value.strip() not in str(refused.value)  # the bad value is never echoed


def test_t4_a_database_bound_under_key_a_is_refused_under_key_b(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    _build_with_key(monkeypatch, key_a).close()
    bound = _stored_scope(runtime_dir)

    # The layer: Database.bind_scope compares the stored fingerprint with the one recomputed
    # under the configured key, and key B's value differs, so the scope is "different".
    with pytest.raises(RuntimeError, match="already bound to a different broker scope"):
        _build_with_key(monkeypatch, key_b)

    assert _stored_scope(runtime_dir) == bound  # nothing rewritten under the wrong key


# --------------------------------------------------------- schema 18, migration 0017


def _make_v17_database(path: Path, *, scope_fingerprint: str | None) -> None:
    """A schema-17 store as the chain leaves it (alembic stamped 0016), optionally bound."""

    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for version in range(2, 18):
            connection.execute(
                sa.text(
                    "INSERT INTO schema_version (version, applied_at) "
                    f"VALUES ({version}, '2026-01-01 00:00:00.000000')"
                )
            )
        if scope_fingerprint is not None:
            connection.execute(
                sa.text(
                    "INSERT INTO database_scope (id, broker_mode, environment, "
                    "account_fingerprint, bound_at) "
                    "VALUES (1, 'ibkr', 'paper', :fp, '2026-08-01 00:00:00.000000')"
                ),
                {"fp": scope_fingerprint},
            )
    engine.dispose()
    command.stamp(_alembic_config(path), "0016")


def _dump(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def _latest_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(
            connection.execute(
                "SELECT version FROM schema_version ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_0017_refuses_a_bound_database_and_changes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "chronos.db"
    _make_v17_database(db_path, scope_fingerprint=_unkeyed("DU7654321"))
    before = _dump(db_path)

    with pytest.raises(RuntimeError) as refused:
        command.upgrade(_alembic_config(db_path), "head")

    message = str(refused.value)
    assert "database_scope" in message
    assert "docs/ops/N3-CUTOVER.md" in message  # the archive runbook (N3-2)
    assert _dump(db_path) == before  # zero changes: rows, schema_version and alembic_version


def test_0017_refusal_names_the_landed_runbook_and_nothing_forthcoming(tmp_path: Path) -> None:
    """C1 F8: the refusal called the N3-2 cutover runbook "forthcoming"; it landed (#305, #310,
    #311-#313). The message names the page and nothing that is still to come."""

    db_path = tmp_path / "chronos.db"
    _make_v17_database(db_path, scope_fingerprint=_unkeyed("DU7654321"))

    with pytest.raises(RuntimeError) as refused:
        command.upgrade(_alembic_config(db_path), "head")

    message = str(refused.value)
    assert "docs/ops/N3-CUTOVER.md" in message
    assert "forthcoming" not in message


def test_0017_upgrades_an_unbound_v17_database_to_schema_18(tmp_path: Path) -> None:
    db_path = tmp_path / "chronos.db"
    _make_v17_database(db_path, scope_fingerprint=None)

    command.upgrade(_alembic_config(db_path), "head")

    assert _latest_version(db_path) == _SCHEMA_18
    db_path.chmod(0o600)
    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()  # the fail-closed drift check accepts the upgraded store
    finally:
        database.dispose()


def test_a_fresh_database_initializes_at_schema_18(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    database = Database(f"sqlite:///{db_path}")
    try:
        database.initialize()
    finally:
        database.dispose()

    assert _latest_version(db_path) == _SCHEMA_18


def test_a_schema_17_database_is_refused_at_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "chronos.db"
    _make_v17_database(db_path, scope_fingerprint=None)
    db_path.chmod(0o600)

    database = Database(f"sqlite:///{db_path}")
    try:
        with pytest.raises(RuntimeError, match="Unsupported Chronos schema version 17"):
            database.initialize()
    finally:
        database.dispose()


# ------------------------------------------------------------------- no dual read


def _domain_tag_sites() -> list[tuple[Path, ast.AST, ast.AST | None]]:
    """Every expression in src that spells the ``chronos-account:`` domain tag, with the
    call it is an argument of (directly or inside an ``.encode()``/concatenation)."""

    sites: list[tuple[Path, ast.AST, ast.AST | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
                text = node.value if isinstance(node.value, str) else node.value.decode()
                if "chronos-account:" not in text:
                    continue
                enclosing = parents.get(node)
                while enclosing is not None and not isinstance(enclosing, ast.Call):
                    enclosing = parents.get(enclosing)
                # step out of ".encode()" so the reported call is the hash/MAC itself
                while (
                    isinstance(enclosing, ast.Call)
                    and isinstance(enclosing.func, ast.Attribute)
                    and enclosing.func.attr == "encode"
                ):
                    enclosing = parents.get(enclosing)
                    while enclosing is not None and not isinstance(enclosing, ast.Call):
                        enclosing = parents.get(enclosing)
                sites.append((path, node, enclosing))
    return sites


def test_no_module_in_src_computes_the_unkeyed_account_formula() -> None:
    sites = _domain_tag_sites()

    assert sites, "the domain tag must still exist (the ruling keeps it)"
    for path, _, call in sites:
        assert path.relative_to(_SRC).as_posix() == "utils/identifiers.py"
        assert call is not None
        assert ast.unparse(call.func) == "hmac.new", (
            f"{path.name}: the domain tag is hashed by {ast.unparse(call.func)}, not keyed"
        )

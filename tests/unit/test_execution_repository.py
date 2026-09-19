"""BP-1: broker executions persist as executions — the persistence ``fills`` + ``commissions``
tables are the canonical per-execution home (OWNER-ASKS 3, Muse). Pins numbered to the BP-1
contract; item 5 (the execution engine's fill path) is BLOCKED at ``execution/engine.py:298`` —
that line holds a ``BrokerEvent`` (intent_id, cumulative_filled, average_fill_price,
commission_usd), never a ``BrokerExecution`` — and is pinned here only as the absence of any
engine import of this module."""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from chronos.domain.enums import OptionRight, OrderSide
from chronos.domain.models import BrokerExecution, OptionContract, UnderlyingContract
from chronos.persistence.database import SCHEMA_VERSION, Database
from chronos.persistence.execution_repository import ExecutionConflict, ExecutionRepository
from chronos.persistence.schema import CommissionRow, FillRow
from chronos.utils.identifiers import account_fingerprint

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "chronos" / "persistence" / "execution_repository.py"
ACCOUNT_ID = "DU1234567"
NOW = datetime(2026, 9, 19, 13, 0, tzinfo=UTC)
FORBIDDEN_PREFIXES = (
    "chronos.orders.risk",
    "chronos.orders.submission",
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.control",
    "chronos.execution",
)
SAFE_ENV = {
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _option(symbol: str = "AAPL") -> OptionContract:
    return OptionContract(
        con_id=2002,
        symbol=symbol,
        expiration=date(2026, 10, 16),
        strike=Decimal("150"),
        right=OptionRight.PUT,
        multiplier=Decimal("100"),
        trading_class=symbol,
        local_symbol=f"{symbol}-261016-P-150",
        currency="USD",
    )


def _execution(
    *,
    execution_id: str = "0000e0d5.64f1a2b3.01.01",
    account_id: str = ACCOUNT_ID,
    quantity: str = "1",
    price: str = "2.00",
    commission: str | None = "0.65",
    commission_currency: str | None = "USD",
    order_ref: str | None = "CHR-BP1-1",
    contract: UnderlyingContract | OptionContract | None = None,
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=account_id,
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        order_ref=order_ref,
        contract=contract or _option(),
        side=OrderSide.SELL,
        quantity=Decimal(quantity),
        price=Decimal(price),
        timestamp=NOW,
        commission=Decimal(commission) if commission is not None else None,
        commission_currency=commission_currency,
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    db.bind_scope(broker_mode="demo", environment="paper", account_id=ACCOUNT_ID)
    yield db
    db.dispose()


def _rows(database: Database) -> tuple[list[tuple], list[tuple]]:
    with database.sessions() as session:
        fills = [
            (
                r.execution_id,
                r.broker_order_id,
                r.permanent_id,
                r.client_id,
                r.order_ref,
                r.symbol,
                r.contract_id,
                r.security_type,
                r.side,
                str(r.quantity),
                str(r.price),
                str(r.multiplier),
                r.currency,
                r.account_fingerprint,
                r.correlation_id,
            )
            for r in session.scalars(sa.select(FillRow).order_by(FillRow.execution_id))
        ]
        commissions = [
            (r.execution_id, str(r.amount), r.currency)
            for r in session.scalars(sa.select(CommissionRow).order_by(CommissionRow.execution_id))
        ]
    return fills, commissions


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ (1) one row per execution


def test_1a_record_writes_one_fill_and_one_commission_and_a_replay_writes_nothing(
    database: Database, tmp_path: Path
) -> None:
    repository = ExecutionRepository(database.sessions)
    assert repository.record(_execution(), correlation_id=None) is True
    fills, commissions = _rows(database)
    assert fills == [
        (
            "0000e0d5.64f1a2b3.01.01",
            7001,
            9001,
            17,
            "CHR-BP1-1",
            "AAPL",
            2002,
            "OPT",
            "SELL",
            "1.00000000",
            "2.00000000",
            "100.00000000",
            "USD",
            account_fingerprint(ACCOUNT_ID),
            None,
        )
    ]
    assert commissions == [("0000e0d5.64f1a2b3.01.01", "0.65000000", "USD")]
    # a replay of the identical execution: False, no second row, the file byte-identical
    with database.sessions() as session:  # checkpoint so the digest is of one file
        session.execute(sa.text("PRAGMA wal_checkpoint(TRUNCATE)"))
    before = _sha(tmp_path / "chronos.db")
    assert repository.record(_execution(), correlation_id=None) is False
    assert _rows(database) == (fills, commissions)
    with database.sessions() as session:
        session.execute(sa.text("PRAGMA wal_checkpoint(TRUNCATE)"))
    assert _sha(tmp_path / "chronos.db") == before


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("quantity", {"quantity": "2"}),
        ("price", {"price": "2.05"}),
        ("commission", {"commission": "0.70"}),
    ],
)
def test_1b_the_same_execution_id_with_different_facts_is_a_typed_conflict_naming_the_field(
    database: Database, field: str, kwargs: dict[str, str]
) -> None:
    repository = ExecutionRepository(database.sessions)
    repository.record(_execution(), correlation_id=None)
    before = _rows(database)
    with pytest.raises(ExecutionConflict, match=rf"execution 0000e0d5\.64f1a2b3\.01\.01 .*{field}"):
        repository.record(_execution(**kwargs), correlation_id=None)
    assert _rows(database) == before  # nothing changed, nothing added


# ------------------------------------------------------------------ (2) migration 0013 + schema


def test_2_fills_carries_the_identity_columns_and_the_drift_checker_accepts_them(
    database: Database,
) -> None:
    inspector = sa.inspect(database.engine)
    columns = {c["name"]: c for c in inspector.get_columns("fills")}
    assert columns["permanent_id"]["nullable"] is True
    assert columns["client_id"]["nullable"] is False
    assert columns["order_ref"]["nullable"] is True
    assert any(
        index["column_names"] == ["permanent_id"] for index in inspector.get_indexes("fills")
    )
    assert SCHEMA_VERSION == 14
    database.initialize()  # the fail-closed drift checker on an initialized store: no drift
    migration = ROOT / "src/chronos/persistence/migrations/versions/0013_execution_identity.py"
    text = migration.read_text(encoding="utf-8")
    assert 'revision = "0013"' in text and 'down_revision = "0012"' in text
    for column in ("permanent_id", "client_id", "order_ref"):
        assert f'"{column}"' in text
    assert "version=14" in text


# ------------------------------------------------------------------ (3) account scope


def test_3_an_execution_outside_the_bound_scope_is_refused_and_no_raw_account_id_is_stored(
    database: Database,
) -> None:
    repository = ExecutionRepository(database.sessions)
    with pytest.raises(ValueError, match="does not match the bound pseudonymous database scope"):
        repository.record(_execution(account_id="DU7654321"), correlation_id=None)
    assert _rows(database) == ([], [])
    # a raw account id smuggled into a string field is rejected before the row exists
    with pytest.raises(ValueError, match="raw broker account IDs"):
        repository.record(
            _execution(execution_id="exec-2", order_ref=f"ref-{ACCOUNT_ID}"), correlation_id=None
        )
    assert _rows(database) == ([], [])
    # the stored fingerprint is the scope's, never the raw id
    repository.record(_execution(), correlation_id=None)
    fills, _ = _rows(database)
    assert fills[0][13] == account_fingerprint(ACCOUNT_ID) and ACCOUNT_ID not in str(fills)


# ------------------------------------------------------------------ (4) commission evidence


def test_4_commission_and_currency_travel_together_or_not_at_all(database: Database) -> None:
    # the model refuses half a commission before the repository is reached (models.py:458-460)
    with pytest.raises(
        ValueError, match="Commission amount and currency must be supplied together"
    ):
        _execution(commission="0.65", commission_currency=None)
    repository = ExecutionRepository(database.sessions)
    repository.record(
        _execution(execution_id="exec-no-commission", commission=None, commission_currency=None),
        correlation_id=None,
    )
    fills, commissions = _rows(database)
    assert [f[0] for f in fills] == ["exec-no-commission"] and commissions == []
    with database.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CommissionRow)) == 0
        # the table itself refuses a currency-less commission row
        session.add(
            CommissionRow(
                execution_id="exec-no-commission",
                amount=Decimal("1"),
                currency=None,
                received_at=NOW,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()


# ------------------------------------------------------------- (5) the engine call site — BLOCKED


def test_5_the_engine_fill_path_holds_a_broker_event_not_an_execution_so_nothing_wires_yet() -> (
    None
):
    """Item 5 is BLOCKED (stop rule): execution/engine.py's fill path receives a BrokerEvent with no
    execution identity; this pin records the fact so the re-draft starts from it."""

    engine = (ROOT / "src/chronos/execution/engine.py").read_text(encoding="utf-8")
    port = (ROOT / "src/chronos/execution/brokers/port.py").read_text(encoding="utf-8")
    assert "self.ledger.record_fill(" in engine and "BrokerExecution" not in engine
    assert (
        "execution_id"
        not in port.split("class BrokerEvent:")[1].split("class SubmissionReceipt")[0]
    )
    assert "execution_repository" not in engine


# ------------------------------------------------------------------ (6) the authority line


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_6_the_repository_imports_no_authority_module_and_none_imports_it() -> None:
    offending = [n for n in _imports(MODULE) if n.startswith(FORBIDDEN_PREFIXES)]
    assert offending == [], offending
    # the reverse direction: no order-plane / autonomy / supervisor / control module imports it
    importers = []
    for prefix in ("orders", "autonomy", "supervisor", "control", "execution"):
        for path in (ROOT / "src" / "chronos" / prefix).rglob("*.py"):
            if "execution_repository" in " ".join(_imports(path)):
                importers.append(path.relative_to(ROOT).as_posix())
    assert importers == [], importers
    # and at runtime: importing the module pulls none of the forbidden packages into sys.modules
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, chronos.persistence.execution_repository; "
            f"print(sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIXES!r})))",
        ],
        cwd=ROOT,
        env={**os.environ, **SAFE_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "[]", probe.stdout

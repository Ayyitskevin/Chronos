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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import OptionRight, OrderSide
from chronos.domain.models import BrokerExecution, OptionContract, UnderlyingContract
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import ExecutionConflict, ExecutionRepository
from chronos.persistence.schema import CommissionRow, FillRow
from chronos.utils.identifiers import account_fingerprint

ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "src/chronos/persistence/migrations"))
    return config


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
    assert columns["client_id"]["nullable"] is True  # r2 (ruling R-a): unknown stays unknown
    assert columns["order_ref"]["nullable"] is True
    assert any(
        index["column_names"] == ["permanent_id"] for index in inspector.get_indexes("fills")
    )
    # 0013's OWN artifact, exactly: the alembic chain holds revision 0013 revising 0012 (the
    # chain HEAD is pinned once, in test_database); its upgrade stamps version 14 (below)
    script = ScriptDirectory.from_config(_alembic_config())
    revision = script.get_revision("0013")
    assert revision is not None and revision.down_revision == "0012"
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


def _imports(path: Path, source: str | None = None) -> list[str]:
    """Every module name a file can reach by import — the shape of
    tests/safety/test_operational_health_boundary.py::_imports_operational_projection, plus
    the relative form: ``import a.b``, ``from a import b [as x]`` (→ ``a`` AND ``a.b``), and
    ``from . import b`` / ``from .b import c`` resolved against the file's own package."""

    tree = ast.parse(source if source is not None else path.read_text(encoding="utf-8"))
    package = ".".join(path.resolve().relative_to(ROOT / "src").with_suffix("").parts[:-1])
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . import x / from .m import y
                base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.append(module)
            names += [f"{module}.{alias.name}" for alias in node.names]
    return names


def _imports_the_repository(names: list[str]) -> bool:
    target = "chronos.persistence.execution_repository"
    return any(n == target or n.startswith(target + ".") for n in names)


def test_6_the_repository_imports_no_authority_module_and_none_imports_it() -> None:
    offending = [n for n in _imports(MODULE) if n.startswith(FORBIDDEN_PREFIXES)]
    assert offending == [], offending
    # the reverse direction: no order-plane / autonomy / supervisor / control module imports it
    importers = []
    for prefix in ("orders", "autonomy", "supervisor", "control", "execution"):
        for path in (ROOT / "src" / "chronos" / prefix).rglob("*.py"):
            if _imports_the_repository(_imports(path)):
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


# ------------------------------------------------------------- (r2) Daybreak's HOLD at b2b370a


def test_r2_1_a_legacy_row_reads_client_id_none_and_a_new_row_carries_the_executions_client_id(
    database: Database,
) -> None:
    """Ruling R-a: unknown stays unknown — the column is nullable, the ALTER has no default
    (the migration suite pins the v13 upgrade to (None, None, None)); every row the
    repository writes still carries the execution's own client id."""

    inspector = sa.inspect(database.engine)
    assert {c["name"]: c for c in inspector.get_columns("fills")}["client_id"]["nullable"] is True
    migration = ROOT / "src/chronos/persistence/migrations/versions/0013_execution_identity.py"
    assert "server_default" not in migration.read_text(encoding="utf-8")
    # a legacy row (written before 0013) reads back None, never a synthesized client 0
    with database.sessions.begin() as session:
        session.execute(
            sa.text(
                "INSERT INTO fills (execution_id, broker_order_id, symbol, contract_id, "
                "security_type, side, quantity, price, multiplier, currency, "
                "account_fingerprint, occurred_at) VALUES ('EXEC-LEGACY', 1, 'AAPL', 100, 'OPT', "
                "'SELL', 1, 2, 100, 'USD', :fp, '2026-01-15 15:30:00.000000')"
            ),
            {"fp": account_fingerprint(ACCOUNT_ID)},
        )
    ExecutionRepository(database.sessions).record(_execution(), correlation_id=None)
    fills, _ = _rows(database)
    by_id = {f[0]: f for f in fills}
    assert by_id["EXEC-LEGACY"][3] is None
    assert by_id["0000e0d5.64f1a2b3.01.01"][3] == 17


@pytest.mark.parametrize(
    "field",
    ["execution_id", "order_ref", "symbol", "currency", "commission_currency"],
)
def test_r2_2_every_persisted_broker_string_passes_the_raw_account_id_guard(
    database: Database, field: str
) -> None:
    """Daybreak's probe: contract.currency='DU1234567' was stored. Each broker-controlled
    string field is now refused by name before any write."""

    raw = ACCOUNT_ID  # a raw broker account id smuggled into the field
    if field == "currency":
        contract = _option().model_copy(update={"currency": raw})
        execution = _execution(contract=contract)
    elif field == "commission_currency":
        execution = _execution(commission="0.65", commission_currency=raw)
    elif field == "symbol":
        contract = _option().model_copy(update={"symbol": raw})
        execution = _execution(contract=contract)
    else:
        execution = _execution(**{field: raw})
    repository = ExecutionRepository(database.sessions)
    with pytest.raises(ValueError, match="raw broker account IDs"):
        repository.record(execution, correlation_id=None)
    assert _rows(database) == ([], [])


def test_r2_2b_the_callers_linkage_strings_pass_the_guard_too(database: Database) -> None:
    repository = ExecutionRepository(database.sessions)
    for kwargs in (
        {"correlation_id": f"corr-{ACCOUNT_ID}"},
        {"correlation_id": None, "wheel_cycle_id": f"cyc-{ACCOUNT_ID}"},
    ):
        with pytest.raises(ValueError, match="raw broker account IDs"):
            repository.record(_execution(), **kwargs)
    assert _rows(database) == ([], [])


def test_r2_3_the_conflict_identity_is_the_broker_facts_incl_timestamp_never_the_callers_linkage(
    database: Database,
) -> None:
    """Ruling R-b. The same execution_id one second apart is a conflict naming `timestamp`;
    the same execution observed again under another correlation/cycle is a replay (False),
    the first linkage kept — reconciliation observes the same execution many times."""

    repository = ExecutionRepository(database.sessions)
    assert repository.record(_execution(), correlation_id=None) is True
    before = _rows(database)
    later = _execution().model_copy(update={"timestamp": NOW + timedelta(seconds=1)})
    with pytest.raises(ExecutionConflict, match=r"a different timestamp"):
        repository.record(later, correlation_id=None)
    assert _rows(database) == before
    # the caller's linkage is not identity: a second observation under another correlation
    # returns False, writes nothing, keeps the first linkage (None here) — one row
    assert (
        repository.record(_execution(), correlation_id="CORR-OTHER", wheel_cycle_id=None) is False
    )
    fills, commissions = _rows(database)
    assert len(fills) == 1 and fills[0][14] is None and (fills, commissions) == before
    # the docstring states the identity set verbatim
    doc = (ROOT / "src/chronos/persistence/execution_repository.py").read_text(encoding="utf-8")
    assert (
        "execution_id + permanent_id + client_id + order_ref + contract identity (symbol, "
        "contract_id, security_type, currency) + side + quantity + price + multiplier + "
        "timestamp + commission amount + commission currency"
    ) in " ".join(doc.split())
    assert "correlation_id, wheel_cycle_id" in " ".join(doc.split())


def _barrier_sessions(
    database: Database, execution_id: str
) -> tuple[sessionmaker[Session], threading.Barrier]:
    """Daybreak's probe: two sessions both observe 'no row' before either inserts."""

    barrier = threading.Barrier(2)

    class BarrierSession(Session):
        def get(self, entity, ident, **kwargs):  # type: ignore[no-untyped-def,override]
            value = super().get(entity, ident, **kwargs)
            if entity is FillRow and ident == execution_id and value is None:
                barrier.wait(timeout=5)
            return value

    return sessionmaker(
        bind=database.engine, expire_on_commit=False, class_=BarrierSession
    ), barrier


def test_r2_4a_two_concurrent_identical_inserts_return_true_and_false_with_one_row(
    database: Database,
) -> None:
    sessions, _barrier = _barrier_sessions(database, "concurrent-exec")
    repository = ExecutionRepository(sessions)

    def write() -> str:
        try:
            execution = _execution(execution_id="concurrent-exec")
            return f"return:{repository.record(execution, correlation_id=None)}"
        except Exception as error:  # the externally visible class is the evidence
            return f"error:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(future.result() for future in [pool.submit(write) for _ in range(2)])
    assert outcomes == ["return:False", "return:True"], outcomes
    fills, commissions = _rows(database)
    assert [f[0] for f in fills] == ["concurrent-exec"] and len(commissions) == 1


def test_r2_4b_a_divergent_concurrent_loser_raises_the_typed_conflict(database: Database) -> None:
    sessions, _barrier = _barrier_sessions(database, "concurrent-exec")
    repository = ExecutionRepository(sessions)

    def write(price: str) -> str:
        try:
            execution = _execution(execution_id="concurrent-exec", price=price)
            return f"return:{repository.record(execution, correlation_id=None)}"
        except ExecutionConflict as error:
            return f"conflict:{'price' in str(error)}"
        except Exception as error:
            return f"error:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            f.result() for f in [pool.submit(write, "2.00"), pool.submit(write, "2.05")]
        )
    assert outcomes == ["conflict:True", "return:True"], outcomes
    fills, _ = _rows(database)
    assert len(fills) == 1  # the winner's row, untouched by the loser


def test_r2_5_the_reverse_scan_resolves_every_import_spelling() -> None:
    sample = Path(
        ROOT / "src/chronos/orders/_probe.py"
    )  # never written: a path for package resolution
    spellings = [
        "import chronos.persistence.execution_repository\n",
        "from chronos.persistence import execution_repository\n",
        "from chronos.persistence import execution_repository as repo\n",
        "from chronos.persistence.execution_repository import ExecutionRepository\n",
    ]
    for source in spellings:
        assert _imports_the_repository(_imports(sample, source)), source
    relative = Path(ROOT / "src/chronos/persistence/_probe.py")
    for source in (
        "from . import execution_repository\n",
        "from .execution_repository import ExecutionRepository\n",
    ):
        assert _imports_the_repository(_imports(relative, source)), source
    # and an unrelated import is not a hit
    assert not _imports_the_repository(_imports(sample, "from chronos.persistence import schema\n"))


# ------------------------------------------------------------------ (BP-1b) the typed refusal


def test_bp1b_4a_an_unknown_correlation_id_is_a_typed_refusal_before_any_insert(
    database: Database,
) -> None:
    """Muse item 2 for BP-1b: a non-draft ``correlation_id`` is refused by the repository itself,
    typed, with zero rows and no ``IntegrityError`` anywhere in the exception's chain."""

    from sqlalchemy.exc import IntegrityError

    from chronos.persistence.execution_repository import UnknownCorrelation

    repository = ExecutionRepository(database.sessions)
    with pytest.raises(UnknownCorrelation) as info:
        repository.record(_execution(), correlation_id="not-a-draft")
    error: BaseException | None = info.value
    chain: list[type[BaseException]] = []
    while error is not None:
        chain.append(type(error))
        error = error.__cause__ or error.__context__
    assert IntegrityError not in chain, chain
    assert (info.value.execution_id, info.value.correlation_id) == (
        "0000e0d5.64f1a2b3.01.01",
        "not-a-draft",
    )
    assert isinstance(info.value, ValueError) and "not-a-draft" in str(info.value)
    assert _rows(database) == ([], [])
    # the session rolled back cleanly: the same execution records normally afterwards
    assert repository.record(_execution(), correlation_id=None) is True
    assert len(_rows(database)[0]) == 1

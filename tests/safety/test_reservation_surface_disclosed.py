"""VCP section 7 (O5), reservation half: the reservation surface classified and pinned.

``chronos.persistence.reservation_enforcement`` is the map; these are its
pins. The sink search is by ORM class name AND by table name over every
module under ``src/chronos`` — not one caller — so a new reader or writer of
``cash_reservations`` / ``share_reservations`` cannot arrive through a module
the map never looked at.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from chronos.persistence import reservation_enforcement
from chronos.persistence.reservation_enforcement import (
    ENFORCED,
    INERT,
    RESERVATION_ENFORCEMENT,
)
from chronos.strategy import reservations

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "chronos"

#: Names that would mean someone acts on the reservation tables. The sink
#: search matches BOTH spellings: the ORM class and the literal table name.
_TABLE_NAMES: tuple[str, ...] = (
    "CashReservationRow",
    "ShareReservationRow",
    "cash_reservations",
    "share_reservations",
)

#: Files allowed to mention the names: the ORM definition, the migration that
#: creates the tables, the required-table list, and the map module itself.
#: Anything else is a new reader or writer and fails until the map reclassifies.
_ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "src/chronos/persistence/schema.py",
        "src/chronos/persistence/migrations/versions/0002_live_wheel_tables.py",
        "src/chronos/persistence/database.py",
        "src/chronos/persistence/reservation_enforcement.py",
    }
)


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_t0_every_surface_is_classified_with_evidence() -> None:
    """The map is well-formed: known classes only, every entry carries evidence."""

    assert set(RESERVATION_ENFORCEMENT) == {
        "in_memory_computation",
        "cash_reservations_table",
        "share_reservations_table",
        "position_netting",
    }
    for surface, entry in RESERVATION_ENFORCEMENT.items():
        assert entry["classification"] in {
            ENFORCED,
            INERT,
            reservation_enforcement.OUT_OF_SCOPE_OWNER_GATE,
        }, surface
        assert entry["evidence"].strip(), surface


def test_t1_no_module_under_src_acts_on_the_reservation_tables() -> None:
    """The INERT half stays INERT: a new reader/writer fails until reclassified."""

    offenders: list[str] = []
    for path in _src_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for name in _TABLE_NAMES:
            if name in text:
                offenders.append(f"{rel}: {name}")
    assert not offenders, (
        "the reservation tables gained a reader or writer; reclassify "
        f"RESERVATION_ENFORCEMENT instead: {offenders}"
    )


def test_t2_the_in_memory_reservation_computation_is_enforced() -> None:
    """The ENFORCED half is real: the order risk path calls both reservers.

    Read by attribute access in the risk module's AST, not by substring, so a
    comment or a same-named string key cannot count as the call.
    """

    from chronos.orders import risk as risk_module

    tree = ast.parse(inspect.getsource(risk_module))
    called = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "reservations"
    }
    assert "reserve_cash" in called, "orders/risk.py no longer calls reservations.reserve_cash"
    assert "reserve_shares" in called, "orders/risk.py no longer calls reservations.reserve_shares"
    assert hasattr(reservations, "reserve_cash")
    assert hasattr(reservations, "reserve_shares")


def test_t3_the_map_module_is_imported_by_nothing_under_src() -> None:
    """CLASSIFICATION TRUTH ONLY: the map enforces nothing and wires nowhere."""

    importers: list[str] = []
    for path in _src_files():
        if path.name == "reservation_enforcement.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "reservation_enforcement" in text:
            importers.append(path.relative_to(REPO_ROOT).as_posix())
    assert not importers, f"the map module must stay data-only, imported by nothing: {importers}"

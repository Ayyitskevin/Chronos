"""Which parts of the reservation surface anything acts on — as data.

:mod:`chronos.autonomy.enforcement` answers this for the owner mandate;
:mod:`chronos.strategies.param_enforcement` for the research lane. This module
answers it for the reservation surface named by VCP section 7 (atomic
reservations, position netting, conflict resolution): what exists today, and
which half of it anything acts on.

**This module enforces nothing.** It records, per surface, whether a
deterministic module on the path that admits, risks, persists or transmits
acts on it today.

``ENFORCED``
    The in-memory reservation computation. The order risk path calls
    ``reservations.reserve_cash`` / ``reservations.reserve_shares``
    (``src/chronos/orders/risk.py:447,483`` at the head) with the input models
    from :mod:`chronos.strategy.reservations` (``reservations.py:36,79``),
    which return in-memory summaries. The result changes what the risk path
    refuses or admits. Nothing is persisted: no reservation row is ever
    written.
``INERT``
    The ``cash_reservations`` / ``share_reservations`` tables. Migration
    ``0002_live_wheel_tables.py:37,38`` creates them;
    ``persistence/database.py:55,56`` lists them among the required tables;
    the ORM classes are ``schema.py:461,474``. No module writes a row and no
    module reads one — a value there changes no admission, size or order
    today. (An economic-looking table nothing acts on is itself the finding-7
    shape this map exists to keep disclosed.)

Out of scope, owner-gated, and deliberately NOT classified here: persisting
reservations, position netting, and cross-proposal conflict resolution. Those
change what is reserved or admitted and are a separate, reviewed packet.

``tests/safety/test_reservation_surface_disclosed.py`` pins this map both
directions: a new reader or writer of either table (by ORM class name or by
table name) anywhere under ``src/`` fails unless this map is reclassified, and
the risk path must keep calling both reservation functions.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "ENFORCED",
    "INERT",
    "OUT_OF_SCOPE_OWNER_GATE",
    "RESERVATION_ENFORCEMENT",
]

ENFORCED = "ENFORCED"
INERT = "INERT"
OUT_OF_SCOPE_OWNER_GATE = "OUT_OF_SCOPE_OWNER_GATE"

RESERVATION_ENFORCEMENT: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "in_memory_computation": MappingProxyType(
            {
                "classification": ENFORCED,
                "surface": "chronos.strategy.reservations reserve_cash / reserve_shares",
                "evidence": "src/chronos/orders/risk.py:447,483 (call sites); "
                "src/chronos/strategy/reservations.py:36,79 (in-memory summaries)",
                "note": "changes what the order risk path refuses or admits; nothing persisted",
            }
        ),
        "cash_reservations_table": MappingProxyType(
            {
                "classification": INERT,
                "surface": "CashReservationRow / cash_reservations",
                "evidence": "src/chronos/persistence/schema.py:461; "
                "src/chronos/persistence/migrations/versions/0002_live_wheel_tables.py:37; "
                "src/chronos/persistence/database.py:55",
                "note": "created by migration 0002 and listed as a required table; "
                "no writer, no reader",
            }
        ),
        "share_reservations_table": MappingProxyType(
            {
                "classification": INERT,
                "surface": "ShareReservationRow / share_reservations",
                "evidence": "src/chronos/persistence/schema.py:474; "
                "src/chronos/persistence/migrations/versions/0002_live_wheel_tables.py:38; "
                "src/chronos/persistence/database.py:56",
                "note": "created by migration 0002 and listed as a required table; "
                "no writer, no reader",
            }
        ),
        "position_netting": MappingProxyType(
            {
                "classification": OUT_OF_SCOPE_OWNER_GATE,
                "surface": "(no authoritative netting module)",
                "evidence": "no netting on the admission/risk path at the head; a display-only, "
                "read-only per-symbol net-position projection exists for the monitor "
                "(src/chronos/monitoring/ledger_view.py:150) and is not an admission input; "
                "VCP section 7 O5",
                "note": "not classified: persisting reservations, netting and "
                "cross-proposal conflict resolution are OWNER GATE, a separate packet",
            }
        ),
    }
)

"""Operator-owned autonomy startup inhibit, read once by the backend lifespan.

Only an explicit ``off`` lifts it. It is deliberately independent of Settings
validation: an unreadable/invalid switch must inhibit autonomy, not crash the
backend that the operator still needs for reconciliation and inspection.
"""

from os import environ


def autonomy_maintenance_enabled() -> bool:
    """Fail closed; never expose a malformed configuration value in a log."""

    try:
        return environ.get("CHRONOS_AUTONOMY_MAINTENANCE", "on").strip().lower() != "off"
    except Exception:
        return True

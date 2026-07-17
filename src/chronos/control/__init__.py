"""Control plane: operating modes, promotion gates, halt state, and rearm.

Nothing in this package talks to a broker or produces an order. It exists to
decide whether the execution plane is *allowed* to act, and to fail closed.
"""

from chronos.control.halt import HaltReason, HaltState, HaltStore
from chronos.control.modes import ExecutionCapability, ModeLock, TradingMode, resolve_mode_lock

__all__ = [
    "ExecutionCapability",
    "HaltReason",
    "HaltState",
    "HaltStore",
    "ModeLock",
    "TradingMode",
    "resolve_mode_lock",
]

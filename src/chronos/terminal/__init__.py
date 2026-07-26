"""The operator terminal: a window onto Chronos, and a set of owner buttons.

ADR-0018 / DECISIONS.md D-18. The terminal is served by the existing FastAPI
backend, with no second runtime and no second language toolchain. Its command
registry and every panel read-model are **Python**, served to the browser as
JSON, so the panel surface cannot drift from what the backend can actually
answer: the backend is what declares it.

This package deliberately re-exports nothing. A panel model, a command, and a
route have different reasons to change, and a package-level surface that
gathered them would make the terminal look like a subsystem with its own
authority. It is not one — ADR-0018 §4: *a window and a set of owner buttons,
never a gateway.*
"""

from __future__ import annotations

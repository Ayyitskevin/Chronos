"""Process-lifetime application resources reused across Streamlit reruns.

The wiring itself lives in :mod:`chronos.runtime` (shared with the FastAPI
backend since Milestone 1 of the live Wheel plan); this module only adds the
Streamlit resource cache so reruns reuse one runtime per UI process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import streamlit as st

from chronos.runtime import (
    AppRuntime,
    _validate_scope_observations,  # noqa: F401 — re-exported for existing tests
    build_runtime,
)

__all__ = ["AppRuntime", "get_runtime"]


get_runtime = cast(
    Callable[[], AppRuntime],
    st.cache_resource(show_spinner="Starting the local Chronos runtime…")(build_runtime),
)

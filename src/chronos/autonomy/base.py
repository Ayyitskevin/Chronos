"""Base model for autonomy contracts whose invariants survive copying.

Pydantic's ``model_copy(update=...)`` does **not** re-run validators. For an
ordinary value object that is a convenience; for an authorization record it is
an escalation vector. Against the M1 contracts it was one:

    m = AutonomyMandate(mode=SHADOW, promotion_level=SHADOW, ...)   # 1-day SHADOW
    m.model_copy(update={"mode": LIVE_AUTONOMOUS,
                         "expires_at": now + timedelta(days=3650)})

produced a ten-year ``LIVE_AUTONOMOUS`` mandate with an empty scope and a
SHADOW promotion rung — every mandate validator skipped, from an object the
holder already had. Found by the M1 adversarial review; see ADR-0016
"Known limitations and residuals" item 0.

:class:`AutonomyModel` closes it by re-validating the merged data whenever an
update is supplied, so a copy is held to exactly the invariants a constructor
call would be. Copying without ``update`` is unchanged (it cannot alter
anything). Every model in this package inherits from it, so the guarantee holds
for nested contracts too, not just the two top-level ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from chronos.domain.models import ChronosModel


class AutonomyModel(ChronosModel):
    """Immutable contract model whose validators also run on ``model_copy``."""

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        copied = super().model_copy(update=update, deep=deep)
        if not update:
            return copied
        # Round-trip through the validator so an updated copy is held to the
        # same invariants as a constructed instance. `model_dump()` in python
        # mode preserves Decimal/datetime/enum types, so nothing is coerced.
        return type(self).model_validate(copied.model_dump())

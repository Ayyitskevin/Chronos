from __future__ import annotations

import pytest

from chronos.autonomy.authority import (
    EXTERNAL_WORKER_STATUS,
    LIVE_AUTHORITY,
    PAPER_AUTHORITY,
    AuthorityError,
    require_external_worker,
    require_live_authority,
    require_paper_authority,
)
from chronos.autonomy.worker_protocol import REFERENCE_WORKER_PINS, WorkerIdentityPins


def test_external_worker_and_paper_live_stay_refused() -> None:
    assert EXTERNAL_WORKER_STATUS == "pending_owner_pins"
    assert PAPER_AUTHORITY == "none"
    assert LIVE_AUTHORITY == "none"
    with pytest.raises(AuthorityError, match="pending_owner_pins"):
        require_external_worker()
    with pytest.raises(AuthorityError, match="does not invoke"):
        require_external_worker(
            WorkerIdentityPins(
                provider="owner-lab",
                model_id="unpinned",
                model_version="1",
                prompt_version="1",
                tool_schema_version="1",
                decision_schema_version="1",
                policy_version="gld-tail-rvol-v1",
            )
        )
    with pytest.raises(AuthorityError, match="pending_owner_pins"):
        require_external_worker(REFERENCE_WORKER_PINS)
    with pytest.raises(AuthorityError, match="paper authority is none"):
        require_paper_authority()
    with pytest.raises(AuthorityError, match="live authority is none"):
        require_live_authority()

"""Strategy read endpoints: reconciliation view and candidate evaluation.

Both are read-only operations (candidate evaluation opens bounded market-data
windows but writes no orders). Order-writing endpoints arrive in Milestone 5
behind the writer lease AND the full gate stack — nothing here can submit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state
from chronos.services.reconciliation import ReconciliationResult
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]


@router.get("/strategy/reconciliation", response_model=ReconciliationResult)
def reconciliation(state: StateDep) -> ReconciliationResult:
    return state.runtime.reconciliation.reconcile()


@router.post("/strategy/candidates/{symbol}", response_model=ShortPutCandidateEvaluation)
def evaluate_candidates(symbol: str, state: StateDep) -> ShortPutCandidateEvaluation:
    normalized = symbol.strip().upper()
    if normalized not in state.runtime.settings.symbol_allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{normalized!r} is not on the symbol allowlist",
        )
    return state.runtime.short_put_candidates.evaluate(normalized)

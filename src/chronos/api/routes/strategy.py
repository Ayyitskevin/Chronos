"""Strategy endpoints: reconciliation, candidates, regime context, and rehearsals.

Everything here is read-only or a DEMO rehearsal (candidate evaluation opens
bounded market-data windows; the what-if/approval services rehearse against
the demo boundary and write no orders). Order-writing endpoints arrive in
Milestone 5 behind the writer lease AND the full gate stack — nothing here
can submit. The regime context is a labeled heuristic loaded from local
research CSVs; it is decision support, never an order gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state
from chronos.services.reconciliation import ReconciliationResult
from chronos.services.regime_context import RegimeContext, compute_regime_context
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.services.short_put_demo_approval import (
    ShortPutDemoApprovalRequest,
    ShortPutDemoApprovalResult,
)
from chronos.services.short_put_demo_what_if import (
    ShortPutDemoWhatIfRequest,
    ShortPutDemoWhatIfResult,
)
from chronos.services.short_put_risk_preview import (
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewResult,
)

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]

_REGIME_DATA_DIR = Path("research/data/raw")


def _require_allowlisted(symbol: str, state: BackendState) -> str:
    normalized = symbol.strip().upper()
    if normalized not in state.runtime.settings.symbol_allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{normalized!r} is not on the symbol allowlist",
        )
    return normalized


@router.get("/strategy/reconciliation", response_model=ReconciliationResult)
def reconciliation(state: StateDep) -> ReconciliationResult:
    return state.runtime.reconciliation.reconcile()


@router.post("/strategy/candidates/{symbol}", response_model=ShortPutCandidateEvaluation)
def evaluate_candidates(symbol: str, state: StateDep) -> ShortPutCandidateEvaluation:
    normalized = _require_allowlisted(symbol, state)
    return state.runtime.short_put_candidates.evaluate(normalized)


@router.get("/strategy/regime/{symbol}", response_model=RegimeContext)
def regime_context(symbol: str, state: StateDep) -> RegimeContext:
    """Labeled heuristic context from local closed daily bars; 200 even when unavailable."""

    normalized = _require_allowlisted(symbol, state)
    return compute_regime_context(normalized, _REGIME_DATA_DIR)


@router.post("/strategy/risk-preview", response_model=ShortPutRiskPreviewResult)
def risk_preview(request: ShortPutRiskPreviewRequest, state: StateDep) -> ShortPutRiskPreviewResult:
    _require_allowlisted(request.symbol, state)
    return state.runtime.short_put_risk_preview.preview(request)


@router.post("/strategy/demo-what-if", response_model=ShortPutDemoWhatIfResult)
def demo_what_if(request: ShortPutDemoWhatIfRequest, state: StateDep) -> ShortPutDemoWhatIfResult:
    _require_allowlisted(request.symbol, state)
    return state.runtime.short_put_demo_what_if.preview(request)


@router.post("/strategy/demo-approval", response_model=ShortPutDemoApprovalResult)
def demo_approval(
    request: ShortPutDemoApprovalRequest, state: StateDep
) -> ShortPutDemoApprovalResult:
    _require_allowlisted(request.symbol, state)
    return state.runtime.short_put_demo_approval.rehearse(request)

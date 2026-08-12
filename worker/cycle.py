"""One gather → think → propose cycle, and the loop that repeats it (ADR-0027).

Every cycle ends in exactly one of the outcomes below, and all but one of them
is a refusal of some kind. That is the intended distribution: this worker
feeds a system whose default answer is no, and its own posture matches —
evidence unavailable means no thinking, no decision means no proposal, an
incoherent decision means a logged local refusal, and forwarding stays off
until the owner turns it on.

The loop is time-driven and single-threaded, like the supervisor's tick: one
cycle at a time, a fixed interval between starts, and no event can make it
think faster. A worker that thought as fast as something could poke it would
be an unbounded decision source in front of a bounded queue.
"""

from __future__ import annotations

import enum
import json
import logging
import time
from typing import Any

import httpx

from worker.config import WorkerConfig
from worker.evidence import TOKEN_HEADER, gather
from worker.model import think
from worker.propose import ProposalRefused, build_proposal

_logger = logging.getLogger("chronos.worker.cycle")

#: The backend enqueues and returns; a slow answer means something is wrong.
FORWARD_TIMEOUT_SECONDS = 15.0

#: The proposer-credential header (ADR-0023), sent on the proposal POST when
#: the worker is registered. Must match the backend's ``chronos.api.auth``.
PROPOSER_HEADER = "X-Chronos-Proposer-Token"


class CycleOutcome(enum.Enum):
    """What one cycle did. Every member but FORWARDED is a kind of refusal."""

    NO_EVIDENCE = "NO_EVIDENCE"
    NO_DECISION = "NO_DECISION"
    REFUSED_LOCALLY = "REFUSED_LOCALLY"
    DRY_RUN = "DRY_RUN"
    INGRESS_REFUSED = "INGRESS_REFUSED"
    FORWARDED = "FORWARDED"


def run_cycle(
    config: WorkerConfig,
    *,
    backend: httpx.Client,
    anthropic: httpx.Client,
) -> CycleOutcome:
    """One full cycle. Clients are caller-supplied so tests inject transports."""

    snapshot = gather(config, backend)
    if snapshot is None:
        return CycleOutcome.NO_EVIDENCE

    decision = think(config, snapshot, anthropic)
    if decision is None:
        return CycleOutcome.NO_DECISION

    try:
        proposal = build_proposal(decision, snapshot, config)
    except ProposalRefused as refusal:
        _logger.warning(
            "The model's %s decision was refused locally: %s",
            decision.get("kind", "?"),
            refusal,
        )
        return CycleOutcome.REFUSED_LOCALLY

    payload = json.dumps(proposal, separators=(",", ":"))
    if not config.forward:
        _logger.info(
            "DRY RUN — the worker would have proposed (set CHRONOS_WORKER_FORWARD=true "
            "to send):\n%s",
            json.dumps(proposal, indent=2),
        )
        return CycleOutcome.DRY_RUN

    return _forward(config, backend, payload, kind=str(proposal.get("kind")))


def _forward(
    config: WorkerConfig, backend: httpx.Client, payload: str, *, kind: str
) -> CycleOutcome:
    # Both credentials ride along when both exist: the local token satisfies a
    # pre-registry backend, and a registry-on backend judges the proposer
    # credential alone (ADR-0023) — so the worker works under either posture
    # without knowing which one the backend is in.
    headers = {TOKEN_HEADER: config.api_token, "Content-Type": "application/json"}
    if config.proposer_token:
        headers[PROPOSER_HEADER] = config.proposer_token
    try:
        response = backend.post(
            f"{config.backend_url}/autonomy/proposals",
            content=payload.encode("utf-8"),
            headers=headers,
            timeout=FORWARD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        _logger.error("The proposal ingress is unreachable: %s", type(error).__name__)
        return CycleOutcome.INGRESS_REFUSED

    answer: dict[str, Any]
    try:
        parsed = response.json()
        answer = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        answer = {}
    if response.status_code == 202 and answer.get("accepted"):
        _logger.info(
            "%s proposal queued (stage=%s). Queued is received, not authorized — the "
            "supervisor judges it on its own tick and may refuse it.",
            kind,
            answer.get("stage"),
        )
        return CycleOutcome.FORWARDED
    _logger.warning(
        "The ingress did not accept the %s proposal: HTTP %d stage=%s refusal=%s detail=%s",
        kind,
        response.status_code,
        answer.get("stage"),
        answer.get("refusal"),
        answer.get("detail"),
    )
    return CycleOutcome.INGRESS_REFUSED


def run_loop(config: WorkerConfig, *, once: bool = False) -> None:
    """The production loop: fixed cadence, one cycle at a time, forever.

    A failing cycle logs and keeps cadence — the worker never dies because one
    read or one model call failed, and it never speeds up to compensate.
    """

    with httpx.Client() as backend, httpx.Client() as anthropic:
        while True:
            started = time.monotonic()
            try:
                outcome = run_cycle(config, backend=backend, anthropic=anthropic)
                _logger.info("Cycle outcome: %s", outcome.value)
            except Exception:
                _logger.exception("Cycle failed unexpectedly; keeping cadence")
            if once:
                return
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, config.interval_seconds - elapsed))

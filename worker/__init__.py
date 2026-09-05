"""The Chronos model worker: the external AI brain that proposes, and only proposes.

ADR-0027. ADR-0016 §3 inverted the usual shape of an AI trading system: Chronos
never calls a model — a model worker, running in its own process, calls IN
through ``POST /autonomy/proposals``, and the supervisor's hostile-input ingress
decides whether what arrived is a proposal. For a year of design that worker was
hypothetical ("running wherever its owner runs it"). This package is that
worker, made real.

## Where it lives, and why exactly there

This directory sits at the repository root, **outside** ``src/chronos``, and is
not part of the ``chronos`` package or its wheel. That placement is load-bearing
three times over:

1. **The broker-holding process keeps its invariant.** ``docs/safety.md``
   promises "no model, no provider SDK, and no API key in the broker-holding
   process." The worker holds the Anthropic API key and talks to a model — so it
   is a separate process, a separate package, and never importable from
   ``chronos``. ``tests/safety/test_model_worker_isolation.py`` pins that
   nothing under ``src/chronos`` imports this package.
2. **No LLM SDK enters the repo's dependency tree.** The worker calls the
   Messages API with raw ``httpx`` — already a chronos dependency — so the
   standing re-verification ``grep anthropic|openai|litellm|langchain
   pyproject.toml requirements.txt`` stays empty, and the isolation suite now
   enforces it structurally instead of by habit.
3. **The worker imports NOTHING from chronos.** Stronger even than the
   TradingView bridge (which borrows ``chronos.utils.time``). This process
   holds a credential, consumes untrusted model output, and renders evidence
   into a prompt — it is exactly where a prompt injection would land, so it is
   built the way ADR-0016 describes the worker: no broker handle, no database
   session, no lease, no kill switch, no submission path, "not because it
   promises not to use them, but because they were never in its address space."

## What one cycle does

1. **Gather** — read the backend's own token-protected read endpoints (account
   summary, positions, open orders, recent daily bars for the watchlist) and
   freeze them into one canonical-JSON snapshot with a SHA-256 digest.
2. **Think** — send the snapshot to the configured provider (Claude by default,
   Grok via ``CHRONOS_WORKER_PROVIDER=xai``, or a loopback OpenAI-compatible
   server via ``CHRONOS_WORKER_PROVIDER=local``) with the owner's editable
   trading policy (``worker/policy.md``) as the system prompt, forcing a single
   ``propose_decision`` tool call so the answer arrives as structure, never
   prose to parse.
3. **Propose** — treat the model's output as hostile: validate it against the
   restated decision vocabulary, refuse incoherent payloads locally with a
   readable reason, attach an evidence citation whose digest is over the exact
   bytes the model saw (the model cannot fabricate provenance — the worker
   stamps it), and POST the candidate to the loopback ingress.

Everything downstream is unchanged: the same ingress, the same fifteen
admission checks, the same sizing, compilation, and full order-pipeline
handoff. A worker proposal can be refused by all of them and widens none.
The mandate remains the only grant of authority, and the worker cannot read,
name, write, or activate one.

## Postures, fail-closed

``CHRONOS_WORKER_FORWARD`` defaults to **false**: the shipped posture gathers,
thinks, logs the decision it *would* have proposed, and sends nothing. The
symbol and kind allowlists are required and empty means nothing is proposable.
A non-loopback backend URL refuses to boot — the worker carries the backend's
API token and may only ever hand it to a backend on this machine. A HOLD, a
local refusal, and an ingress refusal are all normal outcomes; in this system
a correct NO_TRADE is success.
"""

from __future__ import annotations

from worker.config import WorkerConfig, WorkerConfigError, load_config
from worker.cycle import CycleOutcome, run_cycle
from worker.evidence import EvidenceSnapshot
from worker.propose import ProposalRefused, build_proposal

__all__ = [
    "CycleOutcome",
    "EvidenceSnapshot",
    "ProposalRefused",
    "WorkerConfig",
    "WorkerConfigError",
    "build_proposal",
    "load_config",
    "run_cycle",
]

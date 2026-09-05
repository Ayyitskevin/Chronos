"""The worker's output is a proposal the real ingress accepts — proven, not assumed.

Same proof pattern as the TradingView bridge: because ``worker`` deliberately
imports nothing from ``chronos``, no import graph forces its output to agree
with the decision contract. This file closes that gap by running the worker's
own assembly and pushing the bytes through the real
:func:`chronos.supervisor.ingress.parse_proposal` — the same function the
backend route calls on the same bytes — per decision kind.

The refusal half is exercised in the house style: each refusal fires on an
input that should trigger it, and the model's output is treated as what it is —
untrusted text from a language model that may hallucinate, embed control
characters, or try to author its own provenance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from worker.config import WorkerConfig
from worker.evidence import EvidenceSnapshot
from worker.propose import ProposalRefused, build_proposal

from chronos.autonomy import ProposedDecision
from chronos.supervisor import ingress

REFERENCE = "CHR-TEST-0123456789ABCDEF0123456789ABCDEF"

CONFIG = WorkerConfig(
    provider="anthropic",
    anthropic_api_key="sk-test-key-never-logged",
    xai_api_key="",
    local_api_key="",
    model="claude-opus-5",
    api_token="token",
    proposer_token="",
    backend_url="http://127.0.0.1:8000",
    local_base_url="http://127.0.0.1:11434/v1",
    symbols=("SPY", "IWM"),
    kinds=frozenset({"OPEN", "CLOSE", "REDUCE", "HOLD", "CANCEL"}),
    policy="Test policy.",
    interval_seconds=300,
    lookback_days=30,
    forward=False,
    max_daily_tokens=None,
)

_CANONICAL = json.dumps({"account": {"cash": "100"}}, sort_keys=True, separators=(",", ":"))
SNAPSHOT = EvidenceSnapshot(
    canonical=_CANONICAL,
    digest=hashlib.sha256(_CANONICAL.encode("utf-8")).hexdigest(),
    as_of="2026-08-12T14:30:00+00:00",
)


def _decision(**overrides: object) -> dict[str, Any]:
    """A schema-shaped tool output, as the strict tool would deliver it."""

    base: dict[str, Any] = {
        "kind": "OPEN",
        "symbol": "SPY",
        "direction": "LONG",
        "thesis": "20-day breakout with expanding range in the snapshot bars.",
        "rationale": None,
        "quantity": "10",
        "strategy": "LONG_EQUITY",
        "time_horizon": "SWING",
        "target_reference": None,
        "confidence": "0.6",
        "invalidation": ["close below the 20-day moving average"],
    }
    base.update(overrides)
    return base


def _through_the_real_ingress(proposal: dict[str, Any]) -> ingress.IngressOutcome:
    return ingress.parse_proposal(json.dumps(proposal).encode("utf-8"))


# ------------------------------------------------- the worker produces a real proposal


def test_a_worker_decision_is_accepted_by_the_real_ingress() -> None:
    outcome = _through_the_real_ingress(build_proposal(_decision(), SNAPSHOT, CONFIG))

    assert outcome.accepted, f"the real ingress refused the worker's output: {outcome.refusal}"
    assert outcome.proposal is not None
    assert outcome.proposal.kind.value == "OPEN"
    assert outcome.proposal.symbol == "SPY"
    assert outcome.proposal.evidence[0].kind == "worker_evidence_snapshot"


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        ("OPEN", {}),
        (
            "CLOSE",
            {
                "strategy": None,
                "quantity": "5",
                "target_reference": REFERENCE,
                "invalidation": [],
            },
        ),
        (
            "REDUCE",
            {
                "strategy": None,
                "quantity": "5",
                "target_reference": REFERENCE,
                "invalidation": [],
            },
        ),
        (
            "HOLD",
            {
                "strategy": None,
                "quantity": None,
                "direction": "NEUTRAL",
                "invalidation": [],
            },
        ),
        (
            "CANCEL",
            {
                "strategy": None,
                "quantity": None,
                "target_reference": REFERENCE,
                "invalidation": [],
            },
        ),
    ],
)
def test_every_allowlisted_kind_survives_the_real_contract(
    kind: str, overrides: dict[str, Any]
) -> None:
    outcome = _through_the_real_ingress(
        build_proposal(_decision(kind=kind, **overrides), SNAPSHOT, CONFIG)
    )

    assert outcome.accepted, f"{kind} was refused by the real ingress: {outcome.refusal}"
    assert outcome.proposal is not None
    assert outcome.proposal.kind.value == kind


def test_the_worker_emits_only_fields_the_contract_declares() -> None:
    emitted = set(build_proposal(_decision(), SNAPSHOT, CONFIG))
    assert emitted <= set(ProposedDecision.model_fields), (
        f"the worker emits keys the decision contract does not declare: "
        f"{sorted(emitted - set(ProposedDecision.model_fields))}"
    )


def test_the_worker_never_emits_the_writer_owned_fields() -> None:
    proposal = build_proposal(_decision(), SNAPSHOT, CONFIG)
    assert "provenance" not in proposal
    assert "decision_id" not in proposal


def test_the_citation_digest_is_over_what_the_model_actually_saw() -> None:
    """Provenance is stamped by worker code, never authored by the model."""

    proposal = build_proposal(_decision(), SNAPSHOT, CONFIG)
    citation = proposal["evidence"][0]
    assert citation["digest"] == hashlib.sha256(SNAPSHOT.canonical.encode("utf-8")).hexdigest()
    assert citation["evidence_id"] == f"worker-snapshot:{SNAPSHOT.as_of}"


def test_the_api_key_and_token_never_reach_the_proposal() -> None:
    text = json.dumps(build_proposal(_decision(), SNAPSHOT, CONFIG))
    assert CONFIG.anthropic_api_key not in text
    assert CONFIG.api_token not in text


# --------------------------------------------------------------- the refusals do fire


def test_a_hallucinated_kind_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="not a decision kind"):
        build_proposal(_decision(kind="LIQUIDATE_EVERYTHING"), SNAPSHOT, CONFIG)


def test_a_kind_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="allowlist"):
        build_proposal(_decision(kind="HEDGE"), SNAPSHOT, CONFIG)


def test_a_symbol_off_the_watchlist_is_refused() -> None:
    """The model may only propose on symbols it was shown evidence for."""

    with pytest.raises(ProposalRefused, match="watchlist"):
        build_proposal(_decision(symbol="TSLA"), SNAPSHOT, CONFIG)


def test_an_open_without_invalidation_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="invalidation"):
        build_proposal(_decision(invalidation=[]), SNAPSHOT, CONFIG)


def test_a_targeted_kind_without_a_reference_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="Chronos reference"):
        build_proposal(_decision(kind="CLOSE", strategy=None, invalidation=[]), SNAPSHOT, CONFIG)


def test_a_broker_order_id_as_the_target_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="broker order id"):
        build_proposal(
            _decision(kind="CLOSE", strategy=None, invalidation=[], target_reference="0012345"),
            SNAPSHOT,
            CONFIG,
        )


def test_a_hold_with_a_direction_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="direction"):
        build_proposal(
            _decision(kind="HOLD", quantity=None, strategy=None, invalidation=[]),
            SNAPSHOT,
            CONFIG,
        )


def test_a_sizeless_kind_with_a_size_is_refused() -> None:
    with pytest.raises(ProposalRefused, match="size"):
        build_proposal(
            _decision(kind="CANCEL", strategy=None, invalidation=[], target_reference=REFERENCE),
            SNAPSHOT,
            CONFIG,
        )


def test_control_characters_in_the_thesis_die_at_the_real_ingress() -> None:
    """The worker passes narrative through; the contract's guard still fires."""

    proposal = build_proposal(
        _decision(thesis="clean text \x1b[31m then a repaint attempt"), SNAPSHOT, CONFIG
    )
    outcome = _through_the_real_ingress(proposal)
    assert not outcome.accepted
    assert "thesis" in outcome.refusal


def test_a_model_supplied_provenance_would_be_refused_by_the_ingress() -> None:
    """Belt and braces: even if assembly were bypassed, the ingress refuses."""

    proposal = build_proposal(_decision(), SNAPSHOT, CONFIG)
    proposal["provenance"] = {"provider": "self-attested"}
    outcome = _through_the_real_ingress(proposal)
    assert not outcome.accepted
    assert "writer-owned" in outcome.refusal


def test_non_string_fields_from_a_broken_model_are_refused() -> None:
    with pytest.raises(ProposalRefused, match="must be a string"):
        build_proposal(_decision(quantity=10), SNAPSHOT, CONFIG)


def test_an_oversized_thesis_is_refused_locally() -> None:
    with pytest.raises(ProposalRefused, match="4000"):
        build_proposal(_decision(thesis="x" * 4001), SNAPSHOT, CONFIG)

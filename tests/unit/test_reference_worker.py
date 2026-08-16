from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tests.unit.test_feature_compose_replay import _snapshot, _trace

from chronos.autonomy.advisory import advisory_facts_from_export
from chronos.autonomy.enums import DecisionDirection, DecisionKind
from chronos.autonomy.evidence import ADVISORY_BUNDLE_VERSION, issue
from chronos.autonomy.reference_worker import propose, propose_payload
from chronos.autonomy.worker_protocol import (
    REFERENCE_WORKER_PINS,
    WORKER_REQUEST_SCHEMA,
    build_worker_request,
)
from chronos.research.features.advisory_export import project_pairing_frame
from chronos.research.features.compose import compose_pairing_frames
from chronos.research.features.models import FeatureFamily, FeaturePolicy
from chronos.research.five_tool.models import SignalIntent
from chronos.supervisor.ingress import parse_proposal

_NOW = datetime(2026, 8, 15, 21, tzinfo=UTC)


def _bundle(*, intent: SignalIntent, tail_state: str, symbol: str = "QQQ"):
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    trace = _trace(index=0, intent=intent)
    snapshot = _snapshot(FeatureFamily.TAIL_RISK, trace, {"TR_STATE": tail_state})
    composition = compose_pairing_frames((trace,), (snapshot,), policy, symbol=symbol)
    payload = project_pairing_frame(composition.frames[0], trace, symbol=symbol)
    signals, snapshots, vetoes = advisory_facts_from_export(payload)
    return issue(
        bundle_id="eb-worker-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        five_tool_signals=signals,
        feature_snapshots=snapshots,
        pairing_vetoes=vetoes,
    )[0]


def test_reference_worker_holds_unless_enter_and_allow() -> None:
    held = propose(_bundle(intent=SignalIntent.NONE, tail_state="ORDINARY"))
    vetoed = propose(_bundle(intent=SignalIntent.ENTER_LONG, tail_state="FAT_TAILED"))
    opened = propose(_bundle(intent=SignalIntent.ENTER_LONG, tail_state="ORDINARY"))
    assert held.kind is DecisionKind.HOLD
    assert vetoed.kind is DecisionKind.HOLD
    assert opened.kind is DecisionKind.OPEN
    assert opened.direction is DecisionDirection.LONG
    assert opened.requested_quantity is None
    assert opened.symbol == "QQQ"
    iwm = propose(_bundle(intent=SignalIntent.ENTER_LONG, tail_state="ORDINARY", symbol="IWM"))
    assert iwm.kind is DecisionKind.OPEN
    assert iwm.symbol == "IWM"


def test_reference_worker_opens_gld_when_equity_weather_is_stress() -> None:
    policy = FeaturePolicy()
    trace = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    snapshots = (
        _snapshot(FeatureFamily.TAIL_RISK, trace, {"TR_STATE": "ORDINARY"}),
        _snapshot(FeatureFamily.RVOL, trace, {"IN_PLAY": True}),
        _snapshot(
            FeatureFamily.IV_REGIME,
            trace,
            {"IVP_STATE": "STRESS", "IVP_BACKWARDATION": False},
        ),
        _snapshot(FeatureFamily.BREADTH, trace, {"ALIGN": -1}),
    )
    gold = compose_pairing_frames((trace,), snapshots, policy, symbol="GLD")
    equity = compose_pairing_frames((trace,), snapshots, policy, symbol="QQQ")
    gold_bundle = issue(
        bundle_id="eb-gld-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        **dict(
            zip(
                ("five_tool_signals", "feature_snapshots", "pairing_vetoes"),
                advisory_facts_from_export(
                    project_pairing_frame(gold.frames[0], trace, symbol="GLD")
                ),
                strict=True,
            )
        ),
    )[0]
    equity_bundle = issue(
        bundle_id="eb-qqq-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        **dict(
            zip(
                ("five_tool_signals", "feature_snapshots", "pairing_vetoes"),
                advisory_facts_from_export(
                    project_pairing_frame(equity.frames[0], trace, symbol="QQQ")
                ),
                strict=True,
            )
        ),
    )[0]
    opened = propose(gold_bundle)
    held = propose(equity_bundle)
    assert opened.kind is DecisionKind.OPEN
    assert opened.symbol == "GLD"
    assert held.kind is DecisionKind.HOLD


def test_reference_worker_holds_companions_even_when_enter_and_allow() -> None:
    spy = propose(_bundle(intent=SignalIntent.ENTER_LONG, tail_state="ORDINARY", symbol="SPY"))
    qqqm = propose(_bundle(intent=SignalIntent.ENTER_LONG, tail_state="ORDINARY", symbol="QQQM"))
    assert spy.kind is DecisionKind.HOLD
    assert qqqm.kind is DecisionKind.HOLD
    assert spy.symbol == "QQQ"


def test_reference_worker_payload_survives_ingress_and_cannot_self_attest() -> None:
    bundle = _bundle(intent=SignalIntent.ENTER_SHORT, tail_state="ORDINARY")
    payload = propose_payload(bundle)
    outcome = parse_proposal(payload)
    assert outcome.accepted
    assert outcome.proposal is not None
    assert outcome.proposal.kind is DecisionKind.OPEN
    assert outcome.proposal.direction is DecisionDirection.SHORT
    document = json.loads(payload)
    document["provenance"] = {"provider": "forged"}
    refused = parse_proposal(json.dumps(document).encode("utf-8"))
    assert refused.accepted is False
    assert "writer-owned" in refused.refusal


def test_worker_request_binds_the_issued_digest() -> None:
    bundle = _bundle(intent=SignalIntent.NONE, tail_state="ORDINARY")
    request = build_worker_request(
        bundle,
        job_id="job-1",
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    assert request.job.schema_version == WORKER_REQUEST_SCHEMA
    assert request.job.bundle_digest == bundle.digest()
    assert request.job.expected_pins == REFERENCE_WORKER_PINS
    assert "provenance" not in request.evidence
    assert request.evidence["bundle_id"] == bundle.bundle_id

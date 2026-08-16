from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_feature_compose_replay import _snapshot, _trace

from chronos.autonomy.advisory import advisory_facts_from_export
from chronos.autonomy.evidence import ADVISORY_BUNDLE_VERSION, issue
from chronos.autonomy.shadow_journal import ShadowJournalRecord, read_shadow_records
from chronos.autonomy.worker_protocol import build_worker_request
from chronos.research.features.advisory_export import project_pairing_frame
from chronos.research.features.compose import compose_pairing_frames
from chronos.research.features.models import FeatureFamily, FeaturePolicy
from chronos.research.five_tool.models import SignalIntent
from chronos.supervisor.shadow_learning import journal_reference_worker

_NOW = datetime(2026, 8, 15, 21, tzinfo=UTC)


def _issued_bundle():
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    trace = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    snapshot = _snapshot(FeatureFamily.TAIL_RISK, trace, {"TR_STATE": "ORDINARY"})
    composition = compose_pairing_frames((trace,), (snapshot,), policy, symbol="QQQ")
    payload = project_pairing_frame(composition.frames[0], trace, symbol="QQQ")
    signals, snapshots, vetoes = advisory_facts_from_export(payload)
    return issue(
        bundle_id="eb-shadow-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        five_tool_signals=signals,
        feature_snapshots=snapshots,
        pairing_vetoes=vetoes,
    )[0]


def test_shadow_cycle_journals_open_without_transmit(tmp_path: Path) -> None:
    bundle = _issued_bundle()
    request = build_worker_request(
        bundle,
        job_id="job-shadow-1",
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    journal = tmp_path / "shadow.jsonl"
    record = journal_reference_worker(
        bundle=bundle,
        request=request,
        journal_path=journal,
        recorded_at=_NOW + timedelta(seconds=1),
    )
    assert record.ingress_accepted is True
    assert record.proposal_kind == "OPEN"
    assert record.five_tool_intent == "enter_long"
    assert record.veto_status == "allow"
    assert record.admission == "not_attempted"
    assert record.transmit is False
    stored = read_shadow_records(journal)
    assert stored == (record,)


def test_expired_job_is_journaled_and_not_parsed(tmp_path: Path) -> None:
    bundle = _issued_bundle()
    request = build_worker_request(
        bundle,
        job_id="job-shadow-expired",
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    record = journal_reference_worker(
        bundle=bundle,
        request=request,
        journal_path=tmp_path / "shadow.jsonl",
        recorded_at=_NOW + timedelta(minutes=6),
    )
    assert record.ingress_accepted is False
    assert record.proposal_kind is None
    assert "expired" in record.ingress_refusal
    assert record.transmit is False


def test_shadow_cycle_journals_gld_open_without_paper_or_transmit(tmp_path: Path) -> None:
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
    composition = compose_pairing_frames((trace,), snapshots, policy, symbol="GLD")
    payload = project_pairing_frame(composition.frames[0], trace, symbol="GLD")
    signals, snapshots_out, vetoes = advisory_facts_from_export(payload)
    bundle = issue(
        bundle_id="eb-shadow-gld",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        five_tool_signals=signals,
        feature_snapshots=snapshots_out,
        pairing_vetoes=vetoes,
    )[0]
    request = build_worker_request(
        bundle,
        job_id="job-shadow-gld",
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    record = journal_reference_worker(
        bundle=bundle,
        request=request,
        journal_path=tmp_path / "shadow.jsonl",
        recorded_at=_NOW + timedelta(seconds=1),
    )
    assert record.proposal_kind == "OPEN"
    assert record.veto_status == "allow"
    assert record.admission == "not_attempted"
    assert record.transmit is False


def test_shadow_record_cannot_claim_a_transmit() -> None:
    with pytest.raises(ValueError, match="transmit"):
        ShadowJournalRecord(
            schema_version="chronos-shadow-journal-v1",
            recorded_at=_NOW,
            job_id="job-1",
            bundle_id="eb-1",
            bundle_digest="b" * 64,
            ingress_accepted=True,
            transmit=True,
        )

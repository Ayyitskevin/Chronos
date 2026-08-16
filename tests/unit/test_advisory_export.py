from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.unit.test_feature_compose_replay import _snapshot, _trace

from chronos.autonomy.advisory import ADVISORY_EXPORT_SCHEMA as AUTONOMY_EXPORT_SCHEMA
from chronos.autonomy.advisory import advisory_facts_from_export
from chronos.autonomy.evidence import ADVISORY_BUNDLE_VERSION, AdvisoryDatum, issue
from chronos.research.features.advisory_export import ADVISORY_EXPORT_SCHEMA, project_pairing_frame
from chronos.research.features.compose import compose_pairing_frames
from chronos.research.features.models import FeatureFamily, FeaturePolicy
from chronos.research.five_tool.models import SignalIntent

_NOW = datetime(2026, 8, 15, 21, tzinfo=UTC)


def _composition(intent: SignalIntent = SignalIntent.ENTER_LONG):
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    trace = _trace(index=0, intent=intent)
    snapshot = _snapshot(FeatureFamily.TAIL_RISK, trace, {"TR_STATE": "ORDINARY"})
    return compose_pairing_frames((trace,), (snapshot,), policy, symbol="AAA"), trace


def test_export_schema_is_duplicated_not_imported() -> None:
    assert ADVISORY_EXPORT_SCHEMA == AUTONOMY_EXPORT_SCHEMA
    assert ADVISORY_EXPORT_SCHEMA == "chronos-five-tool-advisory-export-v1"


def test_projector_drops_economic_trace_keys() -> None:
    policy = FeaturePolicy(
        enable_tail_risk=True,
        enable_rvol=False,
        enable_iv_regime=False,
        enable_breadth=False,
    )
    trace = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    object.__setattr__(
        trace,
        "features",
        (
            ("regime", 1),
            ("long_score", 4),
            ("risk_scale", 0.5),
            ("long_stop_percent", 2.0),
            ("long_virtual_equity", 50_000.0),
        ),
    )
    snapshot = _snapshot(FeatureFamily.TAIL_RISK, trace, {"TR_STATE": "ORDINARY"})
    composition = compose_pairing_frames((trace,), (snapshot,), policy, symbol="AAA")
    payload = project_pairing_frame(composition.frames[0], trace, symbol="spy")
    names = {item["name"] for item in payload["five_tool"]["values"]}
    assert names == {"regime", "long_score"}
    assert payload["five_tool"]["advisory"] is True
    assert payload["five_tool"]["symbol"] == "SPY"


def test_autonomy_refuses_economic_advisory_names() -> None:
    with pytest.raises(ValueError, match="economic"):
        AdvisoryDatum(name="risk_scale", value=0.5)
    with pytest.raises(ValueError, match="economic"):
        AdvisoryDatum(name="long_stop_percent", value=2.0)


def test_export_round_trips_into_a_digest_pinned_bundle() -> None:
    composition, trace = _composition()
    payload = project_pairing_frame(composition.frames[0], trace, symbol="AAA")
    signals, snapshots, vetoes = advisory_facts_from_export(payload)
    bundle, digest = issue(
        bundle_id="eb-advisory-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        five_tool_signals=signals,
        feature_snapshots=snapshots,
        pairing_vetoes=vetoes,
    )
    assert digest == bundle.digest()
    assert bundle.five_tool_signals[0].intent == "enter_long"
    assert bundle.pairing_vetoes[0].status == "allow"
    assert bundle.five_tool_signals[0].advisory is True
    view = bundle.model_dump(mode="json")
    view["five_tool_signals"] = []
    assert bundle.five_tool_signals[0].intent == "enter_long"


def test_advisory_facts_require_bundle_version_1_1() -> None:
    composition, trace = _composition()
    payload = project_pairing_frame(composition.frames[0], trace, symbol="AAA")
    signals, snapshots, vetoes = advisory_facts_from_export(payload)
    with pytest.raises(ValueError, match="bundle_version"):
        issue(
            bundle_id="eb-advisory-1",
            bundle_version="1",
            issued_at=_NOW,
            five_tool_signals=signals,
            feature_snapshots=snapshots,
            pairing_vetoes=vetoes,
        )


def test_digest_changes_when_the_veto_does() -> None:
    composition, trace = _composition()
    allow = project_pairing_frame(composition.frames[0], trace, symbol="AAA")
    blocked_trace = _trace(index=0, intent=SignalIntent.ENTER_LONG)
    blocked_snapshot = _snapshot(FeatureFamily.TAIL_RISK, blocked_trace, {"TR_STATE": "FAT_TAILED"})
    blocked = compose_pairing_frames(
        (blocked_trace,),
        (blocked_snapshot,),
        FeaturePolicy(
            enable_tail_risk=True,
            enable_rvol=False,
            enable_iv_regime=False,
            enable_breadth=False,
        ),
        symbol="AAA",
    )
    vetoed = project_pairing_frame(blocked.frames[0], blocked_trace, symbol="AAA")
    first = issue(
        bundle_id="eb-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        **dict(
            zip(
                ("five_tool_signals", "feature_snapshots", "pairing_vetoes"),
                advisory_facts_from_export(allow),
                strict=True,
            )
        ),
    )[1]
    second = issue(
        bundle_id="eb-1",
        bundle_version=ADVISORY_BUNDLE_VERSION,
        issued_at=_NOW,
        **dict(
            zip(
                ("five_tool_signals", "feature_snapshots", "pairing_vetoes"),
                advisory_facts_from_export(vetoed),
                strict=True,
            )
        ),
    )[1]
    assert first != second

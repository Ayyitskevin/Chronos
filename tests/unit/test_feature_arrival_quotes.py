from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.research.features.arrival_quotes import (
    ARRIVAL_QUOTE_EXECUTION_STATE,
    ARRIVAL_QUOTE_PLAN_ID,
    AdmissionPoint,
    ArrivalQuote,
    ArrivalQuoteStatus,
    QuoteCondition,
    align_arrival_quotes,
    validate_arrival_quote_plan,
)
from chronos.research.features.models import FeatureInputError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "research" / "five_tool_intraday_quote_evidence_v1_manifest.json"
_ADMISSION = datetime(2026, 8, 24, 14, 30, tzinfo=UTC)


def _admission(*, sequence: str = "five-tool:QQQ:001") -> AdmissionPoint:
    return AdmissionPoint(
        symbol="qqq",
        opportunity_timestamp_utc=_ADMISSION - timedelta(hours=17, minutes=30),
        admission_timestamp_utc=_ADMISSION,
        primary_sequence_id=sequence,
    )


def _quote(
    *,
    source_offset_seconds: int = -2,
    receipt_offset_seconds: int = -1,
    sequence: str = "sip:001",
    bid: str = "99",
    ask: str = "101",
    source: str = "CTA-SIP-v1",
    condition: QuoteCondition = QuoteCondition.NORMAL,
) -> ArrivalQuote:
    return ArrivalQuote(
        symbol="QQQ",
        bid=Decimal(bid),
        ask=Decimal(ask),
        source_timestamp_utc=_ADMISSION + timedelta(seconds=source_offset_seconds),
        received_timestamp_utc=_ADMISSION + timedelta(seconds=receipt_offset_seconds),
        source_sequence_id=sequence,
        source=source,
        condition=condition,
    )


def test_alignment_uses_latest_quote_known_by_admission_without_backfill() -> None:
    causal = _quote(sequence="sip:causal")
    late_receipt = _quote(
        source_offset_seconds=-1,
        receipt_offset_seconds=1,
        sequence="sip:late",
        bid="99.5",
        ask="100.5",
    )
    future = _quote(
        source_offset_seconds=1,
        receipt_offset_seconds=1,
        sequence="sip:future",
        bid="99.9",
        ask="100.1",
    )

    (result,) = align_arrival_quotes(
        (_admission(),),
        (future, late_receipt, causal),
        max_quote_age_seconds=5,
    )

    assert result.status is ArrivalQuoteStatus.VALID
    assert result.measurement_eligible is True
    assert result.quote is causal
    assert result.quote_age_seconds == Decimal(2)
    assert result.relative_quoted_spread_bps == Decimal(200)
    assert result.reasons == ()


def test_late_receipt_and_future_quote_cannot_create_historical_evidence() -> None:
    late_receipt = _quote(
        source_offset_seconds=-1,
        receipt_offset_seconds=1,
        sequence="sip:late",
    )
    future = _quote(
        source_offset_seconds=1,
        receipt_offset_seconds=1,
        sequence="sip:future",
    )

    (result,) = align_arrival_quotes(
        (_admission(),),
        (late_receipt, future),
        max_quote_age_seconds=5,
    )

    assert result.status is ArrivalQuoteStatus.MISSING
    assert result.quote is None
    assert result.relative_quoted_spread_bps is None
    assert result.reasons == ("quote:missing_at_admission",)


def test_stale_quote_keeps_age_but_withholds_decision_grade_spread() -> None:
    stale = _quote(
        source_offset_seconds=-60,
        receipt_offset_seconds=-59,
        sequence="sip:stale",
    )

    (result,) = align_arrival_quotes(
        (_admission(),),
        (stale,),
        max_quote_age_seconds="5",
    )

    assert result.status is ArrivalQuoteStatus.STALE
    assert result.measurement_eligible is False
    assert result.quote_age_seconds == Decimal(60)
    assert result.relative_quoted_spread_bps is None
    assert result.reasons == ("quote:stale",)


@pytest.mark.parametrize(
    ("quote", "expected", "reason"),
    (
        (
            _quote(sequence="sip:halt", condition=QuoteCondition.HALTED),
            ArrivalQuoteStatus.INELIGIBLE_CONDITION,
            "quote:condition:HALTED",
        ),
        (
            _quote(sequence="sip:empty", bid="0", ask="1"),
            ArrivalQuoteStatus.EMPTY,
            "quote:non_positive_side",
        ),
        (
            _quote(sequence="sip:locked", bid="100", ask="100"),
            ArrivalQuoteStatus.LOCKED,
            "quote:locked",
        ),
        (
            _quote(sequence="sip:crossed", bid="101", ask="100"),
            ArrivalQuoteStatus.CROSSED,
            "quote:crossed",
        ),
    ),
)
def test_invalid_market_state_fails_closed(
    quote: ArrivalQuote,
    expected: ArrivalQuoteStatus,
    reason: str,
) -> None:
    (result,) = align_arrival_quotes(
        (_admission(),),
        (quote,),
        max_quote_age_seconds=5,
    )

    assert result.status is expected
    assert result.measurement_eligible is False
    assert result.relative_quoted_spread_bps is None
    assert result.reasons == (reason,)


def test_alignment_refuses_clock_identity_and_sequence_ambiguity() -> None:
    with pytest.raises(FeatureInputError, match="opportunity timestamp"):
        AdmissionPoint(
            symbol="QQQ",
            opportunity_timestamp_utc=_ADMISSION + timedelta(seconds=1),
            admission_timestamp_utc=_ADMISSION,
            primary_sequence_id="future-opportunity",
        )
    with pytest.raises(FeatureInputError, match="source timestamp"):
        _quote(source_offset_seconds=-1, receipt_offset_seconds=-2)
    with pytest.raises(FeatureInputError, match="source identity changed"):
        align_arrival_quotes(
            (_admission(),),
            (_quote(sequence="one"), _quote(sequence="two", source="UTP-SIP-v1")),
            max_quote_age_seconds=5,
        )
    with pytest.raises(FeatureInputError, match="duplicate source timestamp"):
        align_arrival_quotes(
            (_admission(),),
            (_quote(sequence="one"), _quote(sequence="two")),
            max_quote_age_seconds=5,
        )


def test_quote_condition_normalizes_known_wire_value_and_refuses_unknown() -> None:
    normal = _quote(condition="NORMAL")  # type: ignore[arg-type]

    assert normal.condition is QuoteCondition.NORMAL

    with pytest.raises(FeatureInputError, match="condition is not recognized"):
        _quote(condition="AUCTION")  # type: ignore[arg-type]


def test_plan_is_blocked_unselected_and_authorizes_zero_trials() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    report = validate_arrival_quote_plan(manifest)

    assert report.plan_id == ARRIVAL_QUOTE_PLAN_ID
    assert report.execution_state == ARRIVAL_QUOTE_EXECUTION_STATE
    assert report.selected_companion is None
    assert report.executable_trial_count == 0
    assert len(report.manifest_digest) == 64
    assert "pending_owner_certified_dataset" in report.blockers
    assert "thresholds_not_selected_or_frozen" in report.blockers


@pytest.mark.parametrize(
    ("path", "value", "match"),
    (
        (("selected_companion",), "C-1", "selects no companion"),
        (("executable_trial_count",), 1, "zero executable trials"),
        (("promotion_authority",), "paper", "no promotion authority"),
        (("threshold_policy", "max_quote_age_seconds"), 5, "thresholds must remain null"),
        (("data_contract", "dataset_id"), "forged", "dataset_id must remain unset"),
        (("implementation_scope", "may_apply_veto"), True, "forbids may_apply_veto"),
    ),
)
def test_plan_refuses_authority_data_threshold_and_veto_mutations(
    path: tuple[str, ...],
    value: object,
    match: str,
) -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    mutated = copy.deepcopy(manifest)
    target = mutated
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(FeatureInputError, match=match):
        validate_arrival_quote_plan(mutated)

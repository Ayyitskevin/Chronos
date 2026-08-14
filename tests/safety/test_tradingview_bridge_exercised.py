"""The bridge's output is a proposal the real ingress accepts — proven, not assumed.

``chronos.bridge`` deliberately does not import ``chronos.autonomy``, so nothing
in the production import graph forces its output to agree with the decision
contract. This file is what makes that duplication safe: it runs the bridge's own
translation and pushes the bytes through the real
:func:`chronos.supervisor.ingress.parse_proposal`, the same function the backend
route calls on the same bytes.

That direction matters. A test that asserted the dict "looks right" would pass
while the contract moved underneath it. Asserting the *actual parser accepts it*
is the only claim worth making, and it fails the moment a validator tightens.

The refusal half is exercised in the house pattern: each refusal is fired by an
alert that should trigger it and the never-before-seen outcome is asserted — a
refusal that cannot fire is the inert-control shape this repository was burned by
four times (R-24..R-27).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from chronos.autonomy import ProposedDecision
from chronos.bridge.alert import AlertRejected, AlertUnauthorized, parse_alert
from chronos.bridge.config import BridgeConfig
from chronos.bridge.translate import TranslationRefused, build_proposal
from chronos.supervisor import ingress

SECRET = "s" * 40
SENT_AT = datetime(2026, 8, 12, 14, 30, tzinfo=UTC)
REFERENCE = "CHR-TEST-0123456789ABCDEF0123456789ABCDEF"

CONFIG = BridgeConfig(
    secret=SECRET,
    api_token="token",
    proposer_token="",
    ingress_url="http://127.0.0.1:8000/autonomy/proposals",
    evidence_url="http://127.0.0.1:8000/autonomy/evidence",
    host="127.0.0.1",
    port=8109,
    symbols=frozenset({"SPY", "IWM"}),
    kinds=frozenset({"OPEN", "CLOSE", "REDUCE", "HOLD", "CANCEL"}),
    max_alerts_per_minute=10,
    max_alert_age_seconds=120,
    replay_window_seconds=3600,
    forward=False,
)


def _body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "secret": SECRET,
        "alert_id": "spy-breakout-001",
        "sent_at": SENT_AT.isoformat(),
        "action": "OPEN",
        "symbol": "SPY",
        "direction": "LONG",
        "quantity": "10",
        "strategy": "LONG_EQUITY",
        "thesis": "20-day breakout with expanding range",
        "invalidation": ["close below the 20-day moving average"],
    }
    payload.update(overrides)
    return json.dumps({k: v for k, v in payload.items() if v is not None}).encode("utf-8")


def _translate(**overrides: object) -> dict[str, object]:
    alert = parse_alert(_body(**overrides), expected_secret=SECRET)
    return build_proposal(alert, CONFIG)


def _through_the_real_ingress(proposal: dict[str, object]) -> ingress.IngressOutcome:
    return ingress.parse_proposal(json.dumps(proposal).encode("utf-8"))


# ------------------------------------------------- the bridge produces a real proposal


def test_a_translated_alert_is_accepted_by_the_real_ingress() -> None:
    outcome = _through_the_real_ingress(_translate())

    assert outcome.accepted, f"the real ingress refused the bridge's output: {outcome.refusal}"
    assert outcome.proposal is not None
    assert outcome.proposal.kind.value == "OPEN"
    assert outcome.proposal.symbol == "SPY"
    assert outcome.proposal.direction.value == "LONG"
    assert outcome.proposal.evidence[0].kind == "tradingview_alert"


@pytest.mark.parametrize(
    ("action", "overrides"),
    [
        ("OPEN", {}),
        (
            "CLOSE",
            {"strategy": None, "target_reference": REFERENCE, "invalidation": None},
        ),
        (
            "REDUCE",
            {"strategy": None, "target_reference": REFERENCE, "invalidation": None},
        ),
        (
            "HOLD",
            {
                "strategy": None,
                "quantity": None,
                "direction": "NEUTRAL",
                "invalidation": None,
            },
        ),
        (
            "CANCEL",
            {
                "strategy": None,
                "quantity": None,
                "target_reference": REFERENCE,
                "invalidation": None,
            },
        ),
    ],
)
def test_every_allowlisted_kind_translates_into_an_accepted_proposal(
    action: str, overrides: dict[str, object]
) -> None:
    """Each kind the bridge may emit really does survive the contract."""

    outcome = _through_the_real_ingress(_translate(action=action, **overrides))

    assert outcome.accepted, f"{action} was refused by the real ingress: {outcome.refusal}"
    assert outcome.proposal is not None
    assert outcome.proposal.kind.value == action


def test_the_bridge_emits_only_fields_the_contract_declares() -> None:
    """No key the bridge writes is a field the contract does not have.

    A typo'd key would be silently ignored by a lenient parser; the contract
    forbids extras, so this also proves the bridge cannot invent a field name
    that a future contract might give meaning to.
    """

    emitted = set(_translate())
    assert emitted <= set(ProposedDecision.model_fields), (
        f"the bridge emits keys the decision contract does not declare: "
        f"{sorted(emitted - set(ProposedDecision.model_fields))}"
    )


def test_the_bridge_never_emits_the_writer_owned_fields() -> None:
    """A proposal carrying provenance or decision_id is refused loudly, by design."""

    proposal = _translate()
    assert "provenance" not in proposal
    assert "decision_id" not in proposal


def test_the_evidence_digest_is_the_alert_the_owner_can_recompute() -> None:
    """The audit record names which alert text produced the decision."""

    from chronos.bridge.alert import canonical_digest

    raw = _body()
    fields = {k: v for k, v in json.loads(raw).items() if k != "secret"}
    proposal = _translate()
    citation = proposal["evidence"]
    assert isinstance(citation, list)
    assert citation[0]["digest"] == canonical_digest(fields)
    assert citation[0]["evidence_id"] == "tradingview-alert:spy-breakout-001"


def test_the_secret_never_reaches_the_proposal() -> None:
    """It is stripped before the digest, the citation, and the payload."""

    assert SECRET not in json.dumps(_translate())


# --------------------------------------------------------------- the refusals do fire


def test_an_exposure_creating_alert_without_invalidation_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="invalidation"):
        _translate(invalidation=None)


def test_a_targeted_alert_without_a_reference_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="Chronos reference"):
        _translate(action="CLOSE", strategy=None, invalidation=None)


def test_a_broker_order_id_as_the_target_reference_is_refused() -> None:
    """A decision may never name the broker's namespace."""

    with pytest.raises(TranslationRefused, match="broker order id"):
        _translate(
            action="CLOSE",
            strategy=None,
            invalidation=None,
            target_reference="00012345",
        )


def test_a_hold_with_a_direction_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="may not express a direction"):
        _translate(action="HOLD", quantity=None, strategy=None, invalidation=None)


def test_a_sizeless_kind_carrying_a_size_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="may not request a size"):
        _translate(
            action="CANCEL",
            strategy=None,
            invalidation=None,
            target_reference=REFERENCE,
        )


def test_a_risk_reducing_kind_carrying_a_strategy_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="may not request a strategy"):
        _translate(
            action="CLOSE",
            invalidation=None,
            target_reference=REFERENCE,
        )


def test_a_symbol_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="symbol this alert names"):
        _translate(symbol="TSLA")


def test_a_kind_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(TranslationRefused, match="decision kind this alert asks for"):
        _translate(action="HEDGE")


def test_no_translation_refusal_quotes_what_the_sender_sent() -> None:
    """The invariant the response relies on, asserted at its source.

    Every refusal below is triggered with a distinctive caller-supplied value,
    and none of the messages may contain it. Checked here rather than only at
    the transport, so a future message that quotes an alert field fails in the
    module that introduced it.
    """

    marker = "ZZMARKERZZ"
    cases: list[dict[str, object]] = [
        {"symbol": marker[:8]},
        {"action": "HEDGE", "thesis": marker},
        {"invalidation": None, "thesis": marker},
        {"action": "CLOSE", "strategy": None, "invalidation": None, "thesis": marker},
    ]
    for overrides in cases:
        with pytest.raises(TranslationRefused) as error:
            _translate(**overrides)
        assert marker[:8] not in str(error.value), (
            f"a translation refusal quoted the sender's own text: {error.value}"
        )


def test_a_wrong_secret_is_refused_before_anything_else_is_judged() -> None:
    """An unauthenticated caller learns only that it is unauthenticated."""

    body = _body(secret="w" * 40, action="NOT-A-KIND", symbol="!!!")
    with pytest.raises(AlertUnauthorized) as error:
        parse_alert(body, expected_secret=SECRET)
    message = str(error.value)
    assert "NOT-A-KIND" not in message
    assert "!!!" not in message


def test_a_missing_secret_is_refused_identically_to_a_wrong_one() -> None:
    missing = json.loads(_body())
    del missing["secret"]
    with pytest.raises(AlertUnauthorized) as absent:
        parse_alert(json.dumps(missing).encode("utf-8"), expected_secret=SECRET)
    with pytest.raises(AlertUnauthorized) as wrong:
        parse_alert(_body(secret="w" * 40), expected_secret=SECRET)
    assert str(absent.value) == str(wrong.value)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"[1, 2, 3]",
        b'{"secret": "' + SECRET.encode() + b'", "quantity": NaN}',
    ],
    ids=["empty", "not-json", "not-an-object", "nan"],
)
def test_hostile_bodies_are_refused_without_raising_anything_else(body: bytes) -> None:
    with pytest.raises(AlertRejected):
        parse_alert(body, expected_secret=SECRET)


def test_an_oversized_body_is_refused_by_length() -> None:
    oversized = b'{"secret": "' + (b"x" * (16 * 1024 + 64)) + b'"}'
    with pytest.raises(AlertRejected, match="over the"):
        parse_alert(oversized, expected_secret=SECRET)


def test_a_refusal_message_never_echoes_alert_content() -> None:
    """A hostile sender must not write chosen text into the owner's logs."""

    marker = "PWNED-BY-THE-PAYLOAD"
    with pytest.raises(AlertRejected) as error:
        parse_alert(_body(action=marker), expected_secret=SECRET)
    assert marker not in str(error.value)


def test_control_characters_in_narrative_are_refused() -> None:
    """An ANSI escape could repaint the terminal an operator reviews this in."""

    with pytest.raises(AlertRejected, match="control characters"):
        _translate(thesis="clean text \x1b[31m then red")


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    """A field the bridge drops silently is one the author thinks is working."""

    with pytest.raises(AlertRejected, match="unknown field"):
        _translate(order_type="MKT")

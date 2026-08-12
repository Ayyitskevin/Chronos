"""The mandate review reports inert authority, and authors none of its own.

Two things are being pinned here, and only one of them is about output.

**It is read-only.** ``chronos.cli.mandate_check`` is the first tool that reads
an owner's grant outside the backend, so the guarantee that matters most is that
it cannot become a way to write one. The structural half of that lives in
``tests/safety/test_autonomy_contracts.py`` (the module may name the mandate
contract and no decision type); the behavioural half is here: no command it
exposes writes a file, and the template it prints is non-submitting.

**It tells the truth about what binds.** The review exists because a mandate can
be valid, activate cleanly, and still constrain nothing the owner thought it
did. Each finding below is asserted against a mandate that actually has the
defect, because a lint whose rule never fires is the same failure class as a
control that cannot fire.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    SessionPolicy,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.cli.main import main
from chronos.cli.mandate_check import (
    RegisteredPins,
    RegistryPosture,
    Severity,
    load_mandate_text,
    review_mandate,
)
from chronos.domain.enums import DataQuality
from chronos.utils.identifiers import account_fingerprint

_NOW = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
_ACCOUNT = "DU1234567"
_FINGERPRINT = account_fingerprint(_ACCOUNT)

_INGRESS_PINS = {
    "provider": "external-worker",
    "model_id": "ingress",
    "model_version": "1",
    "prompt_version": "1",
    "tool_schema_version": "1",
    "decision_schema_version": "1",
    "policy_version": "1",
}


def _pins(**overrides: str) -> VersionPins:
    return VersionPins(**{**_INGRESS_PINS, **overrides})


def _shadow(**overrides: object) -> AutonomyMandate:
    base: dict[str, object] = {
        "mandate_id": "m-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.SHADOW,
        "promotions": (
            FamilyPromotion(asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.SHADOW),
        ),
        "effective_from": _NOW - timedelta(days=1),
        "expires_at": _NOW + timedelta(days=90),
        "versions": _pins(),
        "owner_authorization_ref": "owner-1",
        "authored_at": _NOW - timedelta(days=1),
    }
    return AutonomyMandate(**{**base, **overrides})  # type: ignore[arg-type]


def _paper(**overrides: object) -> AutonomyMandate:
    """A minimally-valid submitting mandate: every required floor and ceiling set."""

    base: dict[str, object] = {
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY,
                level=PromotionLevel.PAPER_AUTONOMOUS,
            ),
        ),
        "scope": InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            allocated_capital_usd=Decimal(1000),
            max_order_notional_usd=Decimal(200),
            max_gross_exposure_usd=Decimal(1000),
            max_shares_per_order=5,
            min_cash_floor_usd=Decimal(50),
            min_buying_power_usd=Decimal(50),
        ),
        "concentration": ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.5")),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
            max_relative_spread=Decimal("0.02"),
        ),
    }
    return _shadow(**{**base, **overrides})


def _codes(mandate: AutonomyMandate, **kwargs: object) -> dict[str, Severity]:
    findings = review_mandate(
        mandate,
        now=kwargs.pop("now", _NOW),  # type: ignore[arg-type]
        expected_fingerprint=kwargs.pop("expected_fingerprint", _FINGERPRINT),  # type: ignore[arg-type]
        fingerprint_source="test",
        ingress_pins=kwargs.pop("ingress_pins", dict(_INGRESS_PINS)),  # type: ignore[arg-type]
        registry=kwargs.pop("registry", None),  # type: ignore[arg-type]
    )
    assert not kwargs, f"unexpected kwargs {sorted(kwargs)}"
    return {finding.code: finding.severity for finding in findings}


# ----------------------------------------------------------------- read-only


def test_no_mandate_command_writes_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the tool is that it cannot become an authoring path.

    Asserted over the directory rather than over the code: a future command that
    wrote a mandate — even helpfully, even to a path the owner passed — would
    fail here without anyone remembering to add a case for it.
    """

    before = sorted(tmp_path.rglob("*"))
    mandate_file = tmp_path / "mandate.json"
    mandate_file.write_text(_shadow().model_dump_json(), encoding="utf-8")

    assert main(["mandate", "check", "--file", str(mandate_file), "--account-id", _ACCOUNT]) == 0
    assert main(["mandate", "template"]) == 0
    assert main(["mandate", "fingerprint", "--account-id", _ACCOUNT]) == 0

    after = sorted(tmp_path.rglob("*"))
    assert after == [*before, mandate_file], "a mandate command created or removed a file"
    assert mandate_file.read_text(encoding="utf-8") == _shadow().model_dump_json()
    capsys.readouterr()


def test_the_printed_template_is_shadow_and_not_submitting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A template that produced a submitting mandate would be authoring authority."""

    assert main(["mandate", "template"]) == 0
    printed = capsys.readouterr().out
    body = printed.split("\n#", 1)[0]
    skeleton = json.loads(body)
    assert skeleton["mode"] == AutonomyMode.SHADOW.value
    assert AutonomyMode(skeleton["mode"]) not in {
        AutonomyMode.PAPER_AUTONOMOUS,
        AutonomyMode.CANARY_LIVE_AUTONOMOUS,
        AutonomyMode.LIVE_AUTONOMOUS,
    }
    for promotion in skeleton["promotions"]:
        assert promotion["level"] == PromotionLevel.SHADOW.value


def test_the_report_never_claims_to_authorize(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mandate_file = tmp_path / "mandate.json"
    mandate_file.write_text(_shadow().model_dump_json(), encoding="utf-8")
    assert main(["mandate", "check", "--file", str(mandate_file), "--account-id", _ACCOUNT]) == 0
    printed = capsys.readouterr().out
    assert "This is a description, not an authorization." in printed


# ------------------------------------------------------------------ validation


def test_an_invalid_document_is_reported_field_by_field() -> None:
    result = load_mandate_text(b'{"mandate_id": "m", "mandate_version": 0}')
    assert result.mandate is None
    joined = "\n".join(result.errors)
    assert "mandate_version" in joined
    assert "account_fingerprint" in joined


def test_an_invalid_file_exits_one_and_says_the_backend_would_boot_inert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert main(["mandate", "check", "--file", str(bad)]) == 1
    assert "boot inert" in capsys.readouterr().out


def test_a_missing_file_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mandate", "check", "--file", str(tmp_path / "absent.json")]) == 1
    assert "cannot read" in capsys.readouterr().out


# -------------------------------------------------------------------- findings


def test_an_expired_mandate_is_blocking() -> None:
    codes = _codes(_shadow(), now=_NOW + timedelta(days=91))
    assert codes["expired"] is Severity.BLOCKING


def test_a_future_window_is_reported_before_it_opens() -> None:
    codes = _codes(_shadow(), now=_NOW - timedelta(days=2))
    assert codes["not-yet-effective"] is Severity.IMPORTANT


def test_a_wrong_account_is_blocking_because_the_backend_boots_inert() -> None:
    codes = _codes(_shadow(), expected_fingerprint=account_fingerprint("DU7654321"))
    assert codes["account-mismatch"] is Severity.BLOCKING


def test_an_unresolvable_account_is_reported_as_skipped_not_as_passing() -> None:
    """An unevaluated check must never read as a satisfied one."""

    codes = _codes(_shadow(), expected_fingerprint=None)
    assert "account-check-skipped" in codes
    assert "account-match" not in codes


def test_pins_that_disagree_with_the_ingress_are_blocking() -> None:
    codes = _codes(_shadow(versions=_pins(policy_version="7")))
    assert codes["version-pin-mismatch"] is Severity.BLOCKING


def test_unavailable_pins_are_reported_as_skipped_not_as_agreeing() -> None:
    codes = _codes(_shadow(), ingress_pins=None)
    assert "pins-check-skipped" in codes
    assert "version-pins-agree" not in codes


def _registered(proposer_id: str, *, current: bool = True, **overrides: str) -> RegisteredPins:
    return RegisteredPins(
        proposer_id=proposer_id,
        pins={**_INGRESS_PINS, "provider": "anthropic", "model_id": "claude-worker", **overrides},
        current=current,
    )


def test_with_a_registry_the_pins_are_compared_against_registrations_not_the_ingress() -> None:
    """ADR-0023: registry-on flips what the pins must agree with.

    A mandate pinned to a registered author is correct even though it disagrees
    with the static ingress stamp — and the old comparison would have called it
    BLOCKING, which is exactly the wrong-reference defect the seam carries.
    """

    mandate = _shadow(versions=_pins(provider="anthropic", model_id="claude-worker"))
    registry = RegistryPosture(path="proposers.json", proposers=(_registered("claude-worker"),))
    codes = _codes(mandate, registry=registry)
    assert codes["version-pins-authorize"] is Severity.NOTE
    assert "version-pin-mismatch" not in codes
    assert "version-pins-agree" not in codes


def test_pins_matching_no_registration_are_blocking_under_a_registry() -> None:
    # The template's static pins, met by a registry that registers real authors:
    # nothing can propose under this grant.
    registry = RegistryPosture(path="proposers.json", proposers=(_registered("claude-worker"),))
    codes = _codes(_shadow(), registry=registry)
    assert codes["version-pins-authorize-nobody"] is Severity.BLOCKING


def test_pins_matching_only_a_stale_registration_are_reported() -> None:
    mandate = _shadow(versions=_pins(provider="anthropic", model_id="claude-worker"))
    registry = RegistryPosture(
        path="proposers.json", proposers=(_registered("claude-worker", current=False),)
    )
    codes = _codes(mandate, registry=registry)
    assert codes["authorized-proposer-stale"] is Severity.IMPORTANT
    assert "version-pins-authorize" not in codes


def test_a_configured_but_invalid_registry_is_reported_not_fallen_back_from() -> None:
    codes = _codes(_shadow(), registry=RegistryPosture(path="proposers.json", proposers=None))
    assert codes["proposer-registry-invalid"] is Severity.IMPORTANT
    assert "version-pins-agree" not in codes
    assert "version-pin-mismatch" not in codes


def test_an_empty_registry_is_reported_as_the_lockdown_it_is() -> None:
    codes = _codes(_shadow(), registry=RegistryPosture(path="proposers.json", proposers=()))
    assert codes["proposer-registry-empty"] is Severity.IMPORTANT


def test_a_limit_written_into_an_inert_field_is_reported() -> None:
    """The defect this tool exists for: a number that constrains nothing."""

    mandate = _paper(
        concentration=ConcentrationLimits(
            max_symbol_exposure_pct=Decimal("0.5"),
            max_sector_exposure_pct=Decimal("0.25"),
        ),
        sessions=SessionPolicy(permitted_sessions=(), allow_overnight_holding=True),
    )
    findings = review_mandate(
        mandate,
        now=_NOW,
        expected_fingerprint=_FINGERPRINT,
        fingerprint_source="test",
        ingress_pins=dict(_INGRESS_PINS),
    )
    inert = [item for item in findings if item.code == "inert-limit-set"]
    reported = {item.message.split(" ", 1)[0] for item in inert}
    assert reported == {
        "concentration.max_sector_exposure_pct",
        "sessions.allow_overnight_holding",
    }
    assert all(item.severity is Severity.IMPORTANT for item in inert)


def test_an_enforced_limit_is_not_reported_as_inert() -> None:
    """Guard the guard: flagging a binding limit would train the owner to ignore this."""

    codes = _codes(_paper())
    assert "inert-limit-set" not in codes
    assert "inert-scope-set" not in codes


def test_scope_exchange_and_family_are_not_reported_as_inert() -> None:
    """They bind, in the compiler, and the first draft of this tool said they did not.

    ``chronos.supervisor.compiler`` refuses ``EXCHANGE_NOT_PERMITTED`` and
    ``FAMILY_NOT_PERMITTED`` against the qualified contract (M4). Admission does
    not read them, and its docstring said "not enforced anywhere" long after the
    compiler landed. Reporting a binding restriction as decoration is the same
    defect class as the one this tool exists to catch, aimed the other way — so
    it gets its own pin.
    """

    mandate = _paper(
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
            exchanges=("SMART",),
            contract_families=("SPY",),
        )
    )
    assert "inert-scope-set" not in _codes(mandate)


def test_the_compiler_is_searched_before_calling_a_scope_field_inert() -> None:
    """Whatever INERT_SCOPE_FIELDS names must be read by no kernel module.

    The classification for *limits* is pinned against admission, sizing, and
    durable in ``test_supervisor_gateway.py``. Scope fields are compiled rather
    than admitted, so they need their own scan — omitting the compiler is
    exactly how ``exchanges`` came to be misclassified.
    """

    import inspect

    import chronos.supervisor.admission as admission_module
    import chronos.supervisor.compiler as compiler_module
    import chronos.supervisor.durable as durable_module
    import chronos.supervisor.sizing as sizing_module
    from chronos.autonomy.enforcement import INERT_SCOPE_FIELDS

    kernel = "".join(
        inspect.getsource(module)
        for module in (compiler_module, admission_module, sizing_module, durable_module)
    )
    for field in INERT_SCOPE_FIELDS:
        assert f"scope.{field}" not in kernel, (
            f"scope.{field} is classified inert but a kernel module reads it"
        )


def test_a_zero_spread_ceiling_is_reported_as_no_ceiling() -> None:
    """``max_relative_spread`` is a max_ field where zero means unbounded.

    Admission skips the comparison entirely at zero
    (``chronos/supervisor/admission.py``), so the default imposes nothing. The
    mandate contract discloses three floors with this inversion and not this
    one, which is why the review says it out loud.
    """

    mandate = _paper(
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        )
    )
    assert _codes(mandate)["no-spread-ceiling"] is Severity.IMPORTANT
    assert "no-spread-ceiling" not in _codes(_paper())


def test_a_zero_spread_ceiling_is_not_reported_for_a_non_submitting_mandate() -> None:
    """A SHADOW mandate submits nothing, so an unset spread ceiling is not a trap."""

    assert "no-spread-ceiling" not in _codes(_shadow())


def test_model_discretion_names_the_ceilings_it_waives() -> None:
    mandate = _paper(
        capital=CapitalLimits(
            model_discretion=True,
            min_cash_floor_usd=Decimal(50),
            min_buying_power_usd=Decimal(50),
        )
    )
    findings = review_mandate(
        mandate,
        now=_NOW,
        expected_fingerprint=_FINGERPRINT,
        fingerprint_source="test",
        ingress_pins=dict(_INGRESS_PINS),
    )
    discretion = next(item for item in findings if item.code == "model-discretion")
    assert discretion.severity is Severity.IMPORTANT
    assert "max_order_notional_usd" in discretion.message
    assert "min_cash_floor_usd" in discretion.message


def test_a_rung_above_shadow_is_reported_as_self_declared() -> None:
    mandate = _paper()
    assert _codes(mandate)["self-declared-rung"] is Severity.IMPORTANT
    assert "self-declared-rung" not in _codes(_shadow())


def test_strict_turns_important_findings_into_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mandate = _paper(
        concentration=ConcentrationLimits(
            max_symbol_exposure_pct=Decimal("0.5"),
            max_sector_exposure_pct=Decimal("0.25"),
        )
    )
    path = tmp_path / "mandate.json"
    path.write_text(mandate.model_dump_json(), encoding="utf-8")
    argv = ["mandate", "check", "--file", str(path), "--account-id", _ACCOUNT]
    assert main(argv) == 0
    assert main([*argv, "--strict"]) == 1
    capsys.readouterr()


def test_the_report_lists_every_inert_field_not_only_the_ones_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A disclosure, not only a lint: the owner sees the set before writing one."""

    path = tmp_path / "mandate.json"
    path.write_text(_shadow().model_dump_json(), encoding="utf-8")
    assert main(["mandate", "check", "--file", str(path), "--account-id", _ACCOUNT]) == 0
    printed = capsys.readouterr().out
    for field in (
        "concentration.max_sector_exposure_pct",
        "market_data.min_open_interest",
        "sessions.permitted_sessions",
    ):
        assert field in printed

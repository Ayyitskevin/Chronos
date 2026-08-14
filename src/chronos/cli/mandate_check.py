"""``python -m chronos.cli mandate check`` — read the owner's grant back to them.

Strictly read-only. It opens a mandate file, validates it exactly as the backend
does, and reports what the document authorizes, what it does not, and which of
its fields nothing reads. It writes nothing, activates nothing, revokes nothing,
and has no flag that changes authority: authoring a mandate is an owner act and
stays one (ADR-0016 §3, ADR-0017).

Why it exists. This repository's recurring defect is a control that is written
down, validated, covered by a passing test, and structurally unable to act
(R-24 … R-27). A mandate is the same shape one level up:
:class:`~chronos.autonomy.mandate.AutonomyMandate` validates *internal*
consistency and the document is then believed. Several mistakes therefore pass
validation and surface only as silence at trade time:

1. a limit written into a field no deterministic module reads — the
   classification in :mod:`chronos.autonomy.enforcement`;
2. an ``account_fingerprint`` that is not this machine's, which makes the whole
   mandate inert at boot behind one ``ERROR`` line in a log;
3. version pins that disagree with the ingress stamp, which refuses every
   proposal with ``VERSION_PIN_MISMATCH`` *after* admission has begun;
4. a window that has expired, or has not opened yet;
5. ``max_relative_spread`` left at zero, where zero means *no spread ceiling*
   rather than the strictest one.

None of those is a kernel bug. Each is a document that looks complete.

What this tool cannot tell you: whether a promotion rung was earned. Nothing in
Chronos can — a rung is a value typed into the file (ADR-0024, proposed). The
report says so rather than implying the rungs were checked.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from chronos.autonomy.enforcement import INERT, INERT_SCOPE_FIELDS, LIMIT_ENFORCEMENT
from chronos.autonomy.enums import (
    LIVE_AUTONOMY_MODES,
    SUBMITTING_AUTONOMY_MODES,
    PromotionLevel,
    promotion_rank,
)
from chronos.autonomy.mandate import MAX_LIVE_MANDATE_DURATION, AutonomyMandate
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.time import utc_now

#: Rungs at or below which a self-declared level is not yet load-bearing. Above
#: this, ADR-0024 (proposed) would require the rung to name the evidence that
#: earned it; today nothing does, so the report says the rung is an assertion.
_SELF_DECLARED_RUNG_CEILING = PromotionLevel.SHADOW

#: How close to expiry is worth mentioning.
_EXPIRY_WARNING = timedelta(days=14)


class Severity(StrEnum):
    """How much a finding should change what the owner does next."""

    #: As written, this mandate authorizes nothing, or authorizes nothing that
    #: can be acted on. Exit code 1.
    BLOCKING = "BLOCKING"
    #: It grants, but something the owner wrote does not bind, or binds
    #: differently than the field name suggests.
    IMPORTANT = "IMPORTANT"
    #: True, consequential, and not a mistake.
    NOTE = "NOTE"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    code: str
    message: str


def _finding(severity: Severity, code: str, message: str) -> Finding:
    return Finding(severity=severity, code=code, message=message)


@dataclass(frozen=True, slots=True)
class RegisteredPins:
    """One registered proposer's stampable identity, for the pins comparison."""

    proposer_id: str
    pins: dict[str, str]
    current: bool


@dataclass(frozen=True, slots=True)
class RegistryPosture:
    """What AUTONOMY_PROPOSERS_FILE resolves to at review time (ADR-0023).

    ``proposers=None`` means the file is configured and does not load — a
    posture with consequences of its own, distinct from "not configured",
    which is represented by not constructing this object at all.
    """

    path: str
    proposers: tuple[RegisteredPins, ...] | None


@dataclass(frozen=True, slots=True)
class EvidencePosture:
    """What AUTONOMY_EVIDENCE_BUNDLES resolves to at review time (ADR-0028).

    Reported for the same reason the registry posture is: a mandate authored
    against the wrong posture should be BLOCKING at *authoring* time rather than
    become a run of silent refusals at trade time. Not constructing this object
    means "not configured" — the pre-ADR-0028 posture, which needs no finding
    because it is what every existing mandate was written against.
    """

    enabled: bool
    #: Whether a proposer registry is configured alongside it. Evidence binding
    #: issues bundles *to* a credential, so without a registry it refuses every
    #: proposal — a configuration error the owner should learn about here.
    registry_configured: bool
    ttl_seconds: float


# --------------------------------------------------------------------- loading


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Either a validated mandate or the reasons it is not one. Never both."""

    mandate: AutonomyMandate | None
    errors: tuple[str, ...]


def load_mandate_text(raw: bytes) -> LoadResult:
    """Validate mandate bytes the way ``load_persistent_mandate`` does.

    The backend calls ``AutonomyMandate.model_validate_json`` and treats any
    failure as *no mandate*. This mirrors that exactly — same method, same
    model — so a file this reports as valid is one the backend will also accept,
    and the errors here are the errors it would have logged.
    """

    try:
        mandate = AutonomyMandate.model_validate_json(raw)
    except ValidationError as error:
        return LoadResult(mandate=None, errors=tuple(_render_validation_error(error)))
    return LoadResult(mandate=mandate, errors=())


def _render_validation_error(error: ValidationError) -> list[str]:
    """One readable line per problem, deepest field path first.

    Pydantic's own rendering buries the field path under a URL. An owner
    correcting a hand-written JSON document needs the path and the rule.
    """

    lines: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(document root)"
        lines.append(f"{location}: {item['msg']}")
    return lines


# -------------------------------------------------------------------- the review


def review_mandate(
    mandate: AutonomyMandate,
    *,
    now: datetime,
    expected_fingerprint: str | None = None,
    fingerprint_source: str = "",
    ingress_pins: dict[str, str] | None = None,
    registry: RegistryPosture | None = None,
    evidence: EvidencePosture | None = None,
) -> list[Finding]:
    """Every finding this tool can make about a *valid* mandate.

    Pure: no file reads, no clock of its own, no settings lookup. The caller
    supplies ``now`` and whatever it managed to resolve, and a check whose input
    is missing is reported as skipped rather than silently passed — an
    unevaluated check must never read as a satisfied one.
    """

    findings: list[Finding] = []
    findings.extend(_review_window(mandate, now=now))
    findings.extend(
        _review_account(
            mandate, expected=expected_fingerprint, fingerprint_source=fingerprint_source
        )
    )
    findings.extend(_review_pins(mandate, ingress_pins=ingress_pins, registry=registry))
    findings.extend(_review_inert_fields(mandate))
    findings.extend(_review_permissive_zeros(mandate))
    findings.extend(_review_discretion(mandate))
    findings.extend(_review_promotions(mandate))
    findings.extend(_review_posture(mandate))
    findings.extend(_review_evidence_posture(evidence))
    return findings


def _review_evidence_posture(evidence: EvidencePosture | None) -> list[Finding]:
    """Say what evidence binding will do to this mandate's proposals (ADR-0028).

    Three postures, three different things an owner needs to know before trade
    time rather than after a run of refusals:

    - not configured — nothing to say; that is what every mandate to date was
      authored against;
    - configured **without** a registry — BLOCKING. A bundle is issued to a
      credential, so this combination refuses every proposal. It is a
      configuration error with a visible cause, and finding it here is cheaper
      than finding it in the journal;
    - configured **with** a registry — a NOTE, plus the bound. Every
      proposer must now cite a bundle this backend issued, so a proposer that
      has not been updated to ask for one will refuse at STAMP even though its
      credential is perfectly valid.
    """

    if evidence is None or not evidence.enabled:
        return []
    if not evidence.registry_configured:
        return [
            _finding(
                Severity.BLOCKING,
                "EVIDENCE_WITHOUT_REGISTRY",
                "AUTONOMY_EVIDENCE_BUNDLES is set with no AUTONOMY_PROPOSERS_FILE. An "
                "evidence bundle is issued TO a credential, so with no registry there is "
                "no author to issue to and every proposal refuses. Configure a proposer "
                "registry or unset evidence binding.",
            )
        ]
    return [
        _finding(
            Severity.NOTE,
            "EVIDENCE_BINDING_IN_FORCE",
            "Evidence binding is in force: every proposal must cite an evidence bundle "
            f"this backend issued to that proposer within the last {evidence.ttl_seconds:g}s, "
            "judged at the drain's clock. A proposer that does not request one will refuse "
            "at STAMP even with a valid credential. Note the honest bound: equality between "
            "the cited digest and the issued record catches accidental rendering drift, not "
            "a proposer that reasons on other text and cites the issued digest.",
        )
    ]


def _review_window(mandate: AutonomyMandate, *, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    if now >= mandate.expires_at:
        findings.append(
            _finding(
                Severity.BLOCKING,
                "expired",
                f"the window closed at {mandate.expires_at.isoformat()}; admission refuses an "
                "expired mandate regardless of restart_behavior, so this grants nothing now",
            )
        )
    elif now < mandate.effective_from:
        findings.append(
            _finding(
                Severity.IMPORTANT,
                "not-yet-effective",
                f"the window opens at {mandate.effective_from.isoformat()}, which is in the "
                "future; until then this mandate authorizes nothing",
            )
        )
    else:
        remaining = mandate.expires_at - now
        if remaining <= _EXPIRY_WARNING:
            findings.append(
                _finding(
                    Severity.IMPORTANT,
                    "expiring-soon",
                    f"expires in {_humanize(remaining)} at {mandate.expires_at.isoformat()}; "
                    "renewal is a fresh owner act, not an automatic extension",
                )
            )

    duration = mandate.expires_at - mandate.effective_from
    if mandate.mode in LIVE_AUTONOMY_MODES and duration > MAX_LIVE_MANDATE_DURATION * 0.9:
        findings.append(
            _finding(
                Severity.NOTE,
                "near-max-duration",
                f"a live mandate may run at most {MAX_LIVE_MANDATE_DURATION.days} days and this "
                f"one runs {duration.days}",
            )
        )
    return findings


def _review_account(
    mandate: AutonomyMandate, *, expected: str | None, fingerprint_source: str
) -> list[Finding]:
    if expected is None:
        return [
            _finding(
                Severity.NOTE,
                "account-check-skipped",
                "the account binding was NOT checked: no account id was available (pass "
                "--account-id, or set IB_ACCOUNT_ID). A mandate scoped to a different "
                "account boots inert with one ERROR line in the log",
            )
        ]
    if mandate.account_fingerprint != expected:
        return [
            _finding(
                Severity.BLOCKING,
                "account-mismatch",
                f"account_fingerprint {mandate.account_fingerprint[:12]}… does not match the "
                f"fingerprint of {fingerprint_source} ({expected[:12]}…); "
                "build_autonomy_runtime refuses this mandate and autonomy stays inert",
            )
        ]
    return [
        _finding(
            Severity.NOTE,
            "account-match",
            f"account_fingerprint matches {fingerprint_source}",
        )
    ]


def _review_pins(
    mandate: AutonomyMandate,
    *,
    ingress_pins: dict[str, str] | None,
    registry: RegistryPosture | None = None,
) -> list[Finding]:
    """Compare the mandate's version pins against the stamp proposals carry.

    Admission refuses on any mismatch (``VERSION_PIN_MISMATCH``), and it does so
    per decision — the mandate itself still loads and activates, so the failure
    looks like a model that proposes nothing rather than a document that is
    wrong. That is why this is worth catching at authoring time.

    Which stamp the pins must agree with depends on the posture (ADR-0023).
    With a proposer registry configured, provenance is stamped from whichever
    registration's credential authenticated — so the pins bind the grant to an
    **author**, and this compares them against every registration. Without a
    registry, the pins must agree with the static ingress identity, as before.
    """

    if registry is not None:
        return _review_pins_against_registry(mandate, registry)
    if ingress_pins is None:
        return [
            _finding(
                Severity.NOTE,
                "pins-check-skipped",
                "the version pins were NOT checked against the ingress stamp (the app plane "
                "could not be imported here); a mismatch refuses every proposal with "
                "VERSION_PIN_MISMATCH",
            )
        ]
    pins = mandate.versions
    mismatches = [
        f"{label}: mandate pins {getattr(pins, label)!r}, the ingress stamps {value!r}"
        for label, value in sorted(ingress_pins.items())
        if getattr(pins, label, None) != value
    ]
    if mismatches:
        return [
            _finding(
                Severity.BLOCKING,
                "version-pin-mismatch",
                "every proposal arriving over the ingress will be refused "
                "VERSION_PIN_MISMATCH — " + "; ".join(mismatches),
            )
        ]
    return [
        _finding(
            Severity.NOTE,
            "version-pins-agree",
            "version pins agree with the ingress stamp (agreement, not authorship: with no "
            "proposer registry configured the stamp names the boundary that accepted the "
            "proposal, not the model that made it — configure AUTONOMY_PROPOSERS_FILE to "
            "bind these pins to a registered author, ADR-0023)",
        )
    ]


def _review_pins_against_registry(
    mandate: AutonomyMandate, registry: RegistryPosture
) -> list[Finding]:
    """Which registered author, if any, this mandate's pins authorize."""

    if registry.proposers is None:
        return [
            _finding(
                Severity.IMPORTANT,
                "proposer-registry-invalid",
                f"AUTONOMY_PROPOSERS_FILE ({registry.path}) is configured but unreadable or "
                "invalid; the pins comparison is moot because EVERY proposal refuses at the "
                "route (503) and at STAMP until the file is repaired — this does not fall "
                "back to the tokened posture",
            )
        ]
    if not registry.proposers:
        return [
            _finding(
                Severity.IMPORTANT,
                "proposer-registry-empty",
                f"AUTONOMY_PROPOSERS_FILE ({registry.path}) is configured and registers "
                "nobody: a valid lockdown — every proposal refuses until someone is "
                "registered (`python -m chronos.cli proposer mint`)",
            )
        ]
    pins = mandate.versions
    matching = [
        entry
        for entry in registry.proposers
        if all(getattr(pins, label, None) == value for label, value in entry.pins.items())
    ]
    others = sorted(entry.proposer_id for entry in registry.proposers if entry not in matching)
    if not matching:
        return [
            _finding(
                Severity.BLOCKING,
                "version-pins-authorize-nobody",
                "the version pins match NO registered proposer "
                f"(registered: {', '.join(others)}); with a registry configured, provenance "
                "is stamped from the registration that authenticated (ADR-0023), so every "
                "attributed proposal will refuse VERSION_PIN_MISMATCH",
            )
        ]
    findings: list[Finding] = []
    stale = [entry.proposer_id for entry in matching if not entry.current]
    live = [entry.proposer_id for entry in matching if entry.current]
    if live:
        suffix = (
            f"; proposals from the other registrations ({', '.join(others)}) refuse "
            "VERSION_PIN_MISMATCH at admission, which is this grant choosing its author"
            if others
            else ""
        )
        findings.append(
            _finding(
                Severity.NOTE,
                "version-pins-authorize",
                f"the version pins authorize registered proposer(s): {', '.join(sorted(live))}"
                + suffix,
            )
        )
    if stale:
        findings.append(
            _finding(
                Severity.IMPORTANT,
                "authorized-proposer-stale",
                f"the pins match registration(s) {', '.join(sorted(stale))}, which are "
                "expired or disabled — their credentials no longer verify, so nothing can "
                "currently propose under this grant's pins from those registrations; "
                "renewal is an owner edit of the registry file",
            )
        )
    return findings


def _model_defaults(model: BaseModel) -> dict[str, Any]:
    return {name: field.default for name, field in type(model).model_fields.items()}


def _review_inert_fields(mandate: AutonomyMandate) -> list[Finding]:
    """Flag every limit the owner set that no deterministic module reads."""

    findings: list[Finding] = []
    for model_name, classified in LIMIT_ENFORCEMENT.items():
        section = _section_for(mandate, model_name)
        if section is None:
            continue
        defaults = _model_defaults(section)
        for field, status in classified.items():
            if status != INERT:
                continue
            value = getattr(section, field)
            if value == defaults.get(field):
                continue
            findings.append(
                _finding(
                    Severity.IMPORTANT,
                    "inert-limit-set",
                    f"{_attr_path(model_name)}.{field} = {_render_value(value)} constrains "
                    "nothing: no deterministic module reads it (see "
                    "chronos.autonomy.enforcement and the disclosure in "
                    "chronos.supervisor.admission)",
                )
            )

    for field, reason in INERT_SCOPE_FIELDS.items():
        value = getattr(mandate.scope, field, ())
        if value:
            findings.append(
                _finding(
                    Severity.IMPORTANT,
                    "inert-scope-set",
                    f"scope.{field} = {_render_value(value)} constrains nothing: {reason}",
                )
            )
    return findings


def _review_permissive_zeros(mandate: AutonomyMandate) -> list[Finding]:
    """Fields where zero is the *widest* setting, not the strictest.

    ``max_relative_spread`` is the one the mandate contract does not call out.
    Admission skips the spread comparison entirely when it is zero, so a
    ``max_`` field left at its default imposes no ceiling at all.
    """

    if mandate.mode not in SUBMITTING_AUTONOMY_MODES:
        return []
    if mandate.market_data.max_relative_spread > 0:
        return []
    return [
        _finding(
            Severity.IMPORTANT,
            "no-spread-ceiling",
            "market_data.max_relative_spread is 0, which admission reads as NO spread "
            "ceiling rather than the strictest one — the decision will be admitted on a "
            "quote of any width. Set it explicitly if you meant to bound the spread",
        )
    ]


def _review_discretion(mandate: AutonomyMandate) -> list[Finding]:
    """Spell out ADR-0017's inversion, and which ceilings it actually waives."""

    if not mandate.capital.model_discretion:
        return []
    unset = [
        name
        for name in (
            "max_order_notional_usd",
            "max_position_notional_usd",
            "max_gross_exposure_usd",
            "max_net_exposure_usd",
            "max_contracts_per_order",
            "max_shares_per_order",
            "max_leverage",
            "max_margin_utilization_pct",
        )
        if getattr(mandate.capital, name) <= 0
    ]
    waived = ", ".join(unset) if unset else "none — every ceiling is set and still binds"
    return [
        _finding(
            Severity.IMPORTANT,
            "model-discretion",
            "capital.model_discretion is granted (ADR-0017): an UNSET ceiling is no ceiling, "
            "and sizing is bounded by what the account can afford net of the floors. This "
            f"inverts deny-by-default for the unset ceilings, which here are: {waived}. The "
            "floors (min_cash_floor_usd, min_buying_power_usd) still bind — discretion over "
            "size is not discretion over the reserve",
        )
    ]


def _review_promotions(mandate: AutonomyMandate) -> list[Finding]:
    if not mandate.promotions:
        return []
    ceiling = promotion_rank(_SELF_DECLARED_RUNG_CEILING)
    above = [
        f"{promotion.asset_class.value}={promotion.level.value}"
        for promotion in mandate.promotions
        if promotion_rank(promotion.level) > ceiling
    ]
    if not above:
        return []
    return [
        _finding(
            Severity.IMPORTANT,
            "self-declared-rung",
            f"the rungs {', '.join(above)} are above {_SELF_DECLARED_RUNG_CEILING.value} and "
            "are self-declared: no code grants a rung from evidence and none demotes on a "
            "breach, so this file asserts them. The kernel enforces the LIMITS attached to a "
            "rung faithfully; it does not check the rung was earned (ADR-0024, proposed)",
        )
    ]


def _review_posture(mandate: AutonomyMandate) -> list[Finding]:
    findings: list[Finding] = []
    if mandate.restart_behavior.value == "RESUME_UNTIL_EXPIRY":
        findings.append(
            _finding(
                Severity.NOTE,
                "resumes-on-restart",
                "restart_behavior is RESUME_UNTIL_EXPIRY (ADR-0017): bringing the backend up "
                "re-arms this grant with no further owner action, until it expires",
            )
        )
    if mandate.mode in LIVE_AUTONOMY_MODES:
        findings.append(
            _finding(
                Severity.NOTE,
                "live-mode",
                "this is a LIVE-capable mandate, and a mandate alone enables nothing: the "
                "ADR-0009 nine-conjunct settings validation and the ten-gate live stack still "
                "apply. Note the two stop mechanisms fail in OPPOSITE directions — a missing "
                "platform halt file reads HALTED, a missing live kill-switch file reads "
                "DISENGAGED (docs/INCIDENT_RESPONSE.md)",
            )
        )
    return findings


# ------------------------------------------------------------------- rendering


def _section_for(mandate: AutonomyMandate, model_name: str) -> BaseModel | None:
    attribute = _attr_path(model_name)
    section = getattr(mandate, attribute, None)
    return section if isinstance(section, BaseModel) else None


def _attr_path(model_name: str) -> str:
    return {
        "CapitalLimits": "capital",
        "LossLimits": "loss",
        "ConcentrationLimits": "concentration",
        "ActivityLimits": "activity",
        "MarketDataRequirements": "market_data",
        "SessionPolicy": "sessions",
    }[model_name]


def _render_value(value: Any) -> str:
    if isinstance(value, tuple):
        return "(" + ", ".join(_render_value(item) for item in value) + ")"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, StrEnum):
        return value.value
    return str(value)


def _humanize(delta: timedelta) -> str:
    total_hours = int(delta.total_seconds() // 3600)
    days, hours = divmod(total_hours, 24)
    if days and hours:
        return f"{days}d {hours}h"
    if days:
        return f"{days}d"
    return f"{hours}h"


def _print_grant(mandate: AutonomyMandate) -> None:
    scope = mandate.scope
    print("GRANT")
    print(f"  mandate          {mandate.mandate_id} v{mandate.mandate_version}")
    print(f"  mode             {mandate.mode.value}")
    print(
        f"  window           {mandate.effective_from.isoformat()} → "
        f"{mandate.expires_at.isoformat()}"
    )
    print(f"  restart          {mandate.restart_behavior.value}")
    print(f"  account          {mandate.account_fingerprint[:12]}… (pseudonym)")
    promotions = (
        ", ".join(f"{item.asset_class.value}={item.level.value}" for item in mandate.promotions)
        or "none — every family has earned nothing"
    )
    print(f"  promotions       {promotions}")
    print("SCOPE")
    print(f"  asset classes    {_render_scope(scope.asset_classes)}")
    print(f"  symbols          {_render_scope(scope.symbols)}")
    print(f"  futures roots    {_render_scope(scope.futures_roots)}")
    print(f"  strategies       {_render_scope(scope.strategies)}")
    print(f"  order forms      {_render_scope(scope.order_forms)}")
    print(f"  data qualities   {_render_scope(mandate.market_data.permitted_data_qualities)}")


def _render_scope(values: Sequence[Any]) -> str:
    if not values:
        return "(empty — grants nothing)"
    rendered = [_render_value(item) for item in values]
    if len(rendered) <= 8:
        return ", ".join(rendered)
    return ", ".join(rendered[:8]) + f", … (+{len(rendered) - 8} more)"


def _print_enforcement(mandate: AutonomyMandate) -> None:
    """List the inert fields whether or not the owner set them.

    Findings only fire for inert fields that were *set*. This section exists so
    an owner can see the whole set before writing one, which is the difference
    between a lint and a disclosure.
    """

    print("NOT ENFORCED BY THE SUPERVISOR TODAY")
    printed = False
    for model_name, classified in LIMIT_ENFORCEMENT.items():
        inert = [field for field, status in classified.items() if status == INERT]
        if not inert:
            continue
        printed = True
        section = _section_for(mandate, model_name)
        defaults = _model_defaults(section) if section is not None else {}
        for field in inert:
            value = getattr(section, field, None) if section is not None else None
            marker = "  <- set in this mandate" if value != defaults.get(field) else ""
            print(f"  {_attr_path(model_name)}.{field}{marker}")
    for field in INERT_SCOPE_FIELDS:
        marker = "  <- set in this mandate" if getattr(mandate.scope, field, ()) else ""
        print(f"  scope.{field}{marker}")
        printed = True
    if not printed:  # pragma: no cover - defensive; the map is never empty today
        print("  (none)")


def _print_findings(findings: Iterable[Finding]) -> None:
    ordered = sorted(findings, key=lambda item: list(Severity).index(Severity(item.severity)))
    print("FINDINGS")
    empty = True
    for finding in ordered:
        empty = False
        print(f"  [{finding.severity.value}] {finding.code}")
        for line in _wrap(finding.message, width=88, indent=6):
            print(line)
    if empty:
        print("  (none)")


def _wrap(text: str, *, width: int, indent: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = " " * indent
    for word in words:
        candidate = word if current.strip() == "" else f"{current} {word}"
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = " " * indent + word
        else:
            current = candidate if current.strip() else current + word
    if current.strip():
        lines.append(current)
    return lines


# ------------------------------------------------------------------ resolution


def _resolve_expected_fingerprint(account_id: str | None) -> tuple[str | None, str]:
    """The account this machine would bind a mandate to, and where it came from.

    Settings can legitimately fail to construct (they are fail-closed), and this
    is a read-only aid, so a failure downgrades the check to *skipped* rather
    than aborting the report.
    """

    if account_id:
        return account_fingerprint(account_id), f"--account-id {_mask(account_id)}"
    try:
        from chronos.config.settings import get_settings

        configured = get_settings().ib_account_id.strip()
    except Exception:  # any settings failure means "unavailable", not "no mismatch"
        return None, ""
    if not configured:
        return None, ""
    return account_fingerprint(configured), f"IB_ACCOUNT_ID {_mask(configured)}"


def _mask(account_id: str) -> str:
    trimmed = account_id.strip()
    return f"…{trimmed[-3:]}" if len(trimmed) > 3 else "…"


def _resolve_ingress_pins() -> dict[str, str] | None:
    """The seven pins the ingress stamps, imported from the one place they live.

    Imported lazily and defensively: the app plane pulls in the order plane, and
    ``chronos.cli.main`` is asserted elsewhere to load no broker module at
    import time. A failure here is reported as a skipped check.
    """

    try:
        from chronos.api.autonomy_wiring import INGRESS_IDENTITY
    except Exception:  # an unavailable app plane means "unchecked", not "agrees"
        return None
    return {
        "provider": INGRESS_IDENTITY.provider,
        "model_id": INGRESS_IDENTITY.model_id,
        "model_version": INGRESS_IDENTITY.model_version,
        "prompt_version": INGRESS_IDENTITY.prompt_version,
        "tool_schema_version": INGRESS_IDENTITY.tool_schema_version,
        "decision_schema_version": INGRESS_IDENTITY.decision_schema_version,
        "policy_version": INGRESS_IDENTITY.policy_version,
    }


def _resolve_registry_posture(now: datetime) -> RegistryPosture | None:
    """What AUTONOMY_PROPOSERS_FILE resolves to, or None when not configured.

    Imported lazily for the same reason as :func:`_resolve_ingress_pins`: the
    supervisor package pulls planes this CLI is pinned not to load at import
    time. Any resolution failure short of "configured but invalid" reports as
    not-configured, which downgrades the check rather than inventing a result.
    """

    try:
        from chronos.config.settings import get_settings

        path = get_settings().autonomy_proposers_file
    except Exception:  # settings failure means "unavailable", not "unconfigured file"
        return None
    if path is None:
        return None
    try:
        from chronos.supervisor.proposers import load_proposer_registry
    except Exception:  # pragma: no cover - the module ships with this CLI
        return None
    loaded = load_proposer_registry(path)
    if loaded is None:
        return RegistryPosture(path=str(path), proposers=None)
    return RegistryPosture(
        path=str(path),
        proposers=tuple(
            RegisteredPins(
                proposer_id=entry.proposer_id,
                pins={
                    "provider": entry.provider,
                    "model_id": entry.model_id,
                    "model_version": entry.model_version,
                    "prompt_version": entry.prompt_version,
                    "tool_schema_version": entry.tool_schema_version,
                    "decision_schema_version": entry.decision_schema_version,
                    "policy_version": entry.policy_version,
                },
                current=entry.is_current(now),
            )
            for entry in loaded.registry.proposers
        ),
    )


def _resolve_evidence_posture() -> EvidencePosture | None:
    """What AUTONOMY_EVIDENCE_BUNDLES resolves to, or None when unavailable.

    Lazily imported for the same reason as the registry posture. A settings
    failure reports as not-configured, which downgrades this check rather than
    inventing a posture — and the downgrade is safe here because the
    pre-ADR-0028 posture is what an unannotated mandate already assumes.
    """

    try:
        from chronos.config.settings import get_settings

        settings = get_settings()
    except Exception:
        return None
    return EvidencePosture(
        enabled=settings.autonomy_evidence_bundles,
        registry_configured=settings.autonomy_proposers_file is not None,
        ttl_seconds=settings.autonomy_evidence_ttl_seconds,
    )


def _default_mandate_path() -> Path | None:
    try:
        from chronos.config.settings import get_settings

        return get_settings().autonomy_mandate_file
    except Exception:  # see _resolve_expected_fingerprint
        return None


# --------------------------------------------------------------------- command


def cmd_mandate_check(args: argparse.Namespace) -> int:
    """Validate and describe a mandate file. Exit 1 on BLOCKING, 0 otherwise."""

    path: Path | None = args.file or _default_mandate_path()
    if path is None:
        print(
            "no mandate file given and AUTONOMY_MANDATE_FILE is unset; pass --file. "
            "A backend with no mandate file configured boots inert, which is correct."
        )
        return 1
    try:
        raw = path.read_bytes()
    except OSError as error:
        print(f"cannot read {path}: {error}")
        print("the backend treats an unreadable mandate file as NO mandate, and boots inert.")
        return 1

    print(f"MANDATE FILE       {path}")
    result = load_mandate_text(raw)
    if result.mandate is None:
        print("STATUS             INVALID — the backend would boot inert with this file")
        print("VALIDATION ERRORS")
        for line in result.errors:
            print(f"  {line}")
        return 1

    mandate = result.mandate
    expected, source = _resolve_expected_fingerprint(args.account_id)
    review_now = args.now or utc_now()
    findings = review_mandate(
        mandate,
        now=review_now,
        expected_fingerprint=expected,
        fingerprint_source=source,
        ingress_pins=None if args.no_pin_check else _resolve_ingress_pins(),
        registry=None if args.no_pin_check else _resolve_registry_posture(review_now),
        # Not gated on --no-pin-check: the evidence posture is not a pin
        # comparison, it is whether this backend will demand a cited bundle at
        # all — which an owner reviewing a mandate needs to know regardless.
        evidence=_resolve_evidence_posture(),
    )
    print("STATUS             VALID — the document parses and its invariants hold")
    print()
    _print_grant(mandate)
    print()
    _print_enforcement(mandate)
    print()
    _print_findings(findings)

    blocking = [item for item in findings if item.severity is Severity.BLOCKING]
    important = [item for item in findings if item.severity is Severity.IMPORTANT]
    print()
    print(
        f"SUMMARY            {len(blocking)} blocking, {len(important)} important, "
        f"{len(findings) - len(blocking) - len(important)} notes"
    )
    print(
        "This is a description, not an authorization. Nothing here grants, activates, or "
        "renews anything."
    )
    if blocking:
        return 1
    if important and args.strict:
        return 1
    return 0


def cmd_mandate_template(args: argparse.Namespace) -> int:
    """Print a *non-submitting* SHADOW skeleton to stdout, with every field named.

    Deliberately SHADOW and deliberately printed rather than written: a template
    that produced a submitting mandate, or that wrote itself to the configured
    path, would be this tool authoring authority. The owner copies what they
    want, fills in their own account and window, and takes responsibility for
    the result.
    """

    skeleton: dict[str, Any] = {
        "mandate_id": "REPLACE-ME",
        "mandate_version": 1,
        "account_fingerprint": "<64 lowercase hex chars — see `chronos mandate fingerprint`>",
        "mode": "SHADOW",
        "promotions": [{"asset_class": "EQUITY", "level": "SHADOW"}],
        "effective_from": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-02-01T00:00:00+00:00",
        "restart_behavior": "RESUME_UNTIL_EXPIRY",
        "versions": {
            "provider": "external-worker",
            "model_id": "ingress",
            "model_version": "1",
            "prompt_version": "1",
            "tool_schema_version": "1",
            "decision_schema_version": "1",
            "policy_version": "1",
        },
        "scope": {
            "asset_classes": ["EQUITY"],
            "symbols": ["SPY"],
            "strategies": ["LONG_EQUITY"],
            "order_forms": ["LIMIT"],
        },
        "market_data": {
            "max_quote_age_seconds": "5",
            "permitted_data_qualities": ["LIVE", "DELAYED"],
            "max_relative_spread": "0.02",
        },
        "capital": {
            "min_cash_floor_usd": "0",
            "min_buying_power_usd": "0",
        },
        "owner_authorization_ref": "REPLACE-ME",
        "authored_at": "2026-01-01T00:00:00+00:00",
        "note": "",
    }
    print(json.dumps(skeleton, indent=2))
    print()
    print("# SHADOW on purpose. A submitting mode additionally requires positive floors and")
    print("# ceilings, and a promotion rung per asset class that no code can grant you.")
    print('# The "versions" block above matches the STATIC ingress stamp — correct only')
    print("# while AUTONOMY_PROPOSERS_FILE is unset. With a proposer registry configured,")
    print("# provenance is stamped from the registration that authenticated (ADR-0023), so")
    print("# these pins must equal a registered proposer's values or every proposal refuses")
    print("# VERSION_PIN_MISMATCH. `mandate check` compares against whichever applies.")
    print("# Check it before use:  python -m chronos.cli mandate check --file <path>")
    return 0


def cmd_mandate_fingerprint(args: argparse.Namespace) -> int:
    """Print the pseudonym a raw account id maps to. No account id is stored."""

    account_id: str | None = args.account_id
    source = "--account-id"
    if not account_id:
        fingerprint, source = _resolve_expected_fingerprint(None)
        if fingerprint is None:
            print("no account id available; pass --account-id or set IB_ACCOUNT_ID")
            return 1
        print(fingerprint)
        print(f"# derived from {source}", flush=True)
        return 0
    print(account_fingerprint(account_id))
    print(f"# derived from {source} {_mask(account_id)}")
    return 0


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must be timezone-aware, e.g. 2026-08-08T14:00:00Z")
    return parsed


def add_mandate_commands(sub: Any) -> None:
    """Register ``mandate check|template|fingerprint`` on the operator CLI."""

    mandate = sub.add_parser(
        "mandate",
        help="inspect an autonomy mandate (read-only; authors and grants nothing)",
    )
    mandate_sub = mandate.add_subparsers(dest="mandate_command", required=True)

    check = mandate_sub.add_parser(
        "check", help="validate a mandate file and report what it actually authorizes"
    )
    check.add_argument(
        "--file",
        type=Path,
        default=None,
        help="mandate JSON path (defaults to AUTONOMY_MANDATE_FILE)",
    )
    check.add_argument(
        "--account-id",
        default=None,
        help="cross-check the account binding (defaults to IB_ACCOUNT_ID; never stored)",
    )
    check.add_argument(
        "--now",
        type=_parse_instant,
        default=None,
        help="evaluate the window at this aware instant instead of now",
    )
    check.add_argument(
        "--no-pin-check",
        action="store_true",
        help="skip the ingress version-pin comparison (reported as skipped)",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on IMPORTANT findings too, not only BLOCKING ones",
    )
    check.set_defaults(func=cmd_mandate_check)

    template = mandate_sub.add_parser(
        "template", help="print a SHADOW mandate skeleton to stdout (writes no file)"
    )
    template.set_defaults(func=cmd_mandate_template)

    fingerprint = mandate_sub.add_parser(
        "fingerprint", help="print the pseudonym for an account id"
    )
    fingerprint.add_argument("--account-id", default=None)
    fingerprint.set_defaults(func=cmd_mandate_fingerprint)

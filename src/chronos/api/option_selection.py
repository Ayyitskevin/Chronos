"""Read-only app-plane evidence acquisition for autonomous option selection.

The broad broker object remains here, outside the pure selector.  This module
uses only qualification/market-data reads and returns an immutable receipt; it
has no order intent, preview, confirmation, mutation, or submission surface.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from chronos.broker.base import BrokerDataError, OptionChainResponse
from chronos.broker.market_data import (
    ManagedQuote,
    MarketDataCancellationError,
    MarketDataPacingExhaustedError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
    OptionChainSnapshot,
)
from chronos.config.limits import (
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_OPTION_SELECTION_ACQUISITION_SECONDS,
)
from chronos.domain.models import (
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    OptionDeliverableFacts,
    OptionMarketRule,
    UnderlyingContract,
)
from chronos.runtime import AppRuntime
from chronos.supervisor.option_selection import (
    BROKER_RESOLVER_VERSION,
    CANONICALIZATION_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    PROMOTION_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
    RESOLVER_VERSION,
    SCORING_VERSION,
    CandidateAcquisitionPlan,
    CandidateEvidence,
    ObservedDeliverableEvidence,
    ObservedMarketRuleEvidence,
    ObservedOptionQuoteEvidence,
    OptionResolverPromotionArtifact,
    OptionSelectionEvidence,
    OptionSelectionPolicy,
    OptionSelectionReceipt,
    OptionSelectionRequest,
    SelectionRejection,
    SelectionRejectionCode,
    build_candidate_acquisition_plan,
    canonical_digest,
    failure_evidence,
    select_option,
)
from chronos.utils.identifiers import normalize_account_fingerprint
from chronos.utils.time import utc_now

_logger = logging.getLogger("chronos.api.option_selection")

_ZERO_DIGEST = "0" * 64
_ARITHMETIC_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN)
_LIVE_AUTONOMY_MODES = frozenset({"CANARY_LIVE_AUTONOMOUS", "LIVE_AUTONOMOUS"})


@dataclass(frozen=True, slots=True)
class LoadedOptionPromotion:
    artifact: OptionResolverPromotionArtifact
    digest: str


@dataclass(frozen=True, slots=True)
class OptionResolution:
    receipt: OptionSelectionReceipt


class _EvidenceAcquisitionFailure(RuntimeError):
    """Carries the partial facts already observed when a later read fails."""

    def __init__(self, evidence: OptionSelectionEvidence) -> None:
        super().__init__("option evidence acquisition failed closed")
        self.evidence = evidence


@dataclass(frozen=True, slots=True)
class _ObservedOptionQuote:
    """One partial quote observation retained after an exact-set failure."""

    quote: MarketQuote
    fetched_at: datetime
    observed_at: datetime


def load_option_resolver_promotion(path: Path) -> LoadedOptionPromotion | None:
    """Read and validate an owner artifact; never create or modify one."""

    try:
        raw = path.read_bytes()
        artifact = OptionResolverPromotionArtifact.model_validate_json(raw)
    except (OSError, ValueError):
        _logger.exception("Option resolver promotion is unreadable or invalid")
        return None
    return LoadedOptionPromotion(artifact=artifact, digest=hashlib.sha256(raw).hexdigest())


def resolver_compatibility_digest() -> str:
    """Hash every shipped runtime Python source that live promotion binds."""

    package_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in _runtime_source_files(package_root):
        path = package_root / relative
        raw = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_source_files(package_root: Path) -> tuple[str, ...]:
    """Return a stable, conservative manifest of all shipped Python authority."""

    return tuple(
        sorted(
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*.py")
            if path.is_file()
        )
    )


class AutonomousOptionSelectionService:
    """Acquire one complete option snapshot and run the pure resolver."""

    def __init__(
        self,
        runtime: AppRuntime,
        *,
        autonomy_mode: str,
        mandate_digest: str,
        account_fingerprint: str,
    ) -> None:
        self._runtime = runtime
        self._autonomy_mode = autonomy_mode
        self._mandate_digest = mandate_digest
        self._account_fingerprint = normalize_account_fingerprint(account_fingerprint)
        try:
            self._compatibility_digest = resolver_compatibility_digest()
        except OSError:
            _logger.exception("Could not hash material option resolver sources")
            self._compatibility_digest = _ZERO_DIGEST

    def resolve(
        self,
        *,
        request: OptionSelectionRequest,
        policy: OptionSelectionPolicy,
        evaluated_at: datetime | None = None,
    ) -> OptionResolution:
        moment = evaluated_at or utc_now()

        loaded_promotion = (
            self._load_promotion() if self._autonomy_mode in _LIVE_AUTONOMY_MODES else None
        )
        activation, promotion_digest = self._activation(moment, policy, loaded_promotion)
        if activation is not None:
            code, detail = activation
            evidence = failure_evidence(
                observed_at=moment,
                source="option-activation-gate",
                code=code,
                detail=detail,
            )
            return OptionResolution(
                receipt=select_option(
                    request=request,
                    policy=policy,
                    evidence=evidence,
                    mandate_digest=self._mandate_digest,
                    resolver_compatibility_digest=self._compatibility_digest,
                    promotion_digest=promotion_digest,
                )
            )

        try:
            evidence = self._runtime.connection.run(
                self._acquire(request, policy),
                timeout=MAX_OPTION_SELECTION_ACQUISITION_SECONDS,
            )
        except _EvidenceAcquisitionFailure as error:
            evidence = error.evidence
            code = evidence.acquisition_rejections[0].code
            _logger.warning(
                "Autonomous option evidence acquisition failed closed (%s)",
                code.value,
                extra={"event": "option_evidence_partial", "rejection": code.value},
            )
        except Exception as error:
            code = _acquisition_code(error)
            _logger.exception(
                "Autonomous option evidence acquisition failed (%s)",
                type(error).__name__,
            )
            evidence = failure_evidence(
                observed_at=utc_now(),
                source=_source_name(self._runtime),
                code=code,
                detail=f"read-only acquisition failed with {type(error).__name__}",
            )
        post_activation = self._post_acquisition_activation(
            evidence.observed_at,
            policy,
            loaded_promotion,
        )
        if post_activation is not None:
            code, detail = post_activation
            evidence = evidence.model_copy(
                update={
                    "complete": False,
                    "acquisition_rejections": (
                        *evidence.acquisition_rejections,
                        SelectionRejection(code=code, detail=detail),
                    ),
                }
            )
        return OptionResolution(
            receipt=select_option(
                request=request,
                policy=policy,
                evidence=evidence,
                mandate_digest=self._mandate_digest,
                resolver_compatibility_digest=self._compatibility_digest,
                promotion_digest=promotion_digest,
            )
        )

    def refusal_for_exception(
        self,
        request: OptionSelectionRequest,
        policy: OptionSelectionPolicy,
        error: BaseException,
    ) -> OptionResolution:
        """Convert an unexpected option-resolution failure into a canonical refusal."""

        observed_at = utc_now()
        loaded = self._load_promotion() if self._autonomy_mode in _LIVE_AUTONOMY_MODES else None
        evidence = failure_evidence(
            observed_at=observed_at,
            source="option-resolver-exception-guard",
            code=SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
            detail=f"option resolver failed with {type(error).__name__}",
        )
        return OptionResolution(
            receipt=select_option(
                request=request,
                policy=policy,
                evidence=evidence,
                mandate_digest=self._mandate_digest,
                resolver_compatibility_digest=self._compatibility_digest,
                promotion_digest=loaded.digest if loaded is not None else None,
            )
        )

    def _activation(
        self,
        now: datetime,
        policy: OptionSelectionPolicy,
        loaded: LoadedOptionPromotion | None,
    ) -> tuple[tuple[SelectionRejectionCode, str] | None, str | None]:
        settings = self._runtime.settings
        if not settings.enable_autonomy_option_selection:
            return (
                (
                    SelectionRejectionCode.FEATURE_DISABLED,
                    "ENABLE_AUTONOMY_OPTION_SELECTION is false",
                ),
                None,
            )
        if self._compatibility_digest == _ZERO_DIGEST:
            return (
                (
                    SelectionRejectionCode.PROMOTION_MISMATCH,
                    "material resolver sources could not be hashed",
                ),
                None,
            )
        if self._autonomy_mode not in _LIVE_AUTONOMY_MODES:
            return None, None
        if loaded is None:
            return (
                (
                    SelectionRejectionCode.PROMOTION_REQUIRED,
                    "CANARY/LIVE autonomous options require an owner-created resolver promotion",
                ),
                None,
            )
        mismatches = _promotion_mismatches(
            loaded.artifact,
            account_fingerprint=self._account_fingerprint,
            autonomy_mode=self._autonomy_mode,
            mandate_digest=self._mandate_digest,
            policy_digest=canonical_digest(policy),
            compatibility_digest=self._compatibility_digest,
            now=now,
        )
        if mismatches:
            return (
                (
                    SelectionRejectionCode.PROMOTION_MISMATCH,
                    "resolver promotion mismatch: " + ", ".join(mismatches),
                ),
                loaded.digest,
            )
        return None, loaded.digest

    def _post_acquisition_activation(
        self,
        now: datetime,
        policy: OptionSelectionPolicy,
        initial: LoadedOptionPromotion | None,
    ) -> tuple[SelectionRejectionCode, str] | None:
        """Recheck implementation and owner artifact after the bounded reads finish."""

        try:
            current_compatibility = resolver_compatibility_digest()
        except OSError:
            current_compatibility = _ZERO_DIGEST
        if current_compatibility != self._compatibility_digest:
            return (
                SelectionRejectionCode.PROMOTION_MISMATCH,
                "material resolver sources changed during evidence acquisition",
            )
        if self._autonomy_mode not in _LIVE_AUTONOMY_MODES:
            return None
        current = self._load_promotion()
        if initial is None or current is None or current.digest != initial.digest:
            return (
                SelectionRejectionCode.PROMOTION_MISMATCH,
                "owner resolver promotion changed or disappeared during evidence acquisition",
            )
        activation, _ = self._activation(now, policy, current)
        return activation

    def validate_handoff(self, receipt: OptionSelectionReceipt) -> str:
        """Revalidate live owner authority immediately before the order-plane handoff."""

        if self._autonomy_mode not in _LIVE_AUTONOMY_MODES:
            return ""
        try:
            current_compatibility = resolver_compatibility_digest()
        except OSError:
            return "material resolver sources cannot be hashed at handoff"
        if (
            current_compatibility != self._compatibility_digest
            or receipt.resolver_compatibility_digest != current_compatibility
        ):
            return "material resolver sources differ at handoff"
        current = self._load_promotion()
        if current is None or current.digest != receipt.promotion_digest:
            return "owner resolver promotion changed or disappeared before handoff"
        mismatches = _promotion_mismatches(
            current.artifact,
            account_fingerprint=self._account_fingerprint,
            autonomy_mode=self._autonomy_mode,
            mandate_digest=self._mandate_digest,
            policy_digest=receipt.policy_digest,
            compatibility_digest=current_compatibility,
            now=utc_now(),
        )
        return (
            ""
            if not mismatches
            else "resolver promotion invalid at handoff: " + ", ".join(mismatches)
        )

    def _load_promotion(self) -> LoadedOptionPromotion | None:
        path = self._runtime.settings.autonomy_option_resolver_promotion_file
        return load_option_resolver_promotion(path) if path is not None else None

    async def _acquire(
        self,
        request: OptionSelectionRequest,
        policy: OptionSelectionPolicy,
    ) -> OptionSelectionEvidence:
        runtime = self._runtime
        try:
            underlying = await runtime.market_data.qualify_underlying(request.symbol)
        except Exception as error:
            partial_underlyings = _observed_values(error, UnderlyingContract)
            if partial_underlyings:
                raise _partial_failure(
                    runtime,
                    error,
                    underlying=partial_underlyings[0],
                    underlying_qualified_at=_error_observed_at(error),
                ) from error
            raise
        underlying_qualified_at = utc_now()
        try:
            underlying_quote = await runtime.market_data.underlying_quote(
                underlying,
                force_refresh=True,
            )
        except Exception as error:
            partial_quotes = _observed_option_quotes(error)
            # Preserve the observed value even when its identity is the reason
            # the exact-set read failed. The pure selector will record the
            # mismatch; dropping it would erase the contaminating broker fact.
            partial_underlying_quote = partial_quotes[0] if partial_quotes else None
            raise _partial_failure(
                runtime,
                error,
                underlying=underlying,
                underlying_qualified_at=underlying_qualified_at,
                underlying_quote_observation=partial_underlying_quote,
            ) from error
        try:
            chain = await runtime.market_data.option_chain_parameters(
                underlying,
                force_refresh=True,
            )
        except Exception as error:
            partial_chain = _observed_chain(error)
            raise _partial_failure(
                runtime,
                error,
                underlying=underlying,
                underlying_qualified_at=underlying_qualified_at,
                underlying_quote=underlying_quote,
                chain=partial_chain,
            ) from error
        selected_chain = _single_acquisition_chain(chain.parameters, underlying.con_id, policy)
        acquisition_plan: CandidateAcquisitionPlan | None = None
        contracts: tuple[OptionContract, ...] = ()
        if selected_chain is not None:
            quote = underlying_quote.quote
            if quote.bid is not None and quote.ask is not None and quote.bid > 0:
                with localcontext(_ARITHMETIC_CONTEXT):
                    spot = (quote.bid + quote.ask) / Decimal(2)
                acquisition_plan = build_candidate_acquisition_plan(
                    request=request,
                    policy=policy,
                    chain=selected_chain,
                    spot=spot,
                    observed_at=chain.observed_at,
                )
                try:
                    contracts = await runtime.market_data.qualify_option_contracts(
                        acquisition_plan.requested_specs,
                        force_refresh=True,
                    )
                except Exception as error:
                    partial_contracts = _observed_values(error, OptionContract)
                    raise _partial_failure(
                        runtime,
                        error,
                        underlying=underlying,
                        underlying_qualified_at=underlying_qualified_at,
                        underlying_quote=underlying_quote,
                        chain=chain,
                        acquisition_plan=acquisition_plan,
                        contracts=partial_contracts,
                        qualified_at=(_error_observed_at(error) if partial_contracts else None),
                    ) from error
        qualified_at = utc_now()
        try:
            rules = await runtime.market_data.option_market_rules(contracts)
        except Exception as error:
            partial_rules = _observed_values(error, OptionMarketRule)
            raise _partial_failure(
                runtime,
                error,
                underlying=underlying,
                underlying_qualified_at=underlying_qualified_at,
                underlying_quote=underlying_quote,
                chain=chain,
                acquisition_plan=acquisition_plan,
                contracts=contracts,
                qualified_at=qualified_at,
                rules=partial_rules,
                market_rule_at=(_error_observed_at(error) if partial_rules else None),
            ) from error
        market_rule_at = utc_now()
        try:
            deliverables = await runtime.market_data.option_deliverable_facts(contracts)
        except Exception as error:
            partial_deliverables = _observed_values(error, OptionDeliverableFacts)
            raise _partial_failure(
                runtime,
                error,
                underlying=underlying,
                underlying_qualified_at=underlying_qualified_at,
                underlying_quote=underlying_quote,
                chain=chain,
                acquisition_plan=acquisition_plan,
                contracts=contracts,
                qualified_at=qualified_at,
                rules=rules,
                market_rule_at=market_rule_at,
                deliverables=partial_deliverables,
                deliverable_at=(_error_observed_at(error) if partial_deliverables else None),
            ) from error
        deliverable_at = utc_now()
        try:
            managed_quotes = await runtime.market_data.option_quotes(
                contracts,
                force_refresh=True,
            )
        except Exception as error:
            partial_quotes = _observed_option_quotes(error)
            raise _partial_failure(
                runtime,
                error,
                underlying=underlying,
                underlying_qualified_at=underlying_qualified_at,
                underlying_quote=underlying_quote,
                chain=chain,
                acquisition_plan=acquisition_plan,
                contracts=contracts,
                qualified_at=qualified_at,
                rules=rules,
                market_rule_at=market_rule_at,
                deliverables=deliverables,
                deliverable_at=deliverable_at,
                option_quotes=partial_quotes,
            ) from error
        observed_at = utc_now()
        rule_by_id = {item.con_id: item for item in rules}
        deliverable_by_id = {item.con_id: item for item in deliverables}
        quote_by_id = {item.quote.contract.con_id: item for item in managed_quotes}
        candidates = tuple(
            CandidateEvidence(
                contract=contract,
                qualified_at=qualified_at,
                quote=quote_by_id[contract.con_id].quote,
                quote_fetched_at=quote_by_id[contract.con_id].fetched_at,
                quote_observed_at=quote_by_id[contract.con_id].observed_at,
                market_rule=rule_by_id[contract.con_id],
                market_rule_observed_at=market_rule_at,
                deliverable=deliverable_by_id[contract.con_id],
                deliverable_observed_at=deliverable_at,
            )
            for contract in contracts
        )
        return OptionSelectionEvidence(
            source=_source_name(runtime),
            observed_at=observed_at,
            complete=chain.complete,
            truncated=chain.truncated,
            completion_marker=chain.completion_marker,
            underlying=underlying,
            underlying_qualified_at=underlying_qualified_at,
            underlying_quote=underlying_quote.quote,
            underlying_quote_fetched_at=underlying_quote.fetched_at,
            underlying_quote_observed_at=underlying_quote.observed_at,
            chain_parameters=chain.parameters,
            chain_fetched_at=chain.fetched_at,
            chain_observed_at=chain.observed_at,
            chain_completion_observed_at=chain.completion_observed_at,
            chain_completion_source=chain.completion_source,
            acquisition_plan=acquisition_plan,
            observed_market_rules=tuple(
                ObservedMarketRuleEvidence(market_rule=item, observed_at=market_rule_at)
                for item in rules
            ),
            observed_deliverables=tuple(
                ObservedDeliverableEvidence(deliverable=item, observed_at=deliverable_at)
                for item in deliverables
            ),
            observed_option_quotes=tuple(
                ObservedOptionQuoteEvidence(
                    quote=item.quote,
                    fetched_at=item.fetched_at,
                    observed_at=item.observed_at,
                )
                for item in managed_quotes
            ),
            candidates=candidates,
        )


def _single_acquisition_chain(
    rows: tuple[OptionChainParameters, ...],
    underlying_con_id: int,
    policy: OptionSelectionPolicy,
) -> OptionChainParameters | None:
    candidates = tuple(
        row
        for row in rows
        if row.underlying_con_id == underlying_con_id
        and row.exchange in policy.permitted_exchanges
        and row.trading_class in policy.permitted_trading_classes
        and row.multiplier == policy.required_multiplier
    )
    return candidates[0] if len(candidates) == 1 else None


def _source_name(runtime: AppRuntime) -> str:
    settings = runtime.settings
    return f"{settings.broker_mode.value}:{settings.broker_adapter.value}:{BROKER_RESOLVER_VERSION}"


def _acquisition_code(error: Exception) -> SelectionRejectionCode:
    if isinstance(error, MarketDataPacingExhaustedError):
        return SelectionRejectionCode.PACING_EXHAUSTED
    if isinstance(error, MarketDataPermissionError):
        return SelectionRejectionCode.DATA_PERMISSION_DENIED
    if isinstance(error, MarketDataCancellationError):
        return SelectionRejectionCode.DATA_CLEANUP_FAILED
    if isinstance(error, (MarketDataUnavailableError, BrokerDataError, TimeoutError)):
        return SelectionRejectionCode.EVIDENCE_UNAVAILABLE
    return SelectionRejectionCode.EVIDENCE_UNAVAILABLE


def _observed_values[ObservedT](
    error: BaseException,
    expected_type: type[ObservedT],
) -> tuple[ObservedT, ...]:
    """Return only bounded, type-correct observations attached by the read seam."""

    return tuple(
        item for item in getattr(error, "observed_values", ()) if isinstance(item, expected_type)
    )[:MAX_CANDIDATE_REQUEST_CONTRACTS]


def _error_observed_at(error: BaseException) -> datetime:
    observed_at = getattr(error, "observed_at", None)
    return observed_at if isinstance(observed_at, datetime) else utc_now()


def _observed_chain(error: BaseException) -> OptionChainSnapshot | None:
    response = getattr(error, "chain_response", None)
    if not isinstance(response, OptionChainResponse):
        return None
    observed_at = _error_observed_at(error)
    return OptionChainSnapshot(
        parameters=response.parameters,
        fetched_at=response.observed_at,
        observed_at=observed_at,
        from_cache=False,
        fresh=response.observed_at <= observed_at,
        complete=response.complete,
        truncated=response.truncated,
        completion_marker=response.completion_marker,
        completion_observed_at=response.observed_at,
        completion_source=response.source,
    )


def _observed_option_quotes(error: BaseException) -> tuple[_ObservedOptionQuote, ...]:
    observed_at = _error_observed_at(error)
    retained: list[_ObservedOptionQuote] = []
    for item in getattr(error, "observed_values", ()):
        if isinstance(item, ManagedQuote):
            retained.append(
                _ObservedOptionQuote(
                    quote=item.quote,
                    fetched_at=item.fetched_at,
                    observed_at=item.observed_at,
                )
            )
        elif isinstance(item, MarketQuote):
            retained.append(
                _ObservedOptionQuote(
                    quote=item,
                    fetched_at=observed_at,
                    observed_at=observed_at,
                )
            )
    return tuple(
        sorted(
            retained,
            key=lambda item: (
                item.quote.contract.con_id,
                item.quote.timestamp,
                item.quote.bid if item.quote.bid is not None else Decimal("-1"),
                item.quote.ask if item.quote.ask is not None else Decimal("-1"),
                canonical_digest(item.quote),
            ),
        )[:MAX_CANDIDATE_REQUEST_CONTRACTS]
    )


def _acquisition_failure_detail(error: BaseException) -> str:
    detail = f"read-only acquisition failed with {type(error).__name__}"
    contract_ids = tuple(
        sorted(
            {
                item
                for item in getattr(error, "contract_ids", ())
                if isinstance(item, int) and item > 0
            }
        )
    )
    if contract_ids:
        detail += "; affected contract ids: " + ",".join(map(str, contract_ids))
    return detail


def _partial_failure(
    runtime: AppRuntime,
    error: Exception,
    *,
    underlying: UnderlyingContract,
    underlying_qualified_at: datetime,
    underlying_quote: ManagedQuote | None = None,
    underlying_quote_observation: _ObservedOptionQuote | None = None,
    chain: OptionChainSnapshot | None = None,
    acquisition_plan: CandidateAcquisitionPlan | None = None,
    contracts: tuple[OptionContract, ...] = (),
    qualified_at: datetime | None = None,
    rules: tuple[OptionMarketRule, ...] = (),
    market_rule_at: datetime | None = None,
    deliverables: tuple[OptionDeliverableFacts, ...] = (),
    deliverable_at: datetime | None = None,
    option_quotes: tuple[_ObservedOptionQuote, ...] = (),
) -> _EvidenceAcquisitionFailure:
    """Preserve every fact already read while marking the aggregate partial."""

    observed_at = utc_now()
    if contracts and qualified_at is None:
        raise RuntimeError("qualified contracts require an observation timestamp")
    retained_contracts = list(contracts)
    rule_by_id: dict[int, OptionMarketRule] = {}
    for observed_rule in sorted(
        rules,
        key=lambda value: (value.con_id, canonical_digest(value)),
    ):
        rule_by_id.setdefault(observed_rule.con_id, observed_rule)
    deliverable_by_id: dict[int, OptionDeliverableFacts] = {}
    for observed_deliverable in sorted(
        deliverables,
        key=lambda value: (value.con_id, canonical_digest(value)),
    ):
        deliverable_by_id.setdefault(observed_deliverable.con_id, observed_deliverable)
    quote_by_id: dict[int, _ObservedOptionQuote] = {}
    for observed_quote in sorted(
        option_quotes,
        key=lambda value: (
            value.quote.contract.con_id,
            value.quote.timestamp,
            canonical_digest(value.quote),
        ),
    ):
        quote_by_id.setdefault(observed_quote.quote.contract.con_id, observed_quote)
    candidates = tuple(
        CandidateEvidence(
            contract=contract,
            qualified_at=qualified_at or observed_at,
            quote=(quote_by_id[contract.con_id].quote if contract.con_id in quote_by_id else None),
            quote_fetched_at=(
                quote_by_id[contract.con_id].fetched_at if contract.con_id in quote_by_id else None
            ),
            quote_observed_at=(
                quote_by_id[contract.con_id].observed_at if contract.con_id in quote_by_id else None
            ),
            market_rule=rule_by_id.get(contract.con_id),
            market_rule_observed_at=(market_rule_at if contract.con_id in rule_by_id else None),
            deliverable=deliverable_by_id.get(contract.con_id),
            deliverable_observed_at=(
                deliverable_at if contract.con_id in deliverable_by_id else None
            ),
        )
        for contract in retained_contracts
    )
    code = _acquisition_code(error)
    evidence = OptionSelectionEvidence(
        source=_source_name(runtime),
        observed_at=observed_at,
        complete=False,
        truncated=(
            (chain.truncated if chain is not None else False)
            or bool(getattr(error, "truncated", False))
        ),
        completion_marker=chain.completion_marker if chain is not None else "",
        acquisition_rejections=(
            SelectionRejection(
                code=code,
                detail=_acquisition_failure_detail(error),
            ),
        ),
        underlying=underlying,
        underlying_qualified_at=underlying_qualified_at,
        underlying_quote=(
            underlying_quote.quote
            if underlying_quote is not None
            else (
                underlying_quote_observation.quote
                if underlying_quote_observation is not None
                else None
            )
        ),
        underlying_quote_fetched_at=(
            underlying_quote.fetched_at
            if underlying_quote is not None
            else (
                underlying_quote_observation.fetched_at
                if underlying_quote_observation is not None
                else None
            )
        ),
        underlying_quote_observed_at=(
            underlying_quote.observed_at
            if underlying_quote is not None
            else (
                underlying_quote_observation.observed_at
                if underlying_quote_observation is not None
                else None
            )
        ),
        chain_parameters=chain.parameters if chain is not None else (),
        chain_fetched_at=chain.fetched_at if chain is not None else None,
        chain_observed_at=chain.observed_at if chain is not None else None,
        chain_completion_observed_at=(chain.completion_observed_at if chain is not None else None),
        chain_completion_source=chain.completion_source if chain is not None else None,
        acquisition_plan=acquisition_plan,
        observed_market_rules=tuple(
            ObservedMarketRuleEvidence(
                market_rule=item,
                observed_at=market_rule_at or observed_at,
            )
            for item in rules
        ),
        observed_deliverables=tuple(
            ObservedDeliverableEvidence(
                deliverable=item,
                observed_at=deliverable_at or observed_at,
            )
            for item in deliverables
        ),
        observed_option_quotes=tuple(
            ObservedOptionQuoteEvidence(
                quote=item.quote,
                fetched_at=item.fetched_at,
                observed_at=item.observed_at,
            )
            for item in option_quotes
        ),
        candidates=candidates,
    )
    return _EvidenceAcquisitionFailure(evidence)


def _promotion_mismatches(
    artifact: OptionResolverPromotionArtifact,
    *,
    account_fingerprint: str,
    autonomy_mode: str,
    mandate_digest: str,
    policy_digest: str,
    compatibility_digest: str,
    now: datetime,
) -> tuple[str, ...]:
    expected = {
        "account_fingerprint": account_fingerprint,
        "mandate_digest": mandate_digest,
        "policy_digest": policy_digest,
        "resolver_compatibility_digest": compatibility_digest,
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "scoring_version": SCORING_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "broker_resolver_version": BROKER_RESOLVER_VERSION,
    }
    mismatches = [field for field, value in expected.items() if getattr(artifact, field) != value]
    if autonomy_mode not in artifact.permitted_modes:
        mismatches.append("permitted_modes")
    if not artifact.effective_from <= now < artifact.expires_at:
        mismatches.append("effective_window")
    return tuple(sorted(mismatches))


__all__ = [
    "PROMOTION_SCHEMA_VERSION",
    "AutonomousOptionSelectionService",
    "LoadedOptionPromotion",
    "OptionResolution",
    "OptionResolverPromotionArtifact",
    "load_option_resolver_promotion",
    "resolver_compatibility_digest",
]

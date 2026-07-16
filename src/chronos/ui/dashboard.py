"""Two-page Streamlit shell backed by the configured broker boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic

import streamlit as st

from chronos.broker.base import BrokerError
from chronos.broker.demo import DemoBroker
from chronos.domain.enums import BrokerMode, EligibilityStatus
from chronos.domain.models import OptionContract
from chronos.services.reconciliation import ReconciliationResult
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.services.short_put_demo_approval import (
    MAX_CONFIRMATION_INTEGER_INPUT_CHARACTERS,
    MAX_TYPED_SYMBOL_CHARACTERS,
    DemoApprovalStatus,
    ShortPutDemoApprovalRequest,
    ShortPutDemoApprovalResult,
)
from chronos.services.short_put_demo_what_if import (
    MAX_LIMIT_DECIMAL_DIGITS,
    MAX_LIMIT_DECIMAL_PLACES,
    MAX_LIMIT_INPUT_CHARACTERS,
    MAX_LIMIT_PRICE,
    DemoWhatIfStatus,
    ShortPutDemoWhatIfRequest,
    ShortPutDemoWhatIfResult,
    limit_price_decimal_places,
    limit_price_has_bounded_representation,
)
from chronos.services.short_put_risk_preview import (
    MAX_COMMISSION_DECIMAL_DIGITS,
    MAX_COMMISSION_DECIMAL_PLACES,
    MAX_COMMISSION_INPUT_CHARACTERS,
    MAX_TOTAL_COMMISSION_ESTIMATE,
    RiskPreviewStatus,
    ShortPutRiskPreviewRequest,
    ShortPutRiskPreviewResult,
    commission_estimate_decimal_places,
    commission_estimate_has_bounded_representation,
)
from chronos.strategy.strike_resolver import RankedCandidate
from chronos.ui.charts import quote_ladder_figure, short_put_expiration_risk_figure
from chronos.ui.components import (
    format_money,
    render_reconciliation_status,
    render_runtime_scope_unavailable,
    render_runtime_status,
    render_safety_notice,
)
from chronos.ui.portfolio_state import (
    PortfolioObservationSessionRecord,
    retain_portfolio_observation,
    validate_portfolio_observation_record,
)
from chronos.ui.rehearsal_state import (
    DemoApprovalDisposition,
    DemoApprovalSessionRecord,
    abandon_demo_approval_record,
    refresh_demo_approval_record,
    retain_demo_approval_receipt,
    supersede_demo_approval_record,
)
from chronos.ui.runtime_scope import RuntimeScopeView, validate_runtime_scope
from chronos.ui.session import AppRuntime
from chronos.utils.logging import mask_account_identifiers
from chronos.utils.time import as_market_time

_CANDIDATE_EVALUATION_STATE_KEY = "chronos_candidate_evaluation"
_RISK_PREVIEW_STATE_KEY = "chronos_short_put_risk_preview"
_DEMO_WHAT_IF_STATE_KEY = "chronos_short_put_demo_what_if"
_DEMO_WHAT_IF_LIMIT_STATE_KEY = "chronos_demo_what_if_limit"
_DEMO_APPROVAL_STATE_KEY = "chronos_short_put_demo_approval"
_DEMO_APPROVAL_SYMBOL_STATE_KEY = "chronos_demo_approval_symbol"
_DEMO_APPROVAL_QUANTITY_STATE_KEY = "chronos_demo_approval_quantity"
_DEMO_APPROVAL_ACK_STATE_KEY = "chronos_demo_approval_risk_ack"
_DEMO_APPROVAL_FEEDBACK_STATE_KEY = "chronos_demo_approval_feedback"
_PORTFOLIO_OBSERVATION_STATE_KEY = "chronos_portfolio_observation"
_MAX_DEMO_APPROVAL_FEEDBACK_REASONS = 4
_MAX_DEMO_APPROVAL_FEEDBACK_REASON_CHARACTERS = 300


@dataclass(frozen=True, slots=True)
class _DemoApprovalFeedback:
    """Parent-independent, process-memory-only feedback for one withheld attempt."""

    symbol: str
    status: DemoApprovalStatus
    reasons: tuple[str, ...]


def render_dashboard(runtime: AppRuntime) -> None:
    st.title("Chronos")
    st.caption("Local-first Wheel Strategy workspace")
    try:
        runtime_scope = validate_runtime_scope(getattr(runtime, "runtime_scope", None))
    except ValueError:
        logging.getLogger("chronos.ui.dashboard").warning(
            "Runtime scope was rejected before rendering interactive workspace",
            extra={"event": "runtime_scope_rejected"},
        )
        render_runtime_scope_unavailable()
        return
    render_safety_notice(runtime_scope)
    render_runtime_status(runtime_scope)
    _stored_demo_approval_record()
    try:
        page = st.sidebar.radio(
            "Workspace",
            ("Portfolio Dashboard", "Symbol Detail & Order Workspace"),
        )
        if page == "Portfolio Dashboard":
            _render_portfolio(runtime, runtime_scope)
        else:
            _render_symbol_detail(runtime)
    except BrokerError as error:
        logging.getLogger("chronos.ui.dashboard").warning(
            "Broker operation failed",
            extra={
                "event": "broker_ui_operation_failed",
                "error_type": type(error).__name__,
            },
        )
        st.error("The broker request could not complete safely. See the local log.")
    except Exception:
        logging.getLogger("chronos.ui.dashboard").exception(
            "Unexpected dashboard operation failure",
            extra={"event": "dashboard_operation_failed"},
        )
        st.error("The dashboard could not complete this request safely. See the local log.")


def _stored_portfolio_observation(
    runtime_scope: RuntimeScopeView,
) -> PortfolioObservationSessionRecord | None:
    value = st.session_state.get(_PORTFOLIO_OBSERVATION_STATE_KEY)
    if value is None:
        return None
    try:
        return validate_portfolio_observation_record(value, runtime_scope)
    except ValueError:
        st.session_state.pop(_PORTFOLIO_OBSERVATION_STATE_KEY, None)
        logging.getLogger("chronos.ui.dashboard").warning(
            "Stored portfolio observation was rejected before display",
            extra={"event": "portfolio_observation_state_rejected"},
        )
        return None


def _render_portfolio(runtime: AppRuntime, runtime_scope: RuntimeScopeView) -> None:
    st.header("Portfolio Dashboard")
    observation = _stored_portfolio_observation(runtime_scope)
    if st.button("Run read-only portfolio observation", type="primary"):
        st.session_state.pop(_PORTFOLIO_OBSERVATION_STATE_KEY, None)
        observation = None
        try:
            result = runtime.reconciliation.reconcile()
            observation = retain_portfolio_observation(runtime_scope, result)
            st.session_state[_PORTFOLIO_OBSERVATION_STATE_KEY] = observation
        except BrokerError as error:
            logging.getLogger("chronos.ui.dashboard").warning(
                "Explicit portfolio observation could not complete safely",
                extra={
                    "event": "portfolio_observation_failed",
                    "error_type": type(error).__name__,
                },
            )
            st.error("The read-only portfolio observation could not complete safely.")
        except Exception as error:
            logging.getLogger("chronos.ui.dashboard").warning(
                "Explicit portfolio observation was rejected safely",
                extra={
                    "event": "portfolio_observation_rejected",
                    "error_type": type(error).__name__,
                },
            )
            st.error("The read-only portfolio observation could not be retained safely.")

    if observation is None:
        columns = st.columns(2)
        columns[0].metric("Portfolio observation", "NOT_OBSERVED")
        columns[1].metric("Opening actions", "LOCKED")
        st.error(
            "Opening actions are locked. A portfolio observation is historical "
            "decision-support evidence and can never authorize candidates or orders."
        )
        st.caption(
            "No portfolio observation has been requested in this session. Run the explicit "
            "read-only observation to capture one bounded broker and local-evidence window. "
            "Passive reruns do not refresh it."
        )
        return

    st.caption(
        "Last explicit portfolio observation — historical display only. It remains unchanged "
        "until another explicit observation or the session ends and is never reused as service "
        "authority."
    )
    _render_portfolio_result(observation.result)


def _render_portfolio_result(result: ReconciliationResult) -> None:
    render_reconciliation_status(result)
    st.error(
        "Opening actions are locked. A portfolio observation is historical decision-support "
        "evidence and can never authorize candidates or orders."
    )
    if result.reasons:
        st.warning("\n".join(f"- {mask_account_identifiers(reason)}" for reason in result.reasons))

    snapshot = result.snapshot
    if snapshot is None:
        st.error("Chronos withheld account values and exposures because no stable snapshot passed.")
        _render_symbol_reconciliation(result)
        _render_near_term_focus()
        return

    account = snapshot.account
    columns = st.columns(5)
    columns[0].metric(
        "Net liquidation",
        format_money(account.net_liquidation, account.currency),
    )
    columns[1].metric("Cash", format_money(account.total_cash, account.currency))
    columns[2].metric("Buying power", format_money(account.buying_power, account.currency))
    columns[3].metric("Open account orders", str(len(snapshot.open_orders)))
    columns[4].metric("Observed executions", str(snapshot.execution_count))

    st.subheader("Broker positions")
    rows = [
        {
            "Symbol": position.contract.symbol,
            "Type": position.contract.security_type.value,
            "Quantity": str(position.quantity),
            "Broker average cost": format_money(
                position.average_cost,
                position.contract.currency,
            ),
            "Market price": format_money(
                position.market_price,
                position.contract.currency,
            ),
            "Unrealized P&L": format_money(
                position.unrealized_pnl,
                position.contract.currency,
            ),
        }
        for position in snapshot.positions
    ]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.caption("No broker positions were present in the stable observation.")

    st.subheader("Open broker orders")
    order_rows = [
        {
            "Broker order": order.broker_order_id,
            "Symbol": order.contract.symbol,
            "Type": order.contract.security_type.value,
            "Side": order.side.value,
            "Quantity": str(order.quantity),
            "Filled": str(order.filled_quantity),
            "Remaining": str(order.remaining_quantity),
            "Limit": format_money(order.limit_price, order.contract.currency),
            "Lifecycle": order.lifecycle.value,
            "Transmit": "YES" if order.transmit else "NO",
        }
        for order in snapshot.open_orders
    ]
    if order_rows:
        st.dataframe(order_rows, width="stretch", hide_index=True)
    else:
        st.caption("No open broker orders were present in the stable observation.")

    _render_symbol_reconciliation(result)
    _render_near_term_focus()


def _render_symbol_reconciliation(result: ReconciliationResult) -> None:
    st.subheader("Wheel reconciliation")
    st.dataframe(
        [
            {
                "Symbol": symbol.symbol,
                "Status": symbol.status.value,
                "Wheel stage": symbol.stage.value,
                "Stock shares": str(symbol.stock_shares),
                "Short puts": str(symbol.short_put_contracts),
                "Short calls": str(symbol.short_call_contracts),
                "Pending puts": str(symbol.pending_put_contracts),
                "Pending calls": str(symbol.pending_call_contracts),
                "Manual review": "YES" if symbol.manual_review_required else "NO",
                "Opening actions": "LOCKED",
            }
            for symbol in result.symbols
        ],
        width="stretch",
        hide_index=True,
    )
    for symbol in result.symbols:
        safe_symbol = mask_account_identifiers(symbol.symbol)
        with st.expander(f"{safe_symbol} reconciliation reasoning"):
            for reason in symbol.reasons:
                st.write(f"- {mask_account_identifiers(reason)}")


def _render_near_term_focus() -> None:
    st.subheader("Near-Term / Weekly Focus")
    st.info(
        "Read-only short-put evaluation is available in the symbol workspace. It remains locked "
        "unless fresh whole-account reconciliation proves zero exposure and capital provenance."
    )


def _render_symbol_detail(runtime: AppRuntime) -> None:
    st.header("Symbol Detail & Order Workspace")
    symbols = list(runtime.settings.symbol_allowlist)
    label = (
        "Allowlisted demo symbol"
        if isinstance(runtime.broker, DemoBroker)
        else "Allowlisted symbol"
    )
    symbol = st.selectbox(
        label,
        symbols,
        on_change=_clear_short_put_demo_approval_result,
    )
    st.subheader(symbol)

    stored = st.session_state.get(_CANDIDATE_EVALUATION_STATE_KEY)
    evaluation = (
        stored
        if isinstance(stored, ShortPutCandidateEvaluation) and stored.symbol == symbol
        else None
    )
    if stored is not None and evaluation is None:
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        _clear_short_put_risk_preview()
    if st.button("Run read-only evaluation", type="primary"):
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        _clear_short_put_risk_preview()
        evaluation = runtime.short_put_candidates.evaluate(symbol)
        st.session_state[_CANDIDATE_EVALUATION_STATE_KEY] = evaluation

    if evaluation is None:
        _clear_stale_evidence_without_candidate(symbol)

    if evaluation is not None and evaluation.reconciliation is not None:
        render_reconciliation_status(evaluation.reconciliation)
    status_columns = st.columns(2)
    status_columns[0].metric(
        "Candidate result",
        evaluation.status.value if evaluation is not None else "NOT_EVALUATED",
    )
    status_columns[1].metric("Candidate actions", "LOCKED")
    st.error(
        "Opening actions are locked. Candidate evidence cannot authorize or submit an order; "
        "only later deterministic DEMO what-if and approval rehearsals are available."
    )
    if evaluation is None:
        st.info(
            "No candidate request has been made. Run the explicit read-only evaluation to "
            "capture a fresh, bounded evidence window."
        )
    else:
        evaluated_at = as_market_time(evaluation.evaluated_at)
        st.caption(
            f"Last explicit evaluation at {evaluated_at:%Y-%m-%d %H:%M:%S %Z} — "
            "historical display only, not live authorization."
        )
    if evaluation is not None and evaluation.reasons:
        st.warning("\n".join(f"- {reason}" for reason in evaluation.reasons))

    if isinstance(runtime.broker, DemoBroker):
        matching_case = next(
            (fixture for fixture in runtime.broker.fixture_cases if fixture.symbol == symbol),
            None,
        )
        if matching_case is not None:
            st.info(
                "Deterministic fixture catalog context (not an evaluation outcome): "
                f"{matching_case.case.value}: {matching_case.explanation}"
            )

    st.subheader("Candidate ranking")
    if evaluation is None:
        st.caption("No historical candidate evaluation is stored for this symbol.")
    else:
        _render_short_put_evaluation(evaluation)
    evidence_panel = st.empty()

    def clear_evidence_panel() -> None:
        evidence_panel.empty()

    with evidence_panel.container():
        st.subheader("Scenario analysis")
        risk_result = _render_short_put_risk_preview(
            runtime,
            symbol,
            evaluation,
            clear_rendered_evidence=clear_evidence_panel,
        )
        st.subheader("Order preview")
        what_if_result = _render_short_put_demo_what_if(
            runtime,
            symbol,
            risk_result,
            clear_rendered_evidence=clear_evidence_panel,
        )
        _render_short_put_demo_approval(
            runtime,
            symbol,
            what_if_result,
            clear_rendered_evidence=clear_evidence_panel,
        )


def _render_short_put_evaluation(evaluation: ShortPutCandidateEvaluation) -> None:
    quote = evaluation.underlying_quote
    if quote is not None:
        observed_at = as_market_time(quote.timestamp)
        evaluated_at = as_market_time(evaluation.evaluated_at)
        columns = st.columns(4)
        columns[0].metric("Last", format_money(quote.last, quote.contract.currency))
        columns[1].metric("Bid", format_money(quote.bid, quote.contract.currency))
        columns[2].metric("Ask", format_money(quote.ask, quote.contract.currency))
        columns[3].metric("Underlying data quality", quote.data_quality.value)
        st.caption(
            f"Underlying quote observed {observed_at:%Y-%m-%d %H:%M:%S %Z} · "
            f"evaluation completed {evaluated_at:%Y-%m-%d %H:%M:%S %Z}"
        )
        st.plotly_chart(quote_ladder_figure(quote), width="stretch")

    chain = evaluation.chain
    if chain is not None:
        st.write(
            {
                "Trading class": chain.trading_class,
                "Exchange": chain.exchange,
                "Multiplier": str(chain.multiplier),
                "Expirations": [expiration.isoformat() for expiration in chain.expirations],
                "Available strikes": [
                    format_money(strike, quote.contract.currency) for strike in chain.strikes
                ]
                if quote is not None
                else "Withheld because quote currency is unavailable",
            }
        )

    resolution = evaluation.resolution
    if resolution is None:
        st.caption("Resolver evaluation was withheld because prerequisite evidence did not pass.")
        return

    if resolution.candidates:
        st.dataframe(
            [
                {
                    "Rank": rank,
                    "Expiration": candidate.expiration.isoformat(),
                    "DTE": candidate.dte,
                    "Strike": format_money(candidate.strike, candidate.contract.currency),
                    "Bid": format_money(candidate.bid, candidate.contract.currency),
                    "Ask": format_money(candidate.ask, candidate.contract.currency),
                    "Midpoint": format_money(candidate.midpoint, candidate.contract.currency),
                    "Delta": str(candidate.delta),
                    "IV": (
                        str(candidate.implied_volatility)
                        if candidate.implied_volatility is not None
                        else "Unavailable"
                    ),
                    "Volume": candidate.volume,
                    "Open interest": candidate.open_interest,
                    "Relative spread": str(candidate.relative_spread),
                    "Score": str(candidate.score),
                    "Quote age (s)": str(candidate.data_age_seconds),
                    "Data quality": candidate.data_quality.value,
                    "Assignment obligation": format_money(
                        candidate.capital_check.proposal_amount,
                        candidate.contract.currency,
                    ),
                    "Symbol allocation": _format_ratio(
                        candidate.capital_check.resulting_symbol_allocation_pct
                    ),
                    "Total Wheel allocation": _format_ratio(
                        candidate.capital_check.resulting_total_wheel_allocation_pct
                    ),
                    "Opening actions": "LOCKED",
                }
                for rank, candidate in enumerate(resolution.candidates, start=1)
            ],
            width="stretch",
            hide_index=True,
        )
        for rank, candidate in enumerate(resolution.candidates, start=1):
            with st.expander(f"Candidate {rank} rationale"):
                st.write(candidate.selection_rationale)
    else:
        st.warning("No short-put candidate passed every resolver and capital filter.")

    if resolution.no_trade_reasons:
        st.warning("\n".join(f"- {reason}" for reason in resolution.no_trade_reasons))

    rejected_rows = []
    for rejected in resolution.rejected:
        contract = rejected.contract
        if isinstance(contract, OptionContract):
            expiration = contract.expiration.isoformat()
            strike = format_money(contract.strike, contract.currency)
        else:
            expiration = "Not an option"
            strike = "Not an option"
        rejected_rows.append(
            {
                "Contract": contract.con_id,
                "Expiration": expiration,
                "Strike": strike,
                "Data quality": rejected.data_quality.value,
                "Quote age (s)": (
                    str(rejected.data_age_seconds)
                    if rejected.data_age_seconds is not None
                    else "Unavailable"
                ),
                "Reason codes": ", ".join(
                    reason.code.value for reason in rejected.rejection_reasons
                ),
                "Explanations": " | ".join(
                    reason.explanation for reason in rejected.rejection_reasons
                ),
            }
        )
    if rejected_rows:
        st.subheader("Rejected contracts")
        st.dataframe(rejected_rows, width="stretch", hide_index=True)


def _render_short_put_risk_preview(
    runtime: AppRuntime,
    symbol: str,
    evaluation: ShortPutCandidateEvaluation | None,
    *,
    clear_rendered_evidence: Callable[[], None],
) -> ShortPutRiskPreviewResult | None:
    stored = st.session_state.get(_RISK_PREVIEW_STATE_KEY)
    result = (
        stored
        if isinstance(stored, ShortPutRiskPreviewResult) and stored.request.symbol == symbol
        else None
    )
    if stored is not None and result is None:
        st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)

    st.error(
        "Opening actions remain locked. This is an expiration payoff model, not a broker "
        "what-if preview, order draft, forecast, or authorization."
    )
    resolution = evaluation.resolution if evaluation is not None else None
    candidates = (
        resolution.candidates
        if evaluation is not None
        and evaluation.status is EligibilityStatus.ELIGIBLE
        and resolution is not None
        else ()
    )
    if not candidates:
        if result is not None:
            _render_short_put_risk_result(result)
        else:
            st.info(
                "A freshly eligible ranked candidate is required before risk assumptions can "
                "be entered."
            )
        return result

    candidate_by_id = {candidate.contract.con_id: candidate for candidate in candidates}
    selected_contract_id = st.selectbox(
        "Candidate for fresh risk preview",
        list(candidate_by_id),
        format_func=lambda contract_id: _risk_candidate_label(
            candidates,
            contract_id,
        ),
        key="chronos_risk_candidate_contract",
        on_change=_clear_short_put_risk_preview,
    )
    selected = candidate_by_id[selected_contract_id]
    commission_text = st.text_input(
        f"Estimated total commission for one contract ({selected.contract.currency})",
        value="",
        placeholder="Required, for example 0.65",
        key="chronos_risk_total_commission",
        on_change=_clear_short_put_risk_preview,
        help=(
            "Enter an explicit total estimate. Zero is allowed only when you intentionally want "
            "a fees-excluded model. Chronos never supplies a default."
        ),
    )
    commission, commission_error = _parse_commission_estimate(commission_text)
    if commission_error is not None:
        st.warning(commission_error)
    elif commission is None:
        st.info("Enter an explicit nonnegative commission estimate to enable the fresh preview.")

    if result is not None:
        current_request = result.request
        refresh = result.candidate_refresh
        selected_contract_disappeared = current_request.selected_contract_id not in candidate_by_id
        if (
            (
                current_request.selected_contract_id != selected_contract_id
                and not selected_contract_disappeared
            )
            or commission is None
            or current_request.total_commission_estimate != commission
            or (
                refresh is not None
                and evaluation is not None
                and refresh.evaluated_at != evaluation.evaluated_at
            )
        ):
            st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)
            result = None

    if st.button(
        "Refresh evidence & calculate locked risk",
        disabled=commission is None,
    ):
        if commission is None:
            st.error("Commission validation failed; no risk refresh was requested.")
            return result
        request = ShortPutRiskPreviewRequest(
            symbol=symbol,
            selected_contract_id=selected_contract_id,
            total_commission_estimate=commission,
        )
        st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)
        _clear_short_put_demo_what_if()
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        clear_rendered_evidence()
        result = runtime.short_put_risk_preview.preview(request)
        st.session_state[_RISK_PREVIEW_STATE_KEY] = result
        if result.candidate_refresh is not None:
            st.session_state[_CANDIDATE_EVALUATION_STATE_KEY] = result.candidate_refresh
        st.rerun()

    if result is not None:
        _render_short_put_risk_result(result)
    return result


def _risk_candidate_label(candidates: tuple[RankedCandidate, ...], contract_id: int) -> str:
    for rank, candidate in enumerate(candidates, start=1):
        if candidate.contract.con_id != contract_id:
            continue
        return (
            f"Rank {rank} · {candidate.expiration.isoformat()} · "
            f"{candidate.strike:.2f} {candidate.contract.currency} PUT · conId {contract_id}"
        )
    return f"Unavailable contract {contract_id}"


def _parse_commission_estimate(value: str) -> tuple[Decimal | None, str | None]:
    normalized = value.strip()
    if not normalized:
        return None, None
    if len(normalized) > MAX_COMMISSION_INPUT_CHARACTERS:
        return None, "The commission estimate input is too long."
    try:
        estimate = Decimal(normalized)
    except InvalidOperation:
        return None, "The commission estimate must be a valid decimal amount."
    if not estimate.is_finite() or estimate < 0:
        return None, "The commission estimate must be finite and nonnegative."
    if estimate > MAX_TOTAL_COMMISSION_ESTIMATE:
        return (
            None,
            f"The commission estimate cannot exceed {MAX_TOTAL_COMMISSION_ESTIMATE}.",
        )
    if not commission_estimate_has_bounded_representation(estimate):
        return (
            None,
            f"The commission estimate supports at most {MAX_COMMISSION_DECIMAL_DIGITS} "
            "decimal digits.",
        )
    if commission_estimate_decimal_places(estimate) > MAX_COMMISSION_DECIMAL_PLACES:
        return (
            None,
            f"The commission estimate supports at most {MAX_COMMISSION_DECIMAL_PLACES} "
            "fractional decimal places.",
        )
    return estimate, None


def _clear_short_put_risk_preview() -> None:
    st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)
    _clear_short_put_demo_what_if()


def _clear_stale_evidence_without_candidate(symbol: str) -> None:
    stored_risk = st.session_state.get(_RISK_PREVIEW_STATE_KEY)
    preserve_risk_attempt = (
        isinstance(stored_risk, ShortPutRiskPreviewResult)
        and stored_risk.request.symbol == symbol
        and stored_risk.status is RiskPreviewStatus.WITHHELD
        and stored_risk.candidate_refresh is None
    )
    if not preserve_risk_attempt:
        st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)

    stored_what_if = st.session_state.get(_DEMO_WHAT_IF_STATE_KEY)
    preserve_what_if_attempt = (
        isinstance(stored_what_if, ShortPutDemoWhatIfResult)
        and stored_what_if.request.symbol == symbol
        and stored_what_if.status is DemoWhatIfStatus.WITHHELD
        and stored_what_if.risk_refresh is None
    )
    preserve_approval_receipt = _has_standalone_demo_approval_receipt(symbol)
    if preserve_what_if_attempt:
        st.session_state.pop(_DEMO_WHAT_IF_LIMIT_STATE_KEY, None)
        _clear_short_put_demo_approval()
    elif preserve_approval_receipt:
        st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
        st.session_state.pop(_DEMO_WHAT_IF_LIMIT_STATE_KEY, None)
        _clear_short_put_demo_approval_inputs()
    else:
        _clear_short_put_demo_what_if()


def _render_short_put_demo_what_if(
    runtime: AppRuntime,
    symbol: str,
    risk_result: ShortPutRiskPreviewResult | None,
    *,
    clear_rendered_evidence: Callable[[], None],
) -> ShortPutDemoWhatIfResult | None:
    stored = st.session_state.get(_DEMO_WHAT_IF_STATE_KEY)
    result = (
        stored
        if isinstance(stored, ShortPutDemoWhatIfResult) and stored.request.symbol == symbol
        else None
    )
    if stored is not None and result is None:
        st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)

    if not _has_exact_demo_rehearsal_boundary(runtime):
        _clear_short_put_demo_what_if_without_current_parent(symbol)
        st.error(
            "Locked: real IBKR what-if and every submission method remain disabled. "
            "This milestone exposes no order-channel action outside deterministic DEMO mode."
        )
        return None

    st.error(
        "DEMO rehearsal only. A successful result stops at WHAT_IF_PREVIEWED; it cannot confirm, "
        "persist, transmit, or submit an order."
    )
    ready_risk = (
        risk_result
        if risk_result is not None
        and risk_result.status is RiskPreviewStatus.READY
        and risk_result.preview is not None
        and risk_result.candidate_refresh is not None
        else None
    )
    if ready_risk is None:
        if result is not None and result.status is DemoWhatIfStatus.WITHHELD:
            _render_short_put_demo_what_if_result(result)
        else:
            _clear_short_put_demo_what_if_without_current_parent(symbol)
            st.info(
                "A current READY expiration-risk result is required before exact DEMO limit "
                "terms can be rehearsed."
            )
        return result

    risk_preview = ready_risk.preview
    assert risk_preview is not None
    refresh = ready_risk.candidate_refresh
    assert refresh is not None
    resolution = refresh.resolution
    selected_candidate = (
        next(
            (
                candidate
                for candidate in resolution.candidates
                if candidate.contract.con_id == risk_preview.selected_contract.con_id
            ),
            None,
        )
        if resolution is not None
        else None
    )
    if selected_candidate is None:
        _clear_short_put_demo_what_if()
        st.warning("The risk selection is not present in its fresh candidate evidence.")
        return None

    if result is not None:
        parent = result.risk_refresh
        if (
            result.request.selected_contract_id != risk_preview.selected_contract.con_id
            or result.request.total_commission_estimate
            != ready_risk.request.total_commission_estimate
            or parent is None
            or parent != ready_risk
        ):
            _clear_short_put_demo_what_if()
            result = None

    limit_text = st.text_input(
        f"Exact DEMO limit credit per share ({risk_preview.currency})",
        value="",
        placeholder=f"Fresh spread {selected_candidate.bid} - {selected_candidate.ask}",
        key=_DEMO_WHAT_IF_LIMIT_STATE_KEY,
        on_change=_clear_short_put_demo_what_if_result,
        help=(
            "Enter an explicit one-contract limit inside the displayed fresh spread and aligned "
            "to the contract minimum tick. No default is supplied."
        ),
    )
    limit_price, limit_error = _parse_demo_limit_price(limit_text)
    if limit_error is None and limit_price is not None:
        limit_error = _demo_limit_market_error(limit_price, selected_candidate)
    if limit_error is not None:
        st.warning(limit_error)
    elif limit_price is None:
        st.info("Enter an explicit limit credit to enable the deterministic what-if rehearsal.")

    if result is not None and (limit_price is None or result.request.limit_price != limit_price):
        st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
        result = None

    if st.button(
        "Refresh evidence & run locked DEMO what-if",
        disabled=limit_price is None or limit_error is not None,
    ):
        if limit_price is None or limit_error is not None:
            st.error("Limit validation failed; no DEMO what-if was requested.")
            return result
        request = ShortPutDemoWhatIfRequest(
            symbol=symbol,
            selected_contract_id=risk_preview.selected_contract.con_id,
            total_commission_estimate=ready_risk.request.total_commission_estimate,
            limit_price=limit_price,
        )
        _clear_short_put_demo_approval()
        st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
        st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        clear_rendered_evidence()
        result = runtime.short_put_demo_what_if.preview(request)
        st.session_state[_DEMO_WHAT_IF_STATE_KEY] = result
        if result.risk_refresh is not None:
            st.session_state[_RISK_PREVIEW_STATE_KEY] = result.risk_refresh
            if result.risk_refresh.candidate_refresh is not None:
                st.session_state[_CANDIDATE_EVALUATION_STATE_KEY] = (
                    result.risk_refresh.candidate_refresh
                )
        st.rerun()

    if result is not None:
        _render_short_put_demo_what_if_result(result)
    return result


def _parse_demo_limit_price(value: str) -> tuple[Decimal | None, str | None]:
    normalized = value.strip()
    if not normalized:
        return None, None
    if len(normalized) > MAX_LIMIT_INPUT_CHARACTERS:
        return None, "The DEMO limit input is too long."
    try:
        limit_price = Decimal(normalized)
    except InvalidOperation:
        return None, "The DEMO limit must be a valid decimal amount."
    if not limit_price.is_finite() or limit_price <= 0:
        return None, "The DEMO limit must be finite and positive."
    if limit_price > MAX_LIMIT_PRICE:
        return None, f"The DEMO limit cannot exceed {MAX_LIMIT_PRICE}."
    if not limit_price_has_bounded_representation(limit_price):
        return None, f"The DEMO limit supports at most {MAX_LIMIT_DECIMAL_DIGITS} decimal digits."
    if limit_price_decimal_places(limit_price) > MAX_LIMIT_DECIMAL_PLACES:
        return (
            None,
            "The DEMO limit supports at most "
            f"{MAX_LIMIT_DECIMAL_PLACES} fractional decimal places.",
        )
    return limit_price, None


def _demo_limit_market_error(limit_price: Decimal, candidate: RankedCandidate) -> str | None:
    if limit_price < candidate.bid or limit_price > candidate.ask:
        return "The DEMO limit must stay inside the displayed fresh bid/ask spread."
    try:
        if limit_price % candidate.contract.min_tick != 0:
            return (
                "The DEMO limit must align to the contract minimum tick of "
                f"{candidate.contract.min_tick}."
            )
    except InvalidOperation:
        return "The DEMO limit could not be checked safely against the contract minimum tick."
    return None


def _clear_short_put_demo_what_if() -> None:
    _clear_short_put_demo_what_if_result()
    st.session_state.pop(_DEMO_WHAT_IF_LIMIT_STATE_KEY, None)


def _clear_short_put_demo_what_if_without_current_parent(symbol: str) -> None:
    st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
    st.session_state.pop(_DEMO_WHAT_IF_LIMIT_STATE_KEY, None)
    if _has_standalone_demo_approval_receipt(symbol):
        _clear_short_put_demo_approval_inputs()
    else:
        _clear_short_put_demo_approval()


def _has_standalone_demo_approval_receipt(symbol: str) -> bool:
    stored = _stored_demo_approval_record()
    return (
        stored is not None
        and stored.symbol == symbol
        and stored.disposition is DemoApprovalDisposition.RETAINED
        and stored.receipt is not None
    )


def _stored_demo_approval_record() -> DemoApprovalSessionRecord | None:
    """Return current session presentation state after applying its display lease."""

    stored = st.session_state.get(_DEMO_APPROVAL_STATE_KEY)
    if not isinstance(stored, DemoApprovalSessionRecord):
        if stored is not None:
            st.session_state.pop(_DEMO_APPROVAL_STATE_KEY, None)
        return None
    try:
        refreshed = refresh_demo_approval_record(
            stored,
            now_monotonic=monotonic(),
        )
    except (TypeError, ValueError):
        st.session_state.pop(_DEMO_APPROVAL_STATE_KEY, None)
        logging.getLogger("chronos.ui.dashboard").warning(
            "Malformed DEMO approval session record discarded",
            extra={"event": "demo_approval_session_record_discarded"},
        )
        return None
    if refreshed != stored:
        st.session_state[_DEMO_APPROVAL_STATE_KEY] = refreshed
    return refreshed


def _clear_short_put_demo_what_if_result() -> None:
    st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
    _clear_short_put_demo_approval()


def _render_short_put_demo_what_if_result(result: ShortPutDemoWhatIfResult) -> None:
    evaluated_at = as_market_time(result.evaluated_at)
    columns = st.columns(3)
    columns[0].metric("DEMO what-if", result.status.value)
    columns[1].metric("Progression", "STOPPED")
    columns[2].metric("Submission", "LOCKED")
    st.caption(
        f"DEMO rehearsal completed {evaluated_at:%Y-%m-%d %H:%M:%S %Z} — "
        "historical display only; no authority is retained."
    )
    if result.reasons:
        st.warning("\n".join(f"- {reason}" for reason in result.reasons))
    preview = result.preview
    if result.status is not DemoWhatIfStatus.WHAT_IF_PREVIEWED or preview is None:
        st.caption("Broker what-if evidence was withheld; confirmation and submission stay locked.")
        return

    currency = preview.currency
    terms = st.columns(6)
    terms[0].metric("Exact limit / share", format_money(preview.limit_price, currency))
    terms[1].metric("Fresh bid / share", format_money(preview.fresh_bid, currency))
    terms[2].metric("Fresh ask / share", format_money(preview.fresh_ask, currency))
    terms[3].metric(
        "Broker commission estimate",
        format_money(preview.broker_estimated_commission, currency),
    )
    terms[4].metric(
        "Operator commission assumption",
        format_money(preview.operator_commission_estimate, currency),
    )
    terms[5].metric(
        "Commission variance",
        format_money(preview.commission_variance, currency),
    )
    margin = st.columns(3)
    margin[0].metric(
        "Initial margin change",
        format_money(preview.initial_margin_change, currency),
    )
    margin[1].metric(
        "Maintenance margin change",
        format_money(preview.maintenance_margin_change, currency),
    )
    margin[2].metric(
        "Equity-with-loan change",
        format_money(preview.equity_with_loan_change, currency),
    )
    exact_risk = st.columns(5)
    exact_risk[0].metric("Exact-limit gross premium", format_money(preview.gross_premium, currency))
    exact_risk[1].metric("Exact-limit net premium", format_money(preview.net_premium, currency))
    exact_risk[2].metric(
        "Exact-limit effective entry / share",
        format_money(preview.effective_entry_price, currency),
    )
    exact_risk[3].metric(
        "Total assignment obligation",
        format_money(preview.gross_assignment_obligation, currency),
    )
    exact_risk[4].metric(
        "Cash-secured allocation",
        _format_ratio(preview.cash_secured_notional_pct),
    )
    if preview.broker_warning_notice is not None:
        st.warning(preview.broker_warning_notice)
    st.write(
        {
            "Rehearsal reference": preview.correlation_id,
            "Contract": preview.selected_contract.con_id,
            "Intent": preview.intent.value,
            "Side": preview.side.value,
            "Quantity": preview.quantity,
            "Transmit": "NO",
            "Outside RTH": "NO",
            "Environment": "DEMO REHEARSAL ONLY",
            "User confirmation": "UNAVAILABLE",
            "Submission": "LOCKED",
        }
    )
    st.dataframe(
        [
            {
                "What-if reference point": " / ".join(
                    label.value.replace("_", " ").title() for label in point.labels
                ),
                "Underlying at expiration": format_money(point.underlying_price, currency),
                "Exact-limit expiration P&L": format_money(point.expiration_pnl, currency),
                "Submission": "LOCKED",
            }
            for point in preview.scenario_points
        ],
        width="stretch",
        hide_index=True,
    )
    st.error(
        "No confirmation or submit control exists. Real IBKR preview, submission, modification, "
        "and cancellation remain hard-disabled."
    )


def _render_short_put_demo_approval(
    runtime: AppRuntime,
    symbol: str,
    what_if_result: ShortPutDemoWhatIfResult | None,
    *,
    clear_rendered_evidence: Callable[[], None],
) -> None:
    record = _stored_demo_approval_record()
    if (
        record is not None
        and record.symbol != symbol
        and record.disposition is DemoApprovalDisposition.RETAINED
    ):
        st.session_state[_DEMO_APPROVAL_STATE_KEY] = supersede_demo_approval_record(
            record,
            now_monotonic=monotonic(),
        )
        record = None
    matching_record = record if record is not None and record.symbol == symbol else None

    current_what_if = (
        what_if_result
        if _has_exact_demo_rehearsal_boundary(runtime)
        and what_if_result is not None
        and what_if_result.status is DemoWhatIfStatus.WHAT_IF_PREVIEWED
        and what_if_result.preview is not None
        and what_if_result.risk_refresh is not None
        else None
    )
    if current_what_if is None:
        _clear_short_put_demo_approval_inputs()
        if matching_record is not None:
            _render_short_put_demo_approval_record(matching_record)
        _render_short_put_demo_approval_feedback(symbol)
        return

    if (
        matching_record is not None
        and matching_record.disposition is DemoApprovalDisposition.RETAINED
    ):
        matching_record = supersede_demo_approval_record(
            matching_record,
            now_monotonic=monotonic(),
        )
        st.session_state[_DEMO_APPROVAL_STATE_KEY] = matching_record
    st.session_state.pop(_DEMO_APPROVAL_FEEDBACK_STATE_KEY, None)

    preview = current_what_if.preview
    assert preview is not None
    st.subheader("Approval rehearsal")
    st.error(
        "DEMO-only acknowledgment rehearsal. It creates no approval authority, persistence, "
        "order lifecycle, transmission, or broker submission."
    )
    typed_symbol = st.text_input(
        "Type the exact displayed symbol for the DEMO rehearsal",
        value="",
        placeholder=preview.symbol,
        key=_DEMO_APPROVAL_SYMBOL_STATE_KEY,
        on_change=_clear_short_put_demo_approval_result,
        help="Enter the uppercase symbol exactly as displayed. Chronos supplies no default.",
    )
    acknowledged_quantity_text = st.text_input(
        "Type the exact displayed contract quantity for the DEMO rehearsal",
        value="",
        placeholder=str(preview.quantity),
        key=_DEMO_APPROVAL_QUANTITY_STATE_KEY,
        on_change=_clear_short_put_demo_approval_result,
        help="Enter the one-contract quantity exactly as displayed. Chronos supplies no default.",
    )
    risk_terms_acknowledged = st.checkbox(
        "I acknowledge the exact DEMO rehearsal terms and assignment risk: "
        f"conId {preview.selected_contract.con_id}, exact limit "
        f"{format_money(preview.limit_price, preview.currency)} per share, and gross assignment "
        f"obligation {format_money(preview.gross_assignment_obligation, preview.currency)}.",
        value=False,
        key=_DEMO_APPROVAL_ACK_STATE_KEY,
        on_change=_clear_short_put_demo_approval_result,
        help=(
            "This is a deliberate, memoryless rehearsal acknowledgment only. It does not "
            "authorize an order."
        ),
    )

    symbol_error = _demo_approval_symbol_error(typed_symbol, preview.symbol)
    acknowledged_quantity, quantity_error = _parse_demo_approval_quantity(
        acknowledged_quantity_text,
        preview.quantity,
    )
    if symbol_error is not None:
        st.warning(symbol_error)
    elif not typed_symbol:
        st.info("Type the exact uppercase symbol to continue the DEMO approval rehearsal.")
    if quantity_error is not None:
        st.warning(quantity_error)
    elif acknowledged_quantity is None:
        st.info("Type the exact displayed quantity to continue the DEMO approval rehearsal.")
    if not risk_terms_acknowledged:
        st.info("Explicit acknowledgment of the displayed DEMO terms and risk is required.")

    inputs_ready = (
        symbol_error is None
        and typed_symbol == preview.symbol
        and quantity_error is None
        and acknowledged_quantity == preview.quantity
        and risk_terms_acknowledged
    )
    if st.button(
        "Refresh evidence & rehearse locked DEMO approval",
        disabled=not inputs_ready,
    ):
        if not inputs_ready or acknowledged_quantity is None:
            st.error("Approval-rehearsal validation failed; no refresh was requested.")
            return
        request = ShortPutDemoApprovalRequest(
            symbol=preview.symbol,
            selected_contract_id=preview.selected_contract.con_id,
            total_commission_estimate=current_what_if.request.total_commission_estimate,
            limit_price=preview.limit_price,
            gross_assignment_obligation=preview.gross_assignment_obligation,
            typed_symbol=typed_symbol,
            acknowledged_quantity=acknowledged_quantity,
            acknowledged_contract_id=preview.selected_contract.con_id,
            acknowledged_limit_price=preview.limit_price,
            acknowledged_gross_assignment_obligation=preview.gross_assignment_obligation,
            risk_terms_acknowledged=risk_terms_acknowledged,
        )
        previous_record = _stored_demo_approval_record()
        if previous_record is not None and previous_record.symbol != preview.symbol:
            previous_record = None
        st.session_state.pop(_DEMO_WHAT_IF_STATE_KEY, None)
        st.session_state.pop(_RISK_PREVIEW_STATE_KEY, None)
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        clear_rendered_evidence()
        attempt_result = runtime.short_put_demo_approval.rehearse(request)
        if (
            attempt_result.status is DemoApprovalStatus.APPROVAL_REHEARSED
            and attempt_result.receipt is not None
        ):
            st.session_state[_DEMO_APPROVAL_STATE_KEY] = retain_demo_approval_receipt(
                attempt_result.receipt,
                now_monotonic=monotonic(),
                previous=previous_record,
            )
            st.session_state.pop(_DEMO_APPROVAL_FEEDBACK_STATE_KEY, None)
        else:
            st.session_state[_DEMO_APPROVAL_FEEDBACK_STATE_KEY] = _demo_approval_feedback(
                attempt_result
            )
        st.rerun()


def _demo_approval_symbol_error(value: str, expected_symbol: str) -> str | None:
    if not value:
        return None
    if len(value) > MAX_TYPED_SYMBOL_CHARACTERS:
        return "The DEMO approval symbol input is too long."
    if value != expected_symbol:
        return f"The DEMO approval symbol must exactly match {expected_symbol}."
    return None


def _parse_demo_approval_quantity(
    value: str,
    expected_quantity: int,
) -> tuple[int | None, str | None]:
    if not value:
        return None, None
    if len(value) > MAX_CONFIRMATION_INTEGER_INPUT_CHARACTERS:
        return None, "The DEMO approval quantity input is too long."
    if not value.isascii() or not value.isdecimal():
        return None, "The DEMO approval quantity must be an unsigned whole number."
    if value != str(expected_quantity):
        return None, f"The DEMO approval quantity must exactly match {expected_quantity}."
    return expected_quantity, None


def _clear_short_put_demo_approval() -> None:
    _clear_short_put_demo_approval_result()
    _clear_short_put_demo_approval_inputs()


def _clear_short_put_demo_approval_inputs() -> None:
    st.session_state.pop(_DEMO_APPROVAL_SYMBOL_STATE_KEY, None)
    st.session_state.pop(_DEMO_APPROVAL_QUANTITY_STATE_KEY, None)
    st.session_state.pop(_DEMO_APPROVAL_ACK_STATE_KEY, None)


def _clear_short_put_demo_approval_result() -> None:
    stored = _stored_demo_approval_record()
    if stored is None:
        st.session_state.pop(_DEMO_APPROVAL_STATE_KEY, None)
        return
    st.session_state[_DEMO_APPROVAL_STATE_KEY] = supersede_demo_approval_record(
        stored,
        now_monotonic=monotonic(),
    )


def _has_exact_demo_rehearsal_boundary(runtime: AppRuntime) -> bool:
    """Fail closed unless every UI-facing DEMO boundary names one exact adapter."""

    broker = getattr(runtime, "broker", None)
    settings = getattr(runtime, "settings", None)
    connection = getattr(runtime, "connection", None)
    return (
        getattr(settings, "broker_mode", None) is BrokerMode.DEMO
        and type(broker) is DemoBroker
        and getattr(connection, "broker", None) is broker
    )


def _demo_approval_feedback(result: ShortPutDemoApprovalResult) -> _DemoApprovalFeedback:
    reasons = tuple(
        mask_account_identifiers(reason)
        for reason in result.reasons[:_MAX_DEMO_APPROVAL_FEEDBACK_REASONS]
        if reason
        and reason.isprintable()
        and len(reason) <= _MAX_DEMO_APPROVAL_FEEDBACK_REASON_CHARACTERS
    )
    if not reasons:
        reasons = (
            "Fresh DEMO approval evidence was withheld safely; stale parent evidence was cleared.",
        )
    return _DemoApprovalFeedback(
        symbol=result.request.symbol,
        status=DemoApprovalStatus.WITHHELD,
        reasons=reasons,
    )


def _render_short_put_demo_approval_feedback(symbol: str) -> None:
    stored = st.session_state.get(_DEMO_APPROVAL_FEEDBACK_STATE_KEY)
    feedback = (
        stored
        if isinstance(stored, _DemoApprovalFeedback)
        and stored.symbol == symbol
        and stored.status is DemoApprovalStatus.WITHHELD
        else None
    )
    if stored is not None and feedback is None:
        st.session_state.pop(_DEMO_APPROVAL_FEEDBACK_STATE_KEY, None)
    if feedback is None:
        return

    st.subheader("Approval rehearsal attempt")
    columns = st.columns(3)
    columns[0].metric("DEMO approval rehearsal", feedback.status.value)
    columns[1].metric("Progression", "STOPPED")
    columns[2].metric("Submission", "LOCKED")
    st.warning("\n".join(f"- {reason}" for reason in feedback.reasons))
    st.caption(
        "In-memory attempt feedback only. Candidate, risk, and what-if evidence from the "
        "failed refresh was cleared; no approval authority is retained."
    )


def _render_short_put_demo_approval_record(record: DemoApprovalSessionRecord) -> None:
    now_monotonic = monotonic()
    refreshed = refresh_demo_approval_record(
        record,
        now_monotonic=now_monotonic,
    )
    if refreshed != record:
        st.session_state[_DEMO_APPROVAL_STATE_KEY] = refreshed
    record = refreshed

    st.subheader("Approval rehearsal receipt")
    columns = st.columns(4)
    columns[0].metric(
        "DEMO approval rehearsal",
        (
            DemoApprovalStatus.APPROVAL_REHEARSED.value
            if record.disposition is DemoApprovalDisposition.RETAINED
            else record.disposition.value
        ),
    )
    columns[1].metric("Receipt disposition", record.disposition.value)
    columns[2].metric("Progression", "STOPPED")
    columns[3].metric("Submission", "LOCKED")

    receipt = record.receipt
    if record.disposition is not DemoApprovalDisposition.RETAINED or receipt is None:
        disposition_copy = {
            DemoApprovalDisposition.EXPIRED: (
                "The process-memory display lease ended. Receipt terms were discarded."
            ),
            DemoApprovalDisposition.ABANDONED: (
                "The operator abandoned the historical rehearsal. Receipt terms were discarded."
            ),
            DemoApprovalDisposition.SUPERSEDED: (
                "A newer evidence attempt superseded this rehearsal. Receipt terms were discarded."
            ),
        }
        st.caption(disposition_copy[record.disposition])
        st.write(
            {
                "Approval rehearsal reference": record.approval_reference,
                "Symbol": record.symbol,
                "Presentation disposition": record.disposition.value,
                "Approval authority": "NONE",
                "Persistence": "NONE",
                "Submission": "LOCKED",
            }
        )
        st.error(
            "This is a term-free session tombstone. It cannot restore evidence, authorize, "
            "persist, transmit, submit, modify, or cancel an order."
        )
        return

    evaluated_at = as_market_time(receipt.approval_rehearsed_at)
    remaining_seconds = max(
        0.0,
        record.display_expires_at_monotonic - now_monotonic,
    )
    st.caption(
        f"DEMO approval rehearsal completed {evaluated_at:%Y-%m-%d %H:%M:%S %Z} — "
        f"historical display only; terms expire from session display in "
        f"{remaining_seconds:.0f} seconds and create no authority."
    )

    currency = receipt.currency
    terms = st.columns(3)
    terms[0].metric("Rehearsed contract", str(receipt.selected_contract.con_id))
    terms[1].metric("Rehearsed quantity", str(receipt.quantity))
    terms[2].metric("Rehearsed exact limit / share", format_money(receipt.limit_price, currency))
    st.metric(
        "Rehearsed gross assignment obligation",
        format_money(receipt.gross_assignment_obligation, currency),
    )
    st.write(
        {
            "Approval rehearsal reference": receipt.approval_reference,
            "Environment": "DEMO REHEARSAL ONLY",
            "Approval authority": "NONE",
            "Persistence": "NONE",
            "Order lifecycle": "NOT CREATED",
            "Submission": "LOCKED",
        }
    )
    st.error(
        "The acknowledgment was rehearsed in memory only. It cannot authorize, persist, "
        "transmit, submit, modify, or cancel an order."
    )
    if st.button("Abandon historical DEMO rehearsal receipt"):
        st.session_state[_DEMO_APPROVAL_STATE_KEY] = abandon_demo_approval_record(
            record,
            now_monotonic=monotonic(),
        )
        st.rerun()


def _render_short_put_risk_result(result: ShortPutRiskPreviewResult) -> None:
    evaluated_at = as_market_time(result.evaluated_at)
    status_columns = st.columns(2)
    status_columns[0].metric("Risk preview", result.status.value)
    status_columns[1].metric("Opening actions", "LOCKED")
    st.caption(
        f"Fresh preview attempt completed {evaluated_at:%Y-%m-%d %H:%M:%S %Z} — "
        "historical display only."
    )
    if result.reasons:
        st.warning("\n".join(f"- {reason}" for reason in result.reasons))
    preview = result.preview
    if result.status is not RiskPreviewStatus.READY or preview is None:
        st.caption("Expiration payoff evidence was withheld; no modeled outcome is available.")
        return

    evidence_at = as_market_time(preview.evidence_evaluated_at)
    underlying_at = as_market_time(preview.underlying_observed_at)
    st.caption(
        f"Candidate evidence evaluated {evidence_at:%Y-%m-%d %H:%M:%S %Z} · "
        f"underlying observed {underlying_at:%Y-%m-%d %H:%M:%S %Z} · "
        f"option quality {preview.option_data_quality.value} · "
        f"option quote age {preview.option_quote_age_seconds}s"
    )
    currency = preview.currency
    columns = st.columns(6)
    columns[0].metric(
        "Fresh-bid credit / share",
        format_money(preview.hypothetical_credit_per_share, currency),
    )
    columns[1].metric(
        "Total gross premium",
        format_money(preview.gross_premium, currency),
    )
    columns[2].metric(
        "Total commission estimate",
        format_money(preview.total_commission_estimate, currency),
    )
    columns[3].metric(
        "Total net premium",
        format_money(preview.net_premium, currency),
    )
    columns[4].metric(
        "Effective entry / share",
        format_money(preview.effective_entry_price, currency),
    )
    columns[5].metric(
        "Total assignment obligation",
        format_money(preview.gross_assignment_obligation, currency),
    )
    detail_columns = st.columns(5)
    detail_columns[0].metric("Assigned shares", str(preview.assigned_shares))
    detail_columns[1].metric(
        "Total net cash if assigned",
        format_money(preview.net_cash_deployed_if_assigned, currency),
    )
    detail_columns[2].metric(
        "Cash-secured allocation",
        _format_ratio(preview.cash_secured_notional_pct),
    )
    detail_columns[3].metric("Broker margin", "Unavailable — not requested")
    detail_columns[4].metric("Contracts modeled", str(preview.contracts))
    if preview.fees_excluded:
        st.warning("The operator entered zero commission; this model explicitly excludes fees.")
    st.write(
        {
            "Selected contract": preview.selected_contract.con_id,
            "Candidate rank after fresh refresh": preview.candidate_rank,
            "Expiration": preview.selected_contract.expiration.isoformat(),
            "Strike": format_money(preview.selected_contract.strike, currency),
            "Premium source": "Fresh bid; hypothetical and not a fill guarantee",
            "Commission source": "Explicit operator estimate",
            "Opening actions": "LOCKED",
        }
    )
    st.dataframe(
        [
            {
                "Reference point": " / ".join(
                    label.value.replace("_", " ").title() for label in point.labels
                ),
                "Underlying at expiration": format_money(point.underlying_price, currency),
                "Put intrinsic / share": format_money(
                    point.put_intrinsic_value_per_share,
                    currency,
                ),
                "Modeled expiration P&L": format_money(point.expiration_pnl, currency),
                "Opening actions": "LOCKED",
            }
            for point in preview.scenario_points
        ],
        width="stretch",
        hide_index=True,
    )
    try:
        risk_figure = short_put_expiration_risk_figure(preview)
    except ValueError as error:
        logging.getLogger("chronos.ui.dashboard").warning(
            "Risk-preview chart was withheld",
            extra={
                "event": "short_put_risk_chart_withheld",
                "error_type": type(error).__name__,
            },
        )
        st.warning("The payoff chart was withheld because its coordinates were not display-safe.")
    else:
        st.plotly_chart(risk_figure, width="stretch")
    st.info("\n".join(f"- {assumption}" for assumption in preview.assumptions))


def _format_ratio(value: Decimal | None) -> str:
    if value is None:
        return "Unavailable"
    return f"{value * Decimal('100'):.2f}%"

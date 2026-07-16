"""Two-page Streamlit shell backed by the configured broker boundary."""

from __future__ import annotations

import logging
from decimal import Decimal

import streamlit as st

from chronos.broker.base import BrokerError
from chronos.broker.demo import DemoBroker
from chronos.domain.models import OptionContract
from chronos.services.reconciliation import ReconciliationResult
from chronos.services.short_put_candidates import ShortPutCandidateEvaluation
from chronos.ui.charts import quote_ladder_figure
from chronos.ui.components import (
    format_money,
    render_reconciliation_status,
    render_safety_notice,
)
from chronos.ui.session import AppRuntime
from chronos.utils.time import as_market_time

_CANDIDATE_EVALUATION_STATE_KEY = "chronos_candidate_evaluation"


def render_dashboard(runtime: AppRuntime) -> None:
    st.title("Chronos")
    st.caption("Local-first Wheel Strategy workspace")
    render_safety_notice()
    try:
        page = st.sidebar.radio(
            "Workspace",
            ("Portfolio Dashboard", "Symbol Detail & Order Workspace"),
        )
        if page == "Portfolio Dashboard":
            _render_portfolio(runtime.reconciliation.reconcile())
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


def _render_portfolio(result: ReconciliationResult) -> None:
    st.header("Portfolio Dashboard")
    render_reconciliation_status(result)
    st.error(
        "Opening actions are locked. This milestone publishes read-only reconciliation "
        "evidence; it does not activate candidates or orders."
    )
    if result.reasons:
        st.warning("\n".join(f"- {reason}" for reason in result.reasons))

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
        with st.expander(f"{symbol.symbol} reconciliation reasoning"):
            for reason in symbol.reasons:
                st.write(f"- {reason}")


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
    symbol = st.selectbox(label, symbols)
    st.subheader(symbol)

    stored = st.session_state.get(_CANDIDATE_EVALUATION_STATE_KEY)
    evaluation = (
        stored
        if isinstance(stored, ShortPutCandidateEvaluation) and stored.symbol == symbol
        else None
    )
    if stored is not None and evaluation is None:
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
    if st.button("Run read-only evaluation", type="primary"):
        st.session_state.pop(_CANDIDATE_EVALUATION_STATE_KEY, None)
        evaluation = runtime.short_put_candidates.evaluate(symbol)
        st.session_state[_CANDIDATE_EVALUATION_STATE_KEY] = evaluation

    if evaluation is not None and evaluation.reconciliation is not None:
        render_reconciliation_status(evaluation.reconciliation)
    status_columns = st.columns(2)
    status_columns[0].metric(
        "Candidate result",
        evaluation.status.value if evaluation is not None else "NOT_EVALUATED",
    )
    status_columns[1].metric("Candidate actions", "LOCKED")
    st.error(
        "Opening actions are locked. Candidate evidence is read-only and cannot preview or "
        "submit an order."
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
    st.subheader("Scenario analysis")
    st.warning(
        "Locked: the tested scenario engine is not wired to reconciled positions and candidates."
    )
    st.subheader("Order preview")
    st.error(
        "Locked: no order can be previewed or submitted from this milestone; "
        "the application boundary does not expose an action path."
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


def _format_ratio(value: Decimal | None) -> str:
    if value is None:
        return "Unavailable"
    return f"{value * Decimal('100'):.2f}%"

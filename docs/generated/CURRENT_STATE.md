# Chronos current state

> **Generated file — do not hand-edit.** Run `.venv/bin/python scripts/build_current_state.py` after changing a source listed below.

This page reports committed code paths and validated repository defaults. It reads no environment, mandate, promotion file, database, broker, account, or market data. A mapped path is therefore **not authorization**, and `MITIGATED` is not `CLOSED`.

## Default posture

| Setting | Committed default |
| --- | --- |
| broker_mode | `"demo"` |
| broker_adapter | `"official_ibkr"` |
| ib_environment | `"paper"` |
| allow_order_transmit | `false` |
| allow_live_trading | `false` |
| autonomy_mandate_file | `null` |
| autonomy_proposers_file | `null` |
| autonomy_evidence_bundles | `false` |
| enable_autonomy_option_selection | `false` |
| autonomy_option_resolver_promotion_file | `null` |

The default runtime is `INERT_NO_MANDATE`: no autonomy runtime starts without an owner-supplied mandate, transmission defaults off, and autonomous option selection defaults off.

## Compiler capabilities

| Asset family | Decision | Strategy | Order intent | Adapter | Production facts route |
| --- | --- | --- | --- | --- | --- |
| CRYPTO | CLOSE | — | CLOSE_LONG_CRYPTO | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | CLOSE | — | CLOSE_LONG_CRYPTO | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | CLOSE | — | CLOSE_LONG_CRYPTO | ib_async | UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO |
| CRYPTO | INCREASE | LONG_EQUITY | OPEN_LONG_CRYPTO | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | INCREASE | LONG_EQUITY | OPEN_LONG_CRYPTO | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | INCREASE | LONG_EQUITY | OPEN_LONG_CRYPTO | ib_async | UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO |
| CRYPTO | OPEN | LONG_EQUITY | OPEN_LONG_CRYPTO | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | OPEN | LONG_EQUITY | OPEN_LONG_CRYPTO | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | OPEN | LONG_EQUITY | OPEN_LONG_CRYPTO | ib_async | UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO |
| CRYPTO | REDUCE | — | CLOSE_LONG_CRYPTO | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | REDUCE | — | CLOSE_LONG_CRYPTO | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| CRYPTO | REDUCE | — | CLOSE_LONG_CRYPTO | ib_async | UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO |
| EQUITY | CLOSE | — | CLOSE_LONG_STOCK | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | CLOSE | — | CLOSE_LONG_STOCK | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | CLOSE | — | CLOSE_LONG_STOCK | ib_async | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | INCREASE | LONG_EQUITY | OPEN_LONG_STOCK | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | INCREASE | LONG_EQUITY | OPEN_LONG_STOCK | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | INCREASE | LONG_EQUITY | OPEN_LONG_STOCK | ib_async | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | OPEN | LONG_EQUITY | OPEN_LONG_STOCK | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | OPEN | LONG_EQUITY | OPEN_LONG_STOCK | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | OPEN | LONG_EQUITY | OPEN_LONG_STOCK | ib_async | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | REDUCE | — | CLOSE_LONG_STOCK | demo | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | REDUCE | — | CLOSE_LONG_STOCK | official_ibkr | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY | REDUCE | — | CLOSE_LONG_STOCK | ib_async | BROKER_QUALIFIED_CONTRACT_AND_QUOTE |
| EQUITY_OPTION | CLOSE | — | CLOSE_SHORT_OPTION | demo | UNAVAILABLE_IN_PRODUCTION_GATHERER |
| EQUITY_OPTION | CLOSE | — | CLOSE_SHORT_OPTION | official_ibkr | UNAVAILABLE_IN_PRODUCTION_GATHERER |
| EQUITY_OPTION | CLOSE | — | CLOSE_SHORT_OPTION | ib_async | UNAVAILABLE_IN_PRODUCTION_GATHERER |
| EQUITY_OPTION | OPEN | CASH_SECURED_PUT | OPEN_SHORT_PUT | demo | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | OPEN | CASH_SECURED_PUT | OPEN_SHORT_PUT | official_ibkr | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | OPEN | CASH_SECURED_PUT | OPEN_SHORT_PUT | ib_async | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | OPEN | COVERED_CALL | OPEN_COVERED_CALL | demo | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | OPEN | COVERED_CALL | OPEN_COVERED_CALL | official_ibkr | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | OPEN | COVERED_CALL | OPEN_COVERED_CALL | ib_async | OPTION_SELECTION_RECEIPT_DISABLED_BY_DEFAULT |
| EQUITY_OPTION | REDUCE | — | CLOSE_SHORT_OPTION | demo | UNAVAILABLE_IN_PRODUCTION_GATHERER |
| EQUITY_OPTION | REDUCE | — | CLOSE_SHORT_OPTION | official_ibkr | UNAVAILABLE_IN_PRODUCTION_GATHERER |
| EQUITY_OPTION | REDUCE | — | CLOSE_SHORT_OPTION | ib_async | UNAVAILABLE_IN_PRODUCTION_GATHERER |

`UNAVAILABLE_IN_PRODUCTION_GATHERER` means the compiler can express the intent but the backend cannot currently obtain that decision's own qualified contract and quote. Opening equity options have a receipt-bound route, but it is disabled by default. `UNAVAILABLE_ADAPTER_QUALIFY_CRYPTO` means the production gatherer has a crypto branch but that adapter refuses crypto qualification.

## Cross-product status

The JSON expands 12 compiler mappings across 3 broker adapters, 7 autonomy modes, and 3 decision-evidence sources: **756 rows**.

| Current status | Rows |
| --- | --- |
| CONDITIONAL_OWNER_AND_EVIDENCE_GATED | 72 |
| REFUSED_ADAPTER_INSTRUMENT_FACTS | 36 |
| REFUSED_ADAPTER_MODE | 108 |
| REFUSED_NON_SUBMITTING_MODE | 432 |
| REFUSED_NO_INSTRUMENT_FACT_ROUTE | 54 |
| REFUSED_OPTION_SELECTION_DISABLED_BY_DEFAULT | 54 |

## Autonomy modes and promotion

| Mode | Submission class | Minimum promotion | Default promotion status |
| --- | --- | --- | --- |
| RESEARCH | NON_SUBMITTING | BACKTEST | NOT_CONFIGURED_BY_DEFAULT |
| BACKTEST | NON_SUBMITTING | BACKTEST | NOT_CONFIGURED_BY_DEFAULT |
| REPLAY | NON_SUBMITTING | REPLAY | NOT_CONFIGURED_BY_DEFAULT |
| SHADOW | NON_SUBMITTING | SHADOW | NOT_CONFIGURED_BY_DEFAULT |
| PAPER_AUTONOMOUS | SUBMITTING | PAPER_AUTONOMOUS | NOT_CONFIGURED_BY_DEFAULT |
| CANARY_LIVE_AUTONOMOUS | SUBMITTING | CANARY_LIVE_AUTONOMOUS | NOT_CONFIGURED_BY_DEFAULT |
| LIVE_AUTONOMOUS | SUBMITTING | CAPPED_LIVE_AUTONOMOUS | NOT_CONFIGURED_BY_DEFAULT |

Promotion values in a supplied mandate are external owner state. This generator does not load or validate one, so every row reports `NOT_CONFIGURED_BY_DEFAULT` rather than guessing an earned rung.

## Broker adapters and market-evidence sources

| Adapter | Effective implementation | Market-evidence source | Submit implementation | Paper path | Live path |
| --- | --- | --- | --- | --- | --- |
| demo | chronos.broker.demo.DemoBroker | DEMO_BROKER_FIXTURE | UNCONDITIONAL_REFUSAL | no | no |
| official_ibkr | chronos.broker.official_ibkr.OfficialIBKRBroker | IBKR_GATEWAY_OFFICIAL_API | IMPLEMENTED | yes | yes |
| ib_async | chronos.broker.ibkr.IBKRBroker | IBKR_GATEWAY_IB_ASYNC_READ_ONLY | UNCONDITIONAL_REFUSAL | no | no |

Evidence-source labels identify where the runtime would gather facts; they do not prove that a gateway was connected or that observations were correct. `BrokerAdapter.DEMO` has an unresolved naming alias: with `BrokerMode.IBKR`, the runtime fallback constructs `OfficialIBKRBroker`.

## Decision-evidence sources

| Evidence source | Binding | Citation kinds | Configuration required |
| --- | --- | --- | --- |
| placeholder_unbound | DEFAULT_UNBOUND | — | no |
| backend_served | BOUND_DURABLE_RECORD | worker_evidence_snapshot | yes |
| alert_attested | BOUND_DURABLE_RECORD | tradingview_alert | yes |

`placeholder_unbound` is the committed default because evidence binding and the proposer registry both default off. `backend_served` means Chronos witnessed and hashed the bytes; `alert_attested` means the proposer attested to bytes Chronos did not witness. None of these labels establishes that the facts were true.

## Explicitly unmapped vocabulary

- Asset families: `FUTURE`, `FUTURE_OPTION`, `INDEX_OPTION`
- Decision kinds: `CANCEL`, `HEDGE`, `HOLD`, `REPLACE`, `ROLL`
- Strategy shapes: `LONG_CALL`, `LONG_FUTURE`, `LONG_PUT`, `SHORT_EQUITY`, `SHORT_FUTURE`, `VERTICAL_CREDIT_SPREAD`, `VERTICAL_DEBIT_SPREAD`

Unmapped means refused by the compiler whitelist. Vocabulary presence alone is not a capability.

## Source fingerprint

| Source | SHA-256 |
| --- | --- |
| src/chronos/supervisor/compiler.py | `40b9b4a07dd90356f74552bb291296fbc069758b14cd471b122cea92dcd57538` |
| src/chronos/autonomy/enums.py | `96bcff19f76065a34d75c9fba00b5682de59448c8a8f770a8628494eec15a3e9` |
| src/chronos/config/settings.py | `64cc814dac1a85b365c02091b04a85a7aeb0316a729b605a7b9602b5a17016cb` |
| src/chronos/domain/enums.py | `6244760cac081c4ca0cbbec7cfc586efe6fd11471d7e06fbb29c8c4790414828` |
| src/chronos/runtime.py | `e85ead28e76de6a0265985ea21227367aa5ebaa2418bd74d9bb3c380fe6ec738` |
| src/chronos/api/autonomy_wiring.py | `dcb4fcf76465f338485b6536905b1007d58fb8dc3dbc77ada15ddbdc22c8b151` |
| src/chronos/supervisor/evidence_kinds.py | `374d6de281168796200d10b5ae64f83e4b336b0d18d91377fbefbd3d4a488e06` |
| src/chronos/broker/demo.py | `0b957a97f993dbc5beb51d167c0cdbb62b80f99d3b008d11083305450c5f1d1c` |
| src/chronos/broker/official_ibkr.py | `01a9de8212db02d1affcd285847fb398dd56a981344811bc7ea9fdec1598fa5e` |
| src/chronos/broker/ibkr.py | `5e5fc128b57ec79cdab1fe9340bf79abf3390c978849ad893d8d0d7feac2c62a` |

Machine-readable detail: [`capability-matrix.json`](capability-matrix.json).

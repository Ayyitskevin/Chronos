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

## Repository state

Milestone facts derived from the table documents and source listed under *State inputs* — never from HANDOFF.md, TASKS.md or any other prose, which retain history but cannot present old milestone state as current truth (plan §5).

### Volatile facts — run, do not copy

These change without a commit, so this page carries the command that measures each one and never its value.

| Fact | Command |
| --- | --- |
| Default branch | `git ls-remote --symref origin HEAD` |
| Current commit | `git rev-parse HEAD` |
| Test / skip / fail counts | `make gates` — read the pytest line; a commit's `Gate:` footer carries what its author measured at that head, never what this page says |

### ID watermarks (protocol §7 scans)

| Namespace | Highest allocated |
| --- | --- |
| `DECISIONS.md` D-nn | D-75 |
| `docs/adr/` ADR-nnnn | ADR-0059 |
| `RISK_REGISTER.md` R-nn | R-79 |

The next id is `max + 1`, scanned in the same session by the same PR that claims it (docs/AGENT_PROTOCOL.md §7); this table is a reading, not a reservation. The scans, verbatim:

```bash
grep -oE '^\| D-[0-9]+'  DECISIONS.md      | grep -oE '[0-9]+' | sort -n | tail -1
ls docs/adr/ | grep -oE 'ADR-[0-9]{4}' | sort | tail -1
grep -oE '^\| R-[0-9]+'  RISK_REGISTER.md  | grep -oE '[0-9]+' | sort -n | tail -1
```

### Risk register

| Status | Rows |
| --- | --- |
| ACCEPTED | 4 |
| CLOSED | 4 |
| MITIGATED | 50 |
| MITIGATED IN CODE | 17 |
| OPEN | 6 |
| all rows | 81 |

Status is the register's own column with its parenthetical qualifier stripped; `MITIGATED` is not `CLOSED`. Open rows:

| ID | Risk | Sev |
| --- | --- | --- |
| R-08 | Research data provenance (public mirrors, not broker data) | H |
| R-09 | Overfitting / selection bias in strategy choice | H |
| R-12 | ib_async maintenance / TWS API changes | M |
| R-29 | Autonomous model authority materially expands risk | C |
| R-37 | Model self-sizing widens the size envelope (`model_discretion`) | C |
| R-79 | Platform audit head anchor remains locally owner-rewritable | M |

### Vision plan §6 findings

| # | Finding | Status | Marker |
| --- | --- | --- | --- |
| 1 | Reconciliation readiness is consumed after one opening submission, while a complete supervised… | OPEN | Periodic half observed present 2026-09-03 (status note, not a closure) |
| 2 | The incident runbook invokes the deterministic-platform halt while the live order plane has a s… | OPEN | — |
| 3 | Restore guidance overstates safety: a missing live kill-switch file defaults disengaged. | ADDRESSED_WITH_RESIDUAL | Kill-engaged half addressed 2026-09-03 (D-63/ADR-0049, R-66) |
| 4 | Standing-authority prose says the mandate replaces arming, while submission still requires a cu… | OPEN | — |
| 5 | The supervisor treats any non-exception handoff return as `COMPLETE`, although `SubmissionOutco… | ADDRESSED_WITH_RESIDUAL | Addressed 2026-08-13 (A1; R-49) |
| 6 | External-worker provenance is static and its credential is not proposal-only. | ADDRESSED | Addressed 2026-08-12 (ADR-0023 Option A, owner-directed; D-24/R-48) |
| 7 | Several economic-looking fields do not mechanically affect execution. | OPEN | — |
| 8 | Promotion is not mechanically bound to the strategy and evidence that earned the prior rung. | OPEN | — |

Status is read mechanically from the plan's own markers: a struck-through finding carrying a bold *addressed* marker is `ADDRESSED`, or `ADDRESSED_WITH_RESIDUAL` when unstruck text still says "Still open from this finding"; an unstruck finding is `OPEN`; anything else is `UNKNOWN`. UNKNOWN is never closed.

### `chronos.auditlog` public names

`AuditLog`, `AuditLogCorruptionError`, `AuditRecord`, `ChainState`, `ChainVerification`, `bootstrap_anchor`, `read_audit_pair`, `verify_chain`, `verify_chain_text`, `verify_pair_text` — from `chronos.auditlog.__all__`, in declared order.

### Forwarding flags — declared, never read here

| Flag | Declared in | Declared default | Value |
| --- | --- | --- | --- |
| `CHRONOS_TV_BRIDGE_FORWARD` | `src/chronos/bridge/config.py` | `False` | not read (this page reads no environment) |
| `CHRONOS_WORKER_FORWARD` | `worker/config.py` | `False` | not read (this page reads no environment) |

Both are built inert and enabled only by the owner (plan §11); a value would be a claim about a deployment, which this page cannot make.

### State inputs

| Source | SHA-256 |
| --- | --- |
| DECISIONS.md | `a29eaaea431b955fafd8eca8f8c5254a1989dffaf3388ac3a6c2c881ff7a5ab7` |
| RISK_REGISTER.md | `52bfe7604b157992f770c17fa2579d5229876b5f3235832ca48858f5633fa383` |
| docs/VISION_COMPLETION_PLAN.md | `63fecfa948c3d74b577476c59db3010ddb63c1d50d21bad3e0de8e1a35b7a501` |
| src/chronos/auditlog/__init__.py | `5ec898d0ab735be6f0a7773bbeb9d8aacf4c3ea3b5eceff2d446a914d5a1647b` |
| src/chronos/bridge/config.py | `25f2f9a369d625440eb21d0d55f10386aa47d33dfb1843731788295b03cec636` |
| worker/config.py | `6b3763ec4280a65160442eb2fb63d523ae069ae70754842635681f855573dffe` |

## Source fingerprint

| Source | SHA-256 |
| --- | --- |
| src/chronos/supervisor/compiler.py | `40b9b4a07dd90356f74552bb291296fbc069758b14cd471b122cea92dcd57538` |
| src/chronos/autonomy/enums.py | `96bcff19f76065a34d75c9fba00b5682de59448c8a8f770a8628494eec15a3e9` |
| src/chronos/config/settings.py | `dfa0f5c7bb4d5c37dbbd8ad53f4f858fdc39e68248eaa572a6c24574033d1e7a` |
| src/chronos/domain/enums.py | `6244760cac081c4ca0cbbec7cfc586efe6fd11471d7e06fbb29c8c4790414828` |
| src/chronos/runtime.py | `e85ead28e76de6a0265985ea21227367aa5ebaa2418bd74d9bb3c380fe6ec738` |
| src/chronos/api/autonomy_wiring.py | `6538f1670631efdd3ee9c08245f87fecda8a51d082914a97d64e5452c9958ae2` |
| src/chronos/supervisor/evidence_kinds.py | `374d6de281168796200d10b5ae64f83e4b336b0d18d91377fbefbd3d4a488e06` |
| src/chronos/broker/demo.py | `0b957a97f993dbc5beb51d167c0cdbb62b80f99d3b008d11083305450c5f1d1c` |
| src/chronos/broker/official_ibkr.py | `01a9de8212db02d1affcd285847fb398dd56a981344811bc7ea9fdec1598fa5e` |
| src/chronos/broker/ibkr.py | `5e5fc128b57ec79cdab1fe9340bf79abf3390c978849ad893d8d0d7feac2c62a` |

Machine-readable detail: [`capability-matrix.json`](capability-matrix.json).

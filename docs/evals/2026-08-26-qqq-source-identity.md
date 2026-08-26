# QQQ independent-source identity evaluation — 2026-08-26

## Question

Can the frozen QQQ packet still describe an IBKR-family source as independent by changing
case, punctuation, spacing, product name, or client-library name, while clear sponsor and
second-source identities continue to pass?

## Source-family boundary

The boundary is based on provider identity, not string similarity alone:

- [IBKR's TWS API introduction](https://ibkrcampus.com/docs/tws-api/doc/introduction)
  defines the TWS API as connectivity to Trader Workstation or IB Gateway and as an
  interface to Interactive Brokers.
- [IBKR's installation lesson](https://ibkrcampus.com/campus/trading-lessons/installing-configuring-tws-for-the-api/)
  says TWS API clients use Trader Workstation or IB Gateway and describes those two host
  applications as API-equivalent.
- The [`ib_async` project](https://github.com/ib-api-reloaded/ib_async) identifies itself as
  an interface to Interactive Brokers' Trader Workstation and IB Gateway and implements the
  IBKR API protocol.

Therefore none of `IBKR`, `Interactive Brokers`, `TWS`, `Trader Workstation`, `IB Gateway`,
or `ib_async` can identify a source independent of the packet's `ibkr-tws-historical` bars.

## Baseline failure

The action-ingest guard casefolded the label and checked only the literal substrings `ibkr`
and `interactive brokers`. In the first red run, plain `IBKR` refused while 8/8 added
punctuation and product/library variants were accepted. The declaration path checked only
that `--attestation-source-id` was non-empty; its red run accepted all 13/13 forbidden labels
then supplied.

## Method

A disposable harness under `/tmp` created synthetic six-symbol manifests, daily bars,
corporate actions, and capture logs. It launched the production
`scripts/prepare_qqq_certified_data.py` CLI in fresh processes for every decision. No
repository dataset, owner data, credential, gateway, account, holdout, trial, broker, or
order surface was opened.

The final policy applies Unicode NFKC normalization, case folding, and alphanumeric token
normalization. Adjacent tokens are joined only until they exactly equal a reviewed complete
family marker. This catches separators inside a name without banning arbitrary substrings or
the ambiguous token `IB`.

## Results

| Surface and corpus | Expected | Observed |
|---|---:|---:|
| `ingest-actions`: 17 IBKR-family aliases | refuse 17 | refuse 17 |
| `build-declaration`: same 17 aliases | refuse 17 | refuse 17 |
| `ingest-actions`: 5 sponsor/unrelated identities | accept 5 | accept 5 |
| `build-declaration`: 4 independent-source identities | accept 4 | accept 4 |

The forbidden corpus covered plain, dotted, hyphenated, underscored, joined, split-inside-
word, mixed-case, and full-width forms across all six family markers. The accepted corpus
covered Invesco, State Street/SPDR, iShares, Nasdaq, Cboe, LSEG, an exchange bulletin, and an
unrelated `IBEX` token.

**Result: 43/43 expected production-CLI decisions.** The focused regression suite reported
53 passed after implementation.

## Residual

This is a conservative source-family separation rule, not identity authentication. A label
that does not name this family may still be false, incomplete, mistyped, or not actually
consulted. NFKC is not a general Unicode-confusable detector. The owner must still retain a
verifiable provider identity and reconciliation record, and the first real capture remains
owner-gated. No dataset was certified and no Phase-3 evidence gate advanced.

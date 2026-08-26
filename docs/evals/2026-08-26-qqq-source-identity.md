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
normalization. The normalized stream is searched for complete unambiguous markers of four or
more characters, including when they are separated or glued to another word. The shorter
`TWS` marker stays token-exact to avoid a general three-character substring rule; the common
joined `TWSAPI` form is explicit. This keeps unrelated `TWSE` and the ambiguous token `IB`
outside the deny rule.

The first implementation used only exact normalized marker membership. A non-author review
showed that it regressed the old behavior for glued labels such as `ibkrexport`, `ibkrdata`,
`IBKRHistorical`, and `ibkr2`. A second red run reproduced 9 glued aliases at both public CLI
seams (18 failures) before the substring rule above was applied.

## Results

| Surface and corpus | Expected | Observed |
|---|---:|---:|
| `ingest-actions`: 26 IBKR-family aliases | refuse 26 | refuse 26 |
| `build-declaration`: same 26 aliases | refuse 26 | refuse 26 |
| `ingest-actions`: 5 sponsor identities + 4 lexical controls | accept 9 | accept 9 |
| `build-declaration`: 4 second-source identities + 4 lexical controls | accept 8 | accept 8 |

The forbidden corpus covered plain, dotted, hyphenated, underscored, canonical joins,
split-inside-word, mixed-case, full-width, numeric-suffix, and foreign-word-suffix forms
across all six family markers. The accepted corpus covered Invesco, State Street/SPDR,
iShares, Nasdaq, Cboe, LSEG, an exchange bulletin, an unrelated `IBEX` token, and four
TWS-like controls (`net worth statement`, `outwards settlement`, `shortwave`, and `TWSE`).

**Result: 69/69 expected production-CLI decisions.** The focused packet regression suite
reported 79 passed after the HOLD remediation.

## Residual

This is a conservative source-family separation rule, not identity authentication. A label
that does not name this family may still be false, incomplete, mistyped, or not actually
consulted. NFKC is not a general Unicode-confusable detector. The owner must still retain a
verifiable provider identity and reconciliation record. The short `TWS` acronym is
intentionally token-exact except for explicit `TWSAPI`; that tradeoff avoids blocking
unrelated three-letter substrings and makes owner evidence, not label cleverness, the final
truth check. The first real capture remains owner-gated. No dataset was certified and no
Phase-3 evidence gate advanced.

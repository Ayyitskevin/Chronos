# Diagnosis: QQQ independent-source aliases bypassed the packet guard

## Symptom

`ingest-actions` rejected `IBKR corporate actions` but accepted labels such as `TWS API`,
`IB Gateway`, and `ib_async`. `build-declaration` accepted those same labels as the claimed
independent attestation source.

## Ranked hypotheses

1. The action path used exact casefolded substrings, so punctuation and alternative product
   names bypassed it.
2. The attestation path had no source-family check; non-empty text was treated as sufficient.
3. Adding more literal substrings would leave separator placement and joined words as future
   bypasses.

## Evidence and root cause

The first public-CLI red run refused only the pre-existing literal `IBKR` case and accepted
8/8 new aliases. The second red run accepted 13/13 forbidden attestation identities. The code
confirmed both causes: `_parse_action_file` checked only `"ibkr"` and
`"interactive brokers"`, while `_expected_declaration` checked only `.strip()` non-emptiness.

The defect was identity normalization at two evidence seams, not corporate-action parsing,
manifest integrity, provider data, or the certification digest.

## Correction and proof

One deterministic predicate now normalizes Unicode, case, and alphanumeric token boundaries.
It finds unambiguous four-or-more-character family markers across separators and inside
longer tokens; `TWS` remains token-exact while `TWSAPI` is explicit. Both public CLI paths use
the predicate.

The first exact-marker implementation passed its 43-case corpus but regressed labels that the
old substring check blocked. A non-author HOLD demonstrated `ibkrexport`, `ibkrdata`,
`IBKRHistorical`, and `ibkr2` passing at both seams. The review claim reproduced locally as
18 failures across 9 glued aliases before the long-marker substring rule. After remediation,
the expanded production-CLI corpus produced 69/69 expected decisions and the focused packet
suite produced 79 passes.

The correction authenticates no source. Passing the deny-family check remains weaker than
proving the claimed provider was real, complete, and independently reconciled.

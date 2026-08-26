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

One deterministic predicate now normalizes Unicode, case, and alphanumeric token boundaries,
then recognizes complete IBKR-family markers across separators. Both public CLI paths use it.
Red/green regression runs covered action and attestation identities independently; the final
production-CLI corpus produced 43/43 expected decisions and the focused suite produced 53
passes.

The correction authenticates no source. Passing the deny-family check remains weaker than
proving the claimed provider was real, complete, and independently reconciled.

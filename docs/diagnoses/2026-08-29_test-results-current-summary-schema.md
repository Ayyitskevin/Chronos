# TEST_RESULTS current-summary schema regression — 2026-08-29

## Bug

The repository-wide gate failed because Chronos diagnostics could no longer parse the current
validation snapshot from `docs/TEST_RESULTS.md`.

## Root cause

While recording an unmerged SBOM candidate, the heading was changed from the diagnostics contract
`Summary (current — re-measured YYYY-MM-DD)` to `Summary (current candidate — ...)`, and the body
stopped stating `Measured on exact main <40-character SHA>`. The parser intentionally accepts only
default-branch evidence under the current heading; candidate evidence is not a substitute.

## Feedback loop

```bash
.venv/bin/python -m pytest -q tests/unit/test_diagnostics_validation_snapshot.py::test_repository_current_summary_is_parseable
```

Before the fix, the test deterministically failed because `read_validation_snapshot()` returned
`None`.

## Fix

The exact-main `6034e1064c63df65a87411f0b668db015dab8c6f` baseline and its freshly rerun counts
remain under the canonical current heading. The owner-gated SBOM results live in a separate,
explicit candidate section. The parser and its governance boundary did not change.

## Regression test

`tests/unit/test_diagnostics_validation_snapshot.py::test_repository_current_summary_is_parseable`
exercises the real repository document and rejects a malformed current snapshot. Run it with the
feedback-loop command above.

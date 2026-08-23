<!-- Protocol: docs/AGENT_PROTOCOL.md. Delete comments before submitting. -->

## Base

- [ ] Base branch confirmed as the live default: `git ls-remote --symref origin HEAD`
      agrees with this PR's base. (Not stacked; if stacked, the owner has acked and
      the un-strand duty is accepted — docs/AGENT_PROTOCOL.md §3.)

## Task contract

<!-- Canonical definition and field semantics: docs/VISION_COMPLETION_PLAN.md §13 -->

```yaml
plan_phase:
primary_kpi:
gate_advanced:
files:
verification:
evidence_artifact:
owner_gate:
open:
```

## Gates

<!-- Measured at YOUR commit, by YOUR run — never copied. House form:
     Gate: ruff check clean, format clean, mypy clean on <N> source files,
     <N> passed / <N> skipped / 0 failed. -->

## Review

- Requested seat (non-author):
- Verdict (HOLD / PASS, in writing, with evidence — a HOLD is closed only by the
  holder re-verifying in writing):
- Who merges: <!-- owner, for any owner-gate PR — do not self-merge those -->

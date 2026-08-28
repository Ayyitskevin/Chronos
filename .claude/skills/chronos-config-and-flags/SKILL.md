---
name: chronos-config-and-flags
description: 'Diagnose or change Chronos environment settings, CLI flags, risk-policy inputs, and bounded constants. Use for configuration meaning, defaults, validation, wiring, or adding a knob. Differentiator: derives the current surface from live Pydantic models, parser help, examples, consumers, and tests; use chronos-run-and-operate for execution and chronos-change-control for permission to change safety or authority.'
---

# Chronos configuration and flags

A configuration answer is complete only when the checked-out commit proves the
declaration, validation, consumer, and safety effect. Treat this skill as a method,
not a catalog: source, generated help, and tests are the current inventory.

## 1. Establish scope and authority

Read `AGENTS.md`, `docs/AGENT_PROTOCOL.md`, `DECISIONS.md`, the relevant file under
`docs/adr/`, `docs/safety.md`, `docs/limitations.md`, and `RISK_REGISTER.md`. Record
`git rev-parse HEAD`; prior answers and documentation are claims until rechecked.

Classify the requested value before looking it up:

| Surface | Current owner | How to derive it |
|---|---|---|
| Settings-backed environment | `src/chronos/config/settings.py` | Model field, `model_config`, validators, consumers, tests |
| Direct-read environment | The exact `os.environ` / `os.getenv` call | Read site, fallback, parsing, tests |
| Risk policy | `src/chronos/risk/policy.py` plus the selected YAML | Model field, loader, engine consumers, policy tests |
| CLI argument | The parser that owns the entry point | Installed `--help`, parser source, command test |
| Hard limit | The defining source constant | Definition, every read site, invariant or safety test |
| Runtime state or authority | Durable state and its domain module | Route to `chronos-run-and-operate`; a config value alone is not proof |

Configuration is not authority. A field or flag can select a branch while the order,
risk, mandate, reconciliation, and owner gates still refuse the action. Any change
that can affect capital, transmission, account scope, autonomy, promotion evidence,
or a safety ceiling is owner-gated and follows `chronos-change-control` before edits.

## 2. Protect operator data

Never read `.env`; use `.env.example` to inspect the public contract. Never print the process
environment, a `Settings` instance, or `model_dump()` from an operator shell.
Never instantiate `Settings` against the operator environment. Account identifiers,
tokens, file-backed grants, and credential paths are sensitive even when a model field
does not use a secret-specific type.

Class-level Pydantic metadata exposes declarations without resolving dotenv or process
values. Use that for discovery. In tests, `Settings(_env_file=None)` is acceptable only
inside an isolated environment such as pytest `monkeypatch`; it disables dotenv input,
not ordinary process-environment input.

## 3. Derive Settings-backed environment values

First detect the supported Python and Pydantic Settings versions from `pyproject.toml`
and the installed environment:

```bash
rg -n 'requires-python|pydantic-settings' pyproject.toml requirements-dev.lock
.venv/bin/python -c "from importlib.metadata import version; print(version('pydantic-settings'))"
```

Read the declaration and safety validation together:

```bash
sed -n '/^class Settings/,/^@lru_cache/p' src/chronos/config/settings.py
rg -n 'model_config|field_validator|model_validator|<field_name_lower>' \
  src/chronos/config/settings.py tests
```

For a safe, current list of field names, annotations, declared defaults, and aliases,
inspect `Settings.model_fields` on the class:

```bash
.venv/bin/python - <<'PY'
from chronos.config.settings import Settings

for name, field in Settings.model_fields.items():
    print(
        name,
        field.annotation,
        repr(field.default),
        field.validation_alias,
        field.metadata,
    )
PY
```

Check `validation_alias`, `field.metadata`, and `model_config` before deriving an
environment-variable name or parse path. Metadata such as `NoDecode` and
`BeforeValidator` is load-bearing even when the displayed annotation looks ordinary.
Then trace read sites and restart semantics instead of assuming that declaration equals
use:

```bash
rg -n '\.<field_name_lower>\b|get_settings\(' src/chronos scripts tests
rg -n 'os\.(environ|getenv)' src/chronos scripts worker
```

Compare names and safe placeholders with `.env.example`; prove wiring with
`tests/unit/test_settings.py` and
`tests/safety/test_env_example_has_no_phantom_settings.py`. The public example is not
evidence of an operator's configured value. For transmission-related values, inspect
both `transmission_possible` and `live_transmission_possible` plus their order-path
consumers; one field is never the complete authority check.

## 4. Derive direct environment reads and risk policies

For a direct-read environment value, the call site owns its spelling, fallback,
normalization, and timing. Follow its caller and tests; do not promote it into
`Settings` merely to make the surfaces look uniform. For example, bridge environment
inputs are owned by `src/chronos/bridge/config.py`, and model-worker inputs by
`worker/config.py`; `src/chronos/bridge/__main__.py` and `worker/__main__.py` pass
process environment into those loaders.

For risk YAML, inspect the model and loader before the example:

```bash
sed -n '1,220p' src/chronos/risk/policy.py
sed -n '1,220p' config/risk.example.yaml
.venv/bin/python - <<'PY'
from chronos.risk.policy import RiskPolicy

for name, field in RiskPolicy.model_fields.items():
    print(name, field.annotation, repr(field.default))
PY
rg -n 'load_risk_policy|RiskPolicy|<policy_field>' src/chronos tests
```

Identify the exact YAML selected by the caller; `config/risk.example.yaml`, a research
profile, and an owner-authored local policy are different inputs. Derive whether zero,
empty, or missing means deny from the schema and consuming engine. Never transfer that
meaning to a different model such as a mandate without its own source evidence.

## 5. Derive CLI flags from each parser branch

Read `[project.scripts]` in `pyproject.toml` before assuming a console command maps to
a platform CLI. Query generated help for each entry point:

```bash
.venv/bin/python -m chronos.cli --help
.venv/bin/python -m chronos.service --help
.venv/bin/python -m chronos.histdata --help
.venv/bin/python -m chronos.bridge --help
```

The bridge help currently describes command-line arguments only; derive its environment
surface through the direct-read procedure and `src/chronos/bridge/config.py`.

Top-level argparse help does not include a nested subcommand's flags. Descend through
each relevant branch until the leaf command answers:

```bash
.venv/bin/python -m chronos.cli <group> --help
.venv/bin/python -m chronos.cli <group> <subcommand> --help
```

Verify defaults, choices, required arguments, and dispatch in the matching parser:
`src/chronos/cli/main.py`, `src/chronos/service/__main__.py`,
`src/chronos/histdata/__main__.py`, `src/chronos/bridge/__main__.py`, or the owning
script under `scripts/`. Generated help is the interface; parser source and tests prove
what the selected branch executes.

## 6. Trace constants and timing

A capitalized constant is a hard limit only if consumers enforce it. Start from likely
definitions, then trace every reader and test:

```bash
rg -n '^[A-Z_][A-Z0-9_]*\s*=' src/chronos/config/limits.py src/chronos
rg -n '<CONSTANT_NAME>' src/chronos tests
```

Determine whether the value is an input bound, protocol invariant, cache/timer, or
presentation limit. A hard limit is a code change, not an undocumented environment
override. For timing questions, locate construction, cache lifetime, refresh, and
restart behavior; `get_settings()` and its callers are the evidence for when a settings
change can take effect.

## 7. Diagnose one value end to end

Search the exact name and normalized field spelling across source, examples, docs, and
tests. Follow the path in order: declared input -> parser/validator -> in-memory state
-> read sites -> observable refusal or effect. Feedback normally lives in validation
errors, command exit status/help, structured logs, or focused tests; name the concrete
surface for this value.

Return findings in this shape:

```text
State owner: <model, parser, constant, or durable-state module>
Current value source: <declaration plus active precedence; redact operator data>
Declared default: <source-derived value, or none>
Validators and coupled invariants: <source paths and behavior>
Consumers and restart semantics: <read sites and when they observe changes>
Safety classification: <ordinary | operational-care | owner-gated>
Evidence commands: <rerunnable commands against this commit>
Unresolved: <contradictions or facts that require owner confirmation>
```

If source and docs disagree, report the contradiction. Executable behavior wins for
current behavior; accepted ADRs and `DECISIONS.md` remain the authority for intended
design, so a mismatch is work to resolve rather than permission to pick whichever is
convenient.

## 8. Change a configuration axis with red/green evidence

1. Follow `chronos-change-control`; declare the owner, safety effect, callers, tests,
   and intended files. Stop for owner review when the classification is owner-gated.
2. Prove there is one home for the value. Search Settings, direct environment reads,
   parser definitions, policies, constants, and consumers before adding anything.
3. Add a red test that exercises the real input path and observable behavior. For
   Settings parsing, isolate the process environment with pytest `monkeypatch`; for a
   CLI, test the parser/entry point; for YAML, call the real loader.
4. Implement the smallest fail-closed change. Add validation for dangerous
   combinations and a safe placeholder to `.env.example` only when Settings actually
   reads that name.
5. Turn the focused test green, then run the relevant safety coverage:

   ```bash
   .venv/bin/python -m pytest -q tests/unit/test_settings.py \
     tests/safety/test_env_example_has_no_phantom_settings.py
   ```

6. Derive the repository gate from `Makefile`, confirm `.github/workflows/ci.yml`
   invokes the intended surfaces, review the full patch, and run it:

   ```bash
   git diff -- .env.example config src/chronos/config src/chronos/risk src/chronos/cli \
     src/chronos/service src/chronos/histdata src/chronos/bridge scripts worker tests
   make gates
   ```

7. Re-run the live derivation commands. The answer, examples, generated help, tests,
   and implementation must agree before the change is complete.

## Known pitfalls

- Pydantic Settings source precedence matters. `_env_file=None` removes dotenv from
  consideration but leaves process environment active; use class metadata for read-only
  discovery and isolated tests for instantiation.
- `extra`, aliases, case sensitivity, and complex-value decoding can change whether a
  plausible environment name is accepted, rejected, or ignored. `field.annotation`
  alone omits metadata such as `NoDecode` and `BeforeValidator`; derive each behavior
  from the current `model_config`, `field.metadata`, and validators.
- `.env.example` is a safe template, not an exhaustive source of truth and never proof
  of a running value. Direct-read environment variables may live outside `Settings`.
- Argparse prints help one parser at a time. Inspect every nested subcommand needed for
  the task; a top-level inventory silently omits leaf flags.
- A declared default, an operator-configured value, and runtime authority are three
  different facts. Report them separately.
- Zero and empty values do not share one universal meaning across Settings, risk policy,
  mandates, and runtime state. Trace the consumer before calling either deny or unbound.
- A tunable-looking constant may encode a safety or evidence invariant. Route its change
  through `chronos-change-control` instead of inventing a flag.

## Primary API references

- Pydantic Settings usage and environment-source behavior:
  https://docs.pydantic.dev/latest/concepts/pydantic_settings/#usage
- Pydantic class-level field metadata:
  https://docs.pydantic.dev/latest/api/base_model/#pydantic.BaseModel.model_fields
- Python argparse subcommands and per-parser help:
  https://docs.python.org/3.12/library/argparse.html#sub-commands

# Release-artifact validation — 2026-08-27

## Question

Does the repository's CI prove that the artifact an operator would install contains and exposes
Chronos's supported runtime surfaces, rather than proving only that an editable checkout works?

## Scope and acceptance seam

This is an owner-independent slice of the Phase 2 package/release work. The public seams are:

1. build the current non-ignored source as one wheel in an isolated tree;
2. install that wheel with the hash-locked dependency closure in a fresh venv outside the checkout;
3. import Chronos only from that environment and load its `chronos` console entry point;
4. verify terminal assets and the complete migration namespace equal their source bytes;
5. load the installed Alembic graph at its single expected head; and
6. build a disposable v2 database, stamp its no-op `0001` baseline, execute the installed
   migration tree through head, and require Chronos's installed schema-drift checker to accept the
   result; and
7. discover every packaged `src/chronos/**/__main__.py` command surface and run its `--help` from
   the installed wheel.

The gate uses setuptools package-data declarations for non-Python files, following the
[setuptools package-data contract](https://setuptools.pypa.io/en/stable/userguide/datafiles.html#package-data).
It materializes the installed migration resource as a filesystem directory for Alembic using
Python 3.12's documented
[`importlib.resources.as_file`](https://docs.python.org/3.12/library/importlib.resources.html#importlib.resources.as_file)
directory support. Migration execution uses Alembic's documented
[`Config` plus programmatic command API](https://alembic.sqlalchemy.org/en/1.18.5/api/commands.html).

## Red-to-green evidence

Before the package-data correction, the new verifier built the existing project successfully and
then refused the artifact:

```text
ReleaseArtifactError: wheel is missing required members:
['chronos/persistence/migrations/script.py.mako']
```

That failure establishes that the gate can detect the packaging defect it is meant to prevent.
The Python migrations and terminal assets were already present because setuptools discovers the
implicit namespace and the terminal data already had an explicit declaration. The smallest product
change was therefore to package Alembic's non-Python revision template, not to replace package
discovery.

After that correction, the isolated verifier reported:

```text
Installed artifact smoke passed: package origin, console entry point, 3 terminal assets,
migration head 0010, and 2 module entry points.
Release artifact gate passed for chronos-0.1.0-py3-none-any.whl.
```

The executable gate is `scripts/verify_release_artifact.py`; both `make release-gate` and the
GitHub Actions `Validate release artifact` step call it directly.

## Review follow-up — 2026-08-28

Independent review found that the first gate hard-coded both its three terminal filenames and two
of the four packaged `src/chronos/**/__main__.py` command surfaces. A new static-file extension
could therefore be omitted from the wheel while the gate stayed green, and the bridge and
historical-data commands were not exercised. The follow-up makes the source tree the inventory for
both surfaces, broadens the setuptools declaration to terminal files of any non-hidden type at any
depth, and makes `python -m chronos.bridge --help` exit before credentials are read. Ordinary bridge
startup still refuses a missing secret.

The adversarial static probe first reproduced the blind spot against the old declaration, then
made the source-driven verifier refuse the wheel:

```text
ReleaseArtifactError: wheel is missing required members:
['chronos/terminal/static/release-gate-probe.svg']
```

With the broadened declaration, the same probe passed and the installed smoke reported four
terminal assets and all four module entry points. The disposable probe was then removed; focused
tests retain the recursive-inventory cases.

Local and CI source sets intentionally differ. A local run copies tracked and untracked,
non-ignored files so a developer can validate the complete working tree before committing. CI
checks a clean checkout, so its result is authoritative for the committed revision. A local green
result is evidence about the current working tree, not by itself about any commit.

## Installed-wheel migration execution — 2026-08-28

The first focused test failed during collection because the release verifier had no migration
execution seam. The implementation now creates only the frozen v2 baseline tables, records schema
version 2, stamps Alembic revision `0001`, and upgrades to `head` using the migration directory
materialized from the installed package. It then calls `Database.initialize()` from that installed
package; the drill fails on a wrong Chronos schema version or any table, column, type, nullability,
primary-key, unique-constraint, foreign-key, or index drift.

The drill replaces any ambient `DATABASE_URL` with its disposable SQLite URL for the two Alembic
commands and restores the original value even when a revision raises. A guard test copies the
migration tree, injects a sentinel exception into the `0010` upgrade body, and observes that exact
exception. This distinguishes executing revisions from merely loading their graph.

Exact-SHA review of the first implementation passed but found that focused tests supplied a
migration path directly, so they did not pin the installed-smoke resource wiring. The installed
wrapper now accepts only a disposable database path and resolves
`chronos.persistence.migrations` itself through `importlib.resources`; a boundary test substitutes
a sentinel package resource and proves that tree executes. A second mutation adds an unexpected
table while still reaching `0010` and proves the installed `Database.initialize()` drift check
refuses it. The duplicated v2 table-name manifest remains intentional: this gate carries an
independent frozen contract rather than deriving its expected legacy shape from the pytest check.

The isolated installed-wheel gate now reports:

```text
Installed artifact smoke passed: package origin, console entry point, 3 terminal assets,
migration head 0010 after a v2 upgrade across 34 model tables, and 4 module entry points.
Release artifact gate passed for chronos-0.1.0-py3-none-any.whl.
```

## Hash-locked build backend — 2026-08-28

The prior wheel command let pip create an isolated environment and satisfy
`setuptools>=75` from the network. The artifact could therefore change build backends while every
runtime lock and test stayed unchanged. A focused test first failed because the verifier had no
locked build seam; a second test then reproduced that CI's editable install still used isolation.

`pyproject.toml` and `requirements-build.in` now name the same exact backend requirement,
`setuptools==84.0.0`. The generated `requirements-build.lock` carries both published SHA-256
digests. The release verifier installs that lock with `--require-hashes`, then runs `pip wheel`
with `--no-build-isolation --check-build-dependencies`; CI uses the same locked backend and flags
for its editable install. The preceding `--require-hashes` install verifies artifact integrity;
`--check-build-dependencies` only verifies that the installed backend version satisfies the
project requirement. The real release gate passed from a fresh venv with this sequence. This
follows pip's documented rule that disabling build isolation makes the caller responsible for
preinstalling build dependencies:
[pip build-system interface](https://pip.pypa.io/en/stable/reference/build-system/#disabling-build-isolation).

A separate clean-venv probe replayed CI's install order—build lock, runtime/dev lock, then editable
Chronos with isolation disabled—and reported `setuptools=84.0.0` and `chronos=0.1.0`. The temporary
probe environment was moved to the system trash after verification.

Only setuptools is locked because modern setuptools supplies its own wheel-building command; the
separate `wheel` CLI package is not required by this project. The exact version and hashes come
from the corresponding [setuptools PyPI release](https://pypi.org/project/setuptools/84.0.0/).

This supply-chain control is security-sensitive and remains owner-gated: the branch may be tested
and independently reviewed, but only the owner may approve and merge it.

## What this does not prove

The artifact gate now executes the supported v2-baseline-to-head path from the installed wheel. It
does not make v1 upgradable, change the deliberate `create_all` path for a brand-new database, or
exercise the separately owned platform ledger, which has no migration tooling. Like the existing
integration fixture, it freezes the v2 table names but creates those tables from current installed
metadata; it therefore does not independently catch a missing column-alteration revision for a v2
table. That disclosed migration-completeness limit remains unchanged. The gate also does not add
dependency, secret, or static-analysis scanners, emit an SBOM, or sign the wheel. The pip frontend
that bootstraps and invokes the locked backend remains outside the hash lock, and an exact backend
does not make timestamp-bearing wheel ZIP bytes reproducible. Those remain separate Phase 2 work
or disclosed limits, and no broker, account, credential, order path, market-data capture, or
holdout was accessed by this evaluation.

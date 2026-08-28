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
6. run `python -m chronos.cli --help` and `python -m chronos.service --help` from the installed
   wheel.

The gate uses setuptools package-data declarations for non-Python files, following the
[setuptools package-data contract](https://setuptools.pypa.io/en/stable/userguide/datafiles.html#package-data).
It materializes the installed migration resource as a filesystem directory for Alembic using
Python 3.12's documented
[`importlib.resources.as_file`](https://docs.python.org/3.12/library/importlib.resources.html#importlib.resources.as_file)
directory support.

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

## What this does not prove

The ordinary test suite executes historical-schema-to-head migrations and checks schema drift;
the artifact gate proves those exact revision bytes ship and their installed graph loads, but it
does not yet execute an upgrade from an installed wheel. It also does not add dependency, secret,
or static-analysis scanners, pin the PEP 517 build backend, emit an SBOM, or sign the wheel. Those
remain separate Phase 2 work, and no broker, account, credential, order path, market-data capture,
or holdout was accessed by this evaluation.

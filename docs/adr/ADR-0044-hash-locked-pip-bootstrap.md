# ADR-0044 — The pip frontend is exact after one disclosed interpreter bootstrap

Status: **accepted design — owner authorized autonomous merge for this resumed sequence on
2026-08-29. This changes build observation only; it grants no runtime or trading authority.**
Index entry: DECISIONS.md D-58.

## Context

Chronos hash-locks its runtime, development, build-backend, and SBOM-tool domains, then measures
wheel reproducibility and validates the installed runtime inventory. One input still varied before
all of those controls: CI upgraded pip without a version bound, while the release verifier used
whatever pip Python bundled into each fresh virtual environment.

Python's `venv.EnvBuilder(with_pip=True)` invokes the interpreter's offline `ensurepip` bundle.
That bundle is useful as a bootstrap mechanism, but its pip version follows the selected Python
distribution rather than a Chronos-reviewed identity. Because pip consumes every later lock and
invokes the build frontend, its version is part of the release environment.

## Decision

### 1. One dedicated lock owns the post-bootstrap frontend

`requirements-bootstrap.in` contains exactly one exact pip requirement. Its uv-generated lock
contains the published wheel and source-distribution SHA-256 hashes. The interpreter-bundled pip
may perform one operation before verification: install that requirement with `--no-deps` and
`--require-hashes` from the dedicated lock.

CI uses the same `python -m pip` interpreter binding for this and every later pip command. The
release verifier performs the sequence independently in its fresh builder and runtime venvs before
installing any backend, runtime, wheel, or SBOM-tool input.

### 2. Installation and identity verification are distinct steps

`scripts/verify_pip_bootstrap.py` is stdlib-only. Before installation it requires the lock to
contain exactly one `pip==...` requirement and refuses unsupported lines; this prevents pip from
consuming a broadened requirements file before the check. After installation it compares that
version with `importlib.metadata.version("pip")` in the target interpreter. CI and the release gate
invoke both checks with isolated Python. Missing pip, inexact/additional lock requirements, or
installed-version drift fails closed.

The release security gate also refuses a task environment whose pip version differs from the
bootstrap identity. The runtime SBOM now requires that exact pip component alongside every runtime
lock component and refuses previously tolerated unlocked bootstrap tools.

### 3. The trust claim remains bounded

The exact downloaded pip artifacts and installed version are now measured inputs. This does not
hash or authenticate the Python interpreter, the offline `ensurepip` bundle that executes the
self-replacement, the operating system, package-index response, package publisher, source tree, or
builder. SHA-256 equality provides artifact identity and transport integrity, not publisher or
builder provenance. pip is not added as a Chronos runtime dependency, and the runtime advisory scan
continues to assess the application lock rather than silently widening its domain.

No broker, gateway, credential, account, capital, database schema, deployment tree, trading mode,
order path, or external notification is read or changed.

## Consequences

An upstream pip release can no longer enter CI merely because it became latest, and Python patch
distributions with different bundled pip versions converge on the reviewed frontend before they
consume Chronos locks. Updating pip is now an explicit input+lock diff with exact hashes, focused
tests, full gates, hosted candidate CI, and independent review.

Interpreter/`ensurepip` trust, malicious-package and publisher provenance, artifact signing,
independent rebuild attestations, cross-platform verification, and Git-history secret review remain
explicit Phase 2 residuals. This decision alone does not satisfy the Phase 2 exit.

## Sources

- [Python 3.12 `ensurepip`](https://docs.python.org/3.12/library/ensurepip.html) — offline bootstrap
  behavior and the interpreter-bundled pip release cycle.
- [Python 3.12 `venv.EnvBuilder`](https://docs.python.org/3.12/library/venv.html) — `with_pip=True`
  invokes `ensurepip --default-pip`.
- [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/) — exact pins plus
  `--require-hashes` for a hash-checking install mode.
- [pip install](https://pip.pypa.io/en/stable/cli/pip_install/) — interpreter-bound
  `python -m pip`, exact requirements, and requirements-file installation.
- [pip 26.2.1 on PyPI](https://pypi.org/project/pip/26.2.1/) — release files, SHA-256 hashes, Python
  compatibility, and Trusted Publishing provenance supplied by the package index.

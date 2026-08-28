# Diagnosis: Streamlit AppTest entrypoints depended on a removed cwd fallback

## Symptom

The baseline `make gates` run failed 13 Streamlit integration tests with
`FileNotFoundError`. Each `AppTest.from_file` call supplied a repository-root-relative path,
but Streamlit 1.62 resolved it below `tests/integration/`, producing paths such as
`tests/integration/src/chronos/ui/backend_app.py`.

## Ranked hypotheses

1. The installed Streamlit release used caller-file-relative semantics while the tests
   supplied working-directory-relative paths.
2. The environment had drifted from the lock file and exposed an undocumented fallback on
   which the tests depended.
3. A pytest plugin or working-directory change altered path resolution.

## Evidence and root cause

The smallest reproducer was:

```text
.venv/bin/pytest -q tests/integration/test_backend_ui_pages.py::test_portfolio_renders_account_metrics
```

Before the correction it failed in 0.48 seconds because `_APP` resolved beneath the calling
test module. The environment had Streamlit 1.62.0 installed, which is permitted by
`pyproject.toml` (`streamlit>=1.40,<2`) even though `requirements-dev.lock` pins 1.59.2.

Streamlit's [AppTest documentation](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
defines a relative `from_file` path relative to the calling file. Streamlit 1.59.2's
[`AppTest.from_file` implementation](https://github.com/streamlit/streamlit/blob/1.59.2/lib/streamlit/testing/v1/app_test.py)
first accepted a path that happened to exist relative to the process working directory, then
fell back to caller-relative resolution. The tests depended on that undocumented first branch;
Streamlit 1.62 consistently applies the documented caller-relative behavior.

The defect was therefore test-harness path construction, not application rendering, pytest
configuration, or a reason to narrow the declared Streamlit range.

## Correction and regression proof

The three affected modules now derive absolute app entrypoints from
`Path(__file__).resolve().parents[2]`. This is independent of process working directory and
works under both the locked release and newer releases allowed by the project metadata.

The original reproducer passes:

```text
1 passed in 0.44s
```

The complete affected surface passes:

```text
.venv/bin/pytest -q tests/integration/test_backend_ui_pages.py \
  tests/integration/test_monitor_streamlit_app.py \
  tests/integration/test_streamlit_app.py
16 passed in 5.65s
```

No runtime, broker, order, financial, or authority code changed.

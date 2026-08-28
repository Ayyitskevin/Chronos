"""The read-only platform-monitor Streamlit page renders without exceptions.

Unlike the wheel dashboard, this page must never reach a broker: it renders a
:class:`~chronos.monitoring.snapshot.MonitoringSnapshot` built from files on
disk. The test drives it through Streamlit's ``AppTest`` harness with all
configuration supplied by environment variables pointing at a temp directory.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from chronos.control.halt import HaltReason, HaltStore

_APP = Path(__file__).resolve().parents[2] / "src/chronos/monitoring/streamlit_app.py"


# Streamlit ``AppTest`` timeout budget (raised 2026-08-08).
#
# The FIRST render in a process that reaches ``st.dataframe`` pays a one-time
# serialization-stack import. Measured 2026-08-08: a monitor render with data
# took 9.07s as the first in its process and 0.28s as the second, while the same
# app with no data rendered in 1.13s cold. The domain logic is not the cost —
# ``build_snapshot`` returns in under 0.01s in every configuration tested.
#
# That cost lands on whichever AppTest runs first, so a tight timeout is an
# ORDER-DEPENDENT flake rather than a property of any one test. The monitor
# suite's 15s budget gave 1.65x headroom over a 9s cold render and failed once in
# six full-suite runs under CPU contention.
#
# 60s does not make a passing test slower — a warm render finishes in well under
# a second. It is headroom, so that a green suite means "the page rendered" and
# never "the machine happened to be quiet". The timeout exists to stop a hung
# render blocking forever, and 60s serves that as well as 10s did.
_APPTEST_TIMEOUT = 60


def test_monitor_page_renders_halted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).halt(HaltReason.RECONCILIATION_MISMATCH, "startup mismatch")
    data = tmp_path / "data"
    data.mkdir()
    (data / "SPY.csv").write_text(
        "date,open,high,low,close,volume\n2024-01-02,100,101,99,100.5,1000\n"
    )
    monkeypatch.setenv("CHRONOS_MONITOR_MODE", "shadow")
    monkeypatch.setenv("CHRONOS_HALT_FILE", str(halt))
    monkeypatch.setenv("CHRONOS_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("CHRONOS_RISK_POLICY", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("CHRONOS_DATA_DIR", str(data))
    monkeypatch.setenv("CHRONOS_MONITOR_SYMBOLS", "SPY,QQQ")

    app = AppTest.from_file(_APP).run(timeout=_APPTEST_TIMEOUT)

    assert not app.exception
    assert [title.value for title in app.title] == ["Chronos Platform Monitor"]
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Mode"] == "SHADOW"
    assert metrics["Capability"] == "NO_ORDERS"
    # A HALTED platform must surface a red error banner, never colour alone.
    assert any("HALTED" in error.value for error in app.error)


def test_monitor_page_renders_armed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    halt = tmp_path / "halt.json"
    HaltStore(halt).rearm("ready")
    monkeypatch.setenv("CHRONOS_MONITOR_MODE", "shadow")
    monkeypatch.setenv("CHRONOS_HALT_FILE", str(halt))
    monkeypatch.setenv("CHRONOS_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("CHRONOS_DATA_DIR", raising=False)

    app = AppTest.from_file(_APP).run(timeout=_APPTEST_TIMEOUT)

    assert not app.exception
    # Live is hard-disabled: the success banner states it, and no error fires.
    assert any("hard-disabled" in success.value for success in app.success)

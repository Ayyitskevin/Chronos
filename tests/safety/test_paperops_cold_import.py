"""Cold-import and CLI entry must work without circular imports.

Criterion 1 of the paperops pipeline wire: a cold reader can open the ledger
and run paperops verify/replay/review. Package import order luck must not hide
an ImportError that breaks the operator CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_cold_import_chronos_paperops_in_subprocess() -> None:
    """Fresh interpreter: import chronos.paperops without prior module state."""

    probe = (
        "import chronos.paperops as p; "
        "from chronos.paperops.ledger import verify_decision_ledger, DecisionLedger; "
        "from chronos.paperops.review import build_operator_review; "
        "from chronos.paperops.pipeline import PipelineRecorder; "
        "print('OK', p.DecisionLedger is DecisionLedger, "
        "callable(verify_decision_ledger), PipelineRecorder is not None)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        check=False,
    )
    assert result.returncode == 0, (
        f"cold import failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
    assert "ImportError" not in result.stderr
    assert "circular import" not in result.stderr.lower()


def test_cli_paperops_verify_review_replay_subprocess(tmp_path: Path) -> None:
    """Operator CLI entry points must start without ImportError."""

    ledger = tmp_path / "fixture.jsonl"
    ledger.write_text("", encoding="utf-8")
    # Hermetic empty sqlite so audit does not depend on host DATABASE_URL.
    db_url = f"sqlite:///{tmp_path / 'audit.db'}"

    for subcmd in ("verify", "review", "replay", "audit"):
        cmd = [
            sys.executable,
            "-m",
            "chronos.cli",
            "paperops",
            subcmd,
            "--ledger",
            str(ledger),
        ]
        if subcmd == "audit":
            cmd.extend(["--database", db_url])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_REPO),
            check=False,
        )
        assert result.returncode in {0, 1}, (
            f"paperops {subcmd} crashed (rc={result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "ImportError" not in combined
        assert "circular import" not in combined.lower()
        # CLI may exit 1 on empty/incomplete ledger (replay/audit); no traceback.
        if result.returncode != 0:
            assert "Traceback" not in result.stderr


def test_paperops_package_init_does_not_export_pipeline() -> None:
    """Structural guard: package __init__ must not eager-import pipeline."""

    init_path = _REPO / "src" / "chronos" / "paperops" / "__init__.py"
    text = init_path.read_text(encoding="utf-8")
    assert "from chronos.paperops.pipeline import" not in text
    # Direct submodule import remains the supported path for the adapter.
    import chronos.paperops as pkg

    assert not hasattr(pkg, "PipelineRecorder")

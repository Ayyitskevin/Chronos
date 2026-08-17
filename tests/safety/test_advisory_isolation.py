"""Advisory facts stay research-exported and autonomy-validated, never mixed."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from chronos.autonomy import evidence as ev

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AUTONOMY = _REPO_ROOT / "src" / "chronos" / "autonomy"
_FEATURES = _REPO_ROOT / "src" / "chronos" / "research" / "features"

_ECONOMIC_FIELDS = {
    "account_id",
    "password",
    "api_key",
    "token",
    "credential",
    "path",
    "requested_quantity",
    "quantity",
    "protective_stop",
    "transmit",
}


def _imported_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_autonomy_does_not_import_research_features() -> None:
    for path in sorted(_AUTONOMY.glob("*.py")):
        for name in _imported_names(path.read_text(encoding="utf-8")):
            assert not name.startswith("chronos.research"), (
                f"{path.name} imports {name}; advisory facts must be autonomy-native"
            )


def test_importing_autonomy_does_not_load_research() -> None:
    probe = (
        "import chronos.autonomy, sys; "
        "bad=[m for m in sys.modules if m.startswith('chronos.research')]; "
        "print(';'.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_advisory_models_have_no_economic_or_secret_fields() -> None:
    models = (
        ev.AdvisoryDatum,
        ev.AdvisoryFiveToolFact,
        ev.AdvisoryFeatureSnapshotFact,
        ev.AdvisoryVetoFact,
        ev.EvidenceBundle,
    )
    fields: set[str] = set()
    for model in models:
        fields |= set(model.model_fields)
    for forbidden in _ECONOMIC_FIELDS:
        assert forbidden not in fields


def test_shadow_journal_does_not_import_orders_or_broker() -> None:
    source = (_AUTONOMY / "shadow_journal.py").read_text(encoding="utf-8")
    imported = _imported_names(source)
    for forbidden in ("chronos.orders", "chronos.broker", "chronos.execution"):
        assert all(not name.startswith(forbidden) for name in imported)


def test_feature_export_still_cannot_import_autonomy() -> None:
    source = (_FEATURES / "advisory_export.py").read_text(encoding="utf-8")
    imported = _imported_names(source)
    assert all(not name.startswith("chronos.autonomy") for name in imported)

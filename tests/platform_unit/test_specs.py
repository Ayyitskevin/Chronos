"""Strategy specification schema: round-trip, unknown keys, duplicate ids."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chronos.specs.schema import load_spec, load_specs_dir

VALID_SPEC_YAML = """\
strategy_id: demo_strategy_v1
version: "1.0.0"
family: trend
description: minimal valid spec for unit tests
supported_symbols: [SPY, QQQ]
supported_intervals: ["1d"]
required_lookback_bars: 50
signal_logic: EMA cross gated by regime
entry_rules:
  - fast above slow
exit_rules:
  - fast below slow
stop_logic: 2x ATR(14) below entry
sizing_logic: fixed fraction of equity
parameters:
  - name: fast_len
    value: 20
    minimum: 5
    maximum: 50
    description: fast EMA length
status: draft
"""


class TestLoadSpec:
    def test_valid_spec_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "demo.yaml"
        path.write_text(VALID_SPEC_YAML, encoding="utf-8")
        spec = load_spec(path)
        assert spec.strategy_id == "demo_strategy_v1"
        assert spec.version == "1.0.0"
        assert spec.family == "trend"
        assert spec.supported_symbols == ("SPY", "QQQ")
        assert spec.supported_intervals == ("1d",)
        assert spec.required_lookback_bars == 50
        assert spec.entry_rules == ("fast above slow",)
        assert spec.exit_rules == ("fast below slow",)
        assert len(spec.parameters) == 1
        assert spec.parameters[0].name == "fast_len"
        assert spec.parameters[0].value == 20
        # Defaults applied for omitted optional fields.
        assert spec.long_only is True
        assert spec.owner_approved is False
        assert spec.cooldown_bars == 0
        assert spec.status == "draft"

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "demo.yaml"
        path.write_text(VALID_SPEC_YAML + "mystery_knob: 42\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_spec(path)

    def test_non_mapping_spec_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "demo.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a mapping"):
            load_spec(path)

    def test_invalid_status_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "demo.yaml"
        path.write_text(
            VALID_SPEC_YAML.replace("status: draft", "status: yolo_live"), encoding="utf-8"
        )
        with pytest.raises(ValidationError):
            load_spec(path)


class TestLoadSpecsDir:
    def test_directory_load_keys_by_strategy_id(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(VALID_SPEC_YAML, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(
            VALID_SPEC_YAML.replace("demo_strategy_v1", "other_strategy_v1"),
            encoding="utf-8",
        )
        specs = load_specs_dir(tmp_path)
        assert set(specs) == {"demo_strategy_v1", "other_strategy_v1"}

    def test_duplicate_strategy_id_raises(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(VALID_SPEC_YAML, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(VALID_SPEC_YAML, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate strategy_id demo_strategy_v1"):
            load_specs_dir(tmp_path)

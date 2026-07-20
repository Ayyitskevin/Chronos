"""Deterministic research-run produce / replay / compare (Prompt 2)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.research.repro import (
    SCHEMA_VERSION,
    CompareReason,
    CompareStatus,
    ReproError,
    compare_manifests,
    load_manifest,
    normalize_timezone,
    produce_named_backtest_run,
    redact_secrets,
    replay_from_manifest,
    stable_hash,
    verify_datasets,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_POLICY = REPO_ROOT / "config" / "risk.research.yaml"


def _git_rev_parse(revision: str, *, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_csv(
    data_dir: Path,
    symbol: str,
    n_bars: int = 80,
    start: date = date(2019, 1, 2),
) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,volume"]
    session = start
    prev_close = 100.0
    for i in range(n_bars):
        while session.weekday() >= 5:
            session += timedelta(days=1)
        close = 100.0 + 0.25 * i
        open_ = prev_close
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        lines.append(f"{session.isoformat()},{open_},{high},{low},{close},1000000")
        prev_close = close
        session += timedelta(days=1)
    path = data_dir / f"{symbol}.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_stable_hash_and_redaction_are_deterministic() -> None:
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})
    raw = {
        "symbol": "SPY",
        "api_key": "sk-live-should-never-appear",
        "nested": {"password": "hunter2", "ok": 1},
        "IB_ACCOUNT_ID": "U1234567",
    }
    redacted = redact_secrets(raw)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["IB_ACCOUNT_ID"] == "[REDACTED]"
    assert redacted["nested"]["ok"] == 1
    assert redacted["symbol"] == "SPY"
    blob = json.dumps(redacted)
    assert "sk-live" not in blob
    assert "hunter2" not in blob
    assert "U1234567" not in blob


def test_timezone_normalizes_to_utc_only() -> None:
    assert normalize_timezone(None) == "UTC"
    assert normalize_timezone("utc") == "UTC"
    assert normalize_timezone("Z") == "UTC"
    assert normalize_timezone("Etc/UTC") == "UTC"
    with pytest.raises(ReproError) as exc:
        normalize_timezone("America/New_York")
    assert exc.value.reason is CompareReason.TIMEZONE_DRIFT


def test_identical_inputs_match_on_produce_replay_compare(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    run_a = tmp_path / "run_a"
    run_b = tmp_path / "run_b"
    m1 = produce_named_backtest_run(
        run_dir=run_a,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        initial_cash=3000.0,
        slippage_bps=0.0,
        date_start="2019-01-02",
        date_end="2019-04-01",
        seed=7,
    )
    m2 = produce_named_backtest_run(
        run_dir=run_b,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        initial_cash=3000.0,
        slippage_bps=0.0,
        date_start="2019-01-02",
        date_end="2019-04-01",
        seed=7,
        code_commit=m1["code_commit"],
    )
    assert m1["schema_version"] == SCHEMA_VERSION
    assert m1["output_fingerprint"] == m2["output_fingerprint"]
    assert m1["config_hash"] == m2["config_hash"]
    assert m1["datasets"][0]["sha256"] == m2["datasets"][0]["sha256"]
    # produced_at_utc may differ; identity fingerprint excludes it via identity payload
    # but includes outputs — same outputs → same manifest_fingerprint when commit pinned.
    assert m1["manifest_fingerprint"] == m2["manifest_fingerprint"]

    replay_dir = tmp_path / "replay"
    m3 = replay_from_manifest(m1, run_dir=replay_dir, data_dir=data, policy_path=RESEARCH_POLICY)
    report = compare_manifests(m1, m3)
    assert report.ok is True
    assert report.status is CompareStatus.PASS
    assert CompareReason.MATCH in report.reasons or not [
        r for r in report.reasons if r is not CompareReason.COMMIT_DRIFT
    ]


def test_produce_records_chronos_commit_when_cwd_is_an_external_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external-repo"
    external.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=external, check=True)
    (external / "README.md").write_text("external\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=external, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Repro Test",
            "-c",
            "user.email=repro@example.invalid",
            "commit",
            "-qm",
            "external commit",
        ],
        cwd=external,
        check=True,
    )
    external_commit = _git_rev_parse("HEAD", cwd=external)
    chronos_commit = _git_rev_parse("HEAD")
    assert external_commit != chronos_commit

    data = tmp_path / "data"
    _write_csv(data, "SPY")
    monkeypatch.chdir(external)
    manifest = produce_named_backtest_run(
        run_dir=tmp_path / "run",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )

    assert manifest["code_commit"] == chronos_commit
    assert manifest["code_commit"] != external_commit


def test_replay_does_not_forward_original_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    original = produce_named_backtest_run(
        run_dir=tmp_path / "original",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )
    captured: dict[str, object] = {}

    def fake_produce(**kwargs: object) -> dict[str, str]:
        captured.update(kwargs)
        return {"code_commit": _git_rev_parse("HEAD")}

    monkeypatch.setattr(
        "chronos.research.repro.produce_named_backtest_run",
        fake_produce,
    )
    replayed = replay_from_manifest(
        original,
        run_dir=tmp_path / "replayed",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )

    assert captured["code_commit"] is None
    assert replayed["code_commit"] == _git_rev_parse("HEAD")


@pytest.mark.parametrize("bad_commit", ["not-a-sha", "f" * 40])
def test_manifest_rejects_invalid_or_unresolvable_commit(tmp_path: Path, bad_commit: str) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    manifest = produce_named_backtest_run(
        run_dir=tmp_path / "original",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )
    manifest["code_commit"] = bad_commit

    with pytest.raises(ReproError) as exc:
        write_manifest(manifest, tmp_path / "invalid.json")

    assert exc.value.reason is CompareReason.INCOMPLETE_MANIFEST


def test_compare_rejects_stale_output_fingerprint_after_output_mutation(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    expected = produce_named_backtest_run(
        run_dir=tmp_path / "expected",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )
    tampered = json.loads(json.dumps(expected))
    tampered["outputs"]["bars"] += 1

    report = compare_manifests(expected, tampered)

    assert report.ok is False
    assert CompareReason.CHECKSUM_DRIFT in report.reasons


def test_load_manifest_rejects_mutated_output_artifact(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    run_dir = tmp_path / "run"
    produce_named_backtest_run(
        run_dir=run_dir,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
    )
    output_path = run_dir / "output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["bars"] += 1
    output_path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(ReproError) as exc:
        load_manifest(run_dir / "manifest.json")

    assert exc.value.reason is CompareReason.CHECKSUM_DRIFT


@pytest.mark.parametrize("dataset_id_kind", ["traversal", "absolute"])
def test_verify_datasets_rejects_dataset_id_escape(tmp_path: Path, dataset_id_kind: str) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    dataset_id = "../outside.csv" if dataset_id_kind == "traversal" else str(outside)
    manifest = {
        "datasets": [
            {
                "dataset_id": dataset_id,
                "symbol": "",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ]
    }

    with pytest.raises(ReproError) as exc:
        verify_datasets(manifest, data_dir=data_root)

    assert exc.value.reason is CompareReason.INCOMPLETE_MANIFEST


@pytest.mark.parametrize("recorded_path_kind", ["traversal", "absolute"])
def test_verify_datasets_rejects_recorded_path_escape(
    tmp_path: Path, recorded_path_kind: str
) -> None:
    base_dir = tmp_path / "bundle"
    base_dir.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    recorded_path = "../outside.csv" if recorded_path_kind == "traversal" else str(outside)
    manifest = {
        "datasets": [
            {
                "dataset_id": "SPY.csv",
                "symbol": "SPY",
                "path": recorded_path,
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ]
    }

    with pytest.raises(ReproError) as exc:
        verify_datasets(manifest, base_dir=base_dir)

    assert exc.value.reason is CompareReason.INCOMPLETE_MANIFEST


def test_verify_datasets_rejects_symlink_escape(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    (data_root / "SPY.csv").symlink_to(outside)
    manifest = {
        "datasets": [
            {
                "dataset_id": "SPY.csv",
                "symbol": "SPY",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ]
    }

    with pytest.raises(ReproError) as exc:
        verify_datasets(manifest, data_dir=data_root)

    assert exc.value.reason is CompareReason.INCOMPLETE_MANIFEST


def test_data_checksum_drift_fails_clearly(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    run_a = tmp_path / "a"
    m1 = produce_named_backtest_run(
        run_dir=run_a,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        seed=1,
    )
    # Mutate CSV content after the original run.
    path = data / "SPY.csv"
    path.write_text(path.read_text(encoding="utf-8") + "2099-01-01,1,1,1,1,1\n", encoding="utf-8")
    run_b = tmp_path / "b"
    m2 = produce_named_backtest_run(
        run_dir=run_b,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        seed=1,
        code_commit=m1["code_commit"],
    )
    report = compare_manifests(m1, m2)
    assert report.ok is False
    assert CompareReason.CHECKSUM_DRIFT in report.reasons


def test_config_and_seed_drift_fail_clearly(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY")
    base_kwargs = dict(
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        seed=3,
        initial_cash=3000.0,
        slippage_bps=0.0,
    )
    m1 = produce_named_backtest_run(run_dir=tmp_path / "c1", **base_kwargs)  # type: ignore[arg-type]
    m_seed = produce_named_backtest_run(
        run_dir=tmp_path / "c2",
        code_commit=m1["code_commit"],
        **{**base_kwargs, "seed": 99},  # type: ignore[arg-type]
    )
    seed_report = compare_manifests(m1, m_seed)
    assert seed_report.ok is False
    assert CompareReason.SEED_DRIFT in seed_report.reasons

    m_cfg = produce_named_backtest_run(
        run_dir=tmp_path / "c3",
        code_commit=m1["code_commit"],
        **{**base_kwargs, "initial_cash": 5000.0},  # type: ignore[arg-type]
    )
    cfg_report = compare_manifests(m1, m_cfg)
    assert cfg_report.ok is False
    assert CompareReason.CONFIG_DRIFT in cfg_report.reasons


def test_incomplete_and_legacy_manifests_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"strategy": "x", "metrics": {}}), encoding="utf-8")
    with pytest.raises(ReproError) as legacy:
        load_manifest(path)
    assert legacy.value.reason is CompareReason.UNSUPPORTED_LEGACY

    incomplete = {
        "schema_version": SCHEMA_VERSION,
        "code_commit": "abc",
        # missing almost everything
    }
    bad = tmp_path / "incomplete.json"
    bad.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(ReproError) as inc:
        load_manifest(bad)
    assert inc.value.reason is CompareReason.INCOMPLETE_MANIFEST


def test_secrets_never_land_in_written_manifest(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", n_bars=40)
    run_dir = tmp_path / "secure"
    manifest = produce_named_backtest_run(
        run_dir=run_dir,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        seed=0,
    )
    # Inject a secret-like key into config and re-write — must redact.
    polluted = dict(manifest)
    polluted["config"] = {
        **manifest["config"],
        "api_token": "super-secret-value",
        "broker_account_id": "DU0001",
    }
    written = write_manifest(polluted, run_dir / "manifest_redacted.json")
    text = (run_dir / "manifest_redacted.json").read_text(encoding="utf-8")
    assert "super-secret-value" not in text
    assert "DU0001" not in text
    assert written["config"]["api_token"] == "[REDACTED]"
    assert written["config"]["broker_account_id"] == "[REDACTED]"


def test_missing_data_fails_on_replay(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_csv(data, "SPY", n_bars=40)
    run_dir = tmp_path / "orig"
    m1 = produce_named_backtest_run(
        run_dir=run_dir,
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data,
        policy_path=RESEARCH_POLICY,
        seed=2,
    )
    (data / "SPY.csv").unlink()
    with pytest.raises(ReproError) as exc:
        replay_from_manifest(m1, run_dir=tmp_path / "replay", data_dir=data)
    assert exc.value.reason is CompareReason.MISSING_DATA


def test_path_free_identity_same_content_different_dirs(tmp_path: Path) -> None:
    """Identical CSV bytes + params under two data_dir paths must compare equal."""

    data_a = tmp_path / "path_a"
    data_b = tmp_path / "path_b"
    _write_csv(data_a, "SPY", n_bars=50)
    data_b.mkdir(parents=True)
    (data_b / "SPY.csv").write_bytes((data_a / "SPY.csv").read_bytes())

    m1 = produce_named_backtest_run(
        run_dir=tmp_path / "run_path_a",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data_a,
        policy_path=RESEARCH_POLICY,
        seed=4,
        slippage_bps=0.0,
        date_start="2019-01-02",
        date_end="2019-03-01",
    )
    m2 = produce_named_backtest_run(
        run_dir=tmp_path / "run_path_b",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=data_b,
        policy_path=RESEARCH_POLICY,
        seed=4,
        slippage_bps=0.0,
        date_start="2019-01-02",
        date_end="2019-03-01",
        code_commit=m1["code_commit"],
    )
    assert m1["output_fingerprint"] == m2["output_fingerprint"]
    assert m1["config_hash"] == m2["config_hash"]
    assert m1["datasets"][0]["sha256"] == m2["datasets"][0]["sha256"]
    # Paths may differ in stored metadata; identity must not.
    assert m1["datasets"][0]["path"] != m2["datasets"][0]["path"]
    report = compare_manifests(m1, m2)
    assert report.ok is True, report.to_dict()
    assert CompareReason.CONFIG_DRIFT not in report.reasons


def test_replay_data_dir_override_when_original_path_gone(tmp_path: Path) -> None:
    """Original path deleted; --data-dir override with same content must pass."""

    original = tmp_path / "original_data"
    _write_csv(original, "SPY", n_bars=50)
    m1 = produce_named_backtest_run(
        run_dir=tmp_path / "orig_run",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=original,
        policy_path=RESEARCH_POLICY,
        seed=6,
        slippage_bps=0.0,
    )
    # Move content to a new location and remove the original path entirely.
    override = tmp_path / "override_data"
    override.mkdir()
    (override / "SPY.csv").write_bytes((original / "SPY.csv").read_bytes())
    import shutil

    shutil.rmtree(original)
    assert not (original / "SPY.csv").exists()

    m2 = replay_from_manifest(
        m1,
        run_dir=tmp_path / "replay_override",
        data_dir=override,
        policy_path=RESEARCH_POLICY,
    )
    report = compare_manifests(m1, m2)
    assert report.ok is True, report.to_dict()
    assert m1["output_fingerprint"] == m2["output_fingerprint"]


def test_replay_data_dir_override_rejects_different_content(tmp_path: Path) -> None:
    """Override data_dir with different bytes must fail closed at verify (not silent wrong run)."""

    original = tmp_path / "orig"
    _write_csv(original, "SPY", n_bars=50)
    m1 = produce_named_backtest_run(
        run_dir=tmp_path / "r1",
        strategy_id="baseline_buy_hold",
        symbol="SPY",
        data_dir=original,
        policy_path=RESEARCH_POLICY,
        seed=8,
        slippage_bps=0.0,
    )
    # Keep original path intact (matching sha) but provide a bad override.
    bad_override = tmp_path / "bad"
    _write_csv(bad_override, "SPY", n_bars=50, start=date(2020, 1, 2))  # different content
    assert (original / "SPY.csv").exists()

    with pytest.raises(ReproError) as exc:
        replay_from_manifest(
            m1,
            run_dir=tmp_path / "r2",
            data_dir=bad_override,
            policy_path=RESEARCH_POLICY,
        )
    assert exc.value.reason is CompareReason.CHECKSUM_DRIFT
    # And verify_datasets alone must also fail against the override, not the original.
    from chronos.research.repro import verify_datasets

    with pytest.raises(ReproError) as vexc:
        verify_datasets(m1, data_dir=bad_override)
    assert vexc.value.reason is CompareReason.CHECKSUM_DRIFT


def test_cli_repro_produce_replay_compare(tmp_path: Path) -> None:
    from chronos.cli.main import build_parser

    data = tmp_path / "data"
    _write_csv(data, "SPY", n_bars=50)
    run_a = tmp_path / "cli_a"
    run_b = tmp_path / "cli_b"
    parser = build_parser()
    args = parser.parse_args(
        [
            "research",
            "repro",
            "produce",
            "--run-dir",
            str(run_a),
            "--strategy",
            "baseline_buy_hold",
            "--symbol",
            "SPY",
            "--data-dir",
            str(data),
            "--policy",
            str(RESEARCH_POLICY),
            "--seed",
            "5",
            "--slippage-bps",
            "0",
        ]
    )
    assert args.func(args) == 0
    assert (run_a / "manifest.json").exists()

    args_replay = parser.parse_args(
        [
            "research",
            "repro",
            "replay",
            "--manifest",
            str(run_a / "manifest.json"),
            "--run-dir",
            str(run_b),
            "--data-dir",
            str(data),
            "--policy",
            str(RESEARCH_POLICY),
        ]
    )
    assert args_replay.func(args_replay) == 0

    args_cmp = parser.parse_args(
        [
            "research",
            "repro",
            "compare",
            "--expected",
            str(run_a / "manifest.json"),
            "--actual",
            str(run_b / "manifest.json"),
        ]
    )
    assert args_cmp.func(args_cmp) == 0

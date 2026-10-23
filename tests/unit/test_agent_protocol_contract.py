from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from tests.support.forged_ancestry import ARMS, DEAD, plant_forged_parent, raw_git

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "docs" / "AGENT_PROTOCOL.md"


def _protocol_text() -> str:
    return PROTOCOL.read_text(encoding="utf-8")


def test_post_merge_proof_starts_from_the_merge_result() -> None:
    text = _protocol_text()
    required_fragments = (
        "baseRefName,headRefOid,mergeCommit,state",
        "git merge-base --is-ancestor <merge-result-sha> origin/<default>",
        "exact default-branch CI",
    )

    for fragment in required_fragments:
        assert fragment in text, f"agent protocol omits merge-result proof: {fragment}"


def test_merge_strategy_api_claims_cite_official_github_sources() -> None:
    text = _protocol_text()
    required_sources = (
        "https://docs.github.com/en/graphql/reference/pulls#pullrequest",
        "https://docs.github.com/en/pull-requests/reference/pull-request-merges",
    )

    for source in required_sources:
        assert source in text, f"agent protocol omits official GitHub source: {source}"


def test_rewritten_candidates_use_changed_path_equality() -> None:
    text = _protocol_text()
    required_fragments = (
        "squash",
        "rebase",
        "candidate ancestry is expected to fail",
        "git diff --exit-code <candidate-sha> <merge-result-sha> -- <changed-paths>",
        "gh api repos/Ayyitskevin/Chronos/pulls/<n>/files",
        ".previous_filename // empty",
    )

    for fragment in required_fragments:
        assert fragment in text, f"agent protocol omits rewritten-candidate proof: {fragment}"


def test_preserved_candidates_still_require_ancestry() -> None:
    text = _protocol_text()

    assert "git merge-base --is-ancestor <candidate-sha> <merge-result-sha>" in text


def test_approved_stacks_retarget_each_descendant_before_merge() -> None:
    text = " ".join(_protocol_text().split())

    assert "Stacked review may be owner-approved; stacked landing is not" in text
    assert "retarget every descendant to the live default before merging it" in text


def test_protocol_does_not_claim_candidate_ancestry_is_universal() -> None:
    text = _protocol_text()
    forbidden_patterns = (
        r"after the final merge,\s+prove ancestry.*?for every PR in the stack",
        r"every merge\s+is verified by ancestry",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is None, (
            f"agent protocol still treats candidate ancestry as universal: {pattern}"
        )


# D-44: scripts/post_merge_proof.sh, the executable form of §6, reads ancestry four times
# (§6.2 on-main, §6.3 identity, the PR base and the merge commit's parent for the patch
# comparison). Each read must come from the commit objects themselves, as gate 30's does, so a
# forged commit-graph (in the clone or behind GIT_ALTERNATE_OBJECT_DIRECTORIES), a grafts file or
# a replace ref cannot change the proof's verdict. Topology, built per test:
#   B0 -> H1 -> H (the reviewed PR: H1 adds `a`, H adds `b` to p)
#   B0 -> X0 -> X -> MR (main; X0 and X touch only q; MR is the merge result)
#   B0 -> Y (side: p gains `evil`)      B0 -> S (stranded)
PROOF = ROOT / "scripts" / "post_merge_proof.sh"
_GH_STUB = """#!/usr/bin/env bash
case "$*" in
  "repo view"*) echo o/r ;;
  "pr view"*) printf 'MERGED\\tmain\\t%s\\t%s\\n' "$STUB_HEAD" "$STUB_MERGE" ;;
  *"/pulls/7/files"*) printf 'p\\tmodified\\t\\n' ;;
  *"/check-runs"*) printf 'ci\\tcompleted\\tsuccess\\n' ;;
  *) exit 1 ;;
esac
"""
_DROPPED_A = "l1\nl2\nb\n"
_WITH_EVIL = "l1\nl2\na\nb\nevil\n"
_PATCH_DIFFERS = "  FAIL  p: the merge commit's patch is not the PR's patch"


def _proof_lane(tmp_path: Path, merge_result_p: str) -> tuple[Path, dict[str, str]]:
    origin, lane = tmp_path / "origin.git", tmp_path / "lane"
    subprocess.run(("git", "init", "-q", "--bare", "-b", "main", str(origin)), check=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(lane)), check=True)

    def git(*args: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(lane), "-c", "user.name=t", "-c", "user.email=t@t", *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit(name: str, **files: str) -> str:
        for path, text in files.items():
            (lane / path).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", name)
        return git("rev-parse", "HEAD")

    shas = {"B0": commit("B0", p="l1\nl2\n", q="q0\n")}
    git("switch", "-q", "-c", "pr")
    shas["H1"] = commit("H1", p="l1\nl2\na\n")
    shas["H"] = commit("H", p="l1\nl2\na\nb\n")
    git("switch", "-q", "main")
    shas["X0"] = commit("X0", q="q1\n")
    shas["X"] = commit("X", q="q2\n")
    shas["MR"] = commit("MR", p=merge_result_p)
    git("switch", "-q", "-c", "side", shas["B0"])
    shas["Y"] = commit("Y", p="l1\nl2\nevil\n")
    git("switch", "-q", "-c", "stranded", shas["B0"])
    shas["S"] = commit("S", s="s\n")
    git("switch", "-q", "main")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main", "pr", "side", "stranded")
    git("fetch", "-q", "origin")
    return lane, shas


def _run_proof(tmp_path: Path, lane: Path, head: str, merge: str, env: dict[str, str]) -> str:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "gh").write_text(_GH_STUB, encoding="utf-8")
    (stub / "gh").chmod(0o755)
    result = subprocess.run(
        ("bash", str(PROOF), "7", head, "p"),
        cwd=lane,
        env={
            **os.environ,
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "STUB_HEAD": head,
            "STUB_MERGE": merge,
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.stdout


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("arm", ARMS)
def test_post_merge_proof_on_main_check_reads_ancestry_from_commit_objects(
    tmp_path: Path, arm: str
) -> None:
    lane, c = _proof_lane(tmp_path, _DROPPED_A)
    raw = ("merge-base", "--is-ancestor", c["S"], "origin/main")
    assert raw_git(lane, {}, *raw).returncode == 1
    env = plant_forged_parent(lane, arm, target=c["X"], forged_parent=c["S"])
    if raw_git(lane, env, *raw).returncode != 0:
        pytest.fail(f"{DEAD} (arm {arm}: S still not an ancestor of origin/main)")

    out = _run_proof(tmp_path, lane, c["S"], c["S"], env)

    assert f"  FAIL  {c['S']} is NOT an ancestor of origin/main — the result is stranded" in out
    assert f"  ok    {c['S']} is an ancestor of origin/main" not in out


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("arm", ARMS)
def test_post_merge_proof_identity_check_reads_ancestry_from_commit_objects(
    tmp_path: Path, arm: str
) -> None:
    lane, c = _proof_lane(tmp_path, _DROPPED_A)
    raw = ("merge-base", "--is-ancestor", c["H"], c["MR"])
    assert raw_git(lane, {}, *raw).returncode == 1
    env = plant_forged_parent(lane, arm, target=c["X"], forged_parent=c["H"])
    if raw_git(lane, env, *raw).returncode != 0:
        pytest.fail(f"{DEAD} (arm {arm}: H still not an ancestor of MR)")

    out = _run_proof(tmp_path, lane, c["H"], c["MR"], env)

    assert "identity preserved" not in out
    assert "identity rewritten (squash or rebase)" in out
    assert _PATCH_DIFFERS in out


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("arm", ARMS)
def test_post_merge_proof_pr_base_reads_ancestry_from_commit_objects(
    tmp_path: Path, arm: str
) -> None:
    lane, c = _proof_lane(tmp_path, _DROPPED_A)
    raw = ("merge-base", c["H"], f"{c['MR']}^")
    assert raw_git(lane, {}, *raw).stdout.strip() == c["B0"]
    env = plant_forged_parent(lane, arm, target=c["X0"], forged_parent=c["H1"])
    if raw_git(lane, env, *raw).stdout.strip() != c["H1"]:
        pytest.fail(f"{DEAD} (arm {arm}: the PR base is still B0)")

    out = _run_proof(tmp_path, lane, c["H"], c["MR"], env)

    assert _PATCH_DIFFERS in out
    assert "ok (adjacent landing)" not in out


# The graph arms are absent here on purpose: MR is named on the command line, so git parses it
# from its object and a forged graph cannot move MR^ (measured on git 2.53; the A1 probe log).
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("arm", ("grafts", "replace"))
def test_post_merge_proof_merge_parent_reads_ancestry_from_commit_objects(
    tmp_path: Path, arm: str
) -> None:
    lane, c = _proof_lane(tmp_path, _WITH_EVIL)
    raw = ("rev-parse", f"{c['MR']}^")
    assert raw_git(lane, {}, *raw).stdout.strip() == c["X"]
    env = plant_forged_parent(lane, arm, target=c["MR"], forged_parent=c["Y"])
    if raw_git(lane, env, *raw).stdout.strip() != c["Y"]:
        pytest.fail(f"{DEAD} (arm {arm}: MR^ is still X)")

    out = _run_proof(tmp_path, lane, c["H"], c["MR"], env)

    assert _PATCH_DIFFERS in out
    assert "ok (adjacent landing)" not in out
